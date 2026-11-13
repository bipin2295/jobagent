"""Application-level checkpoints persisted to SQLite.

A checkpoint captures the state *after* each completed ATS page so that a
resume can log in and continue rather than restarting from sign-up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Checkpoint:
    application_id: int
    page_index: int
    page_signature: str          # hash of form labels – identifies the page
    account_id: int | None       # FK into accounts table
    filled_values: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "application_id": self.application_id,
                "page_index": self.page_index,
                "page_signature": self.page_signature,
                "account_id": self.account_id,
                "filled_values": self.filled_values,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "Checkpoint":
        d = json.loads(text)
        return cls(**d)


# Checkpoint storage is handled by db.repo.CheckpointRepo (imported lazily to
# avoid circular imports at module load time).
def save_checkpoint(cp: Checkpoint) -> None:
    """Persist *cp* to the DB (lazy import to avoid circular dependency)."""
    from jobagent.db.repo import CheckpointRepo  # noqa: PLC0415
    CheckpointRepo.upsert(cp)


def load_checkpoint(application_id: int) -> Checkpoint | None:
    """Return the latest checkpoint for *application_id*, or None."""
    from jobagent.db.repo import CheckpointRepo  # noqa: PLC0415
    return CheckpointRepo.get(application_id)
