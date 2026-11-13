"""Tests for the config module."""

from __future__ import annotations

from pathlib import Path

from jobagent.config import Settings, load_yaml


def test_settings_defaults():
    s = Settings()
    assert s.portkey_base_url == "https://gateway.ai.cimpress.io/v1"
    assert s.ollama_embed_model == "nomic-embed-text"
    # llm_ready depends on whether JOBAGENT_PORTKEY_API_KEY is in .env;
    # just assert it's a bool, not the specific value
    assert isinstance(s.llm_ready, bool)


def test_settings_db_url():
    s = Settings()
    assert "sqlite" in s.db_url


def test_load_yaml_missing_returns_empty():
    result = load_yaml("definitely_does_not_exist_xyz")
    assert result == {}


def test_load_yaml_guardrails():
    cfg = load_yaml("guardrails")
    if cfg:   # file may not exist in CI; skip if so
        assert "daily_cap" in cfg
        assert cfg["daily_cap"] > 0


def test_load_yaml_models():
    cfg = load_yaml("models")
    if cfg:
        assert "tasks" in cfg
        assert "scoring" in cfg["tasks"]
