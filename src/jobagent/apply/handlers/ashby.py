"""Ashby apply handler.

Ashby apply flow:
1. Navigate to job.apply_url
   (usually https://jobs.ashbyhq.com/{company}/{id}/application).
2. If the page is a job-listing page (not the form yet), click the prominent
   "Apply" button.
3. Wait for the React application form (identified by data-testid attributes
   or aria-labels) to be ready.
4. Fill all common fields using get_by_label() / get_by_role() — Ashby's
   React forms expose accessible labels consistently.
5. Upload resume and fill the cover letter textarea if present.
6. Handle multi-step sections: advance through "Next" / "Continue" buttons,
   re-running fill_all_common_fields on each new page of the form.
7. Handle required checkboxes (consent / GDPR) on each step and on the final
   page before submit.
8. Shadow mode gate → screenshot + stop / live → click Submit.
9. Detect confirmation page and return ApplyResult.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import structlog

from jobagent.apply.base import ApplyHandlerBase, ApplyResult
from jobagent.apply.browser import new_page
from jobagent.apply.form_utils import (
    visible_captcha,
    confirm_via_email,
    form_country_mismatch,
    submitted_at_now,
    upload_resume,
    validation_errors,
)
from jobagent.apply.question_engine import answer_custom_questions
from jobagent.apply.verifier import verify_form
from jobagent.apply.field_mapper import build_field_values, fill_all_common_fields
from jobagent.apply.guardrails import human_like_pause
from jobagent.db.models import Application, Job
from jobagent.profile.loader import load_profile

log = structlog.get_logger(__name__)

# Safety cap to prevent an infinite loop on a runaway multi-step form
_MAX_FORM_STEPS = 10


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _is_confirmation_page(page) -> bool:  # type: ignore[type-arg]
    """Return True if the current page looks like a submission-confirmed page."""
    url = page.url.lower()
    if any(x in url for x in ["confirm", "thank", "success", "submitted", "complete"]):
        return True
    try:
        text = (await page.inner_text("body"))[:500].lower()
        return any(x in text for x in [
            "thank you",
            "application received",
            "successfully applied",
            "we have received",
            "application submitted",
        ])
    except Exception:
        return False


async def _click_ashby_apply_button(page) -> bool:  # type: ignore[type-arg]
    """Click the main Apply button on Ashby job-listing pages.

    Returns True if the button was found and clicked.
    Some Ashby apply_url values already land on the form page, in which case
    no button click is needed.
    """
    apply_btn = (
        page.get_by_role("button", name="Apply").or_(
            page.get_by_role("link", name="Apply")
        ).or_(
            page.get_by_role("button", name="Apply for this job")
        ).or_(
            page.locator(
                "[data-testid='job-apply-button'], "
                "[data-testid*='apply-button'], "
                "a[href*='/application']"
            )
        )
    )
    if await apply_btn.count() > 0:
        await apply_btn.first.click()
        await page.wait_for_load_state("domcontentloaded")
        await human_like_pause()
        log.debug("ashby.apply_button_clicked")
        return True
    return False


async def _has_next_step(page) -> bool:  # type: ignore[type-arg]
    """Return True if a "Next" or "Continue" button is visible — multi-step form."""
    next_btn = (
        page.get_by_role("button", name="Next").or_(
            page.get_by_role("button", name="Continue")
        ).or_(
            page.locator(
                "[data-testid='next-button'], "
                "[data-testid='continue-button'], "
                "button:has-text('Next Step')"
            )
        )
    )
    return await next_btn.count() > 0


async def _advance_to_next_step(page) -> None:  # type: ignore[type-arg]
    """Click the "Next" / "Continue" button to advance the multi-step form."""
    next_btn = (
        page.get_by_role("button", name="Next").or_(
            page.get_by_role("button", name="Continue")
        ).or_(
            page.locator(
                "[data-testid='next-button'], "
                "[data-testid='continue-button'], "
                "button:has-text('Next Step')"
            )
        )
    )
    if await next_btn.count() > 0:
        await next_btn.first.click()
        await page.wait_for_load_state("domcontentloaded")
        await human_like_pause()
        log.debug("ashby.multi_step_advanced")


async def _handle_checkboxes(page) -> None:  # type: ignore[type-arg]
    """Check every required, unchecked checkbox on the current form page."""
    checkboxes = page.locator(
        "input[type='checkbox'][required], "
        "input[type='checkbox'][aria-required='true']"
    )
    count = await checkboxes.count()
    for i in range(count):
        cb = checkboxes.nth(i)
        try:
            if not await cb.is_checked():
                await cb.check()
                log.debug("ashby.checkbox_checked", index=i)
        except Exception as exc:  # noqa: BLE001
            log.debug("ashby.checkbox_skip", index=i, error=str(exc))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _company(job: Job) -> str:
    """Best-effort company name for confirmation-email matching."""
    if job.company_id:
        try:
            from jobagent.db.engine import get_session  # noqa: PLC0415
            from jobagent.db.models import Company  # noqa: PLC0415

            with get_session() as session:
                company = session.get(Company, job.company_id)
            if company and company.name:
                return company.name
        except Exception:
            pass
    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        host = urlparse(job.apply_url or job.url).netloc
        return host.removeprefix("www.").removeprefix("jobs.") or ""
    except Exception:
        return ""


class AshbyHandler(ApplyHandlerBase):
    """Apply handler for Ashby-hosted job postings."""

    name = "ashby"
    ats_type = "ashby"

    async def apply(  # noqa: PLR0912, PLR0915
        self,
        job: Job,
        app: Application,
        package_dir: Path,
        shadow_mode: bool,
        manual_mode: bool = False,
    ) -> ApplyResult:
        """Execute the full Ashby application flow.

        Never raises — all exceptions are caught and surfaced via ApplyResult.
        """
        shot_dir = package_dir / "screenshots"
        shot_dir.mkdir(exist_ok=True)

        # ── Load pre-built answers ───────────────────────────────────────────
        answers_path = package_dir / "answers.json"
        try:
            answers: dict = (  # noqa: F841  (kept for potential dropdown use)
                json.loads(answers_path.read_text(encoding="utf-8"))
                if answers_path.exists()
                else {}
            )
        except Exception:
            answers = {}

        # ── Load candidate profile ───────────────────────────────────────────
        try:
            profile = load_profile()
        except Exception as exc:
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"profile load failed: {exc}",
            )

        try:
            async with new_page("ashby") as page:

                # ── Step 1: Navigate ─────────────────────────────────────────
                target_url: str = job.apply_url or job.url
                log.info("ashby.navigate", url=target_url, job_id=job.id)
                await page.goto(target_url, wait_until="domcontentloaded")
                await human_like_pause()
                await page.screenshot(path=str(shot_dir / "step_01_loaded.png"))

                # ── Step 2: Click main Apply button (job listing → form) ──────
                clicked = await _click_ashby_apply_button(page)
                if clicked:
                    await page.screenshot(
                        path=str(shot_dir / "step_02_apply_clicked.png")
                    )

                # ── Step 3: Wait for application form ────────────────────────
                try:
                    await page.wait_for_selector(
                        "form, "
                        "[data-testid='application-form'], "
                        "[data-testid*='form'], "
                        ".ashby-application-form, "
                        "[aria-label*='application' i]",
                        timeout=15_000,
                    )
                except Exception:
                    pass
                await page.screenshot(path=str(shot_dir / "step_03_form.png"))

                # ── Step 4: CAPTCHA check (pre-fill) ────────────────────────
                if await visible_captcha(page):
                    await page.screenshot(
                        path=str(shot_dir / "step_captcha_blocked.png")
                    )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error="CAPTCHA detected before form fill",
                        screenshot_before=shot_dir / "step_03_form.png",
                    )

                # ── Step 5: Fill common fields (first section) ───────────────
                field_values = build_field_values(profile, job, app.answers_json or "{}")
                resume_path = Path(app.resume_path) if app.resume_path else None
                await fill_all_common_fields(page, field_values, resume_path)
                await human_like_pause()

                # Ashby uses a custom uploader that can silently drop the file.
                if resume_path is not None:
                    if not await upload_resume(page, page, resume_path):
                        await page.screenshot(
                            path=str(shot_dir / "step_resume_failed.png")
                        )
                        return ApplyResult(
                            success=False,
                            application_id=app.id,
                            status="failed",
                            error=(
                                "Resume upload failed: the file input did not "
                                "retain the file."
                            ),
                            screenshot_before=shot_dir / "step_resume_failed.png",
                        )

                # ── Step 6: Cover letter ─────────────────────────────────────
                cl_path = package_dir / "cover_letter.txt"
                if cl_path.exists():
                    # Ashby uses aria-label / data-testid on React textareas
                    cl_textarea = (
                        page.get_by_label("Cover Letter").or_(
                            page.locator(
                                "textarea[aria-label*='cover' i], "
                                "textarea[placeholder*='cover' i], "
                                "textarea[data-testid*='cover'], "
                                "textarea[data-testid*='Cover']"
                            )
                        )
                    )
                    if await cl_textarea.count() > 0:
                        await cl_textarea.first.fill(
                            cl_path.read_text(encoding="utf-8")
                        )
                        log.debug("ashby.cover_letter_filled")

                # ── Step 7: Multi-step form ──────────────────────────────────
                step = 0
                while await _has_next_step(page) and step < _MAX_FORM_STEPS:
                    step += 1
                    await _handle_checkboxes(page)
                    await page.screenshot(
                        path=str(
                            shot_dir / f"step_04_section_{step:02d}_before_next.png"
                        )
                    )
                    await _advance_to_next_step(page)
                    # Refill any fields exposed by the new section
                    await fill_all_common_fields(page, field_values, resume_path)
                    await human_like_pause()
                    await page.screenshot(
                        path=str(
                            shot_dir / f"step_04_section_{step:02d}_after_next.png"
                        )
                    )
                    log.debug("ashby.section_advanced", section=step)

                # ── Step 8: Final required checkboxes ────────────────────────
                await _handle_checkboxes(page)

                # ── Step 8b: Custom questions + country guard ────────────────
                mismatch = await form_country_mismatch(page, job)
                if mismatch:
                    await page.screenshot(
                        path=str(shot_dir / "step_country_mismatch.png")
                    )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            f"Form asks for work authorization in {mismatch}, "
                            f"but the job country is {job.country!r}."
                        ),
                        screenshot_before=shot_dir / "step_country_mismatch.png",
                    )

                unresolved = await answer_custom_questions(page, page, job, profile)
                if unresolved:
                    await page.screenshot(
                        path=str(shot_dir / "step_unanswerable.png")
                    )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "Unanswerable required question(s): "
                            + "; ".join(unresolved[:6])
                        ),
                        screenshot_before=shot_dir / "step_unanswerable.png",
                    )

                # ── Step 9: Pre-submit screenshot ────────────────────────────
                await page.screenshot(path=str(shot_dir / "step_05_filled.png"))

                # ── Step 9b: Read-back verification ──────────────────────────
                verification = await verify_form(
                    page, app, profile, shot_dir / "step_05_verify.png", scope=page
                )
                if not verification.passed:
                    await page.screenshot(
                        path=str(shot_dir / "step_05_verify_failed.png")
                    )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "Pre-submit verifier failed: "
                            + "; ".join(verification.issues[:6])
                        ),
                        screenshot_before=shot_dir / "step_05_verify_failed.png",
                    )

                # ── Step 10: Shadow mode gate ────────────────────────────────
                if shadow_mode:
                    log.info("ashby.shadow_stopped", job_id=job.id)
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="shadow_stopped",
                        screenshot_before=shot_dir / "step_05_filled.png",
                    )

                # ── Step 11: Find and click Submit ───────────────────────────
                submit_btn = (
                    page.get_by_role("button", name="Submit Application").or_(
                        page.get_by_role("button", name="Submit")
                    ).or_(
                        page.locator(
                            "button[type='submit'], "
                            "input[type='submit'], "
                            "[data-testid='submit-button'], "
                            "[data-testid*='submit']"
                        )
                    )
                )
                if await submit_btn.count() == 0:
                    await page.screenshot(path=str(shot_dir / "step_no_submit.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error="Submit button not found",
                        screenshot_before=shot_dir / "step_05_filled.png",
                    )

                submitted_at = submitted_at_now()
                log.info("ashby.submit_clicked", job_id=job.id)
                await submit_btn.first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                await human_like_pause()

                # ── Step 12: Post-submit screenshot ─────────────────────────
                await page.screenshot(path=str(shot_dir / "step_06_after_submit.png"))

                # ── Step 13: CAPTCHA check (post-submit) ─────────────────────
                if await visible_captcha(page):
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error="CAPTCHA appeared after submit",
                        screenshot_before=shot_dir / "step_05_filled.png",
                        screenshot_after=shot_dir / "step_06_after_submit.png",
                    )

                # ── Step 14: Confirmation ────────────────────────────────────
                confirmed = await _is_confirmation_page(page)
                if confirmed:
                    log.info("ashby.submitted", job_id=job.id, url=page.url)
                    return ApplyResult(
                        success=True,
                        application_id=app.id,
                        status="submitted",
                        confirmation_url=page.url,
                        confirmation_text="Application submitted successfully via Ashby",
                        screenshot_before=shot_dir / "step_05_filled.png",
                        screenshot_after=shot_dir / "step_06_after_submit.png",
                    )

                # Cross-check Gmail: a real submission still sends a receipt.
                confirmed = await confirm_via_email(_company(job), submitted_at)
                if confirmed:
                    log.info("ashby.submitted_via_email", job_id=job.id)
                    return ApplyResult(
                        success=True,
                        application_id=app.id,
                        status="submitted",
                        confirmation_url=page.url,
                        confirmation_text="Confirmed via confirmation email",
                        screenshot_before=shot_dir / "step_05_filled.png",
                        screenshot_after=shot_dir / "step_06_after_submit.png",
                    )

                validation = await validation_errors(page)
                if validation:
                    log.warning("ashby.submit_blocked", job_id=job.id, errors=validation)
                return ApplyResult(
                    success=False,
                    application_id=app.id,
                    status="failed",
                    error=(
                        "Confirmation page not detected after submit"
                        + (f" | validation: {'; '.join(validation)}" if validation else "")
                    ),
                    screenshot_before=shot_dir / "step_05_filled.png",
                    screenshot_after=shot_dir / "step_06_after_submit.png",
                )

        except Exception as exc:  # noqa: BLE001
            log.exception("ashby.apply_error", job_id=job.id, error=str(exc))
            before = shot_dir / "step_05_filled.png"
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=(
                    f"{type(exc).__name__}: {exc}\n"
                    f"{traceback.format_exc()}"
                ),
                screenshot_before=before if before.exists() else None,
            )
