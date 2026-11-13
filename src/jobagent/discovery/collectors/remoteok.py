"""RemoteOK API collector.

API: ``https://remoteok.com/api``

The response is a JSON array whose **first element is a metadata object** and
must be skipped.  Jobs are filtered to those whose title or tags contain
"data", "analytics", or "engineer" (or the caller's keyword list if
provided).  ``fetch()`` never raises.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_API_URL = "https://remoteok.com/api"
_TIMEOUT = 20.0
_DOMAIN_KEYWORDS: frozenset[str] = frozenset(
    {"data", "analytics", "analytic", "engineer", "engineering"}
)


def _matches_domain(title: str, tags: list[str]) -> bool:
    """Broad domain filter: keep jobs related to data/analytics/engineering."""
    text = (title + " " + " ".join(tags)).lower()
    return any(kw in text for kw in _DOMAIN_KEYWORDS)


def _matches_titles(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def _to_float(value: Any) -> float | None:
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


class RemoteOKCollector(CollectorBase):
    name = "remoteok"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        keywords: list[str] = search_cfg.get("titles", [])
        results: list[RawJob] = []

        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "jobagent/0.1 (job-search bot; non-commercial)"},
        ) as client:
            try:
                resp = await client.get(_API_URL)
                resp.raise_for_status()
                raw_list: list[dict[str, Any]] = resp.json()
            except Exception as exc:
                log.warning("remoteok.fetch_error", error=str(exc))
                return []

        # First element is a metadata/legal object – skip it
        for job in raw_list[1:]:
            title: str = job.get("title") or job.get("position", "")
            tags: list[str] = job.get("tags") or []

            # Apply filter: user keywords take precedence over domain filter
            if keywords:
                if not _matches_titles(title, keywords):
                    continue
            elif not _matches_domain(title, tags):
                continue

            try:
                results.append(_parse_job(job))
            except Exception as exc:
                log.warning(
                    "remoteok.parse_error", job_id=job.get("id"), error=str(exc)
                )

        log.info("remoteok.done", total=len(results))
        return results


def _parse_job(job: dict[str, Any]) -> RawJob:
    job_id = str(job.get("id", ""))
    job_url: str = job.get("url", "")

    posted_at: datetime | None = None
    date_raw: str | None = job.get("date")
    if date_raw:
        try:
            posted_at = datetime.fromisoformat(
                date_raw.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            pass

    extra: dict[str, Any] = {
        "tags": job.get("tags") or [],
        "original": job.get("original"),
    }

    return RawJob(
        source="remoteok",
        source_job_id=job_id,
        url=job_url,
        company=job.get("company", ""),
        title=job.get("title") or job.get("position", ""),
        apply_url=job_url,
        location_raw=job.get("location"),
        remote_type="remote",
        description_html=job.get("description"),
        posted_at=posted_at,
        salary_min=_to_float(job.get("salary_min")),
        salary_max=_to_float(job.get("salary_max")),
        extra=extra,
    )
