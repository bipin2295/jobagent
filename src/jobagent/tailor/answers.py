"""Screening question answer engine.

Priority order for each field:
1. answer_bank from answers_seed.yaml (deterministic, country-aware)
2. profile skill_years for "Years of experience with X?" questions
3. LLM inference citing profile field
4. Safe default policy
5. 'skipped_unanswerable' if none apply (legal/eligibility with no truthful answer)
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

from jobagent.config import settings
from jobagent.db.models import Job
from jobagent.profile.schema import MasterProfile

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Seed loading
# ---------------------------------------------------------------------------


def load_answers_seed() -> dict:  # type: ignore[type-arg]
    """Load data/profile/answers_seed.yaml.

    Returns an empty dict if the file is not found, so callers always get a
    safe dict to ``.get()`` against.
    """
    seed_path: Path = settings.profiles_dir / "answers_seed.yaml"
    if not seed_path.exists():
        log.warning("answers_seed.not_found", path=str(seed_path))
        return {}
    try:
        with seed_path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as exc:
        log.warning("answers_seed.load_error", path=str(seed_path), error=str(exc))
        return {}


# ---------------------------------------------------------------------------
# Salary helpers
# ---------------------------------------------------------------------------


def get_salary_answer(country: str, period: str, answers_seed: dict) -> str | None:  # type: ignore[type-arg]
    """Return the expected salary answer for *country*, formatted as a string.

    Looks up ``answers_seed["compensation"][country]``.  Returns ``None`` if
    there is no entry or the entry lacks an ``expected`` value (e.g. the special
    ``IN`` block which uses LPA format rather than the standard fields).

    Args:
        country:      ISO-3166-1 alpha-2 country code (or a custom key like
                      ``"REMOTE"``, ``"UK_LONDON"``).
        period:       Caller's requested period type ("year" or "month");
                      currently informational — the seed's own ``period`` field
                      is used in the formatted string.
        answers_seed: The loaded seed dict from ``load_answers_seed()``.

    Returns:
        A human-readable salary string such as ``"85000 EUR per year"`` or
        ``"30000 AED per month"``, or ``None`` if the country is not configured.
    """
    comp_block = answers_seed.get("compensation", {}).get(country)
    if not comp_block:
        return None

    # India stores CTC in lakhs-per-annum rather than a plain ``expected``
    # figure. Handle it here so an IN role does not silently fall through to
    # the EUR default in build_answer_map.
    if comp_block.get("expected") is None and comp_block.get("expected_ctc_lpa"):
        ctc = comp_block["expected_ctc_lpa"]
        return f"{ctc} LPA {comp_block.get('currency', 'INR')}"

    expected = comp_block.get("expected")
    if expected is None:
        return None

    currency = comp_block.get("currency", "")
    comp_period = comp_block.get("period", "year")

    if comp_period:
        return f"{expected} {currency} per {comp_period}"
    return f"{expected} {currency}"


def _format_current_ctc(answers_seed: dict) -> str:  # type: ignore[type-arg]
    """Return the current CTC for India, formatted as "<value> LPA INR" (e.g. "30 LPA INR")."""
    in_block = answers_seed.get("compensation", {}).get("IN", {})
    ctc_lpa = in_block.get("current_ctc_lpa", "")
    if ctc_lpa:
        return f"{ctc_lpa} LPA INR"
    return ""


# ---------------------------------------------------------------------------
# Main answer-map builder
# ---------------------------------------------------------------------------


def build_answer_map(job: Job, profile: MasterProfile) -> dict[str, str]:
    """Build a dict of common form field patterns → deterministic answer strings.

    The map keys are normalised field names (lowercase, underscored) that the
    ATS filler uses to match form labels.  All values are plain strings.

    Work-authorisation answers are country-aware: Indian citizen in IN; requires
    sponsorship everywhere else.  Salary answers prefer the job's country,
    falling back to EUR, then USD.

    Skill-year answers cover three common label patterns per skill:
      - ``years_of_<skill>_experience``
      - ``<skill>_experience_years``
      - ``years_experience_<skill>``

    Args:
        job:     The ``Job`` row being applied to (provides ``country``).
        profile: The loaded ``MasterProfile`` (provides skill years, contact).

    Returns:
        A flat ``dict[str, str]`` ready for ATS form filling.
    """
    answers_seed = load_answers_seed()
    is_india = (job.country or "").upper() == "IN"

    # ── Work authorisation ────────────────────────────────────────────────────
    work_auth: dict[str, str] = {
        "work_authorization":           "Indian citizen" if is_india else "Requires sponsorship",
        "require_sponsorship":          "No" if is_india else "Yes",
        "right_to_work":                "Yes" if is_india else "No",
        "visa_sponsorship_required":    "No" if is_india else "Yes",
    }

    # ── Availability / personal ───────────────────────────────────────────────
    availability = answers_seed.get("availability", {})
    basics = answers_seed.get("basics", {})

    # profile.contact.phone is already the display string (e.g. "+91-98XXXXXXXX")
    phone = profile.contact.phone or basics.get("phone", {}).get("display", "")

    personal: dict[str, str] = {
        "notice_period":    availability.get("notice_period", "4 weeks"),
        "notice_period_days": str(profile.contact.notice_period_days),
        "earliest_start":   availability.get("earliest_start", "30 days from offer"),
        "willing_to_relocate": "Yes",
        "phone":            phone,
        # Residence city — taken from the loaded profile, never hardcoded.
        "city":             profile.contact.location.city,
    }

    # ── Salary ────────────────────────────────────────────────────────────────
    # Try job country first, then fall back to EUR (for EU roles), then USD.
    country_key = (job.country or "").upper()
    expected_salary = (
        get_salary_answer(country_key, "year", answers_seed)
        or get_salary_answer("DE", "year", answers_seed)   # generic EUR fallback
        or get_salary_answer("REMOTE", "year", answers_seed)
        or "Open to discussion"
    )

    salary: dict[str, str] = {
        "current_salary":  _format_current_ctc(answers_seed),
        "expected_salary": expected_salary,
    }

    # ── Diversity / EEO ───────────────────────────────────────────────────────
    eeo_defaults = answers_seed.get("eeo_defaults", {})

    diversity: dict[str, str] = {
        # gender_when_required_no_optout lives under eeo_defaults in the seed;
        # falls through to "" if the key is absent.
        "gender":    eeo_defaults.get("gender_when_required_no_optout", ""),
        "ethnicity": "Prefer not to say",
        "disability": "Prefer not to say",
        "veteran":   "I am not a veteran",
    }

    # ── Assessments / sourcing ────────────────────────────────────────────────
    misc: dict[str, str] = {
        "willing_to_take_assessment":    "Yes",
        "willing_to_complete_skills_test": "Yes",
        "how_did_you_hear": "Job board",
        "source":           "Job board",
    }

    # ── Assemble base map ─────────────────────────────────────────────────────
    answer_map: dict[str, str] = {
        **work_auth,
        **personal,
        **salary,
        **diversity,
        **misc,
    }

    # ── Skill-year answers ────────────────────────────────────────────────────
    # Three label-pattern variants per skill to maximise ATS field matching.
    for skill, years in profile.skill_years.skills.items():
        years_str = str(years)
        answer_map[f"years_of_{skill}_experience"] = years_str
        answer_map[f"{skill}_experience_years"] = years_str
        answer_map[f"years_experience_{skill}"] = years_str

    log.debug(
        "answers.build_answer_map.done",
        job_id=job.id,
        country=job.country,
        total_keys=len(answer_map),
    )

    return answer_map
