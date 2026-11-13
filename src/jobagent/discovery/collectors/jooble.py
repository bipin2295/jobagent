"""Jooble API collector.

Jooble is a global job aggregator.  One free key covers all locations.
Docs: https://jooble.org/api/about
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.config import settings
from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_API_URL_TEMPLATE = "https://jooble.org/api/{key}"

# Fixed set of locations to query; not derived from search_cfg because Jooble
# uses free-text locations that differ from ISO country codes.
_LOCATIONS = [
    "Germany",
    "Netherlands",
    "Ireland",
    "France",
    "UK",
    "India",
    "Singapore",
    "Australia",
    "Remote",
]

_RESULTS_PER_PAGE = 20
_MAX_PAGES = 3


def _parse_salary(salary_str: str | None) -> tuple[float | None, float | None]:
    """Parse a free-text salary string into (min, max) floats.

    Handles formats like:
    - "$50,000 - $70,000"
    - "£45k"
    - "60000"
    - "€80,000 per year"
    """
    if not salary_str:
        return None, None

    # Strip currency symbols, commas, and whitespace artefacts
    cleaned = re.sub(r"[£$€,\s]", "", salary_str)

    # Expand shorthand (e.g. "45k" → "45000")
    cleaned = re.sub(r"(\d+)k", lambda m: str(int(m.group(1)) * 1000), cleaned, flags=re.IGNORECASE)

    nums = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if not nums:
        return None, None

    floats = sorted(float(n) for n in nums)
    if len(floats) == 1:
        return floats[0], None
    return floats[0], floats[-1]


class JoobleCollector(CollectorBase):
    name = "jooble"
    source = "jooble"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        if not settings.jooble_api_key:
            log.warning("jooble.skip", reason="jooble_api_key is not configured")
            return []

        titles = search_cfg.get("titles", [])[:3]
        api_url = _API_URL_TEMPLATE.format(key=settings.jooble_api_key)

        log.info("jooble.start", titles=titles, locations=_LOCATIONS)

        jobs: list[RawJob] = []
        seen_links: set[str] = set()

        async with httpx.AsyncClient(timeout=15.0) as client:
            for title in titles:
                for location in _LOCATIONS:
                    for page in range(1, _MAX_PAGES + 1):
                        batch, has_more = await self._fetch_page(
                            client,
                            api_url,
                            keywords=title,
                            location=location,
                            page=page,
                            seen_links=seen_links,
                        )
                        jobs.extend(batch)
                        if not has_more:
                            break

        log.info("jooble.done", total=len(jobs))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        api_url: str,
        *,
        keywords: str,
        location: str,
        page: int,
        seen_links: set[str],
    ) -> tuple[list[RawJob], bool]:
        payload = {
            "keywords": keywords,
            "location": location,
            "page": page,
            "ResultOnPage": _RESULTS_PER_PAGE,
        }
        try:
            resp = await client.post(api_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning(
                "jooble.page_failed",
                keywords=keywords,
                location=location,
                page=page,
                error=str(exc),
            )
            return [], False

        raw_jobs: list[dict[str, Any]] = []
        if isinstance(data, dict):
            raw_jobs = data.get("jobs") or []
        if not raw_jobs:
            return [], False

        jobs: list[RawJob] = []
        for job in raw_jobs:
            try:
                raw = _parse_job(job, location, seen_links)
                if raw is not None:
                    jobs.append(raw)
            except Exception as exc:
                log.warning(
                    "jooble.parse_error",
                    title=job.get("title"),
                    error=str(exc),
                )

        # If the page is full there is likely a next page
        has_more = len(raw_jobs) >= _RESULTS_PER_PAGE
        return jobs, has_more


def _parse_job(
    job: dict[str, Any],
    search_location: str,
    seen_links: set[str],
) -> RawJob | None:
    link: str = job.get("link", "") or ""

    # Deduplicate within this collector run
    if link in seen_links:
        return None
    seen_links.add(link)

    sid = hashlib.md5(
        f"{job.get('company', '')}|{job.get('title', '')}|{link}".encode()
    ).hexdigest()

    salary_min, salary_max = _parse_salary(job.get("salary"))

    posted_at: datetime | None = None
    if updated := job.get("updated"):
        try:
            posted_at = datetime.fromisoformat(updated)
        except ValueError:
            pass

    snippet: str | None = job.get("snippet") or None

    return RawJob(
        source="jooble",
        source_job_id=sid,
        url=link,
        apply_url=link,
        company=job.get("company") or None,
        title=job.get("title", ""),
        location_raw=job.get("location") or None,
        salary_min=salary_min,
        salary_max=salary_max,
        description_html=snippet,
        employment_type=job.get("type") or None,
        posted_at=posted_at,
        extra={
            "source_site": job.get("source"),
            "search_location": search_location,
        },
    )
