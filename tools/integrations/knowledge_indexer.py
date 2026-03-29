"""Pinecone RAG indexing pipeline for the Training Agent.

Indexes lecture transcripts, summaries, gap analyses, and deep analyses
into a Pinecone vector database so the WhatsApp assistant can retrieve
relevant course knowledge.

Embedding model:
- gemini-embedding-001: text embedding (3072 dims)
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date

from google import genai
from pinecone import Pinecone, ServerlessSpec

from tools.core.config import (
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
    GEMINI_EMBEDDING_MODEL,
    PINECONE_API_KEY,
    PINECONE_INDEX_NAME,
)
from tools.core.retry import retry_with_backoff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_DIMENSION = 3072      # gemini-embedding-001 output size
EMBEDDING_CLOUD = "aws"
EMBEDDING_REGION = "us-east-1"

# Approximate character counts: 4 chars ~= 1 token
CHARS_PER_TOKEN = 4

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds

# Pinecone upsert batch size limit
UPSERT_BATCH_SIZE = 100

# Valid content types (used for metadata and vector ID generation)
CONTENT_TYPES = frozenset({
    "transcript", "summary", "gap_analysis", "deep_analysis",
    "whatsapp_chat", "obsidian_concept", "obsidian_tool",
})


# ---------------------------------------------------------------------------
# Pinecone index management
# ---------------------------------------------------------------------------

_pinecone_index_cache: object | None = None
_pinecone_lock = threading.Lock()


def get_pinecone_index() -> object:
    """Get or create the Pinecone index (cached after first call).

    Uses dimension=3072 (gemini-embedding-001 output size) with cosine metric.
    Creates a serverless index if it does not yet exist.
    Thread-safe via lock to prevent duplicate initialization.

    Returns:
        A Pinecone Index object ready for upsert and query operations.

    Raises:
        RuntimeError: If PINECONE_API_KEY is not configured.
    """
    global _pinecone_index_cache
    if _pinecone_index_cache is not None:
        return _pinecone_index_cache

    with _pinecone_lock:
        # Double-check after acquiring lock (another thread may have initialized)
        if _pinecone_index_cache is not None:
            return _pinecone_index_cache

        if not PINECONE_API_KEY:
            raise RuntimeError("Pinecone API key not configured — set PINECONE_API_KEY in .env")

        pc = Pinecone(api_key=PINECONE_API_KEY)

        existing = [idx.name for idx in pc.list_indexes()]
        if PINECONE_INDEX_NAME not in existing:
            logger.info(
                "Creating Pinecone index '%s' (dim=%d, metric=cosine)...",
                PINECONE_INDEX_NAME,
                EMBEDDING_DIMENSION,
            )
            pc.create_index(
                name=PINECONE_INDEX_NAME,
                dimension=EMBEDDING_DIMENSION,
                metric="cosine",
                spec=ServerlessSpec(cloud=EMBEDDING_CLOUD, region=EMBEDDING_REGION),
            )
            # Wait until the index is ready
            _wait_for_index_ready(pc)
            logger.info("Pinecone index '%s' created and ready.", PINECONE_INDEX_NAME)
        else:
            logger.debug("Pinecone index '%s' already exists.", PINECONE_INDEX_NAME)

        index = pc.Index(PINECONE_INDEX_NAME)
        _pinecone_index_cache = index
        return index


def _wait_for_index_ready(pc: Pinecone, timeout: int = 120) -> None:
    """Poll until the newly created index transitions to ready state.

    Args:
        pc: Authenticated Pinecone client.
        timeout: Maximum seconds to wait before raising TimeoutError.

    Raises:
        TimeoutError: If the index does not become ready within timeout.
    """
    elapsed = 0
    poll_interval = 5
    while elapsed < timeout:
        description = pc.describe_index(PINECONE_INDEX_NAME)
        status = description.status
        ready = status.get("ready", False) if isinstance(status, dict) else getattr(status, "ready", False)
        if ready:
            return
        logger.debug("Index not ready yet (%ds elapsed), waiting...", elapsed)
        time.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(
        f"Pinecone index '{PINECONE_INDEX_NAME}' did not become ready within {timeout}s"
    )


def lecture_exists_in_index(group_number: int, lecture_number: int) -> bool:
    """Check if a lecture has been indexed in Pinecone.

    Uses list() with ID prefix instead of a dummy vector query,
    avoiding undefined cosine similarity behavior with zero vectors.
    """
    try:
        index = get_pinecone_index()
        # Vector IDs follow the pattern: g{group}_l{lecture}_{type}_{chunk}
        prefix = f"g{group_number}_l{lecture_number}_"
        # list() returns vectors matching the prefix — if any exist, the lecture is indexed
        results = index.list(prefix=prefix, limit=1)
        # Pinecone list returns a ListResponse with vectors attribute
        vectors = getattr(results, 'vectors', results) if not isinstance(results, list) else results
        if hasattr(vectors, '__len__'):
            return len(vectors) > 0
        # Fallback: try iterating
        for _ in vectors:
            return True
        return False
    except Exception as exc:
        logger.warning(
            "Pinecone existence check failed for G%d L%d: %s — assuming not indexed",
            group_number, lecture_number, exc,
        )
        return False


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
) -> int:
    """Chunk, embed, and upsert lecture content into Pinecone.

    Each vector is stored with metadata so the assistant can filter by group,
    lecture number, and content type during retrieval.

    Vector ID format: ``g{group_number}_l{lecture_number}_{content_type}_{chunk_index}``

    Args:
        group_number: Training group identifier (1 or 2).
        lecture_number: Lecture sequence number (1–15).
        content: Raw text content to index.
        content_type: One of "transcript", "summary", "gap_analysis", "deep_analysis".

    Returns:
        Number of vectors successfully upserted.

    Raises:
        ValueError: If content_type is not a recognised type.
        RuntimeError: If Pinecone or Gemini API calls fail after retries.
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

    index = get_pinecone_index()
    chunks = chunk_text(content)
    today_iso = date.today().isoformat()

    # Delete stale vectors from a previous indexing run (e.g., if re-indexing
    # produces fewer chunks, old vectors with higher indices would remain)
    id_prefix = f"g{group_number}_l{lecture_number}_{content_type}_"
    try:
        index.delete(
            filter={
                "group_number": {"$eq": group_number},
                "lecture_number": {"$eq": lecture_number},
                "content_type": {"$eq": content_type},
            },
        )
        logger.info("Cleaned stale vectors with prefix '%s'", id_prefix)
    except Exception as e:
        logger.warning("Failed to clean stale vectors: %s — proceeding with upsert", e)

    # Batch-embed all chunks (reduces 212 API calls to ~11 for a typical lecture)
    embeddings = embed_texts_batch(chunks)

    vectors: list[dict] = []
    for chunk_index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        vector_id = f"g{group_number}_l{lecture_number}_{content_type}_{chunk_index}"
        vectors.append({
            "id": vector_id,
            "values": embedding,
            "metadata": {
                "group_number": group_number,
                "lecture_number": lecture_number,
                "content_type": content_type,
                "date": today_iso,
                "chunk_index": chunk_index,
                "text": chunk,
            },
        })
        logger.debug("Prepared vector %s (%d chars).", vector_id, len(chunk))

    # Upsert in batches to stay within Pinecone limits
    total_upserted = _batch_upsert(index, vectors)
    logger.info(
        "Indexed %d vectors for g%d l%d [%s].",
        total_upserted, group_number, lecture_number, content_type,
    )
    return total_upserted


def _batch_upsert(index: object, vectors: list[dict]) -> int:
    """Upsert vectors into Pinecone in batches of UPSERT_BATCH_SIZE.

    Args:
        index: Pinecone Index object.
        vectors: List of vector dicts (id, values, metadata).

    Returns:
        Total number of vectors upserted.
    """
    total = 0
    for batch_start in range(0, len(vectors), UPSERT_BATCH_SIZE):
        batch = vectors[batch_start: batch_start + UPSERT_BATCH_SIZE]
        retry_with_backoff(
            index.upsert,
            vectors=batch,
            max_retries=MAX_RETRIES,
            backoff_base=RETRY_BASE_DELAY,
            operation_name="Pinecone upsert",
        )
        total += len(batch)
        logger.debug(
            "Upserted batch of %d vectors (total so far: %d).", len(batch), total
        )
    return total


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

def query_knowledge(
    query_text: str,
    group_number: int | None = None,
    top_k: int = 5,
) -> list[dict]:
    """Query Pinecone for course knowledge chunks relevant to the query.

    Args:
        query_text: The question or topic to search for.
        group_number: Optional filter — restrict results to a specific group.
        top_k: Number of top results to return (default 5).

    Returns:
        List of result dicts, each containing:
        - ``text`` (str): The matched chunk text.
        - ``score`` (float): Cosine similarity score (higher is better).
        - ``metadata`` (dict): Full vector metadata (group, lecture, type, etc.).

    Raises:
        RuntimeError: If the query fails after retries.
    """
    if not query_text.strip():
        logger.warning("Empty query_text passed to query_knowledge — returning empty results.")
        return []

    index = get_pinecone_index()
    query_vector = embed_text(query_text)

    filter_dict: dict | None = None
    if group_number is not None:
        filter_dict = {"group_number": {"$eq": group_number}}

    logger.info(
        "Querying Pinecone: top_k=%d, group_filter=%s, query='%s...'",
        top_k, group_number, query_text[:80],
    )
    response = retry_with_backoff(
        index.query,
        vector=query_vector,
        top_k=top_k,
        include_metadata=True,
        filter=filter_dict,
        max_retries=MAX_RETRIES,
        backoff_base=RETRY_BASE_DELAY,
        operation_name="Pinecone query",
    )

    MIN_RELEVANCE_SCORE = 0.55  # Filter out low-relevance chunks (raised from 0.45)

    raw_matches = response.get("matches", []) if isinstance(response, dict) else response.matches
    matches = [m for m in raw_matches if (m.get("score", 0) if isinstance(m, dict) else getattr(m, "score", 0)) >= MIN_RELEVANCE_SCORE]

    if not matches:
        logger.info("No Pinecone results above score threshold %.2f for query", MIN_RELEVANCE_SCORE)
        return []

    results: list[dict] = []
    for match in matches:
        metadata = match.get("metadata", {}) if isinstance(match, dict) else match.metadata
        score = match.get("score", 0.0) if isinstance(match, dict) else match.score
        results.append({
            "text": metadata.get("text", ""),
            "score": score,
            "metadata": metadata,
        })

    logger.info("Query returned %d results.", len(results))
    return results


# ---------------------------------------------------------------------------
# WhatsApp chat indexing
# ---------------------------------------------------------------------------


def index_whatsapp_chats() -> int:
    """Fetch WhatsApp chat history from Green API and index into Pinecone.

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
        WHATSAPP_GROUP1_ID,
        WHATSAPP_GROUP2_ID,
    )

    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        logger.warning("Green API not configured — cannot index WhatsApp chats")
        return 0

    total = 0
    chats = [
        (WHATSAPP_GROUP1_ID, 1),
        (WHATSAPP_GROUP2_ID, 2),
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

            # Build readable text from messages
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

            # Index as whatsapp_chat content type, lecture_number=0 (non-lecture)
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
    """Index Obsidian vault concept and tool notes into Pinecone.

    Reads markdown files from the vault's კონცეფციები/ and ინსტრუმენტები/
    directories and indexes their content for RAG retrieval.

    Uses group_number=0 (cross-group) and lecture_number=0 (non-lecture)
    since these are general knowledge notes, not lecture-specific.

    Returns:
        Total number of vectors indexed.
    """
    from pathlib import Path

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
            # Strip YAML frontmatter
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1:
                    text = text[end + 3:].strip()
            if text:
                all_content.append(f"# {md_file.stem}\n{text}")

        if not all_content:
            continue

        combined = "\n\n---\n\n".join(all_content)
        # Use group_number=0 for cross-group knowledge
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
    """Delete all vectors of a specific content type from the index.

    Args:
        content_type: The content type to delete (e.g., "deep_analysis").
        group_number: Optional — only delete for a specific group.

    Returns:
        Approximate number of vectors deleted (best effort).
    """
    index = get_pinecone_index()
    filter_dict: dict = {"content_type": {"$eq": content_type}}
    if group_number is not None:
        filter_dict["group_number"] = {"$eq": group_number}

    try:
        index.delete(filter=filter_dict)
        logger.info(
            "Deleted vectors: content_type=%s, group=%s",
            content_type, group_number or "all",
        )
        return 1  # Pinecone delete doesn't return count
    except Exception as exc:
        logger.error("Failed to delete %s vectors: %s", content_type, exc)
        return 0


# ---------------------------------------------------------------------------
# CLI / bulk indexing entrypoint
# ---------------------------------------------------------------------------

def index_all_existing_content() -> None:
    """CLI entrypoint: scan Google Drive for existing summaries and index them.

    This is a placeholder for a future bulk-indexing script. When implemented,
    it will:

    1. List all lecture folders in Google Drive for both groups.
    2. Download each summary and transcript Google Doc.
    3. Call index_lecture_content() for each document found.

    To index content manually right now, call index_lecture_content() directly
    with the text you want to index, specifying the group number, lecture number,
    and content type ("transcript", "summary", "gap_analysis", or "deep_analysis").

    Example::

        from tools.integrations.knowledge_indexer import index_lecture_content

        count = index_lecture_content(
            group_number=1,
            lecture_number=1,
            content="<lecture summary text here>",
            content_type="summary",
        )
        print(f"Indexed {count} vectors.")
    """
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
