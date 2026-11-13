"""Remotive remote-jobs API collector.

API: ``https://remotive.com/api/remote-jobs?category={cat}&limit=100``

Fetches the ``data`` and ``software-dev`` categories, deduplicates by job
``id``, and parses salary strings such as ``"$120k-$160k"`` or
``"$85,000 - $110,000"`` into numeric ``salary_min``/``salary_max`` fields.
``fetch()`` never raises.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_API_URL = "https://remotive.com/api/remote-jobs"
_CATEGORIES = ["data", "software-dev"]
_LIMIT = 100
_TIMEOUT = 15.0

# Matches: "$120k – $160k", "$85,000-$110,000", "120K-160K", "85000 to 110000"
_SALARY_RE = re.compile(
    r"\$?\s*(?P<min>[\d,]+)\s*(?P<min_k>[kK])?"
    r"\s*(?:[-–—]|to)\s*"
    r"\$?\s*(?P<max>[\d,]+)\s*(?P<max_k>[kK])?",
    re.IGNORECASE,
)


def _to_float(num_str: str, k_suffix: str | None) -> float:
    """Convert a matched number string to a float, expanding ``k`` suffixes."""
    val = float(num_str.replace(",", ""))
    if k_suffix:
        val *= 1000
    return val


def _parse_salary(
    raw: str,
) -> tuple[float | None, float | None, str | None]:
    """Return *(salary_min, salary_max, currency)* from a free-text salary string."""
    if not raw:
        return None, None, None

    currency: str | None = "USD" if "$" in raw else None

    m = _SALARY_RE.search(raw)
    if not m:
        return None, None, currency

    try:
        salary_min = _to_float(m.group("min"), m.group("min_k"))
        salary_max = _to_float(m.group("max"), m.group("max_k"))
        return salary_min, salary_max, currency
    except (ValueError, TypeError):
        return None, None, currency


def _matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


class RemotiveCollector(CollectorBase):
    name = "remotive"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        keywords: list[str] = search_cfg.get("titles", [])
        seen_ids: set[str] = set()
        results: list[RawJob] = []

        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            for category in _CATEGORIES:
                try:
                    resp = await client.get(
                        _API_URL,
                        params={"category": category, "limit": _LIMIT},
                    )
                    resp.raise_for_status()
                    data: dict[str, Any] = resp.json()
                except Exception as exc:
                    log.warning(
                        "remotive.fetch_error",
                        category=category,
                        error=str(exc),
                    )
                    continue

                for job in data.get("jobs", []):
                    job_id = str(job.get("id", ""))
                    if job_id in seen_ids:
                        continue
                    title: str = job.get("title", "")
                    if not _matches(title, keywords):
                        continue
                    try:
                        raw = _parse_job(job)
                        results.append(raw)
                        seen_ids.add(job_id)
                    except Exception as exc:
                        log.warning(
                            "remotive.parse_error",
                            job_id=job_id,
                            error=str(exc),
                        )

        log.info("remotive.done", total=len(results))
        return results


def _parse_job(job: dict[str, Any]) -> RawJob:
    job_id = str(job["id"])
    job_url: str = job.get("url", "")

    posted_at: datetime | None = None
    pub_raw: str | None = job.get("publication_date")
    if pub_raw:
        try:
            posted_at = datetime.fromisoformat(
                pub_raw.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            pass

    salary_raw: str = job.get("salary") or ""
    salary_min, salary_max, currency = _parse_salary(salary_raw)

    extra: dict[str, Any] = {
        "tags": job.get("tags", []),
        "job_type": job.get("job_type"),
        "company_url": job.get("company_url"),
        "salary_raw": salary_raw,
    }

    return RawJob(
        source="remotive",
        source_job_id=job_id,
        url=job_url,
        company=job.get("company_name", ""),
        title=job.get("title", ""),
        apply_url=job_url,
        remote_type="remote",
        description_html=job.get("description"),
        posted_at=posted_at,
        salary_min=salary_min,
        salary_max=salary_max,
        currency=currency,
        employment_type=job.get("job_type"),
        extra=extra,
    )
