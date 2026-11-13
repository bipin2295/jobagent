"""Tailoring orchestrator: build a full application package for each queued job.

Pipeline for one job:
  plan → render resume → cover letter → answer pre-fill → persist Application row

``run_tailoring`` is the main async entry point; it processes up to *limit*
queued jobs with a concurrency cap of 3.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml  # noqa: F401 – available to callers that import from this module
from sqlmodel import Session, select

from jobagent.config import load_yaml, settings
from jobagent.db.engine import get_session
from jobagent.db.models import AppEvent, Application, Company, Job, Score  # noqa: F401
from jobagent.db.repo import ApplicationRepo  # noqa: F401 – re-exported for callers
from jobagent.profile.loader import load_profile
from jobagent.profile.schema import MasterProfile
from jobagent.tailor.answers import build_answer_map
from jobagent.tailor.cover_letter import generate_cover_letter
from jobagent.tailor.planner import PlanResult, plan
from jobagent.tailor.render import build_rendercv_yaml, render_resume, verify_pdf  # noqa: F401

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_queued_jobs(session: Session, limit: int) -> list[tuple[Job, Score | None]]:
    """Return up to *limit* queued jobs ordered by score descending.

    For each job also fetches the latest ``Score`` row (or ``None`` if the job
    has never been scored).

    Parameters
    ----------
    session:
        An open SQLModel ``Session``.
    limit:
        Maximum number of jobs to return.
    """
    from sqlalchemy import case  # noqa: PLC0415
    # Prioritise jobs with a known, handleable ATS type so the apply step can
    # actually submit them; unknown-ATS jobs (aggregator links) go last.
    ats_priority = case(
        (Job.ats_type.in_(["greenhouse", "lever", "ashby", "smartrecruiters"]), 0),
        else_=1,
    )
    # Exclude jobs that already have an active tailored or submitted application
    # so the tailor stage never creates duplicate packages on repeated runs.
    from jobagent.db.models import Application  # noqa: PLC0415
    from jobagent.apply.guardrails import load_guardrails  # noqa: PLC0415
    already_handled = (
        select(Application.job_id)
        .where(Application.status.in_(["tailored", "submitted"]))  # type: ignore[attr-defined]
    )
    stmt = (
        select(Job)
        .where(Job.status == "queued")
        .where(Job.id.notin_(already_handled))  # type: ignore[attr-defined]
        .order_by(ats_priority, Job.score.desc())  # type: ignore[arg-type]
        .limit(limit * 5)  # over-fetch so we can filter by active_ats below
    )
    all_jobs = list(session.exec(stmt).all())

    # Respect active_ats focus — if set, only tailor jobs with matching ATS
    guardrails = load_guardrails()
    if guardrails.active_ats:
        all_jobs = [j for j in all_jobs if j.ats_type in guardrails.active_ats]
    all_jobs = all_jobs[:limit]  # re-apply original limit

    # Re-build result with score rows for the filtered set
    result2: list[tuple[Job, Score | None]] = []
    for job in all_jobs:
        score_stmt2 = (
            select(Score)
            .where(Score.job_id == job.id)
            .order_by(Score.created_at.desc())
            .limit(1)
        )
        score_row = session.exec(score_stmt2).first()
        result2.append((job, score_row))
    return result2


def _resolve_company_name(job: Job, session: Session) -> str:
    """Return the human-readable company name for *job*.

    Falls back to ``job.source`` (e.g. ``"linkedin"``) when the Company row is
    missing or ``job.company_id`` is not set.
    """
    if job.company_id is not None:
        company: Company | None = session.get(Company, job.company_id)
        if company is not None:
            return company.name
    return job.source


# ---------------------------------------------------------------------------
# tailor_one
# ---------------------------------------------------------------------------


async def tailor_one(
    job: Job,
    score_row: Score | None,
    profile: MasterProfile,
    session: Session,
) -> dict[str, Any]:
    """Build a complete application package for one job.

    Runs the full tailoring pipeline:

    a. **Plan** – call ``plan()`` in a thread to get a ``PlanResult``.
    b. **Resume** – render via RenderCV; ATS-verify the PDF.
    c. **Cover letter** – generate via ``generate_cover_letter()``.
    d. **Answers** – pre-fill common form questions via ``build_answer_map()``.
    e. **Persist** – insert an ``Application`` row (in a fresh session).

    Parameters
    ----------
    job:        The ``Job`` row to tailor for.
    score_row:  The latest ``Score`` for the job, or ``None``.
    profile:    The loaded ``MasterProfile``.
    session:    An open ``Session`` used for read queries within this function.

    Returns
    -------
    dict[str, Any]
        ``{app_id, job_id, pdf_path, cl_path, answers_path, plan_path}``

    Raises
    ------
    ValueError
        If ``job.id`` is ``None`` (unsaved job row).
    """
    if job.id is None:
        raise ValueError("Job has no database ID – cannot build output directory.")

    output_dir: Path = settings.data_dir / "applications" / str(job.id)
    output_dir.mkdir(parents=True, exist_ok=True)

    _log = log.bind(job_id=job.id, job_title=job.title)

    # ── a. Plan ───────────────────────────────────────────────────────────────
    _log.info("tailor.planning")
    plan_result: PlanResult = await asyncio.to_thread(
        plan, job, score_row, profile, session
    )
    plan_path = output_dir / "plan.json"
    plan_path.write_text(json.dumps(plan_result.model_dump(), indent=2), encoding="utf-8")

    # ── b. Resume ─────────────────────────────────────────────────────────────
    _log.info("tailor.rendering_resume")
    pdf_path: Path | None = None
    try:
        pdf_path = render_resume(plan_result, profile, output_dir)
        passed, issues = verify_pdf(pdf_path, profile.contact.name)
        if not passed:
            _log.warning("tailor.ats_check_failed", issues=issues, job_id=job.id)
        else:
            _log.info("tailor.ats_check_passed", pdf=str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        _log.error("tailor.render_failed", error=str(exc), job_id=job.id)
        pdf_path = None

    # ── c. Cover letter ───────────────────────────────────────────────────────
    _log.info("tailor.generating_cover_letter")
    company_name = _resolve_company_name(job, session)
    _log.debug("tailor.company_resolved", company=company_name)
    cl_text: str = await asyncio.to_thread(
        generate_cover_letter, job, plan_result, profile, session
    )
    cl_path = output_dir / "cover_letter.txt"
    cl_path.write_text(cl_text, encoding="utf-8")

    # ── d. Answers ────────────────────────────────────────────────────────────
    _log.info("tailor.building_answers")
    answers: dict[str, Any] = build_answer_map(job, profile)
    answers_path = output_dir / "answers.json"
    answers_path.write_text(json.dumps(answers, indent=2), encoding="utf-8")

    # ── e. Persist Application row ────────────────────────────────────────────
    _log.info("tailor.persisting")
    shadow_mode: bool = bool(load_yaml("guardrails").get("shadow_mode", True))

    with get_session() as s:
        app = Application(
            job_id=job.id,
            status="tailored",
            resume_path=str(pdf_path) if pdf_path is not None else None,
            cover_letter_path=str(cl_path),
            answers_json=json.dumps(answers),
            plan_json=plan_result.model_dump_json(),
            shadow_mode=shadow_mode,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(app)
        s.commit()
        s.refresh(app)

    _log.info(
        "tailor.done_one",
        app_id=app.id,
        pdf=str(pdf_path),
        shadow_mode=shadow_mode,
    )

    return {
        "app_id": app.id,
        "job_id": job.id,
        "pdf_path": str(pdf_path) if pdf_path is not None else None,
        "cl_path": str(cl_path),
        "answers_path": str(answers_path),
        "plan_path": str(plan_path),
    }


# ---------------------------------------------------------------------------
# run_tailoring
# ---------------------------------------------------------------------------


async def run_tailoring(limit: int = 20) -> tuple[int, int]:
    """Main entry point for the tailoring pipeline.

    Loads the ``MasterProfile``, fetches up to *limit* queued jobs ordered by
    score, then processes them concurrently (max 3 in-flight at a time).

    Parameters
    ----------
    limit:
        Maximum number of jobs to process in a single run.

    Returns
    -------
    tuple[int, int]
        ``(tailored, failed)`` counts.
    """
    profile = load_profile()

    with get_session() as session:
        jobs_scores = _get_queued_jobs(session, limit)

    if not jobs_scores:
        log.info("tailor.no_queued_jobs", limit=limit)
        return (0, 0)

    log.info("tailor.starting", total=len(jobs_scores), limit=limit)

    sem = asyncio.Semaphore(3)

    async def _process(job: Job, score: Score | None) -> bool:
        async with sem:
            try:
                with get_session() as s:
                    await tailor_one(job, score, profile, s)
                return True
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "tailor.job_failed",
                    job_id=job.id,
                    job_title=job.title,
                    error=str(exc),
                )
                return False

    results: list[bool] = list(
        await asyncio.gather(*[_process(j, sc) for j, sc in jobs_scores])
    )

    tailored = sum(results)
    failed = len(results) - tailored

    log.info("tailor.done", tailored=tailored, failed=failed, total=len(jobs_scores))
    return (tailored, failed)


# ---------------------------------------------------------------------------
# get_tailored_packages
# ---------------------------------------------------------------------------


def get_tailored_packages(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent Application packages with their scores for CLI display.

    Queries Applications with ``status='tailored'``, joins with their Job
    and latest Score rows, and resolves the company name.

    Parameters
    ----------
    limit:
        Maximum number of packages to return (most-recent first).

    Returns
    -------
    list[dict[str, Any]]
        Each dict contains:
        ``{app_id, job_id, job_title, company, score, pdf_path, created_at,
           pitch_angle}``
    """
    with get_session() as session:
        apps = list(
            session.exec(
                select(Application)
                .where(Application.status == "tailored")
                .order_by(Application.created_at.desc())  # type: ignore[arg-type]
                .limit(limit)
            ).all()
        )

        result: list[dict[str, Any]] = []

        for app in apps:
            job: Job | None = session.get(Job, app.job_id)
            if job is None:
                continue

            # Latest score for this job
            score_row: Score | None = session.exec(
                select(Score)
                .where(Score.job_id == job.id)
                .order_by(Score.created_at.desc())
                .limit(1)
            ).first()

            # Company name: Company table → job.source fallback
            company_name: str = job.source
            if job.company_id is not None:
                company: Company | None = session.get(Company, job.company_id)
                if company is not None:
                    company_name = company.name

            # Pitch angle: prefer Score row, then fall back to plan_json
            pitch_angle: str | None = None
            if score_row is not None:
                pitch_angle = score_row.pitch_angle
            if pitch_angle is None and app.plan_json:
                try:
                    plan_data: dict[str, Any] = json.loads(app.plan_json)
                    pitch_angle = plan_data.get("pitch_angle") or None
                except (json.JSONDecodeError, TypeError):
                    pass

            result.append(
                {
                    "app_id": app.id,
                    "job_id": job.id,
                    "job_title": job.title,
                    "company": company_name,
                    "score": score_row.score if score_row is not None else None,
                    "pdf_path": app.resume_path,
                    "created_at": app.created_at,
                    "pitch_angle": pitch_angle,
                }
            )

        return result
