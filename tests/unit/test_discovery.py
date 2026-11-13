"""Tests for the discovery pipeline (no network calls, no DB)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jobagent.discovery.base import RawJob
from jobagent.pipeline.ats_detect import detect_ats, extract_ats_slug
from jobagent.pipeline.normalise import (
    detect_country,
    detect_remote_type,
    normalise,
    normalise_company,
    normalise_title,
)


# ── ATS detection ─────────────────────────────────────────────────────────────

class TestAtsDetect:
    @pytest.mark.parametrize("url,expected_ats,expected_slug", [
        ("https://boards.greenhouse.io/stripe/jobs/1234", "greenhouse", "stripe"),
        ("https://jobs.lever.co/notion/abc-123", "lever", "notion"),
        ("https://jobs.ashbyhq.com/motherduck/xyz", "ashby", "motherduck"),
        ("https://snowflake.wd1.myworkdayjobs.com/en-US/External_Careers/job/abc", "workday", "snowflake"),
        ("https://careers.smartrecruiters.com/acmecorp/jobs", "smartrecruiters", None),
        ("mailto:careers@example.com", "email", None),
        ("https://www.linkedin.com/jobs/view/123", "linkedin", None),
        ("https://company.bamboohr.com/jobs/view.php?id=1", "bamboohr", "company"),
    ])
    def test_detect_ats(self, url, expected_ats, expected_slug):
        assert detect_ats(url) == expected_ats

    @pytest.mark.parametrize("url,expected", [
        ("https://boards.greenhouse.io/stripe/jobs/1", ("greenhouse", "stripe")),
        ("https://jobs.lever.co/databricks/abc", ("lever", "databricks")),
        ("https://jobs.ashbyhq.com/clickhouse/xyz", ("ashby", "clickhouse")),
        ("https://company.wd1.myworkdayjobs.com/en-US/Site/job/abc", ("workday", "company")),
        ("https://www.linkedin.com/jobs/view/123", None),
    ])
    def test_extract_slug(self, url, expected):
        assert extract_ats_slug(url) == expected


# ── Normalisation ─────────────────────────────────────────────────────────────

class TestNormaliseHelpers:
    @pytest.mark.parametrize("raw,expected", [
        ("Stripe, Inc.", "stripe"),
        ("ACME GmbH", "acme"),
        ("Vista (Cimpress)", "vista cimpress"),  # parens stripped, space kept
        ("DataBricks", "databricks"),
        ("Some Company, Ltd.", "some company"),
    ])
    def test_normalise_company(self, raw, expected):
        assert normalise_company(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("Senior Data Engineer", "senior data engineer"),
        ("  Lead  Analytics  Engineer  ", "lead analytics engineer"),
        ("Staff DE (Remote)", "staff de (remote)"),  # parens in titles preserved
    ])
    def test_normalise_title(self, raw, expected):
        assert normalise_title(raw) == expected

    @pytest.mark.parametrize("location,hint,expected", [
        ("Berlin, Germany", None, "DE"),
        ("Amsterdam, Netherlands", None, "NL"),
        ("London, UK", None, "GB"),
        ("Bangalore, India", None, "IN"),
        ("Dubai, UAE", None, "AE"),
        ("Singapore", None, "SG"),
        ("Sydney, Australia", None, "AU"),
        ("Tokyo, Japan", None, "JP"),
        (None, "DE", "DE"),
        (None, None, None),
    ])
    def test_detect_country(self, location, hint, expected):
        assert detect_country(location, hint) == expected

    @pytest.mark.parametrize("location,hint,expected", [
        ("Remote", None, "remote"),
        ("Berlin (Hybrid)", None, "hybrid"),
        ("Berlin, Germany", None, "onsite"),
        (None, "remote", "remote"),
        ("Remote / Hybrid", None, "remote"),
    ])
    def test_detect_remote_type(self, location, hint, expected):
        assert detect_remote_type(location, hint) == expected


class TestNormalise:
    def _make_raw(self, **kwargs) -> RawJob:
        defaults = dict(
            source="test",
            source_job_id="job-001",
            url="https://boards.greenhouse.io/stripe/jobs/1",
            company="Stripe, Inc.",
            title="Senior Data Engineer",
            location_raw="Berlin, Germany",
            description_html="<p>Build <b>cool</b> stuff.</p>",
            posted_at=datetime.now(timezone.utc),
            salary_min=90_000,
            salary_max=120_000,
            currency="EUR",
        )
        defaults.update(kwargs)
        return RawJob(**defaults)

    def test_basic_normalisation(self):
        job = normalise(self._make_raw())
        assert job.title == "Senior Data Engineer"
        assert job.title_norm == "senior data engineer"
        assert job.country == "DE"
        assert job.ats_type == "greenhouse"
        assert job.salary_min == 90_000
        assert job.content_hash  # non-empty

    def test_html_to_markdown(self):
        job = normalise(self._make_raw())
        assert job.description_md is not None
        assert "<" not in job.description_md  # no raw HTML tags left

    def test_remote_type_detection(self):
        job = normalise(self._make_raw(location_raw="Remote"))
        assert job.remote_type == "remote"

    def test_content_hash_deterministic(self):
        a = normalise(self._make_raw())
        b = normalise(self._make_raw())
        assert a.content_hash == b.content_hash

    def test_different_jobs_different_hash(self):
        a = normalise(self._make_raw(title="Senior Data Engineer"))
        b = normalise(self._make_raw(title="Analytics Engineer"))
        assert a.content_hash != b.content_hash

    def test_prefer_description_md(self):
        job = normalise(self._make_raw(description_md="# Already markdown", description_html="<p>HTML</p>"))
        assert job.description_md == "# Already markdown"


# ── Company universe (no DB) ──────────────────────────────────────────────────

class TestCompanyUniverseSeeds:
    def test_seed_lists_non_empty(self):
        from jobagent.discovery.company_universe import GREENHOUSE_SEEDS, LEVER_SEEDS, ASHBY_SEEDS
        assert len(GREENHOUSE_SEEDS) >= 20
        assert len(LEVER_SEEDS) >= 5
        assert len(ASHBY_SEEDS) >= 3

    def test_all_seeds_are_strings(self):
        from jobagent.discovery.company_universe import GREENHOUSE_SEEDS, LEVER_SEEDS, ASHBY_SEEDS
        for slug in GREENHOUSE_SEEDS + LEVER_SEEDS + ASHBY_SEEDS:
            assert isinstance(slug, str) and slug.strip() == slug
