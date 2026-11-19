"""Account manager for ATS platforms that require sign-in.

Handles the full lifecycle:
  1. Check if we already have a working account (``Account.status == "active"``).
  2. If not, create one (sign-up flow).
  3. Verify email (via Gmail API or Auth Assist).
  4. Log in and return a live browser page for the caller.
  5. Update ``Account`` row with result.

The email alias scheme (``<candidate>+{tenant_slug}@example.com``) ensures every
account is distinguishable in the inbox while still delivering to the main
address.

Accounts are **reused** across runs — the second application to the same
company skips sign-up entirely and just logs in.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from jobagent.db.engine import get_session
from jobagent.db.models import Account
from jobagent.email_provider import EmailProvider
from jobagent.vault import CredentialVault

if TYPE_CHECKING:
    from playwright.async_api import Page

log = structlog.get_logger(__name__)

# Max length of tenant slug portion in the alias
_ALIAS_SLUG_MAX = 20


def _base_email() -> str:
    """Return the candidate's base email from the profile or answer seed.

    Never hardcoded: the real address lives only in the git-ignored profile /
    ``answers_seed.yaml``.
    """
    try:
        from jobagent.profile.loader import load_profile  # noqa: PLC0415

        email = (load_profile().contact.email or "").strip()
        if email:
            return email
    except Exception:
        pass
    try:
        from jobagent.tailor.answers import load_answers_seed  # noqa: PLC0415

        basics = load_answers_seed().get("basics") or {}
        return str(basics.get("email", "")).strip()
    except Exception:
        return ""


def make_alias(ats: str, tenant: str, base_email: str | None = None) -> str:
    """Return ``{local}+{ats}-{tenant}@{domain}`` for the candidate's email.

    Derived from the profile/seed unless *base_email* is supplied (useful for
    tests). Returns an empty string when no base email is configured. The
    ``+`` suffix is ignored by Gmail so mail still arrives in the main inbox.
    """
    base = (base_email if base_email is not None else _base_email()).strip()
    if "@" not in base:
        return ""
    local, _, domain = base.partition("@")
    slug = re.sub(r"[^a-z0-9]", "-", (f"{ats}-{tenant}").lower())
    slug = slug[:_ALIAS_SLUG_MAX].rstrip("-")
    return f"{local}+{slug}@{domain}"


def _tenant_key(ats: str, tenant: str) -> str:
    return f"{ats}/{tenant}"


def _load_password_for(ats: str, tenant: str) -> str:
    """Return the stored password for an ATS account, or "" if none exists.

    Read-only: never creates a credential. Used by handler-side form fillers
    that must log into an account created earlier by AccountManager.
    """
    try:
        pw = CredentialVault().get(ats, tenant)
        return pw or ""
    except Exception:
        return ""


class AccountManager:
    """Create, persist and reuse ATS accounts."""

    def __init__(self) -> None:
        self._vault = CredentialVault()
        self._email_provider = EmailProvider()

    # ── High-level entry point ────────────────────────────────────────────────

    async def ensure_logged_in(
        self,
        page: "Page",
        ats: str,
        tenant: str,
        login_url: str,
        *,
        create_if_missing: bool = True,
        shadow_mode: bool = False,
    ) -> bool:
        """Ensure *page* is authenticated for *tenant*.

        Returns ``True`` if the page is now logged in, ``False`` otherwise.
        """
        account = self._load_account(ats, tenant)

        if account and account.status == "active":
            # Try to log in with stored credentials
            success = await self._login(page, account)
            if success:
                return True
            # If login fails, the account may be locked or password changed
            log.warning("account_manager.login_failed_retry", ats=ats, tenant=tenant)
            # Fall through to re-create

        if not create_if_missing:
            return False

        if shadow_mode:
            log.info("account_manager.shadow_skip_create", ats=ats, tenant=tenant)
            return False

        # Create a new account
        alias = make_alias(ats, tenant)
        password = self._vault.get_or_create(ats, tenant)

        log.info("account_manager.creating", ats=ats, tenant=tenant, alias=alias)
        account = await self._signup(page, ats, tenant, login_url, alias, password)
        if account is None:
            return False

        # Verify email
        verified = await self._verify_email(page, account, alias)
        if not verified:
            account.status = "unverified"
            self._save_account(account)
            return False

        account.status = "active"
        account.last_login_at = datetime.now(timezone.utc)
        self._save_account(account)
        return True

    # ── Workday-specific sign-up ───────────────────────────────────────────────

    async def _signup(
        self,
        page: "Page",
        ats: str,
        tenant: str,
        login_url: str,
        alias: str,
        password: str,
    ) -> Account | None:
        """Fill the ATS sign-up form and return a new Account row."""
        from jobagent.apply.guardrails import human_like_pause  # noqa: PLC0415

        try:
            # Navigate to login/sign-up page
            await page.goto(login_url, wait_until="domcontentloaded", timeout=20000)
            await human_like_pause()

            # Look for "Create Account" / "Sign Up" link
            for selector in [
                "a:text-matches('create account|sign up|register|new user', 'i')",
                "button:text-matches('create account|sign up|register', 'i')",
                "[data-automation-id='createAccountLink']",
                "[data-automation-id='signUpBtn']",
            ]:
                try:
                    el = page.locator(selector).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.click()
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                        await human_like_pause()
                        break
                except Exception:
                    continue

            # Fill sign-up form fields
            await _fill_signup_form(page, alias, password)

            # Workday requires the terms-and-conditions checkbox. Without it the
            # form silently refuses to submit ("Please check the box to
            # continue"), no verification email is ever sent, and the caller
            # waits out its whole timeout for mail that will never arrive.
            await _check_consent_boxes(page)

            # Submit
            for sel in [
                "button[type=submit]:text-matches('create|sign up|register', 'i')",
                "[data-automation-id='createAccount']",
                "button:text-matches('^Create Account$', 'i')",
                "button[type=submit]",
            ]:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000, force=True)
                    break

            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await human_like_pause()

            # If the form is still showing its own validation errors, the
            # signup did not go through; report that instead of waiting for an
            # email that will never come.
            if await _signup_rejected(page):
                log.warning(
                    "account_manager.signup_rejected",
                    ats=ats,
                    tenant=tenant,
                    reason=(await _signup_error_text(page))[:160],
                )
                return None

            # Persist the account record
            account = Account(
                ats=ats,
                tenant=tenant,
                login_url=login_url,
                email_alias=alias,
                keychain_ref=self._vault.keychain_ref(ats, tenant),
                status="unverified",
                created_at=datetime.now(timezone.utc),
            )
            self._save_account(account)
            log.info("account_manager.signup_done", ats=ats, tenant=tenant, alias=alias)
            return account

        except Exception as exc:
            log.error("account_manager.signup_error", ats=ats, tenant=tenant, error=str(exc)[:100])
            return None

    async def _login(self, page: "Page", account: Account) -> bool:
        """Log in using stored credentials. Returns True on success."""
        from jobagent.apply.guardrails import human_like_pause  # noqa: PLC0415

        password = self._vault.get(account.ats, account.tenant)
        if not password:
            return False

        try:
            await page.goto(account.login_url, wait_until="domcontentloaded", timeout=20000)
            await human_like_pause()

            # Fill email
            for sel in [
                "[data-automation-id='email']",
                "input[type=email]",
                "input[name=email]",
                "input[placeholder*='email' i]",
            ]:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(account.email_alias)
                    break

            # Fill password
            for sel in [
                "[data-automation-id='password']",
                "input[type=password]",
                "input[name=password]",
            ]:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(password)
                    break

            # Submit
            for sel in [
                "[data-automation-id='signIn']",
                "button[type=submit]:text-matches('sign in|log in', 'i')",
                "button[type=submit]",
            ]:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=10000)
                    break

            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await human_like_pause()

            # Check if we're logged in (URL changed or account menu visible)
            current = page.url
            logged_in = (
                "login" not in current.lower()
                and "signin" not in current.lower()
                and "auth" not in current.lower()
            )

            if logged_in:
                account.last_login_at = datetime.now(timezone.utc)
                self._save_account(account)
                log.info("account_manager.logged_in", ats=account.ats, tenant=account.tenant)
            return logged_in

        except Exception as exc:
            log.debug("account_manager.login_error", error=str(exc)[:80])
            return False

    async def _verify_email(
        self, page: "Page", account: Account, alias: str
    ) -> bool:
        """Wait for a verification email and click the link or enter the OTP."""
        log.info("account_manager.waiting_for_verification", alias=alias)

        # Bounded: a missing verification email must not consume the whole
        # apply budget (this previously blocked for 10 minutes and made runs
        # look hung). Overridable for slow mail delivery.
        try:
            import os  # noqa: PLC0415

            wait_s = int(os.environ.get("JOBAGENT_VERIFY_EMAIL_TIMEOUT", "180"))
        except Exception:
            wait_s = 180

        result = await self._email_provider.wait_for_verification_email(
            alias=alias,
            since=datetime.now(timezone.utc),
            timeout_seconds=wait_s,
        )

        if not result.found:
            log.warning("account_manager.no_verification_email", alias=alias)
            return False

        if result.link:
            try:
                await page.goto(result.link, wait_until="domcontentloaded", timeout=20000)
                log.info("account_manager.email_verified_link", alias=alias)
                return True
            except Exception as exc:
                log.warning("account_manager.verify_link_error", error=str(exc)[:60])

        if result.otp:
            # Look for OTP input field
            for sel in ["input[name=code], input[name=otp], input[placeholder*='code' i]"]:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(result.otp)
                    btn = page.locator("button[type=submit]").first
                    if await btn.count() > 0:
                        await btn.click()
                    log.info("account_manager.email_verified_otp", alias=alias)
                    return True

        return False

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _load_account(self, ats: str, tenant: str) -> Account | None:
        from sqlmodel import select  # noqa: PLC0415
        with get_session() as s:
            return s.exec(
                select(Account)
                .where(Account.ats == ats)
                .where(Account.tenant == tenant)
                .order_by(Account.created_at.desc())
            ).first()

    def _save_account(self, account: Account) -> None:
        with get_session() as s:
            s.add(account)
            s.commit()
            s.refresh(account)


# ── Sign-up form filler ───────────────────────────────────────────────────────

async def _check_consent_boxes(page: "Page") -> None:
    """Tick required consent / terms checkboxes on a sign-up form.

    Workday blocks submission with "Please check the box to continue" until the
    terms checkbox is ticked, and shows no indication until you click Create
    Account. Only checkbox inputs that are required OR whose label reads like
    consent are touched — marketing opt-ins are left alone.
    """
    import re  # noqa: PLC0415

    consent_re = re.compile(
        r"terms|conditions|consent|privacy|agree|acknowledge|read and|policy",
        re.IGNORECASE,
    )
    boxes = page.locator("input[type=checkbox]")
    for i in range(await boxes.count()):
        box = boxes.nth(i)
        try:
            if await box.is_checked() or not await box.is_visible():
                continue
            required = await box.evaluate("e => !!e.required")
            label = ""
            try:
                label = await box.evaluate(
                    "e => (e.labels && e.labels[0] ? e.labels[0].innerText : "
                    "(e.closest('label')?.innerText || e.parentElement?.innerText || ''))"
                )
            except Exception:
                label = ""
            if required or consent_re.search(label or ""):
                await box.check(timeout=5000)
                log.debug("account_manager.consent_checked", label=(label or "")[:60])
        except Exception:
            continue


async def _signup_error_text(page: "Page") -> str:
    """Return visible sign-up validation text, if any."""
    for sel in ("[role='alert']", ".error", "[class*='error']", "[class*='Error']"):
        loc = page.locator(sel)
        for i in range(min(await loc.count(), 4)):
            try:
                text = (await loc.nth(i).inner_text()).strip()
                if text:
                    return text
            except Exception:
                continue
    return ""


async def _signup_rejected(page: "Page") -> bool:
    """True when the sign-up form is still displayed with a validation error.

    A successful submit navigates away from the create-account form, so a
    still-present form plus an error message means nothing was created.
    """
    try:
        text = await _signup_error_text(page)
        if not text:
            return False
        still_form = await page.locator("input[type=password]").count() > 0
        return still_form
    except Exception:
        return False


async def _fill_signup_form(page: "Page", email: str, password: str) -> None:
    """Fill common sign-up form fields (email, password, confirm password)."""
    async def _try_fill(selectors: list[str], value: str) -> None:
        for sel in selectors:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.fill(value)
                return

    await _try_fill([
        "[data-automation-id='email']",
        "input[type=email]",
        "input[name=email]",
        "input[placeholder*='email' i]",
    ], email)

    await _try_fill([
        "[data-automation-id='password']",
        "input[type=password][name*='pass']:not([name*='confirm']):not([name*='current'])",
        "input[type=password]",
    ], password)

    await _try_fill([
        "[data-automation-id='verifyPassword']",
        "input[type=password][name*='confirm']",
        "input[type=password][placeholder*='confirm' i]",
        "input[type=password][id*='confirm' i]",
    ], password)

    # Some sign-up forms ask for first / last name
    from jobagent.profile.loader import load_profile  # noqa: PLC0415
    try:
        profile = load_profile()
        first = profile.contact.name.split()[0]
        last = " ".join(profile.contact.name.split()[1:]) or first

        await _try_fill([
            "input[name='firstName'], input[id*='first' i], input[placeholder*='first' i]"
        ], first)
        await _try_fill([
            "input[name='lastName'], input[id*='last' i], input[placeholder*='last' i]"
        ], last)
    except Exception:
        pass
