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
    """Best-effort visible question text for the control with *field_id*.

    *field_id* may be an element id or an Ashby ``data-field-path`` value.
    """
    lbl = scope.locator(f"label[for='{field_id}']")
    if await lbl.count() > 0:
        text = (await lbl.first.inner_text()).strip()
        if text:
            return re.sub(r"\s+", " ", text)

    # data-field-path wrapper: the question is the wrapper's <label>.
    wrapper = scope.locator(f"[data-field-path='{field_id}']")
    if await wrapper.count() > 0:
        inner = wrapper.first.locator("label").first
        if await inner.count() > 0:
            text = (await inner.inner_text()).strip()
            if text:
                return re.sub(r"\s+", " ", text)

    inp = scope.locator(f"[id='{field_id}']")
    if await inp.count() == 0:
        inp = scope.locator(f"[data-field-path='{field_id}'] input, [data-field-path='{field_id}'] textarea")
    target = inp.first
    try:
        aria = await target.get_attribute("aria-label")
        if aria:
            return aria.strip()
    except Exception:
        pass
    try:
        text = await target.evaluate(
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
    # Ashby marks questions "required" only via CSS classes and often omits the
    # `id`, so neither `[required]` nor `[aria-required]` matches. Walk every
    # visible empty text control and resolve its label from the enclosing
    # field-entry wrapper instead.
    text_loc = scope.locator(
        "input[aria-required='true'], textarea[aria-required='true'], "
        "input[required], textarea[required], "
        "[data-field-path] input[type='text'], [data-field-path] input[type='number'], "
        "[data-field-path] input[type='url'], [data-field-path] textarea, "
        "[data-field-entry-id] input:not([type='hidden']), "
        "[data-field-entry-id] textarea"
    )
    seen_text: set[str] = set()
    for i in range(await text_loc.count()):
        el = text_loc.nth(i)
        try:
            field_id = await el.get_attribute("id") or ""
            if field_id and field_id in _CORE_IDS:
                continue
            if field_id and field_id.startswith("iti-"):
                continue
            role = (await el.get_attribute("role") or "").lower()
            if role == "combobox":
                continue  # handled below
            field_type = (await el.get_attribute("type") or "").lower()
            if field_type in _SKIP_INPUT_TYPES:
                continue
            if not await el.is_visible() or await el.is_disabled():
                continue
            if (await el.input_value() or "").strip():
                continue

            # Fall back to the wrapper's data-field-path when the input has no
            # id — that is exactly how Ashby renders custom text questions.
            if not field_id:
                field_id = (
                    await el.evaluate(
                        "e => { const w = e.closest('[data-field-path]'); "
                        "return w ? w.getAttribute('data-field-path') : ''; }"
                    )
                    or ""
                )
            if not field_id or field_id in seen_text:
                continue
            seen_text.add(field_id)
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
    combo_loc = scope.locator(
        "[role=combobox][aria-required='true'], "
        "[data-field-path] [role=combobox], "
        "[data-field-entry-id] [role=combobox]"
    )
    for i in range(await combo_loc.count()):
        el = combo_loc.nth(i)
        try:
            field_id = await el.get_attribute("id") or ""
            if field_id and (field_id in _CORE_IDS or field_id.startswith("iti-")):
                continue
            if not field_id:
                field_id = (
                    await el.evaluate(
                        "e => { const w = e.closest('[data-field-path]'); "
                        "return w ? w.getAttribute('data-field-path') : ''; }"
                    )
                    or ""
                )
            if not field_id:
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

    # ── Radio groups ─────────────────────────────────────────────────────────
    # Ashby renders "willing to relocate?" style questions as a radio group
    # (yes / no / already there). The engine ignored them, leaving a required
    # question blank while every text field looked correct.
    radios = scope.locator("input[type=radio]")
    groups: dict[str, list] = {}
    for i in range(await radios.count()):
        r = radios.nth(i)
        try:
            if not await r.is_visible():
                continue
            name = await r.get_attribute("name") or ""
            if not name:
                continue
            groups.setdefault(name, []).append(r)
        except Exception:
            continue

    for name, members in groups.items():
        try:
            checked = any([await m.is_checked() for m in members])
            if checked:
                continue
            label_el = scope.locator(f"input[type=radio][name='{name}']").first
            question = ""
            try:
                question = await label_el.evaluate(
                    "e => { const fs = e.closest('fieldset'); if (fs) { "
                    "const lg = fs.querySelector('legend'); if (lg) return lg.innerText; } "
                    "const w = e.closest('[data-field-path]'); "
                    "if (w) { const l = w.querySelector('label'); if (l) return l.innerText; } "
                    "return ''; }"
                )
            except Exception:
                question = ""
            question = re.sub(r"\s+", " ", (question or "").strip())
            if not question:
                continue
            options: list[str] = []
            for m in members:
                try:
                    mid = await m.get_attribute("id") or ""
                    txt = ""
                    if mid:
                        lb = scope.locator(f"label[for='{mid}']")
                        if await lb.count() > 0:
                            txt = (await lb.first.inner_text()).strip()
                    options.append(txt)
                except Exception:
                    options.append("")
            field_id = f"radio:{name}"
            if field_id in seen_text:
                continue
            seen_text.add(field_id)
            fields.append(
                _Field(
                    field_id=field_id,
                    question=question,
                    kind="radio",
                    options=[o for o in options if o],
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


_SALARY_RE = re.compile(
    r"salary|compensation|remuneration|pay\s+expectation|expected\s+pay|"
    r"gross\s+(?:salary|pay)|annual\s+(?:salary|pay)|daily\s+rate|day\s+rate",
    re.IGNORECASE,
)

_EUROPE = frozenset(
    "DE NL FR IE LU SE DK FI AT BE ES PT PL IT CZ EE LT SI CY MT HU RO BG "
    "HR SK LV GR CH NO IS GB UK".split()
)


def _is_salary_question(question: str) -> bool:
    return bool(_SALARY_RE.search(question or ""))


def _salary_answer_for(job: Any, question: str, seed: dict) -> str | None:  # noqa: ANN401
    """Return a formatted salary answer for *question*, or None if unknown.

    Uses the job country's band, falling back to a regional default, then to
    REMOTE (USD). Emits a bare number plus the period the question asks for,
    because ATS salary fields are usually numeric inputs.
    """
    comp = (seed.get("compensation") or {})
    country = (getattr(job, "country", None) or "").upper()

    key: str | None = None
    if country and country in comp:
        key = country
    elif country == "GB":
        key = "UK"
    elif country in _EUROPE:
        key = "DE"
    elif country == "IN":
        key = "IN"
    elif comp.get("REMOTE"):
        key = "REMOTE"

    block = comp.get(key) if key else None
    if not isinstance(block, dict):
        return None

    # India stores CTC in lakhs rather than a plain annual figure.
    if key == "IN" and block.get("expected") is None:
        lpa = block.get("expected_ctc_lpa")
        if lpa is None:
            return None
        return f"{int(float(lpa) * 100000)} INR per year"

    expected = block.get("expected")
    if expected is None:
        return None
    period = str(block.get("period", "year")).lower()

    q = (question or "").lower()
    if "month" in q:
        value = float(expected) / 12
        unit = "per month"
    elif "day" in q or "daily" in q:
        value = float(expected) / 260
        unit = "per day"
    else:
        value = float(expected)
        unit = "per year"

    text = f"{value:.0f}" if abs(value - round(value)) < 0.01 else f"{value:.2f}"
    return f"{text} {block.get('currency', '')} {unit}".strip()


def _candidate_context(profile: MasterProfile, seed: dict, job: Any) -> str:  # noqa: ANN401
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
            "ALWAYS answer a salary question from answer_bank.compensation. Use "
            "the entry for the job country; if that country is absent, use DE "
            "(EUR) when the job is in Europe, REMOTE (USD) otherwise. The stored "
            "'expected' value is ANNUAL gross — use it as-is for 'annual', "
            "divide by 12 for 'monthly', divide by 12 for 'per month'.",
            "A salary question is answerable whenever answer_bank.compensation "
            "has an entry for the job country or a regional default — do NOT "
            "report it unanswerable merely because the question says 'annual'.",
            "If the question names a CURRENCY that differs from the stored one "
            "(e.g. asks USD for an India role), still answer — use the REMOTE "
            "USD band as a reference and state the number only.",
            "For years of experience use candidate.years_experience.",
            "For English level use the candidate's language entry in answer_bank.",
            "For city and country of residence use answer_bank.basics.location.",
            "For 'current country and city of residence' answer "
            "'India, <city>' from answer_bank.basics.location.",
            "For 'earliest possible start date' use "
            "answer_bank.screening.earliest_start_date.",
            "For 'first line of your address' / address line 1 use "
            "answer_bank.screening.street_address; if that is empty, answer with "
            "the city and country from answer_bank.basics.location instead of "
            "reporting it unanswerable.",
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
            "For 'able/willing to work remotely' use "
            "answer_bank.screening.willing_to_work_remotely (true = Yes).",
            "For 'are you related to any current employees' use "
            "answer_bank.screening.related_to_employees (false = No).",
            "For consent to interview recording / AI note-takers / interview "
            "integrity tools use "
            "answer_bank.screening.consent_interview_recording (true = Yes).",
            "For pronouns use answer_bank.screening.pronouns.",
            "For 'your location' / residence questions answer '<city>, <country>' from answer_bank.basics.location (e.g. 'Pune, India').",
            "For 'what is the second line of your address' answer with the city and postcode from answer_bank.basics.location.",
            "For 'largest/most complex cloud dataset you have worked with' use answer_bank.screening.current_cloud_dataset.",
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

        # Deterministic override for salary questions. The LLM keeps refusing
        # these ("no data") even when the seed has an exact figure for the job
        # country, so resolve them in code instead — same source of truth as
        # tailor/answers.py and the filter's salary floor.
        if _is_salary_question(f.question):
            deterministic = _salary_answer_for(job, f.question, seed)
            if deterministic is not None:
                ans = FieldAnswer(
                    field_id=f.field_id,
                    answer=deterministic,
                    unanswerable=False,
                )
                log.debug(
                    "question_engine.salary_deterministic",
                    question=f.question[:50],
                    answer=deterministic,
                )

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
            el = scope.locator(f"[id='{field.field_id}']")
            if await el.count() == 0:
                # Ashby renders custom text questions without an id; target the
                # input/textarea inside the data-field-path wrapper.
                el = scope.locator(
                    f"[data-field-path='{field.field_id}'] input:not([type='hidden']), "
                    f"[data-field-path='{field.field_id}'] textarea"
                )
            target = el.first
            await target.scroll_into_view_if_needed(timeout=3000)

            value = answer
            # Numeric inputs reject "85000 EUR per year" outright — strip to the
            # bare number the field expects (a digit-only input silently refuses
            # the text and the field stays empty).
            input_type = (await target.get_attribute("type") or "").lower()
            if input_type in ("number", "tel"):
                m = re.search(r"-?\d+(?:[.,]\d+)?", answer)
                if m:
                    value = m.group(0).replace(",", "")
            # Playwright's fill() sets the value through the native setter but
            # React-controlled inputs (Ashby is React) sometimes ignore it: the
            # DOM shows the text, yet the component's state stays empty and the
            # server rejects the field as missing. Type the value instead so
            # every keystroke fires the real input/change handlers.
            await target.click(timeout=3000)
            await target.fill("", timeout=3000)
            await target.type(value, delay=25, timeout=8000)

            # Blur to commit React's onBlur validation.
            try:
                await target.press("Tab", timeout=2000)
            except Exception:
                pass
            return bool((await target.input_value() or "").strip())

        if field.kind == "select":
            el = scope.locator(f"[id='{field.field_id}']").first
            options = await el.locator("option").all_inner_texts()
            match = _best_option(answer, options)
            if match is None:
                return False
            await el.select_option(label=match)
            return True

        if field.kind == "radio":
            # field_id is "radio:<name>"; pick the option whose label matches.
            name = field.field_id.split(":", 1)[1]
            members = scope.locator(f"input[type=radio][name='{name}']")
            target_text = answer.strip().lower()
            for i in range(await members.count()):
                m = members.nth(i)
                try:
                    mid = await m.get_attribute("id") or ""
                    label = ""
                    if mid:
                        lb = scope.locator(f"label[for='{mid}']")
                        if await lb.count() > 0:
                            label = (await lb.first.inner_text()).strip().lower()
                    if not label:
                        continue
                    if label == target_text or target_text in label or label in target_text:
                        await m.check(timeout=5000)
                        return await m.is_checked()
                except Exception:
                    continue
            return False

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
    # field_id may be an element id OR a data-field-path value (Ashby system
    # fields such as the residence combobox have no id at all).
    inp = scope.locator(f"[id='{field_id}']")
    if await inp.count() == 0:
        inp = scope.locator(
            f"[data-field-path='{field_id}'] [role=combobox], "
            f"[data-field-path='{field_id}'] input:not([type='hidden'])"
        )
    if await inp.count() == 0:
        return False
    anchor = inp.first
    ctrl = anchor.locator("xpath=ancestor::*[contains(@class,'select__control')]").first
    target = ctrl if (await ctrl.count() > 0 and await ctrl.is_visible()) else anchor
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
