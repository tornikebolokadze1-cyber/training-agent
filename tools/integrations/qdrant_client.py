"""Thin Qdrant Cloud client helpers shared across the codebase.

This module provides a single source of truth for:

  * the Qdrant Python client (cached, thread-safe)
  * deterministic vector IDs that match Pinecone's old conventions
  * primitive operations the migration code needs (list IDs by prefix,
    fetch payloads by ID, batched upsert, count by prefix)

Knowledge-indexer-level functionality (embedding, chunking, full lecture
indexing) stays in ``tools.integrations.knowledge_indexer``. This file
only contains low-level Qdrant primitives so non-indexer modules
(``health_monitor``, ``analytics``, ``drive_audit``, ``obsidian_sync``,
the regen script) can stop importing ``pinecone`` directly.

Vector ID strategy
------------------
Pinecone allowed arbitrary string IDs like ``g3_l5_transcript_42``.
Qdrant requires IDs to be UUIDs or unsigned 64-bit integers, so we
hash the legacy string ID into a deterministic UUIDv5 under a fixed
namespace. Two calls with the same string ID always produce the same
UUID — that's what makes the regen script idempotent.

A copy of the original string ID is stored in the payload under the
``legacy_id`` key so prefix-based listing still works (Qdrant filters
on payload, not ID).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from tools.core.config import (
    QDRANT_API_KEY,
    QDRANT_COLLECTION_NAME,
    QDRANT_URL,
)

logger = logging.getLogger(__name__)

# UUIDv5 namespace for converting legacy Pinecone string IDs into deterministic
# UUIDs. Do NOT change this value — it is what guarantees idempotency across
# re-runs of the regen script.
_LEGACY_ID_NAMESPACE = uuid.UUID("c6f7e1a2-8b3d-4e5f-9c0a-1d2e3f4a5b6c")

# Embedding dimension — matches gemini-embedding-001 (3072).
EMBEDDING_DIMENSION = 3072

# Upsert batch size (Qdrant accepts up to a few thousand per call; 100 is the
# same as the old Pinecone limit so behaviour matches the existing indexer).
UPSERT_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Client cache
# ---------------------------------------------------------------------------

_client_cache: Any = None
_client_lock = threading.Lock()


def get_qdrant_client() -> Any:
    """Return a cached Qdrant client, initializing it on first call.

    Raises:
        RuntimeError: If QDRANT_URL or QDRANT_API_KEY is not configured.
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    with _client_lock:
        if _client_cache is not None:
            return _client_cache

        if not QDRANT_URL or not QDRANT_API_KEY:
            raise RuntimeError(
                "Qdrant not configured — set QDRANT_URL and QDRANT_API_KEY in .env"
            )

        from qdrant_client import QdrantClient

        _client_cache = QdrantClient(
            url=QDRANT_URL,
            api_key=QDRANT_API_KEY,
            # Cloud connections are HTTPS; let the client autodetect from URL.
            timeout=30,
        )
        logger.info("Qdrant client initialized for collection '%s'", QDRANT_COLLECTION_NAME)
        return _client_cache


def reset_client_cache() -> None:
    """Test helper — clear the cached client so a new one is built next call."""
    global _client_cache
    with _client_lock:
        _client_cache = None


# ---------------------------------------------------------------------------
# Deterministic ID helpers
# ---------------------------------------------------------------------------


def legacy_id_to_uuid(legacy_id: str) -> str:
    """Map a Pinecone-style string ID to a deterministic UUIDv5.

    Always returns the same UUID for the same input — that is what makes
    re-running the regen script idempotent.

    Args:
        legacy_id: e.g. ``"g3_l5_transcript_42"``.

    Returns:
        UUID string in canonical form.
    """
    return str(uuid.uuid5(_LEGACY_ID_NAMESPACE, legacy_id))


def lecture_vector_legacy_id(
    group: int,
    lecture: int,
    content_type: str,
    chunk_index: int,
) -> str:
    """Build the Pinecone-shaped string ID for a lecture chunk."""
    return f"g{group}_l{lecture}_{content_type}_{chunk_index}"


def scores_backup_legacy_id(group: int, lecture: int) -> str:
    """Build the Pinecone-shaped string ID for the per-lecture scores backup."""
    return f"scores_backup_g{group}_l{lecture}"


# ---------------------------------------------------------------------------
# Health / metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionHealth:
    """Lightweight health report for a Qdrant collection."""

    reachable: bool
    collection_name: str
    points_count: int
    error: str | None = None


def check_collection_health() -> CollectionHealth:
    """Ping the Qdrant cluster and return basic collection stats.

    Used by ``tools.core.health_monitor.check_qdrant``. Never raises —
    the caller wants a structured result either way.
    """
    try:
        client = get_qdrant_client()
        # list_collections() is a cheap call that confirms the cluster is up.
        collections = client.get_collections()
        names = {c.name for c in collections.collections}
        if QDRANT_COLLECTION_NAME not in names:
            return CollectionHealth(
                reachable=True,
                collection_name=QDRANT_COLLECTION_NAME,
                points_count=0,
                error=(
                    f"Collection '{QDRANT_COLLECTION_NAME}' does not exist yet — "
                    "will be created on first upsert."
                ),
            )

        info = client.get_collection(QDRANT_COLLECTION_NAME)
        # points_count is the canonical Qdrant field for vector counts.
        count = getattr(info, "points_count", 0) or 0
        return CollectionHealth(
            reachable=True,
            collection_name=QDRANT_COLLECTION_NAME,
            points_count=count,
        )
    except Exception as exc:
        return CollectionHealth(
            reachable=False,
            collection_name=QDRANT_COLLECTION_NAME,
            points_count=0,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Prefix-based listing (payload filter)
# ---------------------------------------------------------------------------


def _legacy_prefix_filter(prefix: str) -> Any:
    """Build a Qdrant filter that matches points whose legacy_id starts with prefix.

    Qdrant doesn't have a native string-prefix matcher, so we scroll all points
    and filter client-side using ``match_text``-style support is not portable;
    instead we use ``match_any`` over an enumerated set wherever the caller
    knows the full ID set. For pure prefix scans the callers below scroll
    with no filter and check the payload themselves.

    This helper is kept for forward compatibility — if/when Qdrant adds a
    prefix matcher the implementations below can use it without changing
    their public signatures.
    """
    return None  # Sentinel — callers fall back to client-side prefix matching.


def list_legacy_ids_with_prefix(
    prefix: str,
    *,
    limit_per_page: int = 256,
    max_results: int | None = None,
) -> list[str]:
    """Return every legacy string ID stored in the collection that starts with prefix.

    Implementation note: we scroll the collection and filter on the
    ``legacy_id`` payload field client-side because Qdrant's filter DSL
    does not have a string-prefix matcher at the time of writing. To
    keep this bounded, callers pass a narrow prefix (e.g.
    ``"g3_l5_transcript_"``).

    Args:
        prefix: Legacy-ID prefix to match (e.g. ``"g3_l5_"``).
        limit_per_page: Scroll batch size.
        max_results: Optional cap on returned IDs.

    Returns:
        List of legacy ID strings (in scroll order).
    """
    client = get_qdrant_client()
    found: list[str] = []
    offset: Any = None

    while True:
        points, offset = client.scroll(
            collection_name=QDRANT_COLLECTION_NAME,
            limit=limit_per_page,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            payload = p.payload or {}
            legacy = payload.get("legacy_id", "")
            if legacy.startswith(prefix):
                found.append(legacy)
                if max_results is not None and len(found) >= max_results:
                    return found
        if offset is None:
            break

    return found


def count_legacy_ids_with_prefix(prefix: str) -> int:
    """Return the number of points whose legacy_id starts with prefix.

    Convenience wrapper around ``list_legacy_ids_with_prefix`` — does not
    short-circuit because callers want an exact count. For very large
    prefixes this is O(N) but lecture-level prefixes are bounded by chunk
    count per lecture (~100 max).
    """
    return len(list_legacy_ids_with_prefix(prefix))


def lecture_exists_in_collection(
    group: int,
    lecture: int,
    content_type: str | None = None,
) -> bool:
    """Drop-in replacement for ``knowledge_indexer.lecture_exists_in_index``.

    Returns True if any chunk for this (group, lecture[, content_type])
    is already stored in Qdrant.
    """
    if content_type:
        prefix = f"g{group}_l{lecture}_{content_type}_"
    else:
        prefix = f"g{group}_l{lecture}_"
    ids = list_legacy_ids_with_prefix(prefix, max_results=1)
    return bool(ids)


# ---------------------------------------------------------------------------
# Fetch points by legacy ID
# ---------------------------------------------------------------------------


def fetch_by_legacy_ids(legacy_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Retrieve points by their legacy IDs, returning {legacy_id: payload}.

    Args:
        legacy_ids: List of legacy string IDs.

    Returns:
        Dict mapping legacy_id -> payload (only IDs that exist appear in the dict).
    """
    if not legacy_ids:
        return {}

    client = get_qdrant_client()
    uuid_ids = [legacy_id_to_uuid(lid) for lid in legacy_ids]
    # qdrant-client supports retrieve(ids=[...]) returning a list of Records.
    records = client.retrieve(
        collection_name=QDRANT_COLLECTION_NAME,
        ids=uuid_ids,
        with_payload=True,
        with_vectors=False,
    )

    out: dict[str, dict[str, Any]] = {}
    for rec in records:
        payload = rec.payload or {}
        legacy = payload.get("legacy_id")
        if legacy:
            out[legacy] = payload
    return out


# ---------------------------------------------------------------------------
# Collection initialization
# ---------------------------------------------------------------------------


def ensure_collection_exists() -> None:
    """Create the Qdrant collection if it doesn't exist yet.

    Uses cosine distance and the embedding dimension that matches
    ``gemini-embedding-001`` (3072). Idempotent — safe to call from
    multiple processes.
    """
    client = get_qdrant_client()

    try:
        from qdrant_client.http import models as qmodels
    except ImportError:  # pragma: no cover — qdrant-client always ships http models
        from qdrant_client import models as qmodels  # type: ignore[no-redef]

    existing = {c.name for c in client.get_collections().collections}
    if QDRANT_COLLECTION_NAME in existing:
        return

    client.create_collection(
        collection_name=QDRANT_COLLECTION_NAME,
        vectors_config=qmodels.VectorParams(
            size=EMBEDDING_DIMENSION,
            distance=qmodels.Distance.COSINE,
        ),
    )
    logger.info(
        "Created Qdrant collection '%s' (dim=%d, cosine)",
        QDRANT_COLLECTION_NAME,
        EMBEDDING_DIMENSION,
    )


# ---------------------------------------------------------------------------
# Upsert primitive
# ---------------------------------------------------------------------------


def upsert_points(
    points: Iterable[tuple[str, list[float], dict[str, Any]]],
) -> int:
    """Batch-upsert (legacy_id, vector, payload) triples into Qdrant.

    The function:
      * converts each legacy_id to a deterministic UUID (idempotent)
      * stores legacy_id inside the payload so prefix scans still work
      * batches by ``UPSERT_BATCH_SIZE``

    Args:
        points: Iterable of (legacy_id, vector, payload) tuples.

    Returns:
        Total number of points sent to Qdrant.
    """
    client = get_qdrant_client()

    try:
        from qdrant_client.http import models as qmodels
    except ImportError:  # pragma: no cover
        from qdrant_client import models as qmodels  # type: ignore[no-redef]

    batch: list[Any] = []
    total = 0

    def _flush(buf: list[Any]) -> None:
        nonlocal total
        if not buf:
            return
        client.upsert(collection_name=QDRANT_COLLECTION_NAME, points=buf)
        total += len(buf)

    for legacy_id, vector, payload in points:
        # Store the legacy ID inside the payload so prefix lookups work.
        merged_payload = dict(payload)
        merged_payload["legacy_id"] = legacy_id
        batch.append(
            qmodels.PointStruct(
                id=legacy_id_to_uuid(legacy_id),
                vector=vector,
                payload=merged_payload,
            )
        )
        if len(batch) >= UPSERT_BATCH_SIZE:
            _flush(batch)
            batch = []

    _flush(batch)
    return total


# ---------------------------------------------------------------------------
# Delete primitive
# ---------------------------------------------------------------------------


def delete_by_legacy_ids(legacy_ids: list[str]) -> int:
    """Delete points by legacy ID. Returns the number requested for delete."""
    if not legacy_ids:
        return 0

    client = get_qdrant_client()

    try:
        from qdrant_client.http import models as qmodels
    except ImportError:  # pragma: no cover
        from qdrant_client import models as qmodels  # type: ignore[no-redef]

    uuid_ids = [legacy_id_to_uuid(lid) for lid in legacy_ids]
    client.delete(
        collection_name=QDRANT_COLLECTION_NAME,
        points_selector=qmodels.PointIdsList(points=uuid_ids),
    )
    return len(uuid_ids)
