"""Tests for the ats_board registry: status rules, CompanyUniverse, probes, collectors."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlmodel import select

from jobagent.db.models import AtsBoard, Company, Job
from jobagent.discovery.boards import (
    DEAD_AFTER_FAILURES,
    EMPTY_POLL_INTERVAL,
    BoardOutcome,
    BoardRef,
    apply_outcome,
    is_due_for_poll,
    is_due_for_validation,
    probe_board,
    utcnow,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "test.db"))
    import jobagent.config as cfg
    import jobagent.db.engine as eng

    eng._engine = None  # type: ignore[attr-defined]
    cfg.settings = cfg.Settings()
    from jobagent.db.engine import create_tables

    create_tables()
    yield
    eng._engine = None  # type: ignore[attr-defined]
    cfg.settings = cfg.Settings()


def _board(**kw) -> AtsBoard:
    defaults = {"ats_type": "greenhouse", "slug": "acme", "status": "candidate"}
    defaults.update(kw)
    return AtsBoard(**defaults)


# ── Status transitions ────────────────────────────────────────────────────────

class TestApplyOutcome:
    def test_ok_with_jobs_is_live(self):
        b = _board()
        apply_outcome(b, BoardOutcome("ok", job_count=12, matched_count=3, company_name="Acme"))
        assert (b.status, b.job_count, b.matched_count, b.company_name) == ("live", 12, 3, "Acme")
        assert b.consecutive_failures == 0
        assert b.last_error is None
        assert b.last_checked_at is not None
        assert b.last_job_change_at is not None
        assert b.last_polled_at is None  # validator probe, not a poll

    def test_ok_without_jobs_is_empty(self):
        b = _board()
        apply_outcome(b, BoardOutcome("ok", job_count=0, polled=True))
        assert b.status == "empty"
        assert b.last_polled_at is not None

    def test_ok_unknown_count_keeps_previous(self):
        b = _board(status="live", job_count=40)
        apply_outcome(b, BoardOutcome("ok", job_count=None, matched_count=2))
        assert b.status == "live"
        assert b.job_count == 40
        assert b.matched_count == 2

    def test_candidate_404_dies_immediately(self):
        b = _board()
        apply_outcome(b, BoardOutcome("not_found"))
        assert b.status == "dead"

    def test_live_board_survives_until_threshold(self):
        b = _board(status="live", job_count=5)
        for _ in range(DEAD_AFTER_FAILURES - 1):
            apply_outcome(b, BoardOutcome("not_found"))
        assert b.status == "live"
        apply_outcome(b, BoardOutcome("not_found"))
        assert b.status == "dead"
        assert b.job_count == 0

    def test_error_does_not_change_status(self):
        b = _board(status="live")
        for _ in range(10):
            apply_outcome(b, BoardOutcome("error", error="timeout"))
        assert b.status == "live"
        assert b.consecutive_failures == 10
        assert b.last_error == "timeout"

    def test_candidate_that_keeps_erroring_dies(self):
        b = _board(status="candidate")
        for _ in range(DEAD_AFTER_FAILURES - 1):
            apply_outcome(b, BoardOutcome("error", error="HTTP 401"))
        assert b.status == "candidate"
        apply_outcome(b, BoardOutcome("error", error="HTTP 401"))
        assert b.status == "dead"

    def test_success_resets_failures(self):
        b = _board(status="live", consecutive_failures=2)
        apply_outcome(b, BoardOutcome("ok", job_count=1))
        assert b.consecutive_failures == 0

    def test_dead_board_can_come_back(self):
        b = _board(status="dead", consecutive_failures=5)
        apply_outcome(b, BoardOutcome("ok", job_count=3))
        assert b.status == "live"


class TestScheduling:
    def test_poll_due(self):
        now = utcnow()
        assert is_due_for_poll(_board(status="live"), now)
        assert is_due_for_poll(_board(status="candidate"), now)
        assert not is_due_for_poll(_board(status="dead"), now)
        assert is_due_for_poll(_board(status="empty"), now)  # never polled
        recent = _board(status="empty", last_polled_at=now - timedelta(hours=1))
        stale = _board(status="empty", last_polled_at=now - EMPTY_POLL_INTERVAL)
        assert not is_due_for_poll(recent, now)
        assert is_due_for_poll(stale, now)

    def test_naive_datetimes_from_sqlite(self):
        now = utcnow()
        naive = (now - timedelta(hours=1)).replace(tzinfo=None)
        assert not is_due_for_poll(_board(status="empty", last_polled_at=naive), now)

    def test_validation_due(self):
        now = utcnow()
        assert is_due_for_validation(_board(status="candidate"), now)
        assert not is_due_for_validation(_board(status="live"), now)
        assert not is_due_for_validation(_board(status="dead", last_checked_at=now), now)
        assert is_due_for_validation(
            _board(status="dead", last_checked_at=now - timedelta(days=31)), now
        )


# ── Workday URL parsing ──────────────────────────────────────────────────────

class TestWorkdayUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x_1",
             ("nvidia", "nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite")),
            ("https://hsbc.wd3.myworkdayjobs.com/HSBCGlobalCareers",
             ("hsbc", "hsbc.wd3.myworkdayjobs.com", "HSBCGlobalCareers")),
            ("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Ext/jobs",
             ("acme", "acme.wd1.myworkdayjobs.com", "Ext")),
            ("https://acme.wd1.myworkdayjobs.com/", None),
            ("https://acme.wd1.myworkdayjobs.com/en-US/job/x", None),
            ("https://boards.greenhouse.io/acme", None),
        ],
    )
    def test_extract(self, url, expected):
        from jobagent.pipeline.ats_detect import extract_workday_board

        assert extract_workday_board(url) == expected

    def test_board_key(self):
        from jobagent.discovery.company_universe import board_key_from_url

        assert board_key_from_url("https://job-boards.eu.greenhouse.io/Valtech/jobs/1") == (
            "greenhouse", "valtech", "", None)
        assert board_key_from_url("https://acme.wd3.myworkdayjobs.com/") is None
        assert board_key_from_url("https://example.com/careers") is None


# ── CompanyUniverse (DB) ─────────────────────────────────────────────────────

class TestCompanyUniverse:
    def test_add_board_idempotent_and_lowercases(self, tmp_db):
        from jobagent.discovery.company_universe import CompanyUniverse

        u = CompanyUniverse()
        assert u.add_board("greenhouse", "Acme", discovered_via="guess")
        assert not u.add_board("greenhouse", "acme")
        assert u.add_board("lever", "acme")  # same slug, different ATS
        assert u.all_slugs("greenhouse") == {"acme"}

    def test_workday_keeps_board_case_and_allows_multiple_boards(self, tmp_db):
        from jobagent.discovery.company_universe import CompanyUniverse

        u = CompanyUniverse()
        assert u.add_from_url("https://acme.wd3.myworkdayjobs.com/en-US/External/job/a")
        assert u.add_from_url("https://acme.wd3.myworkdayjobs.com/en-US/Campus/job/b")
        assert not u.add_from_url("https://acme.wd3.myworkdayjobs.com/External/job/c")
        boards = u.get_boards("workday")
        assert sorted(b.board_path for b in boards) == ["Campus", "External"]
        assert all(b.host == "acme.wd3.myworkdayjobs.com" for b in boards)

    def test_get_boards_skips_dead_and_recent_empty(self, tmp_db):
        from jobagent.db.engine import get_session
        from jobagent.discovery.company_universe import CompanyUniverse

        now = utcnow()
        with get_session() as s:
            s.add(_board(slug="live1", status="live"))
            s.add(_board(slug="cand", status="candidate"))
            s.add(_board(slug="gone", status="dead"))
            s.add(_board(slug="quiet", status="empty", last_polled_at=now))
            s.commit()
        u = CompanyUniverse()
        assert {b.slug for b in u.get_boards("greenhouse")} == {"live1", "cand"}
        assert {b.slug for b in u.get_boards("greenhouse", due_only=False)} == {
            "live1", "cand", "quiet"}

    def test_feed_jobs_adds_flywheel_boards(self, tmp_db):
        from jobagent.discovery.base import RawJob
        from jobagent.discovery.company_universe import CompanyUniverse

        raws = [
            RawJob(source="adzuna", source_job_id="1", url="https://x",
                   apply_url="https://boards.greenhouse.io/newco/jobs/1"),
            RawJob(source="adzuna", source_job_id="2", url="https://jobs.lever.co/leverco/abc"),
            RawJob(source="adzuna", source_job_id="3", url="https://example.com/job"),
            # jobs from an ATS collector are not re-fed
            RawJob(source="greenhouse", source_job_id="4",
                   url="https://boards.greenhouse.io/already/jobs/4"),
        ]
        u = CompanyUniverse()
        u.ensure_initialized()
        before = u.all_slugs("greenhouse")
        assert u.feed_jobs(raws) == 2
        assert u.all_slugs("greenhouse") - before == {"newco"}
        assert "leverco" in u.all_slugs("lever")

    def test_record_outcomes_links_company_by_real_name(self, tmp_db):
        from jobagent.db.engine import get_session
        from jobagent.discovery.company_universe import CompanyUniverse

        with get_session() as s:
            placeholder = Company(name="acme", name_norm="acme")
            existing = Company(name="Acme Robotics GmbH", name_norm="acme robotics")
            s.add(placeholder)
            s.add(existing)
            s.commit()
            s.refresh(placeholder)
            s.refresh(existing)
            b1 = _board(slug="acme", company_id=placeholder.id)
            b2 = _board(slug="solo", company_id=None)
            s.add(b1)
            s.add(b2)
            s.commit()
            s.refresh(b1)
            s.refresh(b2)
            ids = (b1.id, b2.id, existing.id)

        CompanyUniverse().record_outcomes([
            (ids[0], BoardOutcome("ok", job_count=4, company_name="Acme Robotics")),
            (ids[1], BoardOutcome("not_found")),
        ])
        with get_session() as s:
            b1 = s.get(AtsBoard, ids[0])
            b2 = s.get(AtsBoard, ids[1])
            assert b1.status == "live"
            assert b1.company_name == "Acme Robotics"
            assert b1.company_id == ids[2]  # linked to the existing real company
            assert b2.status == "dead"

    def test_placeholder_company_renamed_when_no_match(self, tmp_db):
        from jobagent.db.engine import get_session
        from jobagent.discovery.company_universe import CompanyUniverse

        with get_session() as s:
            c = Company(name="zeta", name_norm="zeta")
            s.add(c)
            s.commit()
            s.refresh(c)
            b = _board(slug="zeta", company_id=c.id)
            s.add(b)
            s.commit()
            s.refresh(b)
            bid, cid = b.id, c.id
        CompanyUniverse().record_outcomes([(bid, BoardOutcome("ok", job_count=1, company_name="Zeta Labs"))])
        with get_session() as s:
            c = s.get(Company, cid)
            assert (c.name, c.name_norm) == ("Zeta Labs", "zeta labs")

    def test_backfill_from_company_and_jobs(self, tmp_db):
        from jobagent.db.engine import get_session
        from jobagent.discovery.company_universe import CompanyUniverse

        with get_session() as s:
            s.add(Company(name="alive", name_norm="alive", ats_type="greenhouse", ats_slug="alive"))
            s.add(Company(name="gone", name_norm="gone", ats_type="greenhouse", ats_slug="gone",
                          blacklisted=True))
            s.add(Company(name="nowd", name_norm="nowd", ats_type="workday", ats_slug="nowdtenant"))
            agg = Company(name="Agg Co", name_norm="agg co")
            s.add(agg)
            s.commit()
            s.refresh(agg)
            s.add(Job(company_id=agg.id, source="adzuna", source_job_id="1",
                      url="https://adzuna/x", apply_url="https://jobs.ashbyhq.com/aggco/123",
                      title="DE", title_norm="de", content_hash="h1"))
            s.commit()

        counts = CompanyUniverse().backfill()
        assert counts["from_company"] == 2
        assert counts["legacy_dead"] == 1
        assert counts["workday_missing_board"] == 1
        assert counts["from_jobs"] == 1
        assert counts["seeds"] > 50

        with get_session() as s:
            def get(ats, slug):
                return s.exec(select(AtsBoard).where(
                    AtsBoard.ats_type == ats, AtsBoard.slug == slug)).first()
            assert get("greenhouse", "alive").status == "candidate"
            assert get("greenhouse", "gone").status == "dead"
            ashby = get("ashby", "aggco")
            assert ashby.discovered_via == "backfill"
            assert ashby.company_name == "Agg Co"
            assert ashby.company_id is not None

        # Idempotent
        again = CompanyUniverse().backfill()
        assert again.get("from_company", 0) == 0
        assert again["seeds"] == 0

    def test_mark_dead_legacy_helper(self, tmp_db):
        from jobagent.discovery.company_universe import CompanyUniverse

        u = CompanyUniverse()
        u.add_board("ashby", "nobody")
        u.mark_dead("ashby", "nobody")
        assert "nobody" not in {b.slug for b in u.get_boards("ashby")}


# ── Probes (HTTP mocked) ─────────────────────────────────────────────────────

def _resp(status: int, json=None, method: str = "GET"):
    return httpx.Response(status, json=json, request=httpx.Request(method, "https://x"))


class TestProbes:
    @pytest.mark.asyncio
    async def test_greenhouse_ok(self):
        get = AsyncMock(side_effect=[
            _resp(200, {"name": "GitLab"}),
            _resp(200, {"jobs": [{"id": 1}], "meta": {"total": 254}}),
        ])
        with patch.object(httpx.AsyncClient, "get", new=get):
            async with httpx.AsyncClient() as c:
                r = await probe_board(c, BoardRef(1, "greenhouse", "gitlab"))
        assert (r.outcome, r.job_count, r.company_name) == ("ok", 254, "GitLab")

    @pytest.mark.asyncio
    async def test_greenhouse_404(self):
        with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=_resp(404))):
            async with httpx.AsyncClient() as c:
                r = await probe_board(c, BoardRef(1, "greenhouse", "nope"))
        assert r.outcome == "not_found"

    @pytest.mark.asyncio
    async def test_lever_and_ashby(self):
        with patch.object(httpx.AsyncClient, "get",
                          new=AsyncMock(return_value=_resp(200, [{"id": "a"}, {"id": "b"}]))):
            async with httpx.AsyncClient() as c:
                r = await probe_board(c, BoardRef(1, "lever", "palantir"))
        assert (r.outcome, r.job_count) == ("ok", 2)
        with patch.object(httpx.AsyncClient, "get",
                          new=AsyncMock(return_value=_resp(200, {"jobs": [{}, {}, {}]}))):
            async with httpx.AsyncClient() as c:
                r = await probe_board(c, BoardRef(1, "ashby", "supabase"))
        assert (r.outcome, r.job_count) == ("ok", 3)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "outcome"), [(422, "not_found"), (404, "not_found"),
                                                     (503, "error")])
    async def test_workday_status_mapping(self, status, outcome):
        with patch.object(httpx.AsyncClient, "post",
                          new=AsyncMock(return_value=_resp(status, {}, "POST"))):
            async with httpx.AsyncClient() as c:
                r = await probe_board(
                    c, BoardRef(1, "workday", "acme", "Ext", "acme.wd3.myworkdayjobs.com"))
        assert r.outcome == outcome

    @pytest.mark.asyncio
    async def test_workday_missing_board_is_error(self):
        async with httpx.AsyncClient() as c:
            r = await probe_board(c, BoardRef(1, "workday", "acme"))
        assert r.outcome == "error"

    @pytest.mark.asyncio
    async def test_network_error_never_raises(self):
        with patch.object(httpx.AsyncClient, "get",
                          new=AsyncMock(side_effect=httpx.ConnectTimeout("slow"))):
            async with httpx.AsyncClient() as c:
                r = await probe_board(c, BoardRef(1, "lever", "x"))
        assert r.outcome == "error"


# ── Collectors write back to the registry ────────────────────────────────────

class TestCollectorsUseRegistry:
    @pytest.mark.asyncio
    async def test_greenhouse_collector_promotes_and_kills(self, tmp_db):
        from jobagent.db.engine import get_session
        from jobagent.discovery.collectors.greenhouse import GreenhouseCollector
        from jobagent.discovery.company_universe import CompanyUniverse

        with get_session() as s:
            s.add(_board(slug="placeholder-to-skip-backfill", status="dead"))
            s.commit()
        u = CompanyUniverse()
        u.add_board("greenhouse", "good")
        u.add_board("greenhouse", "bad")

        payload = {"jobs": [
            {"id": 1, "title": "Senior Data Engineer", "absolute_url": "https://g/1",
             "company_name": "Good Inc", "first_published": "2026-11-01T10:00:00Z",
             "updated_at": "2026-11-10T10:00:00Z", "location": {"name": "Berlin"},
             "offices": [{"name": "Berlin"}]},
            {"id": 2, "title": "Office Manager", "absolute_url": "https://g/2",
             "company_name": "Good Inc"},
        ]}

        async def fake_get(self, url, *a, **kw):
            if "/boards/good/" in url:
                return _resp(200, payload)
            return _resp(404)

        with patch.object(httpx.AsyncClient, "get", new=fake_get):
            jobs = await GreenhouseCollector().fetch({"titles": ["data engineer"]})

        assert len(jobs) == 1
        j = jobs[0]
        assert j.company == "Good Inc"
        assert j.posted_at.day == 1  # first_published, not updated_at
        assert j.country == "DE"

        with get_session() as s:
            rows = {b.slug: b for b in s.exec(select(AtsBoard)).all()}
        assert rows["good"].status == "live"
        assert (rows["good"].job_count, rows["good"].matched_count) == (2, 1)
        assert rows["good"].company_name == "Good Inc"
        assert rows["good"].last_polled_at is not None
        assert rows["bad"].status == "dead"

    @pytest.mark.asyncio
    async def test_ashby_collector_reads_jobs_key(self, tmp_db):
        from jobagent.db.engine import get_session
        from jobagent.discovery.collectors.ashby import AshbyCollector
        from jobagent.discovery.company_universe import CompanyUniverse

        with get_session() as s:
            s.add(_board(ats_type="greenhouse", slug="skip-backfill", status="dead"))
            s.commit()
        CompanyUniverse().add_board("ashby", "supa")
        payload = {"apiVersion": "1", "jobs": [
            {"id": "a1", "title": "Data Engineer", "location": "Remote, EMEA",
             "jobUrl": "https://jobs.ashbyhq.com/supa/a1", "applyUrl": "https://jobs.ashbyhq.com/supa/a1/application",
             "publishedAt": "2026-11-10T16:33:17.148+00:00", "isListed": True, "isRemote": True,
             "workplaceType": "Remote"},
            {"id": "a2", "title": "Data Engineer (hidden)", "isListed": False},
        ]}
        with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=_resp(200, payload))):
            jobs = await AshbyCollector().fetch({"titles": ["data engineer"]})
        assert len(jobs) == 1
        assert jobs[0].location_raw == "Remote, EMEA"
        assert jobs[0].apply_url.endswith("/application")
        assert jobs[0].remote_type == "remote"


class TestWorkdayParsing:
    def test_relative_posted(self):
        from datetime import datetime, timezone

        from jobagent.discovery.collectors.workday import _parse_relative_posted

        now = datetime(2026, 11, 13, tzinfo=timezone.utc)
        assert _parse_relative_posted("Posted Today", now) == now
        assert _parse_relative_posted("Posted Yesterday", now).day == 12
        assert _parse_relative_posted("Posted 3 Days Ago", now).day == 10
        assert _parse_relative_posted("Posted 30+ Days Ago", now).month == 10
        assert _parse_relative_posted("whenever", now) is None

    @pytest.mark.asyncio
    async def test_title_filter_and_locations_text(self):
        import asyncio

        from jobagent.discovery.collectors.workday import WorkdayCollector

        payload = {"total": 2, "jobPostings": [
            {"title": "Senior Data Engineer", "externalPath": "/job/SG/x_1",
             "locationsText": "Singapore - East", "postedOn": "Posted Today"},
            {"title": "Senior Associate, Product Management", "externalPath": "/job/IN/y_2",
             "locationsText": "Mumbai", "postedOn": "Posted Today"},
        ]}
        board = BoardRef(1, "workday", "dbs", "DBS_Careers", "dbs.wd3.myworkdayjobs.com", "DBS")
        with patch.object(httpx.AsyncClient, "post",
                          new=AsyncMock(return_value=_resp(200, payload, "POST"))):
            async with httpx.AsyncClient() as c:
                jobs, outcome = await WorkdayCollector()._fetch_board(
                    c, asyncio.Semaphore(1), board, ["data engineer"], ["data engineer"])
        assert [j.title for j in jobs] == ["Senior Data Engineer"]
        assert jobs[0].location_raw == "Singapore - East"
        assert jobs[0].country == "SG"
        assert jobs[0].posted_at is not None
        assert outcome.job_count == 2 and outcome.matched_count == 1


class TestSlugGuessNameMatch:
    @pytest.fixture(autouse=True)
    def _path(self, monkeypatch):
        import pathlib
        import sys

        monkeypatch.syspath_prepend(str(pathlib.Path(__file__).parents[2] / "scripts"))
        sys.modules.pop("discover_greenhouse_slugs", None)

    @pytest.mark.parametrize(
        ("board", "sources", "expected"),
        [
            ("Block", {"Block, Inc."}, True),
            ("Co–Star", {"CoStar Group"}, True),
            ("Samsara", {"Samsara Inc"}, True),
            ("Aimé Leon Dore EU", {"London Stock Exchange Group"}, False),
            ("Upwork- Canada", {"Canada Pension Plan Investment Board"}, False),
            ("Monetary", {"Monetary Authority of Singapore"}, False),
            ("Slice Talent Community", {"TALENT INTERNATIONAL UK LTD"}, False),
            (None, {"Acme"}, False),
        ],
    )
    def test_names_match(self, board, sources, expected):
        from discover_greenhouse_slugs import names_match

        assert names_match(board, sources) is expected

    def test_us_detection_uses_location_name(self):
        from discover_greenhouse_slugs import is_us_only

        assert is_us_only([{"location": {"name": "New York, NY"}}])
        assert not is_us_only([{"location": {"name": "London, UK"}}])
