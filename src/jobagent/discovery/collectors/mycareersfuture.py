"""MyCareersFuture (Singapore Government) collector.

Public API — no authentication required.
Every listing shows a full salary range, which makes it valuable for
compensation calibration as well as job discovery.

API reference: https://api.mycareersfuture.gov.sg/v2/jobs
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.mycareersfuture.gov.sg/v2/jobs"

# The API requires a recognisable User-Agent; without it you get 403.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; jobagent/1.0)",
    "Accept": "application/json",
}

_LIMIT = 100  # Maximum results per request the API supports


class MyCareersFutureCollector(CollectorBase):
    source = "mycareersfuture"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        titles = search_cfg.get("titles", [])[:3]

        log.info("mycareersfuture.start", titles=titles)

        jobs: list[RawJob] = []

        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            for title in titles:
                batch = await self._fetch_title(client, title)
                jobs.extend(batch)

        log.info("mycareersfuture.done", total=len(jobs))
        return jobs

    async def _fetch_title(
        self,
        client: httpx.AsyncClient,
        title: str,
    ) -> list[RawJob]:
        params: dict[str, Any] = {
            "search": title,
            "sort": 1,          # 1 = most recent
            "limit": _LIMIT,
            "page": 0,
        }
        try:
            resp = await client.get(_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning(
                "mycareersfuture.fetch_failed",
                title=title,
                error=str(exc),
            )
            return []

        results: list[dict[str, Any]] = data.get("results", []) or []
        jobs: list[RawJob] = []
        for item in results:
            try:
                raw = _parse_job(item)
                jobs.append(raw)
            except Exception as exc:
                log.warning(
                    "mycareersfuture.parse_error",
                    uuid=item.get("uuid"),
                    error=str(exc),
                )

        return jobs


def _parse_job(item: dict[str, Any]) -> RawJob:
    meta: dict[str, Any] = item.get("metadata", {}) or {}
    salary_obj: dict[str, Any] = meta.get("salary", {}) or {}

    sal_type: str = salary_obj.get("type", "") or ""
    sal_min_raw = salary_obj.get("minimum")
    sal_max_raw = salary_obj.get("maximum")

    salary_min: float | None = None
    salary_max: float | None = None

    if sal_min_raw is not None:
        try:
            salary_min = float(sal_min_raw)
        except (TypeError, ValueError):
            pass
    if sal_max_raw is not None:
        try:
            salary_max = float(sal_max_raw)
        except (TypeError, ValueError):
            pass

    # Normalise monthly figures to annual so downstream comparisons are
    # consistent with the per-country salary floors in search.yaml.
    if sal_type == "Monthly":
        if salary_min is not None:
            salary_min = salary_min * 12
        if salary_max is not None:
            salary_max = salary_max * 12

    # Posting date — prefer newPostingDate, fall back to originalPostingDate
    posted_at: datetime | None = None
    for date_key in ("newPostingDate", "originalPostingDate"):
        if date_str := meta.get(date_key):
            try:
                posted_at = datetime.fromisoformat(
                    date_str.replace("Z", "+00:00")
                )
                break
            except ValueError:
                pass

    # Description: concatenate all description blocks (usually one entry)
    descriptions: list[dict[str, Any]] = item.get("descriptions", []) or []
    description_html: str | None = None
    html_parts = [d.get("content", "") for d in descriptions if d.get("content")]
    if html_parts:
        description_html = "\n".join(html_parts)

    # Employment type (first entry if multiple)
    emp_types: list[dict[str, Any]] = item.get("employmentTypes", []) or []
    employment_type: str | None = (
        emp_types[0].get("description") if emp_types else None
    )

    # Skills list
    skills: list[str] = [
        s.get("skill", "") for s in (item.get("skills", []) or []) if s.get("skill")
    ]

    uuid: str = item.get("uuid", "")

    return RawJob(
        source="mycareersfuture",
        source_job_id=uuid,
        url=f"https://www.mycareersfuture.gov.sg/job/{uuid}",
        apply_url=f"https://www.mycareersfuture.gov.sg/job/{uuid}",
        company=(item.get("postedCompany") or {}).get("name") or None,
        title=item.get("title", ""),
        location_raw=_build_location(item.get("address") or {}),
        country="SG",
        city="Singapore",
        salary_min=salary_min,
        salary_max=salary_max,
        currency="SGD",
        employment_type=employment_type,
        description_html=description_html,
        posted_at=posted_at,
        extra={
            "salary_type_original": sal_type,     # "Monthly" or "Annual"
            "position_level": item.get("positionLevel"),
            "min_years_experience": item.get("minimumYearsExperience"),
            "skills": skills,
            "posting_id": meta.get("jobPostingId"),
            "expiry_date": meta.get("expiryDate"),
        },
    )


def _build_location(address: dict[str, Any]) -> str | None:
    parts = [
        address.get("block", ""),
        address.get("streetName", ""),
        address.get("postalCode", ""),
    ]
    loc = " ".join(p for p in parts if p).strip()
    return loc or None
