"""LLM-based enrichment: extract visa, seniority, skills, and language requirements from JDs.

Each job is processed once (idempotent: jobs with status != 'discovered' are skipped).
Enriched fields are stored back on the Job row and status is advanced to 'enriched'.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog
from pydantic import BaseModel, Field
from sqlmodel import select

from jobagent.db.engine import get_session
from jobagent.db.models import Company, Job
from jobagent.llm.client import ConfigError
from jobagent.llm.structured import structured_complete

log = structlog.get_logger(__name__)

# prompts/enrich_jd.md sits at the project root, four parents above this file:
#   src/jobagent/pipeline/enrich.py
#   parents[0] = src/jobagent/pipeline
#   parents[1] = src/jobagent
#   parents[2] = src
#   parents[3] = <project root>
_PROMPT_PATH = Path(__file__).parents[3] / "prompts" / "enrich_jd.md"
_PROMPT_CACHE: str | None = None


# ---------------------------------------------------------------------------
# Pydantic result model
# ---------------------------------------------------------------------------


class EnrichResult(BaseModel):
    visa_sponsorship: str = "unknown"       # "yes" | "no" | "unknown"
    relocation_support: str = "unknown"     # "yes" | "no" | "unknown"
    seniority: str = "unknown"              # "junior"|"mid"|"senior"|"lead"|"staff"|"principal"|"unknown"
    years_required_min: int | None = None   # minimum stated years, null if absent
    required_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    language_requirements: list[str] = Field(default_factory=list)
    employment_type: str = "unknown"        # "full_time"|"contract"|"part_time"|"unknown"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_prompt() -> str:
    """Return the prompt template, reading from disk on first call then caching."""
    global _PROMPT_CACHE
    if _PROMPT_CACHE is None:
        _PROMPT_CACHE = _PROMPT_PATH.read_text(encoding="utf-8")
    return _PROMPT_CACHE


def _build_job_context(job: Job, session) -> str:  # type: ignore[no-untyped-def]
    """Construct the text block injected into {{JOB_CONTEXT}} in the prompt.

    Format::

        Title: <title>
        Company: <name>              (omitted when company_id is None)
        Location: <country>/<city>/<remote_type>  (omitted when all parts absent)

        <description_md, truncated to 3 000 chars>
    """
    company_name: str = ""
    if job.company_id is not None:
        company = session.get(Company, job.company_id)
        if company is not None:
            company_name = company.name

    lines: list[str] = [f"Title: {job.title}"]
    if company_name:
        lines.append(f"Company: {company_name}")

    location_parts = [p for p in [job.country, job.city, job.remote_type] if p]
    if location_parts:
        lines.append(f"Location: {'/'.join(location_parts)}")

    description = (job.description_md or "")[:3000]
    if description:
        lines.append("")          # blank separator
        lines.append(description)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core pipeline functions
# ---------------------------------------------------------------------------


def enrich_one(job: Job, session) -> EnrichResult | None:  # type: ignore[no-untyped-def]
    """Call the LLM and return structured extraction for *job*, or None on failure.

    Failures are always swallowed so a single bad job never aborts the batch.
    ``ConfigError`` (missing API key) is logged at WARNING — the remaining jobs
    would fail the same way, so the caller should short-circuit if None is
    returned with a ConfigError cause.
    """
    prompt_text = _load_prompt()
    context = _build_job_context(job, session)
    prompt = prompt_text.replace("{{JOB_CONTEXT}}", context)
    messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]

    try:
        return structured_complete(
            task="jd_extraction",
            messages=messages,
            model=EnrichResult,
        )
    except ConfigError:
        log.warning("enrich.config_error", job_id=job.id)
        return None
    except Exception as exc:
        log.error("enrich.llm_error", job_id=job.id, error=str(exc))
        return None


def apply_enrichment(job: Job, result: EnrichResult, session) -> None:  # type: ignore[no-untyped-def]
    """Write *result* back onto *job* and advance its status to 'enriched'.

    Also sets the boolean convenience flags:
      visa_ok=True       when visa_sponsorship == "yes"
      relocation_ok=True when relocation_support == "yes"

    Does NOT commit — the caller is responsible for committing after a full batch.
    """
    job.visa_sponsorship = result.visa_sponsorship
    job.relocation_support = result.relocation_support
    job.seniority = result.seniority
    job.years_required = result.years_required_min
    job.required_skills_json = json.dumps(result.required_skills)
    # Boolean convenience flags — set once, never cleared
    if result.visa_sponsorship == "yes":
        job.visa_ok = True
    if result.relocation_support == "yes":
        job.relocation_ok = True
    job.status = "enriched"
    session.add(job)


# ---------------------------------------------------------------------------
# Async concurrency helpers
# ---------------------------------------------------------------------------


async def _enrich_task(
    job_id: int,
    sem: asyncio.Semaphore,
) -> tuple[int, EnrichResult | None]:
    """Run one enrichment job under the shared semaphore, in a thread pool worker.

    Opens its own session so the synchronous SQLModel/OpenAI calls are
    safely isolated from the event-loop thread.
    """
    async with sem:
        def _work() -> EnrichResult | None:
            with get_session() as s:
                job = s.get(Job, job_id)
                if job is None:
                    log.warning("enrich.job_not_found", job_id=job_id)
                    return None
                return enrich_one(job, s)

        try:
            result = await asyncio.to_thread(_work)
        except Exception as exc:
            log.error("enrich.thread_error", job_id=job_id, error=str(exc))
            result = None

    return job_id, result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_enrichment(
    batch_size: int = 50,
    max_batches: int = 20,
) -> tuple[int, int]:
    """Enrich all 'discovered' jobs via the LLM.

    Jobs are processed in async batches of *batch_size* with up to 10 concurrent
    LLM calls (controlled by a Semaphore).  After each batch the results are
    written and committed in a single session.

    Returns:
        (enriched_count, skipped_count) where skipped = LLM returned None.
    """
    sem = asyncio.Semaphore(10)

    # ── Collect candidate job IDs (single read session) ──────────────────
    with get_session() as session:
        stmt = (
            select(Job)
            .where(Job.status == "discovered")
            .order_by(Job.first_seen_at.desc())
            .limit(batch_size * max_batches)
        )
        job_ids: list[int] = [
            j.id for j in session.exec(stmt).all() if j.id is not None
        ]

    total = len(job_ids)
    if total == 0:
        log.info("enrich.no_jobs_to_process")
        return 0, 0

    log.info("enrich.starting", total=total, batch_size=batch_size, max_batches=max_batches)

    enriched_count = 0
    skipped_count = 0

    for batch_start in range(0, total, batch_size):
        batch_ids = job_ids[batch_start : batch_start + batch_size]

        # ── LLM calls – run up to 10 concurrently ──────────────────────
        pairs: list[tuple[int, EnrichResult | None]] = await asyncio.gather(
            *[_enrich_task(jid, sem) for jid in batch_ids]
        )

        # ── Apply results + batch commit ────────────────────────────────
        with get_session() as session:
            for jid, result in pairs:
                db_job = session.get(Job, jid)
                if db_job is None:
                    skipped_count += 1
                    continue
                if result is not None:
                    apply_enrichment(db_job, result, session)
                    enriched_count += 1
                else:
                    # LLM returned None — run a quick title check.
                    # If the title doesn't match our keywords the job will never
                    # pass the pipeline anyway, so mark it filtered_out now so
                    # it is never re-queued for enrichment.
                    from jobagent.pipeline.filters import apply_filters  # noqa: PLC0415
                    from jobagent.config import load_yaml as _ly          # noqa: PLC0415
                    _scfg = _ly("search")
                    _fr = apply_filters(db_job, _scfg, {})
                    if not _fr.passed:
                        db_job.status = "filtered_out"
                        log.debug(
                            "enrich.skipped_filtered_out",
                            job_id=jid,
                            reason=_fr.reason,
                        )
                    skipped_count += 1
            session.commit()

        log.info(
            "enrich.batch_done",
            enriched=enriched_count,
            skipped=skipped_count,
            total=total,
            batch_start=batch_start,
        )

    log.info(
        "enrich.complete",
        enriched=enriched_count,
        skipped=skipped_count,
        total=total,
    )
    return enriched_count, skipped_count
