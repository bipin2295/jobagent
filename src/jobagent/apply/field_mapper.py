"""Form field mapping: fill standard ATS fields from the answer_bank.

Strategy (applied in order until one works):
1. get_by_label(pattern, exact=False)
2. get_by_placeholder(pattern, exact=False)
3. get_by_role with accessible name
4. CSS fallback for well-known field IDs

This module is intentionally ATS-agnostic; individual ATS handlers call
:func:`fill_all_common_fields` and then apply their own overrides.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import Page

from jobagent.profile.schema import MasterProfile

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Field pattern registry
# ---------------------------------------------------------------------------

#: (label_or_placeholder_text, answer_dict_key) pairs tried in order.
#: The first pattern that resolves to a visible element wins. A third, optional
#: element marks the pattern as an EXACT label match (see ``exact_label``
#: below) — needed for "Name", which as a substring would also match "First
#: Name" / "Last Name" and overwrite an already-filled field.
COMMON_FIELD_PATTERNS: list[tuple[str, str] | tuple[str, str, bool]] = [
    ("First Name", "first_name"),
    ("First name", "first_name"),
    ("Given Name", "first_name"),
    ("Last Name", "last_name"),
    ("Last name", "last_name"),
    ("Family Name", "last_name"),
    ("Surname", "last_name"),
    ("Full Name", "full_name"),
    ("Your name", "full_name"),
    # Ashby and some single-field forms label the full-name input just
    # "Name". Must be an EXACT match: as a substring it would also match
    # "First Name" / "Last Name" and silently overwrite them.
    ("Name", "full_name", True),
    ("Email", "email"),
    ("Email Address", "email"),
    ("Phone", "phone"),
    ("Phone Number", "phone"),
    ("Mobile", "phone"),
    ("LinkedIn", "linkedin"),
    ("LinkedIn Profile", "linkedin"),
    ("LinkedIn URL", "linkedin"),
    ("City", "city"),
    ("Location", "city"),
    ("Current Location", "city"),
    ("How did you hear", "how_did_you_hear"),
    ("How did you find", "how_did_you_hear"),
    ("Where did you hear", "how_did_you_hear"),
    # Cover letter is handled separately via fill_all_common_fields
]

# ---------------------------------------------------------------------------
# Field value builder
# ---------------------------------------------------------------------------


def build_field_values(
    profile: MasterProfile,
    job: Any,
    answers_json: str | None = None,
) -> dict[str, str]:
    """Merge ``answers.json`` with profile-derived fields.

    Parameters
    ----------
    profile:
        The loaded :class:`~jobagent.profile.schema.MasterProfile`.
    job:
        The ``Job`` ORM row (used for context-sensitive answers).
    answers_json:
        Raw JSON string from ``Application.answers_json`` or the
        ``answers.json`` file inside the package directory.  May be
        ``None`` or empty.

    Returns
    -------
    dict[str, str]
        Merged field values keyed by the answer-key strings used in
        ``COMMON_FIELD_PATTERNS``.
    """
    values: dict[str, str] = {}

    # 1. Load the LLM-generated answers from the package
    if answers_json:
        try:
            raw = json.loads(answers_json)
            if isinstance(raw, list):
                # list[{"question": ..., "answer": ...}] format
                for item in raw:
                    if isinstance(item, dict):
                        key = item.get("key") or item.get("question", "")
                        answer = item.get("answer", "")
                        if key and answer:
                            values[str(key)] = str(answer)
            elif isinstance(raw, dict):
                values.update({str(k): str(v) for k, v in raw.items()})
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("field_mapper.answers_parse_error", error=str(exc))

    # 2. Profile-derived fields (overwrite anything from answers_json so the
    #    contact details are always accurate)
    contact = profile.contact
    name_parts = contact.name.strip().split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    values.update(
        {
            "first_name": first_name,
            "last_name": last_name,
            "full_name": contact.name,
            "email": contact.email,
            "phone": contact.phone,
            "city": contact.location.city,
        }
    )

    # 3. LinkedIn — from the profile only (no hardcoded personal URL)
    values.setdefault("linkedin", contact.linkedin or "")

    # 4. Current company — most recent experience entry
    if profile.experience:
        most_recent = profile.experience[0]
        values.setdefault("current_company", most_recent.company)
    else:
        values.setdefault("current_company", "")

    # 5. Reasonable defaults for universal questions
    values.setdefault("how_did_you_hear", "LinkedIn")

    return values


# ---------------------------------------------------------------------------
# Single-field helpers
# ---------------------------------------------------------------------------


async def fill_text_field(
    page: Page,
    label_text: str,
    value: str,
    timeout_ms: int = 3_000,
    exact: bool = False,
) -> bool:
    """Try multiple locator strategies to fill a text input.

    *exact* forces an exact (non-substring) label match. Needed for generic
    labels like "Name" that would otherwise also match "First Name" / "Last
    Name" and overwrite a field that was already filled correctly.

    Returns ``True`` if the field was successfully filled.
    """
    if not value:
        return False

    # Strategy 1: by label
    try:
        loc = page.get_by_label(label_text, exact=exact)
        if await loc.count() > 0:
            target = loc.first
            tag = await target.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("input", "textarea"):
                await target.fill(value, timeout=timeout_ms)
                log.debug(
                    "field_mapper.filled_by_label",
                    label=label_text,
                    strategy="label",
                )
                return True
    except Exception:
        pass

    # Strategies 2-4 (placeholder/role/CSS) all do substring matching and
    # would reintroduce the "Name" vs "First Name" collision — skip them
    # entirely in exact mode. If the exact label lookup above found nothing,
    # there is genuinely no such field on this form.
    if exact:
        log.debug("field_mapper.exact_field_not_found", label=label_text)
        return False

    # Strategy 2: by placeholder
    try:
        loc = page.get_by_placeholder(label_text, exact=False)
        if await loc.count() > 0:
            await loc.first.fill(value, timeout=timeout_ms)
            log.debug(
                "field_mapper.filled_by_placeholder",
                label=label_text,
                strategy="placeholder",
            )
            return True
    except Exception:
        pass

    # Strategy 3: by role (textbox) with accessible name
    try:
        loc = page.get_by_role("textbox", name=label_text)
        if await loc.count() > 0:
            await loc.first.fill(value, timeout=timeout_ms)
            log.debug(
                "field_mapper.filled_by_role",
                label=label_text,
                strategy="role",
            )
            return True
    except Exception:
        pass

    # Strategy 4: CSS — look for an input whose id or name matches the
    # label text (case-insensitive, spaces → dashes/underscores)
    try:
        slug = re.sub(r"\s+", "_", label_text.lower())
        slug_dash = slug.replace("_", "-")
        for attr_val in (slug, slug_dash):
            loc = page.locator(
                f'input[id*="{attr_val}" i], input[name*="{attr_val}" i], '
                f'textarea[id*="{attr_val}" i], textarea[name*="{attr_val}" i]'
            )
            if await loc.count() > 0:
                await loc.first.fill(value, timeout=timeout_ms)
                log.debug(
                    "field_mapper.filled_by_css",
                    label=label_text,
                    strategy="css",
                )
                return True
    except Exception:
        pass

    log.debug("field_mapper.field_not_found", label=label_text)
    return False


async def fill_select_field(
    page: Page,
    label_text: str,
    value: str,
    timeout_ms: int = 3_000,
) -> bool:
    """Select an option in a ``<select>`` element identified by *label_text*.

    Tries ``select_option(label=value)`` first, then ``select_option(value=value)``.
    Returns ``True`` if selection succeeded.
    """
    if not value:
        return False

    # Locate the <select> element
    loc = None
    for strategy_loc in [
        page.get_by_label(label_text, exact=False),
        page.locator(f'select[aria-label*="{label_text}" i]'),
    ]:
        try:
            if await strategy_loc.count() > 0:
                loc = strategy_loc.first
                break
        except Exception:
            pass

    if loc is None:
        log.debug("field_mapper.select_not_found", label=label_text)
        return False

    # Try by visible label text first, then by value attribute
    for kwargs in ({"label": value}, {"value": value}):
        try:
            await loc.select_option(timeout=timeout_ms, **kwargs)
            log.debug(
                "field_mapper.select_filled",
                label=label_text,
                value=value,
                strategy=list(kwargs.keys())[0],
            )
            return True
        except Exception:
            pass

    log.debug(
        "field_mapper.select_option_not_found",
        label=label_text,
        value=value,
    )
    return False


async def upload_file(
    page: Page,
    label_text: str,
    file_path: Path,
    timeout_ms: int = 5_000,
) -> bool:
    """Find a file input and set it to *file_path*.

    Tries label-based lookup first, then falls back to the first
    ``<input type="file">`` on the page.

    Returns ``True`` if the file was attached successfully.
    """
    if not file_path.exists():
        log.warning("field_mapper.upload_file_missing", path=str(file_path))
        return False

    # Strategy 1: input associated with a matching label
    try:
        loc = page.get_by_label(label_text, exact=False)
        if await loc.count() > 0:
            # Confirm it is a file input
            tag = await loc.first.evaluate("el => el.tagName.toLowerCase()")
            input_type = await loc.first.evaluate(
                "el => el.getAttribute('type') || ''"
            )
            if tag == "input" and input_type.lower() == "file":
                await loc.first.set_input_files(str(file_path), timeout=timeout_ms)
                log.info(
                    "field_mapper.file_uploaded_by_label",
                    label=label_text,
                    path=str(file_path),
                )
                return True
    except Exception:
        pass

    # Strategy 2: first file input on the page
    try:
        file_input = page.locator('input[type="file"]')
        if await file_input.count() > 0:
            await file_input.first.set_input_files(str(file_path), timeout=timeout_ms)
            log.info(
                "field_mapper.file_uploaded_fallback",
                path=str(file_path),
            )
            return True
    except Exception:
        pass

    log.warning("field_mapper.upload_failed", label=label_text, path=str(file_path))
    return False


# ---------------------------------------------------------------------------
# Composite helpers
# ---------------------------------------------------------------------------


async def fill_all_common_fields(
    page: Page,
    field_values: dict[str, str],
    pdf_path: Path | None = None,
) -> dict[str, bool]:
    """Fill all registered ``COMMON_FIELD_PATTERNS`` fields and optionally
    attach a resume PDF.

    Parameters
    ----------
    page:
        Active Playwright page with the ATS form loaded.
    field_values:
        Mapping of answer-key → value as returned by :func:`build_field_values`.
    pdf_path:
        Path to the tailored resume PDF.  When provided, the function
        attempts to upload it via any ``<input type="file">`` on the page.

    Returns
    -------
    dict[str, bool]
        ``{answer_key: was_filled}`` for every field attempted.
    """
    results: dict[str, bool] = {}

    for pattern in COMMON_FIELD_PATTERNS:
        label_text, answer_key = pattern[0], pattern[1]
        exact = pattern[2] if len(pattern) > 2 else False
        if answer_key == "cover_letter_placeholder":
            # Handled by individual ATS handlers
            continue

        value = field_values.get(answer_key, "")
        if not value:
            log.debug(
                "field_mapper.skipping_empty_field",
                label=label_text,
                key=answer_key,
            )
            results[answer_key] = False
            continue

        # Avoid duplicate attempts for the same answer_key
        if answer_key in results and results[answer_key]:
            continue

        filled = await fill_text_field(page, label_text, value, exact=exact)
        results[answer_key] = results.get(answer_key, False) or filled

    # Upload resume PDF
    if pdf_path is not None:
        uploaded = await upload_file(page, "Resume", pdf_path)
        if not uploaded:
            # Try a CV-labelled input
            uploaded = await upload_file(page, "CV", pdf_path)
        results["resume"] = uploaded

    # Log any critical fields that were not filled
    critical_keys = {"first_name", "last_name", "email"}
    for key in critical_keys:
        if not results.get(key):
            log.debug(
                "field_mapper.critical_field_unfilled",
                key=key,
            )

    return results


async def handle_checkboxes(page: Page) -> None:
    """Auto-manage checkboxes on ATS forms.

    - Required/consent checkboxes (privacy policy, terms of service,
      GDPR consent) are checked if not already checked.
    - Marketing/newsletter opt-in checkboxes are explicitly left unchecked
      (or unchecked if they are pre-checked).

    This function is best-effort; it will not raise on failure.
    """
    _MARKETING_KEYWORDS = frozenset(
        [
            "newsletter",
            "marketing",
            "promotional",
            "promotions",
            "updates",
            "share.*profile",
            "partner",
            "third.party",
            "receive.*email",
            "send.*me",
            "opt.in",
        ]
    )

    _CONSENT_KEYWORDS = frozenset(
        [
            "privacy",
            "terms",
            "gdpr",
            "consent",
            "agree",
            "accept",
            "policy",
            "authorise",
            "authorize",
        ]
    )

    try:
        checkboxes = page.locator('input[type="checkbox"]')
        count = await checkboxes.count()

        for i in range(count):
            checkbox = checkboxes.nth(i)
            try:
                # Read associated label text via JS
                label: str = await checkbox.evaluate(
                    """el => {
                        // 1. Closest wrapping <label>
                        const wrap = el.closest('label');
                        if (wrap) return wrap.textContent || '';
                        // 2. <label for="id">
                        if (el.id) {
                            const lbl = document.querySelector(`label[for='${el.id}']`);
                            if (lbl) return lbl.textContent || '';
                        }
                        // 3. aria-label attribute
                        return el.getAttribute('aria-label') || '';
                    }"""
                )
                label_lower = label.lower().strip()

                is_marketing = any(
                    re.search(pattern, label_lower)
                    for pattern in _MARKETING_KEYWORDS
                )
                is_consent = any(
                    kw in label_lower for kw in _CONSENT_KEYWORDS
                )

                if is_marketing:
                    # Always opt out of marketing
                    if await checkbox.is_checked():
                        await checkbox.uncheck()
                        log.debug(
                            "field_mapper.checkbox_unchecked_marketing",
                            label=label[:80],
                        )
                elif is_consent or not label_lower:
                    # Consent checkboxes (or unlabelled ones) → ensure checked
                    if not await checkbox.is_checked():
                        await checkbox.check()
                        log.debug(
                            "field_mapper.checkbox_checked_consent",
                            label=label[:80],
                        )
            except Exception as exc:
                log.debug(
                    "field_mapper.checkbox_skip_error",
                    index=i,
                    error=str(exc),
                )

    except Exception as exc:
        log.warning("field_mapper.handle_checkboxes_failed", error=str(exc))
