"""Tests for the master resume importer."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.profile.importer import import_resume, _parse
from jobagent.profile.schema import MasterProfile


RESUME_PATH = Path.home() / "Documents/resume/master_resume.txt"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_real() -> MasterProfile:
    if not RESUME_PATH.exists():
        pytest.skip(f"Real resume not found at {RESUME_PATH}")
    return import_resume(RESUME_PATH)


MINIMAL_RESUME = """
== CONTACT ==
NAME: Test User
EMAIL: test@example.com
PHONE: +1-555-0100
LOCATION: London, UK
CITIZENSHIP: British
NOTICE_PERIOD: 4 weeks
EXPERIENCE_START: January 2020

== SKILL YEARS ==
SINCE_2020-01 (Job A): Python, SQL, dbt
SINCE_2022-06 (Job B): Snowflake, Airflow
NOT_YET skills: Kafka/Kinesis, Redshift

== TITLE VARIANTS ==
DEFAULT: Senior Data Engineer

== SUMMARY VARIANTS ==
[SUMMARY-DEFAULT]  TAGS: CORE
Experienced data engineer with 4+ years building pipelines.
USES_METRICS_FROM: B1-01

== SKILLS (FULL INVENTORY) ==
PROGRAMMING: Python, SQL
NOT_YET (do not claim): Kafka/Kinesis, Redshift

== EXPERIENCE ==
------------------------------------------------------------------------------
[JOB] Acme Corp
TITLE:    Senior Data Engineer
DATES:    January 2022 -- Present
LOCATION: Remote
------------------------------------------------------------------------------

[B1-01] Pipeline optimisation
TAGS: CORE, PERF
RESUME:
  Optimised a critical pipeline, cutting runtime by 50%.
SHORT:
  Cut pipeline runtime by 50%.
INTERVIEW_PREP: explain the approach

------------------------------------------------------------------------------
[JOB] StartupCo
TITLE:    Data Engineer
DATES:    January 2020 -- December 2021
LOCATION: London, UK
------------------------------------------------------------------------------

[B2-01] First pipeline
TAGS: PLAT
RESUME:
  Built the first data pipeline from scratch.

== EDUCATION ==
DEGREE: Bachelor of Science in Computer Science
SCHOOL: University of London, London, UK
DATES: 2016 -- 2020

== LANGUAGES ==
English: Native
French: Elementary; include only for FR roles

== WORK AUTHORIZATION & RELOCATION (BY COUNTRY) ==
UK: British citizen; no sponsorship required.
DEFAULT: Requires work visa sponsorship.

== TAILORING RECIPES ==
DEFAULT:
  TITLE=DEFAULT  SUMMARY=DEFAULT
  B1CORP: 01
  STARTUPCO: 01

== OPEN ITEMS / VERIFY BEFORE SENDING ==
[x] All resolved.

== FILE NAMING ==
Test_User_DE_{Company}.pdf
"""


# ── Unit tests ────────────────────────────────────────────────────────────────

class TestMinimalResume:
    def setup_method(self):
        self.profile = _parse(MINIMAL_RESUME, source_path="<test>")

    def test_contact_name(self):
        assert self.profile.contact.name == "Test User"

    def test_contact_email(self):
        assert self.profile.contact.email == "test@example.com"

    def test_experience_count(self):
        assert len(self.profile.experience) == 2

    def test_first_job_id(self):
        job = self.profile.experience[0]
        assert job.company == "Acme Corp"
        assert job.start == "2022-01"
        assert job.end is None   # Present

    def test_second_job_end(self):
        job = self.profile.experience[1]
        assert job.end == "2021-12"

    def test_bullet_parsed(self):
        job = self.profile.experience[0]
        assert len(job.bullets) == 1
        b = job.bullets[0]
        assert b.id == "B1-01"
        assert "CORE" in b.tags
        assert "PERF" in b.tags
        assert "50%" in b.resume
        assert b.interview_prep.strip() != ""

    def test_bullet_no_verify(self):
        b = self.profile.experience[0].bullets[0]
        assert b.verify_open is False

    def test_skill_years_python(self):
        yrs = self.profile.skill_years.years_for("python")
        assert yrs >= 4   # since 2020

    def test_skill_years_snowflake(self):
        yrs = self.profile.skill_years.years_for("snowflake")
        assert yrs >= 2   # since 2022-06

    def test_not_yet_kafka(self):
        assert self.profile.is_not_yet("kafka")

    def test_not_yet_zero_years(self):
        assert self.profile.years_for_skill("kafka") == 0

    def test_titles(self):
        assert self.profile.titles.get("DEFAULT") == "Senior Data Engineer"

    def test_summary(self):
        s = self.profile.get_summary("DEFAULT")
        assert s is not None
        assert "4+" in s.text

    def test_education(self):
        assert len(self.profile.education) == 1
        edu = self.profile.education[0]
        assert edu.start == 2016
        assert edu.end == 2020

    def test_languages(self):
        names = [l.name for l in self.profile.languages]
        assert "English" in names
        # French has show_on_resume_for
        fr = next(l for l in self.profile.languages if l.name == "French")
        assert "FR" in fr.show_on_resume_for

    def test_work_auth_default(self):
        assert self.profile.work_authorization.get("DEFAULT") == "requires_sponsorship"

    def test_no_open_items(self):
        assert self.profile.open_items == []

    def test_file_naming(self):
        assert "{Company}" in self.profile.file_naming


# ── Integration test against the real resume ─────────────────────────────────

class TestRealResume:
    def setup_method(self):
        self.profile = _load_real()

    def test_name_bipin(self):
        assert "Bipin" in self.profile.contact.name

    def test_vista_job(self):
        companies = [j.company for j in self.profile.experience]
        assert any("Vista" in c for c in companies)

    def test_barclays_job(self):
        companies = [j.company for j in self.profile.experience]
        assert any("Barclays" in c for c in companies)

    def test_vista_01_bullet(self):
        b = self.profile.get_bullet("VISTA-01")
        assert b is not None, "VISTA-01 bullet must be parsed"
        assert "3.7x" in b.resume
        assert "10h43m" in b.resume
        assert b.verify_open is False   # all items are resolved

    def test_vista_04_alt_enabled(self):
        b = self.profile.get_bullet("VISTA-04")
        assert b is not None
        assert b.alt is not None
        assert b.alt.enabled is True   # decision confirmed

    def test_not_yet_kafka(self):
        assert self.profile.is_not_yet("kafka")

    def test_dbt_years(self):
        # dbt since July 2022 → 4 years as of late 2026
        yrs = self.profile.years_for_skill("dbt")
        assert 3 <= yrs <= 6

    def test_python_years(self):
        # python since July 2017 → 9 years
        yrs = self.profile.years_for_skill("python")
        assert yrs >= 9

    def test_no_open_items(self):
        assert self.profile.open_items == [], (
            f"Unexpected open items: {self.profile.open_items}"
        )

    def test_recipes_count(self):
        assert len(self.profile.tailoring_recipes) >= 4

    def test_bullets_by_id_map(self):
        bmap = self.profile.bullets_by_id
        expected_ids = ["VISTA-01", "VISTA-02", "VISTA-03", "VISTA-04", "BARC-01", "BARC-04"]
        for eid in expected_ids:
            assert eid in bmap, f"Bullet {eid} missing from bullets_by_id"
