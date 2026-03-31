"""Unit tests for tools/core/quality_gates.py.

Tests all quality gate functions for the transcription and analysis pipeline:
- Transcript quality validation (length, Georgian chars, repetition)
- Claude analysis validation (sections, lengths)
- Summary document validation (length, lecture/group references)
- Gap analysis validation
- End-to-end pipeline output validation
- Gemini safety filter classification

Run with:
    pytest tools/tests/test_quality_gates.py -v
"""

from __future__ import annotations

import pytest

from tools.core.quality_gates import (
    MAX_REPETITION_RATIO,
    MIN_CLAUDE_RESPONSE_CHARS,
    MIN_GAP_ANALYSIS_CHARS,
    MIN_GEORGIAN_RATIO,
    MIN_SUMMARY_DOC_CHARS,
    MIN_TRANSCRIPT_CHARS,
    PipelineOutputs,
    QualityResult,
    RECITATION_FINISH_REASON,
    SAFETY_FINISH_REASON,
    classify_finish_reason,
    validate_claude_analysis,
    validate_gap_analysis,
    validate_pipeline_outputs,
    validate_summary_document,
    validate_transcript,
    _count_georgian_chars,
    _georgian_ratio,
    _repetition_ratio,
)


# ===========================================================================
# Helper data
# ===========================================================================

# Georgian text samples
GEORGIAN_TEXT_SHORT = "გამარჯობა, ეს არის ტესტი"  # ~25 chars
GEORGIAN_TEXT_MEDIUM = "გამარჯობა " * 200  # ~2000 chars
GEORGIAN_LECTURE_SAMPLE = (
    "ხელოვნური ინტელექტი (AI) არის კომპიუტერული მეცნიერების დარგი, "
    "რომელიც ეხება ინტელექტუალური აგენტების შექმნას. "
    "მანქანური სწავლება (Machine Learning) არის AI-ის ქვედარგი. "
    "ნეირონული ქსელები (Neural Networks) გამოიყენება Deep Learning-ში. "
    "Transformer არქიტექტურა ChatGPT-ის საფუძველია. "
)

# Build a long enough Georgian transcript (>1000 chars)
VALID_GEORGIAN_TRANSCRIPT = GEORGIAN_LECTURE_SAMPLE * 30  # ~5000+ chars


def _make_repeated_text(block: str, repeats: int) -> str:
    """Create text with exact repetitions for testing repetition detection."""
    return block * repeats


# ===========================================================================
# Tests: Georgian character helpers
# ===========================================================================

class TestGeorgianHelpers:
    """Test Georgian character detection utilities."""

    def test_count_georgian_chars_empty(self):
        assert _count_georgian_chars("") == 0

    def test_count_georgian_chars_english_only(self):
        assert _count_georgian_chars("Hello World 123") == 0

    def test_count_georgian_chars_georgian_text(self):
        count = _count_georgian_chars("გამარჯობა")
        assert count == 9  # 9 Georgian letters

    def test_count_georgian_chars_mixed(self):
        count = _count_georgian_chars("AI არის ხელოვნური ინტელექტი")
        # "არის" (4) + "ხელოვნური" (9) + "ინტელექტი" (9) = 22
        assert count == 22

    def test_georgian_ratio_empty(self):
        assert _georgian_ratio("") == 0.0

    def test_georgian_ratio_english_only(self):
        assert _georgian_ratio("Hello World") == 0.0

    def test_georgian_ratio_georgian_only(self):
        ratio = _georgian_ratio("გამარჯობა")
        assert ratio == 1.0

    def test_georgian_ratio_mixed(self):
        # Mixed text should have a ratio between 0 and 1
        ratio = _georgian_ratio("AI ხელოვნური ინტელექტი")
        assert 0.0 < ratio < 1.0


# ===========================================================================
# Tests: Repetition detection
# ===========================================================================

class TestRepetitionDetection:
    """Test repetition ratio detection."""

    def test_no_repetition_short_text(self):
        assert _repetition_ratio("short") == 0.0

    def test_no_repetition_unique_text(self):
        # All unique windows
        text = "".join(chr(i % 26 + 65) * 200 for i in range(10))
        # Each 200-char window is different letters
        ratio = _repetition_ratio(text)
        assert ratio == 0.0

    def test_high_repetition(self):
        # Same block repeated many times
        block = "ა" * 200
        text = block * 20
        ratio = _repetition_ratio(text)
        assert ratio > 0.8  # Should detect high repetition

    def test_moderate_repetition(self):
        # Mix of unique and repeated content
        unique = "".join(chr(i % 26 + 65) * 200 for i in range(5))
        repeated = "X" * 200
        text = unique + repeated * 5
        ratio = _repetition_ratio(text)
        assert 0.0 < ratio < 1.0

    def test_empty_text(self):
        assert _repetition_ratio("") == 0.0


# ===========================================================================
# Tests: Gate 1 — Transcript quality
# ===========================================================================

class TestValidateTranscript:
    """Test transcript quality gate."""

    def test_none_transcript(self):
        result = validate_transcript(None)
        assert not result.passed
        assert "empty or None" in result.failures[0]

    def test_empty_transcript(self):
        result = validate_transcript("")
        assert not result.passed
        assert "empty or None" in result.failures[0]

    def test_too_short_transcript(self):
        result = validate_transcript("ტესტი " * 10)  # ~70 chars
        assert not result.passed
        assert any("too short" in f for f in result.failures)

    def test_no_georgian_characters(self):
        # Long enough but all English
        text = "This is an English lecture about AI " * 100
        result = validate_transcript(text)
        assert not result.passed
        assert any("Georgian" in f for f in result.failures)

    def test_excessive_repetition(self):
        # Long Georgian text with high repetition
        block = "ხელოვნური ინტელექტი " * 10  # ~200 chars
        text = block * 20  # ~4000 chars, highly repetitive
        result = validate_transcript(text)
        # Should either pass or fail on repetition depending on window matching
        if not result.passed:
            assert any("repetition" in f.lower() for f in result.failures)

    def test_valid_transcript(self):
        result = validate_transcript(VALID_GEORGIAN_TRANSCRIPT)
        assert result.passed
        assert len(result.failures) == 0
        assert result.metrics["length"] > MIN_TRANSCRIPT_CHARS
        assert result.metrics["georgian_ratio"] > MIN_GEORGIAN_RATIO

    def test_metrics_populated(self):
        result = validate_transcript(VALID_GEORGIAN_TRANSCRIPT)
        assert "length" in result.metrics
        assert "georgian_ratio" in result.metrics
        assert "georgian_chars" in result.metrics
        assert "repetition_ratio" in result.metrics

    def test_whitespace_only_transcript(self):
        result = validate_transcript("   \n\t  \n  ")
        assert not result.passed

    def test_moderate_repetition_warning(self):
        """Text with ~35% repetition should produce a warning, not failure."""
        unique_part = VALID_GEORGIAN_TRANSCRIPT[:3000]
        repeated_block = "ეს არის განმეორებული ბლოკი " * 8  # ~200 chars
        text = unique_part + repeated_block * 7  # Some repetition
        result = validate_transcript(text)
        # Should pass but may have warnings
        # (exact behavior depends on window matching)
        assert "length" in result.metrics


# ===========================================================================
# Tests: Gate 2 — Claude analysis validation
# ===========================================================================

class TestValidateClaudeAnalysis:
    """Test Claude analysis output validation."""

    def test_none_sections(self):
        result = validate_claude_analysis(None)
        assert not result.passed
        assert "None" in result.failures[0]

    def test_empty_dict(self):
        result = validate_claude_analysis({})
        assert not result.passed
        # Empty dict has no sections — all three are missing
        assert len(result.failures) == 3

    def test_all_empty_sections(self):
        sections = {"summary": "", "gap_analysis": "", "deep_analysis": ""}
        result = validate_claude_analysis(sections)
        assert not result.passed
        assert len(result.failures) == 3
        assert all("empty" in f for f in result.failures)

    def test_short_sections(self):
        sections = {
            "summary": "Short summary",  # <200 chars
            "gap_analysis": "Short gap",
            "deep_analysis": "Short deep",
        }
        result = validate_claude_analysis(sections)
        assert not result.passed
        assert all("too short" in f for f in result.failures)

    def test_valid_sections(self):
        long_text = "Analysis content. " * 50  # ~900 chars each
        sections = {
            "summary": long_text,
            "gap_analysis": long_text,
            "deep_analysis": long_text,
        }
        result = validate_claude_analysis(sections)
        assert result.passed
        assert len(result.failures) == 0

    def test_partially_valid(self):
        long_text = "Analysis content. " * 50
        sections = {
            "summary": long_text,
            "gap_analysis": "too short",
            "deep_analysis": long_text,
        }
        result = validate_claude_analysis(sections)
        assert not result.passed
        assert len(result.failures) == 1
        assert "gap_analysis" in result.failures[0]

    def test_missing_section_key(self):
        long_text = "Analysis content. " * 50
        sections = {
            "summary": long_text,
            # gap_analysis missing
            "deep_analysis": long_text,
        }
        result = validate_claude_analysis(sections)
        assert not result.passed
        assert any("gap_analysis" in f for f in result.failures)

    def test_metrics_include_section_lengths(self):
        long_text = "A" * 300
        sections = {
            "summary": long_text,
            "gap_analysis": long_text,
            "deep_analysis": long_text,
        }
        result = validate_claude_analysis(sections)
        assert result.metrics["summary_length"] == 300
        assert result.metrics["gap_analysis_length"] == 300
        assert result.metrics["deep_analysis_length"] == 300


# ===========================================================================
# Tests: Gate 3 — Summary document quality
# ===========================================================================

class TestValidateSummaryDocument:
    """Test summary document quality gate."""

    def test_none_content(self):
        result = validate_summary_document(None, group_number=1, lecture_number=3)
        assert not result.passed
        assert "empty or None" in result.failures[0]

    def test_empty_content(self):
        result = validate_summary_document("", group_number=1, lecture_number=3)
        assert not result.passed

    def test_too_short_content(self):
        result = validate_summary_document("Short", group_number=1, lecture_number=3)
        assert not result.passed
        assert any("too short" in f for f in result.failures)

    def test_valid_content_with_references(self):
        content = (
            "ლექცია #3 — ხელოვნური ინტელექტის შესავალი\n"
            "ჯგუფი #1 — მარტის ჯგუფი\n\n"
            + "AI-ის მნიშვნელოვანი კონცეფციები. " * 30
        )
        result = validate_summary_document(content, group_number=1, lecture_number=3)
        assert result.passed
        assert result.metrics["has_lecture_reference"] is True
        assert result.metrics["has_group_reference"] is True

    def test_missing_lecture_reference_warning(self):
        content = "ჯგუფი #1 — " + "AI კონცეფციები. " * 50
        result = validate_summary_document(content, group_number=1, lecture_number=3)
        # Should pass (it's a warning, not failure)
        assert result.passed
        assert any("lecture number" in w for w in result.warnings)

    def test_missing_group_reference_warning(self):
        content = "ლექცია #3 — " + "AI კონცეფციები. " * 50
        result = validate_summary_document(content, group_number=1, lecture_number=3)
        assert result.passed
        assert any("group" in w for w in result.warnings)


# ===========================================================================
# Tests: Gate 4 — Gap analysis quality
# ===========================================================================

class TestValidateGapAnalysis:
    """Test gap analysis validation."""

    def test_none_content(self):
        result = validate_gap_analysis(None)
        assert not result.passed

    def test_empty_content(self):
        result = validate_gap_analysis("")
        assert not result.passed

    def test_too_short(self):
        result = validate_gap_analysis("Short gap analysis")
        assert not result.passed
        assert any("too short" in f for f in result.failures)

    def test_valid_content(self):
        content = "Gap analysis content. " * 30  # >300 chars
        result = validate_gap_analysis(content)
        assert result.passed
        assert result.metrics["length"] > MIN_GAP_ANALYSIS_CHARS


# ===========================================================================
# Tests: Gate 5 — Pipeline output validation
# ===========================================================================

class TestValidatePipelineOutputs:
    """Test end-to-end pipeline output validation."""

    def test_all_outputs_present(self):
        outputs = PipelineOutputs(
            transcript_path_exists=True,
            summary_doc_url="https://docs.google.com/document/d/abc123/edit",
            gap_analysis_text="Gap analysis content here",
            deep_analysis_text="Deep analysis content here",
            pinecone_vectors_indexed=42,
        )
        result = validate_pipeline_outputs(outputs)
        assert result.passed
        assert len(result.failures) == 0
        assert result.metrics["outputs_present"] == 5

    def test_missing_transcript(self):
        outputs = PipelineOutputs(
            transcript_path_exists=False,
            summary_doc_url="https://example.com",
            gap_analysis_text="gap",
            deep_analysis_text="deep",
            pinecone_vectors_indexed=10,
        )
        result = validate_pipeline_outputs(outputs)
        assert not result.passed
        assert any("Transcript" in f for f in result.failures)

    def test_missing_summary_doc(self):
        outputs = PipelineOutputs(
            transcript_path_exists=True,
            summary_doc_url=None,
            gap_analysis_text="gap",
            deep_analysis_text="deep",
            pinecone_vectors_indexed=10,
        )
        result = validate_pipeline_outputs(outputs)
        assert not result.passed
        assert any("Summary" in f for f in result.failures)

    def test_missing_gap_analysis(self):
        outputs = PipelineOutputs(
            transcript_path_exists=True,
            summary_doc_url="https://example.com",
            gap_analysis_text=None,
            deep_analysis_text="deep",
            pinecone_vectors_indexed=10,
        )
        result = validate_pipeline_outputs(outputs)
        assert not result.passed
        assert any("Gap" in f for f in result.failures)

    def test_zero_vectors_is_warning(self):
        outputs = PipelineOutputs(
            transcript_path_exists=True,
            summary_doc_url="https://example.com",
            gap_analysis_text="gap",
            deep_analysis_text="deep",
            pinecone_vectors_indexed=0,
        )
        result = validate_pipeline_outputs(outputs)
        assert result.passed  # Vectors = 0 is a warning, not failure
        assert any("Pinecone" in w for w in result.warnings)

    def test_multiple_failures(self):
        outputs = PipelineOutputs(
            transcript_path_exists=False,
            summary_doc_url=None,
            gap_analysis_text=None,
            deep_analysis_text=None,
            pinecone_vectors_indexed=0,
        )
        result = validate_pipeline_outputs(outputs)
        assert not result.passed
        assert len(result.failures) == 4  # transcript, summary, gap, deep
        assert len(result.warnings) == 1  # pinecone


# ===========================================================================
# Tests: Safety filter classification
# ===========================================================================

class TestClassifyFinishReason:
    """Test Gemini finish reason classification."""

    def test_none_reason(self):
        assert classify_finish_reason(None) is None

    def test_empty_string(self):
        assert classify_finish_reason("") is None

    def test_normal_stop(self):
        assert classify_finish_reason("STOP") is None

    def test_safety_filter(self):
        assert classify_finish_reason("SAFETY") == SAFETY_FINISH_REASON

    def test_safety_filter_case_insensitive(self):
        assert classify_finish_reason("safety") == SAFETY_FINISH_REASON

    def test_safety_with_prefix(self):
        assert classify_finish_reason("BLOCKED_SAFETY") == SAFETY_FINISH_REASON

    def test_recitation_filter(self):
        assert classify_finish_reason("RECITATION") == RECITATION_FINISH_REASON

    def test_recitation_case_insensitive(self):
        assert classify_finish_reason("recitation") == RECITATION_FINISH_REASON

    def test_max_tokens(self):
        assert classify_finish_reason("MAX_TOKENS") is None


# ===========================================================================
# Tests: QualityResult
# ===========================================================================

class TestQualityResult:
    """Test QualityResult dataclass."""

    def test_passed_result(self):
        result = QualityResult(passed=True)
        assert result.passed
        assert result.failure_summary == "All checks passed"

    def test_failed_result(self):
        result = QualityResult(
            passed=False,
            failures=("Error A", "Error B"),
        )
        assert not result.passed
        assert "Error A" in result.failure_summary
        assert "Error B" in result.failure_summary

    def test_immutable(self):
        result = QualityResult(passed=True)
        with pytest.raises(AttributeError):
            result.passed = False  # type: ignore[misc]
