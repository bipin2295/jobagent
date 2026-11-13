"""Central configuration – loaded once at import time."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBAGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Paths ───────────────────────────────────────────────────────────
    data_dir: Path = Field(default=Path("data"))
    db_path: Path = Field(default=Path("data/jobagent.db"))
    profile_source: Path = Field(
        default=Path.home() / "Documents/resume/master_resume.txt"
    )
    profiles_dir: Path = Field(default=Path("data/profile"))
    secrets_dir: Path = Field(default=Path("data/secrets"))

    # ── LLM (Portkey) ───────────────────────────────────────────────────
    portkey_api_key: str = ""
    portkey_base_url: str = "https://gateway.ai.cimpress.io/v1"

    # ── Ollama ──────────────────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    ollama_embed_model: str = "nomic-embed-text"

    # ── Telegram ────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Gmail ───────────────────────────────────────────────────────────
    gmail_client_secret_path: Path = Field(
        default=Path("data/secrets/client_secret.json")
    )
    gmail_token_path: Path = Field(default=Path("data/secrets/gmail_token.json"))

    # ── Job-board keys ──────────────────────────────────────────────────
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    jooble_api_key: str = ""
    reed_api_key: str = ""

    # ── Scraping fallbacks ──────────────────────────────────────────────
    apify_api_token: str = ""
    firecrawl_api_key: str = ""

    @field_validator("data_dir", "profiles_dir", "secrets_dir", mode="after")
    @classmethod
    def _ensure_dir(cls, v: Path) -> Path:
        v.mkdir(parents=True, exist_ok=True)
        return v

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def llm_ready(self) -> bool:
        return bool(self.portkey_api_key)

    @property
    def gmail_ready(self) -> bool:
        return self.gmail_token_path.exists()


@lru_cache(maxsize=16)
def load_yaml(name: str) -> dict:  # type: ignore[type-arg]
    """Load config/{name}.yaml, cached after first read."""
    path = Path("config") / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def get_model_config(task: str) -> dict:  # type: ignore[type-arg]
    """Return the model config dict for *task* from config/models.yaml."""
    cfg = load_yaml("models")
    tasks = cfg.get("tasks", {})
    if task not in tasks:
        # Fallback to a cheap model so nothing hard-crashes without config
        return {"model": "gpt-4o-mini", "max_tokens": 1000, "temperature": 0.1}
    return tasks[task]


def get_guardrails() -> dict:  # type: ignore[type-arg]
    return load_yaml("guardrails")


# Module-level singleton – import this everywhere
settings = Settings()
