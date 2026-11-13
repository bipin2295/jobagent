"""Himalayas free remote-jobs API collector.

API: ``https://himalayas.app/jobs/api?q={query}&limit=50``

Three queries are issued in parallel (``data+engineer``,
``analytics+engineer``, ``data+platform``); duplicates are dropped by
``id``.  All Himalayas jobs are remote-only so ``remote_type`` is always set.
``fetch()`` never raises.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_API_URL = "https://himalayas.app/jobs/api"
_QUERIES = ["data+engineer", "analytics+engineer", "data+platform"]
_LIMIT = 50
_TIMEOUT = 15.0


class HimalayasCollector(CollectorBase):
    name = "himalayas"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        # search_cfg is intentionally not used for keyword filtering here:
        # Himalayas is remote-only and the query strings already target the
        # correct job families.  Cross-collector dedup handles any overlap.
        seen_ids: set[str] = set()
        results: list[RawJob] = []

        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            tasks = [self._fetch_query(client, q) for q in _QUERIES]
            batches = await asyncio.gather(*tasks)
            for jobs in batches:
                for raw in jobs:
                    if raw.source_job_id not in seen_ids:
                        results.append(raw)
                        seen_ids.add(raw.source_job_id)

        log.info("himalayas.done", total=len(results))
        return results

    async def _fetch_query(
        self, client: httpx.AsyncClient, query: str
    ) -> list[RawJob]:
        try:
            resp = await client.get(
                _API_URL, params={"q": query, "limit": _LIMIT}
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except Exception as exc:
            log.warning("himalayas.fetch_error", query=query, error=str(exc))
            return []

        jobs: list[RawJob] = []
        for job in data.get("jobs", []):
            try:
                jobs.append(_parse_job(job))
            except Exception as exc:
                log.warning(
                    "himalayas.parse_error",
                    job_id=job.get("id"),
                    error=str(exc),
                )
        return jobs


def _parse_job(job: dict[str, Any]) -> RawJob:
    # API v2: unique key is "guid" (a URL slug). Fallback to hash of applicationLink.
    guid: str = job.get("guid") or job.get("applicationLink") or job.get("id", "")
    if not guid:
        raise KeyError("no guid/applicationLink/id field")
    job_id = guid.rstrip("/").split("/")[-1]  # use the slug portion as a short id

    job_url: str = job.get("guid") or job.get("applicationLink", "")
    apply_url: str = job.get("applicationLink") or job_url

    posted_at: datetime | None = None
    # API v2 uses pubDate (Unix timestamp); v1 used publishedAt (ISO string)
    pub_ts = job.get("pubDate")
    pub_raw: str | None = job.get("publishedAt")
    if pub_ts:
        from datetime import timezone  # noqa: PLC0415
        try:
            posted_at = datetime.fromtimestamp(float(pub_ts), tz=timezone.utc)
        except (ValueError, TypeError):
            pass
    elif pub_raw:
        try:
            posted_at = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    extra: dict[str, Any] = {
        "locationRestrictions": job.get("locationRestrictions") or [],
    }

    return RawJob(
        source="himalayas",
        source_job_id=job_id,
        url=job_url,
        company=job.get("companyName", ""),
        title=job.get("title", ""),
        apply_url=apply_url,
        remote_type="remote",
        description_html=job.get("description"),
        posted_at=posted_at,
        employment_type=job.get("employmentType"),
        extra=extra,
    )
