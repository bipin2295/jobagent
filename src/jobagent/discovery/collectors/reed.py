"""Reed UK API collector.

Reed is the UK's largest job board and provides an official search API.
Auth: HTTP Basic with key as username and empty password.
Docs: https://www.reed.co.uk/developers/jobseeker
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog
from dateutil import parser as date_parser

from jobagent.config import settings
from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_BASE_URL = "https://www.reed.co.uk/api/1.0/search"

# Two sweeps per title: all-UK (empty string) + London to boost city-level hits.
# More granular cities can be added here without changing any other code.
_LOCATIONS: list[str] = ["", "London"]

_RESULTS_TO_TAKE = 100


class ReedCollector(CollectorBase):
    name = "reed"
    source = "reed"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        if not settings.reed_api_key:
            log.warning("reed.skip", reason="reed_api_key is not configured")
            return []

        titles = search_cfg.get("titles", [])[:3]
        auth = httpx.BasicAuth(settings.reed_api_key, "")

        log.info("reed.start", titles=titles)

        jobs: list[RawJob] = []

        async with httpx.AsyncClient(timeout=20.0, auth=auth) as client:
            for title in titles:
                for location in _LOCATIONS:
                    batch = await self._fetch_jobs(client, title, location)
                    jobs.extend(batch)

        log.info("reed.done", total=len(jobs))
        return jobs

    async def _fetch_jobs(
        self,
        client: httpx.AsyncClient,
        title: str,
        location: str,
    ) -> list[RawJob]:
        params: dict[str, Any] = {
            "keywords": title,
            "resultsToTake": _RESULTS_TO_TAKE,
            "minimumSalary": 0,
        }
        if location:
            params["locationName"] = location

        try:
            resp = await client.get(_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning(
                "reed.fetch_failed",
                title=title,
                location=location or "(all UK)",
                error=str(exc),
            )
            return []

        results: list[dict[str, Any]] = data.get("results", [])
        jobs: list[RawJob] = []
        for result in results:
            try:
                raw = _parse_result(result, location)
                jobs.append(raw)
            except Exception as exc:
                log.warning(
                    "reed.parse_error",
                    job_id=result.get("jobId"),
                    error=str(exc),
                )

        return jobs


def _parse_date(value: str | None) -> datetime | None:
    """Best-effort ISO / RFC-2822 date parse; returns None on failure."""
    if not value:
        return None
    try:
        return date_parser.parse(value)
    except Exception:
        return None


def _parse_result(result: dict[str, Any], search_location: str) -> RawJob:
    url: str = result.get("jobUrl", "") or ""
    apply_url: str = result.get("applicationUrl") or url

    # Reed sometimes returns salary_min=0.0 which is not meaningful
    sal_min = result.get("minimumSalary")
    sal_max = result.get("maximumSalary")
    salary_min = float(sal_min) if sal_min else None
    salary_max = float(sal_max) if sal_max else None

    # Prefer the posting date; fall back to expiration date
    posted_at = _parse_date(result.get("date")) or _parse_date(
        result.get("expirationDate")
    )

    return RawJob(
        source="reed",
        source_job_id=str(result["jobId"]),
        url=url,
        apply_url=apply_url,
        company=result.get("employerName") or None,
        title=result.get("jobTitle", ""),
        location_raw=result.get("locationName") or None,
        country="GB",
        city=search_location if search_location else None,
        salary_min=salary_min,
        salary_max=salary_max,
        currency=result.get("currency") or "GBP",
        description_md=result.get("jobDescription") or None,
        posted_at=posted_at,
        extra={
            "search_location": search_location or "all_uk",
        },
    )
