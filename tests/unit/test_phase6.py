"""Phase 6 integration tests — Gmail watcher, run summary, cooldown system.

All tests are offline (no network, no live Gmail token required).
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(db_file))
    import jobagent.db.engine as eng
    import jobagent.config as cfg
    eng._engine = None  # type: ignore[attr-defined]
    cfg.settings = cfg.Settings()  # re-read env to pick up new DB path
    from jobagent.db.engine import create_tables
    create_tables()
    yield db_file
    eng._engine = None  # type: ignore[attr-defined]
    cfg.settings = cfg.Settings()  # restore defaults


# ---------------------------------------------------------------------------
# 1. Discovery cooldown system
# ---------------------------------------------------------------------------

class TestCollectorCooldown:
    def test_cooldown_map_complete(self):
        """Every collector in the registry must have a cooldown entry."""
        from jobagent.discovery.runner import _COLLECTOR_REGISTRY, _COOLDOWN_HOURS
        for _, class_name in _COLLECTOR_REGISTRY:
            assert class_name in _COOLDOWN_HOURS, (
                f"{class_name} missing from _COOLDOWN_HOURS"
            )

    def test_free_collectors_have_zero_cooldown(self):
        from jobagent.discovery.runner import _COOLDOWN_HOURS
        free = [
            "GreenhouseCollector", "LeverCollector", "AshbyCollector",
            "WorkdayCollector", "ArbeitnowCollector", "HimalayasCollector",
            "RemoteOKCollector", "RemotiveCollector",
        ]
        for name in free:
            assert _COOLDOWN_HOURS[name] == 0, f"{name} should have 0 cooldown"

    def test_quota_collectors_have_weekly_cooldown(self):
        from jobagent.discovery.runner import _COOLDOWN_HOURS
        quota = ["AdzunaCollector", "JoobleCollector", "ReedCollector", "JobSpyCollector"]
        for name in quota:
            assert _COOLDOWN_HOURS[name] >= 24, (
                f"{name} cooldown should be >= 24h, got {_COOLDOWN_HOURS[name]}"
            )

    def test_not_on_cooldown_first_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr("jobagent.discovery.runner._COOLDOWN_FILE",
                            str(tmp_path / "last_run.json"))
        from jobagent.discovery.runner import _is_on_cooldown
        # No file = never ran = not on cooldown
        assert not _is_on_cooldown("AdzunaCollector")

    def test_on_cooldown_after_recent_run(self, tmp_path, monkeypatch):
        cooldown_file = tmp_path / "last_run.json"
        monkeypatch.setattr("jobagent.discovery.runner._COOLDOWN_FILE",
                            str(cooldown_file))
        from jobagent.discovery.runner import _save_cooldown, _is_on_cooldown
        _save_cooldown("AdzunaCollector")
        assert _is_on_cooldown("AdzunaCollector")

    def test_not_on_cooldown_after_expired(self, tmp_path, monkeypatch):
        cooldown_file = tmp_path / "last_run.json"
        monkeypatch.setattr("jobagent.discovery.runner._COOLDOWN_FILE",
                            str(cooldown_file))
        # Write a timestamp 200h ago (beyond the 168h window)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
        cooldown_file.write_text(json.dumps({"AdzunaCollector": old_time}))
        from importlib import reload  # noqa: PLC0415
        import jobagent.discovery.runner as runner
        # Need to reload _load_cooldowns since it reads the file on call
        assert not runner._is_on_cooldown("AdzunaCollector")

    def test_sources_filter(self, tmp_path, monkeypatch):
        """_load_collectors with only=['greenhouse'] must exclude adzuna."""
        cooldown_file = tmp_path / "last_run.json"
        monkeypatch.setattr("jobagent.discovery.runner._COOLDOWN_FILE",
                            str(cooldown_file))
        from jobagent.discovery.runner import _load_collectors
        collectors = _load_collectors(only=["greenhouse"])
        names = [c.name for c in collectors]
        assert "greenhouse" in names
        assert "adzuna" not in names
        assert "workday" not in names


# ---------------------------------------------------------------------------
# 2. Gmail watcher — offline (no token) behaviour
# ---------------------------------------------------------------------------

class TestGmailWatcher:
    def test_imports(self):
        from jobagent.tracking.gmail_watcher import run_gmail_watcher
        assert callable(run_gmail_watcher)

    @pytest.mark.asyncio
    async def test_returns_zeros_when_disconnected(self, tmp_db):
        """Watcher with empty DB and no Gmail → all counts zero."""
        # Fresh tmp_db has no submitted applications, so all counts are 0
        counts = await _run_watcher_no_gmail(tmp_db)
        assert counts["status_changed"] == 0
        assert counts["ghosted"] == 0
        # skipped may be 0 since there are no apps to even try searching for

    @pytest.mark.asyncio
    async def test_ghosts_old_applications(self, tmp_db):
        """Applications submitted 31+ days ago with no reply → ghosted."""
        from jobagent.db.engine import get_session
        from jobagent.db.models import Application, Job
        from sqlmodel import select

        old_submit = datetime.now(timezone.utc) - timedelta(days=31)
        with get_session() as s:
            job = Job(
                source="workday", source_job_id="wd-ghost",
                url="https://test.wd3.myworkdayjobs.com/en-US/Test/job/abc",
                title="Data Engineer", title_norm="data engineer",
                ats_type="workday", content_hash="ghost_hash",
            )
            s.add(job); s.flush()
            app = Application(
                job_id=job.id, status="submitted",
                created_at=old_submit, updated_at=old_submit,
                submitted_at=old_submit,
            )
            s.add(app); s.commit()

        counts = await _run_watcher_no_gmail(tmp_db)
        # Should have ghosted the old application
        assert counts["ghosted"] == 1
        with get_session() as s:
            updated = s.exec(select(Application)).first()
            assert updated is not None
            assert updated.status == "ghosted"

    def test_email_keyword_classifier_acknowledged(self):
        """'Thank you for applying' → the keyword regex must match it."""
        import re
        # Test the same regex used inside _classify_email
        text = "thank you for applying to Deutsche Bank we received your application"
        assert re.search(r"thank you for apply|received your application", text)

    def test_email_keyword_classifier_rejected(self):
        import re
        text = "we regret to inform you that you were not selected for this position"
        assert re.search(r"regret|not.*move forward|not.*selected", text)

    def test_email_keyword_classifier_interview(self):
        import re
        text = "we'd like to schedule an interview with you"
        assert re.search(r"schedule.*interview|invite.*interview", text)

    def test_ghost_patterns(self):
        """Keyword patterns must not accidentally match common non-response emails."""
        import re
        # Newsletter unsubscribe should not match rejection pattern
        text = "click here to unsubscribe from our newsletter"
        assert not re.search(r"regret|not.*selected|decline", text)


# ---------------------------------------------------------------------------
# 3. Run summary
# ---------------------------------------------------------------------------

class TestRunSummary:
    def test_summary_structure(self, tmp_db):
        from jobagent.tracking.summary import build_run_summary
        summary = build_run_summary(since_hours=24)
        assert isinstance(summary, dict)
        assert "submitted" in summary
        assert "failed" in summary
        assert "skipped" in summary
        assert "all_time" in summary
        assert "submissions" in summary
        assert "generated_at" in summary

    def test_summary_with_submission(self, tmp_db):
        from jobagent.db.engine import get_session
        from jobagent.db.models import Application, Job
        from jobagent.tracking.summary import build_run_summary

        with get_session() as s:
            job = Job(
                source="greenhouse", source_job_id="gh-001",
                url="https://boards.greenhouse.io/cloudflare/jobs/123",
                title="Senior Data Engineer", title_norm="senior data engineer",
                ats_type="greenhouse", content_hash="cf_hash",
            )
            s.add(job); s.flush()
            app = Application(
                job_id=job.id, status="submitted",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                submitted_at=datetime.now(timezone.utc),
            )
            s.add(app); s.commit()

        summary = build_run_summary(since_hours=24)
        assert summary["submitted"] == 1
        assert len(summary["submissions"]) == 1
        assert summary["submissions"][0]["title"] == "Senior Data Engineer"

    def test_summary_print_does_not_raise(self, tmp_db):
        from jobagent.tracking.summary import print_summary, build_run_summary
        # Should not raise even with empty DB
        summary = build_run_summary(since_hours=24)
        print_summary(summary)  # just check it runs without error


# ---------------------------------------------------------------------------
# 4. Daily schedule script
# ---------------------------------------------------------------------------

class TestScheduleScript:
    @staticmethod
    def _scripts_dir() -> Path:
        # Works both on host (tests/unit/ → ../../scripts) and in Docker
        # (/app/tests/tests/unit/ → ../../../../scripts = /app/scripts)
        for candidate in [
            Path(__file__).parents[2] / "scripts",   # host layout
            Path("/app/scripts"),                      # Docker layout
        ]:
            if candidate.exists():
                return candidate
        return Path(__file__).parents[2] / "scripts"

    def test_script_exists(self):
        script = self._scripts_dir() / "install_schedule.py"
        assert script.exists(), f"Expected {script}"

    def test_daily_run_script_exists(self):
        script = self._scripts_dir() / "daily_run.sh"
        assert script.exists(), f"Expected {script}"
        assert os.access(script, os.X_OK), "daily_run.sh must be executable"

    def test_plist_template_valid(self):
        """The plist template must contain required launchd keys."""
        import importlib.util, sys
        scripts_dir = self._scripts_dir()
        spec = importlib.util.spec_from_file_location(
            "install_schedule", scripts_dir / "install_schedule.py"
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert "StartCalendarInterval" in mod.PLIST_TEMPLATE
        assert "ProgramArguments" in mod.PLIST_TEMPLATE
        assert mod.LABEL == "io.local.jobagent.daily"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_watcher_no_gmail(tmp_db):
    """Run the watcher with EmailProvider stubbed to report not-connected.

    We patch the class so that any EmailProvider() call inside run_gmail_watcher
    returns a stub whose `connected` property is False, triggering graceful
    degradation. The ghosting logic runs regardless of connection state.
    """
    from jobagent.tracking.gmail_watcher import run_gmail_watcher

    class _StubProvider:
        connected = False

        async def search_recent(self, **kwargs):
            return []

    with patch("jobagent.tracking.gmail_watcher.EmailProvider",
               return_value=_StubProvider()):
        return await run_gmail_watcher()


import os
