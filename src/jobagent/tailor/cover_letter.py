"""Cover letter generator."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import structlog

from jobagent.db.models import Company, Job
from jobagent.llm.client import ConfigError, complete
from jobagent.profile.schema import MasterProfile
from jobagent.tailor.planner import PlanResult

log = structlog.get_logger(__name__)

_FALLBACK_TEMPLATE = (
    "Dear Hiring Team,\n\n"
    "I am writing to apply for the {title} role at {company}.\n\n"
    "[Cover letter unavailable — LLM not configured.]\n"
)


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_cover_letter_prompt() -> str:
    """Load and cache the cover letter prompt template."""
    prompt_path = Path("prompts") / "cover_letter.md"
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Cover letter prompt not found at {prompt_path}. "
            "Expected prompts/cover_letter.md relative to the working directory."
        )
    return prompt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_cover_letter(
    job: Job,
    plan: PlanResult,
    profile: MasterProfile,
    session,  # type: ignore[no-untyped-def]
) -> str:
    """Generate a targeted, 3-paragraph cover letter for a job application.

    Resolves the company name, builds the top 2 achievement bullets from the
    plan, substitutes all placeholders in ``prompts/cover_letter.md``, and calls
    the LLM via ``complete``.

    On ``ConfigError`` (LLM not configured) or any other exception, returns a
    plain-text fallback template so the application pipeline can continue.

    Args:
        job:     The ``Job`` row to generate the letter for.
        plan:    The ``PlanResult`` from the tailoring planner.
        profile: The loaded ``MasterProfile``.
        session: An open SQLModel ``Session`` used only for Company lookups.

    Returns:
        Cover letter body text (~180–220 words).
    """
    # Resolve company name from the Company table
    company_name = ""
    if job.company_id is not None:
        company = session.get(Company, job.company_id)
        if company:
            company_name = company.name
    if not company_name:
        company_name = "the company"

    # Build top achievements: first 2 bullet IDs from the plan
    all_bullets = profile.bullets_by_id
    top_bullet_lines: list[str] = []
    for sel in plan.bullet_selections[:2]:
        bullet = all_bullets.get(sel.bullet_id)
        if bullet is None:
            continue
        # Prefer the short one-liner; fall back to effective_text
        text = (bullet.short or bullet.effective_text or "").strip()
        if text:
            top_bullet_lines.append(f"• {text}")

    top_bullets_str = "\n".join(top_bullet_lines) if top_bullet_lines else "(see resume)"

    # Location string shown in the prompt
    location_parts = [job.city or "", job.country or ""]
    location = ", ".join(p for p in location_parts if p) or "Remote"

    # JD excerpt — 600 chars is enough context without over-constraining the LLM
    jd_excerpt = (job.description_md or "")[:600]

    # Positioning note from planner, with a sensible fallback
    pitch = (plan.positioning_note or "Strong technical match for this role").strip()

    try:
        prompt_template = _load_cover_letter_prompt()
        prompt = (
            prompt_template
            .replace("{{CANDIDATE_NAME}}", profile.contact.name)
            .replace("{{JOB_TITLE}}", job.title)
            .replace("{{COMPANY_NAME}}", company_name)
            .replace("{{LOCATION}}", location)
            .replace("{{PITCH_ANGLE}}", pitch)
            .replace("{{TOP_BULLETS}}", top_bullets_str)
            .replace("{{JD_EXCERPT}}", jd_excerpt)
        )

        content = complete(
            task="cover_letter",
            messages=[{"role": "user", "content": prompt}],
        )

        log.info(
            "cover_letter.done",
            job_id=job.id,
            title=job.title,
            company=company_name,
            chars=len(content),
        )
        return content

    except ConfigError:
        log.info(
            "cover_letter.llm_not_configured",
            job_id=job.id,
            title=job.title,
            fallback="template",
        )
        return _FALLBACK_TEMPLATE.format(title=job.title, company=company_name)

    except Exception as exc:
        log.warning(
            "cover_letter.failed",
            job_id=job.id,
            title=job.title,
            company=company_name,
            error=str(exc),
            fallback="template",
        )
        return _FALLBACK_TEMPLATE.format(title=job.title, company=company_name)
