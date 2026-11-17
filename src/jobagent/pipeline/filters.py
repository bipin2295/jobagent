"""Hard filters: rule-based pre-screening before expensive LLM scoring.

Applied after enrichment. Jobs that fail are marked 'filtered_out' and never scored.
Jobs that pass move to status 'ready_to_score'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
import yaml
from sqlmodel import select

from jobagent.config import load_yaml, settings
from jobagent.db.engine import get_session
from jobagent.db.models import Application, Company, Job

log = structlog.get_logger(__name__)

# Safety cap: never process more than this many enriched jobs in a single run.
_MAX_TOTAL_JOBS = 50_000

# ---------------------------------------------------------------------------
# US-company detection
# ---------------------------------------------------------------------------
# Greenhouse slugs of companies that are US-headquartered and rarely sponsor
# visas for international candidates. Jobs with country=None from these
# companies are treated as US-only and rejected before enrichment.
#
# Keep this list conservative — only add slugs where the COMPANY is US-only
# (not just has a US office). EU/CA/APAC subsidiaries of US companies may
# still offer genuine international roles.
_US_ONLY_SLUGS: frozenset[str] = frozenset({
    # US fintech / banking
    "stripe", "brex", "ramp", "robinhood", "marqeta", "blend", "affirm",
    "chime", "nerdwallet", "plaid",
    # US cloud / infra
    "cloudflare",   # has EU roles but mostly US-based
    "fastly", "netlify", "heroku", "doppler",
    # US data / analytics
    "amplitude", "heap", "census", "hightouch",
    "montecarlo", "acceldata", "bigeye", "trifacta",
    # US AI / ML
    "anthropic", "scaleai", "adept", "runwayml",
    # US SaaS
    "airtable", "notion", "figma", "intercom", "twilio",
    "eventbrite", "squarespace", "benchling",
    "duolingo", "waymo", "lyft",
    # US tech companies with Greenhouse
    "asana", "carta", "gusto", "rippling", "lattice",
    "mercury", "mainstreet", "zip-co",
})

# Title / description phrases that signal US-only roles even when country=None
_US_ONLY_PHRASES: list[str] = [
    "must be authorized to work in the us",
    "must be authorized to work in the united states",
    "legally authorized to work in the us",
    "u.s. citizenship required",
    "us citizenship required",
    "must be a us citizen",
    "requires work authorization in the united states",
    "work in the united states without sponsorship",
    "work in the us without sponsorship",
    "not able to provide sponsorship",
    "unable to sponsor",
    "no sponsorship available",
    "h-1b sponsorship not available",
    "relocation to united states required",
    "based in the united states",
    "you must reside in the united states",
    "open to us candidates only",
    # Salary signals (USD without mentioning remote/global)
    # intentionally not added — many USD-salary jobs are remote-global
]

# Sources that never carry a structured country/office field. When such a job's
# location text cannot be resolved to a country, we have no way to tell whether
# it is somewhere we can work — so it is rejected rather than guessed at.
_UNVERIFIABLE_SOURCES: frozenset[str] = frozenset({
    "jooble", "adzuna", "reed", "jobspy", "linkedin", "indeed", "bayt",
})

# Phrases that positively establish a role as remote-worldwide, which is the
# only case where an unknown country is acceptable (the plan allows
# "Remote (global)" but every other role must resolve to an allowlisted country).
_GLOBAL_REMOTE_PHRASES: tuple[str, ...] = (
    "anywhere in the world",
    "work from anywhere",
    "worldwide",
    "globally remote",
    "remote worldwide",
    "remote (global)",
    "global remote",
    "fully distributed worldwide",
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""   # why it was filtered out (empty string when passed)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _phrase_in_text(phrase: str, text: str) -> bool:
    """True if *phrase* appears as whole words inside *text* (case-insensitive).

    Uses ``\\b`` word-boundary anchors so "intern" does not match "internal".
    """
    pattern = r"\b" + re.escape(phrase.lower()) + r"\b"
    return bool(re.search(pattern, text.lower()))


def _is_blacklisted(company_name: str, blacklist: dict) -> bool:  # type: ignore[type-arg]
    """Return True if *company_name* matches any entry in the blacklist config.

    Matching rule: bidirectional case-insensitive substring match against
    primary names and aliases (the blacklisted token is a substring of the
    company name, or vice versa).
    """
    if not company_name:
        return False
    name_lower = company_name.lower().strip()
    for entry in blacklist.get("companies", []):
        candidates: list[str] = [entry.get("name", "")] + list(entry.get("aliases", []))
        for candidate in candidates:
            cand_lower = candidate.lower().strip()
            if not cand_lower:
                continue
            if cand_lower in name_lower or name_lower in cand_lower:
                return True
    return False


def _get_region_for_country(country: str, regions: dict) -> dict | None:  # type: ignore[type-arg]
    """Return the first region config dict whose 'countries' list includes *country*."""
    for _region_name, region_cfg in regions.items():
        if country in region_cfg.get("countries", []):
            return region_cfg
    return None


def _salary_floor(country: str, answers: dict) -> tuple[float | None, str | None]:  # type: ignore[type-arg]
    """Return ``(floor, currency)`` for *country* from the answers seed.

    The seed stores floors in the country's own currency: an ``IN`` block uses
    ``filter_floor_lpa`` (lakhs per annum), every other country uses ``floor``.
    Returns ``(None, None)`` when the country has no configured floor.
    """
    comp = (answers.get("compensation") or {}).get(country)
    if not isinstance(comp, dict):
        return None, None

    currency = comp.get("currency")
    if country == "IN":
        floor_lpa = comp.get("filter_floor_lpa")
        if floor_lpa is None:
            return None, currency
        return float(floor_lpa) * 100_000, currency

    floor = comp.get("floor")
    if floor is None:
        return None, currency
    return float(floor), currency


# ---------------------------------------------------------------------------
# Core filter function
# ---------------------------------------------------------------------------


def apply_filters(job: Job, search_cfg: dict, answers: dict) -> FilterResult:  # type: ignore[type-arg]
    """Apply all hard rules in order and return on first failure.

    Rules (a–h):
        a0) Country must be allowlisted (or demonstrably global-remote when
            unknown); US-company / US-only signals are rejected.
        a) Title must match at least one phrase from search_cfg["titles"].
        b) Title must not contain any excluded keyword.
        c) Posting must not be older than max_age_days.
        c2) Posted salary maximum must not be below the regional floor.
        d) Company must not be on the blacklist.
        e) Seniority (when known) must be in the allowed set.
        f) Language requirements — deferred to LLM scorer (not stored on Job).
        g) Visa: hard-fail when sponsorship is explicitly "no" for non-IN jobs.
        h) Already applied to the same company within the last 30 days.

    Opens its own read-only DB sessions for company and application lookups;
    these sessions are separate from whatever session the caller holds.
    """
    title_lower = (job.title or "").lower()

    # ── a0. Country allowlist + US-company detection ─────────────────────────
    # Jobs with a known country NOT in the allowlist → reject immediately.
    # Jobs with country=None → check for US-company slug, explicit US phrases
    # in the description, or US city/state signals in the title.
    country_allowlist: list[str] = search_cfg.get("country_allowlist", [])
    if country_allowlist:
        if job.country is not None and job.country not in country_allowlist:
            return FilterResult(
                passed=False,
                reason=f"country not in allowlist: '{job.country}'",
            )

        if job.country is None:
            # 1. Known US-only Greenhouse company slug
            if job.company_id:
                try:
                    with get_session() as _s:
                        _co = _s.get(Company, job.company_id)
                    if _co and (_co.ats_slug or "").lower() in _US_ONLY_SLUGS:
                        return FilterResult(
                            passed=False,
                            reason=f"US-headquartered company (slug={_co.ats_slug!r}); no visa sponsorship expected",
                        )
                except Exception:
                    pass

            # 2. US city/state signals in title or city field
            _title_and_loc = (
                (job.title or "") + " " + (job.city or "")
            ).lower()
            _US_LOCATION_SIGNALS = [
                " - usa", " - us", "(usa)", "(us)", "united states",
                " new york", " san francisco", " seattle", " austin",
                " boston", " chicago", " los angeles", " denver",
                " atlanta", " dallas", " miami",
            ]
            if any(sig in _title_and_loc for sig in _US_LOCATION_SIGNALS):
                return FilterResult(
                    passed=False,
                    reason="title/location signals US-only role",
                )

            # 3. Explicit no-sponsorship / US-only phrases in description
            _desc = (job.description_md or "").lower()
            for phrase in _US_ONLY_PHRASES:
                if phrase in _desc:
                    return FilterResult(
                        passed=False,
                        reason=f"description signals US-only: '{phrase[:60]}'",
                    )

            # 4. Unknown country: only acceptable for demonstrably global-remote
            # roles. There is no truthful way to claim visa eligibility for a
            # posting whose location could not be resolved, and aggregator feeds
            # routinely return US roles with a blank location (e.g. Jooble
            # "Remote"). Everything else must resolve to an allowlisted country.
            is_global_remote = job.remote_type == "remote" and any(
                p in _desc for p in _GLOBAL_REMOTE_PHRASES
            )
            if not is_global_remote:
                source = (job.source or "").lower()
                if source in _UNVERIFIABLE_SOURCES:
                    reason = (
                        f"aggregator job with unverifiable country (source={source!r})"
                    )
                else:
                    reason = "country could not be determined and role is not global-remote"
                return FilterResult(passed=False, reason=reason)

    # ── a. Title allowlist ───────────────────────────────────────────────────
    titles: list[str] = search_cfg.get("titles", [])
    if titles:
        if not any(_phrase_in_text(phrase, title_lower) for phrase in titles):
            return FilterResult(
                passed=False,
                reason=f"title not in allowlist: '{job.title}'",
            )

    # ── b. Excluded title keywords ───────────────────────────────────────────
    for kw in search_cfg.get("exclude_titles", []):
        if _phrase_in_text(str(kw), title_lower):
            return FilterResult(passed=False, reason=f"excluded title keyword: '{kw}'")

    # ── c. Age ───────────────────────────────────────────────────────────────
    if job.posted_at is not None:
        posted = job.posted_at
        # Ensure timezone-aware for comparison
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
        age = datetime.now(UTC) - posted
        max_age_days: int = int(search_cfg.get("max_age_days", 21))
        if age.days > max_age_days:
            return FilterResult(
                passed=False,
                reason=f"posting too old ({age.days} days > {max_age_days})",
            )

    # ── c2. Salary floor ─────────────────────────────────────────────────────
    # Reject when the posting's maximum is below the regional floor (PLAN §4.3).
    # Only enforced when the posting currency matches the seed's currency for
    # that country, so we never compare USD figures against a EUR floor.
    if job.salary_max is not None and job.country:
        floor, floor_currency = _salary_floor(job.country, answers)
        # Aggregators quote monthly or daily rates for some markets (e.g.
        # Singapore SGD 1,440/month, Australia AUD 421/day). Comparing those to
        # an annual floor would reject good roles, so only enforce the floor
        # when the figure is at least two months' worth of it — i.e. plausibly
        # an annual salary.
        plausibly_annual = floor is not None and job.salary_max >= (floor / 6)
        if (
            floor is not None
            and floor_currency
            and plausibly_annual
            and (job.currency or "").upper() == floor_currency.upper()
            and job.salary_max < floor
        ):
            return FilterResult(
                passed=False,
                reason=(
                    f"salary below floor: max {job.salary_max:,.0f} "
                    f"{job.currency} < {floor:,.0f} {floor_currency}"
                ),
            )

    # ── d. Blacklist ─────────────────────────────────────────────────────────
    if job.company_id is not None:
        blacklist = load_yaml("blacklist")
        with get_session() as session:
            company = session.get(Company, job.company_id)
        # company is detached from the closed session; scalar attrs remain accessible
        if company is not None and _is_blacklisted(company.name, blacklist):
            return FilterResult(
                passed=False,
                reason=f"blacklisted company: '{company.name}'",
            )

    # ── e. Seniority ─────────────────────────────────────────────────────────
    seniority = job.seniority
    if seniority and seniority != "unknown":
        allowed_seniority: list[str] = search_cfg.get("seniority", ["senior", "lead", "staff"])
        if seniority not in allowed_seniority:
            return FilterResult(
                passed=False,
                reason=f"seniority mismatch: '{seniority}' not in {allowed_seniority}",
            )

    # ── f. Language requirements ─────────────────────────────────────────────
    # language_requirements are not persisted on the Job row (enrichment writes
    # required_skills_json only). This check is deferred to the LLM scorer's
    # red_flags logic where the full enrichment context is available.

    # ── g. Visa ──────────────────────────────────────────────────────────────
    if job.country is not None and job.country != "IN":
        if job.visa_sponsorship == "no":
            return FilterResult(passed=False, reason="no visa sponsorship")
        # visa_sponsorship == "unknown" + strict visa_policy region:
        # don't hard-filter here — let the scorer deduct points instead.

    # ── h. Already SUBMITTED to same company within 30 days ─────────────────
    # Only block on real submissions — skipped/failed/shadow_stopped do NOT
    # count. A "skipped" application means the ATS resolver failed; we never
    # actually sent anything to the company.
    if job.company_id is not None:
        cutoff = datetime.now(UTC) - timedelta(days=30)
        with get_session() as session:
            stmt = (
                select(Application)
                .join(Job, Application.job_id == Job.id)
                .where(
                    Job.company_id == job.company_id,
                    Application.status == "submitted",
                    Application.created_at > cutoff,
                )
                .limit(1)
            )
            existing = session.exec(stmt).first()
        if existing is not None:
            return FilterResult(passed=False, reason="already applied recently (submitted)")

    return FilterResult(passed=True)


# ---------------------------------------------------------------------------
# Batch entry point
# ---------------------------------------------------------------------------


def run_filters(batch_size: int = 500) -> tuple[int, int]:
    """Apply hard filters to all 'enriched' jobs, updating their status in place.

    Loads search config and answers seed once, then processes jobs in batches of
    *batch_size*.  After each batch is committed the next query naturally returns
    a fresh set of still-'enriched' rows.

    Returns:
        (passed, filtered_out) counts.
    """
    search_cfg: dict = load_yaml("search")  # type: ignore[type-arg]

    answers: dict = {}  # type: ignore[type-arg]
    answers_path: Path = settings.profiles_dir / "answers_seed.yaml"
    if answers_path.exists():
        with answers_path.open(encoding="utf-8") as fh:
            answers = yaml.safe_load(fh) or {}

    passed_count = 0
    filtered_count = 0
    total_processed = 0

    while total_processed < _MAX_TOTAL_JOBS:
        with get_session() as session:
            stmt = (
                select(Job)
                .where(Job.status == "enriched")
                .limit(batch_size)
            )
            batch = list(session.exec(stmt).all())

            if not batch:
                break

            for job in batch:
                try:
                    result = apply_filters(job, search_cfg, answers)
                except Exception:
                    log.exception("filter.job_error", job_id=job.id, title=job.title)
                    # Mark as filtered_out so the job doesn't re-enter the loop
                    result = FilterResult(passed=False, reason="filter_error")

                if result.passed:
                    job.status = "ready_to_score"
                    passed_count += 1
                    log.debug("filter.passed", job_id=job.id, title=job.title)
                else:
                    job.status = "filtered_out"
                    filtered_count += 1
                    log.debug(
                        "filter.rejected",
                        job_id=job.id,
                        title=job.title,
                        reason=result.reason,
                    )

                session.add(job)

            session.commit()

        total_processed += len(batch)
        log.info(
            "filter.batch_done",
            passed=passed_count,
            filtered_out=filtered_count,
            batch_size=len(batch),
            total_processed=total_processed,
        )

        if len(batch) < batch_size:
            break

    log.info(
        "filter.complete",
        passed=passed_count,
        filtered_out=filtered_count,
        total_processed=total_processed,
    )
    return passed_count, filtered_count
