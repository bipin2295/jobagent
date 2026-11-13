"""Registry of company job boards on ATS platforms (the ``ats_board`` table).

Sources that feed the registry
------------------------------
1. **Seed lists**  — hardcoded slugs below (+ ``WORKDAY_SEEDS``).
2. **Flywheel**    — every ``apply_url``/``url`` seen by any collector is
   parsed for an ATS board and added as a ``candidate``.
3. **Backfill**    — one-off import of legacy ``company.ats_slug`` rows and of
   ATS URLs already stored in the ``job`` table.
4. **Discovery scripts** — slug guessing, crawls, search (``discovered_via``).

Lifecycle: ``candidate`` → ``live`` / ``empty`` / ``dead``; see
:mod:`jobagent.discovery.boards` for the rules.  Collectors read pollable
boards via :meth:`CompanyUniverse.get_boards` and report back with
:meth:`CompanyUniverse.record_outcomes`; the jobs themselves go to ``job``.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable

import httpx
import structlog
from sqlalchemy import func, or_
from sqlmodel import Session, select

from jobagent.db.engine import get_session
from jobagent.db.models import AtsBoard, Company, Job
from jobagent.discovery.base import RawJob
from jobagent.discovery.boards import (
    PROBE_ATS,
    BoardOutcome,
    BoardRef,
    apply_outcome,
    is_due_for_poll,
    is_due_for_validation,
    probe_board,
    utcnow,
)
from jobagent.pipeline.ats_detect import extract_ats_slug, extract_workday_board
from jobagent.pipeline.normalise import normalise_company

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Hardcoded seed lists
# ---------------------------------------------------------------------------

GREENHOUSE_SEEDS: list[str] = [
    # ── US fintech / payments ─────────────────────────────────────────────────
    "stripe", "brex", "ramp", "robinhood", "carta", "gusto", "rippling",
    "plaid", "marqeta", "blend", "affirm", "chime", "nerdwallet",
    # ── US cloud infrastructure / DevTools ───────────────────────────────────
    "cloudflare", "datadog", "vercel", "sentry", "launchdarkly", "pagerduty",
    "fastly", "netlify", "heroku", "doppler", "teleport",
    "hashicorp", "chef", "puppet", "ansible",
    # ── US data / analytics ───────────────────────────────────────────────────
    "databricks", "fivetran", "amplitude", "heap", "mixpanel",
    "hightouch", "census", "rudderstack", "segment",
    "dbt-labs", "lightdash", "preset-io", "hex-tech",
    "montecarlo", "acceldata", "atlan", "bigeye",
    "trifacta", "alteryx", "talend", "informatica",
    "starburst", "dremio", "duckdb",
    "airbyte", "meltano", "fivetranio",
    # ── US AI / ML ────────────────────────────────────────────────────────────
    "anthropic", "scaleai", "huggingface", "cohere", "adept",
    "stability-ai", "runwayml", "weights-biases",
    # ── US SaaS / enterprise ─────────────────────────────────────────────────
    "airtable", "notion", "figma", "intercom", "twilio", "sendgrid",
    "contentful", "sanity", "algolia",
    "lattice", "leapsome", "pento",
    "eventbrite", "squarespace", "mailchimp",
    "benchling", "veeva", "medidata",
    "duolingo", "waymo", "lyft", "airbnb", "pinterest",
    # ── EU / UK fintech ───────────────────────────────────────────────────────
    "wise", "revolut", "monzo", "n26", "starlingbank", "oaknorth",
    "sumup", "paysafe", "worldpay", "checkout",
    "klarna", "pleo", "personio", "billie", "moss",
    "iwoca", "capchase", "wayflyer",
    # ── EU / UK tech & data ───────────────────────────────────────────────────
    "deliveroo", "gorillas", "getir", "studenmund",
    "spotify", "klarna", "adyen",
    "onfido", "improbable", "tractable", "wayve",
    "multiverse", "brainly", "platform-sh",
    "contentful", "babbel", "about-you", "rewe-digital",
    "personio", "nfon", "celonis", "iobeam",
    "adjustcom", "remerge", "pliant",
    "signavio", "camunda", "processgold",
    "appsflyer", "dynamic-yield", "similarweb",
    "monday", "wix", "fiverr", "taboola", "outbrain",
    # ── APAC / India-linked companies (remote-friendly or India offices) ──────
    "grab", "gojek", "bukalapak", "tokopedia",
    "atlassian", "canva", "SafetyCulture", "airtasker",
    "wealthsimple", "koho", "secureframe",
    # ── Verified non-US live boards (checked 2026-11-12) ─────────────────────
    # Germany / DACH
    "n26",          # Berlin neobank — VERIFIED active DE jobs
    "hellofresh",   # Berlin meal kits — VERIFIED active DE jobs
    "sumup",        # Berlin payments — VERIFIED active DE jobs
    "taxfix",       # Berlin tax — VERIFIED board
    "getir",        # Berlin grocery — VERIFIED board
    "babbel",       # Berlin language — VERIFIED board
    "ecosia",       # Berlin search — VERIFIED board
    "signavio",     # Berlin BPM — VERIFIED board
    "camunda",      # Berlin automation — VERIFIED board
    "staffbase",    # Berlin comms — VERIFIED board
    # Netherlands
    "catawiki",     # NL auction — VERIFIED active DE jobs
    "mollie",       # NL payments — VERIFIED board
    "sendcloud",    # NL logistics — VERIFIED board
    # UK
    "monzo",        # UK neobank — VERIFIED active DE jobs
    "deliveroo",    # UK food delivery — VERIFIED board
    "canonical",    # UK/global Ubuntu — VERIFIED active DE jobs
    "oaknorth",     # UK bank — VERIFIED board
    "farfetch",     # UK luxury ecom — VERIFIED board
    "depop",        # UK resale — VERIFIED board
    "revolut",      # UK fintech — VERIFIED board
    # Nordics
    "klarna",       # Sweden BNPL — VERIFIED board
    "spotify",      # Sweden music — VERIFIED board
    # Remote-first / global
    "gitlab",       # Remote-first — VERIFIED active DE jobs
    "canonical",    # Ubuntu/global — VERIFIED board
    # France
    "valtech",      # Global digital agency — VERIFIED active DE jobs (Snowflake/Lead)
    "payfit",       # Paris HR — VERIFIED board
    "spendesk",     # Paris fintech — VERIFIED board
    # Canada (Global Talent Stream — easier sponsorship than US H-1B)
    "hootsuite",    # Vancouver — VERIFIED board
    "d2l",          # Waterloo edtech — VERIFIED board
    "later",        # Vancouver social — VERIFIED board
    "flipp",        # Toronto retail — VERIFIED board
    "tulip",        # Toronto retail — VERIFIED board
    "remote",       # Remote.com global — VERIFIED board
    # ── Newly discovered via slug-probe 2026-11-12 — non-US, active DE jobs ───
    "artefactjobs",   # Paris/Casablanca — analytics/data consultancy
    "cognite",        # Norway + India — industrial data platform
    "ocadogroup",     # Poland/Bulgaria — UK grocery tech
    "flowtraders",    # Amsterdam/London — trading tech
    "goodnotes",      # Singapore — EdTech
    "wooga",          # Berlin — games/mobile
    "simplifynext",   # Singapore — data consultancy
    "sahajsoftware",  # London/Chennai/Bengaluru — digital delivery
    "dept",           # Global digital agency (EU HQ, Netherlands)
    # ── Remote-first data companies ───────────────────────────────────────────
    "remote", "deel", "oysterhr", "omnipresent",
    "doist", "gitlab-recs", "zapier", "buffer",
    "hotjar", "geckoboard", "chartio",
    # ── Mid-size / growth-stage data engineering ──────────────────────────────
    "posthog", "telemetrytv", "arctype", "count",
    "taktile", "metaplane", "datafold",
    "orchestra-data", "meltano", "orchest",
    "getmercury", "pricefx", "bloomreach",
    "chartboost", "applovin", "ironsource",
    "mparticle", "treasure-data", "lytics",
    "blueshift", "simon-data", "iterable",
]

LEVER_SEEDS: list[str] = [
    # US
    "squareup", "databricks", "reddit", "asana", "benchmarkmail",
    "hashnode", "linear", "retool", "dbt-labs",
    "nerdio", "sumo-logic", "logdna", "papertrail",
    # EU
    "booking", "trivago", "hellofresh", "flixbus",
    "wolt", "getir", "gorillas", "flink",
    "contentful-lever", "babbel-lever",
    "reachdesk", "learnerbly", "smartly-io",
    "lunar", "penta", "agicap",
    "omnipresent-lever", "remote-lever",
    # Remote / global
    "remote", "doist", "close",
    "procurify", "brightpearl", "cin7",
]

ASHBY_SEEDS: list[str] = [
    # Data infra / tooling
    "motherduck", "clickhouse", "neon", "supabase",
    "posthog", "modal", "fly", "render",
    "turso", "pscale", "nile",
    "evidence-dev", "cube-dev", "lightdash-hq",
    "querybook", "datahub-io",
    "mage-ai", "prefect", "marquez",
    # AI / ML platforms
    "together-ai", "fireworks-ai", "anyscale",
    "determined-ai", "bentoml",
    # Remote-first
    "cal-com", "plane-so", "documenso",
    # EU scaleups
    "lago", "trigger-dev", "infisical",
]

# Map ATS name → seed list for internal iteration
_SEEDS: dict[str, list[str]] = {
    "greenhouse": GREENHOUSE_SEEDS,
    "lever": LEVER_SEEDS,
    "ashby": ASHBY_SEEDS,
}

_VALIDATE_CONCURRENCY = 10
_VALIDATE_TIMEOUT = 15.0


def _to_ref(b: AtsBoard) -> BoardRef:
    assert b.id is not None
    return BoardRef(
        id=b.id,
        ats_type=b.ats_type,
        slug=b.slug,
        board_path=b.board_path or "",
        host=b.host,
        company_name=b.company_name,
    )


def board_key_from_url(url: str) -> tuple[str, str, str, str | None] | None:
    """Return ``(ats_type, slug, board_path, host)`` for an ATS URL, or None.

    Workday URLs need the host and board path to be pollable, so they go
    through :func:`extract_workday_board`; a Workday URL without a board path
    is ignored.
    """
    wd = extract_workday_board(url)
    if wd is not None:
        tenant, host, board_path = wd
        return "workday", tenant, board_path, host
    res = extract_ats_slug(url)
    if res is None:
        return None
    ats_type, slug = res
    if ats_type == "workday":
        return None
    return ats_type, slug, "", None


# ---------------------------------------------------------------------------
# CompanyUniverse
# ---------------------------------------------------------------------------


class CompanyUniverse:
    """Read/write access to the ``ats_board`` registry.

    Stateless; safe to instantiate many times.  Every public method opens its
    own session unless one is passed in.
    """

    # ── Queries ─────────────────────────────────────────────────────────────

    def get_boards(self, ats: str, *, due_only: bool = True) -> list[BoardRef]:
        """Return boards for *ats* that collectors should poll.

        ``due_only=True`` (default) returns live + candidate boards and empty
        boards whose poll interval has elapsed.  ``due_only=False`` returns
        every non-dead board.
        """
        self.ensure_initialized()
        now = utcnow()
        with get_session() as session:
            rows = session.exec(
                select(AtsBoard).where(AtsBoard.ats_type == ats, AtsBoard.status != "dead")
            ).all()
            keep = [b for b in rows if (not due_only or is_due_for_poll(b, now))]
            if ats == "workday":
                keep = [b for b in keep if b.host and b.board_path]
            return [_to_ref(b) for b in keep]

    def get_slugs(self, ats: str) -> list[str]:
        """Slugs of the boards due for polling (kept for simple collectors)."""
        return list(dict.fromkeys(b.slug for b in self.get_boards(ats)))

    def all_slugs(self, ats: str) -> set[str]:
        """Every known slug for *ats*, any status (to avoid re-probing)."""
        with get_session() as session:
            return set(session.exec(select(AtsBoard.slug).where(AtsBoard.ats_type == ats)).all())

    def stats(self) -> dict[tuple[str, str], int]:
        """Return ``{(ats_type, status): count}``."""
        with get_session() as session:
            rows = session.exec(
                select(AtsBoard.ats_type, AtsBoard.status, func.count()).group_by(
                    AtsBoard.ats_type, AtsBoard.status
                )
            ).all()
        return {(a, s): int(n) for a, s, n in rows}

    # ── Writes ───────────────────────────────────────────────────────────────

    def add_board(
        self,
        ats: str,
        slug: str,
        *,
        board_path: str = "",
        host: str | None = None,
        discovered_via: str = "manual",
        company_name: str | None = None,
        company_id: int | None = None,
        session: Session | None = None,
    ) -> bool:
        """Insert a ``candidate`` board if it is not already known.

        Returns ``True`` when a new row was inserted.
        """
        if session is None:
            with get_session() as s:
                added = self.add_board(
                    ats, slug, board_path=board_path, host=host,
                    discovered_via=discovered_via, company_name=company_name,
                    company_id=company_id, session=s,
                )
                s.commit()
                return added

        ats = ats.lower().strip()
        slug = slug.strip().lower()  # board_path (Workday) keeps its case
        if not slug:
            return False

        existing = session.exec(
            select(AtsBoard).where(
                AtsBoard.ats_type == ats,
                AtsBoard.slug == slug,
                AtsBoard.board_path == (board_path or ""),
            )
        ).first()
        if existing is None:
            # Pending (unflushed) rows in this session also count as existing
            for obj in session.new:
                if (
                    isinstance(obj, AtsBoard)
                    and obj.ats_type == ats
                    and obj.slug == slug
                    and obj.board_path == (board_path or "")
                ):
                    existing = obj
                    break
        if existing is not None:
            changed = False
            if host and not existing.host:
                existing.host, changed = host, True
            if company_id and not existing.company_id:
                existing.company_id, changed = company_id, True
            if company_name and not existing.company_name:
                existing.company_name, changed = company_name, True
            if changed:
                session.add(existing)
            return False

        if company_id is None and company_name:
            company_id = session.exec(
                select(Company.id).where(Company.name_norm == normalise_company(company_name))
            ).first()

        session.add(
            AtsBoard(
                ats_type=ats,
                slug=slug,
                board_path=board_path or "",
                host=host,
                company_id=company_id,
                company_name=company_name,
                discovered_via=discovered_via,
            )
        )
        log.debug("company_universe.board_added", ats=ats, slug=slug, via=discovered_via)
        return True

    def add_from_url(
        self,
        url: str,
        *,
        company_name: str | None = None,
        discovered_via: str = "flywheel",
        session: Session | None = None,
    ) -> bool:
        """Extract an ATS board from *url* and add it if new."""
        key = board_key_from_url(url)
        if key is None:
            return False
        ats, slug, board_path, host = key
        return self.add_board(
            ats, slug, board_path=board_path, host=host,
            discovered_via=discovered_via, company_name=company_name, session=session,
        )

    def feed_jobs(self, raw_jobs: list[RawJob]) -> int:
        """Add every ATS board referenced by *raw_jobs*; return # new boards.

        Jobs collected straight from an ATS (source == ats type) are skipped:
        their board is already registered.
        """
        new_count = 0
        with get_session() as session:
            for raw in raw_jobs:
                if raw.source in PROBE_ATS:
                    continue
                url = raw.apply_url or raw.url
                if not url:
                    continue
                try:
                    if self.add_from_url(url, company_name=None, session=session):
                        new_count += 1
                except Exception:
                    log.exception("company_universe.feed_error", url=url, source=raw.source)
            session.commit()
        return new_count

    def record_outcomes(self, outcomes: Iterable[tuple[int, BoardOutcome]]) -> None:
        """Apply collector/validator results to their boards (by board id)."""
        outcomes = list(outcomes)
        if not outcomes:
            return
        now = utcnow()
        with get_session() as session:
            for board_id, result in outcomes:
                board = session.get(AtsBoard, board_id)
                if board is None:
                    continue
                apply_outcome(board, result, now)
                if result.outcome == "ok" and result.company_name:
                    _link_company(session, board)
                session.add(board)
            session.commit()

    def mark_dead(self, ats: str, slug: str) -> None:
        """Legacy helper: record a 404 for every board with this slug."""
        with get_session() as session:
            ids = session.exec(
                select(AtsBoard.id).where(AtsBoard.ats_type == ats, AtsBoard.slug == slug)
            ).all()
        self.record_outcomes((i, BoardOutcome("not_found", error="HTTP 404")) for i in ids if i)

    # ── Seeding / backfill ──────────────────────────────────────────────────

    def load_seed_lists(self) -> int:
        """Insert hardcoded seeds (incl. Workday) as candidates; idempotent."""
        from jobagent.discovery.collectors.workday import WORKDAY_SEEDS  # noqa: PLC0415

        new_count = 0
        with get_session() as session:
            for ats_type, slugs in _SEEDS.items():
                for slug in slugs:
                    if self.add_board(ats_type, slug, discovered_via="seed", session=session):
                        new_count += 1
            for name, host, board_path, _region in WORKDAY_SEEDS:
                if self.add_board(
                    "workday", host.split(".")[0], board_path=board_path, host=host,
                    discovered_via="seed", company_name=name, session=session,
                ):
                    new_count += 1
            session.commit()
        log.info("company_universe.seeds_loaded", new=new_count)
        return new_count

    def backfill(self) -> dict[str, int]:
        """Import legacy ``company`` ATS rows and ATS URLs found in ``job``.

        Idempotent.  Legacy rows flagged ``blacklisted`` were set by the old
        404 auto-prune, so they come across as ``dead`` (rechecked after
        ``DEAD_RECHECK_INTERVAL``).  ``company`` itself is not modified.
        """
        counts: Counter[str] = Counter()
        with get_session() as session:
            # 1. Legacy company.ats_slug rows
            companies = session.exec(
                select(Company).where(Company.ats_type.isnot(None), Company.ats_slug.isnot(None))  # type: ignore[union-attr]
            ).all()
            for c in companies:
                assert c.ats_slug is not None
                assert c.ats_type is not None
                board_path, host = "", None
                if c.ats_type == "workday":
                    wd = _workday_board_for_tenant(session, c.ats_slug)
                    if wd is None:
                        counts["workday_missing_board"] += 1
                        continue
                    host, board_path = wd
                real_name = c.name if c.name and c.name.lower() != c.ats_slug.lower() else None
                if self.add_board(
                    c.ats_type, c.ats_slug, board_path=board_path, host=host,
                    discovered_via="backfill", company_id=c.id, company_name=real_name,
                    session=session,
                ):
                    counts["from_company"] += 1
                    if c.blacklisted:
                        session.flush()
                        b = session.exec(
                            select(AtsBoard).where(
                                AtsBoard.ats_type == c.ats_type,
                                AtsBoard.slug == c.ats_slug.lower(),
                                AtsBoard.board_path == board_path,
                            )
                        ).first()
                        if b is not None:
                            b.status = "dead"
                            b.consecutive_failures = 1
                            b.last_error = "legacy: blacklisted after 404"
                            b.last_checked_at = c.last_polled_at
                            session.add(b)
                            counts["legacy_dead"] += 1

            # 2. ATS URLs already stored on jobs (aggregator jobs included)
            rows = session.exec(
                select(Job.apply_url, Job.url, Company.name)
                .join(Company, Company.id == Job.company_id, isouter=True)
                .where(
                    or_(
                        Job.apply_url.contains("greenhouse.io"),  # type: ignore[union-attr]
                        Job.apply_url.contains("lever.co"),  # type: ignore[union-attr]
                        Job.apply_url.contains("ashbyhq.com"),  # type: ignore[union-attr]
                        Job.apply_url.contains("myworkdayjobs.com"),  # type: ignore[union-attr]
                        Job.url.contains("greenhouse.io"),  # type: ignore[attr-defined]
                        Job.url.contains("lever.co"),  # type: ignore[attr-defined]
                        Job.url.contains("ashbyhq.com"),  # type: ignore[attr-defined]
                        Job.url.contains("myworkdayjobs.com"),  # type: ignore[attr-defined]
                    )
                )
            ).all()
            for apply_url, url, company_name in rows:
                for u in (apply_url, url):
                    if u and self.add_from_url(
                        u, company_name=company_name, discovered_via="backfill", session=session
                    ):
                        counts["from_jobs"] += 1
            session.commit()

        counts["seeds"] = self.load_seed_lists()
        log.info("company_universe.backfill_done", **counts)
        return dict(counts)

    def ensure_initialized(self) -> None:
        """Run the backfill once if ``ats_board`` is empty."""
        with get_session() as session:
            has_rows = session.exec(select(AtsBoard.id).limit(1)).first() is not None
        if not has_rows:
            log.info("company_universe.first_run_backfill")
            self.backfill()

    # ── Validation ───────────────────────────────────────────────────────────

    async def validate(
        self,
        ats: list[str] | None = None,
        *,
        limit: int | None = None,
        include_all: bool = False,
        concurrency: int = _VALIDATE_CONCURRENCY,
    ) -> Counter[str]:
        """Probe boards that need checking and record the results.

        By default probes candidates plus dead boards due for a recheck;
        ``include_all=True`` re-probes every board.  Returns a Counter of
        resulting statuses (plus ``"error"`` for transient failures).
        """
        self.ensure_initialized()
        ats_types = [a for a in (ats or list(PROBE_ATS)) if a in PROBE_ATS]
        now = utcnow()
        with get_session() as session:
            rows = session.exec(select(AtsBoard).where(AtsBoard.ats_type.in_(ats_types))).all()  # type: ignore[attr-defined]
            targets = [
                _to_ref(b) for b in rows if include_all or is_due_for_validation(b, now)
            ]
        if limit:
            targets = targets[:limit]
        if not targets:
            return Counter()

        sem = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient(
            timeout=_VALIDATE_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (jobagent board validator)"},
        ) as client:

            async def _one(ref: BoardRef) -> tuple[int, BoardOutcome]:
                async with sem:
                    return ref.id, await probe_board(client, ref)

            results = await asyncio.gather(*(_one(t) for t in targets))

        self.record_outcomes(results)
        summary: Counter[str] = Counter()
        with get_session() as session:
            for board_id, res in results:
                if res.outcome == "error":
                    summary["error"] += 1
                else:
                    b = session.get(AtsBoard, board_id)
                    summary[b.status if b else "missing"] += 1
        log.info("company_universe.validated", probed=len(targets), **summary)
        return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workday_board_for_tenant(session: Session, tenant: str) -> tuple[str, str] | None:
    """Find ``(host, board_path)`` for a Workday tenant from seeds or job URLs."""
    from jobagent.discovery.collectors.workday import WORKDAY_SEEDS  # noqa: PLC0415

    for _name, host, board_path, _r in WORKDAY_SEEDS:
        if host.split(".")[0].lower() == tenant.lower():
            return host, board_path
    urls = session.exec(
        select(Job.apply_url, Job.url).where(
            or_(
                Job.apply_url.contains(f"{tenant}.wd"),  # type: ignore[union-attr]
                Job.url.contains(f"{tenant}.wd"),  # type: ignore[attr-defined]
            )
        ).limit(20)
    ).all()
    for apply_url, url in urls:
        for u in (apply_url, url):
            wd = extract_workday_board(u or "")
            if wd and wd[0] == tenant.lower():
                return wd[1], wd[2]
    return None


def _link_company(session: Session, board: AtsBoard) -> None:
    """Point *board* at the right ``company`` row once its real name is known.

    * If a company with the same normalised name exists, link to it.
    * Else, if the linked company is a slug placeholder (name == slug),
      rename it to the real name so dedup matches future jobs to it.
    """
    if not board.company_name:
        return
    norm = normalise_company(board.company_name)
    if not norm:
        return
    match = session.exec(select(Company).where(Company.name_norm == norm)).first()
    if match is not None:
        board.company_id = match.id
        return
    if board.company_id is not None:
        placeholder = session.get(Company, board.company_id)
        if placeholder is not None and placeholder.name.lower() == board.slug.lower():
            placeholder.name = board.company_name
            placeholder.name_norm = norm
            session.add(placeholder)
