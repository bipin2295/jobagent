"""Orchestrate all job collectors and persist results.

``run_discovery`` — run a list of collectors concurrently (semaphore-limited).
``discover_all``  — top-level entry point: load collectors, run, dedup, store.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import structlog

from jobagent.config import load_yaml
from jobagent.db.engine import get_session
from jobagent.discovery.base import CollectorBase, RawJob
from jobagent.pipeline.dedup import bulk_upsert

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Collector registry
# ---------------------------------------------------------------------------
# Each entry is (module_path, class_name).  Modules are imported lazily so
# that missing optional dependencies (e.g. jobspy) do not break the runner.
# ---------------------------------------------------------------------------

_COLLECTOR_REGISTRY: list[tuple[str, str]] = [
    ("jobagent.discovery.collectors.greenhouse", "GreenhouseCollector"),
    ("jobagent.discovery.collectors.lever", "LeverCollector"),
    ("jobagent.discovery.collectors.ashby", "AshbyCollector"),
    ("jobagent.discovery.collectors.workday", "WorkdayCollector"),
    ("jobagent.discovery.collectors.smartrecruiters", "SmartRecruitersCollector"),
    ("jobagent.discovery.collectors.workable", "WorkableCollector"),
    ("jobagent.discovery.collectors.arbeitnow", "ArbeitnowCollector"),
    ("jobagent.discovery.collectors.remotive", "RemotiveCollector"),
    ("jobagent.discovery.collectors.remoteok", "RemoteOKCollector"),
    ("jobagent.discovery.collectors.himalayas", "HimalayasCollector"),
    ("jobagent.discovery.collectors.weworkremotely", "WeWorkRemotelyCollector"),
    ("jobagent.discovery.collectors.adzuna", "AdzunaCollector"),
    ("jobagent.discovery.collectors.jooble", "JoobleCollector"),
    ("jobagent.discovery.collectors.reed", "ReedCollector"),
    ("jobagent.discovery.collectors.jobspy_collector", "JobSpyCollector"),
    ("jobagent.discovery.collectors.mycareersfuture", "MyCareersFutureCollector"),
    ("jobagent.discovery.collectors.hn_hiring", "HNHiringCollector"),
]

# ---------------------------------------------------------------------------
# Collector cooldowns
# How often each collector is allowed to run (in hours).
# Collectors with API quotas run weekly; free collectors run daily.
# ---------------------------------------------------------------------------
_COOLDOWN_HOURS: dict[str, int] = {
    # Free public APIs — poll every run (no cooldown)
    "GreenhouseCollector":     0,
    "LeverCollector":          0,
    "AshbyCollector":          0,
    "WorkdayCollector":        0,
    "SmartRecruitersCollector":0,
    "WorkableCollector":       0,
    "ArbeitnowCollector":      0,
    "RemotiveCollector":       0,
    "RemoteOKCollector":       0,
    "HimalayasCollector":      0,
    "WeWorkRemotelyCollector": 0,
    "MyCareersFutureCollector":0,
    "HNHiringCollector":       0,
    # Quota-based — once per week (168 h) to stay inside free tiers
    "AdzunaCollector":         168,
    "JoobleCollector":         168,
    "ReedCollector":           168,
    "JobSpyCollector":         168,
}

_COOLDOWN_FILE = "data/collector_last_run.json"


def _load_cooldowns() -> dict[str, str]:
    import json, pathlib  # noqa: PLC0415
    p = pathlib.Path(_COOLDOWN_FILE)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_cooldown(name: str) -> None:
    import json, pathlib  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415
    p = pathlib.Path(_COOLDOWN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    store = _load_cooldowns()
    store[name] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(store, indent=2))


def _is_on_cooldown(name: str) -> bool:
    hours = _COOLDOWN_HOURS.get(name, 0)
    if hours == 0:
        return False
    from datetime import datetime, timezone, timedelta  # noqa: PLC0415
    store = _load_cooldowns()
    last_run_str = store.get(name)
    if not last_run_str:
        return False
    try:
        last_run = datetime.fromisoformat(last_run_str)
        return datetime.now(timezone.utc) - last_run < timedelta(hours=hours)
    except Exception:
        return False


def _load_collectors(only: list[str] | None = None) -> list[CollectorBase]:
    """Instantiate every registered collector, skipping unavailable ones.

    Parameters
    ----------
    only:
        Optional allowlist of collector *source* names (e.g. ``["greenhouse",
        "workday"]``).  When given, only collectors whose ``name`` attribute
        matches are loaded.  Pass ``None`` to load all.

    Import errors (missing optional deps) are logged at DEBUG and silently
    skipped so the runner degrades gracefully.  Quota-based collectors that
    have run within their cooldown window are also skipped.
    """
    collectors: list[CollectorBase] = []
    for module_path, class_name in _COLLECTOR_REGISTRY:
        try:
            mod = importlib.import_module(module_path)
            cls: type[CollectorBase] = getattr(mod, class_name)
            instance = cls()

            # Source-name filter (--sources flag)
            if only and instance.name not in only:
                continue

            # Cooldown check (quota-limited collectors)
            if _is_on_cooldown(class_name):
                hours = _COOLDOWN_HOURS.get(class_name, 0)
                log.info(
                    "collector_on_cooldown",
                    name=class_name,
                    cooldown_hours=hours,
                    note="skipping to preserve API quota",
                )
                continue

            collectors.append(instance)
            log.debug("collector_loaded", name=class_name)
        except (ImportError, ModuleNotFoundError) as exc:
            log.debug(
                "collector_unavailable",
                module=module_path,
                class_name=class_name,
                reason=str(exc),
            )
        except AttributeError as exc:
            log.warning(
                "collector_missing_class",
                module=module_path,
                class_name=class_name,
                reason=str(exc),
            )
        except Exception:
            log.exception(
                "collector_init_failed",
                module=module_path,
                class_name=class_name,
            )
    return collectors


# ---------------------------------------------------------------------------
# Core async runner
# ---------------------------------------------------------------------------


async def run_discovery(
    collectors: list[CollectorBase],
    search_cfg: dict[str, Any],
    *,
    max_concurrent: int = 5,
) -> tuple[list[RawJob], dict[str, int]]:
    """Run *collectors* concurrently, bounded by *max_concurrent*.

    Each collector runs inside a semaphore slot.  Failures are caught per-
    collector: a failed collector returns 0 results and does not abort the
    others.

    Returns
    -------
    all_raw_jobs:
        Flattened list of all :class:`~jobagent.discovery.base.RawJob` objects
        returned by all collectors.
    stats:
        Dict mapping collector name → job count (0 for failed/skipped).
    """
    sem = asyncio.Semaphore(max_concurrent)
    all_jobs: list[RawJob] = []
    stats: dict[str, int] = {}

    async def _run_one(collector: CollectorBase) -> None:
        if not collector.is_enabled(search_cfg):
            log.debug("collector_skipped", name=collector.name)
            stats[collector.name] = 0
            return
        try:
            async with sem:
                jobs = await collector.fetch(search_cfg)
            count = len(jobs)
            stats[collector.name] = count
            all_jobs.extend(jobs)
            log.info("collector_done", name=collector.name, count=count)
            # Mark quota-limited collectors as ran so they observe the cooldown
            class_name = type(collector).__name__
            if _COOLDOWN_HOURS.get(class_name, 0) > 0:
                _save_cooldown(class_name)
        except Exception:
            log.exception("collector_failed", name=collector.name)
            stats[collector.name] = 0

    await asyncio.gather(*(_run_one(c) for c in collectors))
    return all_jobs, stats


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def discover_all(
    search_cfg: dict[str, Any] | None = None,
    only_sources: list[str] | None = None,
) -> tuple[int, int]:
    """Run the full discovery pipeline end-to-end.

    Parameters
    ----------
    only_sources:
        Optional list of collector source names to run, e.g.
        ``["greenhouse", "workday", "ashby"]``.  Pass ``None`` to run all
        collectors that are not on cooldown.  Quota-limited sources (Adzuna,
        Jooble, Reed) are automatically skipped if they ran within the past
        week regardless of this parameter.

    Steps:

    1. Load ``config/search.yaml`` (if *search_cfg* not provided).
    2. Instantiate collectors, respecting cooldowns and the *only_sources* filter.
    3. Run collectors concurrently via :func:`run_discovery`.
    4. Feed discovered apply-URLs into :class:`~jobagent.discovery.company_universe.CompanyUniverse`
       so the flywheel keeps growing.
    5. Persist all raw jobs via :func:`~jobagent.pipeline.dedup.bulk_upsert`.

    Returns
    -------
    (new_jobs, updated_jobs):
        Count of freshly inserted rows and count of updated (seen-again) rows.
    """
    if search_cfg is None:
        search_cfg = load_yaml("search")

    collectors = _load_collectors(only=only_sources)
    if not collectors:
        log.warning("discover_all.no_collectors")
        return 0, 0

    log.info("discover_all.start", collector_count=len(collectors))

    raw_jobs, stats = await run_discovery(collectors, search_cfg)

    total_raw = len(raw_jobs)
    log.info(
        "discover_all.collected",
        total=total_raw,
        per_collector=stats,
    )

    if not raw_jobs:
        log.info("discover_all.nothing_to_store")
        return 0, 0

    # ── Grow the company universe from apply URLs ──────────────────────────
    # Lazy import avoids a circular dependency at module load time.
    try:
        from jobagent.discovery.company_universe import CompanyUniverse  # noqa: PLC0415

        universe = CompanyUniverse()
        new_companies = universe.feed_jobs(raw_jobs)
        log.info("discover_all.universe_updated", new_companies=new_companies)
    except Exception:
        log.exception("discover_all.universe_feed_failed")

    # ── Persist to SQLite ──────────────────────────────────────────────────
    with get_session() as session:
        new_jobs, updated_jobs = bulk_upsert(raw_jobs, session)

    log.info(
        "discover_all.done",
        raw=total_raw,
        new=new_jobs,
        updated=updated_jobs,
    )
    return new_jobs, updated_jobs
