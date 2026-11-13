"""SQLModel table definitions for the job-application agent.

All 17 tables are declared here as SQLModel classes with ``table=True``.
JSON payload columns (lists / dicts) are stored as plain ``str``; callers
are responsible for ``json.dumps`` / ``json.loads`` round-trips.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# ---------------------------------------------------------------------------
# 1. Company
# ---------------------------------------------------------------------------


class Company(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    name_norm: str
    # Legacy ATS columns: superseded by ``ats_board`` (one company can have
    # several boards).  Still written by dedup and read by filters; no longer
    # used to decide which boards get polled.
    ats_type: Optional[str] = None
    ats_slug: Optional[str] = None
    country: Optional[str] = None
    on_sponsor_register: bool = Field(default=False)
    blacklisted: bool = Field(default=False)
    last_polled_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 1b. AtsBoard  (registry of company job boards on ATS platforms)
# ---------------------------------------------------------------------------


class AtsBoard(SQLModel, table=True):
    """One public job board on an ATS (Greenhouse, Lever, Ashby, Workday, ...).

    Lifecycle (``status``):
      candidate → never checked (seed, flywheel, slug guess, crawl, ...)
      live      → board exists and has ≥1 open job
      empty     → board exists but has 0 open jobs (polled less often)
      dead      → board not found (404 / invalid tenant); rechecked rarely

    Collectors read pollable boards from here and write jobs into ``job``.
    """

    __tablename__ = "ats_board"
    __table_args__ = (
        UniqueConstraint("ats_type", "slug", "board_path", name="uq_ats_board"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    ats_type: str = Field(index=True)
    # Board token / company slug (Workday: tenant, e.g. "nvidia")
    slug: str
    # Workday only: board path, e.g. "NVIDIAExternalCareerSite" ("" otherwise)
    board_path: str = Field(default="")
    # Workday only: host, e.g. "nvidia.wd5.myworkdayjobs.com"
    host: Optional[str] = None
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    company_name: Optional[str] = None
    status: str = Field(default="candidate", index=True)
    # seed | flywheel | backfill | guess | commoncrawl | search | careers_page | manual
    discovered_via: str = Field(default="manual")
    job_count: int = Field(default=0)
    matched_count: int = Field(default=0)
    consecutive_failures: int = Field(default=0)
    last_error: Optional[str] = None
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_checked_at: Optional[datetime] = None
    last_polled_at: Optional[datetime] = None
    last_job_change_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 2. Job
# ---------------------------------------------------------------------------


class Job(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    source: str
    source_job_id: str
    url: str
    apply_url: Optional[str] = None
    ats_type: Optional[str] = None
    title: str
    title_norm: str
    country: Optional[str] = None
    city: Optional[str] = None
    remote_type: Optional[str] = None
    description_md: Optional[str] = None
    posted_at: Optional[datetime] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    currency: Optional[str] = None
    employment_type: Optional[str] = None
    content_hash: str
    # "yes" | "no" | "unknown"
    visa_sponsorship: Optional[str] = None
    relocation_support: Optional[str] = None
    # Boolean flags derived from visa_sponsorship / relocation_support.
    # Set by the enrichment pipeline; default False until enrichment runs.
    # visa_ok=True  → company confirmed it will sponsor a work visa
    # relocation_ok=True → company confirmed relocation support/package
    # Use --visa-confirmed in apply/jobs commands to filter on these flags.
    visa_ok: bool = Field(default=False)
    relocation_ok: bool = Field(default=False)
    seniority: Optional[str] = None
    years_required: Optional[int] = None
    required_skills_json: Optional[str] = None  # JSON list[str]
    status: str = Field(default="discovered")
    score: Optional[int] = None
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 3. JobSource
# ---------------------------------------------------------------------------


class JobSource(SQLModel, table=True):
    __tablename__ = "job_source"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id")
    source: str
    source_url: Optional[str] = None


# ---------------------------------------------------------------------------
# 4. Score
# ---------------------------------------------------------------------------


class Score(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id")
    model: str
    prompt_version: str
    score: int
    breakdown_json: Optional[str] = None  # JSON dict
    pitch_angle: Optional[str] = None
    archetype: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 5. Application
# ---------------------------------------------------------------------------


class Application(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id")
    status: str = Field(default="queued")
    channel: Optional[str] = None
    resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    answers_json: Optional[str] = None      # JSON list[dict]
    plan_json: Optional[str] = None         # JSON dict
    fact_check_json: Optional[str] = None   # JSON dict
    shadow_mode: bool = Field(default=True)
    attempts: int = Field(default=0)
    error: Optional[str] = None
    submitted_at: Optional[datetime] = None
    confirmation_path: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 6. AppEvent  (status-transition audit log)
# ---------------------------------------------------------------------------


class AppEvent(SQLModel, table=True):
    __tablename__ = "app_event"

    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id")
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    from_status: Optional[str] = None
    to_status: str
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# 7. AppStep  (granular browser-action trace)
# ---------------------------------------------------------------------------


class AppStep(SQLModel, table=True):
    __tablename__ = "app_step"

    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id")
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    page_type: Optional[str] = None
    layer: Optional[str] = None
    action: Optional[str] = None
    target: Optional[str] = None
    value_masked: Optional[str] = None
    reasoning: Optional[str] = None
    screenshot_path: Optional[str] = None
    tokens: int = Field(default=0)
    cost_usd: float = Field(default=0.0)
    ok: bool = Field(default=True)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# 8. AppCheckpoint  (resume-on-restart state; application_id is the PK)
# ---------------------------------------------------------------------------


class AppCheckpoint(SQLModel, table=True):
    __tablename__ = "app_checkpoint"

    # Single-column PK that is also an FK – no surrogate id needed.
    application_id: int = Field(foreign_key="application.id", primary_key=True)
    page_index: int
    page_signature: str
    account_id: Optional[int] = None
    filled_values_json: Optional[str] = None  # JSON dict
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 9. Account
# ---------------------------------------------------------------------------


class Account(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ats: str
    tenant: str
    login_url: str
    email_alias: str
    keychain_ref: str
    status: str = Field(default="creating")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_login_at: Optional[datetime] = None
    data_submitted_json: Optional[str] = None  # JSON dict


# ---------------------------------------------------------------------------
# 10. LearnedTrajectory
# ---------------------------------------------------------------------------


class LearnedTrajectory(SQLModel, table=True):
    __tablename__ = "learned_trajectory"

    id: Optional[int] = Field(default=None, primary_key=True)
    ats: str
    tenant: str
    page_signature: str
    actions_json: str  # JSON list[dict]
    success_count: int = Field(default=0)
    fail_count: int = Field(default=0)
    last_used_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 11. ControlFlag  (composite PK: scope + scope_id)
# ---------------------------------------------------------------------------


class ControlFlag(SQLModel, table=True):
    __tablename__ = "control_flag"

    scope: str = Field(primary_key=True)
    scope_id: str = Field(primary_key=True)
    state: str = Field(default="running")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_by: Optional[str] = None


# ---------------------------------------------------------------------------
# 12. Block
# ---------------------------------------------------------------------------


class Block(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id")
    reason: str
    evidence_path: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 13. Email
# ---------------------------------------------------------------------------


class Email(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    gmail_id: str = Field(unique=True)
    application_id: Optional[int] = Field(default=None, foreign_key="application.id")
    received_at: datetime
    from_addr: str
    subject: str
    classification: Optional[str] = None
    snippet: Optional[str] = None


# ---------------------------------------------------------------------------
# 14. QuestionSeen
# ---------------------------------------------------------------------------


class QuestionSeen(SQLModel, table=True):
    __tablename__ = "question_seen"

    id: Optional[int] = Field(default=None, primary_key=True)
    question_text_norm: str
    ats_type: Optional[str] = None
    example_job_id: Optional[int] = None
    answer: Optional[str] = None
    answered_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 15. Run  (pipeline-stage execution log)
# ---------------------------------------------------------------------------


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    stage: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: Optional[datetime] = None
    items_in: int = Field(default=0)
    items_out: int = Field(default=0)
    errors: int = Field(default=0)
    status: str = Field(default="running")
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# 16. LlmCall  (per-call observability for cost tracking)
# ---------------------------------------------------------------------------


class LlmCall(SQLModel, table=True):
    __tablename__ = "llm_call"

    id: Optional[int] = Field(default=None, primary_key=True)
    task: str
    model: str
    prompt_version: Optional[str] = None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    cached: bool = Field(default=False)
    trace_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
