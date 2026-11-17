"""Structured (JSON) completions with Pydantic validation and repair retry."""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from jobagent.llm.client import complete

T = TypeVar("T", bound=BaseModel)

_REPAIR_PROMPT = (
    "The JSON you returned failed validation with this error:\n{error}\n\n"
    "Return ONLY valid JSON that matches the schema. No markdown, no explanation."
)


def structured_complete(
    task: str,
    messages: list[dict[str, str]],
    model: type[T],
    max_repair_attempts: int = 2,
    **kwargs: Any,
) -> T:
    """Call the LLM and parse the response into a Pydantic model.

    On validation failure, sends a repair prompt and retries up to
    *max_repair_attempts* times before raising.
    """
    system_hint = (
        f"Respond ONLY with valid JSON that conforms to this schema:\n"
        f"{json.dumps(model.model_json_schema(), indent=2)}\n"
        "No markdown, no explanation. JSON only."
    )

    # Prepend schema hint to system message if present, else add it
    augmented: list[dict[str, str]] = []
    has_system = any(m.get("role") == "system" for m in messages)
    if has_system:
        for m in messages:
            if m.get("role") == "system":
                augmented.append({"role": "system", "content": m["content"] + "\n\n" + system_hint})
            else:
                augmented.append(m)
    else:
        augmented = [{"role": "system", "content": system_hint}] + list(messages)

    last_error: Exception | None = None
    for attempt in range(max_repair_attempts + 1):
        raw = complete(task, augmented, **kwargs)
        try:
            # Strip markdown fences if present
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```", 2)[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.rsplit("```", 1)[0]
            parsed = json.loads(text)
            return model.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            if attempt < max_repair_attempts:
                repair_msg = {"role": "user", "content": _REPAIR_PROMPT.format(error=str(exc))}
                if raw and raw.strip():
                    augmented = augmented + [{"role": "assistant", "content": raw}, repair_msg]
                else:
                    # Never replay an empty assistant message: some gateways
                    # reject it with an opaque "every message needs non-empty
                    # content" 400, hiding the real cause (usually max_tokens
                    # truncating a reasoning model to no visible output).
                    augmented = augmented + [
                        {
                            "role": "user",
                            "content": "Your previous response was empty. " + repair_msg["content"],
                        }
                    ]

    raise ValueError(
        f"structured_complete failed after {max_repair_attempts + 1} attempts "
        f"for task '{task}': {last_error}"
    )
