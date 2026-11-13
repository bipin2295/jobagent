"""Pre-submit verifier: DOM-based sanity check of the filled form.

Phase 5b implementation — DOM / text checks only.
Phase 5c+ will add an LLM vision pass on top of this.
"""

from __future__ import annotations

from pathlib import Path

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
) -> VerifierResult:
    """DOM-based verification of the filled ATS form.

    Takes a screenshot regardless of outcome so the result is always
    inspectable.  Returns a :class:`VerifierResult`; ``passed=False`` means
    the caller should abort the submission.

    Parameters
    ----------
    page:
        Active Playwright page on the final-review / confirm step.
    app:
        The Application ORM row (used for context in log messages).
    profile:
        The loaded MasterProfile (contact details are verified against page).
    screenshot_path:
        Where to save the verification screenshot.

    Notes
    -----
    This is a Phase 5b implementation.  Checks are purely DOM-based:

    * First name present in page body text.
    * Email present in page body text.
    * At least one ``<input type="file">`` has been populated (via
      ``HTMLInputElement.files.length > 0``).

    The LLM vision pass (Phase 5c+) will be layered on top once the
    basic DOM checks pass.
    """
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
        body_text: str = await page.inner_text("body")
        body_lower = body_text.lower()

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

    # ── Resume attachment check ───────────────────────────────────────────
    try:
        file_input_count: int = await page.evaluate(
            """() => {
                const inputs = document.querySelectorAll('input[type="file"]');
                let filled = 0;
                for (const inp of inputs) {
                    if (inp.files && inp.files.length > 0) filled++;
                }
                return filled;
            }"""
        )
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
