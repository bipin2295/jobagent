"""Parse master_resume.txt into a MasterProfile.

The file format uses:
  == SECTION NAME ==   to delimit sections
  [JOB] / [ID]         to delimit job blocks and bullet blocks
  KEY: value           for single-line key-value pairs
  KEY:                 followed by indented lines for multi-line values
  # comment            lines ignored
  ---...---            separators within the EXPERIENCE section
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from jobagent.profile.schema import (
    Bullet,
    BulletVariant,
    CompBand,
    ContactInfo,
    Education,
    JobExperience,
    Language,
    LocationInfo,
    MasterProfile,
    SkillsBlock,
    SkillYears,
    Summary,
    TailoringRecipe,
)


# ── Top-level entry point ─────────────────────────────────────────────────────

def import_resume(source: Path | str | None = None) -> MasterProfile:
    """Parse *source* (defaults to config.settings.profile_source) and return a MasterProfile."""
    if source is None:
        from jobagent.config import settings  # noqa: PLC0415
        source = settings.profile_source

    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Master resume not found: {path}")

    text = path.read_text(encoding="utf-8")
    return _parse(text, source_path=str(path))


# ── Core parser ───────────────────────────────────────────────────────────────

def _parse(text: str, source_path: str = "") -> MasterProfile:
    sections = _split_sections(text)

    contact = _parse_contact(sections.get("CONTACT", []))
    skill_years_obj, not_yet = _parse_skill_years(sections.get("SKILL YEARS", []))
    titles = _parse_title_variants(sections.get("TITLE VARIANTS", []))
    summaries = _parse_summaries(sections.get("SUMMARY VARIANTS", []))
    skills_blocks, not_yet_from_skills = _parse_skills(sections.get("SKILLS (FULL INVENTORY)", []))
    # Merge NOT_YET from both skill years and the skills section
    all_not_yet = _merge_not_yet(not_yet, not_yet_from_skills)
    experience = _parse_experience(sections.get("EXPERIENCE", []))
    education = _parse_education(sections.get("EDUCATION", []))
    languages = _parse_languages(sections.get("LANGUAGES", []))
    work_auth = _parse_work_auth(
        sections.get("WORK AUTHORIZATION & RELOCATION (BY COUNTRY)", [])
    )
    recipes = _parse_recipes(sections.get("TAILORING RECIPES", []))
    open_items = _parse_open_items(
        sections.get("OPEN ITEMS / VERIFY BEFORE SENDING", [])
    )
    file_naming = _parse_file_naming(sections.get("FILE NAMING", []))

    content_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
    meta: dict[str, Any] = {
        "source": source_path,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash,
    }

    return MasterProfile(
        meta=meta,
        contact=contact,
        skill_years=skill_years_obj,
        not_yet=all_not_yet,
        titles=titles,
        summaries=summaries,
        skills=skills_blocks,
        experience=experience,
        education=education,
        languages=languages,
        work_authorization=work_auth,
        tailoring_recipes=recipes,
        file_naming=file_naming or "Bipin_Singh_Data_Engineer_{Company}.pdf",
        open_items=open_items,
    )


# ── Section splitter ──────────────────────────────────────────────────────────

def _split_sections(text: str) -> dict[str, list[str]]:
    """Split text into sections by == NAME == headers. Returns dict of name → lines."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []

    for raw_line in text.splitlines():
        # Strip comment lines but keep blank lines (they may separate blocks)
        line = raw_line.rstrip()
        if line.lstrip().startswith("#"):
            continue
        # Strip the banner ### lines
        if re.match(r'^#{10,}', line):
            continue

        m = re.match(r'^==\s+(.+?)\s+==\s*$', line)
        if m:
            if current is not None:
                sections[current] = buf[:]
            current = m.group(1).strip().upper()
            buf = []
        else:
            if current is not None:
                buf.append(line)

    if current is not None:
        sections[current] = buf

    return sections


# ── Generic helpers ───────────────────────────────────────────────────────────

def _parse_kv(lines: list[str]) -> dict[str, str]:
    """Parse a list of lines into key → value pairs, handling multi-line indented values."""
    result: dict[str, str] = {}
    cur_key: str | None = None
    cur_val: list[str] = []

    for line in lines:
        # A new key starts at column 0 with WORD: pattern
        # Handles: "KEY: value" and "KEY (paren): value"
        m = re.match(r'^([A-Z][A-Z0-9_\s\(\)/&-]*?):\s*(.*)', line)
        if m and not line.startswith(' ') and not line.startswith('\t'):
            if cur_key is not None:
                result[cur_key] = '\n'.join(cur_val).strip()
            cur_key = m.group(1).strip().upper()
            first_val = m.group(2).strip()
            cur_val = [first_val] if first_val else []
        elif cur_key is not None:
            stripped = line.strip()
            if stripped or cur_val:  # allow blank lines inside multi-line values
                cur_val.append(stripped)

    if cur_key is not None:
        result[cur_key] = '\n'.join(cur_val).strip()

    return result


def _split_bullets_from_lines(lines: list[str]) -> list[tuple[str, str, list[str]]]:
    """Split lines into bullet blocks. Returns [(id, description, lines)]."""
    bullets: list[tuple[str, str, list[str]]] = []
    cur_id: str | None = None
    cur_desc = ""
    cur_lines: list[str] = []

    for line in lines:
        m = re.match(r'^\[([A-Z][A-Z0-9-]+)\]\s*(.*)', line)
        if m:
            if cur_id is not None:
                bullets.append((cur_id, cur_desc, cur_lines[:]))
            cur_id = m.group(1).strip()
            cur_desc = m.group(2).strip()
            cur_lines = []
        elif cur_id is not None:
            cur_lines.append(line)

    if cur_id is not None:
        bullets.append((cur_id, cur_desc, cur_lines[:]))

    return bullets


def _normalise_skill_name(raw: str) -> str:
    """Lowercase, strip punctuation, normalise whitespace."""
    s = raw.strip().lower()
    s = re.sub(r'\([^)]*\)', '', s)   # remove parentheticals
    s = re.sub(r'[^a-z0-9\s/]', ' ', s)
    return ' '.join(s.split())


def _compute_years_since(year: int, month: int) -> int:
    """Whole years from (year, month) to today."""
    today = date.today()
    years = today.year - year
    if (today.month, today.day) < (month, 1):
        years -= 1
    return max(0, years)


# ── CONTACT ───────────────────────────────────────────────────────────────────

def _parse_contact(lines: list[str]) -> ContactInfo:
    kv = _parse_kv(lines)

    # Parse location into struct
    loc_raw = kv.get("LOCATION", "")
    loc_parts = [p.strip() for p in loc_raw.split(",")] if loc_raw else []
    city = loc_parts[0] if loc_parts else ""
    country = "IN"

    loc = LocationInfo(
        city=city,
        state=kv.get("CITY_STATE", "").split(",")[1].strip() if "," in kv.get("CITY_STATE", "") else "",
        country=country,
        postal_code=kv.get("POSTAL_CODE", ""),
    )

    # Parse experience start → "YYYY-MM"
    exp_raw = kv.get("EXPERIENCE_START", "July 2017")
    exp_start = _parse_month_year(exp_raw)

    return ContactInfo(
        name=kv.get("NAME", ""),
        email=kv.get("EMAIL", ""),
        phone=kv.get("PHONE", ""),
        location=loc,
        linkedin=kv.get("LINKEDIN", ""),
        citizenship=kv.get("CITIZENSHIP", "Indian"),
        notice_period=kv.get("NOTICE_PERIOD", "4 weeks"),
        notice_period_days=30,
        experience_start=exp_start,
        # "YYYY-MM-DD   (DD-MM-YYYY)" → "YYYY-MM-DD"; absent DOB → ""
        dob=next(iter(kv.get("DOB", "").split()), ""),
        city_state=kv.get("CITY_STATE", ""),
    )


def _parse_month_year(raw: str) -> str:
    """'July 2017   (...)' → '2017-07'"""
    months = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
    }
    raw = raw.split("(")[0].strip()
    for name, num in months.items():
        if name in raw.lower():
            m = re.search(r'\b(\d{4})\b', raw)
            if m:
                return f"{m.group(1)}-{num}"
    # Already in YYYY-MM
    if re.match(r'^\d{4}-\d{2}', raw):
        return raw[:7]
    return raw


# ── SKILL YEARS ───────────────────────────────────────────────────────────────

def _parse_skill_years(lines: list[str]) -> tuple[SkillYears, list[str]]:
    """Return (SkillYears, not_yet_list)."""
    skills: dict[str, int] = {}
    not_yet: list[str] = []

    # Join continuation lines
    joined_lines = _join_continuations(lines)

    for line in joined_lines:
        line = line.strip()
        if not line:
            continue

        # SINCE_YYYY-MM (...): skills...
        m = re.match(r'SINCE_(\d{4})-(\d{2})\s*(?:\([^)]*\))?\s*:\s*(.*)', line)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
            years = _compute_years_since(year, month)
            raw_skills = m.group(3)
            for raw in raw_skills.split(","):
                # Handle "AWS (S3, EMR, ECR, Glue)" as one entry
                name = _normalise_skill_name(raw)
                if name and name != "always 0 years":
                    skills[name] = years
                    # Also index sub-skills inside parens
                    sub = re.search(r'\(([^)]+)\)', raw)
                    if sub:
                        for sub_raw in sub.group(1).split(","):
                            sub_name = _normalise_skill_name(sub_raw)
                            if sub_name:
                                skills[sub_name] = years
            continue

        # NOT_YET skills: ...
        m2 = re.match(r'NOT_YET\s+skills\s*:\s*(.*)', line, re.IGNORECASE)
        if m2:
            raw = m2.group(1)
            if "always 0" not in raw.lower():
                for part in re.split(r',\s*', raw):
                    name = _normalise_skill_name(part)
                    if name:
                        not_yet.append(name)
            continue

    return SkillYears(skills=skills, not_yet=not_yet), not_yet


def _join_continuations(lines: list[str]) -> list[str]:
    """Merge indented continuation lines back onto the previous line."""
    result: list[str] = []
    for line in lines:
        if line.startswith(' ') or line.startswith('\t'):
            if result:
                result[-1] = result[-1].rstrip() + " " + line.strip()
            else:
                result.append(line.strip())
        else:
            result.append(line)
    return result


# ── TITLE VARIANTS ────────────────────────────────────────────────────────────

def _parse_title_variants(lines: list[str]) -> dict[str, str]:
    titles: dict[str, str] = {}
    for line in _join_continuations(lines):
        line = line.strip()
        m = re.match(r'^([A-Z]+):\s+(.*)', line)
        if m:
            titles[m.group(1)] = m.group(2).strip()
    return titles


# ── SUMMARY VARIANTS ──────────────────────────────────────────────────────────

def _parse_summaries(lines: list[str]) -> list[Summary]:
    summaries: list[Summary] = []
    cur_id: str | None = None
    cur_tags: list[str] = []
    cur_text_lines: list[str] = []
    cur_metrics: list[str] = []

    def _flush() -> None:
        if cur_id is not None:
            text = "\n".join(l for l in cur_text_lines if not l.startswith("USES_METRICS_FROM")).strip()
            summaries.append(Summary(
                id=cur_id,
                text=text,
                tags=cur_tags[:],
                uses_metrics_from=cur_metrics[:],
            ))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if cur_id is not None:
                cur_text_lines.append("")
            continue

        # [SUMMARY-NAME]  TAGS: A, B
        m = re.match(r'^\[SUMMARY-([A-Z]+)\]\s+TAGS:\s*(.*)', stripped)
        if m:
            _flush()
            cur_id = m.group(1)
            cur_tags = [t.strip() for t in m.group(2).split(",") if t.strip()]
            cur_text_lines = []
            cur_metrics = []
            continue

        if cur_id is not None:
            if stripped.startswith("USES_METRICS_FROM:"):
                raw = stripped.split(":", 1)[1].strip()
                cur_metrics = [x.strip() for x in raw.split(",") if x.strip()]
            else:
                cur_text_lines.append(stripped)

    _flush()
    return summaries


# ── SKILLS ────────────────────────────────────────────────────────────────────

def _parse_skills(lines: list[str]) -> tuple[list[SkillsBlock], list[str]]:
    blocks: list[SkillsBlock] = []
    not_yet: list[str] = []
    joined = _join_continuations(lines)

    for line in joined:
        stripped = line.strip()
        if not stripped:
            continue

        # NOT_YET (...): items
        if re.match(r'^NOT_YET', stripped):
            colon = stripped.find(":")
            if colon >= 0:
                raw = stripped[colon + 1:].strip()
                for part in re.split(r',\s*', raw):
                    # Expand "X/Y" splits
                    for subpart in part.split("/"):
                        name = _normalise_skill_name(subpart)
                        if name and "do not" not in name:
                            not_yet.append(name)
                            # Also keep sub-items from parentheticals
                            sub = re.search(r'\(([^)]+)\)', subpart)
                            if sub:
                                for sp in sub.group(1).split(","):
                                    sn = _normalise_skill_name(sp)
                                    if sn:
                                        not_yet.append(sn)
            continue

        # CATEGORY: skills text
        m = re.match(r'^([A-Z_]+):\s+(.*)', stripped)
        if m:
            cat = m.group(1)
            text = m.group(2)
            # Build an items list from the text (split on comma, strip parens)
            items: list[str] = []
            for raw_item in re.split(r',\s*', text):
                name = _normalise_skill_name(raw_item)
                if name:
                    items.append(name)
            blocks.append(SkillsBlock(category=cat, text=text, items=items))

    return blocks, not_yet


def _merge_not_yet(a: list[str], b: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in a + b:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ── EXPERIENCE ────────────────────────────────────────────────────────────────

def _parse_experience(lines: list[str]) -> list[JobExperience]:
    jobs: list[JobExperience] = []

    # Split by --- separator lines into job segments
    segments: list[list[str]] = []
    cur_seg: list[str] = []
    for line in lines:
        if re.match(r'^-{10,}', line):
            if cur_seg:
                segments.append(cur_seg[:])
                cur_seg = []
        else:
            cur_seg.append(line)
    if cur_seg:
        segments.append(cur_seg)

    # Pair up: each [JOB] header segment is followed by a bullets segment
    # Actually separators create: [empty?, header, bullets, header, bullets, ...]
    # Group: header = segment containing "[JOB]"
    i = 0
    while i < len(segments):
        seg = segments[i]
        # Does this segment contain a [JOB] line?
        job_line = next((l for l in seg if re.match(r'^\[JOB\]', l.strip())), None)
        if job_line is None:
            i += 1
            continue

        # Parse the job header
        kv = _parse_kv([l for l in seg if not re.match(r'^\[JOB\]', l.strip())])
        m_job = re.match(r'^\[JOB\]\s+(.*)', job_line.strip())
        company = m_job.group(1).strip() if m_job else kv.get("COMPANY", "Unknown")

        # Parse dates: "July 2022 -- Present" or "June 2021 -- July 2022"
        dates_raw = kv.get("DATES", "")
        start_str, end_str = _parse_dates(dates_raw)

        job = JobExperience(
            job_id=_make_job_id(company),
            company=company,
            title=kv.get("TITLE", ""),
            start=start_str,
            end=end_str,
            location=kv.get("LOCATION", ""),
            bullets=[],
        )

        # Next segments until the next [JOB] header contain bullets
        bullet_lines: list[str] = []
        j = i + 1
        while j < len(segments):
            next_seg = segments[j]
            has_next_job = any(re.match(r'^\[JOB\]', l.strip()) for l in next_seg)
            if has_next_job:
                break
            bullet_lines.extend(next_seg)
            j += 1

        job.bullets = _parse_bullets(bullet_lines)
        jobs.append(job)
        i = j if j > i + 1 else i + 1

    return jobs


def _make_job_id(company: str) -> str:
    """'Vista (Cimpress)' → 'VISTA'"""
    base = re.sub(r'\(.*?\)', '', company).strip()
    return re.sub(r'[^A-Z]', '', base.upper())[:10]


def _parse_dates(raw: str) -> tuple[str, str | None]:
    """'July 2022 -- Present' → ('2022-07', None)"""
    if "--" in raw:
        parts = raw.split("--")
        start = _parse_month_year(parts[0].strip())
        end_raw = parts[1].strip()
        end = None if end_raw.lower() in ("present", "current", "") else _parse_month_year(end_raw)
    else:
        start = _parse_month_year(raw)
        end = None
    return start, end


def _parse_bullets(lines: list[str]) -> list[Bullet]:
    """Parse bullet blocks from a list of lines."""
    bullet_tuples = _split_bullets_from_lines(lines)
    bullets: list[Bullet] = []

    for bid, bdesc, blines in bullet_tuples:
        b = _parse_single_bullet(bid, blines)
        bullets.append(b)

    return bullets


def _parse_single_bullet(bid: str, lines: list[str]) -> Bullet:
    """Parse one bullet's fields from its lines."""
    # We need a custom multi-line parser because field names have parens in them.
    # State machine: we track current_field + its lines.
    fields: dict[str, list[str]] = {}
    cur_field: str | None = None
    cur_lines: list[str] = []

    # Field name patterns (at column 0, optionally with parens)
    _FIELD_RE = re.compile(
        r'^(TAGS|RESUME|SHORT|ALT|DETAIL|INTERVIEW_PREP|VERIFIED|VERIFY|BASIS|NOTE|'
        r'PLACEMENT|USES_METRICS_FROM|OPTIONAL|ALT \(.*?\)|DETAIL \(STAR\)):\s*(.*)',
        re.IGNORECASE,
    )

    def flush() -> None:
        if cur_field is not None:
            fields[cur_field] = cur_lines[:]

    for line in lines:
        # Blank lines reset continuation only if we're in a "done" block
        if not line.strip():
            if cur_field is not None:
                cur_lines.append("")
            continue

        m = _FIELD_RE.match(line)
        if m and not (line.startswith(' ') or line.startswith('\t')):
            flush()
            cur_field = m.group(1).upper().strip()
            first = m.group(2).strip()
            cur_lines = [first] if first else []
        elif cur_field is not None:
            cur_lines.append(line.strip())

    flush()

    def _join(key: str) -> str:
        raw_lines = fields.get(key, [])
        # Remove trailing empty lines
        while raw_lines and not raw_lines[-1]:
            raw_lines.pop()
        return "\n".join(raw_lines).strip()

    tags = [t.strip() for t in _join("TAGS").split(",") if t.strip()]

    # ALT: may be "ALT (condition)" or just "ALT"
    alt_key = next((k for k in fields if k.startswith("ALT")), None)
    alt_obj: BulletVariant | None = None
    if alt_key:
        alt_text = _join(alt_key)
        condition_m = re.search(r'ALT\s+\((.+?)\)', alt_key, re.IGNORECASE)
        condition = condition_m.group(1) if condition_m else ""
        # Disabled if the text contains "..." (incomplete) or an "only if" note
        is_incomplete = "..." in alt_text or "(use " in alt_text.lower()
        alt_obj = BulletVariant(
            text=alt_text,
            condition=condition,
            enabled=not is_incomplete,
        )

    # Collect internal notes
    notes: list[str] = []
    for note_key in ("NOTE", "BASIS", "OPTIONAL", "PLACEMENT", "VERIFIED"):
        val = _join(note_key)
        if val:
            notes.append(f"{note_key}: {val}")

    # Open VERIFY items
    verify_open = bool(_join("VERIFY"))

    # Metrics from USES_METRICS_FROM
    umf_raw = _join("USES_METRICS_FROM")
    uses_metrics = [x.strip() for x in umf_raw.split(",") if x.strip()] if umf_raw else []

    return Bullet(
        id=bid,
        tags=tags,
        resume=_join("RESUME"),
        short=_join("SHORT"),
        alt=alt_obj,
        detail_star=_join("DETAIL (STAR)") or _join("DETAIL"),
        interview_prep=_join("INTERVIEW_PREP"),
        notes=notes,
        verify_open=verify_open,
        uses_metrics_from=uses_metrics,
    )


# ── EDUCATION ─────────────────────────────────────────────────────────────────

def _parse_education(lines: list[str]) -> list[Education]:
    kv = _parse_kv(_join_continuations(lines))

    degree_raw = kv.get("DEGREE", "")
    # "Bachelor of Engineering (B.E.) in Computer Engineering"
    # Extract field after "in"
    field = ""
    if " in " in degree_raw.lower():
        field = degree_raw.split(" in ", 1)[1].strip()
    # Strip the abbreviation part for the degree name
    degree = re.sub(r'\([^)]*\)', '', degree_raw).strip()

    dates_raw = kv.get("DATES", "")
    start_yr, end_yr = 0, 0
    year_match = re.findall(r'\b(\d{4})\b', dates_raw)
    if len(year_match) >= 2:
        start_yr, end_yr = int(year_match[0]), int(year_match[1])
    elif len(year_match) == 1:
        start_yr = int(year_match[0])

    school_raw = kv.get("SCHOOL", "")
    # "Vishwakarma Institute of Information Technology (VIIT), Pune, India"
    institution = school_raw.split(",")[0].strip()

    return [
        Education(
            degree=degree,
            field=field,
            institution=institution,
            university=kv.get("UNIVERSITY", ""),
            start=start_yr,
            end=end_yr,
            degree_recognised=True,
            level_for_forms="Bachelor's degree",
            notes=kv.get("NOTE", ""),
        )
    ]


# ── LANGUAGES ────────────────────────────────────────────────────────────────

def _parse_languages(lines: list[str]) -> list[Language]:
    langs: list[Language] = []
    for line in _join_continuations(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r'^([A-Za-z]+):\s+(.*)', stripped)
        if m:
            name = m.group(1)
            desc = m.group(2).strip()
            # Determine level from description
            desc_lower = desc.lower()
            if "native" in desc_lower:
                level = "native"
            elif "fluent" in desc_lower or "professional" in desc_lower:
                level = "fluent"
            elif "elementary" in desc_lower or "basic" in desc_lower or "a1" in desc_lower:
                level = "elementary"
            elif "not yet" in desc_lower:
                continue   # skip undeclared languages
            else:
                level = desc.split(";")[0].strip()

            # show_on_resume_for
            show_for: list[str] = []
            m2 = re.search(r'include only for (.+?)(?:;|$)', desc, re.IGNORECASE)
            if m2:
                show_for = [x.strip() for x in m2.group(1).replace("roles", "").split("/") if x.strip()]

            langs.append(Language(name=name, level=level, show_on_resume_for=show_for))
    return langs


# ── WORK AUTHORIZATION ────────────────────────────────────────────────────────

def _parse_work_auth(lines: list[str]) -> dict[str, str]:
    auth: dict[str, str] = {}
    for line in _join_continuations(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r'^([A-Z][A-Z\s\(\)/,]+?):\s+(.*)', stripped)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()
            # Simplify to canonical form
            if "citizen" in val.lower() and "sponsor" not in val.lower():
                auth[key] = "citizen"
            else:
                auth[key] = "requires_sponsorship"
    # Always ensure INDIA and DEFAULT exist
    if "INDIA" not in auth:
        auth["INDIA"] = "citizen"
    if "IN" not in auth:
        auth["IN"] = "citizen"
    if "DEFAULT" not in auth:
        auth["DEFAULT"] = "requires_sponsorship"
    return auth


# ── TAILORING RECIPES ─────────────────────────────────────────────────────────

def _parse_recipes(lines: list[str]) -> list[TailoringRecipe]:
    recipes: list[TailoringRecipe] = []

    # Each recipe starts at column 0 with ARCHETYPE / SUBTITLE (e.g., ...):
    # and continues until the next such line
    cur_name: str | None = None
    cur_lines: list[str] = []

    def _flush_recipe() -> None:
        if cur_name is None or not cur_lines:
            return
        recipes.append(_build_recipe(cur_name, cur_lines[:]))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Recipe header: "ANALYTICS_ENGINEER / SAAS (e.g., ...):"
        m = re.match(r'^([A-Z][A-Z_\s/]+?)(?:\s+\([^)]*\))?:\s*$', stripped)
        if m and not line.startswith(' ') and not line.startswith('\t'):
            _flush_recipe()
            cur_name = m.group(1).strip().replace(" / ", "_").replace(" ", "_").upper()
            cur_lines = []
        elif cur_name is not None:
            cur_lines.append(stripped)

    _flush_recipe()
    return recipes


def _build_recipe(name: str, lines: list[str]) -> TailoringRecipe:
    title_var = "DEFAULT"
    summary_var = "DEFAULT"
    bullets: dict[str, list[str]] = {}
    notes: list[str] = []

    for line in lines:
        m_title = re.match(r'TITLE\s*=\s*([A-Z]+)', line)
        if m_title:
            title_var = m_title.group(1)
        m_sum = re.match(r'SUMMARY\s*=\s*([A-Z]+)', line)
        if m_sum:
            summary_var = m_sum.group(1)

        # "VISTA: 02, 06, 01, ..." or "BARCLAYS: 01, 04..."
        m_bullets = re.match(r'^([A-Z]+):\s+(.*)', line)
        if m_bullets and m_bullets.group(1) not in ("TITLE", "SUMMARY"):
            job_key = m_bullets.group(1)
            raw_ids = m_bullets.group(2)
            ids = []
            for raw_id in re.split(r',\s*', raw_ids):
                raw_id = raw_id.strip()
                # "04(ALT)" → "04", "(ALT)" note kept in the ID
                if raw_id:
                    ids.append(raw_id)
            # Reconstruct full IDs like "VISTA-02" from "02"
            full_ids: list[str] = []
            for raw_id in ids:
                # Already full (e.g., VISTA-01)
                if re.match(r'^[A-Z]+-\d+', raw_id):
                    full_ids.append(raw_id)
                elif re.match(r'^\d+', raw_id):
                    # Bare number, e.g. "02" under "VISTA:" → "VISTA-02"
                    num_match = re.match(r'^(\d+)(.*)', raw_id)
                    if num_match:
                        num = num_match.group(1).zfill(2)
                        suffix = num_match.group(2).strip()
                        full_id = f"{job_key[:4].rstrip('-')}-{num}"
                        if suffix:
                            full_id += suffix
                        full_ids.append(full_id)
            bullets[job_key] = full_ids

        # Notes lines (don't match above patterns)
        if not re.match(r'^(TITLE|SUMMARY|[A-Z]+:)', line) and line:
            notes.append(line)

    return TailoringRecipe(
        archetype=name,
        title_variant=title_var,
        summary_variant=summary_var,
        bullets=bullets,
        notes="\n".join(notes).strip(),
    )


# ── OPEN ITEMS ────────────────────────────────────────────────────────────────

def _parse_open_items(lines: list[str]) -> list[str]:
    """Return only unresolved ([ ]) items."""
    open_items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[ ]"):
            open_items.append(stripped[3:].strip())
    return open_items


# ── FILE NAMING ───────────────────────────────────────────────────────────────

def _parse_file_naming(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""
