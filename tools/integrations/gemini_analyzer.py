"""Gemini lecture analysis: hybrid model pipeline.

Multimodal transcription with Gemini 2.5 Pro (video chunked into ~45min
segments via ffmpeg to fit within 1M token limit — preserves slides,
demos, and screen shares), then Claude Opus reasoning + Gemini Georgian
writing for analysis.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from google import genai
from google.genai import types

import anthropic

from tools.config import (
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
    GEMINI_MODEL_TRANSCRIPTION,
    GEMINI_MODEL_ANALYSIS,
    ASSISTANT_CLAUDE_MODEL,
    TMP_DIR,
    TRANSCRIPTION_PROMPT,
    TRANSCRIPTION_CONTINUATION_PROMPT,
    SUMMARIZATION_PROMPT,
    GAP_ANALYSIS_PROMPT,
    DEEP_ANALYSIS_PROMPT,
)

from tools.retry import safe_operation

logger = logging.getLogger(__name__)

# File processing states
STATE_ACTIVE = "ACTIVE"
STATE_FAILED = "FAILED"

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds
FILE_POLL_INTERVAL = 10  # seconds
FILE_POLL_TIMEOUT = 600  # 10 minutes max wait for processing


def _is_quota_error(error: Exception) -> bool:
    """Check if an error is a quota/rate-limit issue (switchable to paid key)."""
    error_str = str(error).lower()
    quota_indicators = ["429", "resource exhausted", "quota", "rate limit", "too many requests"]
    return any(indicator in error_str for indicator in quota_indicators)


_client_cache: dict[str, genai.Client] = {}  # keyed by API key

_anthropic_client_cache: anthropic.Anthropic | None = None


def _get_anthropic_client() -> anthropic.Anthropic:
    """Return a cached Anthropic client (avoids creating fresh clients per call)."""
    global _anthropic_client_cache
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


# ---------------------------------------------------------------------------
# Video Chunking (multimodal — keeps video frames for slides/demos)
# ---------------------------------------------------------------------------

CHUNK_DURATION_MINUTES = 45  # ~697K video tokens per chunk (safe under 1M limit)


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

    Logs a warning (instead of raising) for paths outside TMP_DIR to avoid
    breaking CLI usage and tests while still flagging suspicious paths.
    """
    resolved = video_path.resolve()
    tmp_resolved = TMP_DIR.resolve()
    if not str(resolved).startswith(str(tmp_resolved)):
        logger.warning("Media path outside TMP_DIR: %s", resolved)
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
    logger.info(
        "Video is %.0f min — splitting into %d chunks of ~%d min each.",
        duration / 60, num_chunks, CHUNK_DURATION_MINUTES,
    )

    chunk_paths: list[Path] = []
    min_chunk_size = 1024 * 100  # 100 KB minimum for a valid chunk
    for i in range(num_chunks):
        start = i * chunk_seconds
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
            "-t", str(chunk_seconds),
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

def _generate_with_retry(
    client: genai.Client,
    model: str,
    contents: list,
    purpose: str,
    max_output_tokens: int = 8192,
    use_free: bool = False,
) -> str:
    """Call generate_content with retry logic and free-tier fallback.

    Primary: paid key. On quota/rate-limit errors, falls back to the free key.
    Returns the generated text.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            tier = "free" if use_free else "paid"
            logger.info("Generating %s with %s (attempt %d/%d, %s tier)...",
                        purpose, model, attempt, MAX_RETRIES, tier)

            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=max_output_tokens,
                ),
            )

            # response.text raises ValueError if blocked by safety filters
            try:
                text = response.text
            except (ValueError, AttributeError) as resp_err:
                raise ValueError(
                    f"Gemini response blocked or empty for {purpose}: {resp_err}"
                ) from resp_err
            if not text:
                raise ValueError(f"Empty response for {purpose}")

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

            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "%s attempt %d failed: %s — retrying in %ds",
                purpose, attempt, e, delay,
            )
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"{purpose} failed after {MAX_RETRIES} attempts: {e}") from e
            time.sleep(delay)

    raise RuntimeError("Unreachable")


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
        max_output_tokens=65536,
        use_free=use_free,
    )


def transcribe_chunked_video(
    video_path: str | Path,
    use_free: bool = False,
) -> tuple[str, bool]:
    """Split video into chunks if needed, transcribe each multimodally, concatenate.

    Handles the full flow: split → upload → transcribe → cleanup → join.
    Uses try/finally to ensure Gemini uploads and local chunk files are
    cleaned up even when a mid-pipeline failure occurs.

    Args:
        video_path: Path to the original video file.
        use_free: Whether to start with the free API key.

    Returns:
        Tuple of (full transcript text, whether free tier was used).
    """
    chunks = split_video_chunks(video_path)
    total_chunks = len(chunks)
    transcripts: list[str] = []

    # Track resources for cleanup on failure
    uploaded_gemini_files: list[tuple[str, bool]] = []  # (file_name, use_free)

    try:
        for i, chunk_path in enumerate(chunks):
            logger.info(
                "Processing chunk %d/%d: %s", i + 1, total_chunks, chunk_path.name,
            )

            # Upload chunk to Gemini
            file_ref, use_free = upload_video(chunk_path, use_free=use_free)
            uploaded_gemini_files.append((file_ref.name, use_free))

            # Transcribe with chunk context
            transcript = transcribe_video(
                file_ref, use_free=use_free,
                chunk_number=i, total_chunks=total_chunks,
            )
            transcripts.append(transcript)
            logger.info(
                "Chunk %d/%d transcribed: %d chars",
                i + 1, total_chunks, len(transcript),
            )

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
        max_tokens=32000,
        budget_tokens=16000,
    )

    # Parse the three sections
    sections: dict[str, str] = {}
    for key, header in [
        ("summary", "===SUMMARY==="),
        ("gap_analysis", "===GAP_ANALYSIS==="),
        ("deep_analysis", "===DEEP_ANALYSIS==="),
    ]:
        start = raw.find(header)
        if start == -1:
            logger.warning("Section %s not found in Claude response", header)
            sections[key] = ""
            continue
        start += len(header)
        # Find next section or end
        next_headers = [raw.find(h, start) for h in ["===SUMMARY===", "===GAP_ANALYSIS===", "===DEEP_ANALYSIS==="] if raw.find(h, start) != -1]
        end = min(next_headers) if next_headers else len(raw)
        sections[key] = raw[start:end].strip()

    return sections


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

@safe_operation("Combined Claude analysis", alert=True, default={})
def _safe_claude_reason_all(transcript: str) -> dict[str, str]:
    """Run combined Claude reasoning, returning empty dict on failure."""
    sections = _claude_reason_all(transcript)
    logger.info(
        "Combined Claude analysis complete: %s",
        {k: len(v) for k, v in sections.items()},
    )
    return sections


@safe_operation("Georgian writing", alert=True, default="")
def _safe_gemini_write_georgian(
    claude_text: str, prompt: str, label: str, *, use_free: bool = False,
) -> str:
    """Write Georgian text from Claude's English analysis, returning '' on failure."""
    return _gemini_write_georgian(claude_text, prompt, label, use_free=use_free)


# ---------------------------------------------------------------------------
# Full Pipeline
# ---------------------------------------------------------------------------

def analyze_lecture(
    file_path: str | Path,
    existing_transcript: str | None = None,
) -> dict[str, str]:
    """Hybrid lecture analysis: video chunking → multimodal transcription → Claude+Gemini analysis.

    Pipeline:
    - Step 0: Split video into ~45-min chunks (ffmpeg, no re-encoding)
    - Step 1: Gemini 2.5 Pro transcribes each chunk multimodally (sees slides, demos)
    - Step 2: Single Claude Opus call → produces summary + gap + deep analysis (English)
    - Steps 3-5: Gemini writes each analysis in Georgian (3 separate calls, different prompts)

    Args:
        file_path: Path to the video file.
        existing_transcript: If provided, skip transcription and use this text.

    Returns:
        Dict with keys: 'transcript', 'summary', 'gap_analysis', 'deep_analysis'
    """
    file_path = Path(file_path)

    if existing_transcript:
        transcript = existing_transcript
        logger.info("Using existing transcript (%d chars)", len(transcript))
    else:
        # Step 0+1: Split video into chunks and transcribe multimodally
        transcript, _use_free = transcribe_chunked_video(file_path, use_free=False)
        logger.info("Full transcript length: %d chars", len(transcript))

    results: dict[str, str] = {"transcript": transcript}

    # Step 2: Single Claude call for all three analyses (saves ~$3/lecture)
    claude_sections = _safe_claude_reason_all(transcript)

    # Steps 3-5: Gemini writes Georgian from each Claude section
    analysis_configs = [
        ("summary", SUMMARIZATION_PROMPT, "summary"),
        ("gap_analysis", GAP_ANALYSIS_PROMPT, "gap analysis"),
        ("deep_analysis", DEEP_ANALYSIS_PROMPT, "deep analysis"),
    ]
    for key, prompt, label in analysis_configs:
        claude_text = claude_sections.get(key, "")
        if not claude_text:
            logger.warning("Skipping %s — no Claude output", label)
            results[key] = ""
            continue
        results[key] = _safe_gemini_write_georgian(claude_text, prompt, label, use_free=False)

    return results


# ---------------------------------------------------------------------------
# CLI entrypoint for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python -m tools.gemini_analyzer <video_file_path>")
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
