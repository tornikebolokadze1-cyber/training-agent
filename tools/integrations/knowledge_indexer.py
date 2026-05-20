"""Qdrant RAG indexing pipeline for the Training Agent.

Indexes lecture transcripts, summaries, gap analyses, and deep analyses
into a Qdrant Cloud collection so the WhatsApp assistant can retrieve
relevant course knowledge.

Migrated from Pinecone to Qdrant on 2026-05-20 — Pinecone hit its monthly
1M read limit and broke the assistant. Function signatures are preserved
so existing call sites (server.py, scheduler.py, admin_routes.py,
pipeline_retry.py, health_monitor.py) keep working without changes.

Embedding model:
- gemini-embedding-001: text embedding (3072 dims)
"""

from __future__ import annotations

import logging
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date

from google import genai
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from tools.core.config import (
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
    GEMINI_EMBEDDING_MODEL,
    GROUPS,
    PINECONE_SCORE_THRESHOLD_DIRECT,
    PINECONE_SCORE_THRESHOLD_PASSIVE,
    QDRANT_API_KEY,
    QDRANT_COLLECTION_NAME,
    QDRANT_URL,
)
from tools.core.retry import retry_with_backoff

# Shared low-level Qdrant primitives (cached client, deterministic UUID
# hashing). Importing the module — not its individual symbols — keeps the
# import lightweight: knowledge_indexer can still construct its own client
# during transition if qdrant_client.py is unavailable, while delegating
# to the shared cache when it is.
try:
    from tools.integrations import qdrant_client as _shared_qdrant
except Exception:  # pragma: no cover — only triggered if the shared module is missing
    _shared_qdrant = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_DIMENSION = 3072      # gemini-embedding-001 output size

# Approximate character counts: 4 chars ~= 1 token
CHARS_PER_TOKEN = 4

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds

# Qdrant upsert batch size — Qdrant accepts much larger batches than Pinecone
# but we keep the same limit so the throughput envelope is identical.
UPSERT_BATCH_SIZE = 100

# Namespace used for deterministic uuid5 generation. The original Pinecone
# vector IDs (e.g. "g4_l3_summary_0") are converted to UUIDs via
# uuid.uuid5(NAMESPACE, original_id) so re-indexing stays idempotent.
#
# IMPORTANT: must stay in sync with ``tools.integrations.qdrant_client.
# _LEGACY_ID_NAMESPACE`` so the two modules produce identical Qdrant
# point IDs for the same legacy string ID. Changing this UUID would
# orphan all previously-indexed vectors.
_VECTOR_ID_NAMESPACE = uuid.UUID("c6f7e1a2-8b3d-4e5f-9c0a-1d2e3f4a5b6c")

# Valid content types (used for metadata and vector ID generation)
CONTENT_TYPES = frozenset({
    "transcript", "summary", "gap_analysis", "deep_analysis",
    "whatsapp_chat", "obsidian_concept", "obsidian_tool",
    "presentation",
})


def _vector_id_for(
    group_number: int,
    lecture_number: int,
    content_type: str,
    chunk_index: int,
) -> str:
    """Build the deterministic UUID5 used as a Qdrant point ID.

    Qdrant requires UUIDs or uint64 IDs; it does not accept arbitrary strings
    like Pinecone does. We derive a stable UUID5 from the original Pinecone-
    style key ``g{N}_l{N}_{ctype}_{idx}`` so that re-indexing the same lecture
    overwrites the existing point in place.

    Returns:
        A UUID string usable as a Qdrant point ID.
    """
    raw_id = f"g{group_number}_l{lecture_number}_{content_type}_{chunk_index}"
    return str(uuid.uuid5(_VECTOR_ID_NAMESPACE, raw_id))


# ---------------------------------------------------------------------------
# Qdrant client / collection management
# ---------------------------------------------------------------------------

_qdrant_client_cache: QdrantClient | None = None
_qdrant_lock = threading.Lock()


def get_qdrant_client() -> QdrantClient:
    """Get or create the Qdrant client (cached after first call).

    Creates the ``training-course`` collection with vector size 3072 and
    cosine distance if it does not yet exist. Thread-safe.

    Returns:
        A QdrantClient connected to QDRANT_URL.

    Raises:
        RuntimeError: If QDRANT_URL is not configured.
    """
    global _qdrant_client_cache
    if _qdrant_client_cache is not None:
        return _qdrant_client_cache

    with _qdrant_lock:
        if _qdrant_client_cache is not None:
            return _qdrant_client_cache

        if not QDRANT_URL:
            raise RuntimeError(
                "Qdrant URL not configured — set QDRANT_URL in .env"
            )

        # api_key is optional for self-hosted Qdrant; required for Qdrant Cloud.
        client = QdrantClient(
            url=QDRANT_URL,
            api_key=QDRANT_API_KEY or None,
            timeout=60,
        )

        try:
            _ensure_collection(client)
        except Exception as exc:
            logger.warning(
                "Qdrant collection bootstrap failed (%s) — caller may retry on demand",
                exc,
            )

        _qdrant_client_cache = client
        return client


def _ensure_collection(client: QdrantClient) -> None:
    """Create the Qdrant collection if it does not exist.

    Uses vector size 3072 (gemini-embedding-001) and cosine distance.
    """
    try:
        existing = {c.name for c in client.get_collections().collections}
    except Exception as exc:
        logger.warning("Could not list Qdrant collections: %s", exc)
        existing = set()

    if QDRANT_COLLECTION_NAME in existing:
        logger.debug(
            "Qdrant collection '%s' already exists.", QDRANT_COLLECTION_NAME
        )
        return

    logger.info(
        "Creating Qdrant collection '%s' (dim=%d, distance=Cosine)...",
        QDRANT_COLLECTION_NAME,
        EMBEDDING_DIMENSION,
    )
    client.create_collection(
        collection_name=QDRANT_COLLECTION_NAME,
        vectors_config=qmodels.VectorParams(
            size=EMBEDDING_DIMENSION,
            distance=qmodels.Distance.COSINE,
        ),
    )
    logger.info("Qdrant collection '%s' created.", QDRANT_COLLECTION_NAME)


# ---------------------------------------------------------------------------
# Backward-compatible alias — older callers import ``get_pinecone_index``.
# Returning the Qdrant client (which exposes the same high-level operations
# this module wraps) keeps reachability checks like ``await asyncio.to_thread
# (get_pinecone_index)`` in pipeline_retry.py / orchestrator.py working
# without changes.
# ---------------------------------------------------------------------------


def get_pinecone_index() -> QdrantClient:
    """Backward-compatible alias for ``get_qdrant_client``.

    Returns the Qdrant client. The historical name is kept so the dozen
    call sites across server.py, scheduler.py, orchestrator.py, and
    pipeline_retry.py do not need to change in this migration PR.
    """
    return get_qdrant_client()


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

_embed_client_cache: genai.Client | None = None


def _get_embed_client() -> genai.Client:
    """Return a cached Gemini client for embedding calls."""
    global _embed_client_cache
    if _embed_client_cache is not None:
        return _embed_client_cache

    api_key = GEMINI_API_KEY_PAID or GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("Gemini API key not configured — set GEMINI_API_KEY in .env")

    _embed_client_cache = genai.Client(api_key=api_key)
    return _embed_client_cache


def embed_text(text: str) -> list[float]:
    """Generate an embedding vector using gemini-embedding-001.

    Args:
        text: Input text to embed (any length; truncated server-side if needed).

    Returns:
        A list of 3072 floats representing the embedding vector.

    Raises:
        RuntimeError: If GEMINI_API_KEY is not configured or all retries fail.
    """
    client = _get_embed_client()

    def _do_embed() -> list[float]:
        logger.debug(
            "Embedding text (%d chars) with %s...",
            len(text), GEMINI_EMBEDDING_MODEL,
        )
        response = client.models.embed_content(
            model=GEMINI_EMBEDDING_MODEL,
            contents=text,
        )
        vector = response.embeddings[0].values
        logger.debug("Embedding generated (%d dims).", len(vector))
        return list(vector)

    return retry_with_backoff(
        _do_embed,
        max_retries=MAX_RETRIES,
        backoff_base=RETRY_BASE_DELAY,
        operation_name="embedding",
    )


# Max texts per batch embed call (Gemini API limit)
EMBED_BATCH_SIZE = 20


def embed_texts_batch(texts: list[str]) -> list[list[float]]:
    """Generate embedding vectors for multiple texts in batched API calls.

    Reduces API round trips from N to ceil(N / EMBED_BATCH_SIZE).

    Args:
        texts: List of input texts to embed.

    Returns:
        List of embedding vectors (each 3072 floats), same order as input.

    Raises:
        RuntimeError: If any batch fails after retries.
    """
    if not texts:
        return []

    client = _get_embed_client()
    all_vectors: list[list[float]] = []

    for batch_start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[batch_start: batch_start + EMBED_BATCH_SIZE]

        def _do_batch_embed(b: list[str] = batch, bs: int = batch_start) -> list[list[float]]:
            logger.debug(
                "Batch embedding %d texts (%d-%d) with %s...",
                len(b), bs, bs + len(b), GEMINI_EMBEDDING_MODEL,
            )
            response = client.models.embed_content(
                model=GEMINI_EMBEDDING_MODEL,
                contents=b,
            )
            batch_vectors = [list(e.values) for e in response.embeddings]
            logger.debug(
                "Batch embedded %d texts (%d dims each).",
                len(batch_vectors), len(batch_vectors[0]) if batch_vectors else 0,
            )
            return batch_vectors

        batch_vectors = retry_with_backoff(
            _do_batch_embed,
            max_retries=MAX_RETRIES,
            backoff_base=RETRY_BASE_DELAY,
            operation_name="batch embedding",
        )
        all_vectors.extend(batch_vectors)

    return all_vectors


# ---------------------------------------------------------------------------
# Embedding quality validation
# ---------------------------------------------------------------------------


class EmbeddingQualityError(ValueError):
    """Raised when an embedding vector fails quality checks."""


def validate_embedding(vector: list[float], *, label: str = "embedding") -> None:
    """Validate that an embedding vector meets quality criteria.

    Checks:
    - Dimension matches EMBEDDING_DIMENSION (3072 for gemini-embedding-001).
    - Vector is not all zeros.
    - Vector norm is within a reasonable range (not too small or too large).

    Args:
        vector: The embedding vector to validate.
        label: A label for error messages (e.g. chunk ID).

    Raises:
        EmbeddingQualityError: If any check fails.
    """
    if len(vector) != EMBEDDING_DIMENSION:
        raise EmbeddingQualityError(
            f"{label}: expected {EMBEDDING_DIMENSION} dims, got {len(vector)}"
        )

    norm = math.sqrt(sum(v * v for v in vector))

    if norm < 1e-8:
        raise EmbeddingQualityError(
            f"{label}: vector is all zeros (norm={norm:.2e})"
        )

    # Gemini embedding norms are typically close to 1.0 for cosine-metric models,
    # but allow a generous range to avoid false positives.
    if norm < 0.01 or norm > 100.0:
        raise EmbeddingQualityError(
            f"{label}: vector norm out of range ({norm:.4f}); "
            "expected between 0.01 and 100.0"
        )


# ---------------------------------------------------------------------------
# Lecture filter helper
# ---------------------------------------------------------------------------


def _lecture_filter(
    group_number: int,
    lecture_number: int,
    content_type: str | None = None,
) -> qmodels.Filter:
    """Build a Qdrant Filter selecting all points for one (group, lecture[, ctype])."""
    must: list[qmodels.FieldCondition] = [
        qmodels.FieldCondition(
            key="group_number",
            match=qmodels.MatchValue(value=group_number),
        ),
        qmodels.FieldCondition(
            key="lecture_number",
            match=qmodels.MatchValue(value=lecture_number),
        ),
    ]
    if content_type:
        must.append(
            qmodels.FieldCondition(
                key="content_type",
                match=qmodels.MatchValue(value=content_type),
            )
        )
    return qmodels.Filter(must=must)


def lecture_exists_in_index(
    group_number: int,
    lecture_number: int,
    content_type: str | None = None,
) -> bool:
    """Check whether vectors for a lecture already exist in the collection.

    Uses Qdrant's ``count`` API with a filter — fast, exact, no embedding
    required.

    Args:
        group_number: Training group (1, 2, 3, ...).
        lecture_number: Lecture sequence number (1-15).
        content_type: Optional filter — check a specific content type only.

    Returns:
        True if at least one matching point exists.
    """
    try:
        client = get_qdrant_client()
        result = client.count(
            collection_name=QDRANT_COLLECTION_NAME,
            count_filter=_lecture_filter(group_number, lecture_number, content_type),
            exact=False,
        )
        return getattr(result, "count", 0) > 0
    except Exception as exc:
        logger.warning(
            "lecture_exists_in_index check failed for g%d l%d: %s — assuming not indexed",
            group_number, lecture_number, exc,
        )
        return False


def delete_lecture_vectors(
    group_number: int,
    lecture_number: int,
    content_type: str | None = None,
) -> int:
    """Delete every vector belonging to a lecture (or one content_type of it).

    Used by ``/admin/reset-pipeline`` (Issue #45) and the data-reconciliation
    job to evict orphaned vectors when the originating pipeline is invalidated.

    Best-effort: returns the approximate count of points that matched before
    deletion. Failures are logged but do not raise.

    Args:
        group_number: Training group (1, 2, 3, ...).
        lecture_number: Lecture sequence number (1-15).
        content_type: Optional — restrict the delete to one content type.

    Returns:
        Approximate number of points that were deleted. Zero on error or
        when no matching points existed.
    """
    client = get_qdrant_client()
    flt = _lecture_filter(group_number, lecture_number, content_type)

    # Count first so the caller learns how many points went away.
    pre_count = 0
    try:
        result = client.count(
            collection_name=QDRANT_COLLECTION_NAME,
            count_filter=flt,
            exact=True,
        )
        pre_count = int(getattr(result, "count", 0))
    except Exception as exc:
        logger.warning(
            "delete_lecture_vectors: pre-count failed for g%d l%d (%s): %s",
            group_number, lecture_number, content_type or "all", exc,
        )

    if pre_count == 0:
        return 0

    try:
        client.delete(
            collection_name=QDRANT_COLLECTION_NAME,
            points_selector=qmodels.FilterSelector(filter=flt),
        )
    except Exception as exc:
        logger.warning(
            "delete_lecture_vectors: delete failed for g%d l%d (%s): %s",
            group_number, lecture_number, content_type or "all", exc,
        )
        return 0

    logger.info(
        "delete_lecture_vectors: removed %d vectors for g%d l%d (%s)",
        pre_count,
        group_number, lecture_number,
        content_type or "all",
    )
    return pre_count


def get_lecture_vector_count(
    group_number: int,
    lecture_number: int,
    content_type: str | None = None,
) -> int:
    """Count vectors for a lecture in the collection.

    Args:
        group_number: Training group (1, 2, 3, ...).
        lecture_number: Lecture sequence number (1-15).
        content_type: Optional — count for a specific content type only.

    Returns:
        Number of vectors found (0 if none or on error).
    """
    try:
        client = get_qdrant_client()
        result = client.count(
            collection_name=QDRANT_COLLECTION_NAME,
            count_filter=_lecture_filter(group_number, lecture_number, content_type),
            exact=True,
        )
        return int(getattr(result, "count", 0))
    except Exception as exc:
        logger.warning(
            "get_lecture_vector_count failed for g%d l%d: %s",
            group_number, lecture_number, exc,
        )
        return 0


# ---------------------------------------------------------------------------
# Index health check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PineconeHealthReport:
    """Result of check_pinecone_health().

    Name kept as ``PineconeHealthReport`` for backward compatibility with
    callers in health_monitor.py / admin_routes.py that import it directly.
    The semantics now describe the Qdrant collection.
    """

    healthy: bool
    total_vectors: int
    lecture_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


# Forward-compatible alias for new call sites.
QdrantHealthReport = PineconeHealthReport


def check_pinecone_health() -> PineconeHealthReport:
    """Verify the Qdrant collection is reachable and return vector statistics.

    Function name preserved for backward compatibility — see migration notes
    at the top of this file.

    Returns:
        A PineconeHealthReport with total count and per-group/per-lecture counts.
    """
    try:
        client = get_qdrant_client()

        # Total point count across the collection.
        total = 0
        try:
            info = client.get_collection(collection_name=QDRANT_COLLECTION_NAME)
            total = int(getattr(info, "points_count", 0) or 0)
        except Exception as exc:
            logger.debug("get_collection failed, falling back to count(): %s", exc)
            try:
                result = client.count(
                    collection_name=QDRANT_COLLECTION_NAME, exact=True,
                )
                total = int(getattr(result, "count", 0))
            except Exception as inner:
                logger.warning("Qdrant total count fallback failed: %s", inner)

        # Per-group, per-lecture counts.
        lecture_counts: dict[str, int] = {}
        for group_num in sorted(GROUPS.keys()):
            for lecture_num in range(1, 16):
                try:
                    res = client.count(
                        collection_name=QDRANT_COLLECTION_NAME,
                        count_filter=_lecture_filter(group_num, lecture_num),
                        exact=True,
                    )
                    cnt = int(getattr(res, "count", 0))
                    if cnt > 0:
                        lecture_counts[f"g{group_num}_l{lecture_num}"] = cnt
                except Exception:
                    pass  # skip on error, don't fail the whole health check

        return PineconeHealthReport(
            healthy=True,
            total_vectors=total,
            lecture_counts=lecture_counts,
        )

    except Exception as exc:
        logger.error("Qdrant health check failed: %s", exc)
        return PineconeHealthReport(
            healthy=False,
            total_vectors=0,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks by token-approximate character count.

    Uses ~4 chars per token as an approximation so that chunk_size=500 yields
    chunks of roughly 500 tokens (2000 characters).

    Args:
        text: The full text to split.
        chunk_size: Approximate token count per chunk (default 500).
        overlap: Token overlap between consecutive chunks (default 50).

    Returns:
        List of text chunks. Returns a single-element list if the text fits
        within one chunk.
    """
    if not text:
        return []

    char_size = chunk_size * CHARS_PER_TOKEN
    char_overlap = overlap * CHARS_PER_TOKEN

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + char_size, text_len)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        start += char_size - char_overlap

    logger.debug(
        "Chunked %d chars into %d chunks (size=%d tokens, overlap=%d tokens).",
        text_len, len(chunks), chunk_size, overlap,
    )
    return chunks


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def index_lecture_content(
    group_number: int,
    lecture_number: int,
    content: str,
    content_type: str,
    *,
    force: bool = False,
) -> int:
    """Chunk, embed, and upsert lecture content into Qdrant.

    Each point is stored with payload metadata so the assistant can filter by
    group, lecture number, and content type during retrieval. Point IDs are
    deterministic UUID5 values derived from the original Pinecone-style key
    so re-indexing the same chunk overwrites in place.

    Idempotent: if vectors already exist for this lecture+content_type with the
    same chunk count, the function skips re-indexing. If the new content produces
    more chunks (lecture grew), it re-indexes. Use ``force=True`` to always
    re-index.

    Args:
        group_number: Training group identifier (1, 2, 3, ...).
        lecture_number: Lecture sequence number (1-15).
        content: Raw text content to index.
        content_type: One of "transcript", "summary", "gap_analysis", "deep_analysis".
        force: If True, re-index even when chunk counts match.

    Returns:
        Number of vectors successfully upserted (0 if skipped).

    Raises:
        ValueError: If content_type is not a recognised type.
        EmbeddingQualityError: If generated embeddings fail validation.
        RuntimeError: If Qdrant or Gemini API calls fail after retries.
    """
    if content_type not in CONTENT_TYPES:
        raise ValueError(
            f"Unknown content_type '{content_type}'. "
            f"Must be one of: {sorted(CONTENT_TYPES)}"
        )

    if not content.strip():
        logger.warning(
            "Empty content for g%d l%d %s — skipping.", group_number, lecture_number, content_type
        )
        return 0

    client = get_qdrant_client()
    chunks = chunk_text(content)
    new_chunk_count = len(chunks)

    # --- Idempotent indexing check ---
    if not force:
        existing_count = get_lecture_vector_count(
            group_number, lecture_number, content_type
        )
        if existing_count > 0 and existing_count >= new_chunk_count:
            logger.info(
                "Skipping g%d l%d [%s]: already indexed (%d vectors, new would be %d).",
                group_number, lecture_number, content_type,
                existing_count, new_chunk_count,
            )
            return 0

    today_iso = date.today().isoformat()

    # Delete stale vectors from a previous indexing run (e.g., if re-indexing
    # produces fewer chunks, old vectors with higher indices would remain).
    try:
        client.delete(
            collection_name=QDRANT_COLLECTION_NAME,
            points_selector=qmodels.FilterSelector(
                filter=_lecture_filter(group_number, lecture_number, content_type),
            ),
        )
        logger.info(
            "Cleaned stale vectors for g%d l%d [%s]",
            group_number, lecture_number, content_type,
        )
    except Exception as e:
        logger.warning("Failed to clean stale vectors: %s — proceeding with upsert", e)

    # Batch-embed all chunks (reduces N API calls to ceil(N/20))
    embeddings = embed_texts_batch(chunks)

    # Validate all embeddings before upserting
    for i, embedding in enumerate(embeddings):
        validate_embedding(
            embedding,
            label=f"g{group_number}_l{lecture_number}_{content_type}_{i}",
        )

    points: list[qmodels.PointStruct] = []
    for chunk_index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        point_id = _vector_id_for(
            group_number, lecture_number, content_type, chunk_index,
        )
        points.append(
            qmodels.PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "group_number": group_number,
                    "lecture_number": lecture_number,
                    "content_type": content_type,
                    "date": today_iso,
                    "chunk_index": chunk_index,
                    "text": chunk,
                    # Preserve the original Pinecone-style key for debugging
                    # and so future migrations / dashboards can resolve it.
                    "legacy_id": f"g{group_number}_l{lecture_number}_{content_type}_{chunk_index}",
                },
            )
        )
        logger.debug("Prepared point %s (%d chars).", point_id, len(chunk))

    total_upserted = _batch_upsert(client, points)
    logger.info(
        "Indexed %d vectors for g%d l%d [%s].",
        total_upserted, group_number, lecture_number, content_type,
    )
    return total_upserted


def _batch_upsert(client: object, vectors: list) -> int:
    """Upsert points into Qdrant in batches of UPSERT_BATCH_SIZE.

    Accepts either Qdrant PointStruct objects or legacy Pinecone-style dicts
    (``{"id": ..., "values": ..., "metadata": {...}}``) — the dict form is
    converted on the fly so existing tests and any leftover callers don't
    break during the transition.

    Args:
        client: A QdrantClient (or compatible mock).
        vectors: List of PointStruct or legacy vector dicts.

    Returns:
        Total number of points upserted.
    """
    if not vectors:
        return 0

    points = [_to_point(v) for v in vectors]

    total = 0
    for batch_start in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[batch_start: batch_start + UPSERT_BATCH_SIZE]

        retry_with_backoff(
            client.upsert,
            collection_name=QDRANT_COLLECTION_NAME,
            points=batch,
            max_retries=MAX_RETRIES,
            backoff_base=RETRY_BASE_DELAY,
            operation_name="Qdrant upsert",
        )
        total += len(batch)
        logger.debug(
            "Upserted batch of %d points (total so far: %d).", len(batch), total
        )
    return total


def _to_point(v: object) -> qmodels.PointStruct:
    """Coerce a legacy Pinecone-style vector dict into a Qdrant PointStruct.

    Accepts:
    - ``qmodels.PointStruct`` → returned unchanged.
    - ``{"id": str, "values": [...], "metadata": {...}}`` → converted.
    """
    if isinstance(v, qmodels.PointStruct):
        return v
    if isinstance(v, dict):
        raw_id = v.get("id")
        vector = v.get("values") or v.get("vector")
        payload = dict(v.get("metadata") or v.get("payload") or {})
        # Convert Pinecone-style string IDs to deterministic UUID5.
        if isinstance(raw_id, str) and not _looks_like_uuid(raw_id):
            point_id: str | int = str(uuid.uuid5(_VECTOR_ID_NAMESPACE, raw_id))
            payload.setdefault("legacy_id", raw_id)
        else:
            point_id = raw_id  # type: ignore[assignment]
        return qmodels.PointStruct(id=point_id, vector=vector, payload=payload)
    raise TypeError(f"Cannot convert {type(v).__name__} to Qdrant PointStruct")


def _looks_like_uuid(s: str) -> bool:
    """Cheap UUID-format check used to avoid double-hashing already-uuid IDs."""
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

# Conservative pre-filter applied before the mode-specific threshold so very
# weak matches (random Georgian noise hits) never reach the assistant.
MIN_RELEVANCE_SCORE = 0.40


def query_knowledge(
    query_text: str,
    group_number: int | None = None,
    top_k: int = 5,
    *,
    score_threshold: float | None = None,
    mode: str = "direct",
) -> list[dict]:
    """Query Qdrant for course knowledge chunks relevant to the query.

    Args:
        query_text: The question or topic to search for.
        group_number: Optional filter -- restrict results to a specific group.
        top_k: Number of top results to return (default 5).
        score_threshold: Minimum cosine similarity score to include a result.
            If None, uses the default from config based on ``mode``.
        mode: "direct" (explicit question) or "passive" (topic detection).
            Affects the default score threshold.

    Returns:
        List of result dicts (filtered by score), each containing:
        - ``text`` (str): The matched chunk text.
        - ``score`` (float): Cosine similarity score (higher is better).
        - ``metadata`` (dict): Full payload (group, lecture, type, etc.).

    Raises:
        RuntimeError: If the query fails after retries.
    """
    if not query_text.strip():
        logger.warning("Empty query_text passed to query_knowledge — returning empty results.")
        return []

    # Resolve threshold
    if score_threshold is None:
        if mode == "passive":
            score_threshold = PINECONE_SCORE_THRESHOLD_PASSIVE
        else:
            score_threshold = PINECONE_SCORE_THRESHOLD_DIRECT

    client = get_qdrant_client()
    query_vector = embed_text(query_text)

    # Validate query vector before sending
    try:
        validate_embedding(query_vector, label="query_vector")
    except EmbeddingQualityError as exc:
        logger.error("Query embedding failed validation: %s", exc)
        return []

    query_filter: qmodels.Filter | None = None
    if group_number is not None:
        query_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="group_number",
                    match=qmodels.MatchValue(value=group_number),
                )
            ]
        )

    logger.info(
        "Querying Qdrant: top_k=%d, group_filter=%s, threshold=%.2f, "
        "mode=%s, query='%s...'",
        top_k, group_number, score_threshold, mode, query_text[:80],
    )

    try:
        response = retry_with_backoff(
            client.query_points,
            collection_name=QDRANT_COLLECTION_NAME,
            query=query_vector,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
            max_retries=MAX_RETRIES,
            backoff_base=RETRY_BASE_DELAY,
            operation_name="Qdrant query",
        )
    except Exception as exc:
        logger.error("Qdrant query failed: %s", exc)
        return []

    raw_matches = _extract_matches(response)

    # Apply the conservative pre-filter first.
    above_floor = [
        m for m in raw_matches
        if _match_score(m) >= MIN_RELEVANCE_SCORE
    ]

    if not above_floor:
        logger.info(
            "No Qdrant results above pre-filter score %.2f for query",
            MIN_RELEVANCE_SCORE,
        )
        return []

    results: list[dict] = []
    filtered_count = 0
    for match in above_floor:
        score = _match_score(match)
        payload = _match_payload(match)

        if score < score_threshold:
            filtered_count += 1
            continue

        results.append({
            "text": payload.get("text", ""),
            "score": score,
            "metadata": payload,
        })

    logger.info(
        "Query returned %d results (%d filtered below %.2f threshold).",
        len(results), filtered_count, score_threshold,
    )
    return results


def _extract_matches(response: object) -> list:
    """Pull the list of scored points out of a Qdrant query_points response.

    The official client returns a QueryResponse with a ``.points`` attribute;
    some older mocks may return a bare list or a dict — handle all three.
    """
    if response is None:
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        return response.get("points") or response.get("matches") or []
    return list(getattr(response, "points", None) or getattr(response, "matches", []) or [])


def _match_score(match: object) -> float:
    if isinstance(match, dict):
        return float(match.get("score", 0.0) or 0.0)
    return float(getattr(match, "score", 0.0) or 0.0)


def _match_payload(match: object) -> dict:
    if isinstance(match, dict):
        payload = match.get("payload") or match.get("metadata") or {}
        return dict(payload) if isinstance(payload, dict) else {}
    payload = getattr(match, "payload", None) or getattr(match, "metadata", None) or {}
    return dict(payload) if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# WhatsApp chat indexing
# ---------------------------------------------------------------------------


def index_whatsapp_chats() -> int:
    """Fetch WhatsApp chat history from Green API and index into the collection.

    Indexes messages from both training group chats as searchable content.
    Uses a synthetic group-level ID (group_number=N, lecture_number=0)
    to distinguish chat content from lecture content.

    Returns:
        Total number of vectors indexed across both groups.
    """
    import httpx

    from tools.core.config import (
        GREEN_API_INSTANCE_ID,
        GREEN_API_TOKEN,
    )

    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        logger.warning("Green API not configured — cannot index WhatsApp chats")
        return 0

    total = 0
    chats = [
        (group_cfg["whatsapp_chat_id"], group_num)
        for group_num, group_cfg in sorted(GROUPS.items())
        if group_cfg.get("whatsapp_chat_id")
    ]

    for chat_id, group_num in chats:
        if not chat_id:
            continue

        url = (
            f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE_ID}"
            f"/getChatHistory/{GREEN_API_TOKEN}"
        )

        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(url, json={"chatId": chat_id, "count": 100})

            if response.status_code != 200:
                logger.warning("Green API returned %d for group %d", response.status_code, group_num)
                continue

            messages = response.json()
            if not messages:
                continue

            lines: list[str] = []
            for msg in messages:
                sender = msg.get("senderName", msg.get("senderId", "?"))
                text = msg.get("textMessage", "")
                if not text:
                    msg_type = msg.get("typeMessage", "")
                    text = f"[{msg_type}]" if msg_type else "[media]"
                lines.append(f"{sender}: {text}")

            chat_text = "\n".join(lines)
            if not chat_text.strip():
                continue

            count = index_lecture_content(
                group_number=group_num,
                lecture_number=0,
                content=chat_text,
                content_type="whatsapp_chat",
            )
            total += count
            logger.info("Indexed %d WhatsApp chat vectors for group %d", count, group_num)

        except Exception as exc:
            logger.error("Failed to index WhatsApp chat for group %d: %s", group_num, exc)

    return total


# ---------------------------------------------------------------------------
# Obsidian knowledge indexing
# ---------------------------------------------------------------------------


def index_obsidian_knowledge() -> int:
    """Index Obsidian vault concept and tool notes into the collection.

    Reads markdown files from the vault's კონცეფციები/ and ინსტრუმენტები/
    directories and indexes their content for RAG retrieval.

    Uses group_number=0 (cross-group) and lecture_number=0 (non-lecture)
    since these are general knowledge notes, not lecture-specific.

    Returns:
        Total number of vectors indexed.
    """

    from tools.core.config import PROJECT_ROOT

    vault_root = PROJECT_ROOT / "obsidian-vault"
    total = 0

    dirs_and_types = [
        (vault_root / "კონცეფციები", "obsidian_concept"),
        (vault_root / "ინსტრუმენტები", "obsidian_tool"),
    ]

    for dir_path, content_type in dirs_and_types:
        if not dir_path.exists():
            logger.warning("Obsidian directory not found: %s", dir_path)
            continue

        md_files = sorted(dir_path.glob("*.md"))
        all_content: list[str] = []

        for md_file in md_files:
            text = md_file.read_text(encoding="utf-8")
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1:
                    text = text[end + 3:].strip()
            if text:
                all_content.append(f"# {md_file.stem}\n{text}")

        if not all_content:
            continue

        combined = "\n\n---\n\n".join(all_content)
        count = index_lecture_content(
            group_number=0,
            lecture_number=0,
            content=combined,
            content_type=content_type,
        )
        total += count
        logger.info("Indexed %d vectors from %d %s notes", count, len(md_files), content_type)

    return total


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def delete_content_type(content_type: str, group_number: int | None = None) -> int:
    """Delete all vectors of a specific content type from the collection.

    Args:
        content_type: The content type to delete (e.g., "deep_analysis").
        group_number: Optional — only delete for a specific group.

    Returns:
        1 on success, 0 on failure. (Qdrant delete-by-filter does not return
        an exact count without a pre-query; the caller treats this as a
        boolean indicator, matching the historical Pinecone behavior.)
    """
    client = get_qdrant_client()

    must: list[qmodels.FieldCondition] = [
        qmodels.FieldCondition(
            key="content_type",
            match=qmodels.MatchValue(value=content_type),
        ),
    ]
    if group_number is not None:
        must.append(
            qmodels.FieldCondition(
                key="group_number",
                match=qmodels.MatchValue(value=group_number),
            )
        )

    try:
        client.delete(
            collection_name=QDRANT_COLLECTION_NAME,
            points_selector=qmodels.FilterSelector(filter=qmodels.Filter(must=must)),
        )
        logger.info(
            "Deleted vectors: content_type=%s, group=%s",
            content_type, group_number or "all",
        )
        return 1
    except Exception as exc:
        logger.error("Failed to delete %s vectors: %s", content_type, exc)
        return 0


# ---------------------------------------------------------------------------
# CLI / bulk indexing entrypoint
# ---------------------------------------------------------------------------

def index_all_existing_content() -> None:
    """CLI entrypoint placeholder (see original docstring for usage)."""
    logger.info(
        "index_all_existing_content() is a placeholder — "
        "implement Google Drive scanning when Drive tool integration is ready. "
        "See docstring for manual indexing instructions."
    )


# ---------------------------------------------------------------------------
# CLI entrypoint for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  python -m tools.integrations.knowledge_indexer test-embed\n"
            "  python -m tools.integrations.knowledge_indexer index <group> <lecture> <type> <text_file>\n"
            "  python -m tools.integrations.knowledge_indexer query <group> '<question>'\n"
            "  python -m tools.integrations.knowledge_indexer index-all\n"
        )
        sys.exit(1)

    command = sys.argv[1]

    if command == "test-embed":
        sample = "AI ტრენინგი საქართველოში — ლექცია #1"
        print(f"Embedding test text: '{sample}'")
        vec = embed_text(sample)
        print(f"Embedding dimension: {len(vec)}")
        print(f"First 5 values: {vec[:5]}")

    elif command == "index":
        if len(sys.argv) < 6:
            print("Usage: index <group_number> <lecture_number> <content_type> <text_file>")
            sys.exit(1)
        grp = int(sys.argv[2])
        lec = int(sys.argv[3])
        ctype = sys.argv[4]
        text_file = sys.argv[5]
        with open(text_file, encoding="utf-8") as fh:
            raw_text = fh.read()
        n = index_lecture_content(grp, lec, raw_text, ctype)
        print(f"Indexed {n} vectors for group {grp}, lecture {lec}, type '{ctype}'.")

    elif command == "query":
        if len(sys.argv) < 3:
            print("Usage: query [group_number] '<question>'")
            sys.exit(1)
        if len(sys.argv) == 4:
            grp_filter: int | None = int(sys.argv[2])
            question = sys.argv[3]
        else:
            grp_filter = None
            question = sys.argv[2]
        results = query_knowledge(question, group_number=grp_filter, top_k=5)
        print(f"\nTop {len(results)} results for: '{question}'\n")
        for i, r in enumerate(results, 1):
            meta = r["metadata"]
            print(
                f"[{i}] score={r['score']:.4f} | "
                f"group={meta.get('group_number')} "
                f"lecture={meta.get('lecture_number')} "
                f"type={meta.get('content_type')}"
            )
            print(f"    {r['text'][:200].strip()}")
            print()

    elif command == "index-all":
        index_all_existing_content()

    else:
        print(f"Unknown command: '{command}'")
        sys.exit(1)
