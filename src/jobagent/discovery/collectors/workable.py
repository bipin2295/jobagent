"""Workable per-company jobs collector.

API: ``https://{company}.workable.com/api/v3/accounts/{company}/jobs``

Each company subdomain exposes a public REST endpoint that returns a
``results`` array.  All companies are fetched concurrently.
``fetch()`` never raises.
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

_API_URL = "https://{company}.workable.com/api/v3/accounts/{company}/jobs"
_TIMEOUT = 15.0


def _matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


class WorkableCollector(CollectorBase):
    name = "workable"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        slugs = CompanyUniverse().get_slugs("workable")
        if not slugs:
            log.info("workable.no_slugs")
            return []

        keywords: list[str] = search_cfg.get("titles", [])
        results: list[RawJob] = []

        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            tasks = [
                self._fetch_company(client, slug, keywords) for slug in slugs
            ]
            batch_results = await asyncio.gather(*tasks)
            for jobs in batch_results:
                results.extend(jobs)

        log.info("workable.done", slugs=len(slugs), total=len(results))
        return results

    async def _fetch_company(
        self,
        client: httpx.AsyncClient,
        slug: str,
        keywords: list[str],
    ) -> list[RawJob]:
        url = _API_URL.format(company=slug)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except Exception as exc:
            log.warning("workable.fetch_error", slug=slug, error=str(exc))
            return []

        jobs: list[RawJob] = []
        for item in data.get("results", []):
            # Prefer the shorter title; fall back to full_title
            title: str = item.get("title") or item.get("full_title", "")
            if not _matches(title, keywords):
                continue
            try:
                jobs.append(_parse_item(item, slug))
            except Exception as exc:
                log.warning(
                    "workable.parse_error",
                    slug=slug,
                    item_id=item.get("id"),
                    error=str(exc),
                )
        return jobs


def _parse_item(item: dict[str, Any], slug: str) -> RawJob:
    job_url: str = item.get("url", "")
    apply_url: str = item.get("application_url") or job_url

    posted_at: datetime | None = None
    created_raw: str | None = item.get("created_at")
    if created_raw:
        try:
            posted_at = datetime.fromisoformat(
                created_raw.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            pass

    location: dict[str, Any] = item.get("location", {})
    location_raw: str | None = location.get("location_str") or None
    country: str | None = location.get("country")

    # Workable uses "shortcode" as the stable external identifier
    source_job_id: str = item.get("shortcode") or item.get("id", "")

    extra: dict[str, Any] = {"company_slug": slug}

    return RawJob(
        source="workable",
        source_job_id=source_job_id,
        url=job_url,
        company=slug,
        title=item.get("title") or item.get("full_title", ""),
        apply_url=apply_url,
        location_raw=location_raw,
        country=country,
        posted_at=posted_at,
        employment_type=item.get("employment_type"),
        extra=extra,
    )
