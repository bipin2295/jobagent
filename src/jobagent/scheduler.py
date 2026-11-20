"""Run orchestrator – the top-level pipeline that `jobagent run` executes.

Stages (each is idempotent, DB-driven, and cooperative-cancel-aware):
  1. preflight   – connectivity checks before any real work
  2. discover    – collect new jobs from all sources
  3. score       – enrich, filter, embed, LLM-score
  4. tailor      – build tailored resume + CL + answers for top-N jobs
  5. apply       – submit via ATS handlers / browser agent
  6. track       – Gmail scan, status updates
  7. summary     – console + Telegram digest
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


async def run(
    limit: int = 40,
    regions: list[str] | None = None,
    force_shadow: bool = False,
    only_stage: str | None = None,
) -> None:
    """Execute the full pipeline (or a single stage if *only_stage* is set)."""
    # Remove any stale stop-sentinel from a previous session
    stop_sentinel = Path("data/STOP")
    if stop_sentinel.exists():
        stop_sentinel.unlink()
        log.info("removed stale STOP sentinel")

    from jobagent.control.flags import request_resume
    request_resume()

    stages = ["preflight", "discover", "score", "tailor", "apply", "track", "summary"]
    if only_stage and only_stage in stages:
        stages = [only_stage]

    from jobagent.db.repo import RunRepo
    overall_run = RunRepo.start("full_pipeline")

    t0 = time.monotonic()
    errors = 0
    items_in = 0
    items_out = 0

    for stage_name in stages:
        log.info("stage_start", stage=stage_name)
        stage_run = RunRepo.start(stage_name)
        try:
            si, so = await _run_stage(
                stage_name,
                limit=limit,
                regions=regions,
                force_shadow=force_shadow,
            )
            RunRepo.finish(stage_run.id, items_in=si, items_out=so, errors=0)
            items_in += si
            items_out += so
            log.info("stage_done", stage=stage_name, items_in=si, items_out=so)
        except asyncio.CancelledError:
            RunRepo.fail(stage_run.id, "cancelled")
            log.warning("stage_cancelled", stage=stage_name)
            break
        except Exception as exc:  # noqa: BLE001
            errors += 1
            RunRepo.fail(stage_run.id, str(exc))
            log.error("stage_error", stage=stage_name, error=str(exc))
            # Continue to next stage rather than aborting the whole run

    elapsed = int(time.monotonic() - t0)
    RunRepo.finish(overall_run.id, items_in=items_in, items_out=items_out, errors=errors)
    log.info(
        "run_complete",
        elapsed_s=elapsed,
        items_in=items_in,
        items_out=items_out,
        errors=errors,
    )


async def _run_stage(
    name: str,
    *,
    limit: int,
    regions: list[str] | None,
    force_shadow: bool,
) -> tuple[int, int]:
    """Dispatch to the appropriate stage handler. Returns (items_in, items_out)."""
    if name == "preflight":
        return await _preflight()
    if name == "discover":
        return await _discover(regions)
    if name == "score":
        return await _score()
    if name == "tailor":
        return await _tailor(limit)
    if name == "apply":
        return await _apply(limit, force_shadow)
    if name == "track":
        return await _track()
    if name == "summary":
        return await _summary()
    return 0, 0


# ── Stage implementations (stubs, fleshed out in later phases) ────────────────

async def _preflight() -> tuple[int, int]:
    """Check connectivity. Raises on hard failures."""
    from rich.console import Console
    con = Console()

    checks: list[tuple[str, bool, str]] = []

    # 1. DB
    try:
        from jobagent.db.engine import create_tables
        create_tables()
        checks.append(("Database", True, "ready"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("Database", False, str(exc)))

    # 2. Portkey / LLM
    try:
        from jobagent.config import settings
        if settings.portkey_api_key:
            checks.append(("Portkey LLM", True, "key set"))
        else:
            checks.append(("Portkey LLM", False, "JOBAGENT_PORTKEY_API_KEY not set"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("Portkey LLM", False, str(exc)))

    # 3. Ollama
    try:
        import httpx
        from jobagent.config import settings
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=3.0)
        resp.raise_for_status()
        checks.append(("Ollama (embeddings)", True, "reachable"))
    except Exception:  # noqa: BLE001
        checks.append(("Ollama (embeddings)", False, "not reachable – start Ollama first"))

    # 4. Gmail token — verify it actually works, not just that the file exists.
    # A revoked refresh token looks perfectly healthy on disk, which is how
    # runs lost confirmation-email checks while preflight reported success.
    try:
        from jobagent.email_provider import EmailProvider  # noqa: PLC0415

        provider = EmailProvider()
        if provider.connected:
            checks.append(("Gmail token", True, "connected"))
        elif provider.revoked:
            checks.append(
                (
                    "Gmail token",
                    False,
                    "REVOKED/expired — run `python scripts/gmail_connect.py` on the host",
                )
            )
        else:
            checks.append(("Gmail token", False, "run `jobagent gmail-connect`"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("Gmail token", False, str(exc)))

    # 5. Profile
    try:
        from jobagent.profile.loader import load_profile
        load_profile()
        checks.append(("Master profile", True, "loaded"))
    except FileNotFoundError:
        checks.append(("Master profile", False, "run `jobagent profile import` first"))

    # Print preflight table
    from rich.table import Table
    t = Table(title="Preflight checks", show_header=True)
    t.add_column("Check", style="cyan")
    t.add_column("", width=2)
    t.add_column("Note")

    hard_fail = False
    for check_name, ok, note in checks:
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        t.add_row(check_name, icon, note)
        # Hard fails that must stop the run
        if not ok and check_name in ("Database", "Master profile"):
            hard_fail = True

    con.print(t)

    if hard_fail:
        raise RuntimeError("Preflight failed on a required check. Fix the issues above and retry.")

    return len(checks), sum(1 for _, ok, _ in checks if ok)


async def _discover(regions: list[str] | None) -> tuple[int, int]:
    """Phase 2: collect jobs from all configured sources."""
    from jobagent.discovery.runner import discover_all  # noqa: PLC0415
    from jobagent.config import load_yaml              # noqa: PLC0415
    from jobagent.apply.guardrails import load_guardrails  # noqa: PLC0415

    search_cfg = load_yaml("search")
    if regions:
        search_cfg["_active_regions"] = set(regions)

    # Respect active_ats focus — only poll sources that match the active ATS list
    guardrails = load_guardrails()
    only_sources: list[str] | None = guardrails.active_ats if guardrails.active_ats else None

    new_jobs, updated = await discover_all(search_cfg, only_sources=only_sources)

    # Resolve aggregator redirect URLs to the real ATS so they can be tailored.
    from jobagent.pipeline.resolve import resolve_pending  # noqa: PLC0415

    resolved, attempted = await resolve_pending(200, 0)
    log.info("discover_done", new=new_jobs, updated=updated,
             resolved=resolved, resolve_attempted=attempted)
    return new_jobs, updated


async def _score() -> tuple[int, int]:
    """Phase 3: enrich + filter + LLM-score discovered jobs."""
    from jobagent.pipeline.enrich import run_enrichment        # noqa: PLC0415
    from jobagent.pipeline.filters import run_filters          # noqa: PLC0415
    from jobagent.pipeline.score import _run_scoring_async     # noqa: PLC0415

    enriched, _ = await run_enrichment()
    passed, _ = run_filters()
    # Use the async variant directly — run_scoring() calls asyncio.run() which
    # would raise "cannot be called from a running event loop" inside the scheduler.
    queued, rejected = await _run_scoring_async(limit=200, embed_threshold=0.35)
    log.info("score_done", enriched=enriched, passed=passed, queued=queued, rejected=rejected)
    return queued, rejected


async def _tailor(limit: int) -> tuple[int, int]:
    """Phase 4: build tailored resume + cover letter + answers for queued jobs."""
    from jobagent.tailor.orchestrator import run_tailoring  # noqa: PLC0415
    tailored, failed = await run_tailoring(limit=limit)
    log.info("tailor_done", tailored=tailored, failed=failed)
    return tailored, failed


async def _apply(limit: int, force_shadow: bool) -> tuple[int, int]:
    """Phase 5b: submit applications via ATS handlers."""
    from jobagent.apply.orchestrator import run_apply  # noqa: PLC0415
    submitted, shadow_stopped, failed = await run_apply(limit=limit, force_shadow=force_shadow)
    log.info("apply_done", submitted=submitted, shadow_stopped=shadow_stopped, failed=failed)
    return submitted, shadow_stopped


async def _track() -> tuple[int, int]:
    """Phase 6: scan Gmail for replies and update application statuses."""
    from jobagent.tracking.gmail_watcher import run_gmail_watcher  # noqa: PLC0415
    counts = await run_gmail_watcher()
    return counts.get("status_changed", 0), counts.get("ghosted", 0)


async def _summary() -> tuple[int, int]:
    """Print a run summary."""
    from jobagent.tracking.summary import print_summary, build_run_summary  # noqa: PLC0415
    from jobagent.llm.cost import session_stats  # noqa: PLC0415
    stats = session_stats()
    log.info("run_summary", llm_stats=stats)
    summary = build_run_summary(since_hours=24)
    print_summary(summary)
    # TODO Phase 6: Telegram digest
    return 0, 0
