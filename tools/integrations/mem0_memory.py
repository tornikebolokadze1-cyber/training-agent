"""Mem0 long-term memory wrapper for the WhatsApp advisor (მრჩეველი).

Provides a thin, gracefully-degrading layer over the ``mem0ai`` SDK so that
the WhatsApp assistant can store and retrieve per-user conversation memories.

Configuration is read exclusively from environment variables:

Vector store (required for Mem0 to be enabled):
    QDRANT_URL          — Qdrant Cloud endpoint URL
    QDRANT_API_KEY      — Qdrant Cloud API key

Graph store (required for Mem0 to be enabled):
    NEO4J_URL           — Neo4j AuraDB connection URI  (e.g. neo4j+s://...)
    NEO4J_USERNAME      — Neo4j username
    NEO4J_PASSWORD      — Neo4j password

LLM (optional — defaults to Anthropic claude-sonnet-4-6):
    MEM0_LLM_PROVIDER   — e.g. "anthropic" (default)
    MEM0_LLM_MODEL      — e.g. "claude-sonnet-4-6" (default)
    ANTHROPIC_API_KEY   — required when provider is "anthropic"

Embedder (optional — defaults to Gemini gemini-embedding-001):
    MEM0_EMBEDDER_PROVIDER — e.g. "gemini" (default)
    MEM0_EMBEDDER_MODEL    — e.g. "models/gemini-embedding-001" (default)
    GEMINI_API_KEY         — required when provider is "gemini"

Graceful degradation:
    When any required variable is absent, ``is_enabled()`` returns False and
    every public function returns an empty result without raising. A single
    WARNING is logged per process to avoid log spam.

Lazy initialisation:
    The Mem0 ``Memory`` client is not instantiated until the first call to an
    API function, so importing this module never fails even when env vars are
    missing.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required environment variables
# ---------------------------------------------------------------------------

_REQUIRED_VARS: tuple[str, ...] = (
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "NEO4J_URL",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
)

# ---------------------------------------------------------------------------
# Lazy singleton state
# ---------------------------------------------------------------------------

_client: Any = None          # mem0.Memory instance (or None when disabled)
_client_lock = threading.Lock()
_disabled_warned = False     # emit the "mem0 disabled" WARNING only once


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Return True when all required env vars are present and non-empty.

    Does NOT attempt to connect to any external service — purely a config check.

    Returns:
        True if Mem0 can be used; False when any required variable is missing.
    """
    return all(os.environ.get(var, "").strip() for var in _REQUIRED_VARS)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _warn_disabled_once() -> None:
    """Emit a single WARNING that Mem0 is disabled (per process)."""
    global _disabled_warned
    if not _disabled_warned:
        missing = [v for v in _REQUIRED_VARS if not os.environ.get(v, "").strip()]
        logger.warning(
            "Mem0 memory is disabled — missing env vars: %s. "
            "WhatsApp advisor will continue without long-term memory.",
            ", ".join(missing),
        )
        _disabled_warned = True


def _get_client() -> Any | None:
    """Return the lazily-initialised Mem0 Memory client, or None if disabled.

    Thread-safe via lock. Returns None (without raising) when:
    - required env vars are missing (is_enabled() == False), or
    - Mem0 SDK instantiation fails for any reason.

    Returns:
        A ``mem0.Memory`` instance, or None.
    """
    global _client

    if not is_enabled():
        _warn_disabled_once()
        return None

    if _client is not None:
        return _client

    with _client_lock:
        # Double-checked locking — another thread may have initialised by now.
        if _client is not None:
            return _client

        try:
            from mem0 import Memory  # type: ignore[import-untyped]

            llm_provider = os.environ.get("MEM0_LLM_PROVIDER", "anthropic")
            llm_model = os.environ.get("MEM0_LLM_MODEL", "claude-sonnet-4-6")
            embedder_provider = os.environ.get("MEM0_EMBEDDER_PROVIDER", "gemini")
            embedder_model = os.environ.get(
                "MEM0_EMBEDDER_MODEL", "models/gemini-embedding-001"
            )

            config: dict[str, Any] = {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "url": os.environ["QDRANT_URL"],
                        "api_key": os.environ["QDRANT_API_KEY"],
                        "collection_name": "whatsapp_advisor_memory",
                        "embedding_model_dims": 3072,
                    },
                },
                "graph_store": {
                    "provider": "neo4j",
                    "config": {
                        "url": os.environ["NEO4J_URL"],
                        "username": os.environ["NEO4J_USERNAME"],
                        "password": os.environ["NEO4J_PASSWORD"],
                    },
                },
                "llm": {
                    "provider": llm_provider,
                    "config": {"model": llm_model},
                },
                "embedder": {
                    "provider": embedder_provider,
                    "config": {"model": embedder_model},
                },
            }

            _client = Memory.from_config(config)
            logger.info(
                "Mem0 Memory client initialised (llm=%s/%s, embedder=%s/%s, "
                "vector=qdrant, graph=neo4j).",
                llm_provider, llm_model, embedder_provider, embedder_model,
            )

        except Exception as exc:
            logger.error(
                "Failed to initialise Mem0 Memory client: %s — "
                "long-term memory disabled for this process.",
                exc,
            )
            _client = None

    return _client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_memory(
    user_id: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store a message or interaction as a memory for the given user.

    Args:
        user_id: Unique identifier for the WhatsApp user (phone number or chat ID).
        text:    The text to memorise (e.g. a user question or bot answer).
        metadata: Optional key-value metadata attached to the memory record.

    Returns:
        The Mem0 result dict on success; empty dict ``{}`` on failure or when
        Mem0 is disabled. Never raises.
    """
    client = _get_client()
    if client is None:
        return {}

    try:
        messages = [{"role": "user", "content": text}]
        kwargs: dict[str, Any] = {"user_id": user_id}
        if metadata:
            kwargs["metadata"] = metadata

        result = client.add(messages, **kwargs)
        logger.debug("Mem0 add_memory: user=%s text_len=%d", user_id, len(text))
        # Mem0 may return a list or a dict depending on SDK version.
        if isinstance(result, list):
            return {"results": result}
        return result if isinstance(result, dict) else {}

    except Exception as exc:
        logger.error("Mem0 add_memory failed for user %s: %s", user_id, exc)
        return {}


def search_memory(
    user_id: str,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Semantic search over a user's stored memories.

    Args:
        user_id: Unique identifier for the WhatsApp user.
        query:   Natural-language query (e.g. "რა ვკითხე გუშინ?").
        limit:   Maximum number of memories to return (default 5).

    Returns:
        List of memory dicts (each has at least a ``memory`` key with the
        stored text). Returns an empty list on failure or when Mem0 is
        disabled. Never raises.
    """
    client = _get_client()
    if client is None:
        return []

    if not query.strip():
        logger.debug("search_memory called with empty query — returning [].")
        return []

    try:
        results = client.search(query, user_id=user_id, limit=limit)
        logger.debug(
            "Mem0 search_memory: user=%s query='%s...' results=%d",
            user_id, query[:60], len(results) if isinstance(results, list) else 0,
        )
        if isinstance(results, list):
            return results
        # Some SDK versions wrap results in a dict.
        if isinstance(results, dict):
            return results.get("results", [])
        return []

    except Exception as exc:
        logger.error("Mem0 search_memory failed for user %s: %s", user_id, exc)
        return []


def get_all(
    user_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch the most recent memories stored for a user (no semantic filtering).

    Args:
        user_id: Unique identifier for the WhatsApp user.
        limit:   Maximum number of memories to return (default 50).

    Returns:
        List of memory dicts. Returns an empty list on failure or when Mem0
        is disabled. Never raises.
    """
    client = _get_client()
    if client is None:
        return []

    try:
        results = client.get_all(user_id=user_id)
        logger.debug(
            "Mem0 get_all: user=%s raw_count=%s",
            user_id, len(results) if isinstance(results, list) else "?",
        )
        if isinstance(results, list):
            return results[:limit]
        if isinstance(results, dict):
            return results.get("results", [])[:limit]
        return []

    except Exception as exc:
        logger.error("Mem0 get_all failed for user %s: %s", user_id, exc)
        return []


def delete_user(user_id: str) -> None:
    """Wipe all memories for a user (GDPR-style deletion).

    Args:
        user_id: Unique identifier for the WhatsApp user whose memories to
                 delete.

    Returns:
        None. Logs a WARNING on failure but never raises.
    """
    client = _get_client()
    if client is None:
        return

    try:
        client.delete_all(user_id=user_id)
        logger.info("Mem0 delete_user: wiped all memories for user=%s", user_id)

    except Exception as exc:
        logger.warning("Mem0 delete_user failed for user %s: %s", user_id, exc)
