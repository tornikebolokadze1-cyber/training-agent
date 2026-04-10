"""Gemini lecture analysis: hybrid model pipeline.

Multimodal transcription with Gemini 2.5 Pro (video chunked into ~45min
segments via ffmpeg to fit within 1M token limit — preserves slides,
demos, and screen shares), then Claude Sonnet reasoning + Gemini Georgian
writing for analysis.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from google import genai
from google.genai import types

from tools.core.config import (
    ANTHROPIC_API_KEY,
    ASSISTANT_CLAUDE_MODEL,
    DEEP_ANALYSIS_PROMPT,
    GAP_ANALYSIS_PROMPT,
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
    GEMINI_MODEL_ANALYSIS,
    GEMINI_MODEL_TRANSCRIPTION,
    SUMMARIZATION_PROMPT,
    TMP_DIR,
    TRANSCRIPTION_CONTINUATION_PROMPT,
    TRANSCRIPTION_PROMPT,
)
from tools.core.api_resilience import resilient_api_call

logger = logging.getLogger(__name__)

# File processing states
STATE_ACTIVE = "ACTIVE"
STATE_FAILED = "FAILED"

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds
RETRY_EMPTY_RESPONSE_DELAY = 45  # seconds — Gemini may still be processing
MIN_MEANINGFUL_RESPONSE_CHARS = 100  # minimum chars for a valid response

# Enhanced retry for empty responses (Gemini returns 200 OK with 0 output tokens)
MAX_RETRIES_EMPTY = 1  # Each 0-token retry wastes ~$0.20 with no benefit
RETRY_EMPTY_RESPONSE_DELAY_LONG = 60  # 1 minute for truly empty (0 token) responses
try:
    from tools.core.cost_tracker import LECTURE_COST_LIMIT_USD as MAX_COST_PER_LECTURE
except ImportError:
    MAX_COST_PER_LECTURE = float(os.environ.get("LECTURE_COST_LIMIT_USD", "5.0"))

# Fallback model when primary transcription model fails repeatedly
GEMINI_FALLBACK_TRANSCRIPTION_MODEL = os.environ.get(
    "GEMINI_FALLBACK_TRANSCRIPTION_MODEL", "gemini-2.5-pro"
)

# Per-execution pipeline key for cost tracking.
# Using ContextVar ensures concurrent pipelines (threads or asyncio tasks)
# each see their own key and cannot overwrite each other's cost attribution.
_pipeline_key_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pipeline_key", default=""
)

FILE_POLL_INTERVAL = 3  # seconds (reduced from 10 for faster pipeline)
FILE_POLL_TIMEOUT = 1800  # 30 minutes max wait for processing (large videos)
GEMINI_GENERATE_TIMEOUT = 20 * 60  # 20 minutes per generate_content call

# Gemini pricing (per 1M tokens) — updated 2026-04 from Google Cloud SKU rates.
# Video/audio inputs are billed at HIGHER rates than text inputs.
# Flash "input" uses blended video+audio rate since our main use is transcription.
# Source: Google Cloud Billing SKU breakdown, April 2026.
GEMINI_COST_TABLE: dict[str, dict[str, float]] = {
    # Flash video transcription: video ~$0.30/M + audio ~$1.00/M overhead
    "gemini-2.5-flash": {"input": 0.42, "output": 2.50},
    # Pro models — Georgian text generation (text-only, no video)
    "gemini-3.1-pro": {"input": 1.25, "output": 10.0},
    "gemini-3-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-pro-preview": {"input": 1.25, "output": 10.0},
    "gemini-2.0-pro": {"input": 1.25, "output": 10.0},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.0},
}
GEMINI_COST_DEFAULT = {"input": 1.25, "output": 10.0}  # fallback


def _is_quota_error(error: Exception) -> bool:
    """Check if an error is a quota/rate-limit issue (switchable to paid key)."""
    error_str = str(error).lower()
    quota_indicators = ["429", "resource exhausted", "quota", "rate limit", "too many requests"]
    return any(indicator in error_str for indicator in quota_indicators)


_cache_lock = threading.Lock()
_client_cache: dict[str, genai.Client] = {}  # keyed by API key

_anthropic_client_cache: anthropic.Anthropic | None = None


def _get_anthropic_client() -> anthropic.Anthropic:
    """Return a cached Anthropic client (avoids creating fresh clients per call)."""
    global _anthropic_client_cache
    with _cache_lock:
        if _anthropic_client_cache is None:
            if not ANTHROPIC_API_KEY:
                raise RuntimeError("ANTHROPIC_API_KEY not configured in .env")
            _anthropic_client_cache = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        return _anthropic_client_cache


def _get_client(use_free: bool = False) -> genai.Client:
    """Return a cached Gemini API client (avoids creating 9+ clients per pipeline run).

    Primary: paid key (billing-enabled, higher limits).
    Fallback: free key (if paid key fails or is not configured).
    """
    with _cache_lock:
        if use_free:
            if not GEMINI_API_KEY:
                raise RuntimeError("Free Gemini API key not configured — set GEMINI_API_KEY in .env")
            key = GEMINI_API_KEY
            if key not in _client_cache:
                logger.info("Using FREE Gemini API key (fallback)")
                _client_cache[key] = genai.Client(api_key=key)
            return _client_cache[key]

        if not GEMINI_API_KEY_PAID:
            if not GEMINI_API_KEY:
                raise RuntimeError(
                    "No Gemini API key configured — set GEMINI_API_KEY or "
                    "GEMINI_API_KEY_PAID in .env"
                )
            key = GEMINI_API_KEY
            if key not in _client_cache:
                logger.warning("Paid key not configured — falling back to free key")
                _client_cache[key] = genai.Client(api_key=key)
            return _client_cache[key]

        key = GEMINI_API_KEY_PAID
        if key not in _client_cache:
            _client_cache[key] = genai.Client(api_key=key)
        return _client_cache[key]




def cleanup_orphaned_gemini_files() -> int:
    """Delete orphaned Gemini API file uploads from previous crashed runs.

    Called on startup to prevent leaked files from consuming upload slots
    and accumulating storage costs.
    """
    deleted = 0
    try:
        client = _get_client(use_free=False)
        files = client.files.list()
        now = datetime.now(timezone.utc)
        for f in files:
            try:
                create_time = getattr(f, "create_time", None)
                if create_time and (now - create_time).total_seconds() > 7200:  # 2 hours
                    client.files.delete(name=f.name)
                    deleted += 1
                    logger.info(
                        "Cleaned up orphaned Gemini file: %s (created %s)",
                        f.name,
                        create_time,
                    )
            except Exception as e:
                logger.warning("Failed to delete orphaned file %s: %s", f.name, e)
    except Exception as e:
        logger.warning("Gemini orphaned file cleanup failed: %s", e)
    if deleted:
        logger.info("Startup: cleaned up %d orphaned Gemini file(s)", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Video Chunking (multimodal — keeps video frames for slides/demos)
# ---------------------------------------------------------------------------

CHUNK_DURATION_MINUTES = 30  # ~465K tokens/chunk; smaller chunks dodge Gemini phantom-hang observed on >200MB inputs (was 45 — produced 280MB chunks for 187min lectures, ~50% timeout rate on largest chunk)


def _get_video_duration_seconds(video_path: Path) -> float:
    """Get video duration in seconds using ffprobe."""
    video_path = _validate_media_path(video_path)
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "--", str(video_path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-300:]}")
    return float(result.stdout.strip())


def _validate_media_path(video_path: Path) -> Path:
    """Resolve and validate that a media path is inside TMP_DIR.

    Raises ValueError for paths outside TMP_DIR to prevent path traversal.
    """
    resolved = video_path.resolve()
    tmp_resolved = TMP_DIR.resolve()
    try:
        resolved.relative_to(tmp_resolved)
    except ValueError:
        raise ValueError(
            f"Media path outside TMP_DIR rejected: {resolved} "
            f"(expected prefix: {tmp_resolved})"
        )
    return resolved


def split_video_chunks(video_path: str | Path) -> list[Path]:
    """Split a long video into ~45-minute chunks using ffmpeg.

    Uses stream copy (no re-encoding) for near-instant splitting.
    Gemini tokenizes video at ~258 tokens/sec, so 45 min = ~697K tokens
    which fits safely within the 1M token limit with audio overhead.

    For videos under 45 minutes, returns the original file as a single-element list.

    Returns:
        List of chunk file paths in order.
    """
    video_path = _validate_media_path(Path(video_path))
    duration = _get_video_duration_seconds(video_path)

    if duration <= 0:
        raise ValueError(f"Video has zero or negative duration ({duration}s): {video_path}")

    chunk_seconds = CHUNK_DURATION_MINUTES * 60

    if duration <= chunk_seconds:
        logger.info(
            "Video is %.0f min — fits in one chunk, no splitting needed.",
            duration / 60,
        )
        return [video_path]

    num_chunks = int(duration // chunk_seconds) + (1 if duration % chunk_seconds > 0 else 0)

    # Smart merge: if final chunk is < 15 min AND merged chunk stays within
    # token budget, merge with previous chunk to avoid a tiny API call.
    # Safety: merged chunk must not exceed ~55 min (850K tokens, safe under 1M).
    max_merged_seconds = 55 * 60  # hard upper bound for merged chunk
    remainder_seconds = duration % chunk_seconds
    merged_duration = chunk_seconds + remainder_seconds
    if (
        num_chunks > 1
        and 0 < remainder_seconds < 15 * 60
        and merged_duration <= max_merged_seconds
    ):
        num_chunks -= 1
        logger.info(
            "Video is %.0f min — merging short final chunk (%.0f min) with previous "
            "(merged=%.0f min, within budget). Splitting into %d chunks.",
            duration / 60, remainder_seconds / 60, merged_duration / 60, num_chunks,
        )
    else:
        logger.info(
            "Video is %.0f min — splitting into %d chunks of ~%d min each.",
            duration / 60, num_chunks, CHUNK_DURATION_MINUTES,
        )

    chunk_paths: list[Path] = []
    min_chunk_size = 1024 * 100  # 100 KB minimum for a valid chunk
    for i in range(num_chunks):
        start = i * chunk_seconds
        # Last chunk gets all remaining duration (handles smart merge)
        is_last = (i == num_chunks - 1)
        chunk_path = video_path.with_suffix(f".chunk{i}.mp4")

        if chunk_path.exists():
            # Validate existing chunk is not corrupted/truncated
            if chunk_path.stat().st_size >= min_chunk_size:
                logger.info("Chunk %d already exists: %s (valid)", i, chunk_path.name)
                chunk_paths.append(chunk_path)
                continue
            logger.warning(
                "Stale chunk %d too small (%d bytes) — re-creating",
                i, chunk_path.stat().st_size,
            )
            chunk_path.unlink()

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(video_path),
        ]
        if not is_last:
            cmd += ["-t", str(chunk_seconds)]
        # Last chunk: no -t flag → runs to end of video (handles smart merge)
        cmd += [
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "--", str(chunk_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg chunk {i} failed: {result.stderr[-500:]}")

        size_mb = chunk_path.stat().st_size / (1024 * 1024)
        logger.info("Chunk %d: %s (%.1f MB)", i, chunk_path.name, size_mb)
        chunk_paths.append(chunk_path)

    return chunk_paths


# ---------------------------------------------------------------------------
# File Upload (audio or video)
# ---------------------------------------------------------------------------

def upload_video(file_path: str | Path, use_free: bool = False) -> tuple[object, bool]:
    """Upload a video file to the Gemini File API.

    Returns a tuple of (uploaded file object, whether free tier was used).
    Falls back to free tier on paid-key quota errors.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Video file not found: {file_path}")

    client = _get_client(use_free=use_free)
    tier = "free" if use_free else "paid"
    file_size_mb = file_path.stat().st_size / (1024 * 1024)
    logger.info("Uploading video '%s' (%.1f MB) to Gemini (%s tier)...", file_path.name, file_size_mb, tier)

    try:
        uploaded_file = client.files.upload(file=str(file_path))
    except Exception as e:
        if not use_free and _is_quota_error(e) and GEMINI_API_KEY:
            logger.warning("Paid tier quota hit during upload: %s — switching to free tier", e)
            return upload_video(file_path, use_free=True)
        raise

    logger.info("Upload complete. File name: %s", uploaded_file.name)

    # Wait for processing
    processed_file = wait_for_processing(client, uploaded_file.name)
    return processed_file, use_free


def wait_for_processing(client: genai.Client, file_name: str) -> object:
    """Poll until the uploaded file is processed and ready.

    Retries on transient network errors. Raises RuntimeError if processing
    fails or times out.
    """
    logger.info("Waiting for Gemini to process file '%s'...", file_name)
    elapsed = 0
    consecutive_errors = 0

    while elapsed < FILE_POLL_TIMEOUT:
        try:
            file_info = client.files.get(name=file_name)
            consecutive_errors = 0  # Reset on success
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                raise RuntimeError(
                    f"Too many consecutive errors polling file '{file_name}': {e}"
                ) from e
            logger.warning(
                "Network error polling file status (attempt %d/5): %s — retrying in %ds",
                consecutive_errors, e, FILE_POLL_INTERVAL,
            )
            time.sleep(FILE_POLL_INTERVAL)
            elapsed += FILE_POLL_INTERVAL
            continue

        state = str(file_info.state)

        # Handle both enum and string representations
        if STATE_ACTIVE in state:
            logger.info("File processing complete (took ~%ds)", elapsed)
            return file_info

        if STATE_FAILED in state:
            raise RuntimeError(f"Gemini file processing failed for '{file_name}'")

        logger.info("File state: %s — waiting %ds...", state, FILE_POLL_INTERVAL)
        time.sleep(FILE_POLL_INTERVAL)
        elapsed += FILE_POLL_INTERVAL

    raise TimeoutError(
        f"File processing timed out after {FILE_POLL_TIMEOUT}s for '{file_name}'"
    )


# ---------------------------------------------------------------------------
# Step 1: Transcription (multimodal — needs video)
# ---------------------------------------------------------------------------

def _log_gemini_cost(model: str, response: object, purpose: str) -> None:
    """Log estimated cost for a Gemini generate_content call."""
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            logger.info("Cost tracking: no usage_metadata in response for %s", purpose)
            return
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        _raw_thoughts = getattr(usage, "thoughts_token_count", None)
        thoughts_tokens = _raw_thoughts if isinstance(_raw_thoughts, int) else 0
        # Find cost rates for this model
        cost_rates = GEMINI_COST_DEFAULT
        for model_key, rates in GEMINI_COST_TABLE.items():
            if model_key in model:
                cost_rates = rates
                break
        input_cost = (input_tokens / 1_000_000) * cost_rates["input"]
        output_cost = (output_tokens / 1_000_000) * cost_rates["output"]
        total_cost = input_cost + output_cost
        thinking_part = f" | thinking={thoughts_tokens} tokens" if thoughts_tokens > 0 else ""
        logger.info(
            "💰 Gemini cost [%s] model=%s | input=%d tokens ($%.4f) | "
            "output=%d tokens ($%.4f)%s | total=$%.4f",
            purpose, model, input_tokens, input_cost,
            output_tokens, output_cost, thinking_part, total_cost,
        )
        try:
            from tools.core.cost_tracker import record_cost
            record_cost(
                service="gemini",
                model=model,
                purpose=purpose,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=total_cost,
                pipeline_key=_pipeline_key_var.get(),
            )
        except Exception:
            pass  # cost tracking is non-fatal
    except Exception as e:
        logger.debug("Cost tracking failed for %s: %s", purpose, e)


def _is_empty_response_error(error: Exception) -> bool:
    """Check if error is specifically about an empty/insufficient response (not an API failure)."""
    error_str = str(error).lower()
    return any(phrase in error_str for phrase in [
        "empty response",
        "response too short",
        "insufficient content",
    ])


def _generate_with_retry(
    client: genai.Client,
    model: str,
    contents: list,
    purpose: str,
    max_output_tokens: int = 8192,
    use_free: bool = False,
    disable_thinking: bool = False,
) -> str:
    """Call generate_content with retry logic and free-tier fallback.

    Primary: paid key. On quota/rate-limit errors, falls back to the free key.
    Returns the generated text.
    """
    for attempt in range(1, MAX_RETRIES_EMPTY + 1):
        # Budget check before every attempt (including first)
        try:
            from tools.core.cost_tracker import check_lecture_budget
            _ok, _rem = check_lecture_budget(_pipeline_key_var.get())
            if not _ok:
                logger.error("BUDGET EXCEEDED: stopping retries for %s at attempt %d", purpose, attempt)
                raise RuntimeError(f"Lecture budget exceeded during retry for {purpose}")
        except (ImportError, NameError):
            pass
        try:
            tier = "free" if use_free else "paid"
            effective_max = MAX_RETRIES_EMPTY  # 0-token errors get all attempts
            logger.info("Generating %s with %s (attempt %d/%d, %s tier)...",
                        purpose, model, attempt, effective_max, tier)

            _exec = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            _future = _exec.submit(
                client.models.generate_content,
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=max_output_tokens,
                    **({"thinking_config": types.ThinkingConfig(thinking_budget=0)}
                       if disable_thinking else {}),
                ),
            )
            try:
                response = _future.result(timeout=GEMINI_GENERATE_TIMEOUT)
            except concurrent.futures.TimeoutError as te:
                _exec.shutdown(wait=False, cancel_futures=True)
                logger.warning(
                    "PHANTOM BILLING: Gemini call for %s timed out after %d min. "
                    "The API call may still complete in background and be billed by Google. "
                    "Input tokens already sent will be charged.",
                    purpose, GEMINI_GENERATE_TIMEOUT // 60,
                )
                raise TimeoutError(
                    f"Gemini generate_content timed out after "
                    f"{GEMINI_GENERATE_TIMEOUT // 60} min for {purpose}"
                ) from te
            else:
                _exec.shutdown(wait=True)  # Clean shutdown — call already completed

            # Log cost for every generate_content call (even failed ones)
            _log_gemini_cost(model, response, purpose)

            # Check for safety block BEFORE the 0-token check
            # Safety blocks are deterministic — retrying wastes money
            candidates = getattr(response, 'candidates', [])
            if candidates:
                finish_reason = getattr(candidates[0], 'finish_reason', None)
                finish_str = str(finish_reason).upper() if finish_reason else ""
                if "SAFETY" in finish_str or "RECITATION" in finish_str:
                    raise ValueError(
                        f"Gemini SAFETY FILTER blocked {purpose} "
                        f"(finish_reason={finish_reason}). "
                        f"This is deterministic — retrying will not help."
                    )

            # Detect 0 output tokens (Gemini charged for input but produced nothing)
            usage = getattr(response, "usage_metadata", None)
            output_tokens = getattr(usage, "candidates_token_count", 0) or 0 if usage else 0
            if output_tokens == 0:
                logger.warning(
                    "Gemini returned 0 output tokens for %s (attempt %d/%d) — "
                    "charged for input but produced nothing",
                    purpose, attempt, MAX_RETRIES_EMPTY,
                )
                # Use longer delay for zero-token responses
                delay = RETRY_EMPTY_RESPONSE_DELAY_LONG
                if attempt < MAX_RETRIES_EMPTY:
                    logger.info("Waiting %ds before retry (Gemini may need processing time)...", delay)
                    time.sleep(delay)
                    continue
                input_tokens = getattr(usage, "prompt_token_count", 0) or 0
                logger.warning(
                    "COST WASTE: All %d retries exhausted for %s with 0 output tokens. "
                    "~%d input tokens billed per attempt (%d total) with no usable output.",
                    MAX_RETRIES_EMPTY, purpose, input_tokens,
                    input_tokens * MAX_RETRIES_EMPTY,
                )
                raise ValueError(f"Gemini returned 0 output tokens for {purpose} after {MAX_RETRIES_EMPTY} attempts")

            # response.text raises ValueError if blocked by safety filters
            try:
                text = response.text
            except (ValueError, AttributeError) as resp_err:
                raise ValueError(
                    f"Gemini response blocked or empty for {purpose}: {resp_err}"
                ) from resp_err
            if not text:
                raise ValueError(f"Empty response for {purpose}")

            # Validate response has meaningful content (not just whitespace/boilerplate)
            stripped = text.strip()
            if len(stripped) < MIN_MEANINGFUL_RESPONSE_CHARS:
                raise ValueError(
                    f"Response too short for {purpose}: {len(stripped)} chars "
                    f"(minimum {MIN_MEANINGFUL_RESPONSE_CHARS}). "
                    f"Content: {stripped[:200]!r}"
                )

            logger.info("%s generated successfully (%d chars, %s tier)", purpose, len(text), tier)
            return text

        except Exception as e:
            # If quota error on paid tier and free key exists — switch immediately
            if not use_free and _is_quota_error(e) and GEMINI_API_KEY:
                logger.warning(
                    "Paid tier quota hit for %s: %s — switching to free tier",
                    purpose, e,
                )
                free_client = _get_client(use_free=True)
                return _generate_with_retry(
                    free_client, model, contents, purpose,
                    max_output_tokens=max_output_tokens, use_free=True,
                )

            # For non-empty-response errors, give up after MAX_RETRIES attempts
            is_empty = _is_empty_response_error(e)
            if not is_empty and attempt >= MAX_RETRIES:
                raise RuntimeError(f"{purpose} failed after {MAX_RETRIES} attempts: {e}") from e

            # For empty-response errors, give up after MAX_RETRIES_EMPTY attempts
            if is_empty and attempt >= MAX_RETRIES_EMPTY:
                raise RuntimeError(f"{purpose} failed after {MAX_RETRIES_EMPTY} attempts (empty responses): {e}") from e

            # If MAX_RETRIES_EMPTY < MAX_RETRIES and this is a non-empty error on
            # the last iteration, there are no more attempts — raise immediately.
            if not is_empty and attempt >= MAX_RETRIES_EMPTY:
                raise RuntimeError(f"{purpose} failed after {attempt} attempts: {e}") from e

            # Use longer delay for empty responses (Gemini may still be processing)
            # vs short exponential backoff for actual API errors
            if is_empty:
                delay = RETRY_EMPTY_RESPONSE_DELAY
                logger.warning(
                    "%s attempt %d returned empty/insufficient — "
                    "Gemini may still be processing, waiting %ds before retry",
                    purpose, attempt, delay,
                )
            else:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "%s attempt %d failed: %s — retrying in %ds",
                    purpose, attempt, e, delay,
                )
            time.sleep(delay)

    raise RuntimeError(f"{purpose} failed after {MAX_RETRIES_EMPTY} attempts (loop exhausted)")


@resilient_api_call(service="gemini", operation="transcribe_video", max_attempts=1, gemini_quota_fallback=True)
def transcribe_video(file_ref: object, use_free: bool = False,
                     chunk_number: int = 0, total_chunks: int = 1) -> str:
    """Transcribe a video chunk using Gemini 2.5 Pro (multimodal — sees slides/demos).

    Args:
        file_ref: The uploaded file object from upload_video().
        use_free: Whether to use the free API key (default: paid).
        chunk_number: Zero-based chunk index (0 = first/only chunk).
        total_chunks: Total number of chunks for this lecture.

    Returns:
        Georgian transcript for this chunk with timestamps, speaker markers,
        and slide/demo descriptions.
    """
    client = _get_client(use_free=use_free)

    if chunk_number == 0:
        prompt = TRANSCRIPTION_PROMPT
    else:
        prompt = TRANSCRIPTION_CONTINUATION_PROMPT.format(
            chunk_number=chunk_number + 1,
            total_chunks=total_chunks,
        )

    return _generate_with_retry(
        client,
        model=GEMINI_MODEL_TRANSCRIPTION,
        contents=[file_ref, prompt],
        purpose=f"transcription (chunk {chunk_number + 1}/{total_chunks})",
        max_output_tokens=32768,  # Reduced from 65536 — prevents wasteful max-out
        use_free=use_free,
        disable_thinking=True,  # Transcription doesn't need reasoning — saves ~$50+/month
    )


def transcribe_chunked_video(
    video_path: str | Path,
    use_free: bool = False,
    group: int | None = None,
    lecture: int | None = None,
) -> tuple[str, bool]:
    """Split video into chunks if needed, transcribe each multimodally, concatenate.

    Handles the full flow: split → upload → transcribe → cleanup → join.
    Uses try/finally to ensure Gemini uploads and local chunk files are
    cleaned up even when a mid-pipeline failure occurs.

    Checkpoint/resume: When group and lecture are provided, each chunk's
    transcript is saved to .tmp/ after successful transcription. On restart,
    already-transcribed chunks are loaded from disk instead of re-uploading
    and re-transcribing — saving ~$1-2 per chunk.

    Args:
        video_path: Path to the original video file.
        use_free: Whether to start with the free API key.
        group: Group number (for checkpoint filenames). Optional.
        lecture: Lecture number (for checkpoint filenames). Optional.

    Returns:
        Tuple of (full transcript text, whether free tier was used).
    """
    _pipeline_key_var.set(f"g{group}_l{lecture}" if group and lecture else "")

    chunks = split_video_chunks(video_path)
    total_chunks = len(chunks)
    transcripts: list[str] = []
    prefix = _checkpoint_prefix(group, lecture)
    use_checkpoints = bool(prefix)

    # Track resources for cleanup on failure
    uploaded_gemini_files: list[tuple[str, bool]] = []  # (file_name, use_free)

    try:
        for i, chunk_path in enumerate(chunks):
            # Check for existing chunk checkpoint
            if use_checkpoints:
                checkpoint_name = f"{prefix}_chunk{i}_transcript.txt"
                cached = _load_checkpoint(checkpoint_name)
                if cached:
                    logger.info(
                        "Resuming from checkpoint: chunk %d/%d already transcribed (%d chars)",
                        i + 1, total_chunks, len(cached),
                    )
                    transcripts.append(cached)
                    pct = int((i + 1) / total_chunks * 100)
                    logger.info(
                        "Transcription %d%% — chunk %d/%d (from checkpoint)",
                        pct, i + 1, total_chunks,
                    )
                    continue

            # Pre-chunk budget check — stop before spending, not after
            if i > 0:  # Skip check on first chunk (pre-flight already checked)
                try:
                    from tools.core.cost_tracker import check_lecture_budget, LECTURE_COST_LIMIT_USD
                    pk = _pipeline_key_var.get()
                    _budget_ok, _budget_remaining = check_lecture_budget(pk)
                    estimated_cost = LECTURE_COST_LIMIT_USD - _budget_remaining
                    if estimated_cost > MAX_COST_PER_LECTURE:
                        full_transcript = "\n\n".join(transcripts)
                        logger.error(
                            "Cost budget EXCEEDED before chunk %d/%d: $%.2f > $%.2f limit. "
                            "Stopping to prevent further spend. Partial transcript saved.",
                            i + 1, total_chunks, estimated_cost, MAX_COST_PER_LECTURE,
                        )
                        if full_transcript:
                            partial_path = TMP_DIR / f"{Path(video_path).stem}_partial_transcript.txt"
                            partial_path.write_text(full_transcript, encoding="utf-8")
                            logger.info("Partial transcript saved to %s", partial_path)
                        raise RuntimeError(
                            f"Gemini cost budget exceeded: ${estimated_cost:.2f} > ${MAX_COST_PER_LECTURE:.2f}"
                        )
                except (ImportError, NameError):
                    pass

            logger.info(
                "Processing chunk %d/%d: %s", i + 1, total_chunks, chunk_path.name,
            )

            # Check if a Gemini file ref exists from a previous crashed run
            # (avoids re-uploading a video that's already on Google's servers)
            # NOTE: _load_checkpoint rejects <100 chars, so we read directly.
            _reused_upload = False
            if use_checkpoints:
                _ref_path = TMP_DIR / f"{prefix}_chunk{i}_fileref.txt"
                if _ref_path.exists():
                    _cached_ref = _ref_path.read_text(encoding="utf-8").strip()
                    _parts = _cached_ref.split("|")
                    if len(_parts) == 2:
                        _old_name, _old_tier = _parts[0], _parts[1] == "True"
                        try:
                            _client = _get_client(use_free=_old_tier)
                            _file_info = _client.files.get(name=_old_name)
                            _state_str = str(getattr(_file_info, "state", "")).upper()
                            if "ACTIVE" in _state_str:
                                logger.info(
                                    "Reusing Gemini file from previous run: %s (chunk %d/%d)",
                                    _old_name, i + 1, total_chunks,
                                )
                                file_ref = _file_info
                                use_free = _old_tier
                                uploaded_gemini_files.append((_old_name, _old_tier))
                                _reused_upload = True
                            else:
                                logger.info(
                                    "Previous Gemini file %s no longer active (state=%s) — re-uploading",
                                    _old_name, _state_str,
                                )
                        except Exception as e:
                            logger.info(
                                "Previous Gemini file %s not found — re-uploading: %s",
                                _old_name, e,
                            )

            if not _reused_upload:
                # Upload chunk to Gemini (only once — cache for retries)
                file_ref, use_free = upload_video(chunk_path, use_free=use_free)
                uploaded_gemini_files.append((file_ref.name, use_free))

                # Save Gemini file ref for crash recovery (avoid re-upload)
                if use_checkpoints:
                    _ref_path = TMP_DIR / f"{prefix}_chunk{i}_fileref.txt"
                    _ref_path.write_text(
                        f"{file_ref.name}|{use_free}", encoding="utf-8",
                    )

            # Transcribe with chunk context — retries reuse the cached file_ref
            # instead of re-uploading the video (saves $2-3 per retry).
            # If paid-tier quota is hit during transcription (not upload),
            # the file_ref belongs to the paid key's namespace and is
            # inaccessible from the free key — so we must re-upload.
            file_ref_name = file_ref.name if hasattr(file_ref, 'name') else str(file_ref)
            try:
                transcript = transcribe_video(
                    file_ref, use_free=use_free,
                    chunk_number=i, total_chunks=total_chunks,
                )
            except Exception as exc:
                if _is_quota_error(exc) and not use_free and GEMINI_API_KEY:
                    logger.warning(
                        "Paid tier quota hit during transcription of chunk %d — "
                        "re-uploading with free key and retrying...", i,
                    )
                    # Clean up paid-key upload
                    try:
                        paid_client = _get_client(use_free=False)
                        paid_client.files.delete(name=file_ref_name)
                        uploaded_gemini_files.pop()  # Remove from cleanup list
                        logger.info("Cleaned up paid-key Gemini file: %s", file_ref_name)
                    except Exception as del_err:
                        logger.warning("Failed to delete paid-key file %s: %s", file_ref_name, del_err)
                    # Re-upload with free key
                    file_ref, use_free = upload_video(chunk_path, use_free=True)
                    uploaded_gemini_files.append((file_ref.name if hasattr(file_ref, 'name') else str(file_ref), True))
                    # Update file ref checkpoint to reflect the new free-key upload
                    if use_checkpoints:
                        _ref_path = TMP_DIR / f"{prefix}_chunk{i}_fileref.txt"
                        _ref_path.write_text(
                            f"{file_ref.name}|{use_free}", encoding="utf-8",
                        )
                    transcript = transcribe_video(
                        file_ref, use_free=True,
                        chunk_number=i, total_chunks=total_chunks,
                    )
                else:
                    raise
            transcripts.append(transcript)

            # Save chunk checkpoint immediately after successful transcription
            if use_checkpoints:
                _save_checkpoint(checkpoint_name, transcript)

            pct = int((i + 1) / total_chunks * 100)
            logger.info(
                "Transcription %d%% — chunk %d/%d transcribed: %d chars",
                pct, i + 1, total_chunks, len(transcript),
            )

            # Budget check moved to top of loop (pre-chunk, not post-chunk)

            # Clean up Gemini file immediately (success path)
            try:
                client = _get_client(use_free=use_free)
                client.files.delete(name=file_ref.name)
                uploaded_gemini_files.pop()  # Remove from cleanup list
                logger.info("Cleaned up Gemini file: %s", file_ref.name)
            except Exception as e:
                logger.warning("Failed to delete Gemini file %s: %s", file_ref.name, e)

            # Clean up local chunk file (but not the original video)
            if chunk_path != Path(video_path) and chunk_path.exists():
                chunk_path.unlink()
                logger.info("Cleaned up local chunk: %s", chunk_path.name)

    finally:
        # Clean up any remaining Gemini uploads on failure
        for file_name, was_free in uploaded_gemini_files:
            try:
                client = _get_client(use_free=was_free)
                client.files.delete(name=file_name)
                logger.info("Cleaned up leaked Gemini file: %s", file_name)
            except Exception as e:
                logger.warning("Failed to clean up Gemini file %s: %s", file_name, e)

        # Clean up any remaining local chunk files on failure
        for chunk_path in chunks:
            if chunk_path != Path(video_path) and chunk_path.exists():
                try:
                    chunk_path.unlink()
                    logger.info("Cleaned up leaked chunk file: %s", chunk_path.name)
                except Exception as e:
                    logger.warning("Failed to clean up chunk %s: %s", chunk_path.name, e)

    full_transcript = "\n\n".join(transcripts)
    logger.info(
        "Full transcript assembled: %d chars from %d chunks",
        len(full_transcript), total_chunks,
    )
    return full_transcript, use_free


# ---------------------------------------------------------------------------
# Claude Reasoning (extended thinking for deep analysis)
# ---------------------------------------------------------------------------

def _claude_reason(
    transcript: str,
    prompt: str,
    purpose: str,
    max_tokens: int = 16000,
    budget_tokens: int = 10000,
) -> str:
    """Use Claude Opus 4.6 with extended thinking to reason about the transcript.

    Returns Claude's analysis in English (reasoning output), which will then
    be sent to Gemini for Georgian writing.
    """
    client = _get_anthropic_client()

    system_msg = (
        "You are an expert AI training analyst and pedagogy specialist. "
        "You will analyze a lecture transcript and provide deep, structured analysis. "
        "Think carefully and thoroughly about every aspect. "
        "Your analysis will be translated to Georgian by another model, "
        "so write clearly and structurally in English."
    )

    user_msg = f"{prompt}\n\nTRANSCRIPT:\n{transcript}"

    logger.info("Sending transcript to Claude Opus for %s reasoning...", purpose)

    max_attempts = 5  # More attempts for rate limits
    for attempt in range(1, max_attempts + 1):
        # Budget check before every attempt (including first)
        try:
            from tools.core.cost_tracker import check_lecture_budget
            _ok, _rem = check_lecture_budget(_pipeline_key_var.get())
            if not _ok:
                raise RuntimeError(f"Lecture budget exceeded during Claude retry (attempt {attempt})")
        except (ImportError, NameError):
            pass
        try:
            response = client.messages.create(
                model=ASSISTANT_CLAUDE_MODEL,
                max_tokens=max_tokens,
                timeout=600.0,  # 10 min timeout for long transcripts
                thinking={
                    "type": "enabled",
                    "budget_tokens": budget_tokens,
                },
                system=system_msg,
                messages=[{"role": "user", "content": user_msg}],
            )

            # Extract text blocks (skip thinking blocks)
            text_parts = [
                block.text for block in response.content
                if block.type == "text"
            ]
            result = "\n".join(text_parts)
            logger.info(
                "Claude %s reasoning complete (%d chars, %d input tokens, %d output tokens)",
                purpose, len(result),
                response.usage.input_tokens, response.usage.output_tokens,
            )

            # Log Claude API cost
            try:
                usage = getattr(response, 'usage', None)
                if usage:
                    input_tokens = getattr(usage, 'input_tokens', 0) or 0
                    output_tokens = getattr(usage, 'output_tokens', 0) or 0
                    # Sonnet 4.6 pricing: $3/M input, $15/M output
                    input_cost = (input_tokens / 1_000_000) * 3.0
                    output_cost = (output_tokens / 1_000_000) * 15.0
                    total_cost = input_cost + output_cost
                    logger.info(
                        "💰 Claude cost [%s] | input=%d tokens ($%.4f) | "
                        "output=%d tokens ($%.4f) | total=$%.4f",
                        purpose, input_tokens, input_cost,
                        output_tokens, output_cost, total_cost,
                    )
                    try:
                        from tools.core.cost_tracker import record_cost
                        record_cost(
                            service="claude",
                            model="claude-sonnet-4-6",
                            purpose=purpose,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cost_usd=total_cost,
                            pipeline_key=_pipeline_key_var.get(),
                        )
                    except Exception:
                        pass
            except Exception:
                pass

            return result

        except anthropic.RateLimitError as e:
            # Rate limits need long waits (30K tokens/min limit)
            # Exponential backoff: 65s, 130s, 195s, 260s, 325s
            delay = 65 * attempt
            logger.warning(
                "Claude %s rate limited (attempt %d/%d) — waiting %ds for reset...",
                purpose, attempt, max_attempts, delay,
            )
            if attempt == max_attempts:
                raise RuntimeError(
                    f"Claude {purpose} failed after {max_attempts} rate limit hits: {e}"
                ) from e
            time.sleep(delay)

        except Exception as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Claude %s attempt %d/%d failed: %s — retrying in %ds",
                purpose, attempt, max_attempts, e, delay,
            )
            if attempt == max_attempts:
                raise RuntimeError(
                    f"Claude {purpose} failed after {max_attempts} attempts: {e}"
                ) from e
            time.sleep(delay)

    raise RuntimeError("Unreachable")


def _gemini_write_georgian(claude_analysis: str, prompt: str, purpose: str, use_free: bool = False) -> str:
    """Take Claude's English analysis and write the final Georgian output with Gemini.

    Gemini 3.1 Pro excels at Georgian language — it takes Claude's structured
    analysis and produces fluent, professional Georgian text.
    """
    client = _get_client(use_free=use_free)

    writing_prompt = (
        f"{prompt}\n\n"
        "---\n"
        "ქვემოთ მოცემულია ექსპერტის ანალიზი ინგლისურად. "
        "გადმოეცი ეს ანალიზი სრულად და დეტალურად ქართულ ენაზე, "
        "შეინარჩუნე ორიგინალის სტრუქტურა და სიღრმე. "
        "არ გამოტოვო არცერთი მნიშვნელოვანი პუნქტი.\n\n"
        f"EXPERT ANALYSIS:\n{claude_analysis}"
    )

    return _generate_with_retry(
        client,
        model=GEMINI_MODEL_ANALYSIS,
        contents=[writing_prompt],
        purpose=f"{purpose} (Georgian writing)",
        max_output_tokens=32768,
        use_free=use_free,
        disable_thinking=False,  # Gemini 3.1 Pro requires thinking mode (Budget 0 is invalid)
    )


# ---------------------------------------------------------------------------
# Step 2 & 3: Dual-Model Analysis (Claude thinks, Gemini writes Georgian)
# ---------------------------------------------------------------------------

def _claude_reason_all(transcript: str) -> dict[str, str]:
    """Single Claude call that produces summary, gap analysis, and deep analysis.

    Sends the transcript once and asks Claude to produce all three analyses
    in clearly separated sections.  This saves ~$3/lecture vs three separate calls.

    Returns:
        Dict with keys 'summary', 'gap_analysis', 'deep_analysis' (English text).
    """
    combined_prompt = (
        "You are three experts in one: AI training analyst, pedagogy specialist, "
        "and Georgian business context consultant.\n\n"
        "Analyze the lecture transcript below and produce THREE clearly separated analyses.\n"
        "Use the EXACT section headers shown (they are used for parsing).\n\n"
        "===SUMMARY===\n"
        "Comprehensive lecture summary covering:\n"
        "1. Main topics discussed\n"
        "2. Key concepts and ideas explained\n"
        "3. Practical examples and demonstrations shown\n"
        "4. Key takeaways and conclusions\n"
        "5. Action items for participants\n"
        "Be detailed — someone who missed the lecture should understand the core material.\n\n"
        "===GAP_ANALYSIS===\n"
        "Critical quality analysis:\n"
        "1. Teaching Quality — clarity of explanations, vague/incomplete areas\n"
        "2. Critical Gaps — important topics missed or insufficiently covered\n"
        "3. Technical Accuracy — inaccuracies or outdated information\n"
        "4. Pedagogical Recommendations — structure, exercises, engagement\n"
        "5. Pacing and Time Management\n"
        "6. Recommendations for Next Lecture\n"
        "Be honest, constructive, and specific.\n\n"
        "===DEEP_ANALYSIS===\n"
        "PART I — Teaching Quality: points 1-6 from gap analysis (brief, avoid repetition).\n"
        "PART II — Global AI Trends Context:\n"
        "7. Compare against current global AI trends and leading trainers "
        "(Andrew Ng, DeepLearning.AI, Google, Microsoft, fast.ai).\n"
        "8. Market relevance for Georgian managers and businesses.\n"
        "9. Competitive analysis — 3-5 topics competitors teach that this course doesn't.\n"
        "10. Critical blind spots — crucial AI concepts/tools for 2025-2026 that are missing.\n"
        "PART III — Action Plan and Rating:\n"
        "11. 5-7 concrete action steps for the instructor before next lecture.\n"
        "12. Rate on 5 dimensions (1-10): Content Depth, Practical Value, "
        "Participant Engagement, Technical Accuracy, Market Relevance. Justify each.\n"
        "13. One most important critical message — direct and honest.\n\n"
        "Be analytical, honest, and strict. Gap and deep analyses are private — for the instructor only."
    )

    raw = _claude_reason(
        transcript,
        prompt=combined_prompt,
        purpose="combined analysis",
        max_tokens=48000,
        budget_tokens=16000,
    )

    # Parse the three sections — robust against minor variations
    import re
    sections: dict[str, str] = {}
    for key, header in [
        ("summary", "===SUMMARY==="),
        ("gap_analysis", "===GAP_ANALYSIS==="),
        ("deep_analysis", "===DEEP_ANALYSIS==="),
    ]:
        # Match exact header or regex variants (===SUMMARY=== or === SUMMARY ===)
        pattern = rf'={3,}\s*{key.upper().replace("_", "[_ ]")}\s*={3,}'
        match = re.search(pattern, raw)
        if match is None:
            # Fallback: try exact string match
            start = raw.find(header)
            if start == -1:
                logger.warning("Section %s not found in Claude response", header)
                sections[key] = ""
                continue
            start += len(header)
        else:
            start = match.end()

        # Find next section or end
        next_pattern = r'={3,}\s*(?:SUMMARY|GAP[_ ]ANALYSIS|DEEP[_ ]ANALYSIS)\s*={3,}'
        next_match = re.search(next_pattern, raw[start:])
        end = start + next_match.start() if next_match else len(raw)
        sections[key] = raw[start:end].strip()

    # Validate: all three sections must be present and non-trivial
    MIN_SECTION_CHARS = 100
    missing = [k for k, v in sections.items() if len(v.strip()) < MIN_SECTION_CHARS]
    if missing:
        logger.error(
            "Claude analysis has missing/empty sections: %s (lengths: %s)",
            missing,
            {k: len(v) for k, v in sections.items()},
        )
        raise ValueError(
            f"Claude analysis incomplete — missing sections: {', '.join(missing)}"
        )

    return sections


@resilient_api_call(service="gemini", operation="generate_summary", max_attempts=1, gemini_quota_fallback=True)
def generate_summary(transcript: str, use_free: bool = False) -> str:
    """Generate lecture summary: Claude reasons, Gemini writes Georgian."""
    claude_analysis = _claude_reason(
        transcript,
        prompt=(
            "Analyze this AI training lecture transcript thoroughly. Produce a comprehensive summary covering:\n"
            "1. Main topics discussed\n"
            "2. Key concepts and ideas explained\n"
            "3. Practical examples and demonstrations shown\n"
            "4. Key takeaways and conclusions\n"
            "5. Action items for participants\n\n"
            "Be detailed and precise. The summary should be comprehensive enough for "
            "someone who missed the lecture to understand the core material."
        ),
        purpose="summary",
    )
    return _gemini_write_georgian(claude_analysis, SUMMARIZATION_PROMPT, "summary", use_free)


@resilient_api_call(service="gemini", operation="generate_gap_analysis", max_attempts=1, gemini_quota_fallback=True)
def generate_gap_analysis(transcript: str, use_free: bool = False) -> str:
    """Generate gap analysis: Claude reasons, Gemini writes Georgian."""
    claude_analysis = _claude_reason(
        transcript,
        prompt=(
            "You are a critical AI training quality expert and pedagogy specialist. "
            "Analyze this lecture transcript with a critical eye:\n\n"
            "1. Teaching Quality — How clearly was material explained? Any vague/incomplete explanations?\n"
            "2. Critical Gaps — Important topics missed or insufficiently covered? Logical gaps?\n"
            "3. Technical Accuracy — Any inaccuracies or outdated information?\n"
            "4. Pedagogical Recommendations — How to improve structure, exercises, engagement?\n"
            "5. Pacing and Time Management — Too fast/slow? Optimal time distribution?\n"
            "6. Recommendations for Next Lecture — What to cover deeper? What to prepare?\n\n"
            "Be honest, constructive, and specific. The goal is continuous improvement."
        ),
        purpose="gap analysis",
    )
    return _gemini_write_georgian(claude_analysis, GAP_ANALYSIS_PROMPT, "gap analysis", use_free)


@resilient_api_call(service="gemini", operation="generate_deep_analysis", max_attempts=1, gemini_quota_fallback=True)
def generate_deep_analysis(transcript: str, use_free: bool = False) -> str:
    """Generate deep analysis with global AI context: Claude reasons, Gemini writes Georgian."""
    claude_analysis = _claude_reason(
        transcript,
        prompt=(
            "You are three experts in one: AI industry analyst, pedagogy specialist, "
            "and Georgian business context consultant. Perform a comprehensive analysis:\n\n"
            "PART I — Teaching Quality (traditional analysis)\n"
            "1-6. Teaching quality, critical gaps, technical accuracy, pedagogical "
            "recommendations, pacing, next lecture recommendations.\n\n"
            "PART II — Global AI Trends Context\n"
            "7. Compare lecture material against current global AI trends and latest developments. "
            "What are leading AI trainers (Andrew Ng, DeepLearning.AI, Google, Microsoft, fast.ai) "
            "teaching in similar courses? Where does this lecture fall short or exceed?\n"
            "8. Market relevance for Georgian context — how applicable for Georgian managers and businesses?\n"
            "9. Competitive analysis — 3-5 topics/skills competitors teach that this course doesn't.\n"
            "10. Critical blind spots — which AI concepts/tools are crucial in 2025-2026 but fully missing?\n\n"
            "PART III — Action Plan and Rating\n"
            "11. 5-7 concrete, measurable action steps for the instructor before next lecture.\n"
            "12. Rate the lecture on 5 dimensions (1-10): Content Depth, Practical Value, "
            "Participant Engagement, Technical Accuracy, Market Relevance. Justify each score.\n"
            "13. One most important critical message to the instructor — direct and honest.\n\n"
            "Be analytical, honest, and strict. This analysis is private — only for the instructor."
        ),
        purpose="deep analysis",
    )
    return _gemini_write_georgian(claude_analysis, DEEP_ANALYSIS_PROMPT, "deep analysis", use_free)


# ---------------------------------------------------------------------------
# Safe wrappers (used inside analyze_lecture to avoid try/except boilerplate)
# ---------------------------------------------------------------------------

def _safe_claude_reason_all(transcript: str) -> dict[str, str] | None:
    """Run combined Claude reasoning, returning None on failure (not empty dict)."""
    try:
        sections = _claude_reason_all(transcript)
        logger.info(
            "Combined Claude analysis complete: %s",
            {k: len(v) for k, v in sections.items()},
        )
        return sections
    except RuntimeError as e:
        if "budget" in str(e).lower():
            raise  # Budget errors must propagate — do not swallow
        logger.error("Combined Claude analysis failed: %s", e)
        try:
            from tools.integrations.whatsapp_sender import alert_operator
            alert_operator(f"Combined Claude analysis FAILED: {e}")
        except Exception:
            pass
        return None
    except Exception as e:
        logger.error("Combined Claude analysis failed: %s", e)
        try:
            from tools.integrations.whatsapp_sender import alert_operator
            alert_operator(f"Combined Claude analysis FAILED: {e}")
        except Exception:
            pass
        return None


def _safe_gemini_write_georgian(
    claude_text: str, prompt: str, label: str, *, use_free: bool = False,
) -> str:
    """Write Georgian text from Claude's English analysis, returning '' on failure."""
    try:
        return _gemini_write_georgian(claude_text, prompt, label, use_free=use_free)
    except RuntimeError as e:
        if "budget" in str(e).lower():
            raise  # Budget errors must propagate — do not swallow
        logger.error("Georgian writing failed: %s", e)
        try:
            from tools.integrations.whatsapp_sender import alert_operator
            alert_operator(f"Georgian writing FAILED: {e}")
        except Exception:
            pass
        return ""
    except Exception as e:
        logger.error("Georgian writing failed: %s", e)
        try:
            from tools.integrations.whatsapp_sender import alert_operator
            alert_operator(f"Georgian writing FAILED: {e}")
        except Exception:
            pass
        return ""


# ---------------------------------------------------------------------------
# Full Pipeline
# ---------------------------------------------------------------------------

def _checkpoint_prefix(group: int | None, lecture: int | None) -> str:
    """Return the checkpoint file prefix, or empty string if no group/lecture."""
    if group is not None and lecture is not None:
        return f"g{group}_l{lecture}"
    return ""


MIN_CHECKPOINT_CHARS = 100  # Same as MIN_MEANINGFUL_RESPONSE_CHARS


def _load_checkpoint(name: str) -> str | None:
    """Load a checkpoint file from .tmp/ if it exists and has meaningful content."""
    path = TMP_DIR / name
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if content and len(content.strip()) >= MIN_CHECKPOINT_CHARS:
            logger.info("Loaded checkpoint: %s (%d chars)", name, len(content))
            return content
        elif content:
            logger.warning(
                "Checkpoint %s too short (%d chars < %d) — will re-generate",
                name, len(content.strip()), MIN_CHECKPOINT_CHARS,
            )
    return None


def _save_checkpoint(name: str, content: str) -> None:
    """Save content to a checkpoint file in .tmp/ using atomic write."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TMP_DIR / name
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)  # Atomic on POSIX; replace() also works on Windows
    logger.info("Checkpoint saved: %s (%d chars)", name, len(content))


def cleanup_checkpoints(group: int, lecture: int) -> int:
    """Delete all checkpoint files for a given group/lecture.

    Called after the full pipeline succeeds (Drive upload + WhatsApp sent).
    Returns the number of files deleted.
    """
    prefix = _checkpoint_prefix(group, lecture)
    if not prefix:
        return 0

    deleted = 0
    for path in TMP_DIR.glob(f"{prefix}_*"):
        try:
            path.unlink()
            logger.info("Cleaned up checkpoint: %s", path.name)
            deleted += 1
        except OSError as e:
            logger.warning("Failed to delete checkpoint %s: %s", path.name, e)
    return deleted


def analyze_lecture(
    file_path: str | Path,
    existing_transcript: str | None = None,
    group: int | None = None,
    lecture: int | None = None,
) -> dict[str, str]:
    """Hybrid lecture analysis: video chunking → multimodal transcription → Claude+Gemini analysis.

    Pipeline:
    - Step 0: Split video into ~45-min chunks (ffmpeg, no re-encoding)
    - Step 1: Gemini 2.5 Pro transcribes each chunk multimodally (sees slides, demos)
    - Step 2: Single Claude Opus call → produces summary + gap + deep analysis (English)
    - Steps 3-5: Gemini writes each analysis in Georgian (3 separate calls, different prompts)

    Checkpoint/resume: When group and lecture are provided, intermediate results
    are saved to .tmp/ after each expensive step. On restart, completed steps
    are skipped automatically — saving $4-6 per failed pipeline restart.

    Args:
        file_path: Path to the video file.
        existing_transcript: If provided, skip transcription and use this text.
        group: Group number (for checkpoint filenames). Optional.
        lecture: Lecture number (for checkpoint filenames). Optional.

    Returns:
        Dict with keys: 'transcript', 'summary', 'gap_analysis', 'deep_analysis'
    """
    _pipeline_key = f"g{group}_l{lecture}" if group and lecture else ""
    _ctx_token = _pipeline_key_var.set(_pipeline_key)

    # Pre-flight budget check (only when pipeline key is set)
    if _pipeline_key:
        try:
            from tools.core.cost_tracker import check_daily_budget, check_lecture_budget
            daily_ok, daily_remaining = check_daily_budget()
            if not daily_ok:
                raise RuntimeError(
                    f"Daily budget exceeded ($0 remaining). Pipeline for G{group}/L{lecture} refused."
                )
            lecture_ok, lecture_remaining = check_lecture_budget(_pipeline_key)
            if not lecture_ok:
                raise RuntimeError(
                    f"Lecture budget exceeded for {_pipeline_key}. Pipeline refused."
                )
            logger.info("Budget check passed: daily=$%.2f remaining, lecture=$%.2f remaining",
                         daily_remaining, lecture_remaining)
        except RuntimeError:
            raise  # Budget exceeded — must propagate
        except Exception:
            pass  # cost_tracker not available or other non-fatal error

    file_path = Path(file_path)
    prefix = _checkpoint_prefix(group, lecture)
    use_checkpoints = bool(prefix)

    import time as _time
    _stage_times: dict[str, float] = {}
    _pipeline_start = _time.monotonic()

    # Total pipeline steps for progress logging:
    # 1. Transcription (chunked)  2. Claude reasoning  3. Summary  4. Gap  5. Deep
    total_steps = 5
    completed_steps = 0

    def _log_progress(step_name: str) -> None:
        nonlocal completed_steps
        completed_steps += 1
        pct = int(completed_steps / total_steps * 100)
        logger.info("Pipeline %d%% — %s complete", pct, step_name)

    try:
        # --- Step 1: Transcription ---
        _t0 = _time.monotonic()
        if existing_transcript:
            transcript = existing_transcript
            logger.info("Using existing transcript (%d chars)", len(transcript))
            _log_progress("transcription (pre-existing)")
        elif use_checkpoints and (cached := _load_checkpoint(f"{prefix}_full_transcript.txt")):
            transcript = cached
            logger.info("Resuming from checkpoint: full transcript (%d chars)", len(transcript))
            _log_progress("transcription (from checkpoint)")
        else:
            # Step 0+1: Split video into chunks and transcribe multimodally
            transcript, _use_free = transcribe_chunked_video(
                file_path, use_free=False, group=group, lecture=lecture,
            )
            logger.info("Full transcript length: %d chars", len(transcript))
            if use_checkpoints:
                _save_checkpoint(f"{prefix}_full_transcript.txt", transcript)
            _log_progress("transcription")

        _stage_times["transcription"] = _time.monotonic() - _t0
        results: dict[str, str] = {"transcript": transcript}

        # --- Step 2: Single Claude call for all three analyses ---
        _t0 = _time.monotonic()
        claude_checkpoint_name = f"{prefix}_claude_analysis.json" if use_checkpoints else ""
        claude_sections: dict[str, str] = {}

        if use_checkpoints and claude_checkpoint_name:
            cached_json = _load_checkpoint(claude_checkpoint_name)
            if cached_json:
                import json
                try:
                    claude_sections = json.loads(cached_json)
                    logger.info("Resuming from checkpoint: Claude analysis (%s)",
                               {k: len(v) for k, v in claude_sections.items()})
                except json.JSONDecodeError:
                    logger.warning("Corrupt Claude checkpoint — will re-run analysis")
                    claude_sections = {}

        if not claude_sections:
            claude_sections = _safe_claude_reason_all(transcript)
            if claude_sections is None:
                logger.error(
                    "Claude analysis failed completely — proceeding with empty sections "
                    "(quality gate in transcribe_lecture.py will catch this)"
                )
                claude_sections = {"summary": "", "gap_analysis": "", "deep_analysis": ""}
            if use_checkpoints and claude_sections:
                import json
                _save_checkpoint(claude_checkpoint_name, json.dumps(claude_sections, ensure_ascii=False))

        _stage_times["claude_reasoning"] = _time.monotonic() - _t0
        _log_progress("Claude reasoning")

        # --- Steps 3-5: Gemini writes Georgian from each Claude section ---
        analysis_configs = [
            ("summary", SUMMARIZATION_PROMPT, "summary"),
            ("gap_analysis", GAP_ANALYSIS_PROMPT, "gap analysis"),
            ("deep_analysis", DEEP_ANALYSIS_PROMPT, "deep analysis"),
        ]
        for key, prompt, label in analysis_configs:
            checkpoint_name = f"{prefix}_{key}.txt" if use_checkpoints else ""

            # Try checkpoint first
            if use_checkpoints and checkpoint_name:
                cached = _load_checkpoint(checkpoint_name)
                if cached:
                    logger.info("Resuming from checkpoint: %s (%d chars)", label, len(cached))
                    results[key] = cached
                    _log_progress(label)
                    continue

            claude_text = claude_sections.get(key, "")
            if not claude_text:
                logger.warning("Skipping %s — no Claude output", label)
                results[key] = ""
                _log_progress(f"{label} (skipped)")
                continue

            _t0 = _time.monotonic()
            georgian_text = _safe_gemini_write_georgian(claude_text, prompt, label, use_free=False)
            _stage_times["gemini_writing"] = _stage_times.get("gemini_writing", 0) + (_time.monotonic() - _t0)
            results[key] = georgian_text

            if use_checkpoints and georgian_text:
                _save_checkpoint(checkpoint_name, georgian_text)

            _log_progress(label)

        total = _time.monotonic() - _pipeline_start
        logger.info(
            "⏱️ Analysis timing: total=%.0fs | %s",
            total,
            " | ".join(f"{k}={v:.0f}s" for k, v in _stage_times.items()),
        )

        return results
    finally:
        # Reset the pipeline key for this context so it doesn't leak into
        # any code that runs in the same thread/task after this function returns.
        _pipeline_key_var.reset(_ctx_token)


# ---------------------------------------------------------------------------
# CLI entrypoint for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python -m tools.integrations.gemini_analyzer <video_file_path>")
        sys.exit(1)

    video_path = sys.argv[1]
    print(f"Analyzing lecture video: {video_path}")

    results = analyze_lecture(video_path)

    print("\n" + "=" * 60)
    print("TRANSCRIPT (first 1000 chars):")
    print("=" * 60)
    print(results["transcript"][:1000] + "...")

    print("\n" + "=" * 60)
    print("SUMMARY:")
    print("=" * 60)
    print(results["summary"])

    print("\n" + "=" * 60)
    print("GAP ANALYSIS:")
    print("=" * 60)
    print(results["gap_analysis"])

    print("\n" + "=" * 60)
    print("DEEP ANALYSIS:")
    print("=" * 60)
    print(results["deep_analysis"])
