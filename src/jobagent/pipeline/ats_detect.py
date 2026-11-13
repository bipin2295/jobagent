"""Detect ATS type and extract company slug/tenant from a job URL.

``detect_ats(url)`` returns the ATS name as a plain string.
``extract_ats_slug(url)`` returns ``(ats_type, slug)`` for the company-universe
flywheel, or ``None`` when no actionable slug can be extracted.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pattern table
# ---------------------------------------------------------------------------
# Each entry is (compiled_pattern, ats_type, slug_group).
# slug_group > 0  → capture group that holds the slug/tenant.
# slug_group == 0 → ATS is detected but no reusable slug is extractable
#                   (e.g. LinkedIn, mailto links).
# Patterns are tested in order; first match wins.
# ---------------------------------------------------------------------------

ATS_PATTERNS: list[tuple[re.Pattern[str], str, int]] = [
    # ── Greenhouse ──────────────────────────────────────────────────────────
    # https://boards.greenhouse.io/{slug}/jobs/{id}
    # https://job-boards.greenhouse.io/{slug}/jobs/{id}
    # https://job-boards.eu.greenhouse.io/{slug}/jobs/{id}  ← EU domain
    (
        re.compile(r"(?:boards|job-boards(?:\.eu)?)\.greenhouse\.io/([^/?#]+)", re.I),
        "greenhouse",
        1,
    ),
    # Company career pages that use Greenhouse embed via ?gh_jid= param
    # e.g. https://stripe.com/jobs/search?gh_jid=1234
    (
        re.compile(r"\?gh_jid=", re.I),
        "greenhouse",
        0,
    ),
    # ── Lever ───────────────────────────────────────────────────────────────
    # https://jobs.lever.co/{slug}/{uuid}
    (
        re.compile(r"jobs\.lever\.co/([^/?#]+)", re.I),
        "lever",
        1,
    ),
    # ── Ashby ───────────────────────────────────────────────────────────────
    # https://jobs.ashbyhq.com/{slug}
    # https://app.ashbyhq.com/jobs/{slug}
    (
        re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I),
        "ashby",
        1,
    ),
    (
        re.compile(r"app\.ashbyhq\.com/jobs/([^/?#]+)", re.I),
        "ashby",
        1,
    ),
    # ── Workday ─────────────────────────────────────────────────────────────
    # https://{tenant}.wd{n}.myworkdayjobs.com/…
    # https://{tenant}.myworkday.com/…  (newer Workday)
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.wd\d+\.myworkdayjobs\.com", re.I),
        "workday",
        1,
    ),
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.myworkday\.com", re.I),
        "workday",
        1,
    ),
    # ── SmartRecruiters ─────────────────────────────────────────────────────
    # https://jobs.smartrecruiters.com/{slug}/…
    # https://careers.smartrecruiters.com/{slug}/…
    (
        re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", re.I),
        "smartrecruiters",
        1,
    ),
    # ── Workable ────────────────────────────────────────────────────────────
    # https://apply.workable.com/{slug}/  (must come before subdomain pattern)
    # https://{slug}.workable.com/
    (
        re.compile(r"apply\.workable\.com/([^/?#]+)", re.I),
        "workable",
        1,
    ),
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.workable\.com", re.I),
        "workable",
        1,
    ),
    # ── Recruitee ───────────────────────────────────────────────────────────
    # https://{slug}.recruitee.com/
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.recruitee\.com", re.I),
        "recruitee",
        1,
    ),
    # ── Personio ────────────────────────────────────────────────────────────
    # https://{slug}.jobs.personio.de/
    # https://{slug}.jobs.personio.com/
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.jobs\.personio\.(?:de|com|es|fr|at)", re.I),
        "personio",
        1,
    ),
    # ── TeamTailor ──────────────────────────────────────────────────────────
    # https://{slug}.teamtailor.com/
    # https://careers.teamtailor.com/{slug}/  (hosted careers pages)
    (
        re.compile(r"careers\.teamtailor\.com/([^/?#]+)", re.I),
        "teamtailor",
        1,
    ),
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.teamtailor\.com", re.I),
        "teamtailor",
        1,
    ),
    # ── BambooHR ────────────────────────────────────────────────────────────
    # https://{slug}.bamboohr.com/careers/
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.bamboohr\.com", re.I),
        "bamboohr",
        1,
    ),
    # ── iCIMS ───────────────────────────────────────────────────────────────
    # https://{tenant}.icims.com/jobs/…
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.icims\.com", re.I),
        "icims",
        1,
    ),
    # ── SAP SuccessFactors ──────────────────────────────────────────────────
    # https://{tenant}.successfactors.com/
    # https://{tenant}.successfactors.eu/
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.successfactors\.(?:com|eu)", re.I),
        "successfactors",
        1,
    ),
    # ── Oracle Taleo ────────────────────────────────────────────────────────
    # https://{tenant}.taleo.net/
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.taleo\.net", re.I),
        "taleo",
        1,
    ),
    # ── Phenom ──────────────────────────────────────────────────────────────
    # https://{tenant}.phenompeople.com/
    # https://{tenant}.phenom.io/
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.phenompeople\.com", re.I),
        "phenom",
        1,
    ),
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.phenom\.io", re.I),
        "phenom",
        1,
    ),
    # ── Jobvite ─────────────────────────────────────────────────────────────
    # https://jobs.jobvite.com/{slug}/
    # https://app.jobvite.com/CompanyJobs/{slug}/
    (
        re.compile(r"jobs\.jobvite\.com/([^/?#]+)", re.I),
        "jobvite",
        1,
    ),
    (
        re.compile(r"app\.jobvite\.com/CompanyJobs/([^/?#]+)", re.I),
        "jobvite",
        1,
    ),
    # ── Avature ─────────────────────────────────────────────────────────────
    # https://{tenant}.avature.net/
    (
        re.compile(r"([a-zA-Z0-9_-]+)\.avature\.net", re.I),
        "avature",
        1,
    ),
    # ── LinkedIn (no reusable ATS slug) ─────────────────────────────────────
    (
        re.compile(r"linkedin\.com/jobs", re.I),
        "linkedin",
        0,
    ),
    # ── Email (mailto: links) ────────────────────────────────────────────────
    (
        re.compile(r"^mailto:", re.I),
        "email",
        0,
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_ats(url: str) -> str:
    """Return the ATS type string for *url*, or ``"unknown"`` if unrecognised.

    Examples::

        >>> detect_ats("https://boards.greenhouse.io/stripe/jobs/123")
        'greenhouse'
        >>> detect_ats("https://jobs.lever.co/netflix/abc-def")
        'lever'
        >>> detect_ats("https://example.com/jobs")
        'unknown'
    """
    if not url:
        return "unknown"
    for pattern, ats_type, _group in ATS_PATTERNS:
        if pattern.search(url):
            return ats_type
    return "unknown"


def extract_ats_slug(url: str) -> tuple[str, str] | None:
    """Return ``(ats_type, slug)`` extracted from *url*, or ``None``.

    Returns ``None`` when:

    - No ATS pattern matches the URL.
    - The matched ATS has no extractable slug (e.g. LinkedIn, email).

    The returned *slug* is lowercased for consistency.

    Examples::

        >>> extract_ats_slug("https://boards.greenhouse.io/stripe/jobs/123")
        ('greenhouse', 'stripe')
        >>> extract_ats_slug("https://jobs.lever.co/netflix/abc-def")
        ('lever', 'netflix')
        >>> extract_ats_slug("https://www.linkedin.com/jobs/view/123")
        None
    """
    if not url:
        return None
    for pattern, ats_type, slug_group in ATS_PATTERNS:
        match = pattern.search(url)
        if match is None:
            continue
        if slug_group == 0:
            # ATS detected but no actionable slug available
            return None
        slug = match.group(slug_group).lower().rstrip("/")
        if slug:
            return (ats_type, slug)
        return None
    return None


# ---------------------------------------------------------------------------
# Workday board extraction
# ---------------------------------------------------------------------------

_WORKDAY_HOST_RE = re.compile(
    r"https?://(([a-z0-9_-]+)\.wd\d+\.myworkdayjobs\.com)(/[^?#]*)?", re.I
)
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)
_WORKDAY_NON_BOARD = {"job", "jobs", "details", "apply", "login", "wday", "cxs"}


def extract_workday_board(url: str) -> tuple[str, str, str] | None:
    """Return ``(tenant, host, board_path)`` for a Workday job/board URL.

    Workday needs all three to call its jobs API
    (``POST https://{host}/wday/cxs/{tenant}/{board}/jobs``).

    Examples::

        >>> extract_workday_board(
        ...     "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x")
        ('nvidia', 'nvidia.wd5.myworkdayjobs.com', 'NVIDIAExternalCareerSite')
        >>> extract_workday_board("https://acme.wd3.myworkdayjobs.com/") is None
        True

    The tenant and host are lowercased; the board path keeps its case
    (Workday board paths are case-sensitive).
    """
    if not url:
        return None
    m = _WORKDAY_HOST_RE.match(url.strip())
    if not m:
        return None
    host, tenant, path = m.group(1).lower(), m.group(2).lower(), m.group(3) or ""
    segments = [s for s in path.split("/") if s]
    # API-style URL: /wday/cxs/{tenant}/{board}/...
    if len(segments) >= 4 and segments[0].lower() == "wday" and segments[1].lower() == "cxs":
        return tenant, host, segments[3]
    for seg in segments:
        if _LOCALE_RE.match(seg):
            continue
        if seg.lower() in _WORKDAY_NON_BOARD:
            return None
        return tenant, host, seg
    return None
