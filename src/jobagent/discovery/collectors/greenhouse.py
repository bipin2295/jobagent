"""Greenhouse public ATS collector.

API: ``https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``

Boards come from the ``ats_board`` registry
(``CompanyUniverse().get_boards("greenhouse")``).  Each poll result (job count,
404, error) is written back to the registry, so candidates are promoted to
``live``/``empty`` and vanished boards are eventually marked ``dead``.
``fetch()`` never raises – errors are logged and partial results are returned.
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
from jobagent.pipeline.normalise import detect_country

log = structlog.get_logger(__name__)

_API_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
_CONCURRENCY = 20
_BATCH_SIZE = 20
_BATCH_SLEEP = 0.1
_TIMEOUT = 20.0


def _matches(title: str, keywords: list[str]) -> bool:
    """Return True when *title* contains any keyword (case-insensitive)."""
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


class GreenhouseCollector(CollectorBase):
    name = "greenhouse"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        universe = CompanyUniverse()
        boards = universe.get_boards("greenhouse")
        if not boards:
            log.info("greenhouse.no_boards")
            return []

        keywords: list[str] = search_cfg.get("titles", [])
        results: list[RawJob] = []
        outcomes: list[tuple[int, BoardOutcome]] = []
        sem = asyncio.Semaphore(_CONCURRENCY)

        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            for start in range(0, len(boards), _BATCH_SIZE):
                batch = boards[start : start + _BATCH_SIZE]
                batch_results = await asyncio.gather(
                    *(self._fetch_board(client, sem, b, keywords) for b in batch)
                )
                for board, (jobs, outcome) in zip(batch, batch_results, strict=False):
                    results.extend(jobs)
                    outcomes.append((board.id, outcome))
                if start + _BATCH_SIZE < len(boards):
                    await asyncio.sleep(_BATCH_SLEEP)

        try:
            universe.record_outcomes(outcomes)
        except Exception:
            log.exception("greenhouse.record_outcomes_failed")
        log.info("greenhouse.done", boards=len(boards), total=len(results))
        return results

    async def _fetch_board(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        board: BoardRef,
        keywords: list[str],
    ) -> tuple[list[RawJob], BoardOutcome]:
        url = _API_URL.format(token=board.slug)
        async with sem:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                log.warning("greenhouse.fetch_error", slug=board.slug, error=str(exc))
                return [], BoardOutcome("error", error=str(exc), polled=True)
        if (o := outcome_from_status(resp.status_code)) is not None:
            log.warning("greenhouse.fetch_error", slug=board.slug, status=resp.status_code)
            return [], BoardOutcome(o, error=f"HTTP {resp.status_code}", polled=True)
        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            return [], BoardOutcome("error", error=f"bad JSON: {exc}", polled=True)

        all_jobs = data.get("jobs", [])
        jobs: list[RawJob] = []
        company_name = board.company_name
        for job in all_jobs:
            company_name = company_name or job.get("company_name")
            if not _matches(job.get("title", ""), keywords):
                continue
            try:
                jobs.append(_parse_job(job, board.slug, company_name))
            except Exception as exc:
                log.warning(
                    "greenhouse.parse_error", slug=board.slug, job_id=job.get("id"), error=str(exc)
                )
        outcome = BoardOutcome(
            "ok",
            job_count=len(all_jobs),
            matched_count=len(jobs),
            company_name=company_name if not board.company_name else None,
            polled=True,
        )
        return jobs, outcome


_OFFICE_COUNTRY_MAP: dict[str, str] = {
    # Europe
    "germany": "DE", "deutschland": "DE", "berlin": "DE", "munich": "DE",
    "frankfurt": "DE", "hamburg": "DE", "amsterdam": "NL", "netherlands": "NL",
    "london": "GB", "united kingdom": "GB", "england": "GB", "uk": "GB",
    "france": "FR", "paris": "FR", "spain": "ES", "madrid": "ES",
    "sweden": "SE", "stockholm": "SE", "norway": "NO", "oslo": "NO",
    "denmark": "DK", "finland": "FI", "ireland": "IE", "dublin": "IE",
    "switzerland": "CH", "zurich": "CH", "austria": "AT", "vienna": "AT",
    "poland": "PL", "warsaw": "PL", "czech": "CZ", "prague": "CZ",
    "portugal": "PT", "lisbon": "PT", "italy": "IT", "milan": "IT",
    "belgium": "BE", "brussels": "BE", "luxembourg": "LU",
    "estonia": "EE", "tallinn": "EE", "latvia": "LV", "lithuania": "LT",
    "romania": "RO", "bulgaria": "BG", "sofia": "BG", "croatia": "HR",
    # Middle East
    "dubai": "AE", "abu dhabi": "AE", "uae": "AE",
    "saudi": "SA", "riyadh": "SA", "doha": "QA", "qatar": "QA",
    # APAC
    "singapore": "SG", "australia": "AU", "sydney": "AU", "melbourne": "AU",
    "new zealand": "NZ", "auckland": "NZ", "japan": "JP", "tokyo": "JP",
    "hong kong": "HK", "malaysia": "MY", "kuala lumpur": "MY",
    "thailand": "TH", "vietnam": "VN",
    # India
    "india": "IN", "bengaluru": "IN", "bangalore": "IN", "hyderabad": "IN",
    "pune": "IN", "mumbai": "IN", "chennai": "IN", "delhi": "IN",
    "gurugram": "IN", "gurgaon": "IN",
    # Canada
    "canada": "CA", "toronto": "CA", "vancouver": "CA", "montreal": "CA",
    "waterloo": "CA", "ottawa": "CA",
    # US (for rejection)
    "united states": "US", "new york": "US", "san francisco": "US",
    "seattle": "US", "austin": "US", "boston": "US", "chicago": "US",
    "los angeles": "US", "denver": "US", "atlanta": "US", "dallas": "US",
    "miami": "US", "remote - us": "US", "remote - sf bay area": "US",
    "remote, us": "US", "remote (us)": "US", "us remote": "US",
}


def _country_from_offices(office_objects: list[dict]) -> str | None:
    """Derive a country ISO code from the Greenhouse offices array.

    Rules:
    - If ALL offices resolve to a single country → return it.
    - If offices span multiple countries → return None (ambiguous). The
      posting's own location decides the role's country; falling back to an
      arbitrary office is how a Peru role at a multi-office company was tagged
      "Belgium". Unknown country is then rejected by the allowlist filter.
    - If no offices → None.
    """
    countries: list[str] = []
    for office in office_objects:
        name_lower = (office.get("name") or office.get("location") or "").lower()
        if not name_lower:
            continue
        matched: str | None = None
        for keyword, iso in _OFFICE_COUNTRY_MAP.items():
            if keyword in name_lower:
                matched = iso
                break
        if matched:
            countries.append(matched)

    if not countries:
        return None

    unique = list(dict.fromkeys(countries))  # preserve order, dedupe
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    # Multiple countries → ambiguous. Do not guess an office country.
    return None


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_job(job: dict[str, Any], slug: str, company_name: str | None = None) -> RawJob:
    job_url: str = job.get("absolute_url", "")

    # first_published is the real posting date; updated_at changes on every edit
    posted_at = _parse_dt(job.get("first_published")) or _parse_dt(job.get("updated_at"))

    departments: list[str] = [
        d.get("name", "") for d in job.get("departments", [])
    ]
    office_objects: list[dict] = job.get("offices", [])
    offices: list[str] = [o.get("name", "") for o in office_objects]

    # ── Country detection from structured offices data ────────────────────────
    # The offices array is far more reliable than location_raw (a free-text
    # string that is often "Remote" or blank).  Build a set of countries from
    # all offices and use it to tag the job country on the RawJob so the hard
    # filter can reject US-only roles immediately without enrichment.
    # The posting's own location is authoritative; offices are only a fallback
    # for remote/blank locations (and only when they agree on one country).
    location_name = job.get("location", {}).get("name")
    detected_country: str | None = (
        detect_country(location_name) if location_name else None
    )
    if detected_country is None:
        detected_country = _country_from_offices(office_objects)

    extra: dict[str, Any] = {
        "slug": slug,
        "departments": departments,
        "offices": offices,
        "metadata": job.get("metadata", []),
        "updated_at": job.get("updated_at"),
    }

    return RawJob(
        source="greenhouse",
        source_job_id=str(job["id"]),
        url=job_url,
        company=company_name or job.get("company_name") or slug,
        title=job.get("title", ""),
        apply_url=job_url,
        location_raw=job.get("location", {}).get("name"),
        country=detected_country,          # ← set from offices, not just location_raw
        description_html=job.get("content"),
        posted_at=posted_at,
        extra=extra,
    )
