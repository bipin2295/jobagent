"""Tailoring planner: classify job archetype and select bullets from the master profile."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from jobagent.db.models import Company, Job, Score
from jobagent.llm.client import ConfigError
from jobagent.llm.structured import structured_complete
from jobagent.profile.schema import MasterProfile

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class BulletSelection(BaseModel):
    bullet_id: str
    use_alt: bool = False
    rewrite_hint: str = ""


class PlanResult(BaseModel):
    archetype: str = "DEFAULT"
    title_variant: str = "DEFAULT"
    summary_variant: str = "DEFAULT"
    bullet_selections: list[BulletSelection] = Field(default_factory=list)
    skills_first: list[str] = Field(default_factory=list)
    positioning_note: str = ""


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_plan_prompt() -> str:
    """Load and cache the tailoring plan prompt template."""
    prompt_path = Path("prompts") / "tailor_plan.md"
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Tailoring plan prompt not found at {prompt_path}. "
            "Expected prompts/tailor_plan.md relative to the working directory."
        )
    return prompt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Profile summary builder
# ---------------------------------------------------------------------------


def _parse_experience_start(s: str) -> date:
    """Parse experience_start string ("YYYY-MM" or "Month YYYY") into a date."""
    if not s:
        return date(2017, 7, 1)
    s = s.strip()
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    try:
        return datetime.strptime(s, "%B %Y").date()
    except ValueError:
        pass
    m = re.match(r"^(\d{4})$", s)
    if m:
        return date(int(m.group(1)), 1, 1)
    return date(2017, 7, 1)


def _build_profile_summary(profile: MasterProfile) -> str:
    """Build a compact candidate profile summary for the LLM tailoring prompt.

    Format::

        Candidate: <candidate>
        Experience: 9 years (2017 – present)
        Current: Senior Data Engineer at Acme (2022-07 – present)
        Key skills: dbt (4yr) snowflake (4yr) python (7yr) ...
        NOT available: skill_a, skill_b (never to be claimed)

        Bullets available:
          [VISTA-01] (AE, LEAD) Built X that cut runtime by 38%…
          ...
    """
    name = profile.contact.name

    start_date = _parse_experience_start(profile.contact.experience_start)
    today = date.today()
    total_years = max(1, int((today - start_date).days / 365.25))
    experience_start = str(start_date.year)

    # Latest job
    latest_job = profile.experience[0] if profile.experience else None
    latest_title = latest_job.title if latest_job else ""
    latest_company = latest_job.company if latest_job else ""
    latest_start = latest_job.start if latest_job else ""

    # Top 12 skills sorted by years descending, formatted "dbt (4yr) snowflake (4yr) ..."
    skill_items = sorted(
        profile.skill_years.skills.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )[:12]
    skills_str = " ".join(f"{k} ({v}yr)" for k, v in skill_items) if skill_items else "—"

    # NOT available skills (first 6, never to be claimed)
    not_yet_str = ", ".join(profile.not_yet[:6]) if profile.not_yet else "none"

    # All bullets across all jobs
    bullet_lines: list[str] = []
    for job_exp in profile.experience:
        for bullet in job_exp.bullets:
            tags_str = ", ".join(bullet.tags) if bullet.tags else ""
            # Use short one-liner; fall back to first 60 chars of resume text
            if bullet.short:
                display = bullet.short
            elif bullet.resume:
                display = bullet.resume[:60].rstrip()
                if len(bullet.resume) > 60:
                    display += "…"
            else:
                display = ""
            bullet_lines.append(f"  [{bullet.id}] ({tags_str}) {display}")

    bullets_str = "\n".join(bullet_lines) if bullet_lines else "  (no bullets available)"

    return (
        f"Candidate: {name}\n"
        f"Experience: {total_years} years ({experience_start} – present)\n"
        f"Current: {latest_title} at {latest_company} ({latest_start} – present)\n"
        f"Key skills: {skills_str}\n"
        f"NOT available: {not_yet_str} (never to be claimed)\n"
        f"\nBullets available:\n{bullets_str}"
    )


# ---------------------------------------------------------------------------
# JD context builder
# ---------------------------------------------------------------------------


def _build_jd_context(job: Job, score_row: Score | None, company_name: str) -> str:
    """Build a structured job description context block for the LLM tailoring prompt."""
    required_skills = "none listed"
    if job.required_skills_json:
        try:
            skills_list: list[str] = json.loads(job.required_skills_json)
            required_skills = ", ".join(skills_list) if skills_list else "none listed"
        except (json.JSONDecodeError, TypeError):
            required_skills = job.required_skills_json or "none listed"

    location_parts = [
        job.city or "",
        job.country or "",
        job.remote_type or "",
    ]
    location = ", ".join(p for p in location_parts if p) or "Unknown"

    breakdown = score_row.breakdown_json if (score_row and score_row.breakdown_json) else ""
    pitch = score_row.pitch_angle if (score_row and score_row.pitch_angle) else ""

    description_excerpt = (job.description_md or "")[:2000]

    return (
        f"Title: {job.title}\n"
        f"Company: {company_name}\n"
        f"Location: {location}\n"
        f"Visa sponsorship: {job.visa_sponsorship or 'unknown'}\n"
        f"Required skills: {required_skills}\n"
        f"Seniority: {job.seniority or 'unknown'}\n"
        f"Score breakdown: {breakdown}\n"
        f"Pitch from scorer: {pitch}\n"
        f"\nDescription (excerpt):\n{description_excerpt}"
    )


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------

# Keyword lists for each archetype — ordered from most to least specific.
_ARCHETYPE_KEYWORDS: dict[str, list[str]] = {
    "ANALYTICS_ENGINEER_SAAS": [
        "analytics engineer",
        "analytics engineering",
        "dbt",
        "data modelling",
        "data modeling",
        "metrics layer",
        "semantic layer",
        "analyst self-service",
        "looker",
        "metabase",
        "mode analytics",
    ],
    "FINTECH_RISK": [
        "fintech",
        "finance",
        "financial services",
        "banking",
        "bank",
        "insurance",
        "risk",
        "compliance",
        "regulatory",
        "credit",
        "audit",
        "fraud",
        "aml",
        "kyc",
    ],
    "DATA_PLATFORM_ML": [
        "data platform",
        "platform engineering",
        "infrastructure",
        "spark",
        "databricks",
        "pyspark",
        "kafka",
        "flink",
        "mlops",
        "ml platform",
        "machine learning",
        "distributed systems",
        "lakehouse",
        "data mesh",
    ],
    "BIG_COMPANY_LEAD": [
        "staff engineer",
        "principal engineer",
        "tech lead",
        "engineering lead",
        "engineering manager",
        "design review",
        "architecture review",
        "mentoring",
        "mentorship",
        "enterprise",
        "head of data",
    ],
}

_ARCHETYPE_TO_VARIANT: dict[str, str] = {
    "ANALYTICS_ENGINEER_SAAS": "AE",
    "FINTECH_RISK": "FIN",
    "DATA_PLATFORM_ML": "PLAT",
    "BIG_COMPANY_LEAD": "BIGCO",
    "DEFAULT": "DEFAULT",
}


def _classify_archetype(job: Job) -> str:
    """Classify job archetype from title and description keywords."""
    # Build a lowercased search text from title + first 1000 chars of description
    text = " ".join(filter(None, [
        (job.title or "").lower(),
        (job.description_md or "")[:1000].lower(),
    ]))

    scores: dict[str, int] = {arch: 0 for arch in _ARCHETYPE_KEYWORDS}
    for arch, keywords in _ARCHETYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[arch] += 1

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "DEFAULT"
    return best


def _heuristic_plan(job: Job, profile: MasterProfile, company_name: str) -> PlanResult:
    """Simple rule-based fallback plan when the LLM is unavailable.

    Classifies the archetype from job title/description keywords, then looks up
    the matching ``TailoringRecipe`` from the profile to populate bullet IDs.
    Returns the first 8 valid bullets from the recipe (deduplicated, validated).
    """
    archetype = _classify_archetype(job)
    variant = _ARCHETYPE_TO_VARIANT.get(archetype, "DEFAULT")

    # Find the best-matching recipe; fall through to DEFAULT if none found
    recipe = profile.get_recipe(archetype)
    if recipe is None and archetype != "DEFAULT":
        recipe = profile.get_recipe("DEFAULT")

    # Flatten all bullet IDs from the recipe (preserving order across job entries)
    flat_ids: list[str] = []
    if recipe:
        for ids in recipe.bullets.values():
            flat_ids.extend(ids)

    # Validate, deduplicate, and cap at 8
    all_bullets = profile.bullets_by_id
    seen: set[str] = set()
    valid_ids: list[str] = []
    for bid in flat_ids:
        if bid not in seen and bid in all_bullets:
            seen.add(bid)
            valid_ids.append(bid)
        if len(valid_ids) >= 8:
            break

    selections = [BulletSelection(bullet_id=bid) for bid in valid_ids]

    log.info(
        "plan.heuristic",
        archetype=archetype,
        variant=variant,
        bullets=len(selections),
        company=company_name,
        job_id=job.id,
    )

    return PlanResult(
        archetype=archetype,
        title_variant=variant,
        summary_variant=variant,
        bullet_selections=selections,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan(job: Job, score_row: Score | None, profile: MasterProfile, session) -> PlanResult:  # type: ignore[no-untyped-def]
    """Classify job archetype and select resume bullets for a tailored application.

    Calls the LLM via ``structured_complete`` with the tailoring plan prompt,
    validates that every returned bullet ID exists in the profile, and drops any
    that are missing.  Falls back to ``_heuristic_plan`` on ``ConfigError`` (LLM
    not configured) or any other exception.

    Args:
        job:       The ``Job`` row to tailor for.
        score_row: The latest ``Score`` row for this job (may be ``None``).
        profile:   The loaded ``MasterProfile``.
        session:   An open SQLModel ``Session`` used only to look up the company name.

    Returns:
        A validated ``PlanResult`` with archetype, title/summary variants, and
        bullet selections ordered by priority.
    """
    # Resolve company name from the Company table
    company_name = ""
    if job.company_id is not None:
        company = session.get(Company, job.company_id)
        if company:
            company_name = company.name

    result: PlanResult
    try:
        prompt_template = _load_plan_prompt()
        profile_summary = _build_profile_summary(profile)
        jd_context = _build_jd_context(job, score_row, company_name)

        prompt = prompt_template.replace("{{JD_CONTEXT}}", jd_context)
        prompt = prompt.replace("{{PROFILE_SUMMARY}}", profile_summary)

        result = structured_complete(
            task="tailoring_plan",
            messages=[{"role": "user", "content": prompt}],
            model=PlanResult,
        )

        # Validate: drop any bullet IDs that don't exist in the profile
        all_bullets = profile.bullets_by_id
        valid_selections = [s for s in result.bullet_selections if s.bullet_id in all_bullets]
        dropped = [s.bullet_id for s in result.bullet_selections if s.bullet_id not in all_bullets]
        if dropped:
            log.warning(
                "plan.invalid_bullets_dropped",
                dropped=dropped,
                job_id=job.id,
                title=job.title,
            )
        result = result.model_copy(update={"bullet_selections": valid_selections})

    except ConfigError:
        log.info(
            "plan.llm_not_configured",
            job_id=job.id,
            title=job.title,
            fallback="heuristic",
        )
        result = _heuristic_plan(job, profile, company_name)
    except Exception as exc:
        log.warning(
            "plan.llm_failed",
            job_id=job.id,
            title=job.title,
            error=str(exc),
            fallback="heuristic",
        )
        result = _heuristic_plan(job, profile, company_name)

    log.info(
        "plan.done",
        archetype=result.archetype,
        bullets=len(result.bullet_selections),
        job_id=job.id,
        title=job.title,
        company=company_name,
    )

    return result
