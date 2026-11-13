"""Normalise a RawJob into a Job SQLModel instance.

Responsibilities:
- HTML → Markdown conversion for descriptions.
- Company/title string normalisation for consistent deduplication.
- Country and remote-type inference from raw location strings.
- Employment-type standardisation.
- SHA-256 content hash derivation (first 16 hex chars).
- ATS type detection from the apply URL.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Make a datetime timezone-aware (UTC). Pass through None and already-aware datetimes."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
from typing import TYPE_CHECKING

from markdownify import markdownify as _html_to_md

from jobagent.db.models import Job
from jobagent.pipeline.ats_detect import detect_ats

# RawJob is only needed for type annotations; importing it at runtime would
# trigger jobagent.discovery.__init__, which re-imports this module → circular.
if TYPE_CHECKING:
    from jobagent.discovery.base import RawJob

# ---------------------------------------------------------------------------
# Country keyword map  (ISO 3166-1 alpha-2 → list of lowercased keywords)
# ---------------------------------------------------------------------------
# Keywords are matched as substrings of the lowercased location string.
# The map is ordered so that more-specific entries (city names that are unique
# to one country) are checked before broad terms that could appear in longer
# strings.
# ---------------------------------------------------------------------------

COUNTRY_KEYWORDS: dict[str, list[str]] = {
    # Europe
    "GB": [
        "united kingdom", " uk ", "(uk)", "uk,", "uk/", ", uk",
        "england", "scotland", "wales", "london", "manchester",
        "edinburgh", "glasgow", "birmingham", "bristol", "leeds",
        "sheffield", "liverpool", "cambridge", "oxford",
    ],
    "DE": [
        "germany", "deutschland",
        "berlin", "munich", "münchen", "hamburg", "frankfurt",
        "cologne", "köln", "düsseldorf", "stuttgart", "dresden",
        "dortmund", "essen", "hannover", "nuremberg", "nürnberg",
    ],
    "NL": [
        "netherlands", "nederland", "the netherlands", "holland",
        "amsterdam", "rotterdam", "eindhoven", "utrecht", "hague",
        "den haag", "delft", "groningen", "tilburg",
    ],
    "IE": [
        "ireland", "eire", "republic of ireland",
        "dublin", "cork", "limerick", "galway", "waterford",
    ],
    "FR": [
        "france",
        "paris", "lyon", "bordeaux", "marseille", "toulouse",
        "nice", "nantes", "strasbourg", "montpellier", "lille",
    ],
    "ES": [
        "spain", "españa",
        "madrid", "barcelona", "seville", "sevilla", "valencia",
        "bilbao", "málaga", "malaga", "zaragoza",
    ],
    "PT": [
        "portugal",
        "lisbon", "lisboa", "porto", "braga",
    ],
    "PL": [
        "poland", "polska",
        "warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław",
        "gdansk", "gdańsk", "poznan", "poznań", "katowice",
    ],
    "SE": [
        "sweden", "sverige",
        "stockholm", "gothenburg", "göteborg", "malmö", "malmo", "Uppsala",
    ],
    "DK": [
        "denmark", "danmark",
        "copenhagen", "københavn", "aarhus", "odense",
    ],
    "FI": [
        "finland", "suomi",
        "helsinki", "tampere", "espoo", "turku", "oulu",
    ],
    "AT": [
        "austria", "österreich",
        "vienna", "wien", "graz", "linz", "salzburg", "innsbruck",
    ],
    "BE": [
        "belgium", "belgique", "belgië",
        "brussels", "bruxelles", "antwerp", "antwerpen", "ghent", "gent",
        "bruges", "brugge", "liège",
    ],
    "LU": ["luxembourg", "luxemburg"],
    "IT": [
        "italy", "italia",
        "rome", "roma", "milan", "milano", "florence", "firenze",
        "naples", "napoli", "turin", "torino", "bologna", "venice", "venezia",
    ],
    "CZ": [
        "czech", "czechia", "czech republic",
        "prague", "praha", "brno", "ostrava",
    ],
    "EE": ["estonia", "eesti", "tallinn", "tartu"],
    "LT": ["lithuania", "lietuva", "vilnius", "kaunas"],
    "SI": ["slovenia", "slovenija", "ljubljana", "maribor"],
    "CY": ["cyprus", "kypros", "nicosia", "lefkosia", "limassol"],
    "MT": ["malta", "valletta"],
    "HU": ["hungary", "magyarország", "budapest", "debrecen"],
    "RO": ["romania", "românia", "bucharest", "bucurești", "cluj", "timișoara"],
    "BG": ["bulgaria", "bălgarija", "sofia", "plovdiv", "varna"],
    "HR": ["croatia", "hrvatska", "zagreb", "split"],
    "SK": ["slovakia", "slovensko", "bratislava", "košice"],
    "LV": ["latvia", "latvija", "riga"],
    "GR": ["greece", "hellas", "ελλάδα", "athens", "athina", "thessaloniki"],
    "CH": [
        "switzerland", "schweiz", "suisse", "svizzera",
        "zurich", "zürich", "geneva", "genève", "bern", "basel",
        "lausanne",
    ],
    "NO": ["norway", "norge", "oslo", "bergen", "stavanger"],
    "IS": ["iceland", "ísland", "reykjavik", "reykjavík"],
    # Middle East
    "AE": [
        "uae", "united arab emirates",
        "dubai", "abu dhabi", "sharjah",
    ],
    "SA": ["saudi arabia", "ksa", "riyadh", "jeddah", "mecca", "medina"],
    "QA": ["qatar", "doha"],
    # Asia-Pacific
    "SG": ["singapore"],
    "AU": [
        "australia",
        "sydney", "melbourne", "brisbane", "perth", "adelaide",
        "canberra", "gold coast",
    ],
    "NZ": ["new zealand", "auckland", "wellington", "christchurch"],
    "JP": ["japan", "tokyo", "osaka", "kyoto", "yokohama", "nagoya"],
    "HK": ["hong kong", "hongkong"],
    "MY": ["malaysia", "kuala lumpur", "kl", "penang", "johor"],
    "TH": ["thailand", "bangkok", "chiang mai"],
    "VN": ["vietnam", "ho chi minh", "hanoi", "hà nội"],
    # South Asia
    "IN": [
        "india",
        "bangalore", "bengaluru", "mumbai", "bombay",
        "delhi", "new delhi", "hyderabad", "chennai", "madras",
        "pune", "kolkata", "calcutta", "ahmedabad", "noida", "gurgaon",
    ],
}

# ---------------------------------------------------------------------------
# Internal regex helpers
# ---------------------------------------------------------------------------

_LEGAL_SUFFIX_RE = re.compile(
    r"\s*\b(?:inc\.?|incorporated|ltd\.?|limited|llc|llp|l\.l\.c\."
    r"|corp\.?|corporation|gmbh|ag|b\.v\.|n\.v\.|s\.a\.|srl|s\.r\.l\."
    r"|plc|pty\.?|pvt\.?)\b\.?\s*$",
    re.IGNORECASE,
)

_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

_EMPLOYMENT_MAP: dict[str, str] = {
    "full_time": "full_time",
    "fulltime": "full_time",
    "full time": "full_time",
    "full-time": "full_time",
    "ft": "full_time",
    "permanent": "full_time",
    "contract": "contract",
    "contractor": "contract",
    "freelance": "contract",
    "temporary": "contract",
    "temp": "contract",
    "part_time": "part_time",
    "parttime": "part_time",
    "part time": "part_time",
    "part-time": "part_time",
    "pt": "part_time",
}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def normalise_company(name: str) -> str:
    """Return a normalised, lowercase form of a company name.

    Steps:
    1. Lowercase.
    2. Strip trailing legal-entity suffixes (Inc, Ltd, GmbH, …) iteratively.
    3. Replace remaining punctuation with spaces.
    4. Collapse whitespace.

    Examples::

        >>> normalise_company("Stripe, Inc.")
        'stripe'
        >>> normalise_company("Acme GmbH")
        'acme'
        >>> normalise_company("Example Corp Ltd.")
        'example'
    """
    if not name:
        return ""
    result = name.lower().strip()
    # Strip legal suffixes iteratively (handles "Corp. Ltd.")
    prev: str | None = None
    while prev != result:
        prev = result
        result = _LEGAL_SUFFIX_RE.sub("", result).strip()
    # Punctuation → space
    result = _PUNCT_RE.sub(" ", result)
    # Collapse whitespace
    result = _WHITESPACE_RE.sub(" ", result).strip()
    return result


def normalise_title(title: str) -> str:
    """Return a normalised, lowercase form of a job title.

    Lowercases and collapses internal whitespace.

    Example::

        >>> normalise_title("  Senior  Data  Engineer  ")
        'senior data engineer'
    """
    if not title:
        return ""
    return _WHITESPACE_RE.sub(" ", title.lower()).strip()


def detect_country(
    location_raw: str | None,
    country_hint: str | None = None,
) -> str | None:
    """Return an ISO 3166-1 alpha-2 country code, or ``None``.

    Priority:
    1. *country_hint* if it looks like a valid 2-letter ISO code.
    2. Keyword matching against *location_raw*.

    The function never raises; unrecognised locations return ``None``.
    """
    if country_hint and len(country_hint.strip()) == 2:
        return country_hint.strip().upper()
    if not location_raw:
        return None
    lower = location_raw.lower()
    for iso_code, keywords in COUNTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return iso_code
    return None


def detect_remote_type(
    location_raw: str | None,
    remote_hint: str | None = None,
) -> str | None:
    """Return "remote", "hybrid", "onsite", or ``None``.

    Priority:
    1. *remote_hint* if it's already a valid canonical value.
    2. Keyword matching against *location_raw*.
    3. ``None`` when *location_raw* is absent and no hint is given.
    """
    if remote_hint in ("remote", "hybrid", "onsite"):
        return remote_hint
    if not location_raw:
        return None
    lower = location_raw.lower()
    if "remote" in lower:
        return "remote"
    if "hybrid" in lower:
        return "hybrid"
    return "onsite"


def normalise_employment_type(employment_type: str | None) -> str | None:
    """Map a free-form employment type string to a canonical value.

    Returns one of ``"full_time"``, ``"contract"``, ``"part_time"``, or
    ``None`` if the input is unrecognised or absent.
    """
    if not employment_type:
        return None
    return _EMPLOYMENT_MAP.get(employment_type.lower().strip())


def _markdownify(html: str | None) -> str | None:
    """Convert HTML to Markdown.  Returns ``None`` gracefully on empty/None input."""
    if not html:
        return None
    try:
        return _html_to_md(html, heading_style="ATX", strip=["script", "style"])
    except Exception:
        return None


def _content_hash(company_norm: str, title_norm: str, country: str | None) -> str:
    """Return the first 16 hex characters of SHA-256(company|title|country)."""
    raw = f"{company_norm}|{title_norm}|{country or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def normalise(raw: RawJob) -> Job:
    """Convert a :class:`~jobagent.discovery.base.RawJob` into a :class:`~jobagent.db.models.Job`.

    The returned ``Job`` instance has ``company_id`` set to ``None``; callers
    (typically ``upsert_job``) are responsible for resolving and assigning the
    correct company FK after this function returns.

    All fields are populated from *raw* with no database I/O.
    """
    company_norm = normalise_company(raw.company)
    title_norm = normalise_title(raw.title)
    country = detect_country(raw.location_raw, raw.country)
    remote_type = detect_remote_type(raw.location_raw, raw.remote_type)

    # Description: prefer pre-converted markdown; fall back to HTML→MD
    description_md = raw.description_md or _markdownify(raw.description_html)

    # ATS type: check apply URL first, fall back to canonical job URL
    ats_url = raw.apply_url or raw.url
    ats_type_raw = detect_ats(ats_url)
    ats_type: str | None = ats_type_raw if ats_type_raw != "unknown" else None

    content_hash = _content_hash(company_norm, title_norm, country)
    employment_type = normalise_employment_type(raw.employment_type)

    return Job(
        # Identification
        source=raw.source,
        source_job_id=raw.source_job_id,
        url=raw.url,
        apply_url=raw.apply_url,
        ats_type=ats_type,
        # Content
        title=raw.title,
        title_norm=title_norm,
        country=country,
        city=raw.city,
        remote_type=remote_type,
        description_md=description_md,
        # Dates & compensation (ensure timezone-aware for SQLModel's UTCDateTime)
        posted_at=_ensure_utc(raw.posted_at),
        salary_min=raw.salary_min,
        salary_max=raw.salary_max,
        currency=raw.currency,
        employment_type=employment_type,
        # Dedup fingerprint
        content_hash=content_hash,
        # Timestamps
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
        # company_id resolved later by upsert_job
    )
