"""Typer CLI for the job application agent.

Commands
--------
  jobagent profile import       Parse master_resume.txt → master_profile.yaml
  jobagent profile show         Pretty-print the loaded profile
  jobagent discover             Collect new jobs from all sources
  jobagent score                Enrich, filter and score discovered jobs
  jobagent tailor               Generate tailored resume + cover letter packages
  jobagent apply                Submit applications (--manual / --live / shadow)
  jobagent track                Check Gmail for application responses
  jobagent jobs                 Browse jobs in the DB (filter by status/country/ATS)
  jobagent llm ping             Verify Portkey connection and log a test cost
  jobagent gmail connect        Open browser to authenticate Gmail (read-only)
  jobagent db init              Create / migrate all DB tables
  jobagent run                  Full pipeline: discover → score → tailor → apply → track
  jobagent schedule             Install/remove daily launchd schedule
  jobagent boards stats         Board registry counts by ATS and status
  jobagent boards list          List boards (filter by ATS / status)
  jobagent boards validate      Probe candidate / stale-dead boards
  jobagent boards add           Add a board by slug or URL
  jobagent boards backfill      Import legacy company slugs + job URLs
  jobagent ctl pause            Pause all workers
  jobagent ctl resume           Resume paused workers
  jobagent ctl stop             Graceful stop
  jobagent status               Print today's summary
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

from jobagent.config import settings  # needed for tailor_cmd

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

app = typer.Typer(
    name="jobagent",
    help="End-to-end autonomous job application agent.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console()

# ── Sub-command groups ────────────────────────────────────────────────────────

profile_app = typer.Typer(help="Manage your master career profile.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

ctl_app = typer.Typer(help="Runtime control (pause / resume / stop).", no_args_is_help=True)
app.add_typer(ctl_app, name="ctl")

boards_app = typer.Typer(
    help="ATS board registry (Greenhouse / Lever / Ashby / Workday slugs).", no_args_is_help=True
)
app.add_typer(boards_app, name="boards")


# ── profile import ────────────────────────────────────────────────────────────

@profile_app.command("import")
def profile_import(
    source: Annotated[
        Optional[Path],
        typer.Option("--source", "-s", help="Path to master_resume.txt (overrides config)."),
    ] = None,
    out: Annotated[
        Optional[Path],
        typer.Option("--out", "-o", help="Output YAML path (default: data/profile/master_profile.yaml)."),
    ] = None,
) -> None:
    """Parse master_resume.txt and write master_profile.yaml."""
    from jobagent.profile.importer import import_resume
    from jobagent.profile.loader import save_profile

    with console.status("[bold green]Parsing master resume…"):
        try:
            profile = import_resume(source)
        except FileNotFoundError as exc:
            console.print(f"[red]✗ {exc}[/red]")
            raise typer.Exit(1) from exc

    dest = save_profile(profile, out)
    console.print(f"[green]✓[/green] Profile imported: [bold]{dest}[/bold]")

    # Print summary table
    t = Table(title="Profile summary", show_header=True)
    t.add_column("Section", style="cyan")
    t.add_column("Items", justify="right")

    t.add_row("Experience (jobs)", str(len(profile.experience)))
    t.add_row("Bullets", str(sum(len(j.bullets) for j in profile.experience)))
    t.add_row("Summaries", str(len(profile.summaries)))
    t.add_row("Skills blocks", str(len(profile.skills)))
    t.add_row("NOT_YET entries", str(len(profile.not_yet)))
    t.add_row("Languages", str(len(profile.languages)))
    t.add_row("Tailoring recipes", str(len(profile.tailoring_recipes)))
    t.add_row("Open items", str(len(profile.open_items)))
    console.print(t)

    if profile.open_items:
        console.print("\n[yellow]⚠ Open items (bullets will omit numbers until resolved):[/yellow]")
        for item in profile.open_items:
            console.print(f"  [yellow]•[/yellow] {item}")
    else:
        console.print("\n[green]✓ No open items — all bullets have confirmed numbers.[/green]")

    # Print skill years summary
    console.print("\n[bold]Skill years (top 15):[/bold]")
    top = sorted(profile.skill_years.skills.items(), key=lambda x: (-x[1], x[0]))[:15]
    for skill, yrs in top:
        bar = "█" * yrs
        console.print(f"  {skill:<30} {bar}  {yrs}")


# ── profile show ──────────────────────────────────────────────────────────────

@profile_app.command("show")
def profile_show(
    section: Annotated[
        Optional[str],
        typer.Option("--section", "-s", help="Section to show (contact/skills/experience/recipes)."),
    ] = None,
) -> None:
    """Display the current master_profile.yaml."""
    from jobagent.profile.loader import load_profile

    try:
        profile = load_profile()
    except FileNotFoundError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc

    section = (section or "").lower()

    if not section or section == "contact":
        rprint("[bold cyan]── Contact ──[/bold cyan]")
        rprint(profile.contact.model_dump())

    if not section or section == "skills":
        rprint("\n[bold cyan]── Skills ──[/bold cyan]")
        for blk in profile.skills:
            rprint(f"  [bold]{blk.category}:[/bold] {blk.text[:80]}…")

    if not section or section == "experience":
        rprint("\n[bold cyan]── Experience ──[/bold cyan]")
        for job in profile.experience:
            end = job.end or "present"
            rprint(f"  [bold]{job.company}[/bold] | {job.title} | {job.start} – {end}")
            for b in job.bullets:
                flag = " [yellow](verify open)[/yellow]" if b.verify_open else ""
                rprint(f"    [{b.id}] [{', '.join(b.tags)}]{flag}")

    if not section or section == "recipes":
        rprint("\n[bold cyan]── Tailoring recipes ──[/bold cyan]")
        for r in profile.tailoring_recipes:
            rprint(f"  [bold]{r.archetype}[/bold] → title={r.title_variant} summary={r.summary_variant}")


# ── llm ping ─────────────────────────────────────────────────────────────────

@app.command("llm-ping")
def llm_ping() -> None:
    """Send a minimal test request to Portkey and confirm cost logging works."""
    from jobagent.llm.client import ConfigError, complete
    from jobagent.llm.cost import session_stats

    try:
        response = complete(
            task="scoring",
            messages=[{"role": "user", "content": 'Reply with {"status": "ok"} only.'}],
        )
        console.print(f"[green]✓ LLM response:[/green] {response.strip()}")
        console.print(f"[dim]Session cost so far: {session_stats()}[/dim]")
    except ConfigError as exc:
        console.print(f"[yellow]⚠ {exc}[/yellow]")
        console.print("That's expected until you add your Portkey key to .env")


# ── gmail connect ─────────────────────────────────────────────────────────────

@app.command("gmail-connect")
def gmail_connect() -> None:
    """Open the browser to authenticate Gmail (read-only OAuth)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import]
        from google.oauth2.credentials import Credentials  # type: ignore[import]
        import json
    except ImportError:
        console.print(
            "[red]✗ google-auth-oauthlib is not installed.[/red]\n"
            "Run: [bold]uv sync --extra tracking[/bold]"
        )
        raise typer.Exit(1)

    from jobagent.config import settings

    secret_path = settings.gmail_client_secret_path
    if not secret_path.exists():
        console.print(
            f"[red]✗ client_secret.json not found at {secret_path}[/red]\n"
            "Follow the setup steps in PLAN.md §20 to create a free Google Cloud project."
        )
        raise typer.Exit(1)

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)

    # Always use the manual code-paste flow — it works both in Docker and natively.
    # run_local_server() fails inside Docker (no real browser despite DISPLAY=:99).
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    console.print("\n[bold cyan]Gmail OAuth — manual flow[/bold cyan]")
    console.print("\n[bold]Step 1:[/bold] Open this URL in your Mac browser:\n")
    console.print(f"  [link]{auth_url}[/link]\n")
    console.print("[bold]Step 2:[/bold] Sign in with the candidate's Gmail account → Allow → copy the code.")
    console.print("[bold]Step 3:[/bold] Paste the code below and press Enter.\n")

    import sys as _sys  # noqa: PLC0415
    try:
        code = input("  Authorisation code: ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise typer.Exit(1)

    if not code:
        console.print("[red]No code entered.[/red]")
        raise typer.Exit(1)

    flow.fetch_token(code=code)
    creds = flow.credentials

    token_path = settings.gmail_token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())

    console.print(f"\n[green]✓ Gmail token saved to {token_path}[/green]")
    console.print("[dim]Token expires in ~7 days (testing mode). Re-run this command when prompted.[/dim]")


# ── db init ───────────────────────────────────────────────────────────────────

@app.command("db-init")
def db_init() -> None:
    """Create or migrate all database tables."""
    from jobagent.db.engine import create_tables
    from jobagent.config import settings

    with console.status("[bold green]Initialising database…"):
        create_tables()
        from jobagent.discovery.company_universe import CompanyUniverse  # noqa: PLC0415
        CompanyUniverse().ensure_initialized()
    console.print(f"[green]✓ Database ready:[/green] {settings.db_path}")


# ── boards ────────────────────────────────────────────────────────────────────

def _split_csv(value: Optional[str]) -> list[str] | None:
    return [v.strip().lower() for v in value.split(",") if v.strip()] if value else None


@boards_app.command("stats")
def boards_stats() -> None:
    """Show board counts per ATS and status."""
    from jobagent.db.engine import create_tables  # noqa: PLC0415
    from jobagent.discovery.company_universe import CompanyUniverse  # noqa: PLC0415

    create_tables()
    universe = CompanyUniverse()
    universe.ensure_initialized()
    stats = universe.stats()
    statuses = ["live", "empty", "candidate", "dead"]
    t = Table(title="ATS board registry", show_header=True)
    t.add_column("ATS", style="cyan")
    for st in statuses:
        t.add_column(st, justify="right")
    t.add_column("total", justify="right", style="bold")
    for ats in sorted({a for a, _ in stats}):
        row = [stats.get((ats, st), 0) for st in statuses]
        t.add_row(ats, *[str(n) for n in row], str(sum(row)))
    console.print(t)


@boards_app.command("list")
def boards_list(
    ats: Annotated[Optional[str], typer.Option("--ats", "-a", help="Comma-separated ATS types.")] = None,
    status: Annotated[Optional[str], typer.Option("--status", "-s", help="live, empty, candidate, dead (comma-separated).")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
) -> None:
    """List boards, most jobs first."""
    from sqlmodel import select  # noqa: PLC0415

    from jobagent.db.engine import create_tables, get_session  # noqa: PLC0415
    from jobagent.db.models import AtsBoard  # noqa: PLC0415

    create_tables()
    stmt = select(AtsBoard)
    if (ats_list := _split_csv(ats)):
        stmt = stmt.where(AtsBoard.ats_type.in_(ats_list))  # type: ignore[attr-defined]
    if (status_list := _split_csv(status)):
        stmt = stmt.where(AtsBoard.status.in_(status_list))  # type: ignore[attr-defined]
    stmt = stmt.order_by(AtsBoard.matched_count.desc(), AtsBoard.job_count.desc()).limit(limit)  # type: ignore[attr-defined]
    with get_session() as session:
        rows = session.exec(stmt).all()
    t = Table(show_header=True)
    for col in ("ATS", "slug", "company", "status", "jobs", "matched", "via", "last checked", "error"):
        t.add_column(col)
    for b in rows:
        slug = f"{b.slug}/{b.board_path}" if b.board_path else b.slug
        checked = b.last_checked_at.strftime("%Y-%m-%d") if b.last_checked_at else "-"
        t.add_row(b.ats_type, slug, b.company_name or "", b.status, str(b.job_count),
                  str(b.matched_count), b.discovered_via, checked, (b.last_error or "")[:30])
    console.print(t)


@boards_app.command("validate")
def boards_validate(
    ats: Annotated[Optional[str], typer.Option("--ats", "-a", help="Comma-separated ATS types (default: all).")] = None,
    limit: Annotated[Optional[int], typer.Option("--limit", "-n", help="Max boards to probe.")] = None,
    all_boards: Annotated[bool, typer.Option("--all", help="Re-probe every board, not just candidates / stale dead ones.")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", "-c")] = 10,
) -> None:
    """Probe boards against their ATS API and update their status."""
    import anyio  # noqa: PLC0415

    from jobagent.db.engine import create_tables  # noqa: PLC0415
    from jobagent.discovery.company_universe import CompanyUniverse  # noqa: PLC0415

    create_tables()
    universe = CompanyUniverse()

    async def _run():  # type: ignore[no-untyped-def]
        return await universe.validate(
            _split_csv(ats), limit=limit, include_all=all_boards, concurrency=concurrency
        )

    with console.status("[bold green]Probing boards…"):
        summary = anyio.run(_run)
    if not summary:
        console.print("[dim]Nothing to validate.[/dim]")
        return
    console.print("Probe results: " + ", ".join(f"[bold]{k}[/bold]={v}" for k, v in sorted(summary.items())))
    boards_stats()


@boards_app.command("add")
def boards_add(
    target: Annotated[str, typer.Argument(help="Board URL, or a slug together with --ats.")],
    ats: Annotated[Optional[str], typer.Option("--ats", "-a", help="ATS type when TARGET is a slug.")] = None,
    validate: Annotated[bool, typer.Option("--validate/--no-validate")] = True,
) -> None:
    """Add a board (e.g. `boards add https://job-boards.greenhouse.io/acme`)."""
    import anyio  # noqa: PLC0415

    from jobagent.db.engine import create_tables  # noqa: PLC0415
    from jobagent.discovery.company_universe import CompanyUniverse, board_key_from_url  # noqa: PLC0415

    create_tables()
    universe = CompanyUniverse()
    if target.startswith("http"):
        key = board_key_from_url(target)
        if key is None:
            console.print("[red]Could not extract an ATS board from that URL.[/red]")
            raise typer.Exit(1)
        ats_type, slug, board_path, host = key
    else:
        if not ats:
            console.print("[red]Pass --ats when adding a bare slug.[/red]")
            raise typer.Exit(1)
        if ats.lower() == "workday":
            console.print("[red]Workday needs a full board URL (host + board path).[/red]")
            raise typer.Exit(1)
        ats_type, slug, board_path, host = ats.lower(), target, "", None
    added = universe.add_board(ats_type, slug, board_path=board_path, host=host, discovered_via="manual")
    console.print(f"{'[green]Added' if added else '[yellow]Already known'}[/]: {ats_type}/{slug}")
    if validate:
        summary = anyio.run(lambda: universe.validate([ats_type], include_all=False))
        if summary:
            console.print("Probe: " + ", ".join(f"{k}={v}" for k, v in summary.items()))


@boards_app.command("backfill")
def boards_backfill() -> None:
    """Import legacy company.ats_slug rows, ATS URLs from stored jobs, and seeds."""
    from jobagent.db.engine import create_tables  # noqa: PLC0415
    from jobagent.discovery.company_universe import CompanyUniverse  # noqa: PLC0415

    create_tables()
    counts = CompanyUniverse().backfill()
    console.print("Backfill: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    boards_stats()


# ── score ─────────────────────────────────────────────────────────────────────

@app.command("apply")
def apply_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max applications to submit per run.")] = 20,
    live: Annotated[bool, typer.Option("--live", help="Submit for real, fully automatic. Overrides shadow_mode in guardrails.yaml.")] = False,
    manual: Annotated[bool, typer.Option("--manual", help="Fill each form, then hand the browser to you for the final Submit click.")] = False,
    visa_confirmed: Annotated[bool, typer.Option("--visa-confirmed", help="Only apply to jobs where visa sponsorship is confirmed (visa_ok=True).")] = False,
) -> None:
    """Submit applications for tailored packages.

    Three modes:

    \b
      (default)  shadow  — fill the form, stop before Submit (per guardrails.yaml)
      --manual           — fill the form, then YOU review and click Submit
      --live             — fill and submit fully automatically

    Visa filter:

    \b
      --visa-confirmed   — only apply to jobs with confirmed visa sponsorship
    """
    import anyio
    from jobagent.apply.orchestrator import run_apply, get_apply_summary
    from jobagent.apply.guardrails import load_guardrails
    from jobagent.apply.manual import NOVNC_URL

    g = load_guardrails()
    # Manual and live both need the shadow gate off so the flow reaches Submit.
    # None = respect guardrails.yaml.
    force_shadow: bool | None = False if (live or manual) else None
    effective_shadow = False if (live or manual) else g.shadow_mode

    if manual:
        console.print("[bold cyan]🖐  MANUAL mode[/bold cyan] — forms are filled automatically; you click Submit.")
        console.print(f"[dim]Live browser: [bold]{NOVNC_URL}[/bold] — open it now.[/dim]")
    elif effective_shadow:
        console.print("[yellow]⚠ Shadow mode ON[/yellow] — forms will be filled but NOT submitted.")
        console.print("[dim]Pass [bold]--manual[/bold] to review and submit yourself, or [bold]--live[/bold] to auto-submit.[/dim]")
    else:
        console.print("[bold red]⚡ LIVE mode[/bold red] — real applications will be submitted.")

    if visa_confirmed:
        console.print("[bold green]Visa filter ON[/bold green] — only applying to jobs with confirmed visa sponsorship.")

    if manual:
        # No spinner: it would clobber the interactive prompt.
        submitted, shadow_stopped, failed = anyio.run(
            run_apply, limit, force_shadow, True, visa_confirmed
        )
    else:
        with console.status(f"[bold green]Applying to top {limit} tailored jobs…"):
            submitted, shadow_stopped, failed = anyio.run(
                run_apply, limit, force_shadow, False, visa_confirmed
            )

    t = Table(title="Apply run complete", show_header=False)
    t.add_column("Metric", style="cyan")
    t.add_column("Count", justify="right", style="bold")
    t.add_row("Submitted (you confirmed)" if manual else "Submitted (live)", str(submitted))
    t.add_row("Shadow-stopped (form filled, not submitted)", str(shadow_stopped))
    t.add_row("Failed / no handler", str(failed))
    console.print(t)

    summary = get_apply_summary()
    if summary:
        console.print("\n[bold]All-time application counts:[/bold]")
        for status, count in sorted(summary.items()):
            console.print(f"  {status:<25} {count}")

    screenshots_path = settings.data_dir / "applications"
    console.print(f"\n[dim]Screenshots saved in {screenshots_path}/{{job_id}}/screenshots/[/dim]")


@app.command("tailor")
def tailor_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max jobs to tailor per run.")] = 20,
) -> None:
    """Build tailored resume + cover letter + answers for the top queued jobs."""
    import anyio
    from jobagent.tailor.orchestrator import run_tailoring, get_tailored_packages

    with console.status(f"[bold green]Tailoring packages for top {limit} queued jobs…"):
        tailored, failed = anyio.run(run_tailoring, limit)

    console.print(f"[green]✓[/green] Tailored: [bold]{tailored}[/bold]  "
                  f"([dim]{failed} failed[/dim])")

    packages = get_tailored_packages(limit=15)
    if not packages:
        console.print("[yellow]No tailored packages yet.[/yellow]")
        return

    t = Table(title="Recent tailored packages", show_header=True)
    t.add_column("Score", justify="right", style="bold green", width=6)
    t.add_column("Title", style="bold")
    t.add_column("Company")
    t.add_column("PDF", style="dim")
    t.add_column("Pitch", no_wrap=False)

    for pkg in packages:
        pdf = Path(pkg["pdf_path"]).name if pkg.get("pdf_path") else "✗ failed"
        pitch = (pkg.get("pitch_angle") or "")[:60]
        t.add_row(
            str(pkg.get("score") or "?"),
            (pkg.get("job_title") or "")[:45],
            (pkg.get("company") or "")[:22],
            pdf,
            pitch,
        )

    console.print(t)
    console.print(f"\n[dim]Packages saved to [bold]{settings.data_dir}/applications/[/bold][/dim]")


@app.command("score")
def score_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max jobs to score per run.")] = 300,
    threshold: Annotated[float, typer.Option("--threshold", help="Embedding similarity floor (0–1).")] = 0.35,
) -> None:
    """Enrich, filter, and score discovered jobs. Shows the top results."""
    import anyio
    from jobagent.pipeline.enrich import run_enrichment
    from jobagent.pipeline.filters import run_filters
    from jobagent.pipeline.score import run_scoring, get_top_jobs

    with console.status("[bold green]Step 1/3 — Enriching jobs (LLM extraction)…"):
        enriched, skipped = anyio.run(run_enrichment)
    console.print(f"[green]✓[/green] Enriched [bold]{enriched}[/bold] jobs  "
                  f"([dim]{skipped} skipped / LLM unavailable[/dim])")

    with console.status("[bold green]Step 2/3 — Applying hard filters…"):
        passed, filtered = run_filters()
    console.print(f"[green]✓[/green] Passed filters: [bold]{passed}[/bold]  "
                  f"([dim]{filtered} filtered out[/dim])")

    with console.status(f"[bold green]Step 3/3 — Scoring top {limit} jobs (embed + LLM rubric)…"):
        queued, rejected = run_scoring(limit=limit, embed_threshold=threshold)
    console.print(f"[green]✓[/green] Queued (score ≥ min): [bold]{queued}[/bold]  "
                  f"([dim]{rejected} below threshold[/dim])")

    # Show top 20 results
    top = get_top_jobs(limit=20)
    if not top:
        console.print("\n[yellow]No scored jobs yet — run discover first.[/yellow]")
        return

    t = Table(title=f"Top {min(len(top), 20)} jobs by score", show_header=True)
    t.add_column("Score", justify="right", style="bold green", width=6)
    t.add_column("Title", style="bold", no_wrap=False)
    t.add_column("Company", style="cyan")
    t.add_column("Location")
    t.add_column("Pitch angle", no_wrap=False)

    for job, score_row in top[:20]:
        location = " / ".join(filter(None, [job.city, job.country, job.remote_type]))
        pitch = (score_row.pitch_angle or "")[:70]
        t.add_row(str(score_row.score), job.title[:50], job.source[:25], location[:25], pitch)

    console.print(t)
    console.print(f"\n[dim]Run [bold]jobagent score --limit 500[/bold] to score more jobs.[/dim]")


# ── run ───────────────────────────────────────────────────────────────────────

@app.command("discover")
def discover_cmd(
    sources: Annotated[
        Optional[str],
        typer.Option("--sources", "-s", help="Comma-separated collector names to run (default: all)."),
    ] = None,
    regions: Annotated[
        Optional[str],
        typer.Option("--regions", "-r", help="Comma-separated regions to include, e.g. eu,india,remote."),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", "-n", help="Stop after this many NEW jobs are stored."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Collect but do not write to DB.")] = False,
) -> None:
    """Collect new jobs from all configured sources and store them in the DB."""
    import anyio
    from jobagent.discovery.runner import discover_all
    from jobagent.config import load_yaml

    search_cfg = load_yaml("search")
    if regions:
        # narrow regions to what was requested
        region_set = {r.strip() for r in regions.split(",")}
        search_cfg["_active_regions"] = region_set
    if sources:
        search_cfg["_active_sources"] = {s.strip() for s in sources.split(",")}
    if limit:
        search_cfg["_limit"] = limit
    search_cfg["_dry_run"] = dry_run

    # --sources flag wins; otherwise fall back to active_ats in guardrails.yaml
    if sources:
        only_sources: list[str] | None = [s.strip() for s in sources.split(",")]
    else:
        from jobagent.apply.guardrails import load_guardrails  # noqa: PLC0415
        g = load_guardrails()
        only_sources = list(g.active_ats) if g.active_ats else None
        # "linkedin" in active_ats describes the APPLY handler, not a
        # discovery collector — the collector that actually performs a fresh
        # LinkedIn search is registered as "jobspy" (it covers LinkedIn,
        # Indeed, and Glassdoor in one scrape). Without this, active_ats
        # discovery runs never collect fresh LinkedIn postings at all; every
        # "linkedin" job comes from stale Adzuna/Jooble aggregator links
        # re-tagged after the fact.
        if only_sources and "linkedin" in only_sources and "jobspy" not in only_sources:
            only_sources.append("jobspy")
        if only_sources:
            console.print(f"[dim]ATS focus (from guardrails.yaml): [bold]{', '.join(only_sources)}[/bold]. "
                          f"Pass [bold]--sources all[/bold] to override.[/dim]")

    # Special escape hatch: --sources all disables the focus filter
    if only_sources == ["all"]:
        only_sources = None

    with console.status("[bold green]Running discovery…"):
        new_jobs, updated_jobs = anyio.run(discover_all, search_cfg, only_sources)

    # Resolve aggregator redirect URLs to the real ATS so those jobs become
    # tailorable (otherwise they stay ats_type=None and are skipped).
    resolved = attempted = 0
    from jobagent.pipeline.resolve import resolve_pending  # noqa: PLC0415

    with console.status("[bold green]Resolving aggregator URLs…"):
        resolved, attempted = anyio.run(resolve_pending, 200, 0)

    t = Table(title="Discovery complete", show_header=True)
    t.add_column("Metric", style="cyan")
    t.add_column("Count", justify="right", style="bold")
    t.add_row("New jobs stored", str(new_jobs))
    t.add_row("Existing jobs refreshed", str(updated_jobs))
    t.add_row("Total processed", str(new_jobs + updated_jobs))
    t.add_row("Aggregator URLs resolved", f"{resolved}/{attempted}")
    if dry_run:
        t.add_row("[yellow]Dry-run mode[/yellow]", "nothing written")
    console.print(t)

    if new_jobs == 0 and updated_jobs == 0:
        console.print("[yellow]No jobs found. Check your search config and collector keys.[/yellow]")
    else:
        console.print(f"[green]✓[/green] Run [bold]jobagent status[/bold] to see today's totals.")


@app.command("resolve")
def resolve_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max jobs to resolve.")] = 150,
) -> None:
    """Resolve aggregator apply URLs to the real ATS (Greenhouse/Lever/Ashby/Workday)."""
    import anyio
    from jobagent.pipeline.resolve import resolve_pending

    with console.status("[bold green]Resolving aggregator URLs…"):
        resolved, attempted = anyio.run(resolve_pending, limit)

    t = Table(title="URL resolution", show_header=True)
    t.add_column("Metric", style="cyan")
    t.add_column("Count", justify="right", style="bold")
    t.add_row("Attempted", str(attempted))
    t.add_row("Resolved to a known ATS", str(resolved))
    console.print(t)


@app.command("run")
def run_pipeline(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max applications this run.")] = 40,
    regions: Annotated[
        Optional[str],
        typer.Option("--regions", "-r", help="Comma-separated regions to include (e.g. eu,me)."),
    ] = None,
    shadow: Annotated[bool, typer.Option("--shadow/--no-shadow", help="Force shadow (no-submit) mode.")] = False,
    stage: Annotated[
        Optional[str],
        typer.Option("--stage", help="Run only one stage: discover|score|tailor|apply|track."),
    ] = None,
) -> None:
    """Full pipeline: discover → score → tailor → apply → track."""
    from jobagent.scheduler import run as _run
    import anyio

    region_list = [r.strip() for r in regions.split(",")] if regions else None
    anyio.run(_run, limit, region_list, shadow, stage)


# ── ctl commands ──────────────────────────────────────────────────────────────

@ctl_app.command("pause")
def ctl_pause() -> None:
    """Pause all running workers after their current action."""
    from jobagent.control.flags import request_pause
    request_pause()
    console.print("[yellow]⏸ Pause requested. Workers will stop after the current action.[/yellow]")


@ctl_app.command("resume")
def ctl_resume() -> None:
    """Resume paused workers."""
    from jobagent.control.flags import request_resume
    request_resume()
    console.print("[green]▶ Resume requested.[/green]")


@ctl_app.command("stop")
def ctl_stop() -> None:
    """Graceful stop – workers finish the current page then stop."""
    from jobagent.control.flags import request_stop
    from pathlib import Path
    request_stop()
    Path("data/STOP").touch()
    console.print("[red]■ Stop requested. Touch data/STOP removed automatically on next start.[/red]")


# ── status ────────────────────────────────────────────────────────────────────

@app.command("status")
def status() -> None:
    """Show today's run summary."""
    try:
        from jobagent.db.engine import get_session
        from jobagent.db.models import Application, Job, Run
        from sqlmodel import select, func
        from datetime import date, datetime, timezone

        with get_session() as session:
            today_start = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)

            # Jobs
            total_jobs  = session.exec(select(func.count(Job.id))).one()  # type: ignore[call-overload]
            today_jobs  = session.exec(
                select(func.count(Job.id)).where(Job.first_seen_at >= today_start)
            ).one()  # type: ignore[call-overload]

            # Applications
            total_apps  = session.exec(select(func.count(Application.id))).one()  # type: ignore[call-overload]
            today_apps  = session.exec(
                select(func.count(Application.id)).where(Application.created_at >= today_start)
            ).one()  # type: ignore[call-overload]
            submitted   = session.exec(
                select(func.count(Application.id)).where(Application.status == "submitted")
            ).one()  # type: ignore[call-overload]
            blocked     = session.exec(
                select(func.count(Application.id)).where(Application.status == "blocked")
            ).one()  # type: ignore[call-overload]

            # Per-source breakdown (top 10)
            by_source = session.exec(
                select(Job.source, func.count(Job.id))
                .group_by(Job.source)
                .order_by(func.count(Job.id).desc())
            ).all()[:10]

        from jobagent.llm.cost import today_cost
        cost = today_cost()

        t = Table(title=f"Status — {date.today()}", show_header=False)
        t.add_column("Metric", style="cyan")
        t.add_column("Value", justify="right")
        t.add_row("Jobs discovered today", str(today_jobs))
        t.add_row("Jobs in DB (total)",    str(total_jobs))
        t.add_row("Applications today",    str(today_apps))
        t.add_row("Applications submitted",str(submitted))
        t.add_row("Applications blocked",  str(blocked))
        t.add_row("LLM cost today",        f"${cost:.4f}")
        console.print(t)

        if by_source:
            console.print("\n[bold]Jobs by source:[/bold]")
            for src, n in by_source:
                bar = "█" * min(n // 100, 40)
                console.print(f"  [cyan]{src or '?':<25}[/cyan] {bar} [bold]{n}[/bold]")

    except Exception as exc:
        console.print(f"[red]✗ Status error:[/red] {exc}")
        console.print("[dim]Run [bold]make db-init[/bold] if the DB has never been initialised.[/dim]")


# ── jobs ──────────────────────────────────────────────────────────────────────

@app.command("jobs")
def jobs_cmd(
    status: Annotated[str, typer.Option("--status", "-s", help="Filter by status (queued/tailored/submitted/all).")] = "queued",
    country: Annotated[Optional[str], typer.Option("--country", "-c", help="Filter by country code, e.g. CA,GB,SG.")] = None,
    ats: Annotated[Optional[str], typer.Option("--ats", "-a", help="Filter by ATS type, e.g. greenhouse,workday.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows to show.")] = 50,
    min_score: Annotated[int, typer.Option("--min-score", help="Minimum score.")] = 0,
    visa_confirmed: Annotated[bool, typer.Option("--visa-confirmed", help="Only jobs with confirmed visa sponsorship (visa_ok=True).")] = False,
    relocation_confirmed: Annotated[bool, typer.Option("--relocation-confirmed", help="Only jobs with confirmed relocation support (relocation_ok=True).")] = False,
) -> None:
    """Browse jobs stored in the DB.

    Examples:

    \b
      jobagent jobs                                   # top 50 queued jobs
      jobagent jobs --status tailored                 # ready-to-apply packages
      jobagent jobs --status submitted                # already submitted
      jobagent jobs --country CA,GB                   # Canadian + UK jobs
      jobagent jobs --ats greenhouse --status queued
      jobagent jobs --status all --min-score 85
      jobagent jobs --visa-confirmed                  # only visa-confirmed jobs
      jobagent jobs --visa-confirmed --relocation-confirmed  # both confirmed
    """
    from jobagent.db.engine import get_session
    from jobagent.db.models import Job, Application
    from sqlmodel import select
    from rich import box as rbox

    countries = [c.strip().upper() for c in country.split(",")] if country else []
    ats_types = [a.strip().lower() for a in ats.split(",")] if ats else []

    # Application-level statuses live on the Application table, not the Job table.
    # Job-level statuses: queued, discovered, filtered_out, applied, rejected_low_score
    # Application-level: submitted, tailored, failed, blocked, shadow_stopped, skipped, ghosted
    # NOTE: "queued" is a JOB status (scored, ready for apply pipeline) — do NOT
    # route it to the Application table where stale queued rows accumulate.
    _APP_STATUSES = {
        "submitted", "tailored", "failed", "blocked",
        "shadow_stopped", "skipped", "ghosted",
    }

    use_application_table = status != "all" and status in _APP_STATUSES

    with get_session() as s:
        if use_application_table:
            stmt = (
                select(Job)
                .join(Application, Application.job_id == Job.id)
                .where(Application.status == status)
                .distinct()
            )
        else:
            stmt = select(Job)
            if status != "all":
                stmt = stmt.where(Job.status == status)

        if countries:
            stmt = stmt.where(Job.country.in_(countries))  # type: ignore[attr-defined]
        if ats_types:
            stmt = stmt.where(Job.ats_type.in_(ats_types))  # type: ignore[attr-defined]
        if min_score:
            stmt = stmt.where(Job.score >= min_score)
        if visa_confirmed:
            stmt = stmt.where(Job.visa_ok == True)   # noqa: E712
        if relocation_confirmed:
            stmt = stmt.where(Job.relocation_ok == True)  # noqa: E712
        stmt = stmt.order_by(Job.score.desc().nullslast()).limit(limit)
        jobs = s.exec(stmt).all()

    if not jobs:
        console.print(f"[yellow]No jobs found[/yellow] for status={status!r}"
                      + (f" country={country!r}" if country else "")
                      + (f" ats={ats!r}" if ats else ""))
        return

    # Use a wide console so columns aren't dropped in narrow terminals
    from rich.console import Console as _Console  # noqa: PLC0415
    wide = _Console(width=200)

    t = Table(
        title=f"Jobs — {status}" + (f" | country={country}" if country else "")
                                  + (f" | ats={ats}" if ats else ""),
        box=rbox.SIMPLE_HEAD,
        show_lines=False,
        expand=False,
    )
    t.add_column("ID",      style="dim",         width=6,  no_wrap=True)
    t.add_column("Scr",     justify="right",     width=4,  no_wrap=True)
    t.add_column("Cty",                          width=4,  no_wrap=True)
    t.add_column("V",       justify="center",    width=3,  no_wrap=True)  # visa_ok
    t.add_column("R",       justify="center",    width=3,  no_wrap=True)  # relocation_ok
    t.add_column("ATS",     style="cyan",        width=11, no_wrap=True)
    t.add_column("Src",     style="dim",         width=10, no_wrap=True)
    t.add_column("Status",                       width=13, no_wrap=True)
    t.add_column("Title",                        min_width=35, no_wrap=True)
    t.add_column("URL",     style="dim",         min_width=28, no_wrap=True)

    STATUS_COLOUR = {
        "queued":        "cyan",
        "tailored":      "green",
        "submitted":     "bold green",
        "applied":       "bold green",
        "failed":        "red",
        "blocked":       "yellow",
        "filtered_out":  "dim",
        "ghosted":       "dim",
        "skipped":       "dim",
    }

    # For application-level status views, also fetch the application row
    # so we can show submitted_at, app.id etc.
    app_by_job: dict[int, "Application"] = {}
    if use_application_table and jobs:
        with get_session() as s:
            job_ids = [j.id for j in jobs]
            app_rows = s.exec(
                select(Application)
                .where(Application.job_id.in_(job_ids))  # type: ignore[attr-defined]
                .where(Application.status == status)
                .order_by(Application.id.desc())
            ).all()
            for a in app_rows:
                if a.job_id not in app_by_job:
                    app_by_job[a.job_id] = a

    for j in jobs:
        sc = str(j.score) if j.score is not None else "—"
        cty = j.country or "?"
        ats_v = j.ats_type or "—"
        app = app_by_job.get(j.id)
        display_status = (app.status if app else j.status) or "?"
        col = STATUS_COLOUR.get(display_status, "white")
        # For submitted jobs, show submission date if available
        date_str = ""
        if app and app.submitted_at:
            date_str = app.submitted_at.strftime("%-d %b")
        visa_icon = "[green]✓[/green]" if j.visa_ok else "[dim]·[/dim]"
        reloc_icon = "[green]✓[/green]" if j.relocation_ok else "[dim]·[/dim]"
        t.add_row(
            str(j.id),
            sc,
            cty,
            visa_icon,
            reloc_icon,
            ats_v,
            (j.source or "?")[:10],
            f"[{col}]{display_status}[/{col}]",
            (j.title or "(no title)")[:55],
            date_str if date_str else (j.url or "")[:55],
        )

    wide.print(t)
    wide.print(f"[dim]Showing {len(jobs)} jobs  |  DB: [bold]{settings.db_path}[/bold][/dim]")


# ── track ─────────────────────────────────────────────────────────────────────

@app.command("track")
def track_cmd() -> None:
    """Check Gmail for responses to submitted applications and update statuses.

    Classifies new emails as: acknowledged | rejected | interview_invite |
    assessment | offer | other.  Marks applications as 'ghosted' after 30
    days with no reply.
    """
    import anyio
    from jobagent.tracking.gmail_watcher import run_gmail_watcher
    from jobagent.tracking.summary import print_summary, build_run_summary

    with console.status("[bold green]Checking Gmail for application responses…"):
        counts = anyio.run(run_gmail_watcher)

    t = Table(title="Tracking update", show_header=False)
    t.add_column("Metric", style="cyan")
    t.add_column("Count", justify="right", style="bold")
    t.add_row("Status changes", str(counts.get("status_changed", 0)))
    t.add_row("Ghosted (no reply 30d)", str(counts.get("ghosted", 0)))
    t.add_row("Skipped (no alias/domain)", str(counts.get("skipped", 0)))
    console.print(t)

    print_summary(build_run_summary(since_hours=24))


# ── schedule ──────────────────────────────────────────────────────────────────

@app.command("schedule")
def schedule_cmd(
    remove: Annotated[bool, typer.Option("--remove", help="Remove the launchd schedule.")] = False,
    status_only: Annotated[bool, typer.Option("--status", help="Check schedule status.")] = False,
) -> None:
    """Install (or remove) a launchd plist to run jobagent daily at 08:00.

    Uses: docker compose exec app jobagent run --limit 20
    """
    import subprocess, sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    scripts_dir = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
    script = scripts_dir / "install_schedule.py"
    if not script.exists():
        console.print(f"[red]Script not found:[/red] {script}")
        return
    args = [sys.executable, str(script)]
    if remove:
        args.append("--remove")
    elif status_only:
        args.append("--status")
    subprocess.run(args)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
