"""SmartRecruiters public postings collector.

API: ``https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100``

Paginates via ``totalFound`` vs accumulated ``len(content)``.  All companies
are fetched concurrently (one async task per company; pagination within each
company is sequential).  ``fetch()`` never raises.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob
from jobagent.discovery.company_universe import CompanyUniverse

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.smartrecruiters.com/v1/companies/{id}/postings"
_LIMIT = 100
_TIMEOUT = 15.0


def _matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


class SmartRecruitersCollector(CollectorBase):
    name = "smartrecruiters"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        slugs = CompanyUniverse().get_slugs("smartrecruiters")
        if not slugs:
            log.info("smartrecruiters.no_slugs")
            return []

        keywords: list[str] = search_cfg.get("titles", [])
        results: list[RawJob] = []

        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            tasks = [
                self._fetch_company(client, company_id, keywords)
                for company_id in slugs
            ]
            batch_results = await asyncio.gather(*tasks)
            for jobs in batch_results:
                results.extend(jobs)

        log.info("smartrecruiters.done", slugs=len(slugs), total=len(results))
        return results

    async def _fetch_company(
        self,
        client: httpx.AsyncClient,
        company_id: str,
        keywords: list[str],
    ) -> list[RawJob]:
        all_items: list[dict[str, Any]] = []
        offset = 0
        url = _BASE_URL.format(id=company_id)

        while True:
            try:
                resp = await client.get(
                    url, params={"limit": _LIMIT, "offset": offset}
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            except Exception as exc:
                log.warning(
                    "smartrecruiters.fetch_error",
                    company_id=company_id,
                    offset=offset,
                    error=str(exc),
                )
                break

            content: list[dict[str, Any]] = data.get("content", [])
            all_items.extend(content)

            total_found: int = data.get("totalFound", 0)
            offset += len(content)
            # Stop when we've collected everything or the page was empty
            if not content or offset >= total_found:
                break

        jobs: list[RawJob] = []
        for item in all_items:
            title: str = item.get("name", "")
            if not _matches(title, keywords):
                continue
            try:
                jobs.append(_parse_item(item, company_id))
            except Exception as exc:
                log.warning(
                    "smartrecruiters.parse_error",
                    company_id=company_id,
                    item_id=item.get("id"),
                    error=str(exc),
                )
        return jobs


def _parse_item(item: dict[str, Any], company_id: str) -> RawJob:
    item_url: str = item.get("ref", "")

    posted_at: datetime | None = None
    released_raw: str | None = item.get("releasedDate")
    if released_raw:
        try:
            posted_at = datetime.fromisoformat(
                released_raw.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            pass

    location: dict[str, Any] = item.get("location", {})
    city: str | None = location.get("city")
    country_obj: dict[str, Any] = location.get("country") or {}
    country: str | None = country_obj.get("code")
    location_raw: str | None = ", ".join(filter(None, [city, country])) or None

    employment_type: str | None = (item.get("typeOfEmployment") or {}).get("id")

    extra: dict[str, Any] = {"company_id": company_id}

    return RawJob(
        source="smartrecruiters",
        source_job_id=item["id"],
        url=item_url,
        company=company_id,
        title=item.get("name", ""),
        apply_url=item_url,
        location_raw=location_raw,
        city=city,
        country=country,
        posted_at=posted_at,
        employment_type=employment_type,
        extra=extra,
    )
