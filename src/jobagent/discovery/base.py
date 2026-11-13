"""Base class and RawJob schema shared by all discovery collectors."""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class RawJob(BaseModel):
    """Pre-normalisation job record returned by every collector.

    All fields beyond the three required ones are optional because different
    sources expose different subsets of metadata.
    """

    # ── Required ────────────────────────────────────────────────────────────
    source: str
    source_job_id: str
    url: str

    # ── Identification ───────────────────────────────────────────────────────
    company: str | None = None
    title: str = ""
    apply_url: str | None = None

    # ── Location ─────────────────────────────────────────────────────────────
    location_raw: str | None = None
    country: str | None = None          # ISO 3166-1 alpha-2 (e.g. "GB", "IN")
    city: str | None = None
    remote_type: str | None = None      # "remote" | "hybrid" | "onsite" | None

    # ── Description ──────────────────────────────────────────────────────────
    description_html: str | None = None
    description_md: str | None = None

    # ── Timing ───────────────────────────────────────────────────────────────
    posted_at: datetime | None = None

    # ── Compensation ─────────────────────────────────────────────────────────
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None         # ISO 4217 (e.g. "GBP", "INR")

    # ── Classification ───────────────────────────────────────────────────────
    employment_type: str | None = None  # "full_time" | "contract" | etc.

    # ── Source-specific extras ────────────────────────────────────────────────
    extra: dict[str, Any] = Field(default_factory=dict)


class CollectorBase(abc.ABC):
    """Abstract base every job-source collector must implement.

    Contract:
    - ``fetch`` is async and **must never raise**.  Log errors, return whatever
      was successfully collected up to the point of failure.
    - ``name`` / ``source`` class variable is the canonical source name that
      ends up in every ``RawJob.source`` returned by that collector.
    - ``is_enabled`` may be overridden to gate the collector on feature flags
      or required API keys.
    """

    # Both ``name`` and ``source`` point to the same identifier.
    # ``name`` is preferred by the runner; ``source`` is kept for
    # backwards-compatibility with any code written before the rename.
    name: ClassVar[str] = ""
    source: ClassVar[str] = ""

    @abc.abstractmethod
    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        """Fetch jobs for the given search configuration.

        Args:
            search_cfg: Dict loaded from ``config/search.yaml``.  At minimum
                        contains ``titles: list[str]`` and
                        ``regions: dict[str, dict]``.

        Returns:
            List of ``RawJob`` instances; may be empty on total failure.
        """
        ...

    def is_enabled(self, search_cfg: dict[str, Any]) -> bool:  # noqa: ARG002
        """Return ``True`` when this collector should run.

        The default implementation checks ``search_cfg.get("disabled_sources",
        [])`` and returns ``False`` if ``self.name`` (or ``self.source``) is
        in that list.  Subclasses may override this to also check for required
        API keys or feature flags.
        """
        disabled: list[str] = search_cfg.get("disabled_sources", [])
        identifier = self.name or self.source
        return identifier not in disabled
