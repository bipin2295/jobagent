"""Manual submit hand-off.

The agent does all the tedious work — navigate, fill every field, upload the
resume and cover letter, answer the compliance questions — then stops and hands
the live browser to the operator, who reviews the form, solves any CAPTCHA, and
clicks Submit themselves.

This deliberately sidesteps reCAPTCHA Enterprise and bot-detection heuristics:
the final click is a genuine human interaction, and submission confirmation
comes from the operator rather than from fragile DOM/keyword sniffing.

Returned decisions
------------------
``"submitted"``
    Operator clicked Submit and it went through.
``"skipped"``
    Operator chose not to submit this one (also the safe default when there is
    no TTY, so a backgrounded run never silently claims a submission).
``"quit"``
    Operator wants to stop the whole run.
"""

from __future__ import annotations

import os

import anyio
import structlog

log = structlog.get_logger(__name__)

#: Where the operator can watch/drive the containerised browser.
NOVNC_URL = os.getenv("JOBAGENT_NOVNC_URL", "http://localhost:6080/vnc.html")

_BANNER = """
╭──────────────────────────────────────────────────────────────────────╮
│  MANUAL SUBMIT — the form is filled and waiting for you              │
╰──────────────────────────────────────────────────────────────────────╯
  Company : {company}
  Role    : {title}
  Page    : {url}

  1. Open the live browser →  {novnc}
  2. Review the form, fix anything wrong, solve any CAPTCHA.
  3. Click Submit yourself.

  Then report back here:
    y  submitted  — I clicked Submit and it went through
    n  skip       — don't submit this one
    q  quit       — stop the whole run
"""

_PROMPT = "  your choice [y/n/q]: "


async def await_manual_submit(*, company: str, title: str, url: str) -> str:
    """Block until the operator reports what they did.

    Returns ``"submitted"``, ``"skipped"`` or ``"quit"``.
    """
    print(
        _BANNER.format(company=company, title=title, url=url, novnc=NOVNC_URL),
        flush=True,
    )

    while True:
        try:
            raw = await anyio.to_thread.run_sync(lambda: input(_PROMPT))
        except (EOFError, KeyboardInterrupt):
            # No interactive terminal (cron, detached run). Never assume a
            # submission happened — skip instead.
            log.warning("manual.no_tty", note="no TTY for manual prompt; skipping")
            return "skipped"

        choice = raw.strip().lower()
        if choice in ("y", "yes"):
            return "submitted"
        if choice in ("n", "no", "s", "skip"):
            return "skipped"
        if choice in ("q", "quit"):
            return "quit"
        print("  please type y, n or q", flush=True)


async def focus_form(page, form_frame=None) -> None:  # type: ignore[no-untyped-def]
    """Scroll the submit button into view so the operator sees the form.

    Without this the viewport is often left wherever the last fill happened
    (frequently the page footer), which makes the noVNC view confusing.
    Best-effort: never raises.
    """
    target = form_frame if form_frame is not None else page
    for selector in (
        "button[type=submit]",
        "input[type=submit]",
        "#first_name",
    ):
        try:
            el = target.locator(selector).first
            if await el.count() > 0:
                await el.scroll_into_view_if_needed(timeout=3000)
                return
        except Exception:
            continue
