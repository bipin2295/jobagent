"""Apply orchestrator: submit applications for tailored packages.

Flow:
  applications with status='tailored'
    → check daily cap and company cooldown
    → detect ATS type → select handler
    → open browser → fill form → shadow mode gate or submit
    → update application status and record AppEvent
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from jobagent.apply.base import ApplyHandlerBase, ApplyResult
from jobagent.apply.browser import close_all
from jobagent.apply.guardrails import (
    check_company_cooldown,
    check_daily_cap,
    check_stop_sentinel,
    human_like_pause,
    is_active_hours,
    load_guardrails,
)
from jobagent.config import settings, load_yaml
from jobagent.db.engine import get_session
from jobagent.db.models import Application, AppEvent, Job, Company
from jobagent.profile.loader import load_profile
from sqlmodel import select

log = structlog.get_logger(__name__)


_APPLY_BUTTON_SELECTORS = [
    "a[href*='apply']:not([href='#'])",
    "a:text-matches('apply now|apply for|apply to|quick apply', 'i')",
    "button:text-matches('apply now|apply for', 'i')",
    "[data-testid*='apply']",
    ".apply-button, .btn-apply, .job-apply",
]


async def _resolve_ats_via_redirect(job: Job) -> str | None:
    """Navigate to the apply URL, click Apply if needed, and detect the real ATS."""
    from jobagent.apply.browser import new_page  # noqa: PLC0415
    from jobagent.pipeline.ats_detect import detect_ats  # noqa: PLC0415

    target = job.apply_url or job.url
    if not target:
        return None

    async def _detect_and_persist(url: str) -> str | None:
        ats = detect_ats(url)
        if ats and ats != "unknown":
            log.info("apply.ats_resolved", job_id=job.id, ats=ats, url=url[:80])
            try:
                with get_session() as s:
                    db_job = s.get(Job, job.id)
                    if db_job:
                        db_job.ats_type = ats
                        db_job.apply_url = url
                        s.add(db_job)
                        s.commit()
            except Exception:
                pass
            return ats
        return None

    try:
        async with new_page("resolver") as page:
            await page.goto(target, wait_until="domcontentloaded", timeout=20000)

            # Check the final URL after navigation
            result = await _detect_and_persist(page.url)
            if result:
                return result

            # Try every Apply button / link on the aggregator page.
            # Strategy A: read the href without clicking (fast, no new tab).
            # Strategy B: click and capture the popup (Adzuna opens a new tab).
            for selector in _APPLY_BUTTON_SELECTORS:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() == 0 or not await btn.is_visible():
                        continue

                    # A: href-only (same-tab or absolute link)
                    href = await btn.get_attribute("href") or ""
                    if href.startswith("http"):
                        result = await _detect_and_persist(href)
                        if result:
                            return result

                    # B: click and watch for popup / new-tab (Adzuna pattern)
                    popup_url: str | None = None
                    async def _capture_popup(popup) -> None:  # noqa: E501
                        nonlocal popup_url
                        try:
                            await popup.wait_for_load_state(
                                "domcontentloaded", timeout=10000
                            )
                            popup_url = popup.url
                        except Exception:
                            pass

                    page.on("popup", _capture_popup)
                    try:
                        await btn.click(timeout=6000)
                        await page.wait_for_timeout(3000)  # give popup time to load
                    except Exception:
                        pass
                    page.remove_listener("popup", _capture_popup)

                    if popup_url:
                        result = await _detect_and_persist(popup_url)
                        if result:
                            return result

                    # Also check if the same page navigated
                    result = await _detect_and_persist(page.url)
                    if result:
                        return result
                    break
                except Exception:
                    continue

    except Exception as exc:
        log.debug("apply.redirect_resolve_failed", job_id=job.id, error=str(exc))
    return None


# ── Handler registry ──────────────────────────────────────────────────────────

def _load_handlers() -> list[ApplyHandlerBase]:
    """Import and return all registered ATS handlers."""
    handlers: list[ApplyHandlerBase] = []
    try:
        from jobagent.apply.handlers.greenhouse import GreenhouseHandler
        handlers.append(GreenhouseHandler())
    except ImportError:
        pass
    try:
        from jobagent.apply.handlers.lever import LeverHandler
        handlers.append(LeverHandler())
    except ImportError:
        pass
    try:
        from jobagent.apply.handlers.ashby import AshbyHandler
        handlers.append(AshbyHandler())
    except ImportError:
        pass
    try:
        from jobagent.apply.handlers.email_apply import EmailApplyHandler
        handlers.append(EmailApplyHandler())
    except ImportError:
        pass
    try:
        from jobagent.apply.handlers.workday import WorkdayHandler
        handlers.append(WorkdayHandler())
    except ImportError:
        pass
    return handlers


def _select_handler(job: Job, handlers: list[ApplyHandlerBase]) -> ApplyHandlerBase | None:
    # First: match on stored ats_type
    for h in handlers:
        if h.can_handle(job):
            return h

    # Second: for aggregator URLs (adzuna, jooble, reed, remotive…), try to
    # infer the ATS from the apply_url pattern before giving up
    if job.apply_url:
        from jobagent.pipeline.ats_detect import detect_ats  # noqa: PLC0415
        inferred = detect_ats(job.apply_url)
        if inferred and inferred != "unknown":
            # Temporarily patch so can_handle matches
            original_ats = job.ats_type
            job.ats_type = inferred
            for h in handlers:
                if h.can_handle(job):
                    log.info("apply.ats_inferred", job_id=job.id, inferred=inferred)
                    return h
            job.ats_type = original_ats  # restore if no match

    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_tailored_applications(
    session,
    limit: int,
    visa_confirmed: bool = False,
) -> list[tuple[Application, Job]]:
    # Prioritise jobs with a known ATS type (greenhouse, lever, ashby) first —
    # these are the ones we can actually handle. Unknown-ATS jobs go last.
    # Within the same priority tier, visa-confirmed jobs come first.
    from sqlalchemy import case  # noqa: PLC0415
    priority = case(
        (Job.ats_type.in_(["greenhouse", "lever", "ashby", "smartrecruiters"]), 0),
        else_=1,
    )
    # visa_ok jobs float to top within each tier
    visa_priority = case((Job.visa_ok == True, 0), else_=1)  # noqa: E712
    stmt = (
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .where(
            Application.status == "tailored",
            Application.resume_path.isnot(None),
        )
        .order_by(priority, visa_priority, Job.score.desc())
        .limit(limit)
    )
    if visa_confirmed:
        stmt = stmt.where(Job.visa_ok == True)  # noqa: E712
    return list(session.exec(stmt).all())


def _update_app_status(app_id: int, status: str, result: ApplyResult) -> None:
    with get_session() as session:
        app = session.get(Application, app_id)
        if not app:
            return
        app.status = status
        app.updated_at = datetime.now(timezone.utc)
        if result.confirmation_url:
            app.confirmation_path = result.confirmation_url
        session.add(app)

        # Keep the job row in sync with the application (DB golden standard:
        # a submitted application means its job is 'applied'). Without this the
        # job stayed 'queued' and could be re-tailored and re-applied to.
        if status == "submitted" and app.job_id:
            job = session.get(Job, app.job_id)
            if job is not None and job.status != "applied":
                job.status = "applied"
                session.add(job)

        event = AppEvent(
            application_id=app_id,
            ts=datetime.now(timezone.utc),
            from_status="tailored",
            to_status=status,
            note=result.error or result.confirmation_text or "",
        )
        session.add(event)
        session.commit()


# ── Core apply task ───────────────────────────────────────────────────────────

async def _apply_one(
    app: Application,
    job: Job,
    handler: ApplyHandlerBase,
    shadow_mode: bool,
    guardrails: Any,
    manual_mode: bool = False,
) -> ApplyResult:
    """Run the full apply flow for one application."""
    # Pacing
    await human_like_pause(handler.ats_type)

    package_dir = settings.data_dir / "applications" / str(job.id)
    package_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "apply.start",
        app_id=app.id,
        job_id=job.id,
        title=job.title,
        ats=handler.ats_type,
        shadow=shadow_mode,
        manual=manual_mode,
    )

    try:
        result = await handler.apply(
            job, app, package_dir, shadow_mode, manual_mode=manual_mode
        )
    except Exception as exc:
        log.error("apply.unhandled_error", app_id=app.id, error=str(exc))
        result = ApplyResult(
            success=False,
            application_id=app.id,
            status="failed",
            error=str(exc),
        )

    # Map handler status to DB status
    if result.status == "submitted":
        db_status = "submitted"
    elif result.status == "shadow_stopped":
        db_status = "shadow_stopped"
    elif result.status == "blocked":
        db_status = "blocked"
    elif result.status == "skipped":
        db_status = "skipped"
    else:
        db_status = "failed"

    _update_app_status(app.id, db_status, result)

    log.info(
        "apply.done",
        app_id=app.id,
        job_id=job.id,
        status=db_status,
        shadow=shadow_mode,
    )
    return result


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_apply(
    limit: int = 20,
    force_shadow: bool | None = None,
    manual_mode: bool = False,
    visa_confirmed: bool = False,
) -> tuple[int, int, int]:
    """Submit applications for the top *limit* tailored packages.

    When *manual_mode* is set, each form is filled automatically and then
    handed to the operator for the final Submit click; ``shadow_mode`` still
    takes precedence if it is active.

    When *visa_confirmed* is set, only jobs where ``visa_ok=True`` are
    processed.  Useful when you only want to apply to roles where the company
    has explicitly confirmed visa sponsorship.

    Returns (submitted, shadow_stopped, failed).
    """
    guardrails = load_guardrails()
    # None  → respect guardrails.yaml (default)
    # True  → force shadow ON  (--shadow flag)
    # False → force shadow OFF (--live flag, overrides guardrails)
    if force_shadow is None:
        shadow_mode = guardrails.shadow_mode
    else:
        shadow_mode = force_shadow
    handlers = _load_handlers()

    if not handlers:
        log.warning("apply.no_handlers", note="No handlers loaded — check apply/handlers/")
        return 0, 0, 0

    if not is_active_hours():
        log.info("apply.outside_active_hours")
        return 0, 0, 0

    if check_stop_sentinel():
        log.warning("apply.stop_sentinel_active")
        return 0, 0, 0

    # Check global daily cap
    cap_ok, cap_reason = check_daily_cap()
    if not cap_ok:
        log.info("apply.daily_cap_reached", reason=cap_reason)
        return 0, 0, 0

    with get_session() as session:
        pairs = _get_tailored_applications(session, limit, visa_confirmed=visa_confirmed)

    if not pairs:
        log.info("apply.no_tailored_applications")
        return 0, 0, 0

    log.info(
        "apply.starting",
        total=len(pairs),
        shadow=shadow_mode,
        manual=manual_mode,
        handlers=[h.name for h in handlers],
    )

    submitted = shadow_stopped = failed = 0

    # Apply sequentially (not parallel) — browsers are heavy and form filling
    # requires careful pacing to avoid bot detection.
    # For jobs with unknown ATS, we do a lightweight redirect-follow to detect
    # the real ATS before dispatching.
    try:
        for app, job in pairs:
            if check_stop_sentinel():
                log.info("apply.stop_sentinel_during_run")
                break

            # ATS focus gate — skip jobs not in the active_ats whitelist
            # (e.g. while focused on Greenhouse only, skip Workday/aggregator jobs)
            if guardrails.active_ats and job.ats_type not in guardrails.active_ats:
                log.debug(
                    "apply.ats_focus_skip",
                    job_id=job.id,
                    ats_type=job.ats_type,
                    active_ats=guardrails.active_ats,
                )
                _update_app_status(app.id, "skipped", ApplyResult(
                    success=False, application_id=app.id,
                    status="skipped",
                    error=f"ats_type={job.ats_type!r} not in active_ats={guardrails.active_ats}",
                ))
                continue

            # Company cooldown check
            cool_ok, cool_reason = check_company_cooldown(
                job.company_id, guardrails.per_company_cooldown_days
            )
            if not cool_ok:
                log.info("apply.cooldown_skip", job_id=job.id, reason=cool_reason)
                _update_app_status(app.id, "skipped", ApplyResult(
                    success=False, application_id=app.id,
                    status="skipped", error=cool_reason,
                ))
                continue

            # Resolve ATS for aggregator URLs via redirect-following
            if not job.ats_type or job.ats_type == "unknown":
                resolved_ats = await _resolve_ats_via_redirect(job)
                if resolved_ats:
                    job.ats_type = resolved_ats

            handler = _select_handler(job, handlers)
            if not handler:
                log.info("apply.no_handler", job_id=job.id, ats_type=job.ats_type)
                _update_app_status(app.id, "skipped", ApplyResult(
                    success=False, application_id=app.id,
                    status="skipped",
                    error=f"No handler for ats_type={job.ats_type!r}",
                ))
                # Count as skipped, not failed — no handler is expected for
                # aggregator URLs (ats_type=None). Failed is reserved for
                # handlers that crashed or forms that couldn't be submitted.
                continue

            result = await _apply_one(
                app, job, handler, shadow_mode, guardrails, manual_mode=manual_mode
            )

            if result.status == "submitted":
                submitted += 1
            elif result.status == "shadow_stopped":
                shadow_stopped += 1
            elif result.status == "skipped":
                pass  # operator skip / cooldown — not a failure
            else:
                failed += 1

            # Operator asked to stop the run from the manual prompt
            if result.extra.get("quit"):
                log.info("apply.operator_quit", app_id=app.id)
                break

    finally:
        # Always close browser contexts cleanly
        await close_all()

    log.info(
        "apply.run_complete",
        submitted=submitted,
        shadow_stopped=shadow_stopped,
        failed=failed,
        shadow_mode=shadow_mode,
    )
    return submitted, shadow_stopped, failed


# ── Status helpers ────────────────────────────────────────────────────────────

def get_apply_summary() -> dict[str, int]:
    """Return counts by application status for CLI display."""
    from sqlmodel import func
    with get_session() as session:
        result = {}
        for status in ["tailored", "submitted", "shadow_stopped", "failed", "blocked", "skipped"]:
            n = session.exec(
                select(func.count(Application.id)).where(Application.status == status)
            ).one()  # type: ignore[call-overload]
            if n:
                result[status] = n
        return result
