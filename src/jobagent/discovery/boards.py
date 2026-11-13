"""ATS board probing and status-transition rules for the ``ats_board`` table.

Two pieces live here so both the validator (``jobagent boards validate``) and
the collectors share exactly the same rules:

* ``probe_board`` — one cheap HTTP check per ATS that answers "does this board
  exist, how many open jobs does it have, what is the company called?".
* ``apply_outcome`` — pure function that moves an ``AtsBoard`` row between
  ``candidate`` / ``live`` / ``empty`` / ``dead`` given a ``BoardOutcome``.

Nothing in this module touches the database session; callers own that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import structlog

from jobagent.db.models import AtsBoard

log = structlog.get_logger(__name__)

# ATS types that have a probe implementation.
PROBE_ATS: tuple[str, ...] = ("greenhouse", "lever", "ashby", "workday")

# A board that was once live is only marked dead after this many 404s in a
# row, so a single bad response doesn't remove a good board.
DEAD_AFTER_FAILURES = 3
# Empty boards are polled at most this often by collectors.
EMPTY_POLL_INTERVAL = timedelta(days=3)
# Dead boards are rechecked by the validator after this long.
DEAD_RECHECK_INTERVAL = timedelta(days=30)

WORKDAY_PAGE_LIMIT = 20  # Workday rejects limit > 20 with HTTP 400

Outcome = Literal["ok", "not_found", "error"]


@dataclass(frozen=True)
class BoardRef:
    """Detached, read-only view of an ``AtsBoard`` row handed to collectors."""

    id: int
    ats_type: str
    slug: str
    board_path: str = ""
    host: str | None = None
    company_name: str | None = None

    @property
    def display_name(self) -> str:
        return self.company_name or self.slug


@dataclass(frozen=True)
class BoardOutcome:
    """Result of probing or polling one board."""

    outcome: Outcome
    job_count: int | None = None
    matched_count: int | None = None
    company_name: str | None = None
    error: str | None = None
    polled: bool = False  # True when produced by a collector run


# ---------------------------------------------------------------------------
# Time helpers (SQLite returns naive datetimes)
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


def apply_outcome(board: AtsBoard, result: BoardOutcome, now: datetime | None = None) -> None:
    """Update *board* in place according to *result*.

    Rules:
    * ``ok``        → ``live`` (jobs > 0) or ``empty``; failures reset.
    * ``not_found`` → ``dead`` immediately for unverified boards (candidate or
      already dead); otherwise only after ``DEAD_AFTER_FAILURES`` in a row.
    * ``error``     → status unchanged; failure counter and error recorded.
      Exception: a ``candidate`` that errors ``DEAD_AFTER_FAILURES`` times in
      a row becomes ``dead`` (it never proved it exists).
    """
    now = now or utcnow()
    board.last_checked_at = now
    if result.polled:
        board.last_polled_at = now

    if result.outcome == "ok":
        count = board.job_count if result.job_count is None else result.job_count
        if count != board.job_count or board.last_job_change_at is None:
            board.last_job_change_at = now
        board.job_count = count
        if result.matched_count is not None:
            board.matched_count = result.matched_count
        board.status = "live" if count > 0 else "empty"
        board.consecutive_failures = 0
        board.last_error = None
        if result.company_name:
            board.company_name = result.company_name
        return

    board.consecutive_failures += 1
    if result.outcome == "not_found":
        board.last_error = result.error or "not found"
        if (
            board.status in ("candidate", "dead")
            or board.consecutive_failures >= DEAD_AFTER_FAILURES
        ):
            board.status = "dead"
            board.job_count = 0
            board.matched_count = 0
    else:
        board.last_error = (result.error or "error")[:300]
        # A board that never worked isn't worth polling forever; a board that
        # was live once is never killed by transient errors.
        if board.status == "candidate" and board.consecutive_failures >= DEAD_AFTER_FAILURES:
            board.status = "dead"


def is_due_for_poll(board: AtsBoard, now: datetime | None = None) -> bool:
    """Return True if a collector should poll *board* this run."""
    now = now or utcnow()
    if board.status in ("live", "candidate"):
        return True
    if board.status == "empty":
        last = as_aware(board.last_polled_at) or as_aware(board.last_checked_at)
        return last is None or now - last >= EMPTY_POLL_INTERVAL
    return False


def is_due_for_validation(board: AtsBoard, now: datetime | None = None) -> bool:
    """Return True if the validator should (re)probe *board*."""
    now = now or utcnow()
    if board.status == "candidate":
        return True
    if board.status == "dead":
        last = as_aware(board.last_checked_at)
        return last is None or now - last >= DEAD_RECHECK_INTERVAL
    return False


# ---------------------------------------------------------------------------
# Probes (one per ATS)
# ---------------------------------------------------------------------------


async def probe_board(client: httpx.AsyncClient, board: BoardRef) -> BoardOutcome:
    """Probe *board* on its ATS.  Never raises."""
    try:
        if board.ats_type == "greenhouse":
            return await _probe_greenhouse(client, board.slug)
        if board.ats_type == "lever":
            return await _probe_lever(client, board.slug)
        if board.ats_type == "ashby":
            return await _probe_ashby(client, board.slug)
        if board.ats_type == "workday":
            return await _probe_workday(client, board)
        return BoardOutcome("error", error=f"no probe for ats_type={board.ats_type}")
    except httpx.HTTPError as exc:
        return BoardOutcome("error", error=f"{type(exc).__name__}: {exc}"[:300])
    except Exception as exc:  # noqa: BLE001 - probes must never raise
        log.warning("boards.probe_failed", ats=board.ats_type, slug=board.slug, error=str(exc))
        return BoardOutcome("error", error=f"{type(exc).__name__}: {exc}"[:300])


def outcome_from_status(status_code: int, *, not_found_codes: tuple[int, ...] = (404,)) -> Outcome | None:
    """Map an HTTP status to an outcome; ``None`` means "2xx, parse the body"."""
    if 200 <= status_code < 300:
        return None
    if status_code in not_found_codes:
        return "not_found"
    return "error"


async def _probe_greenhouse(client: httpx.AsyncClient, slug: str) -> BoardOutcome:
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
    r = await client.get(base)
    if (o := outcome_from_status(r.status_code)) is not None:
        return BoardOutcome(o, error=f"HTTP {r.status_code}")
    name = (r.json() or {}).get("name") or None
    r2 = await client.get(f"{base}/jobs", params={"content": "false"})
    if (o := outcome_from_status(r2.status_code)) is not None:
        return BoardOutcome(o, error=f"HTTP {r2.status_code} (jobs)")
    data = r2.json() or {}
    count = (data.get("meta") or {}).get("total")
    if count is None:
        count = len(data.get("jobs", []))
    return BoardOutcome("ok", job_count=int(count), company_name=name)


async def _probe_lever(client: httpx.AsyncClient, slug: str) -> BoardOutcome:
    r = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if (o := outcome_from_status(r.status_code)) is not None:
        return BoardOutcome(o, error=f"HTTP {r.status_code}")
    postings = r.json()
    return BoardOutcome("ok", job_count=len(postings) if isinstance(postings, list) else 0)


async def _probe_ashby(client: httpx.AsyncClient, slug: str) -> BoardOutcome:
    r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if (o := outcome_from_status(r.status_code)) is not None:
        return BoardOutcome(o, error=f"HTTP {r.status_code}")
    jobs = (r.json() or {}).get("jobs", [])
    return BoardOutcome("ok", job_count=len(jobs))


def workday_api_url(board: BoardRef) -> str:
    return f"https://{board.host}/wday/cxs/{board.slug}/{board.board_path}/jobs"


async def _probe_workday(client: httpx.AsyncClient, board: BoardRef) -> BoardOutcome:
    if not board.host or not board.board_path:
        return BoardOutcome("error", error="missing host/board_path")
    body = {"appliedFacets": {}, "limit": WORKDAY_PAGE_LIMIT, "offset": 0, "searchText": ""}
    headers = {"Accept": "application/json"}
    r = await client.post(workday_api_url(board), json=body, headers=headers)
    if r.status_code in (401, 403):
        # Some tenants want a CSRF token harvested from the board page first
        page = await client.get(f"https://{board.host}/en-US/{board.board_path}")
        token = next((v for k, v in page.cookies.items() if "csrf" in k.lower()), "")
        if token:
            r = await client.post(
                workday_api_url(board), json=body,
                headers={**headers, "X-Calypso-CSRF-Token": token},
            )
    # 404 = unknown board on a real tenant; 422 = unknown tenant
    if (o := outcome_from_status(r.status_code, not_found_codes=(404, 422))) is not None:
        return BoardOutcome(o, error=f"HTTP {r.status_code}")
    data = r.json() or {}
    return BoardOutcome("ok", job_count=int(data.get("total") or len(data.get("jobPostings", []))))
