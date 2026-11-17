"""Tests for country detection and the hard-filter rules.

These cover the regressions behind "US jobs leak into the queue":
the country keyword table had no US/CA entries, and ``country=None`` was
treated as acceptable for any source.
"""

from __future__ import annotations

import pytest

from jobagent.config import load_yaml
from jobagent.db.models import Job
from jobagent.pipeline.filters import _salary_floor, apply_filters
from jobagent.pipeline.normalise import detect_country


def _make_job(**kwargs) -> Job:
    defaults = {
        "id": 1,
        "source": "greenhouse",
        "source_job_id": "f-001",
        "url": "https://boards.greenhouse.io/testco/jobs/1",
        "title": "Senior Data Engineer",
        "title_norm": "senior data engineer",
        "ats_type": "greenhouse",
        "content_hash": "hash",
        "country": "DE",
        "remote_type": "remote",
        "description_md": "Python Spark dbt Snowflake data engineer role",
    }
    defaults.update(kwargs)
    return Job(**defaults)


@pytest.fixture(scope="module")
def search_cfg():
    return load_yaml("search")


# ---------------------------------------------------------------------------
# Country detection
# ---------------------------------------------------------------------------

class TestDetectCountry:
    @pytest.mark.parametrize(
        "location",
        [
            "United States",
            "Remote - US",
            "Remote, US",
            "New York, NY",
            "San Francisco, CA",
            "Austin, TX",
            "Seattle, WA",
            "Chicago, IL",
            "Denver, CO",
            "Indianapolis, IN",
            "Boston, Massachusetts",
        ],
    )
    def test_us_locations(self, location: str) -> None:
        assert detect_country(location) == "US"

    @pytest.mark.parametrize(
        "location",
        ["Toronto, Canada", "Vancouver, BC", "Montreal, Quebec", "Ottawa, Ontario"],
    )
    def test_canada_locations(self, location: str) -> None:
        assert detect_country(location) == "CA"

    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            ("Berlin, Germany", "DE"),
            ("Kolkata, India", "IN"),
            ("Bengaluru, India", "IN"),
            ("Amsterdam, Netherlands", "NL"),
            ("London, United Kingdom", "GB"),
        ],
    )
    def test_other_countries_unchanged(self, location: str, expected: str) -> None:
        assert detect_country(location) == expected

    def test_indianapolis_is_not_italy(self) -> None:
        # Regression: "napoli" is a substring of "indianapolis".
        assert detect_country("Indianapolis, IN") != "IT"

    def test_unknown_location_is_none(self) -> None:
        assert detect_country("Remote") is None
        assert detect_country("") is None
        assert detect_country(None) is None


# ---------------------------------------------------------------------------
# Unknown-country rule
# ---------------------------------------------------------------------------

class TestUnknownCountryRule:
    def test_aggregator_unknown_country_rejected(self, search_cfg) -> None:
        job = _make_job(source="jooble", country=None, remote_type="remote")
        result = apply_filters(job, search_cfg, {})
        assert not result.passed
        assert "unverifiable country" in result.reason

    def test_direct_ats_unknown_country_rejected(self, search_cfg) -> None:
        job = _make_job(source="greenhouse", country=None, remote_type="onsite")
        result = apply_filters(job, search_cfg, {})
        assert not result.passed
        assert "country could not be determined" in result.reason

    def test_global_remote_unknown_country_allowed(self, search_cfg) -> None:
        job = _make_job(
            source="greenhouse",
            country=None,
            remote_type="remote",
            description_md=(
                "Senior data engineer. This is a remote role open to candidates "
                "anywhere in the world."
            ),
        )
        result = apply_filters(job, search_cfg, {})
        assert result.passed, result.reason

    def test_us_country_rejected_by_allowlist(self, search_cfg) -> None:
        job = _make_job(country="US")
        result = apply_filters(job, search_cfg, {})
        assert not result.passed
        assert "not in allowlist" in result.reason


# ---------------------------------------------------------------------------
# Salary floor
# ---------------------------------------------------------------------------

class TestSalaryFloor:
    def test_floor_lookup(self) -> None:
        answers = {
            "compensation": {
                "DE": {"currency": "EUR", "floor": 75000},
                "IN": {"currency": "INR", "filter_floor_lpa": 58},
            }
        }
        assert _salary_floor("DE", answers) == (75000.0, "EUR")
        assert _salary_floor("IN", answers) == (5_800_000.0, "INR")
        assert _salary_floor("JP", answers) == (None, None)

    def test_below_floor_rejected(self, search_cfg) -> None:
        answers = {"compensation": {"DE": {"currency": "EUR", "floor": 75000}}}
        job = _make_job(
            country="DE", salary_min=50000, salary_max=60000, currency="EUR"
        )
        result = apply_filters(job, search_cfg, answers)
        assert not result.passed
        assert "below floor" in result.reason

    def test_above_floor_passes(self, search_cfg) -> None:
        answers = {"compensation": {"DE": {"currency": "EUR", "floor": 75000}}}
        job = _make_job(
            country="DE", salary_min=80000, salary_max=95000, currency="EUR"
        )
        result = apply_filters(job, search_cfg, answers)
        assert result.passed, result.reason

    def test_currency_mismatch_not_enforced(self, search_cfg) -> None:
        # A USD figure must not be compared against a EUR floor.
        answers = {"compensation": {"DE": {"currency": "EUR", "floor": 75000}}}
        job = _make_job(
            country="DE", salary_min=40000, salary_max=50000, currency="USD"
        )
        result = apply_filters(job, search_cfg, answers)
        assert result.passed, result.reason


# ---------------------------------------------------------------------------
# India salary answer
# ---------------------------------------------------------------------------

class TestIndiaSalaryAnswer:
    def test_india_uses_lpa_not_eur_fallback(self) -> None:
        from jobagent.tailor.answers import get_salary_answer

        seed = {
            "compensation": {
                "IN": {"currency": "INR", "current_ctc_lpa": 20, "expected_ctc_lpa": 30},
                "DE": {"currency": "EUR", "period": "year", "expected": 85000},
            }
        }
        answer = get_salary_answer("IN", "year", seed)
        assert answer == "30 LPA INR"
        assert "EUR" not in answer
