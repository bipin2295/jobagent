"""LLM cost tracking – log every call and surface today's spend."""

from __future__ import annotations

import time
from datetime import datetime, timezone

# Simple in-process accumulator so we can call today_cost() without a DB query
# during a run.
_session_cost: float = 0.0
_session_tokens_in: int = 0
_session_tokens_out: int = 0


def log_llm_call(
    task: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    cached: bool = False,
    prompt_version: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Record an LLM call in the DB and update the session accumulator."""
    global _session_cost, _session_tokens_in, _session_tokens_out

    cost = _estimate_cost(model, tokens_in, tokens_out, cached)
    _session_cost += cost
    _session_tokens_in += tokens_in
    _session_tokens_out += tokens_out

    try:
        from jobagent.db.repo import LlmCallRepo  # noqa: PLC0415

        LlmCallRepo.insert(
            {
                "task": task,
                "model": model,
                "prompt_version": prompt_version or "",
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost,
                "latency_ms": latency_ms,
                "cached": cached,
                "trace_id": trace_id,
                "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception:  # noqa: BLE001
        # Never crash because cost logging failed
        pass


def today_cost() -> float:
    """Return today's LLM spend from the DB (USD)."""
    try:
        from jobagent.db.repo import LlmCallRepo  # noqa: PLC0415

        return LlmCallRepo.today_cost()
    except Exception:  # noqa: BLE001
        return _session_cost


def session_stats() -> dict[str, float | int]:
    return {
        "cost_usd": round(_session_cost, 6),
        "tokens_in": _session_tokens_in,
        "tokens_out": _session_tokens_out,
    }


# ── Rough cost estimates (update as models change) ───────────────────────────
# Prices per 1M tokens, in USD.
_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini":                (0.15, 0.60),
    "gpt-4o":                     (2.50, 10.00),
    "gpt-4.1":                    (2.00, 8.00),
    "gpt-4.1-mini":               (0.10, 0.40),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-haiku-20240307":    (0.25, 1.25),
    "gemini-1.5-flash":           (0.075, 0.30),
    "gemini-1.5-pro":             (1.25, 5.00),
}
_DEFAULT_PRICE = (1.00, 3.00)  # fallback if model unknown


def _estimate_cost(model: str, tokens_in: int, tokens_out: int, cached: bool) -> float:
    # Match on prefix for versioned model names
    price_in, price_out = _DEFAULT_PRICE
    for key, prices in _PRICES.items():
        if model.startswith(key):
            price_in, price_out = prices
            break
    if cached:
        price_in *= 0.5  # cached input tokens are typically half price
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000
