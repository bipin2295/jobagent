"""SQLite engine setup with WAL mode and FK enforcement."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        from jobagent.config import settings  # lazy to avoid circular import

        db_path = settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        _engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )

        @event.listens_for(_engine, "connect")
        def _set_pragmas(dbapi_conn, _conn_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


def create_tables() -> None:
    """Create all tables (idempotent – safe to call on every startup)."""
    # Import all models so SQLModel knows about them before create_all()
    from jobagent.db import models as _  # noqa: F401

    SQLModel.metadata.create_all(get_engine())
