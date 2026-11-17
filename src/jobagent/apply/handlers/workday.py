"""Workday apply handler.

Handles the full 6-page Workday application flow:
  1. My Information  (name, address, phone, email, source)
  2. My Experience   (resume upload → autofill audit, work history, education)
  3. Application Questions (custom per-job questions)
  4. Voluntary Disclosures / Self Identification (EEO)
  5. Review and Submit

Workday uses stable ``data-automation-id`` attributes for most interactive
elements, which makes it unusually automation-friendly compared to custom
React-select builds.

Account management (create / login) is delegated to
:class:`~jobagent.apply.account_manager.AccountManager`.

The handler falls back to manual mode when in ``manual_mode=True``, filling
the form and then handing control to the operator before submit.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import Any

import structlog

from jobagent.apply.base import ApplyHandlerBase, ApplyResult
from jobagent.apply.browser import new_page
from jobagent.apply.guardrails import human_like_pause
from jobagent.apply.manual import await_manual_submit, focus_form
from jobagent.db.models import Application, Job

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Workday page-type constants (data-automation-id values)
# ---------------------------------------------------------------------------
_PAGE_INDICATORS: dict[str, list[str]] = {
    "my_information": [
        "[data-automation-id='legalNameSection']",
        "[data-automation-id='addressSection']",
        "[data-automation-id='phone-section']",
    ],
    "my_experience": [
        "[data-automation-id='resumeSection']",
        "[data-automation-id='workExperienceSection']",
        "[data-automation-id='educationSection']",
    ],
    "application_questions": [
        "[data-automation-id='questionnaire']",
        "[data-automation-id='questionnairePage']",
        "fieldset[data-automation-id]",
    ],
    "voluntary_disclosures": [
        "[data-automation-id='selfIdentification']",
        "[data-automation-id='voluntaryDisclosures']",
        "h2:text-matches('voluntary|self.identif', 'i')",
    ],
    "review": [
        "[data-automation-id='reviewPage']",
        "h2:text-matches('review|summary', 'i')",
        "button[data-automation-id='bottom-navigation-review-button']",
    ],
    "confirmation": [
        "[data-automation-id='confirmationPage']",
        "h1:text-matches('thank you|submitted|received', 'i')",
        "h2:text-matches('application.*submitted|successfully submitted', 'i')",
    ],
    "login": [
        "[data-automation-id='signIn']",
        "[data-automation-id='username']",
        "input[type=password]",
    ],
    "create_account": [
        "[data-automation-id='createAccount']",
        "[data-automation-id='verifyPassword']",
    ],
}

_EEO_PREFER_NOT = {
    "I do not wish to answer",
    "Prefer not to say",
    "Decline to self-identify",
    "I choose not to self-identify",
    "I don't wish to answer",
}


class WorkdayHandler(ApplyHandlerBase):
    """Apply handler for Workday-hosted job postings."""

    name = "workday"
    ats_type = "workday"

    async def apply(
        self,
        job: Job,
        app: Application,
        package_dir: Path,
        shadow_mode: bool,
        manual_mode: bool = False,
    ) -> ApplyResult:
        """Execute the full Workday application flow."""
        shot_dir = package_dir / "screenshots"
        shot_dir.mkdir(exist_ok=True)

        answers_path = package_dir / "answers.json"
        try:
            answers: dict = (
                json.loads(answers_path.read_text(encoding="utf-8"))
                if answers_path.exists()
                else {}
            )
        except Exception:
            answers = {}

        resume_path = Path(app.resume_path) if app.resume_path else None
        cover_path = package_dir / "cover_letter.txt"

        apply_url = job.apply_url or job.url

        try:
            async with new_page("workday") as page:
                await page.screenshot(path=str(shot_dir / "step_01_start.png"))

                # ── Step 1: Account login / creation ─────────────────────────
                from jobagent.apply.account_manager import AccountManager  # noqa: PLC0415
                from jobagent.pipeline.ats_detect import extract_ats_slug   # noqa: PLC0415

                result = extract_ats_slug(apply_url)
                tenant = result[1] if result else "unknown"

                # Workday login URL is usually the base of the apply URL
                from urllib.parse import urlparse  # noqa: PLC0415
                parsed = urlparse(apply_url)
                login_url = f"{parsed.scheme}://{parsed.netloc}/login"

                mgr = AccountManager()
                logged_in = await mgr.ensure_logged_in(
                    page, "workday", tenant, login_url,
                    shadow_mode=shadow_mode,
                )
                if not logged_in and not shadow_mode:
                    log.warning("workday.login_failed", job_id=job.id, tenant=tenant)
                    # Proceed anyway — some Workday jobs allow guest application

                # ── Step 2: Navigate to job ───────────────────────────────────
                log.info("workday.navigate", job_id=job.id, url=apply_url)
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
                await human_like_pause()
                await page.screenshot(path=str(shot_dir / "step_02_job.png"))

                # Click "Apply" button
                for sel in [
                    "a[data-automation-id='applyButton']",
                    "button[data-automation-id='applyButton']",
                    "a:text-matches('^Apply$|^Apply Now$', 'i')",
                    "button:text-matches('^Apply$|^Apply Now$', 'i')",
                    "[data-automation-id='applyCTA']",
                ]:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=10000)
                        await page.wait_for_load_state("domcontentloaded", timeout=20000)
                        await human_like_pause()
                        break

                await page.screenshot(path=str(shot_dir / "step_03_apply_start.png"))

                # ── Step 3: Page loop ─────────────────────────────────────────
                filled_pages: list[str] = []
                for page_num in range(10):  # max 10 pages
                    page_type = await _detect_page_type(page)
                    log.debug("workday.page_detected", page_num=page_num, page_type=page_type)

                    if page_type == "confirmation":
                        await page.screenshot(path=str(shot_dir / "step_confirm.png"))
                        return ApplyResult(
                            success=True,
                            application_id=app.id,
                            status="submitted",
                            confirmation_url=page.url,
                            confirmation_text="Workday application submitted successfully",
                            screenshot_before=shot_dir / "step_03_apply_start.png",
                            screenshot_after=shot_dir / "step_confirm.png",
                        )

                    if page_type == "login":
                        # Re-try login (may have been logged out)
                        await _fill_login_form(page, answers)
                        await human_like_pause()
                        continue

                    if page_type == "my_information":
                        await _fill_my_information(page, answers)
                        filled_pages.append("my_information")

                    elif page_type == "my_experience":
                        await _fill_my_experience(page, answers, resume_path)
                        filled_pages.append("my_experience")

                    elif page_type == "application_questions":
                        await _fill_application_questions(page, answers)
                        filled_pages.append("application_questions")

                    elif page_type == "voluntary_disclosures":
                        await _fill_voluntary_disclosures(page, answers)
                        filled_pages.append("voluntary_disclosures")

                    elif page_type == "review":
                        shot = shot_dir / f"step_review.png"
                        await page.screenshot(path=str(shot))

                        if shadow_mode:
                            return ApplyResult(
                                success=False,
                                application_id=app.id,
                                status="shadow_stopped",
                                screenshot_before=shot,
                            )

                        if manual_mode:
                            await focus_form(page)
                            decision = await await_manual_submit(
                                company=tenant,
                                title=job.title or "(untitled)",
                                url=page.url,
                            )
                            after_shot = shot_dir / "step_manual_after.png"
                            await page.screenshot(path=str(after_shot))
                            if decision == "submitted":
                                return ApplyResult(
                                    success=True, application_id=app.id,
                                    status="submitted",
                                    confirmation_url=page.url,
                                    confirmation_text="Submitted manually by operator",
                                    screenshot_before=shot,
                                    screenshot_after=after_shot,
                                )
                            return ApplyResult(
                                success=False, application_id=app.id,
                                status="skipped",
                                error=f"Manual hand-off: operator chose {decision!r}",
                                screenshot_before=shot,
                                extra={"quit": decision == "quit"},
                            )

                        # Auto submit
                        await _click_submit(page)
                        await asyncio.sleep(5)
                        await page.screenshot(path=str(shot_dir / "step_post_submit.png"))
                        continue

                    # Click "Next" / "Save and Continue" to advance
                    advanced = await _click_next(page)
                    if not advanced:
                        # No next button — might already be on confirmation or stuck
                        await asyncio.sleep(2)
                        new_page_type = await _detect_page_type(page)
                        if new_page_type == page_type:
                            log.warning("workday.stuck", page_type=page_type, job_id=job.id)
                            break
                    await human_like_pause()

                # Reached page limit without confirmation
                return ApplyResult(
                    success=False,
                    application_id=app.id,
                    status="failed",
                    error=f"Did not reach confirmation page. Pages filled: {filled_pages}",
                    screenshot_before=shot_dir / "step_03_apply_start.png",
                )

        except Exception as exc:
            log.exception("workday.apply_error", job_id=job.id, error=str(exc))
            return ApplyResult(
                success=False,
                application_id=app.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            )


# ---------------------------------------------------------------------------
# Page detectors
# ---------------------------------------------------------------------------

async def _detect_page_type(page: Any) -> str:
    for page_type, selectors in _PAGE_INDICATORS.items():
        for sel in selectors:
            try:
                if await page.locator(sel).count() > 0:
                    return page_type
            except Exception:
                continue
    return "unknown"


# ---------------------------------------------------------------------------
# Page fillers
# ---------------------------------------------------------------------------

async def _fill_login_form(page: Any, answers: dict) -> None:
    """Fill the Workday sign-in form."""
    email = answers.get("email", "")
    if not email:
        return
    for sel in ["[data-automation-id='email']", "input[type=email]"]:
        el = page.locator(sel).first
        if await el.count() > 0:
            await el.fill(email)
            break

    btn = page.locator("[data-automation-id='nextBtn']").first
    if await btn.count() > 0:
        await btn.click()
        await asyncio.sleep(2)


async def _fill_my_information(page: Any, answers: dict) -> None:
    """Fill the 'My Information' page."""
    from jobagent.profile.loader import load_profile  # noqa: PLC0415
    try:
        profile = load_profile()
        contact = profile.contact
    except Exception:
        contact = None  # type: ignore[assignment]

    # No hardcoded personal fallbacks: real values come from the git-ignored
    # profile / answer seed. Empty values are skipped by the fill loop below.
    name_parts = (contact.name if contact else answers.get("full_name", "")).split()
    first = name_parts[0] if name_parts else ""
    last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    email = contact.email if contact else answers.get("email", "")
    phone = contact.phone if contact else answers.get("phone", "")
    city = answers.get("city", "")
    country = answers.get("country", "")

    _fills: list[tuple[str, str]] = [
        ("[data-automation-id='legalNameSection'] input[data-automation-id*='firstName']", first),
        ("[data-automation-id='legalNameSection'] input[data-automation-id*='lastName']", last),
        ("[data-automation-id='legalNameSection'] input[placeholder*='first' i]", first),
        ("[data-automation-id='legalNameSection'] input[placeholder*='last' i]", last),
        ("[data-automation-id='email']", email),
        ("[data-automation-id='phone']", phone),
        ("input[data-automation-id='phone-number']", phone.replace("+91", "")),
    ]

    for sel, val in _fills:
        if not val:
            continue
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.fill(val)
        except Exception:
            pass

    # Country/city comboboxes
    await _workday_combobox(page, "[data-automation-id='countryDropdown']", country)
    await _workday_combobox(page, "[data-automation-id='city']", city)

    # How did you hear
    await _workday_combobox(
        page,
        "[data-automation-id='howDidYouHear']",
        answers.get("how_did_you_hear", "Job board"),
    )

    log.debug("workday.my_information_filled")


async def _fill_my_experience(page: Any, answers: dict, resume_path: Path | None) -> None:
    """Fill the 'My Experience' page — upload resume and let Workday parse it."""
    # ── Resume upload ─────────────────────────────────────────────────────────
    if resume_path and resume_path.exists():
        for sel in [
            "input[type=file][data-automation-id*='resume']",
            "input[type=file]",
        ]:
            upload = page.locator(sel).first
            if await upload.count() > 0:
                await upload.set_input_files(str(resume_path))
                await asyncio.sleep(5)  # Workday parses the PDF asynchronously
                log.debug("workday.resume_uploaded")
                break

    # ── Audit auto-parsed fields ──────────────────────────────────────────────
    # Workday pre-fills name, email, phone from the resume. Correct any errors.
    await _fill_my_information(page, answers)

    log.debug("workday.my_experience_filled")


async def _fill_application_questions(page: Any, answers: dict) -> None:
    """Answer custom per-job application questions."""
    import re  # noqa: PLC0415

    # Radio buttons (yes/no questions)
    _RADIO_PATTERNS: list[tuple[str, str]] = [
        ("sponsor.*visa|work.*authoriz|require.*sponsorship", answers.get("require_sponsorship", "Yes")),
        ("legally.*work|right to work|work without", answers.get("right_to_work", "No")),
        ("belarus|cuba|iran|north korea|syria", "No"),
    ]

    radios = page.locator("input[type=radio]")
    for i in range(await radios.count()):
        try:
            radio = radios.nth(i)
            radio_id = await radio.get_attribute("id") or ""
            lbl = page.locator(f"label[for='{radio_id}']")
            lbl_text = (await lbl.first.inner_text()).lower() if await lbl.count() > 0 else ""
            if not lbl_text:
                continue
            for pattern, desired in _RADIO_PATTERNS:
                if re.search(pattern, lbl_text):
                    val = await radio.get_attribute("value") or ""
                    if desired.lower() in val.lower() or desired.lower() in lbl_text:
                        await radio.check()
                        break
        except Exception:
            pass

    # Workday combobox questions
    _CB_PATTERNS: list[tuple[str, str]] = [
        ("sponsor", answers.get("require_sponsorship", "Yes")),
        ("relocat", "willing to relocate"),
        ("how.*hear", answers.get("how_did_you_hear", "Job board")),
    ]

    selects = page.locator("select")
    for i in range(await selects.count()):
        try:
            sel_el = selects.nth(i)
            aria = await sel_el.get_attribute("aria-label") or ""
            sel_id = await sel_el.get_attribute("id") or ""
            lbl_el = page.locator(f"label[for='{sel_id}']")
            lbl = (await lbl_el.first.inner_text() if await lbl_el.count() > 0 else aria).lower()
            for pattern, desired in _CB_PATTERNS:
                if re.search(pattern, lbl):
                    options = await sel_el.locator("option").all_inner_texts()
                    for opt in options:
                        if desired.lower() in opt.lower():
                            await sel_el.select_option(label=opt.strip())
                            break
                    break
        except Exception:
            pass

    log.debug("workday.questions_filled")


async def _fill_voluntary_disclosures(page: Any, answers: dict) -> None:
    """Fill EEO / self-identification section with prefer-not-to-say defaults."""
    _EEO_DEFAULTS: dict[str, str] = {
        "gender": answers.get("gender", "Decline to self-identify"),
        "race":   answers.get("ethnicity", "Decline to self-identify"),
        "ethnicity": answers.get("ethnicity", "Decline to self-identify"),
        "veteran": answers.get("veteran", "I am not a protected veteran"),
        "disability": answers.get("disability", "I do not wish to answer"),
    }

    selects = page.locator("select")
    for i in range(await selects.count()):
        try:
            sel_el = selects.nth(i)
            aria = (await sel_el.get_attribute("aria-label") or "").lower()
            for key, desired in _EEO_DEFAULTS.items():
                if key in aria:
                    options = await sel_el.locator("option").all_inner_texts()
                    best = next((o for o in options if desired.lower() in o.lower()), None)
                    if best is None:
                        best = next((o for o in options if o.strip() in _EEO_PREFER_NOT), None)
                    if best:
                        await sel_el.select_option(label=best.strip())
                    break
        except Exception:
            pass

    # Consent checkboxes
    for cb in await page.locator("input[type=checkbox]").all():
        try:
            if not await cb.is_checked():
                lbl_id = await cb.get_attribute("id") or ""
                lbl = page.locator(f"label[for='{lbl_id}']")
                lbl_text = (await lbl.first.inner_text() if await lbl.count() > 0 else "").lower()
                if any(k in lbl_text for k in ["consent", "acknowledge", "agree", "confirm"]):
                    await cb.check()
        except Exception:
            pass

    log.debug("workday.voluntary_disclosures_filled")


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

_NEXT_SELECTORS = [
    "[data-automation-id='bottom-navigation-next-button']",
    "[data-automation-id='nextButton']",
    "button:text-matches('^Next$|^Save and Continue$|^Continue$', 'i')",
    "button[type=submit]:text-matches('next|continue|save', 'i')",
]

_SUBMIT_SELECTORS = [
    "[data-automation-id='bottom-navigation-review-button']",
    "[data-automation-id='submitButton']",
    "button:text-matches('^Submit$|^Submit Application$', 'i')",
]


async def _click_next(page: Any) -> bool:
    for sel in _NEXT_SELECTORS:
        btn = page.locator(sel).first
        if await btn.count() > 0 and await btn.is_visible():
            await btn.click(timeout=10000, force=True)
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
            return True
    return False


async def _click_submit(page: Any) -> None:
    for sel in _SUBMIT_SELECTORS:
        btn = page.locator(sel).first
        if await btn.count() > 0 and await btn.is_visible():
            await btn.click(timeout=10000, force=True)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            log.info("workday.submit_clicked")
            return


async def _workday_combobox(page: Any, selector: str, desired: str) -> None:
    """Fill a Workday typeahead/combobox by typing and selecting the best match."""
    try:
        el = page.locator(selector).first
        if await el.count() == 0 or not await el.is_visible():
            return
        await el.fill(desired)
        await asyncio.sleep(1.5)
        opts = page.locator("[data-automation-id='promptOption'], [role=option]")
        if await opts.count() > 0:
            # Pick first option containing the desired text
            for i in range(await opts.count()):
                opt = opts.nth(i)
                if desired.lower()[:5] in (await opt.inner_text()).lower():
                    await opt.click()
                    return
            await opts.first.click()
    except Exception:
        pass
