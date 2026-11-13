"""Base types for all apply handlers."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobagent.db.models import Application, Job


@dataclass
class ApplyResult:
    """Result returned by every handler."""

    success: bool
    application_id: int
    # "submitted" | "shadow_stopped" | "failed" | "blocked" | "skipped"
    # In manual mode a "submitted" status means the operator confirmed the
    # click; ``extra["quit"] is True`` asks the orchestrator to stop the run.
    status: str
    confirmation_url: str | None = None
    confirmation_text: str | None = None
    screenshot_before: Path | None = None
    screenshot_after: Path | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ApplyHandlerBase(abc.ABC):
    """Every ATS handler implements this interface."""

    #: Machine name, e.g. "greenhouse"
    name: str = "unknown"
    #: Matches job.ats_type
    ats_type: str = "unknown"

    def can_handle(self, job: Job) -> bool:
        """Return True if this handler should process *job*."""
        return bool(job.ats_type) and job.ats_type == self.ats_type

    @abc.abstractmethod
    async def apply(
        self,
        job: Job,
        app: Application,
        package_dir: Path,
        shadow_mode: bool,
        manual_mode: bool = False,
    ) -> ApplyResult:
        """Execute the full application flow.

        Implementations must never raise — all exceptions must be caught
        internally and surfaced via ``ApplyResult.error``.

        Parameters
        ----------
        job:
            The Job record from the database (read-only).
        app:
            The Application record; the handler may read ``app.id``,
            ``app.answers_json``, etc.
        package_dir:
            Directory that contains the tailored resume PDF, cover letter,
            and ``answers.json`` for this application.
        shadow_mode:
            When *True* the handler should navigate and fill the form but
            must NOT click the final submit button.  It should take a
            screenshot and return ``status="shadow_stopped"``.
        manual_mode:
            When *True* the handler fills the form, leaves the browser open,
            and hands control to the operator (see
            :func:`jobagent.apply.manual.await_manual_submit`) instead of
            clicking Submit itself.  Takes precedence over a live run;
            ``shadow_mode`` still wins over both.
        """
        ...
