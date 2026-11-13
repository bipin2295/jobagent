"""Gmail watcher — classify incoming emails per open application.

Runs at the end of each ``jobagent run`` (and on a 30-minute schedule while a
run is active).  For each application that is in a ``submitted`` / ``acknowledged``
state and was submitted within the last 90 days:

1. Issue a **narrow Gmail search** restricted to the application's email alias
   and company domain, so private mail is never fetched.
2. For any new email, call the LLM to classify it as one of:
   ``acknowledged | rejected | interview_invite | assessment | offer | other``
3. Update ``Application.status`` and append an ``AppEvent``.
4. After 30 days with no reply → mark as ``ghosted``.

The function is fully idempotent — running it multiple times is safe because
each email's Gmail message-ID is stored in the ``Email`` table to prevent
duplicate processing.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from jobagent.db.engine import get_session
from jobagent.db.models import Application, AppEvent, Email, Job
from jobagent.email_provider import EmailProvider
from jobagent.llm.client import get_client

log = structlog.get_logger(__name__)

# Track applications that submitted up to 90 days ago
_TRACK_DAYS = 90
# Ghost after this many days with no response
_GHOST_DAYS = 30

# LLM prompt for email classification
_CLASSIFY_PROMPT = """Classify this email received after a job application.

Company: {company}
Job title: {title}
Email subject: {subject}
Email snippet: {snippet}

Respond with ONLY one of these labels (no explanation):
- acknowledged    (automated "we got your application" reply, no decision)
- rejected        (application declined, no interview)
- interview_invite (recruiter wants to schedule a call or interview)
- assessment      (take-home test, coding challenge, or online assessment)
- offer           (job offer)
- other           (unrelated or unclear)

Label:"""


async def run_gmail_watcher() -> dict[str, int]:
    """Check Gmail for responses to all open applications.

    Ghosting (30-day no-reply → ghosted) runs regardless of Gmail connectivity.
    Email search only runs when Gmail is connected.

    Returns a dict of counts: {status_changed: N, ghosted: N, skipped: N}.
    """
    provider = EmailProvider()
    cutoff = datetime.now(timezone.utc) - timedelta(days=_TRACK_DAYS)
    ghost_cutoff = datetime.now(timezone.utc) - timedelta(days=_GHOST_DAYS)

    with get_session() as s:
        from sqlmodel import select  # noqa: PLC0415
        apps = s.exec(
            select(Application)
            .where(Application.status.in_(["submitted", "acknowledged"]))  # type: ignore[attr-defined]
            .where(Application.submitted_at >= cutoff)
        ).all()
        jobs_by_id = {j.id: j for j in s.exec(select(Job)).all()}

    counts = {"status_changed": 0, "ghosted": 0, "skipped": 0}

    for app in apps:
        job = jobs_by_id.get(app.job_id)
        if not job:
            counts["skipped"] += 1
            continue

        # Ghost check
        if app.submitted_at and app.submitted_at < ghost_cutoff and app.status == "submitted":
            _update_status(app.id, "ghosted", "No response after 30 days")
            counts["ghosted"] += 1
            log.info("gmail_watcher.ghosted", app_id=app.id, company=job.company_id)
            continue

        # Email search requires Gmail — skip gracefully when disconnected
        if not provider.connected:
            counts["skipped"] += 1
            continue

        # Build search query
        alias = _guess_alias(app, job)
        company_domain = _extract_domain(job.url or "")
        since = app.submitted_at or cutoff

        try:
            emails = await provider.search_recent(
                alias=alias,
                sender_domain=company_domain if company_domain else None,
                since=since,
                max_results=5,
            )
        except Exception as exc:
            log.warning("gmail_watcher.search_error", app_id=app.id, error=str(exc)[:60])
            counts["skipped"] += 1
            continue

        for email_result in emails:
            msg_id = _make_msg_id(app.id, email_result.subject, email_result.snippet)
            if _already_processed(msg_id):
                continue

            label = await _classify_email(email_result, job)
            log.info(
                "gmail_watcher.email_classified",
                app_id=app.id,
                label=label,
                subject=email_result.subject[:50],
            )

            _store_email(app.id, msg_id, email_result, label)

            if label in ("rejected", "interview_invite", "assessment", "offer"):
                _update_status(app.id, label, email_result.subject[:100])
                counts["status_changed"] += 1
            elif label == "acknowledged" and app.status == "submitted":
                _update_status(app.id, "acknowledged", email_result.subject[:100])
                counts["status_changed"] += 1

    log.info("gmail_watcher.done", **counts)
    return counts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _classify_email(email_result: Any, job: Job) -> str:
    """Call the LLM to classify the email.  Falls back to keyword heuristic."""
    # Fast heuristic first (no LLM cost)
    text = (email_result.subject + " " + email_result.snippet).lower()
    if re.search(r"thank you for apply|received your application|application.*received", text):
        return "acknowledged"
    if re.search(r"regret|not.*move forward|not.*selected|position.*filled|unfortunately|decline", text):
        return "rejected"
    if re.search(r"schedule.*interview|invite.*interview|interview.*invite|next step|chat with", text):
        return "interview_invite"
    if re.search(r"assessment|coding.*challenge|take.home|test.*link|hackerrank|codility", text):
        return "assessment"
    if re.search(r"offer|congratulation|pleased to.*inform|joining", text):
        return "offer"

    # LLM fallback
    try:
        from jobagent.db.models import Company  # noqa: PLC0415
        with get_session() as s:
            company = s.get(Company, job.company_id)
        company_name = company.name if company else "unknown"

        client = get_client()
        prompt = _CLASSIFY_PROMPT.format(
            company=company_name,
            title=job.title or "",
            subject=email_result.subject,
            snippet=email_result.snippet[:300],
        )
        # openai client is sync — run in thread to avoid blocking the event loop
        def _call() -> str:
            r = client.chat.completions.create(
                model="@recommended/deepseek-ai/DeepSeek-V4.1-Flash-1017",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
            )
            return (r.choices[0].message.content or "").strip().lower()

        resp = await asyncio.to_thread(_call)
        label = resp.split()[0] if resp.split() else "other"
        valid = {"acknowledged", "rejected", "interview_invite", "assessment", "offer", "other"}
        return label if label in valid else "other"
    except Exception:
        return "other"


def _guess_alias(app: Application, job: Job) -> str | None:
    """Return the email alias used for this application, if deterministic."""
    from jobagent.apply.account_manager import make_alias  # noqa: PLC0415
    ats = job.ats_type or "unknown"
    from jobagent.pipeline.ats_detect import extract_ats_slug  # noqa: PLC0415
    result = extract_ats_slug(job.apply_url or job.url or "")
    tenant = result[1] if result else "unknown"
    if ats == "workday":
        return make_alias(ats, tenant)
    # For Greenhouse/Lever we used the main email, so search by company domain
    return None


def _extract_domain(url: str) -> str | None:
    """Extract base domain from a URL for a broad search."""
    try:
        from urllib.parse import urlparse  # noqa: PLC0415
        host = urlparse(url).netloc
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else None
    except Exception:
        return None


def _make_msg_id(app_id: int, subject: str, snippet: str) -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha1(f"{app_id}:{subject}:{snippet[:50]}".encode()).hexdigest()


def _already_processed(msg_id: str) -> bool:
    from sqlmodel import select  # noqa: PLC0415
    with get_session() as s:
        return s.exec(select(Email).where(Email.gmail_id == msg_id)).first() is not None


def _store_email(app_id: int, msg_id: str, email_result: Any, label: str) -> None:
    with get_session() as s:
        email = Email(
            application_id=app_id,
            gmail_id=msg_id,
            received_at=datetime.now(timezone.utc),
            from_addr="",  # not extracted yet — Gmail search gives us snippets only
            subject=email_result.subject[:200],
            snippet=email_result.snippet[:500],
            classification=label,
        )
        s.add(email)
        s.commit()


def _update_status(app_id: int, new_status: str, note: str = "") -> None:
    with get_session() as s:
        app = s.get(Application, app_id)
        if not app:
            return
        old_status = app.status
        app.status = new_status
        app.updated_at = datetime.now(timezone.utc)
        event = AppEvent(
            application_id=app_id,
            ts=datetime.now(timezone.utc),
            from_status=old_status,
            to_status=new_status,
            note=note[:500],
        )
        s.add(app)
        s.add(event)
        s.commit()
