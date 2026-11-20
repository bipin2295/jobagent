"""Lever apply handler.

Lever apply flow:
1. Build the form URL: <job_url>/apply  (Lever always places the form here).
   If apply_url is already set and ends with '/apply', use it as-is.
2. Navigate to the form URL.
3. Fill standard fields: name, email, phone, company, resume, LinkedIn, Twitter.
4. Fill the cover letter textarea if present (Lever calls it "Additional
   information" or "Comments" on many tenants).
5. Handle any dropdown custom questions using the pre-built answers.
6. Handle required checkboxes (consent/GDPR).
7. Shadow mode gate → screenshot + stop / live → click Submit.
8. Detect confirmation page and return ApplyResult.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import structlog

from jobagent.apply.base import ApplyHandlerBase, ApplyResult
from jobagent.apply.browser import new_page
from jobagent.apply.field_mapper import build_field_values, fill_all_common_fields
from jobagent.apply.form_utils import (
    confirm_via_email,
    form_country_mismatch,
    submitted_at_now,
    upload_resume,
    validation_errors,
    visible_captcha,
)
from jobagent.apply.guardrails import human_like_pause
from jobagent.apply.question_engine import answer_custom_questions
from jobagent.apply.verifier import verify_form
from jobagent.db.models import Application, Job
from jobagent.profile.loader import load_profile

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _build_lever_apply_url(job: Job) -> str:
    """Return the Lever /apply URL for *job*.

    Lever always places the application form at ``<job-url>/apply``.
    If ``job.apply_url`` is already set and already ends with ``/apply``,
    return it unchanged to avoid double-appending.
    """
    if job.apply_url:
        url = job.apply_url.rstrip("/")
        return url if url.endswith("/apply") else url + "/apply"
    base = (job.url or "").rstrip("/")
    return base if base.endswith("/apply") else base + "/apply"


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


async def _handle_dropdown_questions(page, answers: dict) -> None:  # type: ignore[type-arg]
    """Attempt to fill Lever custom-question dropdowns from the answers dict.

    Lever renders custom questions as plain <select> elements with a
    preceding <label>.  We match the select's id or name against keys in
    *answers* and pick the closest matching option text.
    """
    selects = page.locator("select")
    count = await selects.count()
    for i in range(count):
        sel = selects.nth(i)
        try:
            sel_id: str = (await sel.get_attribute("id")) or ""
            sel_name: str = (await sel.get_attribute("name")) or ""
            # Find a matching answer key
            matched_val: str | None = None
            for key, val in answers.items():
                k = key.lower()
                if k in sel_id.lower() or k in sel_name.lower():
                    matched_val = str(val)
                    break
            if matched_val is None:
                continue
            options: list[str] = await sel.locator("option").all_inner_texts()
            for opt in options:
                if matched_val.lower() in opt.lower():
                    await sel.select_option(label=opt.strip())
                    log.debug(
                        "lever.dropdown_filled",
                        sel_id=sel_id,
                        value=opt.strip(),
                    )
                    break
        except Exception as exc:  # noqa: BLE001
            log.debug("lever.dropdown_skip", index=i, error=str(exc))


async def _handle_checkboxes(page) -> None:  # type: ignore[type-arg]
    """Check every required, unchecked checkbox."""
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
                log.debug("lever.checkbox_checked", index=i)
        except Exception as exc:  # noqa: BLE001
            log.debug("lever.checkbox_skip", index=i, error=str(exc))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class LeverHandler(ApplyHandlerBase):
    """Apply handler for Lever-hosted job postings."""

    name = "lever"
    ats_type = "lever"

    async def apply(  # noqa: PLR0912, PLR0915
        self,
        job: Job,
        app: Application,
        package_dir: Path,
        shadow_mode: bool,
        manual_mode: bool = False,
    ) -> ApplyResult:
        """Execute the full Lever application flow.

        Never raises — all exceptions are caught and surfaced via ApplyResult.
        """
        shot_dir = package_dir / "screenshots"
        shot_dir.mkdir(exist_ok=True)

        # ── Load pre-built answers ───────────────────────────────────────────
        answers_path = package_dir / "answers.json"
        try:
            answers: dict = (
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
            async with new_page("lever") as page:

                # ── Step 1: Navigate to /apply ───────────────────────────────
                target_url = _build_lever_apply_url(job)
                log.info("lever.navigate", url=target_url, job_id=job.id)
                await page.goto(target_url, wait_until="domcontentloaded")
                await human_like_pause()
                await page.screenshot(path=str(shot_dir / "step_01_loaded.png"))

                # ── Step 2: Wait for form ────────────────────────────────────
                try:
                    await page.wait_for_selector(
                        "form.application-form, "
                        "form[data-qa='application-form'], "
                        ".application-form, "
                        "form",
                        timeout=15_000,
                    )
                except Exception:
                    pass
                await page.screenshot(path=str(shot_dir / "step_02_form.png"))

                # ── Step 3: CAPTCHA check (pre-fill) ────────────────────────
                if await visible_captcha(page):
                    await page.screenshot(path=str(shot_dir / "step_captcha_blocked.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error="CAPTCHA detected before form fill",
                        screenshot_before=shot_dir / "step_02_form.png",
                    )

                # ── Step 4: Fill common fields ───────────────────────────────
                field_values = build_field_values(profile, job, app.answers_json or "{}")
                resume_path = Path(app.resume_path) if app.resume_path else None
                await fill_all_common_fields(page, field_values, resume_path)
                await human_like_pause()

                # ── Step 4b: Location typeahead ──────────────────────────────
                # Lever's location field is an autocomplete that .fill() leaves
                # uncommitted; the required field then fails validation.
                city_text = field_values.get("city") or ""
                if city_text:
                    loc_input = page.locator(
                        "input[id*='location' i][role='combobox'], "
                        "input[id*='location' i], "
                        "input[placeholder*='location' i]"
                    )
                    if await loc_input.count() > 0:
                        target = loc_input.first
                        try:
                            await target.click(timeout=5000)
                            await target.fill("", timeout=3000)
                            await target.type(city_text, delay=90)
                            await asyncio.sleep(2.5)
                            opts = page.locator("[role=option]")
                            picked = False
                            for i in range(await opts.count()):
                                txt = (await opts.nth(i).inner_text()).strip()
                                if not txt or await opts.nth(i).get_attribute("data-dial-code"):
                                    continue
                                if city_text.split(",")[0].lower() in txt.lower():
                                    await opts.nth(i).click()
                                    picked = True
                                    break
                            if not picked:
                                await target.press("Tab")
                            log.debug("lever.location_set", city=city_text, picked=picked)
                        except Exception as exc:
                            log.debug("lever.location_error", error=str(exc)[:80])

                # ── Step 5: Cover letter / additional information ─────────────
                cl_path = package_dir / "cover_letter.txt"
                if cl_path.exists():
                    # Lever tenants use "comments", "additional information",
                    # or "cover letter" as the textarea label — try all three.
                    cl_textarea = page.locator(
                        "textarea[name*='comments'], "
                        "textarea[id*='comments'], "
                        "textarea[name*='cover'], "
                        "textarea[name*='additional'], "
                        "textarea[placeholder*='cover' i], "
                        "textarea[aria-label*='cover' i], "
                        "textarea[aria-label*='additional' i], "
                        "textarea[aria-label*='comments' i]"
                    )
                    if await cl_textarea.count() > 0:
                        await cl_textarea.first.fill(
                            cl_path.read_text(encoding="utf-8")
                        )
                        log.debug("lever.cover_letter_filled")

                # ── Step 6: Dropdown custom questions ────────────────────────
                await _handle_dropdown_questions(page, answers)
                await human_like_pause()

                # ── Step 7: Required checkboxes ──────────────────────────────
                await _handle_checkboxes(page)

                # ── Step 7b: Resume upload verification ──────────────────────
                # Lever's uploader can silently drop a programmatically-set
                # file; verify it stuck before continuing.
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

                # ── Step 7c: Country guard ───────────────────────────────────
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

                # ── Step 7d: LLM answer engine for remaining questions ───────
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

                # ── Step 8: Pre-submit screenshot + verification ──────────────
                await page.screenshot(path=str(shot_dir / "step_03_filled.png"))

                verification = await verify_form(
                    page, app, profile, shot_dir / "step_03_verify.png", scope=page
                )
                if not verification.passed:
                    await page.screenshot(
                        path=str(shot_dir / "step_03_verify_failed.png")
                    )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "Pre-submit verifier failed: "
                            + "; ".join(verification.issues[:6])
                        ),
                        screenshot_before=shot_dir / "step_03_verify_failed.png",
                    )

                # ── Step 9: Shadow mode gate ─────────────────────────────────
                if shadow_mode:
                    log.info("lever.shadow_stopped", job_id=job.id)
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="shadow_stopped",
                        screenshot_before=shot_dir / "step_03_filled.png",
                    )

                # ── Step 10: Find and click Submit ───────────────────────────
                # Lever pages contain a HIDDEN `#hcaptchaSubmitBtn` alongside the
                # real submit button. Selecting `.first` therefore matched the
                # invisible one and the click timed out — visible only failures
                # (shadow mode never reaches submit, so it passed there).
                submit_btn = page.locator(
                    "input[type='submit']:visible, "
                    "button[type='submit']:visible, "
                    "button:visible:has-text('Submit Application'), "
                    "button:visible:has-text('Submit')"
                )
                if await submit_btn.count() == 0:
                    await page.screenshot(path=str(shot_dir / "step_no_submit.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error="Submit button not found (no visible submit control)",
                        screenshot_before=shot_dir / "step_03_filled.png",
                    )

                submitted_at = submitted_at_now()
                log.info("lever.submit_clicked", job_id=job.id)
                await submit_btn.first.click(timeout=15000)
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                await human_like_pause()

                # ── Step 11: Post-submit screenshot ─────────────────────────
                await page.screenshot(path=str(shot_dir / "step_04_after_submit.png"))

                # ── Step 12: CAPTCHA check (post-submit) ─────────────────────
                if await visible_captcha(page):
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error="CAPTCHA appeared after submit",
                        screenshot_before=shot_dir / "step_03_filled.png",
                        screenshot_after=shot_dir / "step_04_after_submit.png",
                    )

                # ── Step 13: Confirmation ────────────────────────────────────
                confirmed = await _is_confirmation_page(page)

                # Cross-check Gmail: Lever always emails a receipt, even when
                # the confirmation UI is not recognised.
                if not confirmed:
                    confirmed = await confirm_via_email(None, submitted_at)

                if confirmed:
                    log.info("lever.submitted", job_id=job.id, url=page.url)
                    return ApplyResult(
                        success=True,
                        application_id=app.id,
                        status="submitted",
                        confirmation_url=page.url,
                        confirmation_text="Application submitted successfully via Lever",
                        screenshot_before=shot_dir / "step_03_filled.png",
                        screenshot_after=shot_dir / "step_04_after_submit.png",
                    )

                validation = await validation_errors(page)
                if validation:
                    log.warning("lever.submit_blocked", job_id=job.id, errors=validation)

                return ApplyResult(
                    success=False,
                    application_id=app.id,
                    status="failed",
                    error="Confirmation page not detected after submit",
                    screenshot_before=shot_dir / "step_03_filled.png",
                    screenshot_after=shot_dir / "step_04_after_submit.png",
                )

        except Exception as exc:  # noqa: BLE001
            log.exception("lever.apply_error", job_id=job.id, error=str(exc))
            before = shot_dir / "step_03_filled.png"
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
