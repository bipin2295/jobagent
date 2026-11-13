#!/usr/bin/env python3
"""Mine new Greenhouse company slugs from company names already in the DB.

Strategy:
1. Take company names from high-scoring jobs with unknown ATS.
2. Generate candidate slug variants (lowercase, alphanumeric only).
3. Batch-probe boards-api.greenhouse.io — the same free public API our
   collector uses. Zero cost, no authentication, polite rate limiting.
4. Every slug that answers 200 is saved to the ``ats_board`` registry as a
   ``candidate`` (discovered_via="guess"); the next discovery run or
   ``jobagent boards validate`` promotes it to live/empty.  Hits with active
   data-engineering jobs are also printed.

Usage:
    docker compose exec app python scripts/discover_greenhouse_slugs.py
    docker compose exec app python scripts/discover_greenhouse_slugs.py --min-score 68 --batch 1000
    docker compose exec app python scripts/discover_greenhouse_slugs.py --no-save   # report only
"""

import asyncio
import re
from argparse import ArgumentParser

import httpx
import structlog

log = structlog.get_logger(__name__)

DE_KEYWORDS = [
    "data engineer", "analytics engineer",
    "data platform", "data infrastructure", "data architect",
]

US_SIGNALS = [
    "san francisco", "new york", "seattle", "austin", "boston",
    "chicago", "los angeles", "denver", "atlanta", "dallas",
    " us ", "united states",
]


def names_match(board_name: str | None, source_names: set[str]) -> bool:
    """True if the Greenhouse board's company name plausibly is one of *source_names*.

    Guards against generic slug guesses ("london", "talent", "canada") that
    exist on Greenhouse but belong to an unrelated company.
    """
    from jobagent.pipeline.normalise import normalise_company

    def compact(n: str) -> str:
        return re.sub(r"[^a-z0-9]", "", normalise_company(n or ""))

    b = compact(board_name or "")
    if len(b) < 2:
        return False
    for src in source_names:
        s_ = compact(src)
        if not s_:
            continue
        if b == s_:
            return True
        short, long_ = sorted((b, s_), key=len)
        # "costar" ↔ "costargroup", "block" ↔ "blockinc": prefix covering ≥ half
        if len(short) >= 4 and long_.startswith(short) and len(short) / len(long_) >= 0.5:
            return True
    return False


def to_slugs(name: str, norm: str) -> set[str]:
    slugs: set[str] = set()
    words = re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).split()
    if words:
        slugs.add(words[0])
        slugs.add("".join(words[:2]))
        slugs.add("".join(words[:3]))
    if norm:
        first = norm.split()[0] if norm.split() else ""
        slugs.add(re.sub(r"[^a-z0-9]", "", first))
        slugs.add(re.sub(r"[^a-z0-9]", "", norm[:20]))
    return {s for s in slugs if len(s) >= 4}


async def probe(
    session: httpx.AsyncClient, slug: str
) -> tuple[str, int | None, list, str | None]:
    """Return (slug, total_jobs or None if no board, DE jobs, board company name)."""
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
    try:
        r = await session.get(f"{base}/jobs?content=false")
        if r.status_code == 200:
            jobs = r.json().get("jobs", [])
            de = [j for j in jobs if any(
                kw in j.get("title", "").lower() for kw in DE_KEYWORDS
            )]
            name = next((j.get("company_name") for j in jobs if j.get("company_name")), None)
            if name is None:
                rb = await session.get(base)
                if rb.status_code == 200:
                    name = rb.json().get("name")
            return slug, len(jobs), de, name
    except Exception:
        pass
    return slug, None, [], None


def is_us_only(de_jobs: list) -> bool:
    # content=false listings carry location.name (no offices array)
    for job in de_jobs[:3]:
        locs = [(job.get("location") or {}).get("name") or ""]
        locs += [o.get("name") or o.get("location") or "" for o in job.get("offices", [])]
        for loc in (x.lower() for x in locs):
            if loc and not any(sig in loc for sig in US_SIGNALS):
                return False  # found a non-US location
    return True  # all locations US or none listed


async def main(min_score: int = 70, batch_size: int = 500, save: bool = True) -> None:
    from sqlmodel import select

    from jobagent.db.engine import get_session
    from jobagent.db.models import Company, Job
    from jobagent.discovery.company_universe import CompanyUniverse

    with get_session() as s:
        rows = s.exec(
            select(Company.name, Company.name_norm)
            .join(Job, Job.company_id == Company.id)
            .where(Job.score >= min_score)
            .where(Company.ats_type.is_(None))  # SQL IS NULL (not Python `is`)
            .distinct()
        ).all()

    universe = CompanyUniverse()
    universe.ensure_initialized()
    known_slugs = universe.all_slugs("greenhouse")  # any status, incl. dead
    sources: dict[str, set[str]] = {}  # slug guess → company names it came from
    for name, norm in rows:
        for slug in to_slugs(name, norm):
            if slug not in known_slugs:
                sources.setdefault(slug, set()).add(name)
    candidates = sorted(sources)[:batch_size]

    print(f"Probing {len(candidates)} candidate slugs (min_score≥{min_score})…")
    print("(This uses the free public Greenhouse Boards API — no cost, no auth)\n")

    hits = []
    existing: list[str] = []
    mismatched: list[tuple[str, str | None]] = []
    limits = httpx.Limits(max_connections=25)
    async with httpx.AsyncClient(timeout=5, limits=limits) as c:
        for i in range(0, len(candidates), 25):
            batch = candidates[i : i + 25]
            results = await asyncio.gather(*[probe(c, s) for s in batch])
            for slug, total, de_jobs, board_name in results:
                if total is None:
                    continue
                if not names_match(board_name, sources.get(slug, set())):
                    mismatched.append((slug, board_name))
                    continue
                existing.append(slug)
                if de_jobs:
                    hits.append((slug, total, de_jobs))
            await asyncio.sleep(0.15)

    if save and existing:
        added = sum(
            universe.add_board("greenhouse", slug, discovered_via="guess") for slug in existing
        )
        print(f"Saved {added} new Greenhouse boards to ats_board (of {len(existing)} that match).")
        print("Run `jobagent boards validate --ats greenhouse` to promote them.\n")
    if mismatched:
        print(f"Skipped {len(mismatched)} boards that exist but belong to another company:")
        for slug, board_name in mismatched[:20]:
            print(f"    {slug:24} → {board_name}")
        print()

    print(f"Found {len(hits)} companies with active DE/AE jobs:\n")
    us_filtered = []
    for slug, total, de_jobs in sorted(hits, key=lambda x: -len(x[2])):
        flag = " [US?]" if is_us_only(de_jobs) else ""
        offices = {
            o.get("name") or o.get("location", "")
            for j in de_jobs[:3]
            for o in j.get("offices", [])
        }
        print(f"  {slug:28} {len(de_jobs):3} DE jobs{flag}")
        for j in de_jobs[:1]:
            print(f"    -> {j['title'][:60]}")
        if offices:
            print(f"    offices: {', '.join(list(offices)[:4])}")
        if flag:
            us_filtered.append(slug)

    print(f"\n{'─'*60}")
    non_us = [s for s, _, _ in hits if s not in us_filtered]
    if non_us:
        print("\n✓ Non-US companies with DE jobs:")
        for slug in non_us:
            print(f"    {slug}")
    else:
        print("No clearly non-US companies found in this batch.")


if __name__ == "__main__":
    parser = ArgumentParser(description="Discover new Greenhouse company slugs")
    parser.add_argument("--min-score", type=int, default=70,
                        help="Minimum job score to consider a company (default: 70)")
    parser.add_argument("--batch", type=int, default=500,
                        help="Max slug candidates to probe (default: 500)")
    parser.add_argument("--no-save", action="store_true",
                        help="Only report; don't write boards to the registry")
    args = parser.parse_args()
    asyncio.run(main(min_score=args.min_score, batch_size=args.batch, save=not args.no_save))
