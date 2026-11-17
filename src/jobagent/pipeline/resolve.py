"""Resolve aggregator apply URLs to the real ATS.

Adzuna/Jooble (and similar feeds) point at their own redirect pages, so their
jobs have ``ats_type=None``. Tailoring only considers jobs whose ATS is in
``guardrails.active_ats``, which meant those jobs were never tailored or
applied to — even when the underlying posting is a Greenhouse/Lever/Ashby role.

This stage follows the redirect (HTTP first) and, failing that, scans the page
HTML for a direct ATS link. It records the detected ATS and the real apply URL
so the jobs flow through the normal pipeline.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin

import httpx
import structlog
from sqlmodel import select

from jobagent.db.engine import get_session
from jobagent.db.models import Job
from jobagent.pipeline.ats_detect import detect_ats

log = structlog.get_logger(__name__)

_AGGREGATOR_SOURCES = frozenset(
    {"jooble", "adzuna", "reed", "jobspy", "linkedin", "indeed", "bayt"}
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_ATS_HREF = re.compile(
    r"https?://[^\s\"'<>\\]*(?:"
    r"greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|myworkday\.com|"
    r"smartrecruiters\.com|workable\.com"
    r")[^\s\"'<>\\]*",
    re.IGNORECASE,
)

_CONCURRENCY = 6


def _ats_links(html: str | None, base: str) -> list[str]:
    if not html:
        return []
    out: list[str] = []
    for m in _ATS_HREF.finditer(html):
        url = urljoin(base, m.group(0))
        if url not in out:
            out.append(url)
        if len(out) >= 5:
            break
    return out


async def _resolve_one_http(
    client: httpx.AsyncClient, job: Job
) -> tuple[str | None, str | None]:
    target = job.apply_url or job.url
    if not target:
        return None, None
    try:
        resp = await client.get(target, follow_redirects=True)
    except Exception:
        return None, None

    final = str(resp.url)
    ats = detect_ats(final)
    if ats and ats != "unknown":
        return ats, final

    for link in _ats_links(resp.text, final):
        ats = detect_ats(link)
        if ats and ats != "unknown":
            return ats, link
    return None, None


def _candidates(limit: int) -> list[int]:
    with get_session() as s:
        stmt = (
            select(Job)
            .where(Job.ats_type.is_(None))  # type: ignore[union-attr]
            .where(Job.apply_url.is_not(None))  # type: ignore[union-attr]
            .where(Job.source.in_(_AGGREGATOR_SOURCES))  # type: ignore[attr-defined]
            .order_by(Job.score.desc())
            .limit(limit)
        )
        return [j.id for j in s.exec(stmt).all() if j.id is not None]


def _save(job_id: int, ats: str, apply_url: str) -> None:
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        job.ats_type = ats
        job.apply_url = apply_url
        s.add(job)
        s.commit()


async def _resolve_browser(job: Job) -> str | None:
    """Fallback: navigate the aggregator page and click through to the ATS."""
    try:
        from jobagent.apply.orchestrator import (  # noqa: PLC0415
            _resolve_ats_via_redirect,
        )

        return await _resolve_ats_via_redirect(job)
    except Exception as exc:
        log.debug("resolve.browser_error", job_id=job.id, error=str(exc)[:80])
        return None


async def resolve_pending(
    limit: int = 150,
    browser_limit: int = 12,
) -> tuple[int, int]:
    """Resolve up to *limit* aggregator jobs. Returns (resolved, attempted).

    HTTP is tried for every candidate (fast). Jobs that need JavaScript are
    retried with a browser for at most *browser_limit* of them (slow).
    """
    job_ids = _candidates(limit)
    if not job_ids:
        log.info("resolve.none_pending")
        return 0, 0

    sem = asyncio.Semaphore(_CONCURRENCY)
    resolved_ids: set[int] = set()

    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
    ) as client:

        async def work(job_id: int) -> None:
            with get_session() as s:
                job = s.get(Job, job_id)
            if job is None:
                return
            async with sem:
                ats, url = await _resolve_one_http(client, job)
            if ats and url:
                _save(job_id, ats, url)
                resolved_ids.add(job_id)
                log.info("resolve.found", job_id=job_id, ats=ats, method="http", url=url[:80])

        await asyncio.gather(*(work(jid) for jid in job_ids))

    unresolved = [jid for jid in job_ids if jid not in resolved_ids]
    for jid in unresolved[:browser_limit]:
        with get_session() as s:
            job = s.get(Job, jid)
        if job is None:
            continue
        ats = await _resolve_browser(job)
        if ats:
            resolved_ids.add(jid)
            log.info("resolve.found", job_id=jid, ats=ats, method="browser")

    resolved = len(resolved_ids)
    log.info(
        "resolve.complete",
        attempted=len(job_ids),
        resolved=resolved,
        unverified=len(job_ids) - resolved,
        browser_attempts=min(len(unresolved), browser_limit),
    )
    return resolved, len(job_ids)
