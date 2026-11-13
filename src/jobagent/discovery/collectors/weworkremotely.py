"""WeWorkRemotely RSS feed collector.

Feeds:
- ``https://weworkremotely.com/remote-jobs.rss``
- ``https://weworkremotely.com/categories/remote-programming-jobs.rss``

``feedparser`` is synchronous, so each feed is parsed in the default
thread-pool executor to avoid blocking the event loop.  Company and job
title are extracted from the RSS ``<title>`` field which typically follows
the pattern ``"Company: Job Title at Location"``.  ``fetch()`` never raises.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import structlog

from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_RSS_FEEDS = [
    "https://weworkremotely.com/remote-jobs.rss",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
]

# "Company: Job Title at Location" – the " at Location" suffix is optional
_TITLE_RE = re.compile(
    r"^(?P<company>[^:]+):\s*(?P<title>.+?)(?:\s+at\s+[^|]+)?$",
    re.IGNORECASE,
)


def _split_title(raw: str) -> tuple[str, str]:
    """Return *(company, title)* extracted from a WWR RSS title string."""
    m = _TITLE_RE.match(raw.strip())
    if m:
        return m.group("company").strip(), m.group("title").strip()
    return "Unknown", raw.strip()


def _matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def _parse_feed_sync(rss_url: str) -> list[Any]:
    """Synchronous feedparser call – intended to run in an executor."""
    try:
        import feedparser  # type: ignore[import-untyped]

        parsed = feedparser.parse(rss_url)
        return list(parsed.entries)
    except Exception as exc:
        log.warning("weworkremotely.feed_error", url=rss_url, error=str(exc))
        return []


class WeWorkRemotelyCollector(CollectorBase):
    name = "weworkremotely"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        keywords: list[str] = search_cfg.get("titles", [])
        loop = asyncio.get_event_loop()

        # Run each feed parse in the default thread-pool executor
        feed_batches: list[list[Any]] = await asyncio.gather(
            *[
                loop.run_in_executor(None, _parse_feed_sync, url)
                for url in _RSS_FEEDS
            ]
        )

        seen_ids: set[str] = set()
        results: list[RawJob] = []

        for entries in feed_batches:
            for entry in entries:
                entry_id: str = (
                    getattr(entry, "id", None)
                    or getattr(entry, "link", None)
                    or ""
                )
                if not entry_id or entry_id in seen_ids:
                    continue

                raw_title: str = getattr(entry, "title", "")
                company, title = _split_title(raw_title)

                # Fall back to the RSS <author> field for the company name
                if company == "Unknown":
                    author = getattr(entry, "author", "")
                    if author:
                        company = author

                if not _matches(title, keywords):
                    continue

                try:
                    results.append(_parse_entry(entry, entry_id, company, title))
                    seen_ids.add(entry_id)
                except Exception as exc:
                    log.warning(
                        "weworkremotely.parse_error",
                        entry_id=entry_id,
                        error=str(exc),
                    )

        log.info("weworkremotely.done", total=len(results))
        return results


def _parse_entry(
    entry: Any,
    entry_id: str,
    company: str,
    title: str,
) -> RawJob:
    entry_url: str = getattr(entry, "link", "")

    posted_at: datetime | None = None
    published: str | None = getattr(entry, "published", None)
    if published:
        try:
            posted_at = parsedate_to_datetime(published)
        except Exception:
            pass

    tags: list[str] = [
        t.get("term", "") for t in (getattr(entry, "tags", None) or [])
    ]

    extra: dict[str, Any] = {"tags": tags}

    return RawJob(
        source="weworkremotely",
        source_job_id=entry_id,
        url=entry_url,
        company=company,
        title=title,
        apply_url=entry_url,
        remote_type="remote",
        description_html=getattr(entry, "summary", "") or "",
        posted_at=posted_at,
        extra=extra,
    )
