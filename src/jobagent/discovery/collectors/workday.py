"""Workday public jobs API collector.

Each Workday board exposes a JSON search API at:
  POST https://{host}/wday/cxs/{tenant}/{board}/jobs
  Body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "..."}

``limit`` must be ≤ 20 (Workday answers HTTP 400 otherwise), so results are
paginated.  Most tenants need no CSRF token; if one answers 401/403 we load
the board page once to harvest it and retry.

Boards (tenant + host + board path) come from the ``ats_board`` registry.
``WORKDAY_SEEDS`` below is loaded into it as ``candidate`` rows; the
validator / first poll decides which are real (404 = unknown board,
422 = unknown tenant → ``dead``).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob
from jobagent.discovery.boards import (
    WORKDAY_PAGE_LIMIT,
    BoardOutcome,
    BoardRef,
    outcome_from_status,
    workday_api_url,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Seed list: (display_name, host, board_path, region_hint)
# host     = the myworkdayjobs.com subdomain, e.g. "hsbc.wd3.myworkdayjobs.com"
# board    = the job-board slug in the URL, e.g. "HSBCGlobalCareers"
# Unverified guesses are fine here: they enter ats_board as candidates.
# ---------------------------------------------------------------------------

WORKDAY_SEEDS: list[tuple[str, str, str, str]] = [
    # ── Fintech / banking (EU + APAC) ──────────────────────────────────────
    ("HSBC",             "hsbc.wd3.myworkdayjobs.com",           "HSBCGlobalCareers",          "GB,HK,SG,IN"),
    ("Deutsche Bank",    "deutschebank.wd3.myworkdayjobs.com",   "DB_External_Careers",        "DE"),
    ("ING",              "ing.wd3.myworkdayjobs.com",            "ING",                        "NL,DE"),
    ("Commerzbank",      "commerzbank.wd3.myworkdayjobs.com",    "cb_extern",                  "DE"),
    ("ABN AMRO",         "abnamro.wd3.myworkdayjobs.com",        "ABN_AMRO_Careers",           "NL"),
    ("Rabobank",         "rabobank.wd3.myworkdayjobs.com",       "Rabobank_External",          "NL"),
    ("Emirates NBD",     "emiratesnbd.wd3.myworkdayjobs.com",    "ENBDCareers",                "AE"),
    ("First Abu Dhabi",  "fab.wd3.myworkdayjobs.com",            "ExternalCareers",            "AE"),
    ("DBS Bank",         "dbs.wd3.myworkdayjobs.com",            "DBS_Careers",                "SG,IN"),
    ("Standard Chartered","standardchartered.wd3.myworkdayjobs.com","StandardChartered",       "GB,SG,IN,AE"),
    # ── Tech / Cloud (EU + APAC) ───────────────────────────────────────────
    ("SAP",              "sap.wd3.myworkdayjobs.com",            "SAP_External_Careers",       "DE,IN"),
    ("Siemens",          "siemens.wd3.myworkdayjobs.com",        "Siemens",                    "DE"),
    ("Bosch",            "bosch.wd3.myworkdayjobs.com",          "Bosch_Extern",               "DE,IN"),
    ("Zalando",          "zalando.wd3.myworkdayjobs.com",        "zalando_DE_jobs",            "DE"),
    ("Amadeus",          "amadeus.wd3.myworkdayjobs.com",        "Amadeus",                    "ES,FR,IN"),
    ("Criteo",           "criteo.wd1.myworkdayjobs.com",         "Criteo_Jobs",                "FR,DE"),
    ("Booking.com",      "booking.wd12.myworkdayjobs.com",       "booking",                    "NL,DE"),
    ("Grab",             "grab.wd5.myworkdayjobs.com",           "Careers",                    "SG,MY"),
    ("Sea",              "sea.wd5.myworkdayjobs.com",            "SeaLimited_Careers",         "SG"),
    ("Shopee",           "shopee.wd5.myworkdayjobs.com",         "Careers",                    "SG"),
    ("Goto",             "goto.wd5.myworkdayjobs.com",           "Careers",                    "ID,SG"),
    # ── Canada (Global Talent Stream) ──────────────────────────────────────
    ("RBC",              "rbc.wd3.myworkdayjobs.com",            "RBC_Careers",                "CA"),
    ("TD Bank",          "td.wd3.myworkdayjobs.com",             "TD_Careers",                 "CA"),
    ("Scotiabank",       "scotiabank.wd3.myworkdayjobs.com",     "Scotiabank_Careers",         "CA"),
    ("BMO",              "bmo.wd3.myworkdayjobs.com",            "BMO_Careers",                "CA"),
    ("CIBC",             "cibc.wd3.myworkdayjobs.com",           "CIBC_Careers",               "CA"),
    ("Rogers",           "rogers.wd3.myworkdayjobs.com",         "Rogers_Careers",             "CA"),
    ("Bell",             "bell.wd3.myworkdayjobs.com",           "Bell_Careers",               "CA"),
    ("Telus",            "telus.wd3.myworkdayjobs.com",          "TELUS_Careers",              "CA"),
    ("Manulife",         "manulife.wd3.myworkdayjobs.com",       "Manulife_Careers",           "CA"),
    ("Sun Life",         "sunlife.wd3.myworkdayjobs.com",        "SunLife_Careers",            "CA"),
    ("Shopify",          "shopify.wd5.myworkdayjobs.com",        "Shopify",                    "CA,remote"),
    ("Lightspeed",       "lightspeed.wd3.myworkdayjobs.com",     "Lightspeed_Careers",         "CA"),
    # ── Consulting / Services (global) ─────────────────────────────────────
    ("Accenture",        "accenture.wd3.myworkdayjobs.com",      "Accenture_Careers",          "IN,DE,SG,AE,CA"),
    ("McKinsey",         "mckinsey.wd5.myworkdayjobs.com",       "McKinseyAndCompany",         "DE,IN,SG,AE"),
    ("BCG",              "bcg.wd3.myworkdayjobs.com",            "BCGCareers",                 "DE,IN,SG,AE"),
    ("Capgemini",        "capgemini.wd3.myworkdayjobs.com",      "Capgemini",                  "FR,IN,DE"),
    ("Infosys",          "infosys.wd3.myworkdayjobs.com",        "Infosys_External",           "IN"),
    ("Wipro",            "wipro.wd3.myworkdayjobs.com",          "Wipro",                      "IN"),
    ("HCL",              "hcl.wd3.myworkdayjobs.com",            "HCLCareers",                 "IN"),
    ("TCS",              "tcs.wd5.myworkdayjobs.com",            "TCSCareers",                 "IN"),
    # ── E-commerce / Logistics ─────────────────────────────────────────────
    ("Amazon",           "amazon.wd5.myworkdayjobs.com",         "Amazon_Jobs",                "DE,SG,IN"),
    ("Flipkart",         "flipkart.wd3.myworkdayjobs.com",       "Flipkart",                   "IN"),
    ("Myntra",           "myntra.wd5.myworkdayjobs.com",         "Myntra",                     "IN"),
    # ── Media / Telecom ────────────────────────────────────────────────────
    ("Spotify",          "spotify.wd5.myworkdayjobs.com",        "New_Positions",              "SE,DE,GB"),
    ("Netflix",          "netflix.wd1.myworkdayjobs.com",        "netflix",                    "remote"),
    ("Mastercard",       "mastercard.wd1.myworkdayjobs.com",     "Mastercard",                 "DE,SG,IN,AE"),
    ("Visa",             "visa.wd5.myworkdayjobs.com",           "visa_career",                "DE,SG,IN,AE"),
]

_MAX_PAGES_PER_KEYWORD = 5  # 5 × 20 = 100 results per keyword per board
_MAX_KEYWORDS = 5
_CONCURRENCY = 8
_TIMEOUT = 15.0
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


_RELATIVE_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


def _parse_relative_posted(text: str, now: datetime | None = None) -> datetime | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    m = _RELATIVE_RE.search(text or "")
    if not m:
        return None
    now = now or datetime.now(UTC)
    word = m.group(1).lower()
    days = 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2))
    return now - timedelta(days=days)


def _matches(title: str, keywords: list[str]) -> bool:
    """Workday search is full-text, so re-check the title like other collectors."""
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def _slug_from_host(host: str) -> str:
    """Extract the tenant slug from the host, e.g. 'hsbc.wd3.myworkdayjobs.com' → 'hsbc'."""
    return host.split(".")[0]


class WorkdayCollector(CollectorBase):
    name = "workday"
    source = "workday"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        from jobagent.discovery.company_universe import CompanyUniverse  # noqa: PLC0415

        title_filter: list[str] = search_cfg.get("titles", [])
        keywords: list[str] = title_filter[:_MAX_KEYWORDS] or ["data engineer"]
        universe = CompanyUniverse()
        boards = universe.get_boards("workday")
        if not boards:
            log.info("workday.no_boards")
            return []
        log.info("workday.start", boards=len(boards), keywords=keywords)

        sem = asyncio.Semaphore(_CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            batch_results = await asyncio.gather(
                *(self._fetch_board(client, sem, b, keywords, title_filter) for b in boards)
            )

        results: list[RawJob] = []
        outcomes: list[tuple[int, BoardOutcome]] = []
        for board, (jobs, outcome) in zip(boards, batch_results, strict=False):
            results.extend(jobs)
            outcomes.append((board.id, outcome))
        try:
            universe.record_outcomes(outcomes)
        except Exception:
            log.exception("workday.record_outcomes_failed")
        log.info("workday.done", boards=len(boards), total=len(results))
        return results

    async def _post(
        self, client: httpx.AsyncClient, board: BoardRef, body: dict[str, Any], csrf: str
    ) -> httpx.Response:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if csrf:
            headers["X-Calypso-CSRF-Token"] = csrf
        return await client.post(workday_api_url(board), json=body, headers=headers)

    async def _csrf_token(self, client: httpx.AsyncClient, board: BoardRef) -> str:
        try:
            r = await client.get(f"https://{board.host}/en-US/{board.board_path}", timeout=10)
        except httpx.HTTPError:
            return ""
        for key, val in r.cookies.items():
            if "csrf" in key.lower():
                return val
        return r.headers.get("X-CSRF-Token", "")

    async def _fetch_board(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        board: BoardRef,
        keywords: list[str],
        title_filter: list[str] | None = None,
    ) -> tuple[list[RawJob], BoardOutcome]:
        base_url = f"https://{board.host}"
        jobs: list[RawJob] = []
        seen: set[str] = set()
        csrf = ""
        board_total: int | None = None

        async with sem:
            # Health check + board size: empty search, first page only
            try:
                body = {"appliedFacets": {}, "limit": WORKDAY_PAGE_LIMIT, "offset": 0, "searchText": ""}
                r = await self._post(client, board, body, csrf)
                if r.status_code in (401, 403):
                    csrf = await self._csrf_token(client, board)
                    r = await self._post(client, board, body, csrf)
            except httpx.HTTPError as exc:
                return [], BoardOutcome("error", error=str(exc)[:200], polled=True)
            o = outcome_from_status(r.status_code, not_found_codes=(404, 422))
            if o is not None:
                log.debug("workday.board_unavailable", host=board.host, board=board.board_path,
                          status=r.status_code)
                return [], BoardOutcome(o, error=f"HTTP {r.status_code}", polled=True)
            try:
                board_total = int(r.json().get("total") or 0)
            except (ValueError, AttributeError):
                board_total = None

            for keyword in keywords:
                for page in range(_MAX_PAGES_PER_KEYWORD):
                    body = {
                        "appliedFacets": {},
                        "limit": WORKDAY_PAGE_LIMIT,
                        "offset": page * WORKDAY_PAGE_LIMIT,
                        "searchText": keyword,
                    }
                    try:
                        r = await self._post(client, board, body, csrf)
                    except httpx.HTTPError as exc:
                        log.debug("workday.fetch_error", host=board.host, keyword=keyword,
                                  error=str(exc)[:60])
                        break
                    if r.status_code != 200:
                        log.debug("workday.search_error", host=board.host, status=r.status_code)
                        break
                    data = r.json()
                    postings = data.get("jobPostings", [])
                    for posting in postings:
                        key = posting.get("externalPath") or posting.get("title", "")
                        if key in seen:
                            continue
                        seen.add(key)
                        if not _matches(posting.get("title", ""), title_filter or []):
                            continue
                        try:
                            jobs.append(
                                _parse_posting(posting, board.host or "", board.board_path,
                                               base_url, board.company_name)
                            )
                        except Exception as exc:
                            log.debug("workday.parse_error", host=board.host, error=str(exc)[:50])
                    total = int(data.get("total") or 0)
                    if len(postings) < WORKDAY_PAGE_LIMIT or (page + 1) * WORKDAY_PAGE_LIMIT >= total:
                        break

        if jobs:
            log.debug("workday.board_done", host=board.host, jobs=len(jobs))
        return jobs, BoardOutcome(
            "ok", job_count=board_total, matched_count=len(jobs), polled=True
        )


def _parse_posting(
    posting: dict[str, Any],
    host: str,
    board: str,
    base_url: str,
    company_name: str | None = None,
) -> RawJob:
    """Convert a Workday job posting dict into a RawJob."""
    external_path = posting.get("externalPath", "")
    job_url = f"{base_url}/en-US/{board}{external_path}" if external_path else base_url

    title = posting.get("title", "")

    # Location: postedLocations (detail payload) or locationsText (search payload)
    locations = posting.get("postedLocations", [])
    location_raw: str | None = None
    if locations:
        location_raw = locations[0].get("location", {}).get("descriptor") if isinstance(locations[0], dict) else str(locations[0])
    location_raw = location_raw or posting.get("locationsText") or None

    # Country code from location
    country: str | None = None
    if location_raw:
        for iso, kw in [("DE","Germany"),("NL","Netherlands"),("GB","United Kingdom"),
                        ("SG","Singapore"),("IN","India"),("AE","UAE"),("AU","Australia"),
                        ("FR","France"),("US","United States"),("ID","Indonesia")]:
            if kw.lower() in (location_raw or "").lower():
                country = iso
                break

    company = company_name or _slug_from_host(host)

    # Try to extract a date (ISO, or Workday's relative "Posted 3 Days Ago")
    posted_at: datetime | None = None
    for date_field in ["postedOn", "startDate", "closingDate"]:
        raw_date = posting.get(date_field)
        if raw_date:
            try:
                posted_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                break
            except (ValueError, AttributeError):
                posted_at = _parse_relative_posted(raw_date)
                if posted_at:
                    break

    return RawJob(
        source="workday",
        source_job_id=external_path or title,
        url=job_url,
        apply_url=job_url,
        company=company,
        title=title,
        location_raw=location_raw,
        country=country,
        posted_at=posted_at,
        extra={
            "host": host,
            "board": board,
            "external_path": external_path,
            "remote": posting.get("remoteType", ""),
        },
    )
