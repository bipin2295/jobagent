"""Scoring pipeline: embedding funnel → LLM rubric → store in Score table.

Flow:
  status='ready_to_score'
    → embed job + cosine similarity with profile  (free, local Ollama)
    → if similarity < threshold: status='rejected_low_score', reason='low_embedding_sim'
    → if similarity >= threshold: LLM rubric score
    → if score < min_score: status='rejected_low_score'
    → if score >= min_score: status='queued', job.score = score
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import col, select

from jobagent.config import get_model_config, load_yaml
from jobagent.db.engine import get_session
from jobagent.db.models import Company, Job, Score
from jobagent.llm.embeddings import cosine_similarity, embed_text
from jobagent.llm.structured import structured_complete
from jobagent.profile.loader import load_profile
from jobagent.profile.schema import MasterProfile

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pydantic output model
# ---------------------------------------------------------------------------


class ScoringResult(BaseModel):
    score: int = Field(ge=0, le=100)
    skills_match: int = Field(ge=0, le=30, default=0)       # was 40
    experience_match: int = Field(ge=0, le=20, default=0)    # was 25
    domain_match: int = Field(ge=0, le=10, default=0)
    location_visa_fit: int = Field(ge=0, le=30, default=0)   # was 15 — most important
    seniority_fit: int = Field(ge=0, le=10, default=0)
    missing_must_haves: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    pitch_angle: str = ""


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_score_prompt() -> str:
    """Load and cache the scoring prompt template from prompts/score_jd.md."""
    prompt_path = Path("prompts") / "score_jd.md"
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Scoring prompt not found at {prompt_path}. "
            "Expected prompts/score_jd.md relative to the working directory."
        )
    return prompt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Candidate profile text builders
# ---------------------------------------------------------------------------


def _parse_experience_start(s: str) -> date:
    """Parse ``experience_start`` ("YYYY-MM" or "Month YYYY") into a date."""
    if not s:
        return date(2017, 1, 1)
    s = s.strip()
    # "YYYY-MM"
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    # "Month YYYY" e.g. "July 2017"
    try:
        return datetime.strptime(s, "%B %Y").date()
    except ValueError:
        pass
    # "YYYY"
    m = re.match(r"^(\d{4})$", s)
    if m:
        return date(int(m.group(1)), 1, 1)
    return date(2017, 1, 1)


def _build_candidate_profile_text(profile: MasterProfile) -> str:
    """Return a compact multi-line candidate summary for the LLM scoring prompt.

    Format::

        Name: Bipin Singh
        Role: Senior Data Engineer / Analytics Engineer / Lead DE
        Experience: 8 years total (2017–present)
        Current: Senior Data Engineer at Acme
        Key skills: dbt (4yr), Python (7yr), ...
        Strong domains: AE, PLAT, FIN
        Top achievements:
          • [Acme] Built X that did Y ...
        Target: Senior/Lead/Staff roles, visa sponsorship required outside India, willing to relocate
    """
    name = profile.contact.name

    # Total years of experience
    start_date = _parse_experience_start(profile.contact.experience_start)
    today = date.today()
    years = max(1, int((today - start_date).days / 365.25))
    start_year = start_date.year

    # Latest position
    latest_job = profile.experience[0] if profile.experience else None
    current_title = latest_job.title if latest_job else "Senior Data Engineer"
    current_company = latest_job.company if latest_job else ""

    # Top 15 skills sorted by years desc, formatted as "dbt (4yr)"
    skill_items = sorted(
        profile.skill_years.skills.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )[:15]
    skills_str = ", ".join(f"{k} ({v}yr)" for k, v in skill_items) if skill_items else "—"

    # Strong domains: collect unique uppercase short tags from bullets (AE, PLAT, FIN, REL …)
    domains: list[str] = []
    seen_tags: set[str] = set()
    for job_exp in profile.experience:
        for bullet in job_exp.bullets:
            for tag in bullet.tags:
                # Accept short all-caps codes (2–6 chars)
                if tag == tag.upper() and tag.isalpha() and 2 <= len(tag) <= 6 and tag not in seen_tags:
                    domains.append(tag)
                    seen_tags.add(tag)
    domains_str = ", ".join(domains[:10]) if domains else "DE"

    # Top achievements: first bullet from each of the last 5 jobs (1–2 sentences)
    achievements: list[str] = []
    for job_exp in profile.experience[:5]:
        if not job_exp.bullets:
            continue
        b = job_exp.bullets[0]
        # Prefer the short variant; fall back to full resume text
        text = (b.short or b.resume or "").strip()
        if not text:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", text)
        snippet = " ".join(sentences[:2])
        if snippet:
            achievements.append(f"  \u2022 [{job_exp.company}] {snippet}")

    achievements_str = "\n".join(achievements) if achievements else "  \u2022 (see resume)"

    return (
        f"Name: {name}\n"
        f"Role: Senior Data Engineer / Analytics Engineer / Lead DE\n"
        f"Experience: {years} years total ({start_year}\u2013present)\n"
        f"Current: {current_title} at {current_company}\n"
        f"Key skills: {skills_str}\n"
        f"Strong domains: {domains_str}\n"
        f"Top achievements:\n{achievements_str}\n"
        f"Target: Senior/Lead/Staff roles, visa sponsorship required outside India, willing to relocate"
    )


def _build_profile_embedding_text(profile: MasterProfile) -> str:
    """Return a short, keyword-focused string used for embedding the profile.

    Concatenates title variants, all flat skill names, and the first 300 chars
    of the DEFAULT summary.  Kept compact so the embedding captures the
    candidate's core competencies rather than prose.
    """
    title_variants = list(profile.titles.values())

    all_skills: list[str] = []
    for sb in profile.skills:
        all_skills.extend(sb.items)

    default_summary = profile.get_summary("DEFAULT")
    summary_text = (default_summary.text if default_summary else "")[:300]

    return " ".join(filter(None, [" ".join(title_variants), " ".join(all_skills), summary_text]))


@lru_cache(maxsize=4)
def _cached_profile_embedding(content_hash: str, emb_text: str) -> list[float]:
    """Inner cache keyed on (content_hash, emb_text) so the vector is computed once per session."""
    return embed_text(emb_text)


def _get_profile_embedding(profile: MasterProfile) -> list[float]:
    """Return the profile embedding, computing it at most once per process.

    The cache key is ``profile.meta["content_hash"]`` so a regenerated profile
    with the same hash reuses the cached vector.
    """
    content_hash = str(profile.meta.get("content_hash", ""))
    emb_text = _build_profile_embedding_text(profile)
    return _cached_profile_embedding(content_hash, emb_text)


# ---------------------------------------------------------------------------
# Job context builder
# ---------------------------------------------------------------------------


def _build_job_context_for_scoring(job: Job, session) -> str:  # type: ignore[no-untyped-def]
    """Format a structured job context block for the LLM scoring prompt.

    Looks up the company name from the ``Company`` table via *session*.
    """
    company_name = ""
    if job.company_id is not None:
        company = session.get(Company, job.company_id)
        if company:
            company_name = company.name

    required_skills = "none listed"
    if job.required_skills_json:
        try:
            skills_list: list[str] = json.loads(job.required_skills_json)
            required_skills = ", ".join(skills_list) if skills_list else "none listed"
        except (json.JSONDecodeError, TypeError):
            required_skills = job.required_skills_json

    description_excerpt = (job.description_md or "")[:2000]

    return (
        f"Title: {job.title}\n"
        f"Company: {company_name}\n"
        f"Location: {job.city or 'Unknown'}, {job.country or 'Unknown'} | {job.remote_type or 'unknown'}\n"
        f"Visa sponsorship: {job.visa_sponsorship or 'unknown'}\n"
        f"Seniority: {job.seniority or 'unknown'}\n"
        f"Years required: {job.years_required or 'not stated'}\n"
        f"Required skills: {required_skills}\n"
        f"Description excerpt:\n{description_excerpt}"
    )


# ---------------------------------------------------------------------------
# LLM scoring helpers
# ---------------------------------------------------------------------------


def _run_llm_scoring(profile: MasterProfile, job_context: str) -> ScoringResult | None:
    """Pure LLM scoring call — no DB access, safe to run inside a thread pool.

    Loads the prompt template, injects candidate + job context, calls
    ``structured_complete``, and returns the validated ``ScoringResult``.
    Returns ``None`` on any exception (the caller decides how to handle it).
    """
    prompt_template = _load_score_prompt()
    candidate_text = _build_candidate_profile_text(profile)

    prompt = prompt_template.replace("{{CANDIDATE_PROFILE}}", candidate_text)
    prompt = prompt.replace("{{JOB_CONTEXT}}", job_context)

    return structured_complete(
        task="scoring",
        messages=[{"role": "user", "content": prompt}],
        model=ScoringResult,
    )


def score_one(job: Job, profile: MasterProfile, session) -> ScoringResult | None:  # type: ignore[no-untyped-def]
    """Score a single job against the candidate profile using the LLM rubric.

    Builds the job context from *session*, then calls the LLM.
    Catches all exceptions and returns ``None`` on failure so the caller can
    mark the job as rejected without crashing the pipeline.

    Args:
        job:     The ``Job`` row to score.
        profile: The loaded ``MasterProfile``.
        session: An open SQLModel ``Session`` used only to look up the company name.

    Returns:
        A validated ``ScoringResult``, or ``None`` if the LLM call fails.
    """
    try:
        job_context = _build_job_context_for_scoring(job, session)
        return _run_llm_scoring(profile, job_context)
    except Exception as exc:
        log.warning("score.one.failed", job_id=job.id, title=job.title, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------


async def _run_scoring_async(
    limit: int,
    embed_threshold: float,
) -> tuple[int, int]:
    """Async implementation of :func:`run_scoring`.

    Separated so it can be awaited directly from other async contexts without
    re-entering an event loop.
    """
    profile = load_profile()
    profile_emb = _get_profile_embedding(profile)
    min_score: int = int(load_yaml("guardrails").get("min_score", 75))
    model_name: str = get_model_config("scoring")["model"]

    # ── 1. Load all ready_to_score jobs ──────────────────────────────────────
    with get_session() as session:
        all_jobs: list[Job] = list(
            session.exec(
                select(Job)
                .where(col(Job.status) == "ready_to_score")
                .order_by(col(Job.first_seen_at).desc())
                .limit(limit)
            ).all()
        )

    if not all_jobs:
        log.info("score.done", queued=0, rejected=0, embed_filtered=0)
        return 0, 0

    # ── 2. Embedding funnel + build job contexts (single session) ─────────────
    # Store only primitive data to avoid detached-instance issues after the
    # session closes: (job_id, job_context_str) for LLM candidates,
    # (job_id, similarity) for embedding rejects.
    to_score: list[tuple[int, str]] = []        # (job_id, pre-built context)
    embed_rejected: list[tuple[int, float]] = []  # (job_id, cosine_sim)
    embed_filtered = 0

    with get_session() as session:
        for job in all_jobs:
            job_emb_text = (
                f"{job.title} "
                f"{job.required_skills_json or ''} "
                f"{(job.description_md or '')[:400]}"
            )
            sim = cosine_similarity(embed_text(job_emb_text), profile_emb)

            if sim < embed_threshold:
                embed_rejected.append((job.id, sim))  # type: ignore[arg-type]
                embed_filtered += 1
            else:
                job_context = _build_job_context_for_scoring(job, session)
                to_score.append((job.id, job_context))  # type: ignore[arg-type]

        # Persist embedding rejections immediately
        for job_id, sim in embed_rejected:
            db_job = session.get(Job, job_id)
            if db_job is None:
                continue
            db_job.status = "rejected_low_score"
            rejection_score = Score(
                job_id=job_id,
                model="embedding",
                prompt_version="v1",
                score=0,
                breakdown_json=json.dumps({
                    "rejected_by": "embedding",
                    "similarity": round(sim, 3),
                }),
            )
            session.add(rejection_score)
            session.add(db_job)

        session.commit()

    log.info(
        "score.embed_filter",
        total=len(all_jobs),
        passed=len(to_score),
        filtered=embed_filtered,
        threshold=embed_threshold,
    )

    # ── 3. Async LLM scoring with bounded concurrency ─────────────────────────
    sem = asyncio.Semaphore(5)

    async def _do_score(job_id: int, job_context: str) -> tuple[int, ScoringResult | None]:
        async with sem:
            try:
                result = await asyncio.to_thread(_run_llm_scoring, profile, job_context)
                return job_id, result
            except Exception as exc:
                log.warning("score.async.failed", job_id=job_id, error=str(exc))
                return job_id, None

    gathered: list[tuple[int, ScoringResult | None] | BaseException] = await asyncio.gather(
        *[_do_score(jid, ctx) for jid, ctx in to_score],
        return_exceptions=True,
    )

    # ── 4. Persist LLM results in batches of 20 ──────────────────────────────
    queued = 0
    rejected = 0
    batch_count = 0

    with get_session() as session:
        for item in gathered:
            if isinstance(item, BaseException):
                # asyncio.gather exception wrapper (shouldn't happen – _do_score catches)
                log.warning("score.gather.exception", error=str(item))
                continue

            job_id, result = item
            db_job = session.get(Job, job_id)
            if db_job is None:
                log.warning("score.job_missing", job_id=job_id)
                continue

            if result is None:
                # LLM call failed (already logged in _do_score)
                db_job.status = "rejected_low_score"
                rejected += 1
            else:
                score_row = Score(
                    job_id=job_id,
                    model=model_name,
                    prompt_version="v1",
                    score=result.score,
                    breakdown_json=json.dumps(result.model_dump()),
                    pitch_angle=result.pitch_angle,
                )
                session.add(score_row)
                db_job.score = result.score

                if result.score >= min_score:
                    db_job.status = "queued"
                    queued += 1
                else:
                    db_job.status = "rejected_low_score"
                    rejected += 1

            session.add(db_job)
            batch_count += 1

            if batch_count % 20 == 0:
                session.commit()

        session.commit()  # flush any remaining rows

    log.info("score.done", queued=queued, rejected=rejected, embed_filtered=embed_filtered)
    return queued, rejected


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_scoring(
    limit: int = 200,
    embed_threshold: float = 0.35,
) -> tuple[int, int]:
    """Score all jobs in ``status='ready_to_score'`` and update their status.

    Pipeline:
    1. Load up to *limit* ready-to-score jobs ordered by ``first_seen_at`` desc.
    2. Embed each job title + skills + description excerpt; filter against the
       profile embedding using cosine similarity.  Jobs below *embed_threshold*
       are marked ``rejected_low_score`` immediately (cheap, local, no LLM cost).
    3. For each surviving job, call the LLM rubric via
       ``asyncio.to_thread`` with a ``Semaphore(5)`` to bound concurrency.
    4. Persist ``Score`` rows and update ``job.status``:
       - ``score >= min_score`` (from ``config/guardrails.yaml``) → ``"queued"``
       - ``score <  min_score`` or LLM failure               → ``"rejected_low_score"``
    5. Commits are issued every 20 rows to bound transaction size.

    Args:
        limit:           Maximum number of jobs to process in one run.
        embed_threshold: Cosine-similarity floor below which the LLM is not called.

    Returns:
        ``(queued_count, rejected_count)``

    Note:
        Uses ``asyncio.run()`` internally.  If called from an already-running
        event loop, await ``_run_scoring_async(limit, embed_threshold)`` instead.
    """
    return asyncio.run(_run_scoring_async(limit, embed_threshold))


def get_top_jobs(limit: int = 50) -> list[tuple[Job, Score]]:
    """Return top-scoring jobs for CLI display.

    Queries jobs with ``status='queued'`` or a non-null ``score``, ordered by
    ``score`` descending, and pairs each with its latest ``Score`` row.
    Jobs that have no ``Score`` row (edge-case during development) are omitted.

    Args:
        limit: Maximum number of job+score pairs to return.

    Returns:
        A list of ``(Job, Score)`` tuples ordered by score descending.
    """
    with get_session() as session:
        jobs: list[Job] = list(
            session.exec(
                select(Job)
                .where(
                    or_(
                        col(Job.status) == "queued",
                        col(Job.score).isnot(None),
                    )
                )
                .order_by(col(Job.score).desc())
                .limit(limit)
            ).all()
        )

        result: list[tuple[Job, Score]] = []
        for job in jobs:
            # Fetch the latest Score row for this job (there may be more than
            # one if the job was re-scored across pipeline runs).
            score = session.exec(
                select(Score)
                .where(col(Score.job_id) == job.id)
                .order_by(col(Score.created_at).desc())
            ).first()
            if score is not None:
                result.append((job, score))

        return result
