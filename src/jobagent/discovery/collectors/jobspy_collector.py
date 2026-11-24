"""JobSpy-based collector for LinkedIn, Indeed, and Glassdoor.

python-jobspy wraps multiple job boards in a single synchronous call that
returns a Pandas DataFrame.  Because it is sync and potentially slow we run
each scrape in a thread-pool executor, but only one at a time (Semaphore(1))
to avoid triggering rate-limits and IP bans.

Install the optional dependency:
    pip install "job-application-agent[discovery]"
or:
    pip install python-jobspy
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
from datetime import datetime
from typing import Any

import structlog

from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

# Sites supported by python-jobspy
_SITES = ["linkedin", "indeed", "glassdoor"]

# (location, country_indeed) pairs; country_indeed is used by Indeed's
# geo-routing and may be an empty string for non-Indeed sites.
_REGION_CONFIGS: list[tuple[str, str]] = [
    ("India", "India"),
    ("Germany", "Germany"),
    ("Netherlands", "Netherlands"),
    ("Dubai", ""),
    ("Remote", ""),
]


def _scrape_sync(
    site: str,
    search_term: str,
    location: str,
    country_indeed: str,
) -> list[RawJob]:
    """Synchronous scrape for one site / search-term / location combo.

    This function is intentionally isolated so it can be passed to
    ``run_in_executor`` without closures or lambdas.
    """
    try:
        import pandas as pd  # type: ignore[import]
        from jobspy import scrape_jobs  # type: ignore[import]
    except ImportError:
        log.warning(
            "jobspy.import_failed",
            reason="python-jobspy or pandas is not installed",
        )
        return []

    try:
        df = scrape_jobs(
            site_name=[site],
            search_term=search_term,
            location=location,
            results_wanted=50,
            country_indeed=country_indeed if country_indeed else None,
        )
    except Exception as exc:
        log.warning(
            "jobspy.scrape_failed",
            site=site,
            search_term=search_term,
            location=location,
            error=str(exc),
        )
        return []

    if df is None or (hasattr(df, "empty") and df.empty):
        return []

    jobs: list[RawJob] = []
    for _, row in df.iterrows():
        try:
            raw = _row_to_raw_job(row, pd, location)
            if raw:
                jobs.append(raw)
        except Exception as exc:
            log.warning(
                "jobspy.row_error",
                site=site,
                error=str(exc),
            )

    return jobs


def _safe_str(value: Any, pd: Any) -> str | None:
    """Return str(value) or None if value is NaN/None."""
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value) if value is not None else None


def _safe_float(value: Any, pd: Any) -> float | None:
    """Return float(value) or None if value is NaN/None."""
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_to_raw_job(row: Any, pd: Any, search_location: str) -> RawJob | None:
    job_url: str = _safe_str(row.get("job_url"), pd) or ""
    if not job_url:
        return None

    # Prefer an explicit id; fall back to a hash of the URL
    raw_id = row.get("id")
    if raw_id is not None:
        try:
            if not pd.isna(raw_id):
                source_job_id = str(raw_id)
            else:
                source_job_id = hashlib.md5(job_url.encode()).hexdigest()[:12]
        except (TypeError, ValueError):
            source_job_id = hashlib.md5(job_url.encode()).hexdigest()[:12]
    else:
        source_job_id = hashlib.md5(job_url.encode()).hexdigest()[:12]

    # Date posted
    posted_at: datetime | None = None
    dp = row.get("date_posted")
    if dp is not None:
        try:
            if not pd.isna(dp):
                if isinstance(dp, datetime):
                    posted_at = dp
                else:
                    posted_at = datetime.fromisoformat(str(dp))
        except (TypeError, ValueError):
            pass

    is_remote_val = row.get("is_remote")
    remote_type: str | None = None
    try:
        if is_remote_val and not pd.isna(is_remote_val) and bool(is_remote_val):
            remote_type = "remote"
    except (TypeError, ValueError):
        pass

    site_val: str = _safe_str(row.get("site"), pd) or "unknown"

    return RawJob(
        source=f"jobspy_{site_val}",
        source_job_id=source_job_id,
        url=job_url,
        apply_url=_safe_str(row.get("job_url_direct"), pd) or job_url,
        company=_safe_str(row.get("company"), pd),
        title=_safe_str(row.get("title"), pd) or "",
        location_raw=_safe_str(row.get("location"), pd),
        remote_type=remote_type,
        salary_min=_safe_float(row.get("min_amount"), pd),
        salary_max=_safe_float(row.get("max_amount"), pd),
        currency=_safe_str(row.get("currency"), pd),
        employment_type=_safe_str(row.get("job_type"), pd),
        description_md=_safe_str(row.get("description"), pd),
        posted_at=posted_at,
        extra={
            "search_location": search_location,
            "job_level": _safe_str(row.get("job_level"), pd) or "",
            "job_function": _safe_str(row.get("job_function"), pd) or "",
            "is_remote": bool(remote_type),
        },
    )


class JobSpyCollector(CollectorBase):
    # ``name`` (not just ``source``) must be set: _load_collectors' --sources
    # filter and discover_cmd's active_ats fallback both match on
    # ``instance.name``. Leaving it at the base class default ("") meant
    # `--sources jobspy` silently matched zero collectors and `discover`
    # never ran the only collector that performs a fresh LinkedIn/Indeed/
    # Glassdoor search — every "linkedin" job in the DB came exclusively
    # from stale aggregator feeds (Adzuna/Jooble) re-tagged after the fact.
    name = "jobspy"
    source = "jobspy"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        # Quick import-check so we return early without spinning up tasks
        try:
            import jobspy  # noqa: F401  # type: ignore[import]
        except ImportError:
            log.warning(
                "jobspy.skip",
                reason="python-jobspy is not installed; add it via the 'discovery' extra",
            )
            return []

        titles = search_cfg.get("titles", [])[:2]
        loop = asyncio.get_running_loop()

        # Semaphore(1) → sequential execution; avoids simultaneous scrapes
        # hammering the same board from the same IP.
        semaphore = asyncio.Semaphore(1)

        async def _run_one(
            site: str, title: str, location: str, country_indeed: str
        ) -> list[RawJob]:
            async with semaphore:
                return await loop.run_in_executor(
                    None,
                    functools.partial(
                        _scrape_sync,
                        site,
                        title,
                        location,
                        country_indeed,
                    ),
                )

        tasks = [
            _run_one(site, title, location, country_indeed)
            for site in _SITES
            for title in titles
            for location, country_indeed in _REGION_CONFIGS
        ]

        log.info(
            "jobspy.start",
            sites=_SITES,
            titles=titles,
            regions=[loc for loc, _ in _REGION_CONFIGS],
            total_tasks=len(tasks),
        )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        jobs: list[RawJob] = []
        for result in results:
            if isinstance(result, BaseException):
                log.warning("jobspy.task_error", error=str(result))
            elif isinstance(result, list):
                jobs.extend(result)

        log.info("jobspy.done", total=len(jobs))
        return jobs
