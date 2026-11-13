"""LLM layer – Portkey-routed OpenAI-compatible client."""

from .client import ConfigError, get_client
from .cost import log_llm_call, today_cost
from .embeddings import embed_text
from .structured import structured_complete

__all__ = [
    "ConfigError",
    "get_client",
    "log_llm_call",
    "today_cost",
    "embed_text",
    "structured_complete",
]
