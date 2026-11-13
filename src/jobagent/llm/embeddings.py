"""Ollama embedding client with in-memory LRU cache."""

from __future__ import annotations

import hashlib
from functools import lru_cache

import httpx


@lru_cache(maxsize=4096)
def embed_text(text: str) -> list[float]:
    """Return the embedding vector for *text* from local Ollama.

    Cached by content hash so the same text is never embedded twice in a session.
    """
    from jobagent.config import settings  # noqa: PLC0415

    # Normalise whitespace so trivial variations don't miss the cache
    text = " ".join(text.split())

    try:
        resp = httpx.post(
            f"{settings.ollama_base_url}/api/embeddings",
            json={"model": settings.ollama_embed_model, "prompt": text},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Ollama embedding failed (is Ollama running at {settings.ollama_base_url}?): {exc}"
        ) from exc


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return cosine similarity in [0, 1] between two embedding vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
