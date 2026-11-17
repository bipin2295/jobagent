"""Pydantic models for master_profile.yaml – the structured career knowledge base."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ── Contact & personal ────────────────────────────────────────────────────────

class LocationInfo(BaseModel):
    city: str = ""
    state: str = ""
    country: str = "IN"
    postal_code: str = ""


class ContactInfo(BaseModel):
    name: str
    email: str
    phone: str = ""
    location: LocationInfo = Field(default_factory=LocationInfo)
    linkedin: str = ""
    citizenship: str = "Indian"
    notice_period: str = "4 weeks"
    notice_period_days: int = 30
    experience_start: str = ""   # "YYYY-MM" or "July 2017"
    dob: str = ""                # "YYYY-MM-DD", form-fill only
    city_state: str = ""


# ── Skill years ───────────────────────────────────────────────────────────────

class SkillYears(BaseModel):
    """Maps skill name (lowercase) → whole years of experience."""
    skills: dict[str, int] = Field(default_factory=dict)
    not_yet: list[str] = Field(default_factory=list)

    def years_for(self, skill: str) -> int:
        """Return years for *skill*, 0 if NOT_YET or unknown."""
        key = skill.strip().lower()
        if key in self.not_yet:
            return 0
        # Exact match
        if key in self.skills:
            return self.skills[key]
        # Partial match (e.g. "pyspark" matches "apache spark / pyspark")
        for k, v in self.skills.items():
            if key in k or k in key:
                return v
        return 0


# ── Bullets ───────────────────────────────────────────────────────────────────

class BulletVariant(BaseModel):
    text: str
    condition: str = ""    # e.g. "you made the migration decision"
    enabled: bool = True


class Bullet(BaseModel):
    id: str
    tags: list[str] = Field(default_factory=list)

    # Allowed output text (planner picks one)
    resume: str = ""
    short: str = ""
    alt: BulletVariant | None = None

    # Internal only – never output
    detail_star: str = ""
    interview_prep: str = ""
    notes: list[str] = Field(default_factory=list)   # NOTE, BASIS, OPTIONAL, PLACEMENT, VERIFIED

    # Verification
    verify_open: bool = False       # True → use bullet without numbers
    uses_metrics_from: list[str] = Field(default_factory=list)

    @property
    def safe_resume(self) -> str:
        """Return resume text; number-stripped version if verify_open."""
        if not self.verify_open:
            return self.resume
        # Strip standalone numbers like "75%", "3.7x", "10h43m", "100M+"
        import re
        return re.sub(
            r'\b\d[\d,\.]*\s*(%|x|k|M\+?|B|TB|GB|h\d{2}m|h|min|LPA|months?|years?|days?|runs?|batches?|squads?|pipelines?)\b',
            '[N]',
            self.resume,
        )

    @property
    def effective_text(self) -> str:
        """The text the planner should use by default."""
        return self.safe_resume


# ── Experience ────────────────────────────────────────────────────────────────

class JobExperience(BaseModel):
    job_id: str
    company: str
    title: str
    start: str    # "YYYY-MM"
    end: str | None = None   # "YYYY-MM" or None for present
    location: str = ""
    bullets: list[Bullet] = Field(default_factory=list)

    def bullet(self, bullet_id: str) -> Bullet | None:
        for b in self.bullets:
            if b.id == bullet_id:
                return b
        return None


# ── Summaries & titles ────────────────────────────────────────────────────────

class Summary(BaseModel):
    id: str                    # "DEFAULT", "AE", "PLAT", "FIN", "BIGCO"
    text: str
    tags: list[str] = Field(default_factory=list)
    uses_metrics_from: list[str] = Field(default_factory=list)

    def has_unresolved_metrics(self, bullets_by_id: dict[str, Bullet]) -> bool:
        for bid in self.uses_metrics_from:
            b = bullets_by_id.get(bid)
            if b and b.verify_open:
                return True
        return False


# ── Skills ────────────────────────────────────────────────────────────────────

class SkillsBlock(BaseModel):
    category: str
    text: str
    items: list[str] = Field(default_factory=list)   # normalised, lowercase


# ── Education ─────────────────────────────────────────────────────────────────

class Education(BaseModel):
    degree: str
    field: str = ""
    institution: str
    university: str = ""
    start: int = 0
    end: int = 0
    degree_recognised: bool = True
    level_for_forms: str = "Bachelor's degree"
    notes: str = ""


# ── Languages ─────────────────────────────────────────────────────────────────

class Language(BaseModel):
    name: str
    level: str
    show_on_resume_for: list[str] = Field(default_factory=list)


# ── Tailoring recipes ─────────────────────────────────────────────────────────

class TailoringRecipe(BaseModel):
    archetype: str                              # "ANALYTICS_ENGINEER_SAAS", etc.
    title_variant: str                          # "AE", "FIN", "PLAT", "BIGCO", "DEFAULT"
    summary_variant: str
    bullets: dict[str, list[str]] = Field(default_factory=dict)   # job_id → [bullet_ids]
    notes: str = ""


# ── Compensation ──────────────────────────────────────────────────────────────

class CompBand(BaseModel):
    currency: str
    period: str = "year"           # "year" | "month"
    expected: float = 0.0
    floor: float = 0.0
    band: list[float] = Field(default_factory=list)
    answer_range: list[float] = Field(default_factory=list)
    package_incl_super: float | None = None   # AU: total package
    salaries_per_year: int | None = None      # AT: 14, most: 12
    priority: str = "normal"                  # "low" for MY/VN etc.


# ── Master profile ────────────────────────────────────────────────────────────

class MasterProfile(BaseModel):
    meta: dict[str, Any] = Field(default_factory=dict)
    contact: ContactInfo
    skill_years: SkillYears = Field(default_factory=SkillYears)
    titles: dict[str, str] = Field(default_factory=dict)
    summaries: list[Summary] = Field(default_factory=list)
    skills: list[SkillsBlock] = Field(default_factory=list)
    not_yet: list[str] = Field(default_factory=list)
    experience: list[JobExperience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    work_authorization: dict[str, str] = Field(default_factory=dict)
    tailoring_recipes: list[TailoringRecipe] = Field(default_factory=list)
    file_naming: str = "Data_Engineer_{Company}.pdf"
    open_items: list[str] = Field(default_factory=list)
    compensation: dict[str, CompBand] = Field(default_factory=dict)

    # ── Convenience helpers ───────────────────────────────────────────────────

    @property
    def bullets_by_id(self) -> dict[str, Bullet]:
        result: dict[str, Bullet] = {}
        for job in self.experience:
            for b in job.bullets:
                result[b.id] = b
        return result

    def get_bullet(self, bullet_id: str) -> Bullet | None:
        return self.bullets_by_id.get(bullet_id)

    def get_summary(self, variant: str = "DEFAULT") -> Summary | None:
        for s in self.summaries:
            if s.id == variant:
                return s
        return None

    def get_recipe(self, archetype: str) -> TailoringRecipe | None:
        for r in self.tailoring_recipes:
            if r.archetype == archetype:
                return r
        return None

    def years_for_skill(self, skill: str) -> int:
        return self.skill_years.years_for(skill)

    def is_not_yet(self, skill: str) -> bool:
        key = skill.strip().lower()
        return any(key in term or term in key for term in self.not_yet)
