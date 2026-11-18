"""ATS-agnostic form helpers shared by apply handlers.

These were originally private to the Greenhouse handler. Greenhouse, Ashby and
Lever all render the same kinds of widgets (React uploaders, React-select
comboboxes, multi-step forms), so the logic lives here once and each handler
wires it in.

Every function takes an explicit *scope* (a ``Page`` or a form-scoped
``Locator``) so it works for both inline forms and iframes.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Seed fillers used to detect whether a resume was attached.
FILE_INPUT_SELECTORS: tuple[str, ...] = (
    "input[type=file][id='resume']",
    "input[type=file][name*='resume' i]",
    "input[type=file][id*='resume' i]",
    "input[type=file][data-testid*='resume' i]",
)

#: Button labels that open a native file chooser.
UPLOAD_BUTTON_LABELS: tuple[str, ...] = (
    "Attach",
    "Upload",
    "Choose file",
    "Add resume",
    "Upload resume",
    "Upload CV",
    "Attach resume",
)


async def click_robust(loc: Any, timeout: int = 8000) -> None:  # noqa: ANN401
    """Click *loc*, falling back to a forced click when something intercepts it.

    Sticky headers, cookie backdrops and floating reCAPTCHA badges routinely
    sit over form controls, making a normal click fail its hit-target check.
    """
    try:
        await loc.click(timeout=timeout)
    except Exception:
        await loc.click(timeout=timeout, force=True)


async def dismiss_consent(page: Any) -> bool:  # noqa: ANN401
    """Dismiss a cookie/consent modal if one is covering the page.

    These modals use ``aria-modal`` backdrops that intercept pointer events, so
    any click below them times out. Best-effort; never raises.
    """
    for selector in (
        "button:text-matches('^(Accept all|Accept All|Accept|I accept|Agree|"
        "I agree|Got it|OK|Allow all)$', 'i')",
        ".consent-modal button, .cookie-modal button, .cookie-banner button",
        "[aria-label='Cookie consent'] button",
        "[id*='cookie'] button, [class*='cookie'] button",
    ):
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click(timeout=5000)
                await page.wait_for_timeout(600)
                log.debug("form_utils.consent_dismissed", selector=selector[:40])
                return True
        except Exception:
            continue
    return False


async def resume_attached(scope: Any, filename: str) -> bool:  # noqa: ANN401
    """True when a file input holds a file or the filename is shown on the form."""
    inputs = scope.locator("input[type=file]")
    for i in range(await inputs.count()):
        try:
            if await inputs.nth(i).evaluate("e => (e.files ? e.files.length : 0)"):
                return True
        except Exception:
            continue
    try:
        return filename.lower() in (await scope.inner_text()).lower()
    except Exception:
        return False


async def upload_resume(page: Any, scope: Any, path: str | Path) -> bool:  # noqa: ANN401
    """Upload the resume through the tenant's widget and verify it stuck.

    Some React uploaders clear the file input after a programmatic
    ``set_input_files`` (so ``files.length`` returns 0 and the field stays
    "required"). Try the specific resume inputs, then a real file-chooser
    click, then any file input — verifying after each.
    """
    p = Path(path)
    if not p.exists():
        log.warning("form_utils.resume_missing", path=str(p))
        return False

    for sel in FILE_INPUT_SELECTORS:
        loc = scope.locator(sel)
        if await loc.count() == 0:
            continue
        try:
            await loc.first.set_input_files(str(p), timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        if await resume_attached(scope, p.name):
            log.info("form_utils.resume_uploaded", method="direct", path=str(p))
            return True

    for text in UPLOAD_BUTTON_LABELS:
        btn = scope.get_by_role("button", name=text)
        if await btn.count() == 0:
            continue
        try:
            async with page.expect_file_chooser(timeout=6000) as fc_info:
                await click_robust(btn.first)
            chooser = await fc_info.value
            await chooser.set_files(str(p))
        except Exception:
            continue
        await asyncio.sleep(2)
        if await resume_attached(scope, p.name):
            log.info(
                "form_utils.resume_uploaded", method=f"chooser:{text}", path=str(p)
            )
            return True

    any_input = scope.locator("input[type=file]")
    if await any_input.count() > 0:
        try:
            await any_input.first.set_input_files(str(p), timeout=8000)
            await asyncio.sleep(1.5)
        except Exception:
            pass
        if await resume_attached(scope, p.name):
            log.info("form_utils.resume_uploaded", method="fallback", path=str(p))
            return True

    log.warning("form_utils.resume_upload_failed", path=str(p))
    return False


async def form_country_mismatch(scope: Any, job: Any) -> str | None:  # noqa: ANN401
    """Return an ISO code if the form asks to work in a different country.

    Forms often ask "Are you legally authorized to work in <country>?". If that
    country is neither the candidate's home country nor the job's country, the
    posting is for a role we cannot take, so the caller aborts before submit.
    """
    from jobagent.pipeline.normalise import detect_country  # noqa: PLC0415

    try:
        text = await scope.inner_text()
    except Exception:
        return None

    job_country = (getattr(job, "country", None) or "").upper()
    for m in re.finditer(
        r"(?:authori[sz]ed|eligible|permitted|right)\s+to\s+work\s+in\s+([^\n?.!;]{2,40})",
        text,
        re.IGNORECASE,
    ):
        iso = detect_country(m.group(1).strip())
        if not iso or iso == "IN":
            continue
        if job_country and iso == job_country:
            continue
        return iso
    return None


async def validation_errors(scope: Any) -> list[str]:  # noqa: ANN401
    """Collect visible validation messages after a blocked submit."""
    found: list[str] = []
    for sel in ("[aria-invalid='true']", "[role='alert']", ".error", "[class*='error']"):
        loc = scope.locator(sel)
        for i in range(min(await loc.count(), 6)):
            try:
                text = (await loc.nth(i).inner_text()).strip()
                if text:
                    found.append(re.sub(r"\s+", " ", text)[:140])
            except Exception:
                continue
    try:
        body = await scope.inner_text()
        for line in body.splitlines():
            text = line.strip()
            if re.search(
                r"this field is required|please enter|is required|invalid|must be",
                text,
                re.IGNORECASE,
            ):
                found.append(text[:140])
    except Exception:
        pass
    return list(dict.fromkeys(found))[:8]


async def handle_security_code(
    page: Any,  # noqa: ANN401
    scope: Any,  # noqa: ANN401
    since: datetime,
    sender_domain: str = "greenhouse",
) -> bool:
    """Complete a post-submit email security-code step.

    Some tenants finalise only after an emailed code is entered. Reads the code
    from Gmail and enters it into either the single-character boxes or one code
    field, then clicks the submit button. Returns True when a code was entered.
    """
    inputs = scope.locator("input[maxlength='1']")
    single_boxes = await inputs.count()
    field = scope.locator(
        "input[name*='code' i], input[id*='code' i], "
        "input[placeholder*='code' i], input[autocomplete='one-time-code']"
    )
    has_field = await field.count() > 0
    if single_boxes < 4 and not has_field:
        return False

    log.info(
        "form_utils.security_code_prompt",
        single_boxes=single_boxes,
        single_field=has_field,
    )
    try:
        from jobagent.email_provider import EmailProvider  # noqa: PLC0415

        provider = EmailProvider()
        if not provider.connected:
            log.warning("form_utils.security_code_no_gmail")
            return False
        res = await provider.wait_for_verification_email(
            sender_domain=sender_domain,
            since=since,
            timeout_seconds=180,
            subject_hint="Security",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("form_utils.security_code_email_error", error=str(exc)[:120])
        return False

    otp = (res.otp or "").strip()
    if not otp:
        log.warning("form_utils.security_code_missing")
        return False

    try:
        if single_boxes >= len(otp):
            for i, ch in enumerate(otp):
                box = inputs.nth(i)
                try:
                    await box.fill(ch, timeout=3000)
                except Exception:
                    await box.click()
                    await page.keyboard.type(ch)
            log.info("form_utils.security_code_entered_boxes", boxes=len(otp))
        else:
            target = field.first
            await click_robust(target)
            await target.fill(otp)
            log.info("form_utils.security_code_entered_field")
    except Exception as exc:  # noqa: BLE001
        log.warning("form_utils.security_code_fill_error", error=str(exc)[:120])
        return False

    await asyncio.sleep(1)
    for name in ("Submit application", "Submit", "Verify", "Confirm", "Continue"):
        btn = page.get_by_role("button", name=name)
        if await btn.count() > 0:
            try:
                await click_robust(btn.first)
                log.info("form_utils.security_code_submitted", button=name)
            except Exception:
                pass
            break
    await asyncio.sleep(6)
    return True


async def confirm_via_email(company: str | None, since: datetime) -> bool:
    """Cross-check Gmail for an application-received email."""
    try:
        from jobagent.email_provider import EmailProvider  # noqa: PLC0415

        provider = EmailProvider()
        if not provider.connected:
            return False
        return await provider.wait_for_confirmation_email(
            since=since, company=company, timeout_seconds=75
        )
    except Exception:
        return False


def submitted_at_now(slack_seconds: int = 30) -> datetime:
    """Lower bound for emailed codes/confirmations created by this submission."""
    return datetime.now(UTC) - timedelta(seconds=slack_seconds)


async def visible_captcha(page: Any) -> bool:  # noqa: ANN401
    """True only when a CAPTCHA widget is actually visible and interactive.

    Most ATS forms embed an *invisible* reCAPTCHA Enterprise widget: it lives in
    the DOM and runs at submit time but renders nothing. Counting DOM nodes
    therefore blocked every Ashby/Lever form with "CAPTCHA detected". Only a
    visible, non-invisible widget is a real blocker.
    """
    for selector in (
        ".g-recaptcha",
        "[data-sitekey]",
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "[data-testid*='captcha']",
    ):
        loc = page.locator(selector)
        for i in range(await loc.count()):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                size = (await el.get_attribute("data-size") or "").lower()
                src = (await el.get_attribute("src") or "").lower()
                if "invisible" in size or "invisible" in src:
                    continue
                # A visible reCAPTCHA badge is ~256x60; anchors are tiny.
                box = await el.bounding_box()
                if box and box["width"] >= 120 and box["height"] >= 40:
                    log.info(
                        "form_utils.captcha_visible",
                        selector=selector[:40],
                        size=f"{box['width']:.0f}x{box['height']:.0f}",
                    )
                    return True
            except Exception:
                continue
    return False
