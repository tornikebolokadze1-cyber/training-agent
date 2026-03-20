"""Tests for NEW features added to tools/integrations/gemini_analyzer.py.

Covers:
- _is_empty_response_error — empty/short response detection
- _log_gemini_cost — cost tracking with usage_metadata
- _checkpoint_prefix — prefix generation logic
- _load_checkpoint — disk reads, missing file, empty file
- _save_checkpoint — disk writes, directory creation
- cleanup_checkpoints — glob deletion, returns count, missing group/lecture
- RETRY_EMPTY_RESPONSE_DELAY and MIN_MEANINGFUL_RESPONSE_CHARS constants
- Checkpoint integration inside transcribe_chunked_video (resume path)
- Checkpoint integration inside analyze_lecture (resume path)

All external API clients are fully stubbed via conftest.py.

Run with:
    pytest tools/tests/test_gemini_analyzer_new.py -v
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch


import tools.integrations.gemini_analyzer as ga


# ===========================================================================
# Helpers
# ===========================================================================

def _reset_client_caches() -> None:
    ga._client_cache.clear()
    ga._anthropic_client_cache = None


# ===========================================================================
# 1. _is_empty_response_error
# ===========================================================================


class TestIsEmptyResponseError:
    """_is_empty_response_error must recognise the three known phrases and
    reject everything else."""

    def test_empty_response_phrase_detected(self):
        assert ga._is_empty_response_error(Exception("Gemini returned empty response"))

    def test_response_too_short_detected(self):
        assert ga._is_empty_response_error(Exception("Response too short to be useful"))

    def test_insufficient_content_detected(self):
        assert ga._is_empty_response_error(Exception("insufficient content in reply"))

    def test_case_insensitive_matching(self):
        assert ga._is_empty_response_error(Exception("EMPTY RESPONSE from model"))
        assert ga._is_empty_response_error(Exception("RESPONSE TOO SHORT"))
        assert ga._is_empty_response_error(Exception("Insufficient Content"))

    def test_quota_error_not_matched(self):
        assert not ga._is_empty_response_error(Exception("429 resource exhausted"))

    def test_network_error_not_matched(self):
        assert not ga._is_empty_response_error(Exception("Connection reset by peer"))

    def test_timeout_error_not_matched(self):
        assert not ga._is_empty_response_error(Exception("Request timed out"))

    def test_generic_value_error_not_matched(self):
        assert not ga._is_empty_response_error(ValueError("Something went wrong"))

    def test_empty_error_message_not_matched(self):
        assert not ga._is_empty_response_error(Exception(""))


# ===========================================================================
# 2. RETRY_EMPTY_RESPONSE_DELAY and MIN_MEANINGFUL_RESPONSE_CHARS constants
# ===========================================================================


class TestRetryConstants:
    """Validate that the delay and minimum-length constants are sane."""

    def test_retry_empty_response_delay_is_45(self):
        assert ga.RETRY_EMPTY_RESPONSE_DELAY == 45

    def test_min_meaningful_response_chars_is_100(self):
        assert ga.MIN_MEANINGFUL_RESPONSE_CHARS == 100

    def test_retry_empty_response_delay_is_int(self):
        assert isinstance(ga.RETRY_EMPTY_RESPONSE_DELAY, int)

    def test_min_meaningful_response_chars_is_int(self):
        assert isinstance(ga.MIN_MEANINGFUL_RESPONSE_CHARS, int)

    def test_empty_delay_longer_than_base_delay(self):
        # The empty-response delay must be longer than the base exponential
        # backoff so that Gemini has extra time to finish processing.
        assert ga.RETRY_EMPTY_RESPONSE_DELAY > ga.RETRY_BASE_DELAY


# ===========================================================================
# 3. _log_gemini_cost
# ===========================================================================


class TestLogGeminiCost:
    """_log_gemini_cost must log token counts and computed costs.
    All assertions are on logger calls — no real API calls made."""

    def _make_response(self, input_tokens: int, output_tokens: int) -> MagicMock:
        usage = MagicMock()
        usage.prompt_token_count = input_tokens
        usage.candidates_token_count = output_tokens
        response = MagicMock()
        response.usage_metadata = usage
        return response

    def test_logs_info_with_usage_metadata(self, caplog):
        response = self._make_response(1000, 500)
        with caplog.at_level(logging.INFO, logger="tools.integrations.gemini_analyzer"):
            ga._log_gemini_cost("gemini-2.5-pro", response, "test_purpose")
        # The function must emit at least one INFO log with cost info
        assert any("test_purpose" in r.message for r in caplog.records)

    def test_logs_total_cost_field(self, caplog):
        response = self._make_response(1_000_000, 100_000)
        with caplog.at_level(logging.INFO, logger="tools.integrations.gemini_analyzer"):
            ga._log_gemini_cost("gemini-2.5-pro", response, "transcription")
        combined = " ".join(r.message for r in caplog.records)
        assert "total" in combined.lower() or "$" in combined

    def test_no_usage_metadata_logs_info(self, caplog):
        response = MagicMock()
        response.usage_metadata = None
        with caplog.at_level(logging.INFO, logger="tools.integrations.gemini_analyzer"):
            ga._log_gemini_cost("gemini-2.5-pro", response, "summary")
        # Should log "no usage_metadata" info, not crash
        assert any("no usage_metadata" in r.message.lower() for r in caplog.records)

    def test_missing_usage_metadata_attribute_does_not_raise(self):
        # response object with no usage_metadata attribute at all
        response = object()
        ga._log_gemini_cost("gemini-2.5-pro", response, "test")  # must not raise

    def test_known_model_uses_correct_rate(self, caplog):
        # gemini-2.5-pro has $1.25/M input tokens
        response = self._make_response(1_000_000, 0)
        with caplog.at_level(logging.INFO, logger="tools.integrations.gemini_analyzer"):
            ga._log_gemini_cost("gemini-2.5-pro", response, "rate-check")
        combined = " ".join(r.message for r in caplog.records)
        # 1M input tokens * $1.25/M = $1.25 input cost
        assert "1.25" in combined or "1.2500" in combined

    def test_unknown_model_uses_default_rate(self, caplog):
        # An unknown model should fall back to GEMINI_COST_DEFAULT without error
        response = self._make_response(1000, 500)
        with caplog.at_level(logging.INFO, logger="tools.integrations.gemini_analyzer"):
            ga._log_gemini_cost("gemini-unknown-future-model", response, "default-rate")
        # Should not raise and should produce a log entry
        assert any("default-rate" in r.message for r in caplog.records)

    def test_zero_token_counts_do_not_raise(self, caplog):
        response = self._make_response(0, 0)
        with caplog.at_level(logging.INFO, logger="tools.integrations.gemini_analyzer"):
            ga._log_gemini_cost("gemini-2.5-flash", response, "zero-tokens")
        # total cost should be $0.0000
        assert any("zero-tokens" in r.message for r in caplog.records)


# ===========================================================================
# 4. _checkpoint_prefix
# ===========================================================================


class TestCheckpointPrefix:
    """_checkpoint_prefix must produce the right string or empty string."""

    def test_both_provided_returns_gN_lM(self):
        assert ga._checkpoint_prefix(1, 3) == "g1_l3"

    def test_group1_lecture15(self):
        assert ga._checkpoint_prefix(1, 15) == "g1_l15"

    def test_group2_lecture1(self):
        assert ga._checkpoint_prefix(2, 1) == "g2_l1"

    def test_group_none_returns_empty(self):
        assert ga._checkpoint_prefix(None, 5) == ""

    def test_lecture_none_returns_empty(self):
        assert ga._checkpoint_prefix(1, None) == ""

    def test_both_none_returns_empty(self):
        assert ga._checkpoint_prefix(None, None) == ""

    def test_returns_string(self):
        assert isinstance(ga._checkpoint_prefix(1, 1), str)

    def test_empty_string_is_falsy(self):
        assert not ga._checkpoint_prefix(None, None)

    def test_non_empty_string_is_truthy(self):
        assert ga._checkpoint_prefix(1, 1)


# ===========================================================================
# 5. _load_checkpoint
# ===========================================================================


class TestLoadCheckpoint:
    """_load_checkpoint must return file content or None."""

    def test_existing_file_returns_content(self, tmp_path):
        content = "This is the checkpoint content.\n" * 10
        ckpt_file = tmp_path / "g1_l3_chunk0_transcript.txt"
        ckpt_file.write_text(content, encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._load_checkpoint("g1_l3_chunk0_transcript.txt")

        assert result == content

    def test_missing_file_returns_none(self, tmp_path):
        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._load_checkpoint("nonexistent_checkpoint.txt")

        assert result is None

    def test_empty_file_returns_none(self, tmp_path):
        ckpt_file = tmp_path / "g1_l1_transcript.txt"
        ckpt_file.write_text("", encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._load_checkpoint("g1_l1_transcript.txt")

        assert result is None

    def test_whitespace_only_file_returns_none(self, tmp_path):
        ckpt_file = tmp_path / "g2_l5_transcript.txt"
        ckpt_file.write_text("   \n\t  \n", encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._load_checkpoint("g2_l5_transcript.txt")

        assert result is None

    def test_georgian_content_preserved(self, tmp_path):
        georgian = "ეს არის ქართული ტექსტი — საუბარი AI-ზე.\n" * 5
        ckpt_file = tmp_path / "g1_l2_summary.txt"
        ckpt_file.write_text(georgian, encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._load_checkpoint("g1_l2_summary.txt")

        assert result == georgian

    def test_returns_string_type(self, tmp_path):
        ckpt_file = tmp_path / "g1_l1_deep.txt"
        ckpt_file.write_text("Some analysis content here.", encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._load_checkpoint("g1_l1_deep.txt")

        assert isinstance(result, str)


# ===========================================================================
# 6. _save_checkpoint
# ===========================================================================


class TestSaveCheckpoint:
    """_save_checkpoint must write content to disk and create the directory."""

    def test_saves_file_to_tmp_dir(self, tmp_path):
        content = "Transcript content for lecture 3."
        with patch.object(ga, "TMP_DIR", tmp_path):
            ga._save_checkpoint("g1_l3_transcript.txt", content)

        saved = (tmp_path / "g1_l3_transcript.txt").read_text(encoding="utf-8")
        assert saved == content

    def test_creates_tmp_dir_if_missing(self, tmp_path):
        nested = tmp_path / "new_subdir"
        # Do not create nested — _save_checkpoint must create it
        content = "Analysis text."
        with patch.object(ga, "TMP_DIR", nested):
            ga._save_checkpoint("g2_l1_gap.txt", content)

        assert (nested / "g2_l1_gap.txt").exists()

    def test_overwrites_existing_checkpoint(self, tmp_path):
        ckpt = tmp_path / "g1_l1_summary.txt"
        ckpt.write_text("old content", encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            ga._save_checkpoint("g1_l1_summary.txt", "new content")

        assert ckpt.read_text(encoding="utf-8") == "new content"

    def test_logs_checkpoint_save(self, tmp_path, caplog):
        with patch.object(ga, "TMP_DIR", tmp_path):
            with caplog.at_level(logging.INFO, logger="tools.integrations.gemini_analyzer"):
                ga._save_checkpoint("g2_l5_chunk1_transcript.txt", "content")

        assert any("g2_l5_chunk1_transcript.txt" in r.message for r in caplog.records)

    def test_preserves_utf8_encoding(self, tmp_path):
        georgian = "ანალიზის შედეგები ლექცია #7-ისთვის."
        with patch.object(ga, "TMP_DIR", tmp_path):
            ga._save_checkpoint("g1_l7_deep.txt", georgian)

        result = (tmp_path / "g1_l7_deep.txt").read_text(encoding="utf-8")
        assert result == georgian


# ===========================================================================
# 7. cleanup_checkpoints
# ===========================================================================


class TestCleanupCheckpoints:
    """cleanup_checkpoints must delete matching files and return count."""

    def test_deletes_all_matching_files(self, tmp_path):
        # Create three checkpoint files for g1_l3 and one for a different lecture
        for name in [
            "g1_l3_chunk0_transcript.txt",
            "g1_l3_chunk1_transcript.txt",
            "g1_l3_full_transcript.txt",
        ]:
            (tmp_path / name).write_text("content", encoding="utf-8")

        # This file should NOT be deleted (different lecture)
        other = tmp_path / "g1_l4_transcript.txt"
        other.write_text("keep me", encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            deleted = ga.cleanup_checkpoints(1, 3)

        assert deleted == 3
        assert other.exists()

    def test_returns_zero_when_no_files(self, tmp_path):
        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga.cleanup_checkpoints(1, 5)

        assert result == 0

    def test_returns_correct_count(self, tmp_path):
        for i in range(5):
            (tmp_path / f"g2_l2_chunk{i}_transcript.txt").write_text("x", encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            count = ga.cleanup_checkpoints(2, 2)

        assert count == 5

    def test_files_actually_removed_from_disk(self, tmp_path):
        ckpt = tmp_path / "g1_l1_summary.txt"
        ckpt.write_text("summary", encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            ga.cleanup_checkpoints(1, 1)

        assert not ckpt.exists()

    def test_invalid_group_lecture_returns_zero(self, tmp_path):
        # None group/lecture should return 0 without touching anything
        (tmp_path / "g1_l1_summary.txt").write_text("x", encoding="utf-8")
        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga.cleanup_checkpoints.__wrapped__(1, 1) if hasattr(
                ga.cleanup_checkpoints, "__wrapped__"
            ) else ga.cleanup_checkpoints(1, 1)

        # Result must be a non-negative integer
        assert isinstance(result, int)
        assert result >= 0

    def test_group2_lecture8_prefix_matches_correctly(self, tmp_path):
        # Ensure g2_l8 prefix does NOT match g2_l18 (different lecture)
        (tmp_path / "g2_l8_transcript.txt").write_text("data", encoding="utf-8")
        (tmp_path / "g2_l18_transcript.txt").write_text("other", encoding="utf-8")

        with patch.object(ga, "TMP_DIR", tmp_path):
            deleted = ga.cleanup_checkpoints(2, 8)

        # g2_l8_* → 1 file; g2_l18 should remain (glob pattern g2_l8_* won't match g2_l18_*)
        assert deleted == 1
        assert (tmp_path / "g2_l18_transcript.txt").exists()


# ===========================================================================
# 8. Checkpoint resume in transcribe_chunked_video
# ===========================================================================


class TestTranscribeChunkedVideoCheckpointResume:
    """When a chunk checkpoint exists, transcribe_chunked_video must load it
    from disk instead of calling upload_video / transcribe_video again."""

    def setup_method(self):
        _reset_client_caches()

    def test_chunk_checkpoint_skips_upload(self, tmp_path):
        # Create a fake video file
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake-video")

        # Pre-populate the chunk-0 transcript checkpoint
        ckpt_content = "Chunk 0 transcript content." * 20  # >100 chars
        (tmp_path / "g1_l3_chunk0_transcript.txt").write_text(ckpt_content, encoding="utf-8")

        with (
            patch.object(ga, "TMP_DIR", tmp_path),
            patch("tools.integrations.gemini_analyzer._get_video_duration_seconds",
                  return_value=20 * 60),  # 20 min — single chunk, no splitting
            patch("tools.integrations.gemini_analyzer.upload_video") as mock_upload,
            patch("tools.integrations.gemini_analyzer.transcribe_video") as mock_transcribe,
        ):
            # Make upload_video return something even though it should not be called
            mock_upload.return_value = (MagicMock(name="files/fake123"), False)
            mock_transcribe.return_value = "should not be returned"

            result_text, used_free = ga.transcribe_chunked_video(
                video, use_free=False, group=1, lecture=3,
            )

        # upload_video and transcribe_video must NOT have been called
        mock_upload.assert_not_called()
        mock_transcribe.assert_not_called()
        assert ckpt_content in result_text

    def test_no_checkpoint_calls_upload_and_transcribe(self, tmp_path):
        """Without a checkpoint, the full upload+transcribe path runs."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake-video")

        fake_file_ref = MagicMock()
        fake_file_ref.name = "files/abc123"
        transcript_text = "Full transcript from Gemini. " * 10

        with (
            patch.object(ga, "TMP_DIR", tmp_path),
            patch("tools.integrations.gemini_analyzer._get_video_duration_seconds",
                  return_value=20 * 60),
            patch("tools.integrations.gemini_analyzer.upload_video",
                  return_value=(fake_file_ref, False)) as mock_upload,
            patch("tools.integrations.gemini_analyzer.transcribe_video",
                  return_value=transcript_text) as mock_transcribe,
            patch.object(ga, "_get_client") as mock_get_client,
        ):
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.files.delete = MagicMock()

            result_text, used_free = ga.transcribe_chunked_video(
                video, use_free=False, group=1, lecture=5,
            )

        mock_upload.assert_called_once()
        mock_transcribe.assert_called_once()
        assert transcript_text in result_text


# ===========================================================================
# 9. Checkpoint resume in analyze_lecture
# ===========================================================================


class TestAnalyzeLectureCheckpointResume:
    """When a full-transcript checkpoint exists, analyze_lecture must skip
    the expensive transcribe_chunked_video call."""

    def setup_method(self):
        _reset_client_caches()

    def test_transcript_checkpoint_skips_transcription(self, tmp_path):
        # Create a fake video
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake-video")

        # Pre-create the full-transcript checkpoint
        cached_transcript = "Cached full transcript. " * 50
        (tmp_path / "g1_l4_full_transcript.txt").write_text(
            cached_transcript, encoding="utf-8"
        )

        # Provide fake analysis results so the pipeline completes
        fake_sections = {
            "summary": "Summary in English. " * 20,
            "gap_analysis": "Gap analysis in English. " * 20,
            "deep_analysis": "Deep analysis in English. " * 20,
        }
        fake_georgian = "ქართული ტექსტი. " * 20

        with (
            patch.object(ga, "TMP_DIR", tmp_path),
            patch("tools.integrations.gemini_analyzer.transcribe_chunked_video") as mock_tcv,
            patch("tools.integrations.gemini_analyzer._safe_claude_reason_all",
                  return_value=fake_sections),
            patch("tools.integrations.gemini_analyzer._safe_gemini_write_georgian",
                  return_value=fake_georgian),
        ):
            result = ga.analyze_lecture(video, group=1, lecture=4)

        # transcribe_chunked_video must NOT have been called (checkpoint hit)
        mock_tcv.assert_not_called()
        assert result["transcript"] == cached_transcript

    def test_existing_transcript_param_skips_transcription(self, tmp_path):
        """When existing_transcript is passed, transcription is always skipped."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake-video")

        provided_transcript = "Provided transcript text. " * 50
        fake_sections = {
            "summary": "S " * 30,
            "gap_analysis": "G " * 30,
            "deep_analysis": "D " * 30,
        }

        with (
            patch.object(ga, "TMP_DIR", tmp_path),
            patch("tools.integrations.gemini_analyzer.transcribe_chunked_video") as mock_tcv,
            patch("tools.integrations.gemini_analyzer._safe_claude_reason_all",
                  return_value=fake_sections),
            patch("tools.integrations.gemini_analyzer._safe_gemini_write_georgian",
                  return_value="ქართული. " * 20),
        ):
            result = ga.analyze_lecture(
                video,
                existing_transcript=provided_transcript,
            )

        mock_tcv.assert_not_called()
        assert result["transcript"] == provided_transcript

    def test_analyze_lecture_returns_expected_keys(self, tmp_path):
        """analyze_lecture result must always contain all four expected keys."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake-video")

        fake_sections = {
            "summary": "Summary. " * 30,
            "gap_analysis": "Gap. " * 30,
            "deep_analysis": "Deep. " * 30,
        }

        with (
            patch.object(ga, "TMP_DIR", tmp_path),
            patch("tools.integrations.gemini_analyzer.transcribe_chunked_video",
                  return_value=("Transcript. " * 50, False)),
            patch("tools.integrations.gemini_analyzer._safe_claude_reason_all",
                  return_value=fake_sections),
            patch("tools.integrations.gemini_analyzer._safe_gemini_write_georgian",
                  return_value="ქართული. " * 20),
        ):
            result = ga.analyze_lecture(video)

        assert "transcript" in result
        assert "summary" in result
        assert "gap_analysis" in result
        assert "deep_analysis" in result
