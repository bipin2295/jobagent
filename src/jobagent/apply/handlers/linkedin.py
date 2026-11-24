"""LinkedIn Easy Apply handler.

Only works for postings that offer **Easy Apply** (an in-LinkedIn modal).
Postings that say "Apply on company website" are external and are returned as
``skipped`` — LinkedIn does not expose the underlying ATS, so there is nothing
to automate.

Authentication: uses the persistent profile ``data/browser_profiles/linkedin``.
Sign in once through noVNC (see ``scripts/open_browser.py``); cookies are reused
afterwards. If the session is missing, LinkedIn redirects to /login and this
handler returns ``blocked`` rather than attempting a login (no stored password,
and automated logins are a fast route to account restriction).

Safety posture for this channel is deliberately conservative:
  * it never invents answers — unknown required fields abort with ``failed``
  * a CAPTCHA/verification wall returns ``blocked``
  * ``--manual`` is the intended mode: the modal is filled and handed over
"""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path

import structlog

from jobagent.apply.base import ApplyHandlerBase, ApplyResult
from jobagent.apply.browser import new_page
from jobagent.apply.field_mapper import build_field_values
from jobagent.apply.form_utils import (
    click_robust,
    confirm_via_email,
    dismiss_consent,
    submitted_at_now,
    upload_resume,
)

log = structlog.get_logger(__name__)

_MAX_STEPS = 8


def _company(job) -> str:  # type: ignore[no-untyped-def]
    try:
        from jobagent.db.engine import get_session  # noqa: PLC0415
        from jobagent.db.models import Company  # noqa: PLC0415

        if job.company_id:
            with get_session() as s:
                c = s.get(Company, job.company_id)
            if c and c.name:
                return c.name
    except Exception:
        pass
    return ""


async def _needs_login(page) -> bool:  # type: ignore[no-untyped-def]
    url = (page.url or "").lower()
    if "/login" in url or "/authwall" in url:
        return True
    try:
        body = (await page.inner_text("body"))[:1500].lower()
    except Exception:
        return False
    return "sign in" in body and "join now" in body


async def _easy_apply_button(page):  # type: ignore[no-untyped-def]
    """Locate the Easy Apply control, scrolling to force lazy rendering."""
    for _ in range(3):
        btn = page.locator("button:has-text('Easy Apply')")
        if await btn.count() > 0:
            return btn.first
        await page.mouse.wheel(0, 900)
        await asyncio.sleep(1.5)
    return None


async def _advance_modal(page) -> bool:  # type: ignore[no-untyped-def]
    """Advance the Easy Apply modal. Returns False if no advance control.

    Order matters: the final page offers "Submit application" (NOT "Review"),
    and clicking "Review" again on the review page is a no-op that would spin
    until the step limit. So prefer the most-specific forward action, and never
    press Review once the review page is showing.
    """
    for label in ("Next", "Continue", "Review"):
        btn = page.locator(f"button:visible:has-text('{label}')")
        if await btn.count() > 0:
            await click_robust(btn.first)
            await asyncio.sleep(2)
            return True
    return False


class LinkedInHandler(ApplyHandlerBase):
    """Apply handler for LinkedIn Easy Apply postings."""

    name = "linkedin"
    ats_type = "linkedin"

    async def apply(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        job,  # type: ignore[no-untyped-def]
        app,  # type: ignore[no-untyped-def]
        package_dir: Path,
        shadow_mode: bool,
        manual_mode: bool = False,
    ) -> ApplyResult:
        shot_dir = package_dir / "screenshots"
        shot_dir.mkdir(exist_ok=True)

        try:
            answers: dict = (
                json.loads((package_dir / "answers.json").read_text(encoding="utf-8"))
                if (package_dir / "answers.json").exists()
                else {}
            )
        except Exception:
            answers = {}

        resume_path = Path(app.resume_path) if app.resume_path else None

        try:
            from jobagent.profile.loader import load_profile  # noqa: PLC0415

            profile = load_profile()
        except Exception as exc:  # noqa: BLE001
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"profile load failed: {exc}",
            )

        url = job.apply_url or job.url or ""
        if "linkedin.com/jobs" not in url:
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="skipped",
                error="Not a LinkedIn job URL (aggregator link) — cannot Easy Apply.",
            )

        try:
            async with new_page("linkedin") as page:
                log.info("linkedin.navigate", job_id=job.id, url=url[:90])
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await asyncio.sleep(3)
                await dismiss_consent(page)

                if await _needs_login(page):
                    await page.screenshot(path=str(shot_dir / "step_login_wall.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error=(
                            "LinkedIn session missing or expired. Sign in once via "
                            "noVNC (python scripts/open_browser.py linkedin "
                            "https://www.linkedin.com/login) and retry."
                        ),
                        screenshot_before=shot_dir / "step_login_wall.png",
                    )

                btn = await _easy_apply_button(page)
                if btn is None:
                    await page.screenshot(path=str(shot_dir / "step_no_easy_apply.png"))
                    try:
                        body = (await page.inner_text("body"))[:3000].lower()
                    except Exception:
                        body = ""
                    if "apply on company" in body or "company website" in body:
                        reason = "External application (Apply on company website)."
                    else:
                        reason = "No Easy Apply button found on this posting."
                    log.info("linkedin.no_easy_apply", job_id=job.id, reason=reason[:60])
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="skipped",
                        error=reason,
                        screenshot_before=shot_dir / "step_no_easy_apply.png",
                    )

                await click_robust(btn)
                await asyncio.sleep(3)
                await page.screenshot(path=str(shot_dir / "step_modal_open.png"))

                # The modal renders as a plain overlay (no stable role/class in
                # current LinkedIn markup). Detect it by its heading and the
                # paged footer instead of CSS classes.
                async def _modal_present() -> bool:
                    for probe in (
                        "h2:has-text('Apply to')",
                        "h3:has-text('Apply to')",
                        "text=/\\d+\\/\\d+ pages?/i",
                        "button:visible:has-text('Next')",
                    ):
                        try:
                            if await page.locator(probe).count() > 0:
                                return True
                        except Exception:
                            continue
                    return False

                if not await _modal_present():
                    await page.screenshot(path=str(shot_dir / "step_modal_missing.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error="Easy Apply modal did not open after clicking the button.",
                        screenshot_before=shot_dir / "step_modal_missing.png",
                    )

                field_values = build_field_values(profile, job, app.answers_json or "{}")

                # Walk the modal steps: fill what is present, then advance.
                from jobagent.apply.field_mapper import fill_all_common_fields  # noqa: PLC0415

                for step in range(_MAX_STEPS):
                    await fill_all_common_fields(page, field_values, None)

                    if resume_path is not None and resume_path.exists():
                        await upload_resume(page, page, resume_path)

                    # Custom screening questions (notice period, years of
                    # experience, …) are not covered by the common patterns.
                    # LinkedIn shows "Invalid input" and blocks Review until
                    # each required field holds a value.
                    from jobagent.apply.question_engine import (  # noqa: PLC0415
                        answer_custom_questions,
                    )

                    unresolved = await answer_custom_questions(
                        page, page, job, profile
                    )
                    if unresolved:
                        await page.screenshot(
                            path=str(shot_dir / f"step_unanswered_{step:02d}.png")
                        )
                        log.warning(
                            "linkedin.unanswered",
                            job_id=job.id,
                            step=step,
                            questions=[q[:60] for q in unresolved[:4]],
                        )
                        # Skip rather than fail: some postings ask things that
                        # cannot be answered truthfully (custom eligibility
                        # criteria, company-specific questions). Marking these
                        # "skipped" keeps them distinct from real failures and
                        # lets the run move to the next posting.
                        return ApplyResult(
                            success=False,
                            application_id=app.id,
                            status="skipped",
                            error=(
                                "Easy Apply asks question(s) that cannot be "
                                "answered truthfully: "
                                + "; ".join(unresolved[:4])
                            ),
                            screenshot_before=shot_dir
                            / f"step_unanswered_{step:02d}.png",
                        )

                    await page.screenshot(
                        path=str(shot_dir / f"step_modal_{step:02d}.png")
                    )

                    # Final step: LinkedIn labels it "Submit application".
                    submit = page.locator(
                        "button:visible:has-text('Submit application')"
                    )
                    if await submit.count() > 0:
                        log.info("linkedin.reached_submit", job_id=job.id, step=step)
                        break

                    if not await _advance_modal(page):
                        log.warning("linkedin.modal_stuck", job_id=job.id, step=step)
                        await page.screenshot(
                            path=str(shot_dir / "step_modal_stuck.png")
                        )
                        return ApplyResult(
                            success=False,
                            application_id=app.id,
                            status="failed",
                            error=(
                                f"Easy Apply modal stalled at step {step}: no "
                                "Next/Review/Submit control found."
                            ),
                            screenshot_before=shot_dir / "step_modal_stuck.png",
                        )

                await page.screenshot(path=str(shot_dir / "step_before_submit.png"))

                if shadow_mode:
                    log.info("linkedin.shadow_stopped", job_id=job.id)
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="shadow_stopped",
                        screenshot_before=shot_dir / "step_before_submit.png",
                    )

                if manual_mode:
                    from jobagent.apply.manual import (  # noqa: PLC0415
                        await_manual_submit,
                        focus_form,
                    )

                    await focus_form(page)
                    decision = await await_manual_submit(
                        company=_company(job),
                        title=job.title or "(untitled)",
                        url=page.url,
                    )
                    await page.screenshot(path=str(shot_dir / "step_manual.png"))
                    if decision == "submitted":
                        return ApplyResult(
                            success=True,
                            application_id=app.id,
                            status="submitted",
                            confirmation_url=page.url,
                            confirmation_text="Submitted manually by operator",
                            screenshot_before=shot_dir / "step_before_submit.png",
                            screenshot_after=shot_dir / "step_manual.png",
                        )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="skipped",
                        error=f"Manual hand-off: operator chose {decision!r}",
                        extra={"quit": decision == "quit"},
                    )

                # ── Live submit ──────────────────────────────────────────────
                submit = page.locator("button:visible:has-text('Submit application')")
                if await submit.count() == 0:
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error="Submit application button not found.",
                        screenshot_before=shot_dir / "step_before_submit.png",
                    )

                submitted_at = submitted_at_now()
                log.info("linkedin.submit_clicked", job_id=job.id)
                await submit.first.click(timeout=15_000)
                await asyncio.sleep(5)
                await page.screenshot(path=str(shot_dir / "step_after_submit.png"))

                body = ""
                try:
                    body = (await page.inner_text("body"))[:2500].lower()
                except Exception:
                    pass

                if "your application was sent" in body or "application sent" in body:
                    log.info("linkedin.submitted", job_id=job.id)
                    return ApplyResult(
                        success=True,
                        application_id=app.id,
                        status="submitted",
                        confirmation_url=page.url,
                        confirmation_text="Application sent (LinkedIn Easy Apply)",
                        screenshot_before=shot_dir / "step_before_submit.png",
                        screenshot_after=shot_dir / "step_after_submit.png",
                    )

                # Fall back to the Gmail receipt before declaring failure.
                if await confirm_via_email(_company(job), submitted_at):
                    return ApplyResult(
                        success=True,
                        application_id=app.id,
                        status="submitted",
                        confirmation_url=page.url,
                        confirmation_text="Confirmed via email",
                        screenshot_before=shot_dir / "step_before_submit.png",
                        screenshot_after=shot_dir / "step_after_submit.png",
                    )

                return ApplyResult(
                    success=False,
                    application_id=app.id,
                    status="failed",
                    error="Confirmation not detected after clicking Submit application.",
                    screenshot_before=shot_dir / "step_before_submit.png",
                    screenshot_after=shot_dir / "step_after_submit.png",
                )

        except Exception as exc:  # noqa: BLE001
            log.exception("linkedin.apply_error", job_id=job.id, error=str(exc))
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            )
