"""Credential vault — per-tenant password storage via macOS Keychain.

Uses the ``keyring`` library which on macOS backends to the system Keychain.
On Linux (Docker) it falls back to a plain-text file at
``data/vault/.keyring`` (gitignored) since there is no system keychain.

Usage::

    vault = CredentialVault()
    pw = vault.get_or_create("workday", "acme")   # returns existing or new
    vault.set("workday", "acme", "my_password")
    vault.delete("workday", "acme")
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_SERVICE_PREFIX = "jobagent"
_PW_LENGTH = 24
_PW_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*"

# Fallback flat-file store used in Docker (no system keychain)
_FALLBACK_PATH = Path("data/vault/.keyring.json")


def _generate_password() -> str:
    """Generate a 24-char password meeting all common complexity rules."""
    while True:
        pw = "".join(secrets.choice(_PW_ALPHABET) for _ in range(_PW_LENGTH))
        # Must have at least one of each class
        if (any(c.isupper() for c in pw)
                and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*" for c in pw)):
            return pw


def _service(ats: str) -> str:
    return f"{_SERVICE_PREFIX}/{ats}"


class CredentialVault:
    """Thread-safe (by design of keyring) per-tenant credential store."""

    def __init__(self) -> None:
        self._backend: str = "system"
        try:
            import keyring  # type: ignore[import]
            # Quick smoke test — if this raises we fall back to file
            keyring.get_keyring()
            self._keyring = keyring
        except Exception:
            self._keyring = None  # type: ignore[assignment]
            self._backend = "file"
            _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, ats: str, tenant: str) -> str | None:
        """Return stored password or ``None`` if not set."""
        if self._backend == "system":
            try:
                return self._keyring.get_password(_service(ats), tenant)
            except Exception:
                pass
        # file fallback
        return self._read_file().get(f"{ats}/{tenant}")

    def set(self, ats: str, tenant: str, password: str) -> None:
        """Store password."""
        if self._backend == "system":
            try:
                self._keyring.set_password(_service(ats), tenant, password)
                return
            except Exception:
                self._backend = "file"
        store = self._read_file()
        store[f"{ats}/{tenant}"] = password
        self._write_file(store)

    def delete(self, ats: str, tenant: str) -> None:
        """Remove a stored credential."""
        if self._backend == "system":
            try:
                self._keyring.delete_password(_service(ats), tenant)
                return
            except Exception:
                pass
        store = self._read_file()
        store.pop(f"{ats}/{tenant}", None)
        self._write_file(store)

    def get_or_create(self, ats: str, tenant: str) -> str:
        """Return existing password, or generate + store a new one."""
        existing = self.get(ats, tenant)
        if existing:
            return existing
        pw = _generate_password()
        self.set(ats, tenant, pw)
        log.debug("vault.created", ats=ats, tenant=tenant)
        return pw

    def keychain_ref(self, ats: str, tenant: str) -> str:
        """Return a stable reference string stored in the Account.keychain_ref column."""
        return f"{_service(ats)}/{tenant}"

    # ── File-based fallback ───────────────────────────────────────────────────

    def _read_file(self) -> dict[str, str]:
        try:
            if _FALLBACK_PATH.exists():
                return json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _write_file(self, store: dict[str, str]) -> None:
        try:
            _FALLBACK_PATH.write_text(
                json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.chmod(_FALLBACK_PATH, 0o600)  # owner-read-write only
        except Exception as exc:
            log.warning("vault.write_error", error=str(exc))
