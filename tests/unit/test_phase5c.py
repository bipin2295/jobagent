"""Phase 5c integration tests — Workday, vault, email provider, account manager.

All tests are offline (no network, no live browser, no Gmail token required).
They use a temporary SQLite DB so they don't touch the real data/jobagent.db.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point the DB engine at a fresh temp file for each test."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(db_file))
    # Reset both the engine cache AND the settings singleton so they
    # re-read the new JOBAGENT_DB_PATH env variable.
    import jobagent.db.engine as eng
    import jobagent.config as cfg
    eng._engine = None  # type: ignore[attr-defined]
    cfg.settings = cfg.Settings()  # re-read env
    from jobagent.db.engine import create_tables
    create_tables()
    yield db_file
    eng._engine = None  # type: ignore[attr-defined]
    cfg.settings = cfg.Settings()  # restore defaults


@pytest.fixture()
def tmp_vault_dir(tmp_path, monkeypatch):
    """Redirect the file-based vault to a temp directory."""
    vault_file = tmp_path / "vault" / ".keyring.json"
    import jobagent.vault as v
    monkeypatch.setattr(v, "_FALLBACK_PATH", vault_file)
    yield vault_file


# ---------------------------------------------------------------------------
# 1. Imports — all Phase 5c modules must import without error
# ---------------------------------------------------------------------------

class TestImports:
    def test_workday_collector(self):
        from jobagent.discovery.collectors.workday import WorkdayCollector
        assert WorkdayCollector().name == "workday"

    def test_vault(self):
        from jobagent.vault import CredentialVault
        assert CredentialVault is not None

    def test_email_provider(self):
        from jobagent.email_provider import EmailProvider
        assert EmailProvider is not None

    def test_account_manager(self):
        from jobagent.apply.account_manager import AccountManager, make_alias
        assert AccountManager is not None
        assert make_alias("workday", "acme") == "bipin2295+workday-acme@gmail.com"

    def test_workday_handler(self):
        from jobagent.apply.handlers.workday import WorkdayHandler
        h = WorkdayHandler()
        assert h.ats_type == "workday"
        assert h.name == "workday"

    def test_workday_handler_registered(self):
        """WorkdayHandler must appear in the apply orchestrator handler list."""
        from jobagent.apply.orchestrator import _load_handlers
        handlers = _load_handlers()
        names = [h.name for h in handlers]
        assert "workday" in names


# ---------------------------------------------------------------------------
# 2. CredentialVault — file-based backend (no system keychain in Docker)
# ---------------------------------------------------------------------------

class TestCredentialVault:
    def test_generate_and_retrieve(self, tmp_vault_dir):
        from jobagent.vault import CredentialVault
        vault = CredentialVault()
        vault._backend = "file"  # force file backend

        pw = vault.get_or_create("workday", "acme")
        assert len(pw) == 24
        # Second call returns the same password
        assert vault.get_or_create("workday", "acme") == pw

    def test_different_tenants_different_passwords(self, tmp_vault_dir):
        from jobagent.vault import CredentialVault
        vault = CredentialVault()
        vault._backend = "file"
        pw_a = vault.get_or_create("workday", "acme")
        pw_b = vault.get_or_create("workday", "stripe")
        assert pw_a != pw_b

    def test_password_complexity(self, tmp_vault_dir):
        from jobagent.vault import CredentialVault, _generate_password
        for _ in range(20):
            pw = _generate_password()
            assert len(pw) == 24
            assert any(c.isupper() for c in pw)
            assert any(c.islower() for c in pw)
            assert any(c.isdigit() for c in pw)
            assert any(c in "!@#$%^&*" for c in pw)

    def test_set_and_delete(self, tmp_vault_dir):
        from jobagent.vault import CredentialVault
        vault = CredentialVault()
        vault._backend = "file"
        vault.set("workday", "foo", "secret123!")
        assert vault.get("workday", "foo") == "secret123!"
        vault.delete("workday", "foo")
        assert vault.get("workday", "foo") is None

    def test_keychain_ref_format(self, tmp_vault_dir):
        from jobagent.vault import CredentialVault
        vault = CredentialVault()
        ref = vault.keychain_ref("workday", "acme")
        assert "workday" in ref
        assert "acme" in ref


# ---------------------------------------------------------------------------
# 3. EmailProvider — graceful no-token degradation
# ---------------------------------------------------------------------------

class TestEmailProvider:
    def test_not_connected_when_no_token(self, tmp_path, monkeypatch):
        """EmailProvider must degrade gracefully when no Gmail token exists."""
        missing = tmp_path / "nonexistent_token.json"
        # Patch both the module-level constant AND the instance lookup path
        monkeypatch.setattr("jobagent.email_provider._TOKEN_PATH", missing)
        # Create a fresh instance that reads from the patched path
        from jobagent.email_provider import EmailProvider
        import jobagent.email_provider as ep
        orig = ep._TOKEN_PATH
        ep._TOKEN_PATH = missing
        try:
            provider = EmailProvider()
            assert not provider.connected
        finally:
            ep._TOKEN_PATH = orig

    @pytest.mark.asyncio
    async def test_wait_returns_not_found_when_disconnected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jobagent.email_provider._TOKEN_PATH",
            tmp_path / "nonexistent_token.json",
        )
        from importlib import reload
        import jobagent.email_provider as ep
        reload(ep)
        provider = ep.EmailProvider()
        result = await provider.wait_for_verification_email(
            alias="bipin2295+test@gmail.com",
            since=datetime.now(timezone.utc),
            timeout_seconds=1,  # very short — should return immediately
        )
        assert not result.found
        assert result.link is None
        assert result.otp is None

    def test_otp_extraction(self):
        from jobagent.email_provider import _extract_otp
        assert _extract_otp("Your verification code is 123456", "code 123456") == "123456"
        assert _extract_otp("Code: 7890", "7890") == "7890"
        assert _extract_otp("No code here", "no digits") is None

    def test_link_extraction(self):
        from jobagent.email_provider import _extract_verification_link
        body = "Click here to verify your email: https://workday.com/verify?token=abc123"
        link = _extract_verification_link(body)
        assert link is not None
        assert "verify" in link

    def test_query_builder_alias(self):
        from jobagent.email_provider import _build_query
        q = _build_query(
            alias="bipin2295+workday-acme@gmail.com",
            sender_domain=None,
            since=datetime(2024, 1, 1, tzinfo=timezone.utc),
            subject_hint="",
        )
        assert "deliveredto:bipin2295+workday-acme@gmail.com" in q
        assert "after:" in q

    def test_query_builder_domain(self):
        from jobagent.email_provider import _build_query
        q = _build_query(
            alias=None,
            sender_domain="workday.com",
            since=datetime(2024, 1, 1, tzinfo=timezone.utc),
            subject_hint="verify",
        )
        assert "from:workday.com" in q
        assert "subject:verify" in q


# ---------------------------------------------------------------------------
# 4. AccountManager — alias scheme, DB persistence
# ---------------------------------------------------------------------------

class TestAccountManager:
    def test_alias_format(self):
        from jobagent.apply.account_manager import make_alias
        alias = make_alias("workday", "deutschebank")
        assert alias.startswith("bipin2295+")
        assert alias.endswith("@gmail.com")
        # Must not be too long for email systems
        assert len(alias) < 80

    def test_alias_sanitises_special_chars(self):
        from jobagent.apply.account_manager import make_alias
        alias = make_alias("workday", "some.company.with.dots")
        # Dots in the plus portion should be replaced with dashes
        local = alias.split("@")[0].split("+")[1]
        assert "." not in local

    def test_alias_truncated(self):
        from jobagent.apply.account_manager import make_alias
        # Very long tenant name should be truncated
        alias = make_alias("workday", "a" * 100)
        local = alias.split("@")[0].split("+")[1]
        assert len(local) <= 25  # _ALIAS_SLUG_MAX + ats prefix

    def test_account_load_missing(self, tmp_db):
        from jobagent.apply.account_manager import AccountManager
        mgr = AccountManager()
        result = mgr._load_account("workday", "nonexistent")
        assert result is None

    def test_account_save_and_load(self, tmp_db):
        from jobagent.apply.account_manager import AccountManager
        from jobagent.db.models import Account
        mgr = AccountManager()
        acc = Account(
            ats="workday",
            tenant="test-co",
            login_url="https://test-co.wd3.myworkdayjobs.com/login",
            email_alias="bipin2295+workday-test-co@gmail.com",
            keychain_ref="jobagent/workday/test-co",
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        mgr._save_account(acc)
        loaded = mgr._load_account("workday", "test-co")
        assert loaded is not None
        assert loaded.status == "active"
        assert loaded.email_alias == "bipin2295+workday-test-co@gmail.com"


# ---------------------------------------------------------------------------
# 5. WorkdayCollector — seed list, instantiation, maintenance handling
# ---------------------------------------------------------------------------

def _wd_board(host: str, board_path: str, name: str | None = None):
    from jobagent.discovery.boards import BoardRef
    return BoardRef(id=1, ats_type="workday", slug=host.split(".")[0],
                    board_path=board_path, host=host, company_name=name)


class TestWorkdayCollector:
    def test_seed_list_non_empty(self):
        from jobagent.discovery.collectors.workday import WORKDAY_SEEDS
        assert len(WORKDAY_SEEDS) >= 10
        # Each entry is (name, host, board, region)
        for entry in WORKDAY_SEEDS:
            assert len(entry) == 4
            name, host, board, region = entry
            assert "myworkdayjobs.com" in host
            assert board  # board path must be non-empty

    def test_slug_from_host(self):
        from jobagent.discovery.collectors.workday import _slug_from_host
        assert _slug_from_host("hsbc.wd3.myworkdayjobs.com") == "hsbc"
        assert _slug_from_host("deutschebank.wd3.myworkdayjobs.com") == "deutschebank"

    def test_collector_registered(self):
        """WorkdayCollector must appear in the discovery runner registry."""
        from jobagent.discovery.runner import _COLLECTOR_REGISTRY
        names = [cls for _, cls in _COLLECTOR_REGISTRY]
        assert "WorkdayCollector" in names

    @pytest.mark.asyncio
    async def test_handles_maintenance_response(self):
        """503 / maintenance response must return empty list, not raise."""
        import asyncio, httpx
        from jobagent.discovery.collectors.workday import WorkdayCollector

        with patch.object(httpx.AsyncClient, "post",
                          new=AsyncMock(return_value=httpx.Response(503, text="Workday is unavailable"))), \
             patch.object(httpx.AsyncClient, "get",
                          new=AsyncMock(return_value=httpx.Response(200, text=""))):
            async with httpx.AsyncClient() as client:
                sem = asyncio.Semaphore(1)
                jobs, outcome = await WorkdayCollector()._fetch_board(
                    client, sem,
                    _wd_board("hsbc.wd3.myworkdayjobs.com", "HSBCGlobalCareers", "HSBC"),
                    ["data engineer"],
                )
        assert jobs == []
        assert outcome.outcome == "error"  # transient: board must not be marked dead

    @pytest.mark.asyncio
    async def test_handles_404_response(self):
        """404 means wrong board path — must return empty, not raise."""
        import asyncio, httpx
        from jobagent.discovery.collectors.workday import WorkdayCollector

        with patch.object(httpx.AsyncClient, "post",
                          new=AsyncMock(return_value=httpx.Response(404, text="Not Found"))), \
             patch.object(httpx.AsyncClient, "get",
                          new=AsyncMock(return_value=httpx.Response(200, text=""))):
            async with httpx.AsyncClient() as client:
                sem = asyncio.Semaphore(1)
                jobs, outcome = await WorkdayCollector()._fetch_board(
                    client, sem,
                    _wd_board("test.wd3.myworkdayjobs.com", "TestBoard"),
                    ["data engineer"],
                )
        assert jobs == []
        assert outcome.outcome == "not_found"

    @pytest.mark.asyncio
    async def test_parses_valid_response(self):
        """Valid Workday API response must be parsed into RawJob objects."""
        import asyncio, httpx
        from jobagent.discovery.collectors.workday import WorkdayCollector

        MOCK_RESPONSE = {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Senior Data Engineer",
                    "externalPath": "/job/Berlin/Senior-Data-Engineer_12345",
                    "postedLocations": [{"location": {"descriptor": "Berlin, Germany"}}],
                    "postedOn": "2024-01-15T00:00:00Z",
                },
                {
                    "title": "Lead Data Engineer",
                    "externalPath": "/job/Amsterdam/Lead-Data-Engineer_67890",
                    "postedLocations": [{"location": {"descriptor": "Amsterdam, Netherlands"}}],
                    "postedOn": "2024-01-10T00:00:00Z",
                },
            ],
        }

        with patch.object(httpx.AsyncClient, "post",
                          new=AsyncMock(return_value=httpx.Response(200, json=MOCK_RESPONSE))), \
             patch.object(httpx.AsyncClient, "get",
                          new=AsyncMock(return_value=httpx.Response(200, text=""))):
            async with httpx.AsyncClient() as client:
                sem = asyncio.Semaphore(1)
                jobs, outcome = await WorkdayCollector()._fetch_board(
                    client, sem,
                    _wd_board("deutschebank.wd3.myworkdayjobs.com", "DB_External_Careers",
                              "Deutsche Bank"),
                    ["data engineer"],
                )

        assert outcome.outcome == "ok" and outcome.job_count == 2
        assert len(jobs) == 2
        assert jobs[0].company == "Deutsche Bank"
        assert jobs[0].title == "Senior Data Engineer"
        assert jobs[1].title == "Lead Data Engineer"
        assert all(j.source == "workday" for j in jobs)
        assert "Berlin" in (jobs[0].location_raw or "")


# ---------------------------------------------------------------------------
# 6. WorkdayHandler — can_handle, shadow mode
# ---------------------------------------------------------------------------

class TestWorkdayHandler:
    def _make_job(self, **kwargs):
        from jobagent.db.models import Job
        defaults = dict(
            id=999,
            source="workday",
            source_job_id="wd-001",
            url="https://deutschebank.wd3.myworkdayjobs.com/en-US/DB_External_Careers/job/xyz",
            apply_url="https://deutschebank.wd3.myworkdayjobs.com/en-US/DB_External_Careers/job/xyz",
            ats_type="workday",
            title="Senior Data Engineer",
            title_norm="senior data engineer",
            country="DE",
            content_hash="abc123",
        )
        defaults.update(kwargs)
        return Job(**defaults)

    def test_can_handle_workday(self):
        from jobagent.apply.handlers.workday import WorkdayHandler
        h = WorkdayHandler()
        job = self._make_job()
        assert h.can_handle(job)

    def test_cannot_handle_greenhouse(self):
        from jobagent.apply.handlers.workday import WorkdayHandler
        h = WorkdayHandler()
        job = self._make_job(ats_type="greenhouse")
        assert not h.can_handle(job)

    def test_page_type_detection_imports(self):
        """Page type detector must be importable and return valid types."""
        from jobagent.apply.handlers.workday import _PAGE_INDICATORS
        valid_types = {
            "my_information", "my_experience", "application_questions",
            "voluntary_disclosures", "review", "confirmation", "login",
            "create_account",
        }
        assert set(_PAGE_INDICATORS.keys()) == valid_types
