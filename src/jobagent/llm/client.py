"""Cimpress AI Gateway client (Portkey-based, OpenAI-compatible).

The gateway at https://gateway.ai.cimpress.io/v1 is a Portkey instance.
Auth is via x-portkey-api-key header; the openai.OpenAI api_key is set to
"dummy" because the gateway ignores it in favour of the Portkey header.

Model IDs in config/models.yaml must be the INTERNAL ids from the wellknown
document (e.g. "@Anthropic/eu.anthropic.claude-sonnet-5-5"), not the display
names like "eu.anthropic.claude-sonnet-5-5".
"""

from __future__ import annotations

import os
import time
from typing import Any

import openai

_client: openai.OpenAI | None = None


class ConfigError(RuntimeError):
    """Raised when a required API key or config is missing."""


def get_client() -> openai.OpenAI:
    """Return the cached gateway client. Raises ConfigError if key is missing."""
    global _client
    if _client is None:
        from jobagent.config import settings  # noqa: PLC0415

        if not settings.portkey_api_key:
            raise ConfigError(
                "JOBAGENT_PORTKEY_API_KEY is not set. "
                "Add it to .env  (the key is called CIMPRESS_AI_GATEWAY_KEY in the "
                "gateway docs — it's the same key OpenCode uses)."
            )

        # The gateway uses x-portkey-api-key for auth.
        # api_key="dummy" silences the openai SDK's key-not-set guard while
        # the real auth flows through default_headers.
        _client = openai.OpenAI(
            api_key="dummy",
            base_url=settings.portkey_base_url,
            default_headers={
                "x-portkey-api-key": settings.portkey_api_key,
                "x-portkey-metadata": '{"harness":"jobagent"}',
                # Forward these headers so Valar-routed models work correctly
                "x-portkey-forward-headers": (
                    "x-valar-client-id,x-valar-harness,"
                    "x-valar-completion-window,anthropic-beta"
                ),
                "x-valar-client-id": os.environ.get("USER", "jobagent"),
                "x-valar-harness": "jobagent",
                "x-valar-completion-window": "asap",
            },
        )
    return _client


def complete(
    task: str,
    messages: list[dict[str, str]],
    **override_kwargs: Any,
) -> str:
    """Call the LLM for *task* and return the response content string."""
    from jobagent.config import get_model_config  # noqa: PLC0415
    from jobagent.llm.cost import log_llm_call    # noqa: PLC0415

    cfg = get_model_config(task)
    kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "max_tokens": cfg.get("max_tokens", 1000),
        "temperature": cfg.get("temperature", 0.1),
        **override_kwargs,
    }

    client = get_client()
    t0 = time.monotonic()
    response = client.chat.completions.create(messages=messages, **kwargs)  # type: ignore[arg-type]
    latency_ms = int((time.monotonic() - t0) * 1000)

    usage = response.usage
    content = response.choices[0].message.content or ""

    log_llm_call(
        task=task,
        model=kwargs["model"],
        tokens_in=usage.prompt_tokens if usage else 0,
        tokens_out=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        cached=False,
    )

    return content
