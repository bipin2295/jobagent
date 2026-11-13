"""Playwright browser context management.

One persistent context per ATS family per process, backed by a browser profile
stored in data/browser_profiles/{ats_family}/.  Persistent contexts preserve
cookies and localStorage, so ATS login state survives across runs.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright

from jobagent.config import settings

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

# ats_family → BrowserContext
_contexts: dict[str, BrowserContext] = {}
_playwright_instance = None
_playwright_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Chromium launch arguments suitable for Docker + noVNC
# ---------------------------------------------------------------------------

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-popup-blocking",
    "--start-maximized",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_playwright():
    """Return (or lazily start) the shared Playwright instance."""
    global _playwright_instance
    async with _playwright_lock:
        if _playwright_instance is None:
            _playwright_instance = await async_playwright().start()
            log.debug("playwright.started")
    return _playwright_instance


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_context(ats_family: str = "default") -> BrowserContext:
    """Return (or create) a persistent browser context for *ats_family*.

    The context is cached for the lifetime of the process; subsequent calls
    with the same *ats_family* return the same object without re-launching.

    Parameters
    ----------
    ats_family:
        Logical grouping of ATS platforms that share a login profile.
        E.g. ``"greenhouse"`` or ``"workday"``.  Use ``"default"`` when a
        single shared profile is sufficient.
    """
    global _contexts

    if ats_family not in _contexts:
        profile_dir: Path = settings.data_dir / "browser_profiles" / ats_family
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Remove stale lock files left by crashed previous sessions
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie", ".org.chromium.Chromium.*"):
            for lock_file in profile_dir.glob(lock_name):
                try:
                    lock_file.unlink()
                    log.debug("browser.removed_stale_lock", file=str(lock_file))
                except Exception:
                    pass

        pw = await _get_playwright()

        context = await pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,          # headed so noVNC can display the session
            args=_LAUNCH_ARGS,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="Asia/Kolkata",
            accept_downloads=True,
            # Spoof a realistic user-agent to avoid trivial bot detection
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        # Apply stealth at context level so ALL pages in this context get it
        try:
            from playwright_stealth import Stealth  # noqa: PLC0415
            await Stealth().apply_stealth_async(context)
            log.debug("browser.stealth_applied_to_context", ats_family=ats_family)
        except Exception:
            pass

        _contexts[ats_family] = context
        log.info(
            "browser.context_created",
            ats_family=ats_family,
            profile=str(profile_dir),
        )

    return _contexts[ats_family]


@asynccontextmanager
async def new_page(
    ats_family: str = "default",
    timeout_ms: int = 30_000,
) -> AsyncGenerator[Page, None]:
    """Open a new page in the persistent context for *ats_family*.

    The page is closed when the context manager exits, even on error.

    Parameters
    ----------
    ats_family:
        Forwarded to :func:`get_context`.
    timeout_ms:
        Default navigation and element-action timeout in milliseconds.
    """
    context = await get_context(ats_family)
    page = await context.new_page()
    page.set_default_timeout(timeout_ms)
    page.set_default_navigation_timeout(timeout_ms)

    log.debug("browser.page_opened", ats_family=ats_family)
    try:
        yield page
    finally:
        try:
            await page.close()
            log.debug("browser.page_closed", ats_family=ats_family)
        except Exception:
            pass


async def close_all() -> None:
    """Close all open browser contexts and the Playwright instance.

    Call this during graceful shutdown (e.g. from an ``atexit`` hook or the
    top-level ``finally`` block of the agent process).
    """
    global _contexts, _playwright_instance

    for ats_family, context in list(_contexts.items()):
        try:
            await context.close()
            log.info("browser.context_closed", ats_family=ats_family)
        except Exception as exc:
            log.warning(
                "browser.context_close_failed",
                ats_family=ats_family,
                error=str(exc),
            )

    _contexts.clear()

    if _playwright_instance is not None:
        try:
            await _playwright_instance.stop()
            log.info("playwright.stopped")
        except Exception as exc:
            log.warning("playwright.stop_failed", error=str(exc))
        _playwright_instance = None
