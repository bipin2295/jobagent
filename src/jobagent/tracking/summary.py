"""Run summary — human-readable report at the end of each run.

Printed to the terminal and stored in the ``runs`` table's ``summary_json``
field so it can be retrieved later.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

import structlog

from jobagent.db.engine import get_session
from jobagent.db.models import Application, Job

log = structlog.get_logger(__name__)


def build_run_summary(since_hours: int = 24) -> dict:
    """Build a summary dict covering the last *since_hours* hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    with get_session() as s:
        from sqlmodel import select, func  # noqa: PLC0415

        # Applications submitted in the window
        recent_apps = s.exec(
            select(Application)
            .where(Application.updated_at >= cutoff)
        ).all()

        # All-time counts by status
        all_counts = dict(
            s.exec(
                select(Application.status, func.count(Application.id))
                .group_by(Application.status)
            ).all()
        )

        # Recent submissions with job titles
        submitted_today = [
            a for a in recent_apps if a.status == "submitted"
        ]
        job_ids = {a.job_id for a in submitted_today}
        jobs_map = {j.id: j for j in s.exec(
            select(Job).where(Job.id.in_(job_ids))  # type: ignore[attr-defined]
        ).all()} if job_ids else {}

        # Email responses received today
        from jobagent.db.models import Email  # noqa: PLC0415
        recent_emails = s.exec(
            select(Email)
            .where(Email.received_at >= cutoff)
        ).all()
        email_counts = Counter(e.classification for e in recent_emails)

    submissions = []
    for app in submitted_today:
        job = jobs_map.get(app.job_id)
        submissions.append({
            "app_id": app.id,
            "title": job.title if job else "Unknown",
            "url": job.url if job else "",
            "ats": job.ats_type if job else "",
        })

    return {
        "window_hours": since_hours,
        "submitted": len(submitted_today),
        "submissions": submissions,
        "failed": sum(1 for a in recent_apps if a.status == "failed"),
        "skipped": sum(1 for a in recent_apps if a.status in ("skipped", "shadow_stopped")),
        "email_responses": dict(email_counts),
        "all_time": all_counts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def print_summary(summary: dict | None = None) -> None:
    """Print a formatted run summary to stdout."""
    if summary is None:
        summary = build_run_summary()

    from rich.console import Console  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415
    from rich.panel import Panel  # noqa: PLC0415
    from rich import box  # noqa: PLC0415

    console = Console()

    console.print()
    console.print(Panel(
        f"[bold]Run summary[/bold] — last {summary['window_hours']}h",
        style="blue",
    ))

    # Quick metrics
    console.print(f"  Submitted:  [bold green]{summary['submitted']}[/bold green]")
    console.print(f"  Failed:     [bold red]{summary['failed']}[/bold red]")
    console.print(f"  Skipped:    [dim]{summary['skipped']}[/dim]")

    # Email responses
    responses = summary.get("email_responses", {})
    if responses:
        console.print()
        console.print("[bold]Email responses today:[/bold]")
        for label, count in sorted(responses.items(), key=lambda x: -x[1]):
            colour = {
                "interview_invite": "bold green",
                "offer": "bold magenta",
                "rejected": "red",
                "acknowledged": "cyan",
            }.get(label, "white")
            console.print(f"  [{colour}]{label}[/{colour}]: {count}")

    # What was submitted
    if summary["submissions"]:
        console.print()
        t = Table(title="Applications submitted", box=box.SIMPLE)
        t.add_column("App", style="dim")
        t.add_column("Title")
        t.add_column("ATS", style="cyan")
        for sub in summary["submissions"]:
            t.add_row(str(sub["app_id"]), sub["title"][:45], sub["ats"] or "?")
        console.print(t)

    # All-time counts
    console.print()
    console.print("[bold]All-time application counts:[/bold]")
    for status, count in sorted(summary["all_time"].items()):
        console.print(f"  {status:<25} {count}")
    console.print()
