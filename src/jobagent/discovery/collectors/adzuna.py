"""Adzuna API v1 collector.

Docs: https://developer.adzuna.com/activedocs
Free tier: app_id + app_key, up to 25 req/s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog

from jobagent.config import settings
from jobagent.discovery.base import CollectorBase, RawJob

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.adzuna.com/v1/api/jobs"

# Map search.yaml ISO country code → Adzuna country code.
# Only countries supported by Adzuna are listed here; others are silently skipped.
_SUPPORTED_ADZUNA: dict[str, str] = {
    # US intentionally removed — H-1B lottery makes it impractical for the candidate.
    # Even if Adzuna has US listings, we'd waste API quota collecting jobs that
    # the country_allowlist filter in filters.py would immediately reject.
    "GB": "gb",
    "UK": "gb",   # search.yaml uses "UK", Adzuna uses "gb"
    "AU": "au",
    "CA": "ca",   # Canada — Global Talent Stream, easier than US H-1B
    "DE": "de",
    "FR": "fr",
    "IN": "in",
    "NL": "nl",
    "NZ": "nz",
    "PL": "pl",
    "SG": "sg",
}

# Reverse map: Adzuna code → ISO alpha-2 we store in RawJob.country
_ADZUNA_TO_ISO: dict[str, str] = {
    "us": "US",
    "gb": "GB",
    "au": "AU",
    "br": "BR",
    "ca": "CA",
    "de": "DE",
    "fr": "FR",
    "in": "IN",
    "nl": "NL",
    "nz": "NZ",
    "pl": "PL",
    "ru": "RU",
    "sg": "SG",
    "za": "ZA",
}

# Default local currency per Adzuna country code
_CURRENCY: dict[str, str] = {
    "us": "USD",
    "gb": "GBP",
    "au": "AUD",
    "br": "BRL",
    "ca": "CAD",
    "de": "EUR",
    "fr": "EUR",
    "in": "INR",
    "nl": "EUR",
    "nz": "NZD",
    "pl": "PLN",
    "ru": "RUB",
    "sg": "SGD",
    "za": "ZAR",
}

_RESULTS_PER_PAGE = 50
_MAX_PAGES = 3


def _collect_adzuna_countries(search_cfg: dict[str, Any]) -> list[str]:
    """Extract distinct Adzuna country codes from search_cfg regions."""
    seen: set[str] = set()
    for region_data in search_cfg.get("regions", {}).values():
        for iso in region_data.get("countries", []):
            if not isinstance(iso, str):
                continue  # guard against YAML booleans (e.g. bare NO → False)
            adzuna_code = _SUPPORTED_ADZUNA.get(iso.upper())
            if adzuna_code and adzuna_code not in seen:
                seen.add(adzuna_code)
    if not seen:
        # Sensible fallback when regions are absent
        seen = {"gb", "in", "de", "nl", "au"}
    return sorted(seen)


class AdzunaCollector(CollectorBase):
    name = "adzuna"
    source = "adzuna"

    async def fetch(self, search_cfg: dict[str, Any]) -> list[RawJob]:
        if not settings.adzuna_app_id:
            log.warning(
                "adzuna.skip",
                reason="adzuna_app_id is not configured",
            )
            return []
        if not settings.adzuna_app_key:
            log.warning(
                "adzuna.skip",
                reason="adzuna_app_key is not configured",
            )
            return []

        titles = search_cfg.get("titles", [])[:3]
        countries = _collect_adzuna_countries(search_cfg)

        log.info(
            "adzuna.start",
            titles=titles,
            countries=countries,
        )

        jobs: list[RawJob] = []

        async with httpx.AsyncClient(timeout=20.0) as client:
            for title in titles:
                for country_code in countries:
                    for page in range(1, _MAX_PAGES + 1):
                        batch = await self._fetch_page(
                            client, country_code, title, page
                        )
                        jobs.extend(batch)
                        if len(batch) < _RESULTS_PER_PAGE:
                            # Fewer results than page size → last page
                            break

        log.info("adzuna.done", total=len(jobs))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        country_code: str,
        title: str,
        page: int,
    ) -> list[RawJob]:
        url = f"{_BASE_URL}/{country_code}/search/{page}"
        params: dict[str, Any] = {
            "app_id": settings.adzuna_app_id,
            "app_key": settings.adzuna_app_key,
            "what": title,
            "results_per_page": _RESULTS_PER_PAGE,
            "content-type": "application/json",
        }
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning(
                "adzuna.page_failed",
                country=country_code,
                title=title,
                page=page,
                error=str(exc),
            )
            return []

        results = data.get("results", [])
        currency = _CURRENCY.get(country_code, "")
        country_iso = _ADZUNA_TO_ISO.get(country_code)

        jobs: list[RawJob] = []
        for result in results:
            try:
                job = _parse_result(result, country_iso, currency)
                jobs.append(job)
            except Exception as exc:
                log.warning(
                    "adzuna.parse_error",
                    result_id=result.get("id"),
                    error=str(exc),
                )

        return jobs


def _parse_result(
    result: dict[str, Any],
    country_iso: str | None,
    currency: str,
) -> RawJob:
    location_obj = result.get("location", {})
    location_raw = location_obj.get("display_name", "")

    posted_at: datetime | None = None
    if created := result.get("created"):
        try:
            posted_at = datetime.fromisoformat(
                created.replace("Z", "+00:00")
            )
        except ValueError:
            pass

    salary_min = result.get("salary_min")
    salary_max = result.get("salary_max")

    contract_type = result.get("contract_type")
    contract_time = result.get("contract_time")
    employment_type = contract_type or contract_time

    return RawJob(
        source="adzuna",
        source_job_id=str(result["id"]),
        url=result["redirect_url"],
        apply_url=result["redirect_url"],
        company=result.get("company", {}).get("display_name"),
        title=result.get("title", ""),
        location_raw=location_raw or None,
        country=country_iso,
        salary_min=float(salary_min) if salary_min is not None else None,
        salary_max=float(salary_max) if salary_max is not None else None,
        currency=currency or None,
        employment_type=employment_type,
        description_md=result.get("description") or None,
        posted_at=posted_at,
        extra={
            "contract_type": contract_type,
            "contract_time": contract_time,
            "location_area": location_obj.get("area", []),
        },
    )
