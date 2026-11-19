"""Greenhouse apply handler.

Greenhouse apply flow:
1. Navigate to job.apply_url (or job.url) — the form is embedded on the page
   or served at a separate /apply path.
2. Wait for the application form (#app or form with class 'application').
3. If there is an "Apply for this job" button, click it.
4. Fill: first_name, last_name, email, phone, resume upload, LinkedIn,
   cover letter (if field exists).
5. Handle EEO/demographic questions (use answer_bank; "prefer not to say" defaults).
6. Handle any dropdown custom questions.
7. Handle required checkboxes (consent/GDPR/terms).
8. Shadow mode gate → if shadow: screenshot + stop; if live: click Submit.
9. Detect confirmation page and return ApplyResult.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path

import structlog

from jobagent.apply.base import ApplyHandlerBase, ApplyResult
from jobagent.apply.browser import new_page
from jobagent.apply.field_mapper import build_field_values, fill_all_common_fields
from jobagent.apply.form_utils import (
    click_robust as _click_robust,
    confirm_via_email,
    dismiss_consent as _dismiss_consent,
    form_country_mismatch as _form_country_mismatch,
    handle_security_code as _handle_security_code,
    submitted_at_now,
    resume_attached as _resume_attached,
    upload_resume as _upload_resume,
    validation_errors as _validation_errors,
)
from jobagent.apply.guardrails import human_like_pause
from jobagent.apply.manual import await_manual_submit, focus_form
from jobagent.apply.question_engine import answer_custom_questions
from jobagent.apply.verifier import verify_form
from jobagent.db.engine import get_session
from jobagent.db.models import Application, Company, Job
from jobagent.profile.loader import load_profile

log = structlog.get_logger(__name__)


async def _fill_phone(scope, phone: str) -> bool:  # type: ignore[no-untyped-def]
    """Fill the phone field and make the intl-tel-input dial code match.

    Greenhouse's phone widget (intl-tel-input) keeps a country dial-code
    selector beside the input. Left on its default (often US), a ``+91`` number
    can be reformatted or rejected. Set the flag to +91 first, then type the
    digits so the widget's own handlers run and commit the value.
    """
    import re as _re  # noqa: PLC0415

    digits = _re.sub(r"[^\d+]", "", phone or "")
    if not digits:
        return False
    try:
        tel = scope.locator("input[type='tel'], input#phone")
        if await tel.count() == 0:
            by_label = scope.get_by_label("Phone", exact=False)
            if await by_label.count() > 0:
                tel = by_label
        if await tel.count() == 0:
            return False
        target = tel.first

        flag = scope.locator(".iti__selected-flag, .iti__flag-container").first
        if await flag.count() > 0:
            try:
                await _click_robust(flag)
                await asyncio.sleep(0.3)
                india = scope.locator(
                    ".iti__country[data-dial-code='91'], li[data-dial-code='91']"
                )
                if await india.count() > 0:
                    await _click_robust(india.first)
            except Exception:
                pass

        await target.scroll_into_view_if_needed(timeout=2000)
        await target.fill("")
        await target.type(digits, delay=40)
        return bool((await target.input_value() or "").strip())
    except Exception:
        return False


async def _looks_like_application_form(frame) -> bool:  # type: ignore[no-untyped-def]
    """True only for a real application form, not a job-alert signup widget.

    Career sites commonly render a "get notified about new jobs" box that also
    exposes ``first_name``/``last_name``/``email`` inputs, so name fields alone
    are not a safe signal — filling and submitting one of those would send a
    newsletter signup instead of an application. Every genuine Greenhouse
    application form offers a resume upload, so that is the discriminator.

    Deliberately narrow: text like "Upload" or "Resume" appears all over career
    pages (including in job descriptions), so matching on it yields false
    positives. A resume ``input[type=file]`` is the dependable marker --
    Greenhouse always renders one, even when it is visually hidden behind a
    styled button, and ``.count()`` sees hidden elements.
    """
    try:
        return await frame.locator(
            "input[type=file], form#application_form, #application_form"
        ).count() > 0
    except Exception:
        return False


async def _form_scope(frame):  # type: ignore[no-untyped-def]
    """Narrow the fill/submit target to the element that owns the resume upload.

    Career pages frequently host a job-alert signup widget whose
    ``first_name``/``email`` inputs sit *earlier* in the DOM than the real
    application form. Page-wide label lookups therefore fill — and would
    submit — the newsletter box instead. Scoping to the form that contains the
    resume ``input[type=file]`` keeps every interaction on the right form.

    Falls back to *frame* when no such container is identifiable, preserving
    the previous behaviour for simple single-form pages.

    The selectors are deliberately ordered stable-first. A predicate like
    ``form:has(input[type=file])`` is *not* stable: Greenhouse swaps the file
    input for a "resume.pdf ✕" chip once the upload completes, so a scope
    resolved that way silently stops matching mid-flow and every later query
    returns zero elements. ``#first_name`` survives those re-renders.
    """
    for selector in (
        "form#application_form",   # classic Greenhouse boards
        "form#application-form",   # job-boards.greenhouse.io
        "form:has(#first_name)",   # stable across re-renders
        "form:has(input[type=file])",
        "#application_form",
    ):
        try:
            loc = frame.locator(selector).first
            if await loc.count() > 0:
                log.debug("greenhouse.form_scoped", selector=selector)
                return loc
        except Exception:
            continue
    return frame


async def _form_scope(frame):  # type: ignore[no-untyped-def]
    """Narrow the fill/submit target to the element that owns the resume upload.

    Career pages frequently host a job-alert signup widget whose
    ``first_name``/``email`` inputs sit *earlier* in the DOM than the real
    application form. Page-wide label lookups therefore fill — and would
    submit — the newsletter box instead. Scoping to the form that contains the
    resume ``input[type=file]`` keeps every interaction on the right form.

    Falls back to *frame* when no such container is identifiable, preserving
    the previous behaviour for simple single-form pages.

    The selectors are deliberately ordered stable-first. A predicate like
    ``form:has(input[type=file])`` is *not* stable: Greenhouse swaps the file
    input for a "resume.pdf ✕" chip once the upload completes, so a scope
    resolved that way silently stops matching mid-flow and every later query
    returns zero elements. ``#first_name`` survives those re-renders.
    """
    for selector in (
        "form#application_form",   # classic Greenhouse boards
        "form#application-form",   # job-boards.greenhouse.io
        "form:has(#first_name)",   # stable across re-renders
        "form:has(input[type=file])",
        "#application_form",
    ):
        try:
            loc = frame.locator(selector).first
            if await loc.count() > 0:
                log.debug("greenhouse.form_scoped", selector=selector)
                return loc
        except Exception:
            continue
    return frame


def _company_name(job: Job) -> str:
    """Best-effort human-readable company name for operator-facing output."""
    if job.company_id:
        try:
            with get_session() as session:
                company = session.get(Company, job.company_id)
                if company and company.name:
                    return company.name
        except Exception:
            pass
    # Fall back to the posting host, e.g. jobs.elastic.co → elastic.co
    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        host = urlparse(job.apply_url or job.url).netloc
        return host.removeprefix("www.").removeprefix("jobs.") or "(unknown company)"
    except Exception:
        return "(unknown company)"

_EEO_PREFER_NOT: set[str] = {
    "I don't wish to answer",
    "I do not wish to answer",
    "Prefer not to say",
    "Prefer Not to Say",
    "Prefer Not To Say",
    "Decline to self identify",
    "Decline to Self Identify",
    "I do not wish to identify",
    "Choose not to disclose",
    "I prefer not to answer",
}

_EEO_VETERAN_DEFAULT = "I am not a veteran"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _is_confirmation_page(page) -> bool:  # type: ignore[type-arg]
    """Return True if the current page looks like a submission-confirmed page."""
    url = page.url.lower()
    if any(x in url for x in ["confirm", "thank", "success", "submitted", "complete"]):
        return True
    try:
        text = (await page.inner_text("body"))[:1000].lower()
        confirmed = any(x in text for x in [
            "thank you",
            "application received",
            "successfully applied",
            "we have received",
            "application submitted",
            "your application",
            "we'll be in touch",
            "we will be in touch",
            "review your application",
            "verify your email",        # Greenhouse email-verify post-submit
            "check your email",
            "confirmation email",
            "email has been sent",
            "create a job alert",       # Greenhouse post-submit state (form disappears)
            "job alert",                # Greenhouse post-submit
        ])
        if confirmed:
            return True

        # Also check if the form/apply section is gone (page reverted to job listing = submitted)
        # Greenhouse: after submit, the apply form disappears and "Apply" button returns
        form_gone = await page.locator("form button[type=submit]").count() == 0
        apply_btn_back = await page.locator("button:text('Apply'), a:text('Apply for this job')").count() > 0
        if form_gone and apply_btn_back:
            return True

        return False
    except Exception:
        return False


async def _handle_custom_questions(page, answers: dict, frame=None) -> None:  # type: ignore[no-untyped-def, type-arg]
    """Answer Greenhouse custom questions (radio + text) based on keyword matching.

    *page* is the scoped form.  *frame* (the full page/frame) is used for
    ``[role=option]`` lookups, because React-select menus are portalled to
    ``document.body`` — outside the form — and are invisible to a form-scoped
    locator.  Without it, combobox answers silently fail (type + Enter does not
    commit a React-select).

    Handles patterns like:
    - "Will you require sponsorship?" → Yes / No based on answers["require_sponsorship"]
    - "right to work / work without authorization" → based on answers["right_to_work"]
    - "countries where you have unrestricted right to work" → "India"
    - Sanctions / export controls → No
    - Privacy / consent acknowledgment → accept
    """
    # Keyword patterns → answer value
    # Order matters: "which countries do you have the right to work in?" is a
    # different question from "do you have the right to work without
    # sponsorship?", and the latter must not be answered with "Yes" by a
    # candidate who needs sponsorship.
    _PATTERNS: list[tuple[list[str], str]] = [
        (["countries"], "India"),
        (["right to work", "unrestricted right", "work without sponsorship", "legally authori"],
         answers.get("right_to_work", "No")),
        (["sponsorship", "sponsor", "work authorization", "work authorisation"],
         answers.get("require_sponsorship", "Yes")),
        (["belarus", "cuba", "iran", "north korea", "dprk", "syria", "russia"], "No"),
        (["acknowledge", "privacy", "consent", "agreement", "i agree"], "I acknowledge"),
    ]

    # Handle radio groups
    radio_groups: dict[str, list] = {}
    for radio in await page.locator("input[type=radio]").all():
        name = await radio.get_attribute("name") or ""
        if name:
            radio_groups.setdefault(name, []).append(radio)

    for group_name, radios in radio_groups.items():
        try:
            # Get the label for this radio group via the fieldset/legend or first radio's label
            group_label = ""
            for r in radios:
                rid = await r.get_attribute("id") or ""
                if rid:
                    lbl = page.locator(f"label[for='{rid}']")
                    if await lbl.count() > 0:
                        parent_text = await lbl.first.inner_text()
                        if parent_text:
                            group_label = parent_text.lower()
                            break

            if not group_label:
                continue

            # Find matching pattern
            desired_value: str | None = None
            for keywords, answer_val in _PATTERNS:
                if any(kw in group_label for kw in keywords):
                    desired_value = answer_val
                    break
            if desired_value is None:
                continue

            # Find the radio option that best matches the desired value
            for radio in radios:
                radio_value = (await radio.get_attribute("value") or "").lower()
                radio_label_id = await radio.get_attribute("id") or ""
                label_text = ""
                if radio_label_id:
                    lbl = page.locator(f"label[for='{radio_label_id}']")
                    if await lbl.count() > 0:
                        label_text = (await lbl.first.inner_text()).lower()

                if desired_value.lower() in radio_value or desired_value.lower() in label_text:
                    await radio.check()
                    log.debug("greenhouse.custom_q_answered",
                              group=group_name[:30], answer=desired_value)
                    break

        except Exception as exc:  # noqa: BLE001
            log.debug("greenhouse.custom_q_skip", group=group_name[:30], error=str(exc)[:50])

    # Handle combobox-style questions (React Select, role=combobox)
    # Patterns: label keyword → answer to select
    # ── Location autocomplete ────────────────────────────────────────────────
    # #candidate-location uses Google Places. We type, wait for suggestions,
    # then click the first one. Even if input_value() returns '' after clicking,
    # the React state IS updated and the form validates correctly.
    try:
        loc_field = page.locator("#candidate-location")
        if await loc_field.count() == 0:
            raise ValueError("no #candidate-location on this form")

        # Decide if the field still needs filling. We check for a committed
        # React-select value (the .select__single-value sibling div) rather
        # than relying on input visibility, because fill_all_common_fields uses
        # Playwright's .fill() which sets the raw value without triggering
        # keyboard events — Google Places never fires, the React-select stays in
        # "search query" mode, and the input appears visible but uncommitted.
        # Clicking it then fails because it's in a half-state.
        # A committed value shows as .select__single-value; an uncommitted
        # one shows .select__placeholder (or nothing).
        ctrl_ancestor = loc_field.locator(
            "xpath=ancestor::*[contains(@class,'select__container') "
            "or contains(@class,'select-shell')]"
        ).first
        already_committed = (
            await ctrl_ancestor.count() > 0
            and await ctrl_ancestor.locator(".select__single-value").count() > 0
        )
        if already_committed:
            log.debug("greenhouse.location_already_committed")
            raise StopIteration  # skip to finally

        # Click the .select__control wrapper (always full-width and clickable),
        # not the raw input (which may be zero-width or in a half-filled state).
        await loc_field.first.scroll_into_view_if_needed(timeout=3000)
        ctrl = loc_field.locator(
            "xpath=ancestor::*[contains(@class,'select__control')]"
        ).first
        click_target = ctrl if await ctrl.count() > 0 else loc_field.first
        await _click_robust(click_target)
        await asyncio.sleep(0.3)

        # Clear any residual search text from the .fill() pass, then type
        # slowly so the Google Places input handler fires properly.
        await loc_field.first.select_text()
        city_text = answers.get("city") or ""
        if not city_text:
            raise StopIteration  # no city known — leave the field alone
        await loc_field.first.type(city_text, delay=90)
        await asyncio.sleep(2.5)  # wait for Google Places API response

        # Find the Places suggestion list. It may be portalled outside the
        # form; search the full frame to be safe.
        # Exclude intl-tel-input options (they have data-dial-code attribute).
        opts = (frame or page).locator("[role=option]")
        chosen = False
        for _oi in range(await opts.count()):
            _o = opts.nth(_oi)
            if await _o.get_attribute("data-dial-code"):
                continue  # phone country code option — skip
            _txt = (await _o.inner_text()).strip()
            if city_text.lower().split(",")[0] in _txt.lower():
                await _click_robust(_o)
                await asyncio.sleep(0.5)
                log.debug("greenhouse.location_autocomplete_selected", option=_txt[:50])
                chosen = True
                break
        if not chosen:
            # Accept as-is — some tenants don't strictly require a Places match
            await loc_field.first.press("Tab")
            log.debug("greenhouse.location_no_places_match", city=city_text)
    except StopIteration:
        pass
    except Exception as exc:
        log.debug("greenhouse.location_fill_error", error=str(exc)[:80])
    finally:
        # Close any open suggestion menu so it doesn't overlay later fields.
        try:
            await page.locator("#candidate-location").first.press("Escape", timeout=2000)
        except Exception:
            pass

    # ── Plain text custom questions (no dropdown) ─────────────────────────────
    # "In what countries do you have the unrestricted right to work?" = free text
    _TEXT_QS: list[tuple[str, str]] = [
        ("unrestricted right to work", "India"),
        ("countries.*right to work",   "India"),
        ("right to work.*countries",   "India"),
    ]
    import re as _re2  # noqa: PLC0415
    text_inputs = page.locator("input[type=text]:not([role=combobox]):not(#first_name):not(#last_name):not(#email):not(#phone):not(#candidate-location)")
    for i in range(await text_inputs.count()):
        ti = text_inputs.nth(i)
        try:
            ti_id = await ti.get_attribute("id") or ""
            lbl_el = page.locator(f"label[for='{ti_id}']")
            if await lbl_el.count() == 0:
                continue
            lbl = (await lbl_el.first.inner_text()).lower()
            for pattern, answer in _TEXT_QS:
                if _re2.search(pattern, lbl):
                    current = await ti.input_value()
                    if not current:
                        await ti.fill(answer)
                        await asyncio.sleep(0.3)
                        log.debug("greenhouse.text_q_filled", label=lbl[:40], value=answer)
                    break
        except Exception:
            pass

    _COMBOBOX_ANSWERS: list[tuple[str, str]] = [
        # Country-specific question first, then the two sponsorship phrasings in
        # the correct precedence order.
        ("which countries|countries where|countries do you|countries in which", "India"),
        ("right to work|work without sponsorship|legally authori",
         answers.get("right_to_work", "No")),
        ("require.*sponsor|sponsorship", answers.get("require_sponsorship", "Yes")),
        ("relocat",              "Yes"),   # willing to relocate / live in location
        ("belarus.*cuba.*iran",  "No"),    # sanctions question
        ("citizenship.*cuba.*iran", "No"), # citizenship sanctions
        ("permanent residency.*cuba", "No"),
        ("legal permanent", "No"),
        ("acknowledge",          "I acknowledge"),  # privacy acknowledgment
    ]

    # For React-select comboboxes we need to click and then select the option.
    # NOTE: input_value() returns '' for React-select (it updates a hidden input),
    # but clicking an option DOES set the display value correctly and passes validation.
    import re as _re  # noqa: PLC0415

    comboboxes = page.locator("[role=combobox]")
    cb_count = await comboboxes.count()
    for i in range(cb_count):
        cb = comboboxes.nth(i)
        try:
            cb_id = await cb.get_attribute("id") or ""
            # Get associated label
            label_text = ""
            if cb_id:
                lbl_el = page.locator(f"label[for='{cb_id}']")
                if await lbl_el.count() > 0:
                    label_text = (await lbl_el.first.inner_text()).lower()
            if not label_text:
                continue

            # Skip EEO fields (handled separately) and location (handled above)
            if any(kw in label_text for kw in ["gender", "ethnicity", "race", "veteran", "disability", "hispanic"]):
                continue
            if "candidate-location" == cb_id or "location" in cb_id:
                continue

            desired: str | None = None
            for pattern, answer in _COMBOBOX_ANSWERS:
                if _re.search(pattern, label_text):
                    desired = answer
                    break
            if desired is None:
                continue

            # React-select hides its text input once a value is committed.
            # But the same element appears invisible when it has simply scrolled
            # off the viewport. We need to distinguish the two cases:
            # - off-screen (needs answering): scroll in, it becomes visible
            # - answered (React-select closes input): stays invisible after scroll
            try:
                await cb.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            if not await cb.is_visible():
                log.debug("greenhouse.combobox_already_set", label=label_text[:40])
                continue

            # Click to open dropdown. Bounded timeout: an unbounded click on a
            # combobox covered by a stray menu stalls the whole run for 30s.
            await _click_robust(cb)
            await asyncio.sleep(0.8)

            # Find the option that matches desired value
            opt_root = frame or page
            opt = opt_root.locator(f"[role=option]:text-matches('{desired}', 'i')")
            if await opt.count() > 0:
                await _click_robust(opt.first)
                await asyncio.sleep(0.3)
                log.debug("greenhouse.combobox_answered", label=label_text[:40], value=desired)
            else:
                # Type the value and accept
                await cb.fill(desired)
                await asyncio.sleep(0.5)
                opts_after = (frame or page).locator("[role=option]")
                if await opts_after.count() > 0:
                    await _click_robust(opts_after.first)
                else:
                    await cb.press("Enter")
                log.debug("greenhouse.combobox_typed", label=label_text[:40], value=desired)
        except Exception as exc:  # noqa: BLE001
            log.debug("greenhouse.combobox_skip", index=i, error=str(exc)[:50])
        finally:
            # Dismiss any menu this combobox left open so it cannot overlay —
            # and block clicks on — the questions that follow.
            try:
                await cb.press("Escape", timeout=2000)
            except Exception:
                pass


async def _handle_eeo_dropdowns(page, answers: dict, frame=None) -> None:  # type: ignore[no-untyped-def, type-arg]
    """Select the best EEO option for each demographic field.

    Covers both native ``<select>`` elements and React-select comboboxes
    (``role=combobox``). The latter are common on job-boards.greenhouse.io
    (e.g. Cloudflare) even though classic boards.greenhouse.io uses native
    selects for the same questions.

    Strategy:
    1. Use the value from *answers* if present (from answers_seed.yaml).
    2. Otherwise default to any "prefer not to say" label in the options.
    3. Veteran questions default to "I am not a protected veteran".
    """
    import re as _re  # noqa: PLC0415

    eeo_lookup: dict[str, str] = {
        "gender":     answers.get("gender", "Prefer not to say"),
        "race":       answers.get("ethnicity", "Prefer not to say"),
        "ethnicity":  answers.get("ethnicity", "Prefer not to say"),
        "veteran":    answers.get("veteran", _EEO_VETERAN_DEFAULT),
        "hispanic":   answers.get("hispanic", "No"),
        "disability": answers.get("disability", "Prefer not to say"),
    }

    # ── Native <select> elements ─────────────────────────────────────────────
    selects = page.locator("select")
    count = await selects.count()
    for i in range(count):
        sel = selects.nth(i)
        try:
            aria_label = (await sel.get_attribute("aria-label")) or ""
            sel_id = (await sel.get_attribute("id")) or ""
            label_text = ""
            if sel_id:
                label_el = page.locator(f"label[for='{sel_id}']")
                if await label_el.count() > 0:
                    label_text = await label_el.first.inner_text()
            label_text = (label_text or aria_label).lower()

            matched_desired: str | None = None
            for key, desired in eeo_lookup.items():
                if key in label_text:
                    matched_desired = desired
                    break
            if matched_desired is None:
                continue

            options: list[str] = await sel.locator("option").all_inner_texts()
            best: str | None = None
            for opt in options:
                if matched_desired.lower() in opt.lower() or opt.lower() in matched_desired.lower():
                    best = opt.strip()
                    break
            if best is None:
                for opt in options:
                    if opt.strip() in _EEO_PREFER_NOT:
                        best = opt.strip()
                        break
            if best:
                await sel.select_option(label=best)
                log.debug("greenhouse.eeo_field_filled", label=label_text[:40], value=best)

        except Exception as exc:  # noqa: BLE001
            log.debug("greenhouse.eeo_field_skip", index=i, error=str(exc)[:50])

    # ── React-select comboboxes (role=combobox) for EEO questions ───────────
    # On job-boards.greenhouse.io the same EEO section uses React-select, so
    # the native <select> pass above finds nothing. We need a separate pass
    # that opens the dropdown, reads the visible options, and clicks the best.
    combos = page.locator("[role=combobox]")
    for i in range(await combos.count()):
        cb = combos.nth(i)
        try:
            if not await cb.is_visible():
                continue  # already answered (React-select hides input after selection)

            cb_id = (await cb.get_attribute("id")) or ""
            lbl_text = ""
            if cb_id:
                lbl_el = page.locator(f"label[for='{cb_id}']")
                if await lbl_el.count() > 0:
                    lbl_text = (await lbl_el.first.inner_text()).lower()

            # Only handle EEO fields in this pass
            matched_desired_eeo: str | None = None
            for key, desired in eeo_lookup.items():
                if key in lbl_text:
                    matched_desired_eeo = desired
                    break
            if matched_desired_eeo is None:
                continue

            # React-select shrinks its text input to width=0 when no value is
            # selected and the control is not focused. The clickable target for
            # a React-select is the *.select__control* container that wraps it,
            # not the input itself. We click that wrapper instead.
            # First scroll into view in case the question is below the fold.
            try:
                await cb.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass

            # Prefer the React-select control wrapper; fall back to the input.
            ctrl = cb.locator("xpath=ancestor::*[contains(@class,'select__control')]").first
            if await ctrl.count() == 0:
                # Try sibling/parent structures used by older Greenhouse builds
                ctrl = cb.locator("xpath=..").first
            click_target = ctrl if (await ctrl.count() > 0 and await ctrl.is_visible()) else cb

            await _click_robust(click_target)
            await asyncio.sleep(0.5)

            opts = (frame or page).locator("[role=option]")
            opt_texts = []
            for k in range(await opts.count()):
                opt_texts.append((k, (await opts.nth(k).inner_text()).strip()))

            # 1. Try desired value
            chosen_idx: int | None = None
            for k, text in opt_texts:
                if (matched_desired_eeo.lower() in text.lower()
                        or text.lower() in matched_desired_eeo.lower()):
                    chosen_idx = k
                    break
            # 2. Fall back to prefer-not-to-say
            if chosen_idx is None:
                for k, text in opt_texts:
                    if text in _EEO_PREFER_NOT:
                        chosen_idx = k
                        break
            # 3. For veteran: match "not a protected veteran" / "not a veteran"
            if chosen_idx is None and "veteran" in lbl_text:
                for k, text in opt_texts:
                    if _re.search(r"not a.*veteran|i am not", text, _re.IGNORECASE):
                        chosen_idx = k
                        break

            if chosen_idx is not None:
                await _click_robust(opts.nth(chosen_idx))
                await asyncio.sleep(0.3)
                log.debug(
                    "greenhouse.eeo_combobox_filled",
                    label=lbl_text[:40],
                    value=opt_texts[chosen_idx][1][:30] if opt_texts else "?",
                )
            else:
                # Close without selecting so the menu doesn't block later fields
                await cb.press("Escape", timeout=2000)

        except Exception as exc:  # noqa: BLE001
            log.debug("greenhouse.eeo_combobox_skip", index=i, error=str(exc)[:50])
        finally:
            try:
                await cb.press("Escape", timeout=2000)
            except Exception:
                pass


#: Question-label pattern → answer, for questions rendered as native <select>.
#: Ordered: the first match wins, so put the more specific patterns first.
_SELECT_ANSWERS: list[tuple[str, str]] = [
    ("relocat",                            "Yes"),
    ("how did you hear",                   "Job board"),
    ("acknowledge|privacy policy|consent", "Yes"),
]


async def _label_for(scope, el) -> str:  # type: ignore[no-untyped-def]
    """Best-effort visible question text for a form control, lower-cased."""
    try:
        el_id = await el.get_attribute("id") or ""
        if el_id:
            # CSS.escape equivalent is overkill here; Greenhouse ids are simple.
            lbl = scope.locator(f"label[for='{el_id}']")
            if await lbl.count() > 0:
                return (await lbl.first.inner_text()).strip().lower()
        for attr in ("aria-label", "name", "placeholder"):
            val = await el.get_attribute(attr)
            if val:
                return val.strip().lower()
    except Exception:
        pass
    return ""


async def _handle_native_selects(scope, answers: dict | None = None) -> None:  # type: ignore[no-untyped-def]
    """Answer questions rendered as a plain ``<select>``.

    Greenhouse tenants render custom questions either as React comboboxes
    (handled in :func:`_handle_custom_questions`) or as native ``<select>``
    elements. The latter have no ``role=combobox``, so they were previously
    left on "Select..." and silently failed required-field validation.

    Only touches a select that still sits on its placeholder, so demographic
    answers set by :func:`_handle_eeo_dropdowns` are never overwritten.
    """
    import re as _re  # noqa: PLC0415

    selects = scope.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        try:
            label = await _label_for(scope, sel)
            if not label:
                continue

            answers = answers or {}
            desired: str | None = None
            # Work authorisation is country-aware, so take it from the answers
            # map (built per job) rather than a fixed default. Check the
            # "without sponsorship" phrasing FIRST: "right to work without
            # sponsorship?" must not be answered as if it were "require
            # sponsorship?".
            if _re.search(r"right to work|work without sponsorship|legally authori", label):
                desired = answers.get("right_to_work", "No")
            elif _re.search(
                r"require.*sponsorship|immigration sponsorship|sponsorship", label
            ):
                desired = answers.get("require_sponsorship", "Yes")
            else:
                for pattern, answer in _SELECT_ANSWERS:
                    if _re.search(pattern, label):
                        desired = answer
                        break
            if desired is None:
                continue

            # Leave already-answered selects alone (placeholder values only).
            current = (await sel.input_value() or "").strip().lower()
            if current and current not in ("", "0", "select...", "select"):
                continue

            # Pick the option whose text best matches the desired answer.
            options = sel.locator("option")
            chosen: str | None = None
            for k in range(await options.count()):
                text = (await options.nth(k).inner_text()).strip()
                if not text or text.lower().startswith("select"):
                    continue
                if text.lower() == desired.lower():
                    chosen = text
                    break
                if chosen is None and desired.lower() in text.lower():
                    chosen = text
            if chosen is None:
                continue

            await sel.select_option(label=chosen)
            log.debug("greenhouse.select_answered", label=label[:45], value=chosen)
        except Exception as exc:  # noqa: BLE001
            log.debug("greenhouse.select_skip", index=i, error=str(exc)[:60])


async def _handle_checkboxes(page) -> None:  # type: ignore[type-arg]
    """Check consent / GDPR / acknowledgment checkboxes.

    Covers two cases:

    1. Inputs explicitly marked ``required`` / ``aria-required``.
    2. Inputs whose own label or surrounding question text reads like a
       consent or acknowledgment. Many tenants (Cloudflare among them) put the
       required marker on the question label rather than on the input, so
       attribute-only detection left the box unchecked and the form invalid.
    """
    import re as _re  # noqa: PLC0415

    consent_re = _re.compile(
        "acknowledge|consent|privacy|agree|terms|confirm|certify|gdpr",
        _re.IGNORECASE,
    )

    checkboxes = page.locator("input[type='checkbox']")
    count = await checkboxes.count()
    for i in range(count):
        cb = checkboxes.nth(i)
        try:
            if await cb.is_checked():
                continue

            required = bool(
                await cb.get_attribute("required")
                or await cb.get_attribute("aria-required") == "true"
            )
            if not required:
                label = await _label_for(page, cb)
                if not consent_re.search(label):
                    continue

            await cb.check(timeout=5000)
            log.debug("greenhouse.checkbox_checked", index=i)
        except Exception as exc:  # noqa: BLE001
            log.debug("greenhouse.checkbox_skip", index=i, error=str(exc)[:60])


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class GreenhouseHandler(ApplyHandlerBase):
    """Apply handler for Greenhouse-hosted job postings."""

    name = "greenhouse"
    ats_type = "greenhouse"

    async def apply(  # noqa: PLR0912, PLR0915
        self,
        job: Job,
        app: Application,
        package_dir: Path,
        shadow_mode: bool,
        manual_mode: bool = False,
    ) -> ApplyResult:
        """Execute the full Greenhouse application flow.

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
            async with new_page("greenhouse") as page:

                # ── Step 1: Navigate ─────────────────────────────────────────
                target_url: str = job.apply_url or job.url
                log.info("greenhouse.navigate", url=target_url, job_id=job.id)
                await page.goto(target_url, wait_until="domcontentloaded")
                await human_like_pause()
                # Clear any consent backdrop first — it intercepts pointer
                # events and would make every later click time out.
                await _dismiss_consent(page)
                await page.screenshot(path=str(shot_dir / "step_01_loaded.png"))

                # ── Step 1b: Click Apply if page hosts but doesn't show form ─
                # Some companies show the job listing and require clicking
                # an "Apply Now" link to load the Greenhouse iframe.
                gh_iframe_sel = (
                    "iframe[src*='greenhouse.io/embed'], "
                    "iframe[src*='greenhouse.io/job_app']"
                )
                await asyncio.sleep(2)
                form_frame = page  # default

                # If no Greenhouse iframe yet, check for /form links and click them
                if await page.locator(gh_iframe_sel).count() == 0:
                    apply_links = page.locator(
                        "a[href*='/form'], a[href*='gh_jid'], "
                        "a:text-matches('Apply Now', 'i'), "
                        "a:text-matches('Apply for this job', 'i')"
                    )
                    if await apply_links.count() > 0:
                        for i in range(await apply_links.count()):
                            href = await apply_links.nth(i).get_attribute("href") or ""
                            if "form" in href or "gh_jid" in href or "apply" in href.lower():
                                log.debug("greenhouse.clicking_apply_link", href=href[:60])
                                pages_before = len(page.context.pages)
                                try:
                                    await apply_links.nth(i).click(timeout=10_000)
                                except Exception:
                                    # Something is still covering the link —
                                    # clear it and force the click through.
                                    await _dismiss_consent(page)
                                    await apply_links.nth(i).click(
                                        timeout=10_000, force=True
                                    )
                                await asyncio.sleep(4)
                                # Some career sites open the ATS form in a new
                                # tab (target=_blank). Follow it, otherwise we
                                # keep inspecting the original page and never
                                # find the form.
                                if len(page.context.pages) > pages_before:
                                    page = page.context.pages[-1]
                                    form_frame = page
                                    try:
                                        await page.wait_for_load_state(
                                            "domcontentloaded", timeout=15_000
                                        )
                                    except Exception:
                                        pass
                                    await _dismiss_consent(page)
                                    log.debug("greenhouse.followed_new_tab", url=page.url[:80])
                                break

                # Now check if the Greenhouse iframe appeared
                if await page.locator(gh_iframe_sel).count() > 0:
                    log.debug("greenhouse.iframe_detected")
                    form_frame = page.frame_locator(gh_iframe_sel).first
                    await asyncio.sleep(2)  # let iframe content load

                # ── Step 2: Click "Apply" if form not yet rendered ────────────
                # Must be the real application form — a job-alert widget with
                # name/email inputs does not count (see _looks_like_application_form).
                form_already_present = await _looks_like_application_form(form_frame)

                if not form_already_present and form_frame is page:
                    apply_btn = (
                        page.get_by_role("link", name="Apply for this job", exact=True).or_(
                            page.get_by_role("button", name="Apply for this job", exact=True)
                        ).or_(
                            page.locator("a.btn-apply, a.application-button")
                        )
                    )
                    if await apply_btn.count() > 0:
                        log.debug("greenhouse.apply_button_clicked")
                        await apply_btn.first.click()
                        await page.wait_for_load_state("domcontentloaded")
                        await human_like_pause()
                else:
                    log.debug("greenhouse.form_already_present")

                # ── Step 3: Wait for form ────────────────────────────────────
                try:
                    await page.wait_for_selector(
                        "form, #app, .application, .application-form",
                        timeout=15_000,
                    )
                except Exception:
                    # Form may already be rendered; proceed
                    pass
                await page.screenshot(path=str(shot_dir / "step_02_form.png"))

                # ── Step 3b: Guard — is this actually an application form? ───
                # Refuse to fill anything that lacks a resume upload. Without
                # this we would happily fill (and in live mode submit) a
                # job-alert signup widget that merely looks like a form.
                if not await _looks_like_application_form(form_frame):
                    log.warning("greenhouse.no_application_form", job_id=job.id, url=page.url)
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "No application form found (no resume upload on the page). "
                            "The posting may route to an external ATS, require login, "
                            "or be closed."
                        ),
                        screenshot_before=shot_dir / "step_02_form.png",
                    )

                # ── Step 4: CAPTCHA / redirect check ────────────────────────
                # Also catch cases where we got redirected to a sign-in page
                current_url = page.url
                if "sign_in" in current_url or "login" in current_url or "auth" in current_url:
                    return ApplyResult(
                        success=False, application_id=app.id,
                        status="blocked",
                        error=f"Redirected to login page: {current_url[:80]}",
                    )
                # Only block on VISIBLE CAPTCHAs (size != invisible).
                # Invisible reCAPTCHA Enterprise runs at submit time — the form
                # is still fillable and stealth may pass the check.
                visible_captcha = False
                for captcha_el in await page.locator(
                    ".g-recaptcha, [data-sitekey], iframe[src*='recaptcha']"
                ).all():
                    size = await captcha_el.get_attribute("data-size") or ""
                    src = await captcha_el.get_attribute("src") or ""
                    is_invisible = "invisible" in size.lower() or "invisible" in src.lower()
                    if not is_invisible:
                        visible_captcha = True
                        break

                if visible_captcha:
                    await page.screenshot(path=str(shot_dir / "step_captcha_blocked.png"))
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error="CAPTCHA detected before form fill",
                        screenshot_before=shot_dir / "step_02_form.png",
                    )

                # ── Step 4b: Answer yes/no questions BEFORE location fill ───
                # Some questions (relocation, work authorisation) become hidden
                # by React once the city/location field is committed — the form
                # hides them or auto-answers them based on the entered location.
                # We must answer them first, while the form is still in its
                # fresh state. Use a minimal early pass: only visible comboboxes
                # that match a known yes/no pattern. The full combobox pass in
                # _handle_custom_questions will catch anything that is still
                # visible after the main fill.
                fill_scope_pre = await _form_scope(form_frame)
                # Early-pass patterns: question label keyword → answer keyword
                # (The answer keyword is matched against the visible option text,
                # not expected to be an exact string.)
                _EARLY_PATTERNS: list[tuple[str, str]] = [
                    ("relocat",                                       "willing to relocate"),
                    ("require.*sponsorship|immigration sponsorship",  "Yes"),
                    ("legally authori[sz]ed|right to work",           "Yes"),
                ]
                import re as _re_early  # noqa: PLC0415
                early_combos = fill_scope_pre.locator("[role=combobox]")
                for _ei in range(await early_combos.count()):
                    _ecb = early_combos.nth(_ei)
                    try:
                        _ecb_id = (await _ecb.get_attribute("id")) or ""
                        _lbl_el = fill_scope_pre.locator(f"label[for='{_ecb_id}']")
                        if await _lbl_el.count() == 0:
                            continue
                        _lbl = (await _lbl_el.first.inner_text()).lower()
                        _desired: str | None = None
                        for _pat, _ans in _EARLY_PATTERNS:
                            if _re_early.search(_pat, _lbl):
                                _desired = _ans
                                break
                        if not _desired or not await _ecb.is_visible():
                            continue

                        # Click the React-select *control* div, not the raw input.
                        # The input has zero/minimal width and clicking it can
                        # accidentally trigger an adjacent widget (e.g. the
                        # intl-tel-input phone dropdown). The .select__control
                        # wrapper is always a full-width visible click target.
                        _ctrl = fill_scope_pre.locator(
                            f"[id='{_ecb_id}']"
                            " >> xpath=ancestor::*[contains(@class,'select__control')]"
                        ).first
                        _click_tgt = _ctrl if (await _ctrl.count() > 0 and await _ctrl.is_visible()) else _ecb
                        await _click_robust(_click_tgt)
                        await asyncio.sleep(0.6)

                        # Options render inside .select__menu, which may be a
                        # portal at document root — search the full frame.
                        _menu = form_frame.locator(".select__menu").last
                        _opts = _menu.locator("[role=option]") if await _menu.count() > 0 else form_frame.locator("[role=option]")
                        _chosen = False
                        for _oi in range(await _opts.count()):
                            _otxt = (await _opts.nth(_oi).inner_text()).lower()
                            if _desired.lower() in _otxt:
                                await _click_robust(_opts.nth(_oi))
                                log.debug("greenhouse.early_q_answered", label=_lbl[:40], value=_otxt[:30])
                                _chosen = True
                                break
                        if not _chosen:
                            log.debug("greenhouse.early_q_no_option", label=_lbl[:40], desired=_desired)
                        await asyncio.sleep(0.3)
                        try:
                            await _ecb.press("Escape", timeout=2000)
                        except Exception:
                            pass
                    except Exception as _exc:
                        log.debug("greenhouse.early_q_skip", error=str(_exc)[:50])

                # ── Step 5: Fill common fields ───────────────────────────────
                field_values = build_field_values(profile, job, app.answers_json or "{}")
                resume_path = Path(app.resume_path) if app.resume_path else None
                # form_frame may be a FrameLocator for iframe-embedded forms;
                # scope narrows further to the real application form so we
                # never fill a job-alert signup widget on the same page.
                fill_scope = await _form_scope(form_frame)
                fill_results = await fill_all_common_fields(
                    fill_scope, field_values, resume_path
                )
                await human_like_pause()

                # Phone needs its own pass: the intl-tel-input widget must have
                # its dial code set before the digits are typed, otherwise the
                # number can be reformatted or rejected.
                phone_value = field_values.get("phone", "")
                if phone_value:
                    phone_ok = await _fill_phone(fill_scope, phone_value)
                    fill_results["phone"] = fill_results.get("phone", False) or phone_ok

                for _key in ("first_name", "last_name", "email"):
                    if not fill_results.get(_key):
                        log.warning(
                            "greenhouse.critical_field_unfilled",
                            job_id=job.id,
                            field=_key,
                        )

                # Resume is mandatory on Greenhouse forms; upload through the
                # tenant's widget and verify it actually attached.
                if resume_path is not None:
                    # fill_all_common_fields already attempted the upload; only
                    # re-upload if nothing is attached, otherwise a second
                    # set_input_files on some tenants CLEARS the first one.
                    already = await _resume_attached(fill_scope, Path(resume_path).name)
                    if not already and not await _upload_resume(
                        page, fill_scope, resume_path
                    ):
                        await page.screenshot(
                            path=str(shot_dir / "step_resume_failed.png")
                        )
                        return ApplyResult(
                            success=False,
                            application_id=app.id,
                            status="failed",
                            error=(
                                "Resume upload failed: the file input did not retain "
                                "the file (field still marked required)."
                            ),
                            screenshot_before=shot_dir / "step_resume_failed.png",
                        )

                # ── Step 6: Upload cover letter ──────────────────────────────
                cl_path = package_dir / "cover_letter.txt"
                if cl_path.exists():
                    cl_textarea = fill_scope.locator(
                        "textarea[name*='cover'], textarea[id*='cover'], "
                        "textarea[placeholder*='cover' i], "
                        "textarea[aria-label*='cover' i]"
                    )
                    if await cl_textarea.count() > 0:
                        await cl_textarea.first.fill(cl_path.read_text(encoding="utf-8"))
                        log.debug("greenhouse.cover_letter_filled_textarea")
                    else:
                        cl_upload = fill_scope.locator(
                            "input[type='file'][name*='cover'], "
                            "input[type='file'][id*='cover'], "
                            "input[type='file'][aria-label*='cover' i]"
                        )
                        if await cl_upload.count() > 0:
                            await cl_upload.first.set_input_files(str(cl_path))
                            log.debug("greenhouse.cover_letter_uploaded_file")

                # Location autocomplete needs a city. Older tailored packages
                # predate the "city" answer, so fall back to the profile value.
                if not answers.get("city"):
                    answers["city"] = field_values.get("city", "")

                # ── Step 7: EEO dropdowns ────────────────────────────────────
                # Pass form_frame so portalled (document.body) React-select
                # menus are still reachable.
                await _handle_eeo_dropdowns(fill_scope, answers, frame=form_frame)

                # ── Step 7b: Custom yes/no questions (sponsorship, compliance)
                await _handle_custom_questions(fill_scope, answers, frame=form_frame)
                await human_like_pause()

                # ── Step 8: Dismiss cookie/consent modal (main page) ─────────
                await _dismiss_consent(page)

                # ── Step 8b: Required checkboxes ─────────────────────────────
                # Native <select> questions (relocation, sponsorship, …) —
                # after the EEO pass so demographic answers are preserved.
                await _handle_native_selects(fill_scope, answers)

                await _handle_checkboxes(fill_scope)

                # ── Step 8c: LLM answer engine for remaining custom questions ─
                # Greenhouse tenants attach arbitrary required questions that a
                # keyword table cannot cover. Answer them from the answer bank;
                # if any cannot be answered truthfully, abort BEFORE submit.
                unresolved = await answer_custom_questions(
                    fill_scope, form_frame, job, profile
                )
                if unresolved:
                    await page.screenshot(
                        path=str(shot_dir / "step_03_unanswerable.png")
                    )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "Unanswerable required question(s): "
                            + "; ".join(unresolved[:6])
                        ),
                        screenshot_before=shot_dir / "step_03_unanswerable.png",
                    )

                # ── Step 8d: country/role guard ──────────────────────────────
                country_mismatch = await _form_country_mismatch(fill_scope, job)
                if country_mismatch:
                    await page.screenshot(
                        path=str(shot_dir / "step_country_mismatch.png")
                    )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="failed",
                        error=(
                            "Form asks for work authorization in "
                            f"{country_mismatch}, but the job country is "
                            f"{job.country!r} — role appears to be elsewhere."
                        ),
                        screenshot_before=shot_dir / "step_country_mismatch.png",
                    )

                # ── Step 9: Pre-submit screenshot ────────────────────────────
                await page.screenshot(path=str(shot_dir / "step_03_filled.png"))

                # ── Step 9b: Read-back verification (PLAN §10A.6) ────────────
                # Check the DOM before any submit: name/email present and every
                # visible required field non-empty. On failure, repair once,
                # then abort rather than submitting a half-filled form.
                verification = await verify_form(
                    page,
                    app,
                    profile,
                    shot_dir / "step_03_verify.png",
                    scope=fill_scope,
                )
                if not verification.passed:
                    log.warning(
                        "greenhouse.verify_failed",
                        job_id=job.id,
                        issues=verification.issues[:6],
                    )
                    try:
                        await fill_all_common_fields(fill_scope, field_values, resume_path)
                        if phone_value:
                            await _fill_phone(fill_scope, phone_value)
                        await _handle_custom_questions(fill_scope, answers, frame=form_frame)
                        await _handle_native_selects(fill_scope, answers)
                        await _handle_checkboxes(fill_scope)
                    except Exception:
                        log.debug("greenhouse.verify_repair_error", job_id=job.id)

                    verification = await verify_form(
                        page,
                        app,
                        profile,
                        shot_dir / "step_03_verify_retry.png",
                        scope=fill_scope,
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
                    log.info("greenhouse.verify_recovered", job_id=job.id)

                # ── Step 10: Shadow mode gate ────────────────────────────────
                if shadow_mode:
                    log.info("greenhouse.shadow_stopped", job_id=job.id)
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="shadow_stopped",
                        screenshot_before=shot_dir / "step_03_filled.png",
                    )

                # ── Step 10b: Manual hand-off ────────────────────────────────
                # Form is filled; let the operator do the final click. This
                # avoids reCAPTCHA Enterprise entirely and gives us a reliable
                # human confirmation instead of DOM keyword guessing.
                if manual_mode:
                    await focus_form(page, fill_scope)
                    decision = await await_manual_submit(
                        company=_company_name(job),
                        title=job.title or "(untitled role)",
                        url=page.url,
                    )
                    await page.screenshot(path=str(shot_dir / "step_04_manual.png"))
                    log.info("greenhouse.manual_decision", job_id=job.id, decision=decision)

                    if decision == "submitted":
                        return ApplyResult(
                            success=True,
                            application_id=app.id,
                            status="submitted",
                            confirmation_url=page.url,
                            confirmation_text="Submitted manually by operator",
                            screenshot_before=shot_dir / "step_03_filled.png",
                            screenshot_after=shot_dir / "step_04_manual.png",
                        )
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="skipped",
                        error=f"Manual hand-off: operator chose {decision!r}",
                        screenshot_before=shot_dir / "step_03_filled.png",
                        screenshot_after=shot_dir / "step_04_manual.png",
                        extra={"quit": decision == "quit"},
                    )

                # ── Step 11: Find and click Submit ───────────────────────────
                submit_btn = (
                    fill_scope.get_by_role("button", name="Submit Application").or_(
                        fill_scope.get_by_role("button", name="Submit").or_(
                            fill_scope.locator("button[type=submit], input[type=submit]")
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
                        screenshot_before=shot_dir / "step_03_filled.png",
                    )

                # Dismiss any consent/cookie modal that may have reappeared
                await _dismiss_consent(page)

                log.info("greenhouse.submit_clicked", job_id=job.id)
                # Capture a lower bound for the security-code email so we only
                # read the code generated by THIS submission.
                submitted_at = submitted_at_now()
                await submit_btn.first.click(force=True)  # force=True bypasses intercepting overlays
                # Wait for the page to settle after submit (some Greenhouse tenants
                # stay at the same URL but remove the form; allow extra time)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                except Exception:
                    pass
                await asyncio.sleep(5)  # extra wait for client-side state changes
                await human_like_pause()

                # ── Step 12: Post-submit screenshot ─────────────────────────
                await page.screenshot(path=str(shot_dir / "step_04_after_submit.png"))

                # ── Step 12b: Email security-code step ──────────────────────
                # Some tenants finalise only after an 8-character code emailed
                # to the applicant. Read it from Gmail and enter it.
                if await _handle_security_code(page, fill_scope, submitted_at):
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=20_000
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(4)
                    await page.screenshot(
                        path=str(shot_dir / "step_04b_after_otp.png")
                    )

                # ── Step 13: CAPTCHA check (post-submit) ─────────────────────
                # Only block on visible CAPTCHAs after submit
                post_visible_captcha = False
                for el in await page.locator(
                    ".g-recaptcha, [data-sitekey], iframe[src*='recaptcha']"
                ).all():
                    size = await el.get_attribute("data-size") or ""
                    src = await el.get_attribute("src") or ""
                    if "invisible" not in size.lower() and "invisible" not in src.lower():
                        post_visible_captcha = True
                        break
                if post_visible_captcha:
                    return ApplyResult(
                        success=False,
                        application_id=app.id,
                        status="blocked",
                        error="Visible CAPTCHA appeared after submit",
                        screenshot_before=shot_dir / "step_03_filled.png",
                        screenshot_after=shot_dir / "step_04_after_submit.png",
                    )

                # ── Step 14: Confirmation ────────────────────────────────────
                # For iframe-based forms, treat submit success differently:
                # Greenhouse's iframe resets to show the blank form after submission.
                # Detection strategy:
                #   1. Standard page-level confirmation keywords (works for direct GH pages)
                #   2. For iframe forms: if the first_name field is now EMPTY after submission
                #      AND we successfully filled it before → submission went through
                confirmed = await _is_confirmation_page(page)
                if not confirmed and form_frame is not page:
                    try:
                        # Check if the form reset (fields are empty after we filled them)
                        fn_val = await fill_scope.locator("#first_name").input_value(timeout=3000)
                        email_val = await fill_scope.locator("#email").input_value(timeout=3000)
                        # If fields are empty after our fill + submit → form reset = submission success
                        if fn_val == "" and email_val == "":
                            confirmed = True
                            log.info("greenhouse.iframe_form_reset_confirmed", job_id=job.id)
                    except Exception:
                        pass
                # Cross-check via email: a real submission produces a
                # confirmation mail even when the page's confirmation UI is
                # not recognised.
                if not confirmed:
                    confirmed = await confirm_via_email(_company_name(job), submitted_at)

                if confirmed:
                    log.info("greenhouse.submitted", job_id=job.id, url=page.url)
                    return ApplyResult(
                        success=True,
                        application_id=app.id,
                        status="submitted",
                        confirmation_url=page.url,
                        confirmation_text="Application submitted successfully via Greenhouse",
                        screenshot_before=shot_dir / "step_03_filled.png",
                        screenshot_after=shot_dir / "step_04_after_submit.png",
                    )

                # Submission click landed on an unexpected page
                validation = await _validation_errors(fill_scope)
                if validation:
                    log.warning(
                        "greenhouse.submit_blocked", job_id=job.id, errors=validation
                    )
                return ApplyResult(
                    success=False,
                    application_id=app.id,
                    status="failed",
                    error=(
                        "Confirmation page not detected after submit"
                        + (f" | validation: {'; '.join(validation)}" if validation else "")
                    ),
                    screenshot_before=shot_dir / "step_03_filled.png",
                    screenshot_after=shot_dir / "step_04_after_submit.png",
                )

        except Exception as exc:  # noqa: BLE001
            log.exception("greenhouse.apply_error", job_id=job.id, error=str(exc))
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
