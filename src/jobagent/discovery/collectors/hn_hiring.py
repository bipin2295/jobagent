"""Hacker News 'Who is Hiring' thread collector.

Uses the Algolia HN Search API to find the latest monthly hiring thread and
parse top-level comments that mention relevant titles or technologies.

No authentication required; the Algolia HN API is public.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"

# Always include comments that mention any of these tech keywords regardless of
# whether they appear in search_cfg.titles.
_TECH_KEYWORDS = frozenset(
    [
        "data",
        "sql",
        "python",
        "analytics",
        "snowflake",
        "dbt",
        "airflow",
        "databricks",
        "bigquery",
        "spark",
        "engineer",
        "pipeline",
    ]
)

# Heuristics to skip meta / question comments.  Checked after lowercasing and
# stripping leading whitespace.
_SKIP_PREFIXES = (
    "is anyone",
    "any tips",
    "any advice",
    "has anyone",
    "what do you",
    "can anyone",
    "does anyone",
    "looking for a job",
    "seeking",
)

_HIT_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace to plain text."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#x27;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Relevance filter
# ---------------------------------------------------------------------------

def _is_relevant(plain_text: str, search_titles: list[str]) -> bool:
    """Return True when the comment is likely a job posting we care about."""
    lower = plain_text.lower()
    stripped = lower.lstrip()

    # Skip question / meta comments
    for prefix in _SKIP_PREFIXES:
        if stripped.startswith(prefix):
            return False

    # Match individual words from each title (skip very short words)
    for title in search_titles:
        for word in title.lower().split():
            if len(word) > 3 and word in lower:
                return True

    # Match any hard-coded tech keyword
    for kw in _TECH_KEYWORDS:
        if kw in lower:
            return True

    return False


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class HNHiringCollector(CollectorBase):
    source = "hn_hiring"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        titles = search_cfg.get("titles", [])

        async with httpx.AsyncClient(timeout=20.0) as client:
            thread_id = await self._get_latest_thread_id(client)
            if not thread_id:
                log.warning("hn_hiring.no_thread", reason="could not find hiring thread")
                return []

            log.info("hn_hiring.thread_found", thread_id=thread_id)
            comments = await self._get_all_comments(client, thread_id)

        log.info("hn_hiring.comments_fetched", count=len(comments))

        jobs: list[RawJob] = []
        for hit in comments:
            comment_html: str = hit.get("comment_text") or ""
            if not comment_html:
                continue

            plain = _strip_html(comment_html)
            if not _is_relevant(plain, titles):
                continue

            try:
                job = _parse_comment(hit, comment_html, plain)
                if job is not None:
                    jobs.append(job)
            except Exception as exc:
                log.warning(
                    "hn_hiring.parse_error",
                    hit_id=hit.get("objectID"),
                    error=str(exc),
                )

        log.info("hn_hiring.done", thread_id=thread_id, jobs=len(jobs))
        return jobs

    # ── Private helpers ──────────────────────────────────────────────────────

    async def _get_latest_thread_id(
        self, client: httpx.AsyncClient
    ) -> str | None:
        """Find the objectID of the most recent 'Who is Hiring' thread."""
        url = (
            f"{_ALGOLIA_BASE}/search"
            "?query=Ask+HN+Who+is+hiring"
            "&tags=ask_hn,author_whoishiring"
            "&hitsPerPage=1"
        )
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", [])
            if hits:
                return str(hits[0]["objectID"])
        except Exception as exc:
            log.warning(
                "hn_hiring.thread_lookup_failed",
                error=str(exc),
            )
        return None

    async def _get_all_comments(
        self,
        client: httpx.AsyncClient,
        thread_id: str,
    ) -> list[dict[str, Any]]:
        """Paginate through all top-level comments in the given thread."""
        all_hits: list[dict[str, Any]] = []
        page = 0
        num_pages = 1  # will be updated after the first response

        while page < num_pages:
            url = (
                f"{_ALGOLIA_BASE}/search"
                f"?tags=comment,story_{thread_id}"
                f"&hitsPerPage={_HIT_PAGE_SIZE}"
                f"&page={page}"
            )
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning(
                    "hn_hiring.comments_page_failed",
                    thread_id=thread_id,
                    page=page,
                    error=str(exc),
                )
                break

            hits: list[dict[str, Any]] = data.get("hits", [])
            all_hits.extend(hits)

            num_pages = data.get("nbPages", 1)
            page += 1

        return all_hits


# ---------------------------------------------------------------------------
# Comment parser
# ---------------------------------------------------------------------------

def _parse_comment(
    hit: dict[str, Any],
    comment_html: str,
    plain_text: str,
) -> RawJob | None:
    object_id: str = str(hit.get("objectID", ""))
    if not object_id:
        return None

    item_url = f"https://news.ycombinator.com/item?id={object_id}"

    # ----- Company + location from first pipe-delimited line ----------------
    # HN hiring comments conventionally start with:
    #   "Company | Location | (Remote/Onsite) | Employment type | ..."
    # We use the plain-text version of the first non-empty line.
    lines = [ln.strip() for ln in plain_text.split("\n") if ln.strip()]
    first_line = lines[0] if lines else plain_text[:200]

    parts = [p.strip() for p in first_line.split("|")]
    company: str = parts[0][:100] if parts else "Unknown"
    location_raw: str | None = parts[1][:100] if len(parts) > 1 else None

    # ----- Remote detection -------------------------------------------------
    lower = plain_text.lower()
    remote_type: str | None = None
    if "remote" in lower:
        if "no remote" in lower or "onsite only" in lower or "not remote" in lower:
            remote_type = None
        elif "hybrid" in lower:
            remote_type = "hybrid"
        else:
            remote_type = "remote"

    # ----- Date -------------------------------------------------------------
    posted_at: datetime | None = None
    if created := hit.get("created_at"):
        try:
            posted_at = datetime.fromisoformat(
                str(created).replace("Z", "+00:00")
            )
        except ValueError:
            pass

    # ----- Title ------------------------------------------------------------
    # Use the story title as a fallback label; we don't have a job title.
    story_title: str = hit.get("story_title") or f"[HN] {company}"

    return RawJob(
        source="hn_hiring",
        source_job_id=object_id,
        url=item_url,
        apply_url=item_url,
        company=company,
        title=story_title[:200],
        location_raw=location_raw,
        remote_type=remote_type,
        description_html=comment_html,
        description_md=plain_text,
        posted_at=posted_at,
        extra={
            "author": hit.get("author"),
            "story_id": str(hit.get("story_id", "")),
            "first_line": first_line[:200],
        },
    )
