"""Application step recorder.

Every agent action (observe → classify → act → verify) is recorded as an
AppStep row so the dashboard can show a full timeline with screenshots and
per-step cost.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StepRecord:
    application_id: int
    page_type: str
    layer: str           # "playbook" | "trajectory" | "agent"
    action: str          # e.g. "click", "fill", "select", "upload"
    target: str          # element label / selector description
    value_masked: str    # PII-safe representation of the value typed
    reasoning: str       # agent's short reasoning or playbook step name
    screenshot_path: str | None = None
    tokens: int = 0
    cost_usd: float = 0.0
    ok: bool = True
    error: str | None = None
    _started_at: float = field(default_factory=time.monotonic, repr=False)

    @property
    def duration_ms(self) -> int:
        return int((time.monotonic() - self._started_at) * 1000)


def record_step(step: StepRecord) -> None:
    """Persist *step* to the DB (lazy import)."""
    try:
        from jobagent.db.repo import StepRepo  # noqa: PLC0415
        StepRepo.insert(step)
    except Exception:  # noqa: BLE001
        # Never crash a browser session because logging failed
        pass
