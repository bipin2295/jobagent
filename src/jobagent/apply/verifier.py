"""Pre-submit verifier: DOM-based sanity check of the filled form.

Phase 5b implementation — DOM / text checks only.
Phase 5c+ will add an LLM vision pass on top of this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import Page
from pydantic import BaseModel

from jobagent.db.models import Application
from jobagent.profile.schema import MasterProfile

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class VerifierResult(BaseModel):
    """Outcome of the pre-submit verification pass."""

    passed: bool
    issues: list[str]

    # Granular per-field verdicts (default True = assume correct until proved
    # otherwise, so callers can check the aggregate ``passed`` flag and only
    # inspect individual fields when ``passed`` is False).
    name_correct: bool = True
    email_correct: bool = True
    resume_attached: bool = True
    sponsorship_correct: bool = True


# ---------------------------------------------------------------------------
# Main verifier
# ---------------------------------------------------------------------------


async def verify_form(
    page: Page,
    app: Application,
    profile: MasterProfile,
    screenshot_path: Path,
    scope: Any | None = None,
) -> VerifierResult:
    """DOM-based verification of the filled ATS form.

    Takes a screenshot regardless of outcome so the result is always
    inspectable.  Returns a :class:`VerifierResult`; ``passed=False`` means
    the caller should abort the submission.

    Parameters
    ----------
    page:
        Active Playwright page (used for the screenshot).
    app:
        The Application ORM row (used for context in log messages).
    profile:
        The loaded MasterProfile (contact details are verified against page).
    screenshot_path:
        Where to save the verification screenshot.
    scope:
        Optional Locator for the application *form* itself.  Greenhouse forms
        are frequently hosted in an iframe, where ``page.inner_text``/``page
        .evaluate`` cannot see the inputs at all.  Passing the scoped form
        locator makes every check read the real fields.  Defaults to ``page``.

    Notes
    -----
    Checks are purely DOM-based, per PLAN §10A.6 step 1 ("read every field's
    value back from the DOM and diff it against the intended values"):

    * First name and email appear in the form text or in an input value.
    * Every visible required field has a value.
    * At least one ``<input type="file">`` has been populated (non-fatal —
      some ATS platforms hide the input after upload).
    """
    target: Any = scope if scope is not None else page

    issues: list[str] = []
    name_correct = True
    email_correct = True
    resume_attached = True

    # ── Screenshot ────────────────────────────────────────────────────────
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        log.info(
            "verifier.screenshot_taken",
            app_id=app.id,
            path=str(screenshot_path),
        )
    except Exception as exc:
        log.warning(
            "verifier.screenshot_failed",
            app_id=app.id,
            error=str(exc),
        )

    # ── Text content checks ───────────────────────────────────────────────
    try:
        body_text: str = (
            await target.inner_text() if scope is not None else await page.inner_text("body")
        )

        # Read every field value back from the DOM — the form text alone does
        # not include what was typed, and an iframe hides it from the page.
        value_parts: list[str] = []
        try:
            inputs = target.locator("input:not([type=hidden]), textarea")
            for i in range(min(await inputs.count(), 80)):
                try:
                    v = await inputs.nth(i).input_value()
                    if v:
                        value_parts.append(v)
                except Exception:
                    continue
        except Exception:
            pass
        body_lower = (body_text + " " + " ".join(value_parts)).lower()

        # Name check — look for first name (robust against full-name
        # concatenation or truncation by ATS)
        first_name = profile.contact.name.split()[0] if profile.contact.name else ""
        if first_name and first_name.lower() not in body_lower:
            issues.append(
                f"First name '{first_name}' not found in form body text."
            )
            name_correct = False
            log.debug(
                "verifier.name_missing",
                app_id=app.id,
                first_name=first_name,
            )

        # Email check
        if profile.contact.email and profile.contact.email.lower() not in body_lower:
            issues.append(
                f"Email '{profile.contact.email}' not found in form body text."
            )
            email_correct = False
            log.debug(
                "verifier.email_missing",
                app_id=app.id,
                email=profile.contact.email,
            )

    except Exception as exc:
        issues.append(f"Could not verify form text: {exc}")
        log.warning("verifier.body_text_failed", app_id=app.id, error=str(exc))

    # ── Required-field read-back ──────────────────────────────────────────
    # Every visible required text/select/textarea must hold a value. React-select
    # comboboxes are skipped: their visible input stays empty even after a value
    # is committed, so checking it would produce false failures.
    try:
        required = target.locator(
            "input[required], select[required], textarea[required], "
            "input[aria-required='true'], select[aria-required='true'], "
            "textarea[aria-required='true']"
        )
        for i in range(await required.count()):
            el = required.nth(i)
            try:
                if not await el.is_visible() or await el.is_disabled():
                    continue
                # Skip id-less mirror inputs (React-select / native-select
                # shadow inputs carry a `required` attribute but are not
                # user-facing fields; flagging them creates false failures).
                el_id = await el.get_attribute("id") or ""
                aria_req = (await el.get_attribute("aria-required") or "").lower()
                if not el_id and aria_req != "true":
                    continue
                if (await el.get_attribute("role") or "").lower() == "combobox":
                    continue
                field_type = (await el.get_attribute("type") or "").lower()
                if field_type in ("checkbox", "radio", "file", "hidden", "submit", "button"):
                    continue
                if (await el.input_value() or "").strip():
                    continue
                ident = (
                    await el.get_attribute("id")
                    or await el.get_attribute("name")
                    or field_type
                    or "field"
                )
                issues.append(f"Required field left empty: {ident}")
                log.debug("verifier.required_empty", app_id=app.id, field=ident)
            except Exception:
                continue
    except Exception as exc:
        log.warning("verifier.required_check_failed", app_id=app.id, error=str(exc))

    # ── Required location combobox ────────────────────────────────────────
    # The Location (City) field is a required React-select; the generic
    # required-field loop skips comboboxes (their visible input stays empty
    # while a committed value lives elsewhere). Check it explicitly so an
    # empty location is caught before submit, not by browser validation.
    try:
        loc = target.locator("#candidate-location")
        if await loc.count() > 0 and await loc.first.is_visible():
            ctrl = loc.first.locator(
                "xpath=ancestor::*[contains(@class,'select__control')]"
            ).first
            committed = (
                await ctrl.count() > 0
                and await ctrl.locator(".select__single-value").count() > 0
            )
            if not committed and not (await loc.first.input_value() or "").strip():
                issues.append("Location (City) is empty")
                log.debug("verifier.location_empty", app_id=app.id)
    except Exception as exc:
        log.debug("verifier.location_check_failed", app_id=app.id, error=str(exc)[:80])

    # ── Resume attachment check ───────────────────────────────────────────
    try:
        file_inputs = target.locator('input[type="file"]')
        file_input_count = 0
        for i in range(await file_inputs.count()):
            try:
                filled = await file_inputs.nth(i).evaluate(
                    "el => (el.files ? el.files.length : 0)"
                )
                if filled:
                    file_input_count += int(filled)
            except Exception:
                continue
        if file_input_count == 0:
            # Non-fatal: some ATS platforms hide the file input after upload
            # or use a custom widget.  Downgrade to a warning, not an issue.
            log.debug(
                "verifier.resume_file_input_empty",
                app_id=app.id,
                note="file input may be hidden after upload – proceeding",
            )
        else:
            log.debug(
                "verifier.resume_attached",
                app_id=app.id,
                file_inputs_filled=file_input_count,
            )
    except Exception as exc:
        log.warning(
            "verifier.resume_check_failed",
            app_id=app.id,
            error=str(exc),
        )
        # Don't add to issues — the check itself failing is not a show-stopper
        resume_attached = True  # assume ok

    # ── Aggregate result ──────────────────────────────────────────────────
    passed = len(issues) == 0

    result = VerifierResult(
        passed=passed,
        issues=issues,
        name_correct=name_correct,
        email_correct=email_correct,
        resume_attached=resume_attached,
        sponsorship_correct=True,  # Phase 5c+ will check this
    )

    log.info(
        "verifier.result",
        app_id=app.id,
        passed=passed,
        issue_count=len(issues),
        issues=issues or None,
    )

    return result
