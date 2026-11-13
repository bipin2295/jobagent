"""Ashby public job-board collector.

API: ``https://api.ashbyhq.com/posting-api/job-board/{name}``

Returns JSON ``{"apiVersion": ..., "jobs": [...]}``.  Each job has ``title``,
``location``, ``jobUrl``, ``applyUrl``, ``publishedAt``, ``descriptionHtml``,
``descriptionPlain``, ``employmentType``, ``workplaceType``, ``isRemote``.
Unlisted postings (``isListed: false``) are skipped.

Boards come from the ``ats_board`` registry and poll results are written
back.  ``fetch()`` never raises.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob
from jobagent.discovery.boards import BoardOutcome, BoardRef, outcome_from_status
from jobagent.discovery.company_universe import CompanyUniverse

log = structlog.get_logger(__name__)

_API_URL = "https://api.ashbyhq.com/posting-api/job-board/{name}"
_TIMEOUT = 15.0
_CONCURRENCY = 20


def _matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


class AshbyCollector(CollectorBase):
    name = "ashby"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        universe = CompanyUniverse()
        boards = universe.get_boards("ashby")
        if not boards:
            log.info("ashby.no_boards")
            return []

        keywords: list[str] = search_cfg.get("titles", [])
        sem = asyncio.Semaphore(_CONCURRENCY)
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            batch_results = await asyncio.gather(
                *(self._fetch_board(client, sem, b, keywords) for b in boards)
            )

        results: list[RawJob] = []
        outcomes: list[tuple[int, BoardOutcome]] = []
        for board, (jobs, outcome) in zip(boards, batch_results, strict=False):
            results.extend(jobs)
            outcomes.append((board.id, outcome))
        try:
            universe.record_outcomes(outcomes)
        except Exception:
            log.exception("ashby.record_outcomes_failed")
        log.info("ashby.done", boards=len(boards), total=len(results))
        return results

    async def _fetch_board(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        board: BoardRef,
        keywords: list[str],
    ) -> tuple[list[RawJob], BoardOutcome]:
        async with sem:
            try:
                resp = await client.get(_API_URL.format(name=board.slug))
            except httpx.HTTPError as exc:
                log.warning("ashby.fetch_error", name=board.slug, error=str(exc))
                return [], BoardOutcome("error", error=str(exc), polled=True)
        if (o := outcome_from_status(resp.status_code)) is not None:
            log.warning("ashby.fetch_error", name=board.slug, status=resp.status_code)
            return [], BoardOutcome(o, error=f"HTTP {resp.status_code}", polled=True)
        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            return [], BoardOutcome("error", error=f"bad JSON: {exc}", polled=True)

        # "jobs" is the current key; "jobPostings" kept for older payloads
        postings = [
            p for p in (data.get("jobs") or data.get("jobPostings") or [])
            if p.get("isListed", True)
        ]
        jobs: list[RawJob] = []
        for posting in postings:
            if not _matches(posting.get("title", ""), keywords):
                continue
            try:
                jobs.append(_parse_posting(posting, board.slug, board.company_name))
            except Exception as exc:
                log.warning(
                    "ashby.parse_error", name=board.slug, posting_id=posting.get("id"), error=str(exc)
                )
        return jobs, BoardOutcome(
            "ok", job_count=len(postings), matched_count=len(jobs), polled=True
        )


def _parse_posting(
    posting: dict[str, Any], name: str, company_name: str | None = None
) -> RawJob:
    pid: str = posting["id"]
    job_url: str = posting.get("jobUrl") or f"https://jobs.ashbyhq.com/{name}/{pid}"
    apply_url: str = posting.get("applyUrl") or posting.get("applyLink") or job_url

    posted_at: datetime | None = None
    published_raw: str | None = posting.get("publishedAt")
    if published_raw:
        try:
            posted_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    workplace = (posting.get("workplaceType") or "").lower()
    remote_type: str | None = None
    if posting.get("isRemote") or workplace == "remote":
        remote_type = "remote"
    elif workplace == "hybrid":
        remote_type = "hybrid"
    elif workplace in ("onsite", "on-site"):
        remote_type = "onsite"

    extra: dict[str, Any] = {
        "company_slug": name,
        "department": posting.get("department"),
        "team": posting.get("team"),
        "secondary_locations": posting.get("secondaryLocations", []),
    }

    return RawJob(
        source="ashby",
        source_job_id=pid,
        url=job_url,
        company=company_name or name,
        title=posting.get("title", ""),
        apply_url=apply_url,
        location_raw=posting.get("location") or posting.get("locationName"),
        remote_type=remote_type,
        description_html=posting.get("descriptionHtml"),
        description_md=posting.get("descriptionPlain"),
        posted_at=posted_at,
        employment_type=posting.get("employmentType"),
        extra=extra,
    )
