#!/usr/bin/env python3
"""Database cleanup migration.

Runs in 4 phases; prints a before/after diff at the end.
Safe to re-run: every change is idempotent.

Phases
------
1. Delete test / orphan rows.
2. Standardise NULL-equivalent values.
3. Enrich company rows (real names + country from jobs).
4. Backfill job.country from company; mark US-only jobs filtered_out.

Usage
-----
    uv run python scripts/db_cleanup.py           # dry run (default)
    uv run python scripts/db_cleanup.py --apply   # write to DB
"""

from __future__ import annotations

import sys
from collections import Counter
from textwrap import dedent

DRY_RUN = "--apply" not in sys.argv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _connect():
    import sqlite3
    from jobagent.config import settings
    con = sqlite3.connect(str(settings.db_path))
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def snapshot(con) -> dict:
    """Capture key data-quality counts for before/after comparison."""
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "jobs":                      q("SELECT count(*) FROM job"),
        "job country NULL/?":        q("SELECT count(*) FROM job WHERE country IS NULL OR country='?'"),
        "job country US":            q("SELECT count(*) FROM job WHERE country='US'"),
        "job ats_type NULL":         q("SELECT count(*) FROM job WHERE ats_type IS NULL"),
        "job apply_url NULL":        q("SELECT count(*) FROM job WHERE apply_url IS NULL"),
        "job score NULL":            q("SELECT count(*) FROM job WHERE score IS NULL"),
        "job status filtered_out":   q("SELECT count(*) FROM job WHERE status='filtered_out'"),
        "job status queued":         q("SELECT count(*) FROM job WHERE status='queued'"),
        "companies":                 q("SELECT count(*) FROM company"),
        "co name=slug placeholder":  q("SELECT count(*) FROM company WHERE ats_slug IS NOT NULL AND lower(name)=lower(ats_slug)"),
        "co country NULL":           q("SELECT count(*) FROM company WHERE country IS NULL"),
        "accounts":                  q("SELECT count(*) FROM account"),
        "ats_boards":                q("SELECT count(*) FROM ats_board"),
        "orphan job_source":         q("SELECT count(*) FROM job_source WHERE job_id NOT IN (SELECT id FROM job)"),
        "orphan score":              q("SELECT count(*) FROM score WHERE job_id NOT IN (SELECT id FROM job)"),
        "job orphan company":        q("SELECT count(*) FROM job WHERE company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM company)"),
        "job no company_id":         q("SELECT count(*) FROM job WHERE company_id IS NULL"),
    }


def print_diff(before: dict, after: dict) -> None:
    print("\n── Before/After ──────────────────────────────────────────")
    print(f"{'Metric':<35} {'Before':>8} {'After':>8}  {'Δ':>7}")
    print("─" * 62)
    for k in before:
        b, a = before[k], after[k]
        diff = a - b
        mark = " ✓" if diff != 0 else ""
        print(f"  {k:<33} {b:>8} {a:>8}  {diff:>+7}{mark}")
    print("─" * 62)


# ---------------------------------------------------------------------------
# Phase 1 — Remove invalid / test / orphan rows
# ---------------------------------------------------------------------------

PHASE1 = dedent("""\
    -- 1a. Test accounts (test-co tenant written by unit tests)
    DELETE FROM account WHERE tenant='test-co';

    -- 1b. Test ats_board slug
    DELETE FROM ats_board WHERE slug='testco';

    -- Helper: collect all job ids to be removed into a temporary set.
    -- Covers (c) test-URL jobs, (d) orphan-FK jobs, (e) null-company_id jobs.

    -- 1c-e. Delete in FK order: app_event → application → job_source/score → job
    --       for all three categories of bad jobs at once.

    DELETE FROM app_event WHERE application_id IN (
        SELECT a.id FROM application a WHERE a.job_id IN (
            SELECT id FROM job WHERE
                url LIKE '%test.wd3.myworkdayjobs%'
                OR (company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM company))
                OR company_id IS NULL));

    DELETE FROM application WHERE job_id IN (
        SELECT id FROM job WHERE
            url LIKE '%test.wd3.myworkdayjobs%'
            OR (company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM company))
            OR company_id IS NULL);

    DELETE FROM job_source WHERE job_id IN (
        SELECT id FROM job WHERE
            url LIKE '%test.wd3.myworkdayjobs%'
            OR (company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM company))
            OR company_id IS NULL);

    DELETE FROM score WHERE job_id IN (
        SELECT id FROM job WHERE
            url LIKE '%test.wd3.myworkdayjobs%'
            OR (company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM company))
            OR company_id IS NULL);

    DELETE FROM job WHERE
        url LIKE '%test.wd3.myworkdayjobs%'
        OR (company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM company))
        OR company_id IS NULL;

    -- 1f. Remaining orphan job_source rows (job deleted elsewhere)
    DELETE FROM job_source WHERE job_id NOT IN (SELECT id FROM job);

    -- 1g. Remaining orphan score rows
    DELETE FROM score WHERE job_id NOT IN (SELECT id FROM job);
""")

# ---------------------------------------------------------------------------
# Phase 2 — Standardise NULL-equivalent / incorrect values
# ---------------------------------------------------------------------------

PHASE2 = dedent("""\
    -- 2a. '?' is not a valid ISO country code - set to NULL
    UPDATE job SET country=NULL WHERE country='?';

    -- 2b. apply_url missing → copy job URL (4 rows)
    UPDATE job SET apply_url=url WHERE apply_url IS NULL AND url IS NOT NULL;

    -- 2c. ats_type recoverable from URL (greenhouse, lever, ashby, workday)
    UPDATE job SET ats_type='greenhouse'
    WHERE ats_type IS NULL
    AND (url  LIKE '%greenhouse.io%' OR apply_url LIKE '%greenhouse.io%');

    UPDATE job SET ats_type='lever'
    WHERE ats_type IS NULL
    AND (url  LIKE '%lever.co%' OR apply_url LIKE '%lever.co%');

    UPDATE job SET ats_type='ashby'
    WHERE ats_type IS NULL
    AND (url  LIKE '%ashbyhq.com%' OR apply_url LIKE '%ashbyhq.com%');

    UPDATE job SET ats_type='workday'
    WHERE ats_type IS NULL
    AND (url  LIKE '%myworkdayjobs.com%' OR apply_url LIKE '%myworkdayjobs.com%');
""")

# ---------------------------------------------------------------------------
# Phase 3 — Enrich company rows
# ---------------------------------------------------------------------------
# 3a. Real company name from ats_board (where we have it and name is still a slug)
# 3b. company.country from most-common non-US country seen in that company's jobs.

def _enrich_companies(con) -> Counter:
    counts: Counter = Counter()

    # 3a. Names: placeholder company name (name=lower(ats_slug)) → real name from ats_board
    rows = con.execute("""
        SELECT c.id, c.ats_slug, b.company_name
        FROM company c
        JOIN ats_board b ON lower(b.slug)=lower(c.ats_slug)
                       AND b.ats_type=c.ats_type
        WHERE c.ats_slug IS NOT NULL
          AND lower(c.name)=lower(c.ats_slug)
          AND b.company_name IS NOT NULL
          AND trim(b.company_name) != ''
    """).fetchall()

    from jobagent.pipeline.normalise import normalise_company
    for cid, slug, real_name in rows:
        norm = normalise_company(real_name)
        # Avoid overwriting if another company already has this norm
        clash = con.execute(
            "SELECT id FROM company WHERE name_norm=? AND id!=?", (norm, cid)
        ).fetchone()
        if clash:
            counts["co_name_clash"] += 1
            continue
        con.execute(
            "UPDATE company SET name=?, name_norm=? WHERE id=?",
            (real_name, norm, cid),
        )
        counts["co_name_updated"] += 1

    # 3b. Countries: use the most-common non-US, non-NULL country seen in the company's jobs
    rows = con.execute("""
        SELECT c.id, j.country, count(*) as n
        FROM company c
        JOIN job j ON j.company_id=c.id
        WHERE c.country IS NULL
          AND j.country IS NOT NULL
          AND j.country != 'US'
        GROUP BY c.id, j.country
    """).fetchall()

    best: dict[int, tuple[str, int]] = {}
    for cid, country, n in rows:
        if cid not in best or n > best[cid][1]:
            best[cid] = (country, n)
    for cid, (country, _) in best.items():
        con.execute("UPDATE company SET country=? WHERE id=?", (country, cid))
        counts["co_country_set"] += 1

    return counts


# ---------------------------------------------------------------------------
# Phase 4 — Backfill job.country from company; mark US-only as filtered_out
# ---------------------------------------------------------------------------

PHASE4 = dedent("""\
    -- 4a. Backfill job.country from company.country for actionable, country-less jobs.
    --     Only for status levels that matter (not filtered_out / rejected_low_score).
    UPDATE job
    SET country = (
        SELECT c.country FROM company c WHERE c.id=job.company_id
    )
    WHERE (country IS NULL)
      AND company_id IS NOT NULL
      AND status NOT IN ('filtered_out', 'rejected_low_score')
      AND (SELECT c.country FROM company c WHERE c.id=job.company_id) IS NOT NULL;

    -- 4b. Any job now tagged US in a non-terminal status → filter out
    --     (US jobs should never reach queued/discovered; these slipped through
    --      because country was NULL during the original filter pass.)
    UPDATE job
    SET status='filtered_out'
    WHERE country='US'
      AND status IN ('discovered', 'queued');
""")

# ---------------------------------------------------------------------------
# Phase 5 — retire jobs whose apply URL can never be reached
# ---------------------------------------------------------------------------

#: Aggregator sources whose listings hide the real ATS behind JavaScript or
#: bot-blocking. Verified empirically: HTTP redirects return 403 or contain no
#: ATS link, and the browser fallback (clicking through) finds nothing.
#: Note ``jobspy_indeed`` and ``arbeitnow`` are deliberately EXCLUDED — they
#: often carry direct company/ATS URLs that *are* resolvable.
UNREACHABLE_SOURCES: tuple[str, ...] = ("adzuna", "jooble")

PHASE5 = dedent("""\
    -- 5. Mark queued jobs from JS-only aggregators as filtered_out.
    --    They cannot be resolved to a real ATS, so they would sit in the queue
    --    forever and misreport available inventory.
    UPDATE job
    SET status='filtered_out'
    WHERE status='queued'
      AND ats_type IS NULL
      AND source IN ('adzuna', 'jooble');
""")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"{'DRY RUN — pass --apply to write' if DRY_RUN else '*** APPLYING CHANGES ***'}")
    con = _connect()

    before = snapshot(con)

    if DRY_RUN:
        print("\nPhase 1 — would delete test/orphan rows")
        for label, sql in [
            ("test-co accounts",          "SELECT count(*) FROM account WHERE tenant='test-co'"),
            ("testco ats_board",          "SELECT count(*) FROM ats_board WHERE slug='testco'"),
            ("test URL jobs",             "SELECT count(*) FROM job WHERE url LIKE '%test.wd3%'"),
            ("orphan FK jobs",            "SELECT count(*) FROM job WHERE company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM company)"),
            ("null company_id jobs",      "SELECT count(*) FROM job WHERE company_id IS NULL"),
            ("orphan job_source",         "SELECT count(*) FROM job_source WHERE job_id NOT IN (SELECT id FROM job)"),
            ("orphan score",              "SELECT count(*) FROM score WHERE job_id NOT IN (SELECT id FROM job)"),
        ]:
            n = con.execute(sql).fetchone()[0]
            print(f"  {label}: {n} rows")

        print("\nPhase 2 — would standardise")
        for label, sql in [
            ("country='?' → NULL",        "SELECT count(*) FROM job WHERE country='?'"),
            ("apply_url NULL → url",      "SELECT count(*) FROM job WHERE apply_url IS NULL AND url IS NOT NULL"),
            ("ats_type fix from URL",     "SELECT count(*) FROM job WHERE ats_type IS NULL AND (url LIKE '%greenhouse%' OR url LIKE '%lever.co%' OR url LIKE '%ashbyhq%' OR url LIKE '%myworkdayjobs%')"),
        ]:
            n = con.execute(sql).fetchone()[0]
            print(f"  {label}: {n} rows")

        print("\nPhase 3 — would enrich company rows")
        n1 = con.execute("""
            SELECT count(*) FROM company c JOIN ats_board b
            ON lower(b.slug)=lower(c.ats_slug) AND b.ats_type=c.ats_type
            WHERE c.ats_slug IS NOT NULL AND lower(c.name)=lower(c.ats_slug)
              AND b.company_name IS NOT NULL AND trim(b.company_name)!=''
        """).fetchone()[0]
        n2 = con.execute("""
            SELECT count(DISTINCT c.id) FROM company c
            JOIN job j ON j.company_id=c.id
            WHERE c.country IS NULL AND j.country IS NOT NULL AND j.country!='US'
        """).fetchone()[0]
        print(f"  company names to update:   {n1}")
        print(f"  company countries to set:  {n2}")

        print("\nPhase 4 — would backfill job.country / mark US filtered")
        n1 = con.execute("""
            SELECT count(*) FROM job j JOIN company c ON c.id=j.company_id
            WHERE j.country IS NULL AND j.status NOT IN ('filtered_out','rejected_low_score')
              AND c.country IS NOT NULL
        """).fetchone()[0]
        n2 = con.execute("""
            SELECT count(*) FROM job WHERE country='US'
              AND status IN ('discovered','queued')
        """).fetchone()[0]
        print(f"  job.country backfills:  {n1}")
        print(f"  US jobs to filter_out:  {n2}")

        print("\nPhase 5 — would retire unreachable aggregator jobs")
        n5 = con.execute(
            "SELECT count(*) FROM job WHERE status='queued' AND ats_type IS NULL "
            "AND source IN ('adzuna','jooble')"
        ).fetchone()[0]
        print(f"  queued adzuna/jooble to filtered_out:  {n5}")
        print("\nRe-run with --apply to execute.\n")
        return

    # --- Apply ---
    with con:
        def run_sql_block(block: str) -> None:
            """Execute a block of SQL statements, skipping blank lines and comments."""
            import re
            # Strip line comments then split on ; at end of non-comment lines
            stmts = re.split(r";\s*\n", block)
            for stmt in stmts:
                # Remove full-line comments and blank lines
                cleaned = "\n".join(
                    line for line in stmt.splitlines()
                    if line.strip() and not line.strip().startswith("--")
                ).strip()
                if cleaned:
                    con.execute(cleaned)

        # Phase 1
        run_sql_block(PHASE1)
        print("Phase 1 done: orphan/test rows deleted")

        # Phase 2
        run_sql_block(PHASE2)
        print("Phase 2 done: values standardised")

        # Phase 3
        counts3 = _enrich_companies(con)
        print(f"Phase 3 done: {counts3}")

        # Phase 4
        run_sql_block(PHASE4)
        print("Phase 4 done: job.country backfilled / US jobs filtered")

        # Phase 5
        run_sql_block(PHASE5)
        print("Phase 5 done: unreachable aggregator jobs retired")

    con.execute("PRAGMA wal_checkpoint(FULL)")
    after = snapshot(con)
    print_diff(before, after)


if __name__ == "__main__":
    main()
