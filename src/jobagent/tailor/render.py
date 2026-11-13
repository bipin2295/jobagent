"""Resume renderer – converts a MasterProfile + PlanResult into a PDF via RenderCV.

RenderCV (``pip install rendercv``) accepts a YAML file and generates a PDF.
This module:

1. Defines ``PlanResult`` – the shared tailoring plan model (imported by
   ``planner.py`` and ``orchestrator.py``).
2. ``build_rendercv_yaml`` – assembles a RenderCV-compatible dict.
3. ``render_resume`` – writes the YAML, calls the CLI, returns the PDF path.
4. ``verify_pdf`` – lightweight ATS sanity check using pypdf (optional dep).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from jobagent.profile.schema import Bullet, MasterProfile

# ---------------------------------------------------------------------------
# PlanResult – shared model; imported by planner.py and orchestrator.py
# ---------------------------------------------------------------------------


class PlanResult(BaseModel):
    """Output of the tailoring planner.

    Drives resume rendering, cover-letter generation, and answer pre-fill.
    ``planner.py`` produces instances of this model; ``render.py``,
    ``cover_letter.py``, and ``answers.py`` consume them.
    """

    summary_variant: str = "DEFAULT"
    """Which summary ID from the profile to use (e.g. 'AE', 'PLAT', 'DEFAULT')."""

    bullet_selections: list[str] = Field(default_factory=list)
    """Ordered list of bullet IDs selected for this application."""

    skills_first: list[str] = Field(default_factory=list)
    """Skill names to surface in the first rows of the technologies section."""

    pitch_angle: str = ""
    """One-line pitch angle, e.g. 'Platform-scale data engineering at scale'."""

    title_variant: str = "DEFAULT"
    """Which title variant from profile.titles to display on the resume."""

    extra_sections: dict[str, Any] = Field(default_factory=dict)
    """Optional extra RenderCV sections injected verbatim (future extension)."""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

# Matches standalone numeric metrics: "75%", "3.7x", "10h43m", "100M+", etc.
# NOTE: trailing (?!\w) instead of \b so that non-word-char units like % are
# also caught (a trailing \b requires a word-char boundary which % lacks).
_NUMBER_RE = re.compile(
    r"\b\d[\d,\.]*\s*"
    r"(?:%|x|k|M\+?|B|TB|GB|h\d{2}m|h|min|LPA|months?|years?|days?|runs?"
    r"|batches?|squads?|pipelines?)(?!\w)",
    re.IGNORECASE,
)

# Splits on sentence boundaries (period / bang / question mark followed by space).
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _strip_numbers(text: str) -> str:
    """Replace numeric metrics in *text* with the ``[N]`` placeholder."""
    return _NUMBER_RE.sub("[N]", text)


def _first_n_sentences(text: str, n: int = 3) -> str:
    """Return the first *n* sentences from *text*."""
    parts = _SENTENCE_RE.split(text.strip())
    return " ".join(parts[:n])


def _bullet_text(b: Bullet) -> str:
    """Choose the best display text for a resume bullet.

    Prefers the ``short`` variant when it is set and under 120 chars.
    For the long fallback, applies ``_strip_numbers`` (the fixed regex with
    ``(?!\\w)`` trailing assertion) directly when ``verify_open`` is True,
    rather than delegating to ``Bullet.safe_resume`` which carries an older
    regex that misses ``%``-suffixed metrics.
    """
    if b.short and len(b.short) < 120:
        return b.short
    if b.verify_open:
        return _strip_numbers(b.resume)
    return b.resume


def _ensure_https(url: str) -> str:
    """Return *url* as a full ``https://`` URL.

    Handles bare LinkedIn handles (``linkedin.com/…``) and partial paths that
    start with ``linkedin``.
    """
    url = url.strip()
    if not url:
        return ""
    if url.startswith("http"):
        return url
    # e.g. "linkedin.com/in/…" or "linkedin/in/…"
    if url.startswith("linkedin") or "/" not in url:
        return f"https://{url}"
    return url


# ---------------------------------------------------------------------------
# build_rendercv_yaml
# ---------------------------------------------------------------------------


def build_rendercv_yaml(plan: PlanResult, profile: MasterProfile) -> dict[str, Any]:
    """Assemble a RenderCV-compatible dict from *plan* and *profile*.

    The returned dict can be serialised with ``yaml.dump`` and fed directly to
    ``rendercv render``.  All sections are built from the profile data; only
    bullets explicitly listed in ``plan.bullet_selections`` are included.

    Parameters
    ----------
    plan:
        The tailoring plan produced by the planner.
    profile:
        The loaded ``MasterProfile``.

    Returns
    -------
    dict[str, Any]
        A nested dict matching the RenderCV ``engineeringresumes`` theme
        schema (``cv`` + ``design`` top-level keys).
    """
    contact = profile.contact
    bullets_by_id = profile.bullets_by_id

    # ── Summary ───────────────────────────────────────────────────────────────
    summary_obj = profile.get_summary(plan.summary_variant)
    if summary_obj is None and plan.summary_variant != "DEFAULT":
        summary_obj = profile.get_summary("DEFAULT")

    if summary_obj is not None:
        raw_text = summary_obj.text
        if summary_obj.has_unresolved_metrics(bullets_by_id):
            raw_text = _strip_numbers(raw_text)
        summary_text = raw_text
    else:
        # Last-resort: first 3 sentences of the DEFAULT summary
        default_obj = profile.get_summary("DEFAULT")
        summary_text = _first_n_sentences(default_obj.text) if default_obj else ""

    summary_section: list[str] = [summary_text] if summary_text.strip() else []

    # ── Experience ────────────────────────────────────────────────────────────
    # bullet_selections may be strings (render.PlanResult) or BulletSelection objects (planner.PlanResult)
    selected_set = set(
        s if isinstance(s, str) else s.bullet_id  # type: ignore[union-attr]
        for s in plan.bullet_selections
    )
    experience_entries: list[dict[str, Any]] = []

    for job_exp in profile.experience:
        # Only include bullets that the planner explicitly selected
        job_bullets = [b for b in job_exp.bullets if b.id in selected_set]
        if not job_bullets:
            continue

        entry: dict[str, Any] = {
            "company": job_exp.company,
            "position": job_exp.title,
            "start_date": job_exp.start,
            "end_date": "present" if job_exp.end is None else job_exp.end,
            "highlights": [_bullet_text(b) for b in job_bullets],
        }
        if job_exp.location:
            entry["location"] = job_exp.location

        experience_entries.append(entry)

    # ── Education ─────────────────────────────────────────────────────────────
    education_entries: list[dict[str, Any]] = []

    for edu in profile.education:
        institution = edu.institution or edu.university or ""
        edu_entry: dict[str, Any] = {
            "institution": institution,
            # RenderCV 'area' = field of study; fall back to degree name if unset
            "area": edu.field or edu.degree,
            "degree": edu.degree,
        }
        if edu.start:
            edu_entry["start_date"] = str(edu.start)
        if edu.end:
            edu_entry["end_date"] = str(edu.end)
        education_entries.append(edu_entry)

    # ── Technologies / Skills ─────────────────────────────────────────────────
    all_skills = list(profile.skills)
    skills_first_set = {s.strip().lower() for s in plan.skills_first}

    def _priority(block: Any) -> int:
        """Count how many skills_first terms appear in this block."""
        if not skills_first_set:
            return 0
        text_lower = block.text.lower()
        items_lower = {i.lower() for i in block.items}
        return sum(
            1
            for sf in skills_first_set
            if sf in text_lower or any(sf in item for item in items_lower)
        )

    # Stable descending sort: high-priority blocks surface first
    ranked = sorted(all_skills, key=_priority, reverse=True)

    # Include 5–7 rows: maximise up to 7; go below 5 only if the profile is sparse.
    # Priority-ranked rows already float to the top via the sort above.
    n_total = len(ranked)
    n_rows = min(7, n_total)  # e.g. 9 blocks → 7 rows; 4 blocks → 4 rows

    technologies_entries: list[dict[str, str]] = [
        {
            "label": block.category.replace("_", " ").title(),
            "details": block.text,
        }
        for block in ranked[:n_rows]
    ]

    # ── Inject any extra sections from the plan ───────────────────────────────
    extra: dict[str, Any] = getattr(plan, "extra_sections", None) or {}

    # ── Assemble cv dict ──────────────────────────────────────────────────────
    linkedin_url = _ensure_https(contact.linkedin)

    cv: dict[str, Any] = {
        "name": contact.name,
        "email": contact.email,
    }
    if contact.phone:
        cv["phone"] = contact.phone
    if linkedin_url:
        cv["website"] = linkedin_url

    sections: dict[str, Any] = {}
    if summary_section:
        sections["summary"] = summary_section
    if experience_entries:
        sections["experience"] = experience_entries
    if education_entries:
        sections["education"] = education_entries
    if technologies_entries:
        sections["technologies"] = technologies_entries
    # Merge any extra sections the planner injected
    sections.update(extra)

    cv["sections"] = sections

    return {
        "cv": cv,
        # RenderCV design block – page_size and font_size are top-level in
        # the global design schema, not inside the per-theme block.
        "design": {
            "theme": "engineeringresumes",
        },
    }


# ---------------------------------------------------------------------------
# render_resume
# ---------------------------------------------------------------------------


def render_resume(plan: PlanResult, profile: MasterProfile, output_dir: Path) -> Path:
    """Render a PDF resume from *plan* + *profile* using the RenderCV CLI.

    Steps:
    1. Build the RenderCV dict via :func:`build_rendercv_yaml`.
    2. Write it to ``output_dir/resume_input.yaml``.
    3. Invoke ``rendercv render … --output-folder-name <output_dir>``.
    4. Rename the generated PDF to ``output_dir/resume.pdf``.
    5. Return ``output_dir/resume.pdf``.

    Parameters
    ----------
    plan:
        Tailoring plan from the planner.
    profile:
        Loaded ``MasterProfile``.
    output_dir:
        Directory where the YAML and PDF are written (created if absent).

    Returns
    -------
    Path
        Absolute path to ``output_dir/resume.pdf``.

    Raises
    ------
    RuntimeError
        If RenderCV exits with a non-zero code or produces no PDF.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    rendercv_dict = build_rendercv_yaml(plan, profile)
    yaml_path = output_dir / "resume_input.yaml"

    with yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(
            rendercv_dict,
            fh,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            indent=2,
        )

    result = subprocess.run(
        [
            "rendercv",
            "render",
            str(yaml_path),
            "--output-folder",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"RenderCV failed (exit {result.returncode}):\n{result.stderr}"
        )

    # RenderCV may write to output_dir directly OR to a deeply-nested
    # subdirectory (it mirrors the absolute path when given an absolute --output-folder).
    # Search recursively for any PDF that isn't our already-renamed resume.pdf.
    resume_pdf = output_dir / "resume.pdf"
    all_pdfs = [p for p in output_dir.rglob("*.pdf") if p.name != "resume.pdf"]

    if not all_pdfs:
        raise RuntimeError(
            f"RenderCV produced no PDF in {output_dir}.\n{result.stderr}"
        )

    # Pick the most recently modified PDF (guards against stale artefacts)
    generated = max(all_pdfs, key=lambda p: p.stat().st_mtime)
    import shutil
    shutil.copy2(generated, resume_pdf)   # copy (not rename) so the original path stays for reference

    return resume_pdf


# ---------------------------------------------------------------------------
# verify_pdf
# ---------------------------------------------------------------------------


def verify_pdf(pdf_path: Path, required_name: str) -> tuple[bool, list[str]]:
    """Lightweight ATS sanity check on the generated PDF.

    Uses ``pypdf`` (optional dep in the ``profile-tools`` extra).  If pypdf
    is not installed the function returns ``(True, [])`` so a missing optional
    dependency never blocks the pipeline.

    Parameters
    ----------
    pdf_path:
        Path to the generated PDF file.
    required_name:
        The candidate's full name; must appear somewhere in the extracted text.

    Returns
    -------
    tuple[bool, list[str]]
        ``(passed, issues)`` – *passed* is ``True`` iff *issues* is empty.
    """
    try:
        from pypdf import PdfReader  # noqa: PLC0415
    except ImportError:
        return (True, [])

    try:
        reader = PdfReader(str(pdf_path))
        full_text = " ".join(
            (page.extract_text() or "") for page in reader.pages
        ).lower()
    except Exception as exc:  # noqa: BLE001
        return (False, [f"Could not read PDF: {exc}"])

    issues: list[str] = []

    if required_name.lower() not in full_text:
        issues.append(f"Candidate name '{required_name}' not found in PDF text")

    section_keywords = ["experience", "education", "skills", "engineer"]
    hits = [kw for kw in section_keywords if kw in full_text]
    if len(hits) < 2:
        missing = [kw for kw in section_keywords if kw not in full_text]
        issues.append(
            f"Required section keywords missing: found {len(hits)}/4 "
            f"(missing: {', '.join(missing)})"
        )

    return (len(issues) == 0, issues)
