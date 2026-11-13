"""3-layer deduplication for incoming raw jobs.

Layer 1 — Exact match on (source, source_job_id):
    The same ATS job ID seen again from the same collector.

Layer 2 — Cross-source content-hash match:
    The same role posted on multiple boards (e.g. Greenhouse *and* LinkedIn).
    Identified by SHA-256 hash of (company_norm | title_norm | country).

Layer 3 — Embedding similarity (Phase 3):
    Deferred; not implemented here.

Public API
----------
is_duplicate(raw, session) → bool
upsert_job(raw, session)   → (Job, is_new: bool)
bulk_upsert(raw_jobs, session) → (new_count, updated_count)
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlmodel import Session, select

from jobagent.db.models import Company, Job, JobSource
from jobagent.discovery.base import RawJob
from jobagent.pipeline.ats_detect import extract_ats_slug
from jobagent.pipeline.normalise import detect_country, normalise, normalise_company

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_job_source(
    job_id: int,
    source: str,
    source_url: str | None,
    session: Session,
) -> None:
    """Add a ``JobSource`` row if one for *(job_id, source)* does not exist."""
    stmt = select(JobSource).where(
        JobSource.job_id == job_id,
        JobSource.source == source,
    )
    if session.exec(stmt).first() is None:
        session.add(JobSource(job_id=job_id, source=source, source_url=source_url))


def _find_or_create_company(raw: RawJob, session: Session) -> int | None:
    """Return the ``company.id`` for *raw*, creating a new row if needed.

    Lookup order:
    1. Existing ``Company`` with the same ``name_norm``.
    2. Create a new ``Company`` row seeded with whatever ATS info is available.

    Returns ``None`` only if the ``flush()`` fails to populate ``company.id``
    (should not happen with SQLite auto-increment).
    """
    # Derive a usable company name: prefer raw.company, fall back to the URL
    # host (e.g. "boards.greenhouse.io/acme" → "acme").  Some sources return
    # None for the company field; inserting that would violate the NOT NULL
    # constraint on company.name.
    company_name: str = raw.company or ""
    if not company_name.strip():
        try:
            from urllib.parse import urlparse  # noqa: PLC0415
            host = urlparse(raw.apply_url or raw.url).netloc
            # Strip common prefixes to get something human-readable
            company_name = (
                host.removeprefix("www.")
                    .removeprefix("jobs.")
                    .removeprefix("boards.")
                    .split(".")[0]
            ) or "unknown"
        except Exception:
            company_name = "unknown"

    name_norm = normalise_company(company_name)
    stmt = select(Company).where(Company.name_norm == name_norm)
    company = session.exec(stmt).first()

    if company is not None:
        # Opportunistically fill in ATS info if previously unknown
        if company.ats_type is None or company.ats_slug is None:
            ats_url = raw.apply_url or raw.url
            result = extract_ats_slug(ats_url)
            if result:
                ats_type, slug = result
                company.ats_type = company.ats_type or ats_type
                company.ats_slug = company.ats_slug or slug
                session.add(company)
        return company.id

    # New company – extract ATS info
    ats_url = raw.apply_url or raw.url
    ats_result = extract_ats_slug(ats_url)
    ats_type: str | None = None
    ats_slug: str | None = None
    if ats_result:
        ats_type, ats_slug = ats_result

    company = Company(
        name=company_name,
        name_norm=name_norm,
        ats_type=ats_type,
        ats_slug=ats_slug,
        country=detect_country(raw.location_raw, raw.country),
    )
    session.add(company)
    session.flush()
    return company.id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_duplicate(raw: RawJob, session: Session) -> bool:
    """Return ``True`` if *raw* already exists in the database.

    Layer 1: exact (source, source_job_id) match.
    Layer 2: cross-source content-hash match.
    """
    # Layer 1
    stmt = select(Job).where(
        Job.source == raw.source,
        Job.source_job_id == raw.source_job_id,
    )
    if session.exec(stmt).first() is not None:
        return True

    # Layer 2
    job = normalise(raw)
    stmt = select(Job).where(Job.content_hash == job.content_hash)
    return session.exec(stmt).first() is not None


def upsert_job(raw: RawJob, session: Session) -> tuple[Job, bool]:
    """Insert or update *raw* in the database.

    Returns ``(job, True)`` when a new row is created, or
    ``(existing_job, False)`` when an existing row is updated.

    On update: ``last_seen_at`` is refreshed and a new ``JobSource`` record is
    added if the *(job_id, source)* combination has not been seen before.
    On insert: the ``Company`` row is found or created, then the ``Job`` and
    its first ``JobSource`` row are written.

    Callers are responsible for committing the session.  This function only
    calls ``flush()`` to materialise auto-generated IDs within the same
    transaction.
    """
    now = datetime.now(timezone.utc)

    # ── Layer 1: exact match ─────────────────────────────────────────────
    stmt = select(Job).where(
        Job.source == raw.source,
        Job.source_job_id == raw.source_job_id,
    )
    existing = session.exec(stmt).first()
    if existing is not None:
        existing.last_seen_at = now
        session.add(existing)
        assert existing.id is not None
        _ensure_job_source(existing.id, raw.source, raw.url, session)
        session.flush()
        return (existing, False)

    # ── Layer 2: content-hash match ──────────────────────────────────────
    job = normalise(raw)
    stmt = select(Job).where(Job.content_hash == job.content_hash)
    existing = session.exec(stmt).first()
    if existing is not None:
        existing.last_seen_at = now

        # ATS upgrade: if the existing record came from an aggregator
        # (ats_type=None) but the incoming raw has a known ATS type
        # (e.g. greenhouse), upgrade the record so it can be applied to.
        raw_ats = job.ats_type  # set by normalise() from the apply_url
        if raw_ats and raw_ats != "unknown" and existing.ats_type in (None, "unknown"):
            existing.ats_type = raw_ats
            existing.apply_url = raw.apply_url or raw.url
            # If the job was filtered_out/rejected purely because ats_type=None
            # blocked application (not because of country/title), reset to allow
            # re-scoring with the now-known ATS.
            if existing.status in ("filtered_out", "rejected_low_score"):
                from jobagent.pipeline.filters import apply_filters  # noqa: PLC0415
                from jobagent.config import load_yaml as _ly          # noqa: PLC0415
                _result = apply_filters(existing, _ly("search"), {})
                if _result.passed:
                    existing.status = "discovered"  # re-enter pipeline with correct ATS

        session.add(existing)
        assert existing.id is not None
        _ensure_job_source(existing.id, raw.source, raw.url, session)
        session.flush()
        return (existing, False)

    # ── New job ──────────────────────────────────────────────────────────
    company_id = _find_or_create_company(raw, session)
    job.company_id = company_id
    job.first_seen_at = now
    job.last_seen_at = now
    session.add(job)
    session.flush()
    assert job.id is not None
    _ensure_job_source(job.id, raw.source, raw.url, session)
    session.flush()
    return (job, True)


def bulk_upsert(
    raw_jobs: list[RawJob],
    session: Session,
) -> tuple[int, int]:
    """Upsert *raw_jobs* in a single database transaction.

    Each job is processed inside its own ``SAVEPOINT`` so that a failure on
    one row does not roll back the rest of the batch.

    Returns ``(new_count, updated_count)``.  The session is committed before
    returning.
    """
    new_count = 0
    updated_count = 0

    for raw in raw_jobs:
        try:
            with session.begin_nested():   # SAVEPOINT per job
                _, is_new = upsert_job(raw, session)
            if is_new:
                new_count += 1
            else:
                updated_count += 1
        except Exception:
            log.exception(
                "bulk_upsert.job_failed",
                source=raw.source,
                source_job_id=raw.source_job_id,
                company=raw.company,
                title=raw.title,
            )

    session.commit()
    return (new_count, updated_count)
