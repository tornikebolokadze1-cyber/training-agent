"""Quality gates for the transcription and analysis pipeline.

Validates outputs at each pipeline stage to prevent garbage data from
propagating through Drive uploads, WhatsApp notifications, and Pinecone
indexing.

Each gate returns a QualityResult with pass/fail status and details.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Georgian character detection
# ---------------------------------------------------------------------------

# Unicode range for Georgian script: U+10A0–U+10FF (Mkhedruli + Asomtavruli)
# Also U+2D00–U+2D2F (Mtavruli supplement)
_GEORGIAN_PATTERN = re.compile(r"[\u10A0-\u10FF\u2D00-\u2D2F]")


def _count_georgian_chars(text: str) -> int:
    """Count the number of Georgian script characters in text."""
    return len(_GEORGIAN_PATTERN.findall(text))


def _georgian_ratio(text: str) -> float:
    """Return the ratio of Georgian characters to total alphabetic characters."""
    if not text:
        return 0.0
    alpha_chars = sum(1 for c in text if c.isalpha())
    if alpha_chars == 0:
        return 0.0
    return _count_georgian_chars(text) / alpha_chars


# ---------------------------------------------------------------------------
# Repetition detection
# ---------------------------------------------------------------------------

def _repetition_ratio(text: str, window_size: int = 200) -> float:
    """Estimate how much of the text is repeated content.

    Splits text into windows and checks how many are duplicates.
    Gemini sometimes enters a loop producing the same paragraph over and over.

    Returns a ratio between 0.0 (no repetition) and 1.0 (all repeated).
    """
    if not text or len(text) < window_size * 2:
        return 0.0

    windows: list[str] = []
    for i in range(0, len(text) - window_size + 1, window_size):
        window = text[i:i + window_size].strip()
        if window:
            windows.append(window)

    if len(windows) <= 1:
        return 0.0

    unique_windows = set(windows)
    return 1.0 - (len(unique_windows) / len(windows))


# ---------------------------------------------------------------------------
# Quality result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityResult:
    """Result of a quality gate check."""

    passed: bool
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metrics: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def failure_summary(self) -> str:
        """Human-readable summary of failures."""
        if not self.failures:
            return "All checks passed"
        return "; ".join(self.failures)


# ---------------------------------------------------------------------------
# Gate 1: Transcript quality
# ---------------------------------------------------------------------------

# Thresholds
MIN_TRANSCRIPT_CHARS = 1000
MIN_GEORGIAN_RATIO = 0.15  # At least 15% Georgian chars (mixed with tech terms)
MAX_REPETITION_RATIO = 0.50  # No more than 50% repeated content


def validate_transcript(text: str | None) -> QualityResult:
    """Validate transcript quality after Gemini transcription.

    Checks:
    - Not empty/None
    - Length > 1000 characters (a 2-hour lecture produces 50K+ chars)
    - Contains Georgian characters (lectures are in Georgian)
    - No more than 50% repetition (Gemini loop detection)
    """
    failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, float | int | str] = {}

    if not text:
        return QualityResult(
            passed=False,
            failures=("Transcript is empty or None",),
            metrics={"length": 0},
        )

    text_stripped = text.strip()
    length = len(text_stripped)
    metrics["length"] = length

    if length < MIN_TRANSCRIPT_CHARS:
        failures.append(
            f"Transcript too short: {length} chars (minimum {MIN_TRANSCRIPT_CHARS})"
        )

    geo_ratio = _georgian_ratio(text_stripped)
    geo_count = _count_georgian_chars(text_stripped)
    metrics["georgian_ratio"] = round(geo_ratio, 3)
    metrics["georgian_chars"] = geo_count

    if geo_ratio < MIN_GEORGIAN_RATIO:
        failures.append(
            f"Insufficient Georgian content: {geo_ratio:.1%} Georgian characters "
            f"({geo_count} chars, need >{MIN_GEORGIAN_RATIO:.0%})"
        )

    rep_ratio = _repetition_ratio(text_stripped)
    metrics["repetition_ratio"] = round(rep_ratio, 3)

    if rep_ratio > MAX_REPETITION_RATIO:
        failures.append(
            f"Excessive repetition: {rep_ratio:.1%} of content is repeated "
            f"(max {MAX_REPETITION_RATIO:.0%})"
        )
    elif rep_ratio > 0.30:
        warnings.append(f"Moderate repetition detected: {rep_ratio:.1%}")

    return QualityResult(
        passed=len(failures) == 0,
        failures=tuple(failures),
        warnings=tuple(warnings),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Gate 2: Claude analysis validation
# ---------------------------------------------------------------------------

MIN_CLAUDE_RESPONSE_CHARS = 200
EXPECTED_CLAUDE_SECTIONS = ("summary", "gap_analysis", "deep_analysis")


def validate_claude_analysis(sections: dict[str, str] | None) -> QualityResult:
    """Validate Claude's combined analysis output.

    Checks:
    - Response is not None or empty dict
    - Each section has > 200 characters
    - All expected sections are present
    """
    failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, float | int | str] = {}

    if sections is None:
        return QualityResult(
            passed=False,
            failures=("Claude analysis is None",),
            metrics={"sections_count": 0},
        )

    metrics["sections_count"] = len(sections)

    for section_key in EXPECTED_CLAUDE_SECTIONS:
        text = sections.get(section_key, "")
        section_len = len(text.strip()) if text else 0
        metrics[f"{section_key}_length"] = section_len

        if not text or section_len == 0:
            failures.append(f"Claude section '{section_key}' is empty")
        elif section_len < MIN_CLAUDE_RESPONSE_CHARS:
            failures.append(
                f"Claude section '{section_key}' too short: "
                f"{section_len} chars (minimum {MIN_CLAUDE_RESPONSE_CHARS})"
            )

    return QualityResult(
        passed=len(failures) == 0,
        failures=tuple(failures),
        warnings=tuple(warnings),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Gate 3: Summary document quality (before Drive upload)
# ---------------------------------------------------------------------------

MIN_SUMMARY_DOC_CHARS = 500


def validate_summary_document(
    content: str | None,
    group_number: int,
    lecture_number: int,
) -> QualityResult:
    """Validate summary document quality before uploading to Drive.

    Checks:
    - Content > 500 characters
    - Contains the lecture number
    - Contains the group name or number
    - Gap analysis section exists (even if "no gaps found")
    """
    failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, float | int | str] = {}

    if not content:
        return QualityResult(
            passed=False,
            failures=("Summary document content is empty or None",),
            metrics={"length": 0},
        )

    content_stripped = content.strip()
    length = len(content_stripped)
    metrics["length"] = length

    if length < MIN_SUMMARY_DOC_CHARS:
        failures.append(
            f"Summary too short: {length} chars (minimum {MIN_SUMMARY_DOC_CHARS})"
        )

    # Check for lecture number reference
    lecture_patterns = [
        f"#{lecture_number}",
        f"ლექცია {lecture_number}",
        f"Lecture {lecture_number}",
        f"ლექცია #{lecture_number}",
    ]
    has_lecture_ref = any(pat in content_stripped for pat in lecture_patterns)
    metrics["has_lecture_reference"] = has_lecture_ref
    if not has_lecture_ref:
        warnings.append(
            f"Summary does not reference lecture number {lecture_number}"
        )

    # Check for group reference
    group_patterns = [
        f"ჯგუფი #{group_number}",
        f"ჯგუფი {group_number}",
        f"Group {group_number}",
        f"Group #{group_number}",
    ]
    has_group_ref = any(pat in content_stripped for pat in group_patterns)
    metrics["has_group_reference"] = has_group_ref
    if not has_group_ref:
        warnings.append(
            f"Summary does not reference group {group_number}"
        )

    return QualityResult(
        passed=len(failures) == 0,
        failures=tuple(failures),
        warnings=tuple(warnings),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Gate 4: Gap analysis quality (before Drive upload)
# ---------------------------------------------------------------------------

MIN_GAP_ANALYSIS_CHARS = 300


def validate_gap_analysis(content: str | None) -> QualityResult:
    """Validate gap analysis content exists and is non-trivial."""
    failures: list[str] = []
    metrics: dict[str, float | int | str] = {}

    if not content:
        return QualityResult(
            passed=False,
            failures=("Gap analysis content is empty or None",),
            metrics={"length": 0},
        )

    length = len(content.strip())
    metrics["length"] = length

    if length < MIN_GAP_ANALYSIS_CHARS:
        failures.append(
            f"Gap analysis too short: {length} chars (minimum {MIN_GAP_ANALYSIS_CHARS})"
        )

    return QualityResult(
        passed=len(failures) == 0,
        failures=tuple(failures),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Gate 5: End-to-end pipeline validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PipelineOutputs:
    """Expected outputs from the full pipeline."""

    transcript_path_exists: bool = False
    summary_doc_url: str | None = None
    gap_analysis_text: str | None = None
    deep_analysis_text: str | None = None
    pinecone_vectors_indexed: int = 0


def validate_pipeline_outputs(outputs: PipelineOutputs) -> QualityResult:
    """Validate all expected outputs exist before marking pipeline COMPLETE.

    If any output is missing, the pipeline should be marked PARTIAL_COMPLETE
    (still deliver what's available).
    """
    failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, float | int | str] = {}

    if not outputs.transcript_path_exists:
        failures.append("Transcript file missing from .tmp/")

    if not outputs.summary_doc_url:
        failures.append("Summary document URL missing (Drive upload may have failed)")

    if not outputs.gap_analysis_text:
        failures.append("Gap analysis text is empty")

    if not outputs.deep_analysis_text:
        failures.append("Deep analysis text is empty")

    if outputs.pinecone_vectors_indexed == 0:
        warnings.append("No Pinecone vectors indexed")

    metrics["outputs_present"] = 5 - len(failures)
    metrics["outputs_total"] = 5
    metrics["pinecone_vectors"] = outputs.pinecone_vectors_indexed

    return QualityResult(
        passed=len(failures) == 0,
        failures=tuple(failures),
        warnings=tuple(warnings),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Gemini safety filter classification
# ---------------------------------------------------------------------------

SAFETY_FINISH_REASON = "SAFETY"
RECITATION_FINISH_REASON = "RECITATION"


def classify_finish_reason(finish_reason: str | None) -> str | None:
    """Classify a Gemini finish reason as SAFETY, RECITATION, or None (normal).

    Returns the classification string or None if the finish reason is normal.
    """
    if not finish_reason:
        return None
    upper = str(finish_reason).upper()
    if "SAFETY" in upper:
        return SAFETY_FINISH_REASON
    if "RECITATION" in upper:
        return RECITATION_FINISH_REASON
    return None
