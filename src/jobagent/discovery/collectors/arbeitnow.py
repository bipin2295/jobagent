"""Arbeitnow free job-board API collector.

API: ``https://www.arbeitnow.com/api/job-board-api?page={page}``

Paginates up to 10 pages or until an empty page is returned.  Includes a
``visa_sponsorship`` flag per job which is surfaced in ``extra``.
``fetch()`` never raises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_API_URL = "https://www.arbeitnow.com/api/job-board-api"
_MAX_PAGES = 10
_TIMEOUT = 15.0


def _matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


class ArbeitnowCollector(CollectorBase):
    name = "arbeitnow"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        keywords: list[str] = search_cfg.get("titles", [])
        results: list[RawJob] = []

        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            for page in range(1, _MAX_PAGES + 1):
                try:
                    resp = await client.get(_API_URL, params={"page": page})
                    resp.raise_for_status()
                    data: dict[str, Any] = resp.json()
                except Exception as exc:
                    log.warning(
                        "arbeitnow.fetch_error", page=page, error=str(exc)
                    )
                    break

                jobs_page: list[dict[str, Any]] = data.get("data", [])
                if not jobs_page:
                    log.debug("arbeitnow.empty_page", page=page)
                    break

                for job in jobs_page:
                    title: str = job.get("title", "")
                    if not _matches(title, keywords):
                        continue
                    try:
                        results.append(_parse_job(job))
                    except Exception as exc:
                        log.warning(
                            "arbeitnow.parse_error",
                            slug=job.get("slug"),
                            error=str(exc),
                        )

        log.info("arbeitnow.done", total=len(results))
        return results


def _parse_job(job: dict[str, Any]) -> RawJob:
    slug: str = job["slug"]
    job_url: str = job.get("url") or f"https://www.arbeitnow.com/jobs/{slug}"

    posted_at: datetime | None = None
    created_ts = job.get("created_at")
    if created_ts is not None:
        try:
            posted_at = datetime.fromtimestamp(int(created_ts), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            pass

    remote_type: str | None = "remote" if job.get("remote") else None

    extra: dict[str, Any] = {
        "visa_sponsorship": job.get("visa_sponsorship", False),
        "tags": job.get("tags", []),
    }

    return RawJob(
        source="arbeitnow",
        source_job_id=slug,
        url=job_url,
        company=job.get("company_name", ""),
        title=job.get("title", ""),
        apply_url=job_url,
        location_raw=job.get("location"),
        remote_type=remote_type,
        description_html=job.get("description"),
        posted_at=posted_at,
        extra=extra,
    )
