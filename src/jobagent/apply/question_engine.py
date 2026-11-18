"""LLM-backed answer engine for ATS custom questions.

Greenhouse tenants attach arbitrary required questions to an application
(``question_<id>`` text inputs, native ``<select>``s and React-select
comboboxes). A keyword table cannot cover these, so this module:

1. Enumerates the *visible, empty, required* custom controls on the form.
2. Asks the LLM for an answer to each, grounded strictly in the candidate's
   answer bank (``answers_seed.yaml``) and profile.
3. Fills text fields, native selects and React-select comboboxes.

Truthfulness contract: the model must mark a question ``unanswerable`` when the
supplied data does not support an answer. The caller then aborts *before*
submit, so we never fabricate personal facts (GPA, salary, authorisation).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field

from jobagent.llm.structured import structured_complete
from jobagent.profile.schema import MasterProfile
from jobagent.tailor.answers import load_answers_seed

log = structlog.get_logger(__name__)

# Fields handled by the core field mapper / location autocomplete, never by us.
_CORE_IDS = frozenset(
    {"first_name", "last_name", "email", "phone", "candidate-location", "resume"}
)

_SKIP_INPUT_TYPES = frozenset(
    {"file", "checkbox", "radio", "hidden", "submit", "button", "reset"}
)


class FieldAnswer(BaseModel):
    field_id: str
    answer: str | None = None
    unanswerable: bool = False
    reason: str = ""


class FormAnswers(BaseModel):
    answers: list[FieldAnswer] = Field(default_factory=list)


class _Field(BaseModel):
    field_id: str
    question: str
    kind: str  # "text" | "select" | "combobox" | "yesno"
    options: list[str] = Field(default_factory=list)


async def _click_robust(loc: Any, timeout: int = 8000) -> None:  # noqa: ANN401
    try:
        await loc.click(timeout=timeout)
    except Exception:
        await loc.click(timeout=timeout, force=True)


def _years_experience(seed: dict) -> int:
    start = str(seed.get("experience_start", "2017-07"))
    try:
        started = datetime.strptime(start, "%Y-%m")
    except ValueError:
        return 0
    now = datetime.now(UTC)
    return max(0, (now.year - started.year) - (1 if now.month < started.month else 0))


async def _label_for(scope, field_id: str) -> str:  # type: ignore[no-untyped-def]
    """Best-effort visible question text for the control with *field_id*."""
    lbl = scope.locator(f"label[for='{field_id}']")
    if await lbl.count() > 0:
        text = (await lbl.first.inner_text()).strip()
        if text:
            return re.sub(r"\s+", " ", text)
    inp = scope.locator(f"[id='{field_id}']").first
    try:
        aria = await inp.get_attribute("aria-label")
        if aria:
            return aria.strip()
    except Exception:
        pass
    try:
        text = await inp.evaluate(
            "e => (e.closest('div')?.innerText || '')"
        )
        return re.sub(r"\s+", " ", (text or "").strip())[:300]
    except Exception:
        return ""


async def _combobox_options(scope, frame, field_id: str) -> list[str]:  # type: ignore[no-untyped-def]
    """Open a React-select, read its option texts, then close it."""
    inp = scope.locator(f"[id='{field_id}']").first
    ctrl = inp.locator("xpath=ancestor::*[contains(@class,'select__control')]").first
    target = ctrl if (await ctrl.count() > 0 and await ctrl.is_visible()) else inp
    try:
        await _click_robust(target)
        await asyncio.sleep(0.6)
        opts = (frame or scope).locator("[role=option]")
        out: list[str] = []
        for i in range(await opts.count()):
            opt = opts.nth(i)
            if await opt.get_attribute("data-dial-code"):
                continue
            text = (await opt.inner_text()).strip()
            if text:
                out.append(text)
        return out
    except Exception:
        return []
    finally:
        try:
            await inp.press("Escape", timeout=1500)
        except Exception:
            pass


async def _check_current_employment(scope) -> None:  # type: ignore[no-untyped-def]
    """Tick any "I currently work here" boxes so end-date fields are optional.

    Employment-history widgets (Coinbase, Workday-style) require an end date
    unless the "currently work here" checkbox is set. Ticking it first removes
    the impossible "Present" end-date questions.
    """
    cbs = scope.locator("input[type='checkbox']")
    for i in range(await cbs.count()):
        cb = cbs.nth(i)
        try:
            if await cb.is_checked() or not await cb.is_visible():
                continue
            cb_id = await cb.get_attribute("id") or ""
            label = await _label_for(scope, cb_id) if cb_id else ""
            if not label:
                label = await cb.evaluate(
                    "e => (e.closest('label')?.innerText || e.closest('div')?.innerText || '')"
                )
            if re.search(
                r"currently work|current position|i currently|current role|present",
                label or "",
                re.IGNORECASE,
            ):
                await cb.check(timeout=3000)
                log.debug(
                    "question_engine.current_employment_checked",
                    label=(label or "")[:50],
                )
        except Exception:
            continue


async def _collect_fields(scope, frame) -> list[_Field]:  # type: ignore[no-untyped-def]
    """Return the visible, empty, required custom controls on the form."""
    fields: list[_Field] = []

    # ── text / textarea ──────────────────────────────────────────────────────
    text_loc = scope.locator(
        "input[aria-required='true'], textarea[aria-required='true'], "
        "input[required], textarea[required]"
    )
    for i in range(await text_loc.count()):
        el = text_loc.nth(i)
        try:
            field_id = await el.get_attribute("id") or ""
            if not field_id or field_id in _CORE_IDS:
                continue
            role = (await el.get_attribute("role") or "").lower()
            if role == "combobox":
                continue  # handled below
            field_type = (await el.get_attribute("type") or "").lower()
            if field_type in _SKIP_INPUT_TYPES:
                continue
            aria = (await el.get_attribute("aria-required") or "").lower()
            if not field_id and aria != "true":
                continue
            if not await el.is_visible() or await el.is_disabled():
                continue
            if (await el.input_value() or "").strip():
                continue
            question = await _label_for(scope, field_id)
            if not question:
                continue
            fields.append(
                _Field(field_id=field_id, question=question, kind="text")
            )
        except Exception:
            continue

    # ── native <select> ──────────────────────────────────────────────────────
    sel_loc = scope.locator("select[required], select[aria-required='true']")
    for i in range(await sel_loc.count()):
        el = sel_loc.nth(i)
        try:
            field_id = await el.get_attribute("id") or ""
            if not field_id or field_id in _CORE_IDS:
                continue
            if not await el.is_visible() or await el.is_disabled():
                continue
            current = (await el.input_value() or "").strip().lower()
            if current and current not in ("", "0", "select...", "select"):
                continue
            options = [
                t.strip()
                for t in await el.locator("option").all_inner_texts()
                if t.strip() and not t.strip().lower().startswith("select")
            ]
            question = await _label_for(scope, field_id)
            if not question:
                continue
            fields.append(
                _Field(
                    field_id=field_id,
                    question=question,
                    kind="select",
                    options=options,
                )
            )
        except Exception:
            continue

    # ── React-select comboboxes ──────────────────────────────────────────────
    combo_loc = scope.locator("[role=combobox][aria-required='true']")
    for i in range(await combo_loc.count()):
        el = combo_loc.nth(i)
        try:
            field_id = await el.get_attribute("id") or ""
            if not field_id or field_id in _CORE_IDS or field_id.startswith("iti-"):
                continue
            if not await el.is_visible() or await el.is_disabled():
                continue
            ctrl = el.locator(
                "xpath=ancestor::*[contains(@class,'select__control')]"
            ).first
            committed = (
                await ctrl.count() > 0
                and await ctrl.locator(".select__single-value").count() > 0
            )
            if committed:
                continue
            question = await _label_for(scope, field_id)
            if not question:
                continue
            options = await _combobox_options(scope, frame, field_id)
            fields.append(
                _Field(
                    field_id=field_id,
                    question=question,
                    kind="combobox",
                    options=options,
                )
            )
        except Exception:
            continue

    # ── Yes/No button groups (Ashby) ─────────────────────────────────────────
    # Ashby renders yes/no questions as two <button data-option="yes|no">
    # elements next to a hidden checkbox — no input and no role=combobox, so
    # the passes above never saw them and the required field stayed blank.
    yesno = scope.locator("[data-field-path]:has(button[data-option])")
    for i in range(await yesno.count()):
        group = yesno.nth(i)
        try:
            if not await group.is_visible():
                continue
            pressed = group.locator("button[aria-pressed='true']")
            if await pressed.count() > 0:
                continue  # already answered
            field_id = await group.get_attribute("data-field-path") or ""
            if not field_id or field_id in _CORE_IDS:
                continue
            label = group.locator("label").first
            question = ""
            if await label.count() > 0:
                question = re.sub(r"\s+", " ", (await label.inner_text()).strip())
            if not question:
                continue
            options = await group.locator("button[data-option]").all_inner_texts()
            fields.append(
                _Field(
                    field_id=field_id,
                    question=question,
                    kind="yesno",
                    options=[o.strip() for o in options if o.strip()],
                )
            )
        except Exception:
            continue

    return fields


def _candidate_context(profile: MasterProfile, seed: dict, job: Any) -> str:  # noqa: ANN401
    """Compact, grounded context for the answerer. Never includes guesses."""
    contact = profile.contact
    # Include ALL skills: forms ask about specific ones (Docker, dbt, …) that a
    # top-N cut would hide, causing false "unanswerable" results.
    skills = dict(profile.skill_years.skills)
    experience = [
        {
            "company": e.company,
            "title": e.title,
            "start": str(getattr(e, "start", "") or ""),
            "end": str(getattr(e, "end", "") or ""),
        }
        for e in profile.experience
    ]
    payload = {
        "job": {
            "title": job.title,
            "country": job.country,
            "remote_type": job.remote_type,
        },
        "candidate": {
            "name": contact.name,
            "first_name": (contact.name.split()[0] if contact.name else ""),
            "email": contact.email,
            "phone": contact.phone,
            "city": contact.location.city,
            "years_experience": _years_experience(seed),
            "skills_years": skills,
            "experience": experience,
        },
        "answer_bank": {
            "basics": seed.get("basics"),
            "work_authorization": seed.get("work_authorization"),
            "languages": seed.get("languages"),
            "availability": seed.get("availability"),
            "compensation": seed.get("compensation"),
            "relocation": seed.get("relocation"),
            "education": seed.get("education"),
            "eeo_defaults": seed.get("eeo_defaults"),
            "screening": seed.get("screening"),
        },
        "rules": [
            "The candidate's nationality, residence, education and salary come "
            "from answer_bank; never invent them.",
            "Outside the candidate's home country they REQUIRE visa sponsorship "
            "and relocation support.",
            "For 'authorized to work in <country>' where the country is not the "
            "candidate's home country, answer No.",
            "A vague form asking 'do you have work authorization to work in that "
            "country?' (no country named) refers to the JOB's country: answer Yes "
            "only if the job country is the candidate's home country, otherwise No.",
            "For 'require sponsorship', answer Yes outside the home country.",
            "For salary questions use the answer_bank compensation for the job "
            "country; convert to monthly when asked (annual/12).",
            "If the question names a CURRENCY that differs from the stored one "
            "(e.g. asks USD for an India role), still answer — use the REMOTE "
            "USD band as a reference and state the number only.",
            "For years of experience use candidate.years_experience.",
            "For English level use the candidate's language entry in answer_bank.",
            "For city and country of residence use answer_bank.basics.location.",
            "For GPA use answer_bank.education.gpa. If the question offers "
            "ranges, pick the range that contains the value on the SAME scale as "
            "the options: use education.gpa_20 for a 0-20 scale, education.gpa_10 "
            "for a 0-10 scale, education.gpa_percent for a 0-100 scale.",
            "If it offers grade classes (First Class, Distinction, 2:1), choose "
            "the class equivalent to education.gpa_percent.",
            "For 'how many years' questions answer a bare number (e.g. 9), not "
            "'9 years', especially when the input is numeric.",
            "For any question whose options are numeric ranges (salary, years, "
            "percentage), choose the option whose range contains the known value.",
            "For 'are you okay with a contract/fixed-term/temporary role' use "
            "answer_bank.screening.ok_with_contract_roles (true means Yes).",
            "For 'preferred name' use answer_bank.screening.preferred_name if "
            "non-empty, otherwise candidate.first_name.",
            "For 'how did you hear' answer LinkedIn.",
            "answer_bank.screening holds the candidate's standing answers to "
            "compliance/preference questions (government official, non-compete, "
            "previously employed here, referral, notice period, security "
            "clearance, sports fan, AI tool usage, commute). Use them: a false "
            "means No, a true means Yes; security_clearance 'None' means No.",
            "For the end date of the candidate's CURRENT role answer "
            "answer_bank.screening.current_role_end ('Present'); if that is not "
            "a valid option, leave it unanswerable.",
            "Never invent facts. If the answer is not supported by the data above, "
            "mark it unanswerable.",
            "When options are provided, the answer MUST be one of those options verbatim.",
        ],
    }
    return json.dumps(payload, default=str)


async def answer_custom_questions(
    scope,
    frame,
    job: Any,  # noqa: ANN401
    profile: MasterProfile,
) -> list[str]:
    """Fill remaining required custom questions.

    Returns a list of questions that could not be answered truthfully. An empty
    list means every required custom question was filled.
    """
    # Tick "currently work here" first: it removes the end-date questions for
    # the current role before we enumerate required fields.
    await _check_current_employment(scope)
    await asyncio.sleep(0.5)

    fields = await _collect_fields(scope, frame)
    if not fields:
        return []
    log.debug(
        "question_engine.collected",
        fields=[(f.field_id, f.kind, f.question[:40]) for f in fields],
    )

    seed = load_answers_seed()
    questions = [
        {"field_id": f.field_id, "question": f.question, "options": f.options}
        for f in fields
    ]

    system = (
        "You answer job-application form questions for a specific candidate. "
        "Answer truthfully and ONLY from the supplied data. If the data does not "
        "support an answer, set unanswerable=true and explain why. When a question "
        "lists options, return one option verbatim."
    )
    user = (
        "CANDIDATE DATA:\n"
        + _candidate_context(profile, seed, job)
        + "\n\nQUESTIONS (JSON):\n"
        + json.dumps(questions, indent=2)
        + "\n\nReturn an entry for every field_id."
    )

    try:
        result: FormAnswers = structured_complete(
            task="form_field_mapping",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=FormAnswers,
        )
    except Exception as exc:
        log.warning("question_engine.llm_failed", error=str(exc)[:200])
        return [f.question for f in fields]

    by_id = {a.field_id: a for a in result.answers}
    unresolved: list[str] = []

    for f in fields:
        ans = by_id.get(f.field_id)
        if ans is None or ans.unanswerable or not (ans.answer or "").strip():
            reason = (ans.reason if ans else "no answer returned") or "unanswered"
            log.info(
                "question_engine.unanswerable",
                field_id=f.field_id,
                question=f.question[:80],
                reason=reason[:80],
            )
            unresolved.append(f.question)
            continue

        ok = await _fill_field(scope, frame, f, ans.answer.strip())
        if ok:
            log.debug(
                "question_engine.filled",
                field_id=f.field_id,
                kind=f.kind,
                question=f.question[:60],
                answer=ans.answer[:60],
            )
        else:
            log.info(
                "question_engine.fill_failed",
                field_id=f.field_id,
                question=f.question[:80],
                answer=ans.answer[:60],
            )
            unresolved.append(f.question)

    return unresolved


async def _fill_field(scope, frame, field: _Field, answer: str) -> bool:  # type: ignore[no-untyped-def]
    try:
        if field.kind == "text":
            el = scope.locator(f"[id='{field.field_id}']").first
            await el.scroll_into_view_if_needed(timeout=3000)
            await el.fill(answer, timeout=5000)
            return bool((await el.input_value() or "").strip())

        if field.kind == "select":
            el = scope.locator(f"[id='{field.field_id}']").first
            options = await el.locator("option").all_inner_texts()
            match = _best_option(answer, options)
            if match is None:
                return False
            await el.select_option(label=match)
            return True

        if field.kind == "yesno":
            group = scope.locator(f"[data-field-path='{field.field_id}']").first
            want = answer.strip().lower()
            is_yes = want.startswith(("y", "true")) or "yes" == want
            is_no = want.startswith(("n", "false")) or "no" == want
            if not (is_yes or is_no):
                return False
            btn = group.locator(f"button[data-option='{'yes' if is_yes else 'no'}']").first
            if await btn.count() == 0:
                return False
            await _click_robust(btn)
            await asyncio.sleep(0.4)
            return await group.locator("button[aria-pressed='true']").count() > 0

        # combobox (React-select)
        return await _fill_combobox(scope, frame, field.field_id, answer)
    except Exception as exc:
        log.debug("question_engine.fill_error", field_id=field.field_id, error=str(exc)[:80])
        return False


def _best_option(answer: str, options: list[str]) -> str | None:
    ans = answer.strip().lower()
    for opt in options:
        if opt.strip().lower() == ans:
            return opt
    for opt in options:
        if ans and ans in opt.strip().lower():
            return opt
    for opt in options:
        if opt.strip().lower() and opt.strip().lower() in ans:
            return opt
    return None


async def _fill_combobox(scope, frame, field_id: str, answer: str) -> bool:  # type: ignore[no-untyped-def]
    inp = scope.locator(f"[id='{field_id}']").first
    ctrl = inp.locator("xpath=ancestor::*[contains(@class,'select__control')]").first
    target = ctrl if (await ctrl.count() > 0 and await ctrl.is_visible()) else inp
    await _click_robust(target)
    await asyncio.sleep(0.6)

    opts = (frame or scope).locator("[role=option]")
    texts: list[str] = []
    for i in range(await opts.count()):
        if await opts.nth(i).get_attribute("data-dial-code"):
            continue
        texts.append((await opts.nth(i).inner_text()).strip())

    match = _best_option(answer, [t for t in texts if t])
    if match is not None:
        for i in range(await opts.count()):
            if (await opts.nth(i).inner_text()).strip() == match:
                await _click_robust(opts.nth(i))
                await asyncio.sleep(0.3)
                return True

    # Free-text fallback: type the answer and accept the first suggestion.
    try:
        await inp.fill(answer)
        await asyncio.sleep(0.6)
        opts2 = (frame or scope).locator("[role=option]")
        if await opts2.count() > 0:
            await _click_robust(opts2.first)
            await asyncio.sleep(0.3)
            return True
        await inp.press("Enter")
        await asyncio.sleep(0.3)
    except Exception:
        pass

    committed = (
        await inp.locator(
            "xpath=ancestor::*[contains(@class,'select__control')]"
            "//*[contains(@class,'select__single-value')]"
        ).count()
    )
    try:
        await inp.press("Escape", timeout=1500)
    except Exception:
        pass
    return committed > 0
