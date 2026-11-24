#!/usr/bin/env python3
"""Classify queued LinkedIn jobs as Easy Apply vs external.

Visiting each posting costs one page load and no LLM tokens, so it is much
cheaper than tailoring first and discovering the posting only offers
"Apply on company website" (which LinkedIn never resolves to the real ATS).

Writes the verdict to job.description_md? No — that would corrupt content.
Instead it sets job.ats_type to "linkedin" for Easy Apply and leaves
external ones untouched, and prints a summary the caller can act on.

Usage (inside the container):
    python scripts/classify_linkedin.py --limit 30
"""

from __future__ import annotations

import argparse
import asyncio

import structlog
from sqlmodel import select

from jobagent.apply.browser import new_page
from jobagent.db.engine import get_session
from jobagent.db.models import Job

log = structlog.get_logger(__name__)

EASY = "easy_apply"
EXTERNAL = "external"
APPLIED = "applied"
CLOSED = "closed"
LINKEDIN_FORM = "linkedin_form"
UNKNOWN = "unknown"


async def _classify(page, url: str) -> str:  # type: ignore[no-untyped-def]
    """Classify one LinkedIn posting.

    LinkedIn has four distinct apply states, and only the first is automatable
    with the Easy Apply modal:

      EASY           "Easy Apply" button — in-LinkedIn modal
      LINKEDIN_FORM  plain "Apply" + "Responses managed off LinkedIn" — the form
                     is on LinkedIn but posts to the employer's ATS; there is no
                     modal and no exposed ATS URL
      EXTERNAL       "Apply on company website" — leaves LinkedIn entirely
      APPLIED/CLOSED already submitted / not accepting applications
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=40_000)
    except Exception:
        return UNKNOWN
    await asyncio.sleep(2.5)

    try:
        body = (await page.inner_text("body"))[:4000].lower()
    except Exception:
        body = ""

    # Checked first: once applied, LinkedIn replaces the buttons with status.
    if "application submitted" in body or "you applied" in body:
        return APPLIED
    if (
        "not currently accepting applications" in body
        or "no longer accepting applications" in body
    ):
        return CLOSED

    # Lazy rendering: buttons can sit below the fold.
    for _ in range(3):
        try:
            if await page.locator("button:has-text('Easy Apply')").count() > 0:
                return EASY
        except Exception:
            pass
        try:
            await page.mouse.wheel(0, 900)
        except Exception:
            break
        await asyncio.sleep(1.2)

    try:
        body = (await page.inner_text("body"))[:5000].lower()
    except Exception:
        return UNKNOWN

    if "apply on company" in body or "company website" in body:
        return EXTERNAL

    # A bare "Apply" control with responses managed off LinkedIn: the form runs
    # on LinkedIn but is not the Easy Apply modal, and the destination ATS is
    # never exposed. Treated as not automatable, but it is a live posting — so
    # it is reported separately rather than as "closed".
    if "responses managed off linkedin" in body:
        return LINKEDIN_FORM

    return UNKNOWN


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    with get_session() as s:
        rows = s.exec(
            select(Job)
            .where(Job.status == "queued", Job.ats_type == "linkedin")
            .order_by(Job.score.desc())
            .limit(args.limit)
        ).all()
        targets = [(j.id, j.url or j.apply_url or "", j.score) for j in rows]

    easy: list[tuple[int, int]] = []
    external_ids: list[int] = []
    applied_ids: list[int] = []
    closed_ids: list[int] = []
    form_ids: list[int] = []
    ext = 0
    unk = 0

    async with new_page("linkedin") as page:
        for jid, url, score in targets:
            if "linkedin.com/jobs" not in url:
                unk += 1
                continue
            verdict = await _classify(page, url)
            if verdict == EASY:
                easy.append((jid, score))
                print(f"  EASY APPLY  {jid}  score={score}")
            elif verdict == EXTERNAL:
                ext += 1
                external_ids.append(jid)
                print(f"  external    {jid}")
            elif verdict == APPLIED:
                applied_ids.append(jid)
                print(f"  already applied  {jid}")
            elif verdict == CLOSED:
                closed_ids.append(jid)
                print(f"  closed           {jid}")
            elif verdict == LINKEDIN_FORM:
                form_ids.append(jid)
                print(f"  linkedin-form    {jid}")
            else:
                unk += 1
                print(f"  unknown     {jid}")
            await asyncio.sleep(2)

    print()
    print(
        f"checked={len(targets)} easy_apply={len(easy)} "
        f"external={ext} already_applied={len(applied_ids)} "
        f"closed={len(closed_ids)} linkedin_form={len(form_ids)} "
        f"unknown={unk}"
    )
    if easy:
        print("easy_apply job ids:", [j for j, _ in easy])

    # Retire confirmed-external postings so later runs stop retrying them.
    # LinkedIn never exposes the underlying ATS, so these can never be applied
    # to automatically. Unknowns are left queued for a future re-check.
    if external_ids:
        with get_session() as s:
            for jid in external_ids:
                j = s.get(Job, jid)
                if j and j.status == "queued":
                    j.status = "filtered_out"
                    s.add(j)
            s.commit()
        print(f"retired {len(external_ids)} external postings (filtered_out)")

    # linkedin_form postings are live but not automatable by any current
    # handler (no modal, no exposed ATS URL). Leaving them queued means every
    # future run re-visits the same unresolvable postings forever, since they
    # sort highest by score and never change state. Retire them the same way
    # as external.
    if form_ids:
        with get_session() as s:
            for jid in form_ids:
                j = s.get(Job, jid)
                if j and j.status == "queued":
                    j.status = "filtered_out"
                    s.add(j)
            s.commit()
        print(f"retired {len(form_ids)} linkedin_form postings (filtered_out)")

    # Already-applied postings: mark the job applied so the pipeline stops
    # considering them. This is how LinkedIn reports a job you applied to
    # outside the agent (or before a DB reset).
    if applied_ids:
        with get_session() as s:
            for jid in applied_ids:
                j = s.get(Job, jid)
                if j and j.status == "queued":
                    j.status = "applied"
                    s.add(j)
            s.commit()
        print(f"marked {len(applied_ids)} already-applied postings as applied")

    if closed_ids:
        with get_session() as s:
            for jid in closed_ids:
                j = s.get(Job, jid)
                if j and j.status == "queued":
                    j.status = "filtered_out"
                    s.add(j)
            s.commit()
        print(f"retired {len(closed_ids)} closed postings (filtered_out)")


if __name__ == "__main__":
    asyncio.run(main())
