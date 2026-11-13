"""Lever public ATS collector.

API: ``https://api.lever.co/v0/postings/{company}?mode=json``

Returns a JSON array of postings directly (no pagination needed – Lever
returns all active postings in one call).  Boards come from the
``ats_board`` registry and poll results are written back.  ``fetch()``
never raises.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob
from jobagent.discovery.boards import BoardOutcome, BoardRef, outcome_from_status
from jobagent.discovery.company_universe import CompanyUniverse

log = structlog.get_logger(__name__)

_API_URL = "https://api.lever.co/v0/postings/{company}?mode=json"
_TIMEOUT = 15.0
_CONCURRENCY = 20


def _matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


class LeverCollector(CollectorBase):
    name = "lever"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        universe = CompanyUniverse()
        boards = universe.get_boards("lever")
        if not boards:
            log.info("lever.no_boards")
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
            log.exception("lever.record_outcomes_failed")
        log.info("lever.done", boards=len(boards), total=len(results))
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
                resp = await client.get(_API_URL.format(company=board.slug))
            except httpx.HTTPError as exc:
                log.warning("lever.fetch_error", slug=board.slug, error=str(exc))
                return [], BoardOutcome("error", error=str(exc), polled=True)
        if (o := outcome_from_status(resp.status_code)) is not None:
            log.warning("lever.fetch_error", slug=board.slug, status=resp.status_code)
            return [], BoardOutcome(o, error=f"HTTP {resp.status_code}", polled=True)
        try:
            postings: list[dict[str, Any]] = resp.json()
        except ValueError as exc:
            return [], BoardOutcome("error", error=f"bad JSON: {exc}", polled=True)
        if not isinstance(postings, list):
            return [], BoardOutcome("error", error="unexpected payload", polled=True)

        jobs: list[RawJob] = []
        for posting in postings:
            if not _matches(posting.get("text", ""), keywords):
                continue
            try:
                jobs.append(_parse_posting(posting, board.slug, board.company_name))
            except Exception as exc:
                log.warning(
                    "lever.parse_error", slug=board.slug, posting_id=posting.get("id"), error=str(exc)
                )
        return jobs, BoardOutcome(
            "ok", job_count=len(postings), matched_count=len(jobs), polled=True
        )


def _parse_posting(
    posting: dict[str, Any], slug: str, company_name: str | None = None
) -> RawJob:
    hosted_url: str = posting.get("hostedUrl", "")
    apply_url: str = posting.get("applyUrl") or hosted_url

    posted_at: datetime | None = None
    created_ms: int | None = posting.get("createdAt")
    if created_ms:
        try:
            posted_at = datetime.fromtimestamp(
                int(created_ms) / 1000, tz=UTC
            )
        except (ValueError, TypeError, OSError):
            pass

    cats: dict[str, Any] = posting.get("categories", {})

    extra: dict[str, Any] = {
        "slug": slug,
        "team": cats.get("team"),
        "commitment": cats.get("commitment"),
        "department": cats.get("department"),
        "tags": posting.get("tags", []),
    }

    return RawJob(
        source="lever",
        source_job_id=posting["id"],
        url=hosted_url,
        company=company_name or slug,
        title=posting.get("text", ""),
        apply_url=apply_url,
        location_raw=cats.get("location"),
        description_html=posting.get("description"),
        description_md=posting.get("descriptionPlain"),
        posted_at=posted_at,
        extra=extra,
    )
