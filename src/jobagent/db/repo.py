"""Repository layer – thin CRUD helpers over SQLModel / SQLite.

Each class groups static methods for one domain entity.  Methods are kept
small: they open a session, do exactly one logical operation, and close.

JSON serialisation for payload columns (``*_json`` fields) is the caller's
responsibility; repos store and retrieve raw strings.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlmodel import select

from .engine import get_session
from .models import (
    Application,
    AppCheckpoint,
    AppEvent,
    AppStep,
    Job,
    LlmCall,
    Run,
)


# ---------------------------------------------------------------------------
# JobRepo
# ---------------------------------------------------------------------------


class JobRepo:
    @staticmethod
    def upsert(job_data: dict) -> int:  # type: ignore[type-arg]
        """Insert or update a job keyed on (source, source_job_id).

        Returns the row ``id``.
        """
        with get_session() as session:
            stmt = select(Job).where(
                Job.source == job_data["source"],
                Job.source_job_id == job_data["source_job_id"],
            )
            job = session.exec(stmt).first()
            if job is None:
                job = Job(**job_data)
                session.add(job)
            else:
                for key, value in job_data.items():
                    setattr(job, key, value)
                session.add(job)
            session.commit()
            session.refresh(job)
            assert job.id is not None  # satisfy type-checker
            return job.id

    @staticmethod
    def get(job_id: int) -> Job | None:
        with get_session() as session:
            return session.get(Job, job_id)

    @staticmethod
    def get_by_status(status: str, limit: int = 100) -> list[Job]:
        with get_session() as session:
            stmt = select(Job).where(Job.status == status).limit(limit)
            return list(session.exec(stmt).all())

    @staticmethod
    def update_status(job_id: int, status: str) -> None:
        with get_session() as session:
            job = session.get(Job, job_id)
            if job is not None:
                job.status = status
                session.add(job)
                session.commit()


# ---------------------------------------------------------------------------
# ApplicationRepo
# ---------------------------------------------------------------------------


class ApplicationRepo:
    @staticmethod
    def create(job_id: int, **kwargs: Any) -> Application:
        """Create and persist a new Application row; returns the new instance."""
        with get_session() as session:
            app = Application(job_id=job_id, **kwargs)
            session.add(app)
            session.commit()
            session.refresh(app)
            return app

    @staticmethod
    def get(app_id: int) -> Application | None:
        with get_session() as session:
            return session.get(Application, app_id)

    @staticmethod
    def get_by_status(status: str, limit: int = 50) -> list[Application]:
        with get_session() as session:
            stmt = (
                select(Application)
                .where(Application.status == status)
                .limit(limit)
            )
            return list(session.exec(stmt).all())

    @staticmethod
    def transition(app_id: int, to_status: str, note: str = "") -> None:
        """Atomically update ``status`` and append an ``AppEvent`` row."""
        with get_session() as session:
            app = session.get(Application, app_id)
            if app is None:
                raise ValueError(f"Application {app_id} not found")
            from_status = app.status
            app.status = to_status
            app.updated_at = datetime.now(timezone.utc)
            event = AppEvent(
                application_id=app_id,
                from_status=from_status,
                to_status=to_status,
                note=note or None,
            )
            session.add(app)
            session.add(event)
            session.commit()


# ---------------------------------------------------------------------------
# CheckpointRepo
# ---------------------------------------------------------------------------


class CheckpointRepo:
    @staticmethod
    def upsert(cp: Any) -> None:
        """Persist a checkpoint.

        ``cp`` is expected to be a ``jobagent.observability.Checkpoint``
        dataclass/model with at minimum these attributes:

        - ``application_id: int``
        - ``page_index: int``
        - ``page_signature: str``
        - ``account_id: int | None``  (optional)
        - ``filled_values: dict | None``  (optional; will be JSON-encoded)
        """
        # Lazy import – observability imports from db, so we avoid circularity
        try:
            from jobagent.observability import Checkpoint  # noqa: F401
        except ImportError:
            pass  # type stubs not yet available; proceed duck-typed

        filled_json: str | None = None
        filled = getattr(cp, "filled_values", None)
        if filled is not None:
            filled_json = json.dumps(filled)

        with get_session() as session:
            existing = session.get(AppCheckpoint, cp.application_id)
            if existing is None:
                record = AppCheckpoint(
                    application_id=cp.application_id,
                    page_index=cp.page_index,
                    page_signature=cp.page_signature,
                    account_id=getattr(cp, "account_id", None),
                    filled_values_json=filled_json,
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(record)
            else:
                existing.page_index = cp.page_index
                existing.page_signature = cp.page_signature
                existing.account_id = getattr(cp, "account_id", None)
                existing.filled_values_json = filled_json
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)
            session.commit()

    @staticmethod
    def get(application_id: int) -> AppCheckpoint | None:
        """Return the raw ``AppCheckpoint`` DB row, or ``None``."""
        with get_session() as session:
            return session.get(AppCheckpoint, application_id)


# ---------------------------------------------------------------------------
# StepRepo
# ---------------------------------------------------------------------------


class StepRepo:
    @staticmethod
    def insert(step: Any) -> None:
        """Persist a step record.

        ``step`` is expected to be a ``jobagent.observability.StepRecord``
        dataclass/model whose attributes map 1-to-1 with ``AppStep`` fields.
        Unknown/missing attributes fall back to column defaults.
        """
        # Lazy import – avoids circular dependency
        try:
            from jobagent.observability import StepRecord  # noqa: F401
        except ImportError:
            pass

        with get_session() as session:
            record = AppStep(
                application_id=step.application_id,
                ts=getattr(step, "ts", datetime.now(timezone.utc)),
                page_type=getattr(step, "page_type", None),
                layer=getattr(step, "layer", None),
                action=getattr(step, "action", None),
                target=getattr(step, "target", None),
                value_masked=getattr(step, "value_masked", None),
                reasoning=getattr(step, "reasoning", None),
                screenshot_path=getattr(step, "screenshot_path", None),
                tokens=getattr(step, "tokens", 0),
                cost_usd=getattr(step, "cost_usd", 0.0),
                ok=getattr(step, "ok", True),
                error=getattr(step, "error", None),
            )
            session.add(record)
            session.commit()


# ---------------------------------------------------------------------------
# RunRepo
# ---------------------------------------------------------------------------


class RunRepo:
    @staticmethod
    def start(stage: str) -> Run:
        """Create a ``Run`` row in status ``"running"`` and return it."""
        with get_session() as session:
            run = Run(stage=stage)
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

    @staticmethod
    def finish(run_id: int, items_in: int, items_out: int, errors: int) -> None:
        """Mark a run as ``"done"`` and record final counters."""
        with get_session() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.ended_at = datetime.now(timezone.utc)
                run.items_in = items_in
                run.items_out = items_out
                run.errors = errors
                run.status = "done"
                session.add(run)
                session.commit()

    @staticmethod
    def fail(run_id: int, error: str) -> None:
        """Mark a run as ``"failed"`` and store the error message in ``notes``."""
        with get_session() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.ended_at = datetime.now(timezone.utc)
                run.status = "failed"
                run.notes = error
                session.add(run)
                session.commit()


# ---------------------------------------------------------------------------
# LlmCallRepo
# ---------------------------------------------------------------------------


class LlmCallRepo:
    @staticmethod
    def insert(call: dict) -> None:  # type: ignore[type-arg]
        """Persist a ``LlmCall`` row.  Keys must match ``LlmCall`` field names."""
        with get_session() as session:
            record = LlmCall(**call)
            session.add(record)
            session.commit()

    @staticmethod
    def today_cost() -> float:
        """Return total ``cost_usd`` for all LLM calls on today's UTC date."""
        today_str = date.today().isoformat()
        with get_session() as session:
            stmt = select(func.sum(LlmCall.cost_usd)).where(
                func.date(LlmCall.created_at) == today_str
            )
            # Use the underlying SQLAlchemy execute for scalar aggregate queries
            result = session.execute(stmt).scalar()
            return float(result) if result is not None else 0.0
