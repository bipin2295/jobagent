"""Apply guardrails: cap checks, shadow-mode gate, pacing, STOP sentinel.

All public functions are designed to be called at the top of the apply loop
before any browser automation starts.  They are cheap (DB reads + file stats)
and must never raise.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import Page
from pydantic import BaseModel, Field

from jobagent.config import load_yaml, settings
from jobagent.db.engine import get_session
from jobagent.db.models import Application

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pydantic model – mirrors config/guardrails.yaml structure
# ---------------------------------------------------------------------------


class PacingConfig(BaseModel):
    """Timing constraints between individual application attempts."""

    min_seconds: float = 60.0
    max_seconds: float = 180.0
    active_hours: dict[str, int] = Field(
        default_factory=lambda: {"start": 8, "end": 22}
    )


class BudgetConfig(BaseModel):
    """Per-application cost / token budget."""

    max_llm_cost_usd: float = 0.25
    max_steps: int = 60
    max_pages: int = 15


class ApplicationGuardrails(BaseModel):
    """Runtime guardrails loaded from ``config/guardrails.yaml``."""

    # Hard cap on total applications submitted per calendar day
    daily_cap: int = 20
    # Number of days that must pass before reapplying to the same company
    per_company_cooldown_days: int = 90
    # Minimum score (0–100) a job must have to be applied to
    min_score: int = 60
    # When True the agent fills forms but never clicks Submit
    shadow_mode: bool = True
    # Number of shadow runs required before auto-graduating to live mode
    shadow_min_runs: int = 5
    # Fraction of shadow runs that must succeed to graduate
    shadow_success_threshold: float = 0.8
    # Per-channel daily caps: {"linkedin_easy_apply": 10, ...}
    per_channel_caps: dict[str, int] = Field(default_factory=dict)
    # Restrict discovery, tailoring and apply to these ATS types only.
    # Empty list = all supported ATS types are active.
    active_ats: list[str] = Field(default_factory=list)
    # Human-like pacing between applications
    pacing: PacingConfig = Field(default_factory=PacingConfig)
    # LLM + browser cost budget per application
    per_application_budget: BudgetConfig = Field(default_factory=BudgetConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_guardrails() -> ApplicationGuardrails:
    """Load ``config/guardrails.yaml`` and return an :class:`ApplicationGuardrails`.

    Missing keys fall back to the model's defaults, so the function always
    succeeds even when the config file is absent.
    """
    raw: dict[str, Any] = load_yaml("guardrails")

    # Nested models need explicit construction from sub-dicts
    if "pacing" in raw and isinstance(raw["pacing"], dict):
        raw["pacing"] = PacingConfig(**raw["pacing"])
    if "per_application_budget" in raw and isinstance(
        raw["per_application_budget"], dict
    ):
        raw["per_application_budget"] = BudgetConfig(**raw["per_application_budget"])

    return ApplicationGuardrails(**raw)


# ---------------------------------------------------------------------------
# Cap checks
# ---------------------------------------------------------------------------


def check_daily_cap(channel: str = "default") -> tuple[bool, str]:
    """Return ``(allowed, reason)`` against the daily application cap.

    Counts rows in the ``application`` table whose ``submitted_at`` is today
    (UTC).  Also checks the per-channel cap when ``channel`` has an entry in
    :attr:`ApplicationGuardrails.per_channel_caps`.

    Parameters
    ----------
    channel:
        ATS channel name, e.g. ``"linkedin_easy_apply"`` or ``"greenhouse"``.
        Use ``"default"`` to skip the per-channel check.
    """
    guardrails = load_guardrails()
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )

    with get_session() as session:
        from sqlmodel import select, func as sqlfunc

        # Total count across all channels today
        stmt = select(sqlfunc.count(Application.id)).where(  # type: ignore[arg-type]
            Application.submitted_at >= today_start,
            Application.status == "submitted",
        )
        total_today: int = session.exec(stmt).one()  # type: ignore[assignment]

    if total_today >= guardrails.daily_cap:
        reason = (
            f"Daily cap reached: {total_today}/{guardrails.daily_cap} "
            f"applications submitted today."
        )
        log.info("guardrails.daily_cap_reached", total=total_today, cap=guardrails.daily_cap)
        return False, reason

    # Per-channel cap
    if channel != "default" and channel in guardrails.per_channel_caps:
        channel_cap = guardrails.per_channel_caps[channel]
        with get_session() as session:
            from sqlmodel import select, func as sqlfunc

            stmt_ch = select(sqlfunc.count(Application.id)).where(  # type: ignore[arg-type]
                Application.submitted_at >= today_start,
                Application.status == "submitted",
                Application.channel == channel,
            )
            channel_today: int = session.exec(stmt_ch).one()  # type: ignore[assignment]

        if channel_today >= channel_cap:
            reason = (
                f"Per-channel cap reached for '{channel}': "
                f"{channel_today}/{channel_cap} applications today."
            )
            log.info(
                "guardrails.channel_cap_reached",
                channel=channel,
                count=channel_today,
                cap=channel_cap,
            )
            return False, reason

    return True, f"OK ({total_today}/{guardrails.daily_cap} today)"


def check_company_cooldown(
    company_id: int | None,
    cooldown_days: int | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for the per-company reapplication cooldown.

    Parameters
    ----------
    company_id:
        The ``Company.id`` associated with the job.  When ``None`` (company
        unknown) the check is skipped and ``(True, "no company")`` is
        returned.
    cooldown_days:
        Override the value from guardrails.  Defaults to
        :attr:`ApplicationGuardrails.per_company_cooldown_days`.
    """
    if company_id is None:
        return True, "no company – cooldown skipped"

    guardrails = load_guardrails()
    days = cooldown_days if cooldown_days is not None else guardrails.per_company_cooldown_days

    with get_session() as session:
        from sqlmodel import select
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Find the most recent *submitted* application for this company
        # Use a simple subquery to avoid SQLAlchemy join complexity
        from jobagent.db.models import Job as JobModel  # noqa: PLC0415
        from sqlalchemy import text  # noqa: PLC0415

        job_ids_stmt = select(JobModel.id).where(
            JobModel.company_id == company_id,
        )
        job_ids = [row for row in session.exec(job_ids_stmt).all()]

        if not job_ids:
            return True, ""

        stmt = (
            select(Application)
            .where(
                Application.status == "submitted",
                Application.submitted_at >= cutoff,
                Application.job_id.in_(job_ids),  # type: ignore[union-attr]
            )
            .order_by(Application.submitted_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )

        try:
            recent = session.exec(stmt).first()
        except Exception:
            # Fallback: skip cooldown if query fails (schema may not have join)
            log.warning(
                "guardrails.company_cooldown_query_failed",
                company_id=company_id,
            )
            return True, "cooldown query failed – proceeding"

    if recent is not None and recent.submitted_at is not None:
        delta = datetime.now(timezone.utc) - recent.submitted_at.replace(
            tzinfo=timezone.utc
        )
        remaining = days - delta.days
        reason = (
            f"Company cooldown active: applied {delta.days} day(s) ago; "
            f"{remaining} day(s) remaining (cooldown={days}d)."
        )
        log.info(
            "guardrails.company_cooldown_active",
            company_id=company_id,
            days_since=delta.days,
            cooldown=days,
        )
        return False, reason

    return True, f"OK (no recent application within {days}d)"


# ---------------------------------------------------------------------------
# STOP sentinel
# ---------------------------------------------------------------------------


def check_stop_sentinel() -> bool:
    """Return ``True`` if the ``data/STOP`` file exists.

    Callers should treat ``True`` as "halt all activity immediately".
    The file can be created by the operator at any time to emergency-stop
    the agent.

    >>> from pathlib import Path
    >>> Path("data/STOP").touch()   # stop the agent  # doctest: +SKIP
    """
    stop_path: Path = settings.data_dir / "STOP"
    exists = stop_path.exists()
    if exists:
        log.warning("guardrails.stop_sentinel_detected", path=str(stop_path))
    return exists


# ---------------------------------------------------------------------------
# Shadow-mode gate
# ---------------------------------------------------------------------------


async def shadow_mode_gate(
    page: Page,
    app_id: int,
    screenshot_dir: Path,
) -> bool:
    """Take a pre-submit screenshot and gate on shadow mode.

    Always takes a screenshot of the current page (before the submit button
    would be clicked).  Returns ``True`` only when shadow mode is **off**,
    meaning the caller may proceed to click Submit.

    Parameters
    ----------
    page:
        Active Playwright page positioned at the final review / submit step.
    app_id:
        Application ID used to name the screenshot file.
    screenshot_dir:
        Directory where the screenshot is saved.

    Returns
    -------
    bool
        ``True`` → submit is allowed.
        ``False`` → shadow mode active; caller must NOT submit.
    """
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / f"app_{app_id}_before_submit.png"

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        log.info(
            "guardrails.screenshot_taken",
            app_id=app_id,
            path=str(screenshot_path),
        )
    except Exception as exc:
        log.warning(
            "guardrails.screenshot_failed",
            app_id=app_id,
            error=str(exc),
        )

    guardrails = load_guardrails()
    if guardrails.shadow_mode:
        log.info(
            "shadow_mode.stopped_before_submit",
            app_id=app_id,
            screenshot=str(screenshot_path),
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Human-like pacing
# ---------------------------------------------------------------------------


async def human_like_pause(channel: str = "default") -> None:
    """Sleep for a random duration within the configured pacing window.

    Parameters
    ----------
    channel:
        Reserved for future per-channel pacing overrides.  Currently unused.
    """
    guardrails = load_guardrails()
    pacing = guardrails.pacing
    delay = random.uniform(pacing.min_seconds, pacing.max_seconds)
    log.debug("guardrails.pacing_pause", seconds=round(delay, 1), channel=channel)
    await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Active-hours check
# ---------------------------------------------------------------------------


def is_active_hours() -> bool:
    """Return ``True`` if the current local hour is within the configured window.

    Uses the wall-clock time of the machine running the agent (not UTC), so
    the agent respects the operator's local working hours.
    """
    guardrails = load_guardrails()
    start_h: int = guardrails.pacing.active_hours.get("start", 8)
    end_h: int = guardrails.pacing.active_hours.get("end", 22)
    current_hour = datetime.now().hour  # local time
    in_window = start_h <= current_hour < end_h
    if not in_window:
        log.debug(
            "guardrails.outside_active_hours",
            current_hour=current_hour,
            window=f"{start_h:02d}:00–{end_h:02d}:00",
        )
    return in_window
