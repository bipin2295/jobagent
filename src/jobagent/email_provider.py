"""EmailProvider — read application-related emails from Gmail.

Only ever issues **narrow server-side searches** so private emails are never
fetched or sent to an LLM.  Search scope is always restricted to:
  - A specific alias (``deliveredto:bipin2295+{tenant}@gmail.com``)
  - OR a sender domain + time window (``from:{domain} after:{unix_ts}``)

This keeps the read-only OAuth grant meaningful: it cannot be abused to scan
the whole inbox.

Usage::

    ep = EmailProvider()
    if not ep.connected:
        print("Run: jobagent gmail-connect")
        # fall back to Auth Assist / manual
    else:
        result = await ep.wait_for_verification_email(
            alias="bipin2295+acme-workday@gmail.com",
            since=datetime.now(timezone.utc),
            timeout_seconds=600,
        )
        if result.link:
            # navigate to result.link
        elif result.otp:
            # type result.otp
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Gmail API is optional — the class degrades gracefully when not installed or
# when no token is present.
try:
    from google.oauth2.credentials import Credentials  # type: ignore[import]
    from googleapiclient.discovery import build  # type: ignore[import]
    _HAS_GOOGLE_LIBS = True
except ImportError:
    _HAS_GOOGLE_LIBS = False

_TOKEN_PATH = Path("data/secrets/gmail_token.json")
_POLL_INTERVAL = 15  # seconds between Gmail polls


@dataclass
class EmailResult:
    """Parsed result from a verification email."""
    found: bool = False
    link: str | None = None      # verification / activation URL
    otp: str | None = None       # numeric OTP code
    subject: str = ""
    snippet: str = ""
    raw_body: str = ""


class EmailProvider:
    """Gmail-backed provider for verification emails.

    When the Gmail token is not available or the google-auth libraries are not
    installed, every ``wait_for_*`` method returns immediately with
    ``EmailResult(found=False)`` so the caller can fall back to Auth Assist.
    """

    def __init__(self) -> None:
        self._service: Any = None
        if not _HAS_GOOGLE_LIBS:
            log.debug("email_provider.no_google_libs")
            return
        if not _TOKEN_PATH.exists():
            log.debug("email_provider.no_token", path=str(_TOKEN_PATH))
            return
        try:
            creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH))
            if creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request  # type: ignore[import]
                creds.refresh(Request())
                _TOKEN_PATH.write_text(creds.to_json())
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            log.debug("email_provider.connected")
        except Exception as exc:
            log.warning("email_provider.init_error", error=str(exc)[:80])

    @property
    def connected(self) -> bool:
        return self._service is not None

    # ── Main public API ───────────────────────────────────────────────────────

    async def wait_for_verification_email(
        self,
        *,
        alias: str | None = None,
        sender_domain: str | None = None,
        since: datetime | None = None,
        timeout_seconds: int = 600,
        subject_hint: str = "",
    ) -> EmailResult:
        """Poll Gmail until a verification email arrives or timeout expires.

        Parameters
        ----------
        alias:
            Full ``bipin2295+tenant@gmail.com`` alias. Used in a
            ``deliveredto:`` query so we don't scan unrelated mail.
        sender_domain:
            E.g. ``"workday.com"`` or ``"acme.com"``. Combined with
            ``after:`` to narrow the search when no alias is available.
        since:
            Reject emails older than this timestamp.
        timeout_seconds:
            Total wait time before giving up.
        subject_hint:
            Optional substring to require in the subject line.
        """
        if not self.connected:
            return EmailResult(found=False)

        since = since or datetime.now(timezone.utc)
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        query = _build_query(alias=alias, sender_domain=sender_domain, since=since,
                             subject_hint=subject_hint)
        log.debug("email_provider.waiting", query=query, timeout=timeout_seconds)

        while asyncio.get_event_loop().time() < deadline:
            result = await asyncio.to_thread(self._search_once, query, since)
            if result.found:
                return result
            await asyncio.sleep(_POLL_INTERVAL)

        log.warning("email_provider.timeout", query=query, timeout=timeout_seconds)
        return EmailResult(found=False)

    async def search_recent(
        self,
        *,
        alias: str | None = None,
        sender_domain: str | None = None,
        since: datetime | None = None,
        subject_hint: str = "",
        max_results: int = 10,
    ) -> list[EmailResult]:
        """Return all matching emails (non-blocking, single poll).

        Used by the Gmail watcher to classify responses.
        """
        if not self.connected:
            return []
        since = since or datetime.now(timezone.utc)
        query = _build_query(alias=alias, sender_domain=sender_domain, since=since,
                             subject_hint=subject_hint)
        return await asyncio.to_thread(self._list_emails, query, since, max_results)

    # ── Gmail API helpers (run in thread) ─────────────────────────────────────

    def _search_once(self, query: str, since: datetime) -> EmailResult:
        """Single Gmail search; returns the first matching message or not-found."""
        results = self._list_emails(query, since, max_results=1)
        return results[0] if results else EmailResult(found=False)

    def _list_emails(self, query: str, since: datetime, max_results: int) -> list[EmailResult]:
        try:
            resp = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            messages = resp.get("messages", [])
            results = []
            for msg in messages:
                result = self._fetch_message(msg["id"], since)
                if result.found:
                    results.append(result)
            return results
        except Exception as exc:
            log.warning("email_provider.search_error", error=str(exc)[:80])
            return []

    def _fetch_message(self, msg_id: str, since: datetime) -> EmailResult:
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except Exception:
            return EmailResult(found=False)

        # Check received timestamp
        internal_date_ms = int(msg.get("internalDate", 0))
        received_at = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)
        if received_at < since:
            return EmailResult(found=False)

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        snippet = msg.get("snippet", "")

        body = _extract_body(msg.get("payload", {}))
        link = _extract_verification_link(body)
        otp = _extract_otp(body, snippet) if not link else None

        return EmailResult(
            found=True,
            link=link,
            otp=otp,
            subject=subject,
            snippet=snippet,
            raw_body=body[:2000],
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_query(
    *,
    alias: str | None,
    sender_domain: str | None,
    since: datetime | None,
    subject_hint: str,
) -> str:
    parts: list[str] = []
    if alias:
        parts.append(f"deliveredto:{alias}")
    if sender_domain:
        parts.append(f"from:{sender_domain}")
    if since:
        # Gmail's after: filter takes a date (YYYY/MM/DD), not an epoch. The
        # exact timestamp is enforced later in _fetch_message.
        parts.append(f"after:{since.strftime('%Y/%m/%d')}")
    if subject_hint:
        parts.append(f"subject:{subject_hint}")
    return " ".join(parts) if parts else "in:inbox"


def _extract_body(payload: dict[str, Any]) -> str:
    """Recursively extract plain-text or HTML body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if "text" in mime:
        data = payload.get("body", {}).get("data", "")
        if data:
            import base64  # noqa: PLC0415
            try:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:
                pass
    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result
    return ""


_VERIFY_PATTERN = re.compile(
    r"https?://[^\s\"'<>]*(?:verify|activate|confirm|validate|click-here|magic-link)[^\s\"'<>]*",
    re.IGNORECASE,
)
_OTP_PATTERN = re.compile(
    r"(?:code|otp|pin|token)[^0-9]*(\b\d{4,8}\b)",
    re.IGNORECASE,
)
_BARE_OTP_PATTERN = re.compile(r"\b(\d{6})\b")


def _extract_verification_link(body: str) -> str | None:
    m = _VERIFY_PATTERN.search(body)
    return m.group(0) if m else None


def _extract_otp(body: str, snippet: str) -> str | None:
    for text in [body, snippet]:
        m = _OTP_PATTERN.search(text)
        if m:
            return m.group(1)
    # Last resort: any 6-digit standalone number
    m = _BARE_OTP_PATTERN.search(snippet)
    return m.group(1) if m else None
