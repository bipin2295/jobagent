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
from jobagent.apply.guardrails import human_like_pause, shadow_mode_gate
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
                if await page.locator(
                    ".g-recaptcha, [data-sitekey], iframe[src*='recaptcha']"
                ).count() > 0:
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

                # ── Step 8: Pre-submit screenshot ────────────────────────────
                await page.screenshot(path=str(shot_dir / "step_03_filled.png"))

                # ── Step 9: Shadow mode gate ─────────────────────────────────
                if shadow_mode:
                    log.info("lever.shadow_stopped", job_id=job.id)
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="shadow_stopped",
                        screenshot_before=shot_dir / "step_03_filled.png",
                    )

                await shadow_mode_gate()

                # ── Step 10: Find and click Submit ───────────────────────────
                # Lever typically uses input[type='submit'] or button[type='submit']
                submit_btn = page.locator(
                    "input[type='submit'], "
                    "button[type='submit'], "
                    "button:has-text('Submit Application'), "
                    "button:has-text('Submit')"
                )
                if await submit_btn.count() == 0:
                    await page.screenshot(path=str(shot_dir / "step_no_submit.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error="Submit button not found",
                        screenshot_before=shot_dir / "step_03_filled.png",
                    )

                log.info("lever.submit_clicked", job_id=job.id)
                await submit_btn.first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                await human_like_pause()

                # ── Step 11: Post-submit screenshot ─────────────────────────
                await page.screenshot(path=str(shot_dir / "step_04_after_submit.png"))

                # ── Step 12: CAPTCHA check (post-submit) ─────────────────────
                if await page.locator(
                    ".g-recaptcha, [data-sitekey], iframe[src*='recaptcha']"
                ).count() > 0:
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
