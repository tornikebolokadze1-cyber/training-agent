"""Pinecone RAG indexing pipeline for the Training Agent.

Indexes lecture transcripts, summaries, gap analyses, and video frame
embeddings into a Pinecone vector database so the WhatsApp assistant can
retrieve relevant course knowledge — including visual context from slides.

Embedding models:
- gemini-embedding-001: text embedding (3072 dims)
- gemini-embedding-2-preview: multimodal embedding for video frames (3072 dims)
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

from google import genai
from pinecone import Pinecone, ServerlessSpec

from tools.config import (
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
    GEMINI_EMBEDDING_MODEL,
    GEMINI_EMBEDDING_MULTIMODAL,
    PINECONE_API_KEY,
    PINECONE_INDEX_NAME,
    TMP_DIR,
)

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
CONTENT_TYPES = frozenset({"transcript", "summary", "gap_analysis", "deep_analysis", "frame"})

# Frame extraction settings
FRAME_INTERVAL_SECONDS = 60  # 1 screenshot per minute


# ---------------------------------------------------------------------------
# Pinecone index management
# ---------------------------------------------------------------------------

_pinecone_index_cache: object | None = None
_pinecone_lock = threading.Lock()


def get_pinecone_index() -> object:
    """Get or create the Pinecone index (cached after first call).

    Uses dimension=3072 (gemini-embedding-001 output size) with cosine metric.
    Creates a serverless index if it does not yet exist.
    Thread-safe: uses a lock to prevent concurrent initialization.

    Returns:
        A Pinecone Index object ready for upsert and query operations.

    Raises:
        RuntimeError: If PINECONE_API_KEY is not configured.
    """
    global _pinecone_index_cache
    if _pinecone_index_cache is not None:
        return _pinecone_index_cache

    with _pinecone_lock:
        # Double-check after acquiring lock
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


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

_embed_client_cache: genai.Client | None = None
_embed_lock = threading.Lock()


def _get_embed_client() -> genai.Client:
    """Return a cached Gemini client for embedding calls. Thread-safe."""
    global _embed_client_cache
    if _embed_client_cache is not None:
        return _embed_client_cache

    with _embed_lock:
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

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(
                "Embedding text (%d chars) with %s (attempt %d/%d)...",
                len(text), GEMINI_EMBEDDING_MODEL, attempt, MAX_RETRIES,
            )
            response = client.models.embed_content(
                model=GEMINI_EMBEDDING_MODEL,
                contents=text,
            )
            vector = response.embeddings[0].values
            logger.debug("Embedding generated (%d dims).", len(vector))
            return list(vector)

        except Exception as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Embedding attempt %d/%d failed: %s — retrying in %ds",
                attempt, MAX_RETRIES, e, delay,
            )
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Embedding failed after {MAX_RETRIES} attempts: {e}"
                ) from e
            time.sleep(delay)

    raise RuntimeError("Unreachable")


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

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.debug(
                    "Batch embedding %d texts (%d-%d) with %s (attempt %d/%d)...",
                    len(batch), batch_start, batch_start + len(batch),
                    GEMINI_EMBEDDING_MODEL, attempt, MAX_RETRIES,
                )
                response = client.models.embed_content(
                    model=GEMINI_EMBEDDING_MODEL,
                    contents=batch,
                )
                batch_vectors = [list(e.values) for e in response.embeddings]
                all_vectors.extend(batch_vectors)
                logger.debug(
                    "Batch embedded %d texts (%d dims each).",
                    len(batch_vectors), len(batch_vectors[0]) if batch_vectors else 0,
                )
                break
            except Exception as e:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Batch embedding attempt %d/%d failed: %s — retrying in %ds",
                    attempt, MAX_RETRIES, e, delay,
                )
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"Batch embedding failed after {MAX_RETRIES} attempts: {e}"
                    ) from e
                time.sleep(delay)

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
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                index.upsert(vectors=batch)
                total += len(batch)
                logger.debug(
                    "Upserted batch of %d vectors (total so far: %d).", len(batch), total
                )
                break
            except Exception as e:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Upsert batch attempt %d/%d failed: %s — retrying in %ds",
                    attempt, MAX_RETRIES, e, delay,
                )
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"Pinecone upsert failed after {MAX_RETRIES} attempts: {e}"
                    ) from e
                time.sleep(delay)
    return total


# ---------------------------------------------------------------------------
# Frame extraction & multimodal embedding
# ---------------------------------------------------------------------------


def extract_frames(video_path: str | Path, interval: int = FRAME_INTERVAL_SECONDS) -> list[Path]:
    """Extract one frame per interval from a video using ffmpeg.

    Args:
        video_path: Path to the video file.
        interval: Seconds between frames (default: 60 = 1 per minute).

    Returns:
        Sorted list of extracted frame file paths.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    frames_dir = TMP_DIR / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Clean any previous frames
    for old in frames_dir.glob("frame_*.jpg"):
        old.unlink()

    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"fps=1/{interval}",
        "-q:v", "2",
        "-y",
        str(frames_dir / "frame_%04d.jpg"),
    ]
    logger.info("Extracting frames every %ds from %s...", interval, video_path.name)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed: {result.stderr[:500]}")

    frames = sorted(frames_dir.glob("frame_*.jpg"))
    logger.info("Extracted %d frames.", len(frames))
    return frames


def embed_frame(image_path: Path) -> list[float]:
    """Generate an embedding for an image using Gemini Embedding 2 (multimodal).

    Args:
        image_path: Path to a JPEG image file.

    Returns:
        A list of 3072 floats representing the embedding vector.
    """
    client = _get_embed_client()
    image_bytes = image_path.read_bytes()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(
                "Embedding frame %s with %s (attempt %d/%d)...",
                image_path.name, GEMINI_EMBEDDING_MULTIMODAL, attempt, MAX_RETRIES,
            )
            response = client.models.embed_content(
                model=GEMINI_EMBEDDING_MULTIMODAL,
                contents=genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            )
            vector = response.embeddings[0].values
            logger.debug("Frame embedding generated (%d dims).", len(vector))
            return list(vector)

        except Exception as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Frame embedding attempt %d/%d failed: %s — retrying in %ds",
                attempt, MAX_RETRIES, e, delay,
            )
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Frame embedding failed after {MAX_RETRIES} attempts: {e}"
                ) from e
            time.sleep(delay)

    raise RuntimeError("Unreachable")


def index_lecture_frames(
    group_number: int,
    lecture_number: int,
    video_path: str | Path,
) -> int:
    """Extract frames from a lecture video, embed, and upsert into Pinecone.

    Each frame is embedded with Gemini Embedding 2 (multimodal) and stored
    alongside text embeddings in the same Pinecone index. This enables
    cross-modal search: text queries can match visual content from slides.

    Frames are extracted every FRAME_INTERVAL_SECONDS (default 60s), embedded,
    then deleted from disk — only vectors persist in Pinecone.

    Args:
        group_number: Training group (1 or 2).
        lecture_number: Lecture sequence number (1–15).
        video_path: Path to the lecture video file.

    Returns:
        Number of frame vectors upserted.
    """
    video_path = Path(video_path)
    frames: list[Path] = []

    try:
        frames = extract_frames(video_path)
        if not frames:
            logger.warning("No frames extracted from %s", video_path.name)
            return 0

        index = get_pinecone_index()
        today_iso = date.today().isoformat()

        # Clean stale frame vectors from previous runs
        try:
            index.delete(
                filter={
                    "group_number": {"$eq": group_number},
                    "lecture_number": {"$eq": lecture_number},
                    "content_type": {"$eq": "frame"},
                },
            )
            logger.info("Cleaned stale frame vectors for g%d l%d", group_number, lecture_number)
        except Exception as e:
            logger.warning("Failed to clean stale frame vectors: %s", e)

        vectors: list[dict] = []
        for i, frame_path in enumerate(frames):
            minute = (i + 1) * (FRAME_INTERVAL_SECONDS // 60)
            try:
                embedding = embed_frame(frame_path)
                vector_id = f"g{group_number}_l{lecture_number}_frame_{i}"
                vectors.append({
                    "id": vector_id,
                    "values": embedding,
                    "metadata": {
                        "group_number": group_number,
                        "lecture_number": lecture_number,
                        "content_type": "frame",
                        "date": today_iso,
                        "chunk_index": i,
                        "minute": minute,
                        "text": f"ლექცია #{lecture_number}, წუთი {minute} — ვიზუალური ფრეიმი",
                    },
                })
                logger.debug("Embedded frame %d (minute %d)", i, minute)
            except Exception as e:
                logger.warning("Failed to embed frame %d: %s — skipping", i, e)
                continue

        if not vectors:
            logger.warning("No frames successfully embedded")
            return 0

        total = _batch_upsert(index, vectors)
        logger.info(
            "Indexed %d frame vectors for g%d l%d.",
            total, group_number, lecture_number,
        )
        return total

    finally:
        # Always clean up frame files
        frames_dir = TMP_DIR / "frames"
        if frames_dir.exists():
            for f in frames_dir.glob("frame_*.jpg"):
                f.unlink()
            logger.debug("Cleaned up frame files from %s", frames_dir)


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

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(
                "Querying Pinecone: top_k=%d, group_filter=%s, query='%s...'",
                top_k, group_number, query_text[:80],
            )
            response = index.query(
                vector=query_vector,
                top_k=top_k,
                include_metadata=True,
                filter=filter_dict,
            )
            break
        except Exception as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Query attempt %d/%d failed: %s — retrying in %ds",
                attempt, MAX_RETRIES, e, delay,
            )
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Pinecone query failed after {MAX_RETRIES} attempts: {e}"
                ) from e
            time.sleep(delay)

    matches = response.get("matches", []) if isinstance(response, dict) else response.matches
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

        from tools.knowledge_indexer import index_lecture_content

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
            "  python -m tools.knowledge_indexer test-embed\n"
            "  python -m tools.knowledge_indexer index <group> <lecture> <type> <text_file>\n"
            "  python -m tools.knowledge_indexer query <group> '<question>'\n"
            "  python -m tools.knowledge_indexer index-all\n"
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
