"""Gemini lecture analysis: hybrid model pipeline.

Cost-optimized: 2.5 Flash for video transcription (~$1/lecture),
3.1 Pro for text analysis (~$1.30/lecture). Total ~$2.30/lecture.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from google import genai
from google.genai import types

from tools.config import (
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
    GEMINI_MODEL_TRANSCRIPTION,
    GEMINI_MODEL_ANALYSIS,
    GEMINI_MODEL_DEEP_ANALYSIS,
    TRANSCRIPTION_PROMPT,
    SUMMARIZATION_PROMPT,
    GAP_ANALYSIS_PROMPT,
    DEEP_ANALYSIS_PROMPT,
)

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


def _get_client(use_free: bool = False) -> genai.Client:
    """Create and return a Gemini API client.

    Primary: paid key (billing-enabled, higher limits).
    Fallback: free key (if paid key fails or is not configured).
    """
    if use_free:
        if not GEMINI_API_KEY:
            raise RuntimeError("Free Gemini API key not configured — set GEMINI_API_KEY in .env")
        logger.info("Using FREE Gemini API key (fallback)")
        return genai.Client(api_key=GEMINI_API_KEY)
    if not GEMINI_API_KEY_PAID:
        logger.warning("Paid key not configured — falling back to free key")
        return genai.Client(api_key=GEMINI_API_KEY)
    return genai.Client(api_key=GEMINI_API_KEY_PAID)


# ---------------------------------------------------------------------------
# Video Upload
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

    Raises RuntimeError if processing fails or times out.
    """
    logger.info("Waiting for Gemini to process file '%s'...", file_name)
    elapsed = 0

    while elapsed < FILE_POLL_TIMEOUT:
        file_info = client.files.get(name=file_name)
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

            text = response.text
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


def transcribe_video(file_ref: object, use_free: bool = False) -> str:
    """Transcribe a video lecture using Gemini 2.5 Flash (cheap, fast, multimodal).

    Args:
        file_ref: The uploaded file object from upload_video().
        use_free: Whether to use the free API key (default: paid).

    Returns:
        Full Georgian transcript with timestamps and speaker markers.
    """
    client = _get_client(use_free=use_free)
    return _generate_with_retry(
        client,
        model=GEMINI_MODEL_TRANSCRIPTION,
        contents=[file_ref, TRANSCRIPTION_PROMPT],
        purpose="transcription",
        max_output_tokens=65536,  # Transcripts can be very long for 2hr lectures
        use_free=use_free,
    )


# ---------------------------------------------------------------------------
# Step 2 & 3: Text Analysis with 3.1 Pro (smartest model, text-only = cheap)
# ---------------------------------------------------------------------------

def generate_summary(transcript: str, use_free: bool = False) -> str:
    """Generate a lecture summary using Gemini 3.1 Pro (text-only, deep analysis).

    Args:
        transcript: Full lecture transcript text.
        use_free: Whether to use the free API key (default: paid).

    Returns:
        Lecture summary text in Georgian.
    """
    client = _get_client(use_free=use_free)
    return _generate_with_retry(
        client,
        model=GEMINI_MODEL_ANALYSIS,
        contents=[SUMMARIZATION_PROMPT + transcript],
        purpose="summary",
        max_output_tokens=16384,
        use_free=use_free,
    )


def generate_gap_analysis(transcript: str, use_free: bool = False) -> str:
    """Generate gap analysis using Gemini 3.1 Pro (text-only, deep analysis).

    Args:
        transcript: Full lecture transcript text.
        use_free: Whether to use the free API key (default: paid).

    Returns:
        Gap analysis text in Georgian (private — for instructor only).
    """
    client = _get_client(use_free=use_free)
    return _generate_with_retry(
        client,
        model=GEMINI_MODEL_ANALYSIS,
        contents=[GAP_ANALYSIS_PROMPT + transcript],
        purpose="gap analysis",
        max_output_tokens=16384,
        use_free=use_free,
    )


def generate_deep_analysis(transcript: str, use_free: bool = False) -> str:
    """Generate deep analysis with global AI trends context using Gemini 3.1 Pro.

    This is the comprehensive analysis that compares the lecture against
    world-class AI training standards, identifies blind spots, and provides
    a scored rubric. Sent privately to Tornike only.

    Args:
        transcript: Full lecture transcript text.
        use_free: Whether to use the free API key (default: paid).

    Returns:
        Deep analysis text in Georgian (private — for instructor only).
    """
    client = _get_client(use_free=use_free)
    return _generate_with_retry(
        client,
        model=GEMINI_MODEL_DEEP_ANALYSIS,
        contents=[DEEP_ANALYSIS_PROMPT + transcript],
        purpose="deep analysis (global AI context)",
        max_output_tokens=32768,
        use_free=use_free,
    )


# ---------------------------------------------------------------------------
# Full Pipeline
# ---------------------------------------------------------------------------

def analyze_lecture(file_path: str | Path) -> dict[str, str]:
    """Hybrid lecture analysis: 2.5 Flash transcribes video, 3.1 Pro analyzes text.

    Cost-optimized pipeline (~$2.30 per 2-hour lecture):
    - Step 1: 2.5 Flash watches the video and transcribes (~$1.00)
    - Step 2: 3.1 Pro reads transcript and writes summary (~$0.56)
    - Step 3: 3.1 Pro reads transcript and writes gap analysis (~$0.74)

    Args:
        file_path: Path to the video file.

    Returns:
        Dict with keys: 'transcript', 'summary', 'gap_analysis', 'file_name'
    """
    # Step 1: Upload video and transcribe with 2.5 Flash (cheap multimodal)
    file_ref, use_free = upload_video(file_path)
    transcript = transcribe_video(file_ref, use_free=use_free)
    logger.info("Transcript length: %d chars", len(transcript))

    # Clean up Gemini file immediately — video no longer needed
    try:
        client = _get_client(use_free=use_free)
        client.files.delete(name=file_ref.name)
        logger.info("Cleaned up Gemini file: %s", file_ref.name)
    except Exception as e:
        logger.warning("Failed to delete Gemini file %s: %s", file_ref.name, e)

    # Step 2: Summary with 3.1 Pro (text-only, smartest analysis)
    summary = generate_summary(transcript, use_free=use_free)

    # Step 3: Gap analysis with 3.1 Pro (text-only, deep teaching insights)
    gap_analysis = generate_gap_analysis(transcript, use_free=use_free)

    # Step 4: Deep analysis with global AI context (text-only, highest reasoning)
    deep_analysis = generate_deep_analysis(transcript, use_free=use_free)

    return {
        "transcript": transcript,
        "summary": summary,
        "gap_analysis": gap_analysis,
        "deep_analysis": deep_analysis,
        "file_name": file_ref.name,
    }


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
