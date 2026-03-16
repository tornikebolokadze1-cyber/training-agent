"""Tests for tools/gemini_analyzer.py — Gemini/Claude analysis pipeline.

Covers:
- _get_client caching (same key returns same instance; different keys return different instances)
- _get_anthropic_client caching (singleton pattern)
- analyze_lecture quality gates (alert_operator called on each analysis failure)
- _claude_reason timeout (timeout=600.0 passed to client.messages.create)
- analyze_lecture return dict structure (all expected keys present)
- Client cache isolation between different API keys

All external API clients are fully stubbed following the same pattern used in
test_core.py — no real network calls are ever made.

Run with:
    pytest tools/tests/test_gemini_analyzer.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.gemini_analyzer as ga


# ===========================================================================
# Helpers
# ===========================================================================

def _reset_client_caches():
    """Clear both client caches between tests so they do not bleed state."""
    ga._client_cache.clear()
    ga._anthropic_client_cache = None


# ===========================================================================
# 1. _get_client — Gemini client caching
# ===========================================================================


class TestGetClientCaching:
    """_get_client must return the same instance on repeated calls for the
    same API key and must instantiate genai.Client exactly once per key.

    We patch "tools.gemini_analyzer.genai.Client" via string path (create=True)
    so the patch is applied to the exact name that _get_client calls, regardless
    of how the stub module was set up in a shared pytest session.
    """

    def setup_method(self):
        _reset_client_caches()

    def test_same_paid_key_returns_same_instance(self):
        fake_client = MagicMock()
        mock_genai_client = MagicMock(return_value=fake_client)

        with patch.object(ga, "GEMINI_API_KEY_PAID", "paid-key-abc"), \
             patch.object(ga, "GEMINI_API_KEY", "free-key-xyz"), \
             patch("tools.gemini_analyzer.genai.Client", mock_genai_client,
                   create=True):

            first = ga._get_client(use_free=False)
            second = ga._get_client(use_free=False)

        assert first is second
        # Constructor called exactly once
        assert mock_genai_client.call_count == 1

    def test_same_free_key_returns_same_instance(self):
        fake_client = MagicMock()
        mock_genai_client = MagicMock(return_value=fake_client)

        with patch.object(ga, "GEMINI_API_KEY", "free-key-xyz"), \
             patch.object(ga, "GEMINI_API_KEY_PAID", ""), \
             patch("tools.gemini_analyzer.genai.Client", mock_genai_client,
                   create=True):

            first = ga._get_client(use_free=True)
            second = ga._get_client(use_free=True)

        assert first is second
        assert mock_genai_client.call_count == 1

    def test_different_keys_get_different_clients(self):
        paid_client = MagicMock(name="paid_client")
        free_client = MagicMock(name="free_client")

        def _make_client(api_key):
            return paid_client if api_key == "paid-key-abc" else free_client

        mock_genai_client = MagicMock(side_effect=_make_client)

        with patch.object(ga, "GEMINI_API_KEY_PAID", "paid-key-abc"), \
             patch.object(ga, "GEMINI_API_KEY", "free-key-xyz"), \
             patch("tools.gemini_analyzer.genai.Client", mock_genai_client,
                   create=True):

            result_paid = ga._get_client(use_free=False)
            result_free = ga._get_client(use_free=True)

        assert result_paid is not result_free

    def test_no_api_key_raises_runtime_error(self):
        with patch.object(ga, "GEMINI_API_KEY", ""), \
             patch.object(ga, "GEMINI_API_KEY_PAID", ""):
            with pytest.raises(RuntimeError):
                ga._get_client(use_free=False)

    def test_free_key_missing_raises_runtime_error(self):
        with patch.object(ga, "GEMINI_API_KEY", ""):
            with pytest.raises(RuntimeError):
                ga._get_client(use_free=True)


# ===========================================================================
# 2. _get_anthropic_client — Anthropic client caching
# ===========================================================================


class TestGetAnthropicClientCaching:
    """_get_anthropic_client must return the same singleton instance on every
    call and must call anthropic.Anthropic() exactly once.

    We patch "tools.gemini_analyzer.anthropic.Anthropic" via string path so
    the patch lands on the exact binding used inside the module, not on the
    shared stub object which may or may not be the same reference in a
    combined test session.
    """

    def setup_method(self):
        _reset_client_caches()

    def test_returns_same_instance_on_repeated_calls(self):
        fake_client = MagicMock()
        mock_constructor = MagicMock(return_value=fake_client)

        with patch.object(ga, "ANTHROPIC_API_KEY", "test-anthropic-key"), \
             patch("tools.gemini_analyzer.anthropic.Anthropic", mock_constructor,
                   create=True):

            first = ga._get_anthropic_client()
            second = ga._get_anthropic_client()

        assert first is second
        assert mock_constructor.call_count == 1

    def test_missing_api_key_raises_runtime_error(self):
        with patch.object(ga, "ANTHROPIC_API_KEY", ""):
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
                ga._get_anthropic_client()

    def test_constructor_called_with_correct_key(self):
        fake_client = MagicMock()
        mock_constructor = MagicMock(return_value=fake_client)

        with patch.object(ga, "ANTHROPIC_API_KEY", "my-secret-key"), \
             patch("tools.gemini_analyzer.anthropic.Anthropic", mock_constructor,
                   create=True):
            ga._get_anthropic_client()

        mock_constructor.assert_called_once_with(api_key="my-secret-key")


# ===========================================================================
# 3. _claude_reason — timeout parameter
# ===========================================================================


class TestClaudeReasonTimeout:
    """_claude_reason must pass timeout=600.0 to client.messages.create."""

    def setup_method(self):
        _reset_client_caches()

    def _make_fake_response(self, text: str):
        """Build a minimal response object that _claude_reason can extract text from."""
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = text

        thinking_block = MagicMock()
        thinking_block.type = "thinking"

        response = MagicMock()
        response.content = [thinking_block, text_block]
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50
        return response

    def test_timeout_600_passed_to_messages_create(self):
        fake_response = self._make_fake_response("analysis result")
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response

        with patch.object(ga, "ANTHROPIC_API_KEY", "test-key"), \
             patch.object(ga, "_anthropic_client_cache", fake_client):

            ga._claude_reason("transcript text", "test prompt", "test purpose")

        create_call = fake_client.messages.create.call_args
        assert create_call.kwargs.get("timeout") == 600.0

    def test_timeout_is_float_not_int(self):
        fake_response = self._make_fake_response("result")
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response

        with patch.object(ga, "ANTHROPIC_API_KEY", "test-key"), \
             patch.object(ga, "_anthropic_client_cache", fake_client):

            ga._claude_reason("transcript", "prompt", "purpose")

        timeout_value = fake_client.messages.create.call_args.kwargs["timeout"]
        assert isinstance(timeout_value, float)

    def test_model_and_max_tokens_also_passed(self):
        fake_response = self._make_fake_response("result")
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response

        with patch.object(ga, "ANTHROPIC_API_KEY", "test-key"), \
             patch.object(ga, "_anthropic_client_cache", fake_client), \
             patch.object(ga, "ASSISTANT_CLAUDE_MODEL", "claude-opus-test"):

            ga._claude_reason("transcript", "prompt", "purpose")

        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs.get("model") == "claude-opus-test"
        assert kwargs.get("max_tokens") == 16000


# ===========================================================================
# 4. analyze_lecture — quality gates (alert_operator called on failures)
# ===========================================================================


class TestAnalyzeLectureQualityGates:
    """When any analysis step (summary / gap / deep) raises an exception,
    analyze_lecture must call alert_operator with a descriptive message and
    must still return a dict with all four expected keys."""

    def setup_method(self):
        _reset_client_caches()

    def _run_analyze_with_failures(
        self,
        fail_summary: bool = False,
        fail_gap: bool = False,
        fail_deep: bool = False,
    ):
        """Patch generate_* functions and alert_operator, then run analyze_lecture."""

        def _maybe_raise(label: bool, name: str):
            if label:
                raise RuntimeError(f"Simulated {name} failure")
            return f"{name} output"

        mock_alert = MagicMock()

        with patch.object(ga, "transcribe_chunked_video", return_value=("transcript text", False)), \
             patch.object(ga, "generate_summary",
                          side_effect=lambda t, **kw: _maybe_raise(fail_summary, "summary")), \
             patch.object(ga, "generate_gap_analysis",
                          side_effect=lambda t, **kw: _maybe_raise(fail_gap, "gap_analysis")), \
             patch.object(ga, "generate_deep_analysis",
                          side_effect=lambda t, **kw: _maybe_raise(fail_deep, "deep_analysis")), \
             patch("tools.whatsapp_sender.alert_operator", mock_alert), \
             patch("tools.gemini_analyzer.alert_operator", mock_alert, create=True):

            result = ga.analyze_lecture(Path("/fake/lecture.mp4"))

        return result, mock_alert

    def test_summary_failure_triggers_alert(self):
        result, mock_alert = self._run_analyze_with_failures(fail_summary=True)

        # alert_operator must have been called at least once for summary
        assert mock_alert.call_count >= 1
        alert_messages = " ".join(str(c) for c in mock_alert.call_args_list)
        assert "Summary" in alert_messages or "summary" in alert_messages.lower()

    def test_gap_analysis_failure_triggers_alert(self):
        result, mock_alert = self._run_analyze_with_failures(fail_gap=True)

        assert mock_alert.call_count >= 1
        alert_messages = " ".join(str(c) for c in mock_alert.call_args_list)
        assert "Gap" in alert_messages or "gap" in alert_messages.lower()

    def test_deep_analysis_failure_triggers_alert(self):
        result, mock_alert = self._run_analyze_with_failures(fail_deep=True)

        assert mock_alert.call_count >= 1
        alert_messages = " ".join(str(c) for c in mock_alert.call_args_list)
        assert "Deep" in alert_messages or "deep" in alert_messages.lower()

    def test_all_three_failures_each_trigger_alert(self):
        result, mock_alert = self._run_analyze_with_failures(
            fail_summary=True, fail_gap=True, fail_deep=True
        )

        # One alert per failed step
        assert mock_alert.call_count == 3

    def test_failed_steps_produce_empty_string_in_result(self):
        result, _ = self._run_analyze_with_failures(
            fail_summary=True, fail_gap=True, fail_deep=True
        )

        assert result["summary"] == ""
        assert result["gap_analysis"] == ""
        assert result["deep_analysis"] == ""

    def test_successful_steps_not_empty(self):
        result, mock_alert = self._run_analyze_with_failures(fail_summary=False)

        assert result["summary"] == "summary output"
        assert mock_alert.call_count == 0


# ===========================================================================
# 5. analyze_lecture — return dict structure
# ===========================================================================


class TestAnalyzeLectureDictStructure:
    """analyze_lecture must always return a dict with exactly the four
    documented keys regardless of which steps succeed or fail."""

    EXPECTED_KEYS = {"transcript", "summary", "gap_analysis", "deep_analysis"}

    def setup_method(self):
        _reset_client_caches()

    def _run_happy_path(self):
        with patch.object(ga, "transcribe_chunked_video",
                          return_value=("full transcript", False)), \
             patch.object(ga, "generate_summary", return_value="summary text"), \
             patch.object(ga, "generate_gap_analysis", return_value="gap text"), \
             patch.object(ga, "generate_deep_analysis", return_value="deep text"):
            return ga.analyze_lecture(Path("/fake/lecture.mp4"))

    def test_result_is_dict(self):
        result = self._run_happy_path()
        assert isinstance(result, dict)

    def test_all_expected_keys_present_on_success(self):
        result = self._run_happy_path()
        assert set(result.keys()) == self.EXPECTED_KEYS

    def test_transcript_key_contains_transcript(self):
        result = self._run_happy_path()
        assert result["transcript"] == "full transcript"

    def test_all_expected_keys_present_on_full_failure(self):
        with patch.object(ga, "transcribe_chunked_video",
                          return_value=("transcript", False)), \
             patch.object(ga, "generate_summary",
                          side_effect=RuntimeError("fail")), \
             patch.object(ga, "generate_gap_analysis",
                          side_effect=RuntimeError("fail")), \
             patch.object(ga, "generate_deep_analysis",
                          side_effect=RuntimeError("fail")), \
             patch("tools.whatsapp_sender.alert_operator", MagicMock()), \
             patch("tools.gemini_analyzer.alert_operator", MagicMock(), create=True):
            result = ga.analyze_lecture(Path("/fake/lecture.mp4"))

        assert set(result.keys()) == self.EXPECTED_KEYS

    def test_existing_transcript_skips_transcription(self):
        """When existing_transcript is supplied, transcribe_chunked_video must
        not be called at all."""
        with patch.object(ga, "transcribe_chunked_video") as mock_transcribe, \
             patch.object(ga, "generate_summary", return_value="s"), \
             patch.object(ga, "generate_gap_analysis", return_value="g"), \
             patch.object(ga, "generate_deep_analysis", return_value="d"):
            result = ga.analyze_lecture(
                Path("/fake/lecture.mp4"),
                existing_transcript="pre-built transcript",
            )

        mock_transcribe.assert_not_called()
        assert result["transcript"] == "pre-built transcript"

    def test_values_are_strings(self):
        result = self._run_happy_path()
        for key in self.EXPECTED_KEYS:
            assert isinstance(result[key], str), f"Key '{key}' is not a str"


# ===========================================================================
# 6. _is_quota_error
# ===========================================================================


class TestIsQuotaError:
    """_is_quota_error checks error message strings for quota/rate-limit indicators."""

    def test_429_detected(self):
        assert ga._is_quota_error(Exception("HTTP 429 Too Many Requests"))

    def test_resource_exhausted_detected(self):
        assert ga._is_quota_error(Exception("RESOURCE EXHAUSTED: billing limit"))

    def test_rate_limit_detected(self):
        assert ga._is_quota_error(Exception("rate limit exceeded"))

    def test_too_many_requests_detected(self):
        assert ga._is_quota_error(Exception("too many requests — slow down"))

    def test_quota_detected(self):
        assert ga._is_quota_error(Exception("Quota exceeded for project"))

    def test_network_timeout_not_quota(self):
        assert not ga._is_quota_error(Exception("network timeout"))

    def test_auth_failed_not_quota(self):
        assert not ga._is_quota_error(Exception("authentication failed"))

    def test_generic_error_not_quota(self):
        assert not ga._is_quota_error(Exception("something went wrong"))


# ===========================================================================
# 7. split_video_chunks
# ===========================================================================


class TestSplitVideoChunks:
    """split_video_chunks uses ffprobe/ffmpeg — all subprocess calls are mocked."""

    def setup_method(self):
        _reset_client_caches()

    def test_short_video_returns_original(self, tmp_path):
        """Video under 45 min returns original path as single-element list."""
        video = tmp_path / "short.mp4"
        video.write_bytes(b"\x00" * 1000)

        with patch("tools.gemini_analyzer.subprocess.run") as mock_run:
            # ffprobe returns 30 minutes
            mock_run.return_value = MagicMock(returncode=0, stdout="1800.0\n", stderr="")
            result = ga.split_video_chunks(video)

        assert result == [video]

    def test_long_video_splits_into_chunks(self, tmp_path):
        """Video of 100 min should produce 3 chunks (~45+45+10)."""
        video = tmp_path / "long.mp4"
        video.write_bytes(b"\x00" * 1000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return MagicMock(returncode=0, stdout="6000.0\n", stderr="")
            # ffmpeg — create the chunk file
            # The output path is the last argument (after --)
            out_path = Path(cmd[-1])
            out_path.write_bytes(b"\x00" * 200_000)  # >100KB
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools.gemini_analyzer.subprocess.run", side_effect=fake_run):
            result = ga.split_video_chunks(video)

        assert len(result) == 3
        for p in result:
            assert p.suffix == ".mp4"

    def test_zero_duration_raises_value_error(self, tmp_path):
        video = tmp_path / "zero.mp4"
        video.write_bytes(b"\x00" * 100)

        with patch("tools.gemini_analyzer.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0.0\n", stderr="")
            with pytest.raises(ValueError, match="zero or negative"):
                ga.split_video_chunks(video)

    def test_negative_duration_raises_value_error(self, tmp_path):
        video = tmp_path / "neg.mp4"
        video.write_bytes(b"\x00" * 100)

        with patch("tools.gemini_analyzer.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="-5.0\n", stderr="")
            with pytest.raises(ValueError, match="zero or negative"):
                ga.split_video_chunks(video)

    def test_existing_valid_chunk_reused(self, tmp_path):
        """If a chunk file already exists and is big enough, it is reused."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"\x00" * 1000)

        # Pre-create a valid chunk0 file (>100KB)
        chunk0 = tmp_path / "lecture.chunk0.mp4"
        chunk0.write_bytes(b"\x00" * 200_000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return MagicMock(returncode=0, stdout="6000.0\n", stderr="")
            out_path = Path(cmd[-1])
            out_path.write_bytes(b"\x00" * 200_000)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools.gemini_analyzer.subprocess.run", side_effect=fake_run) as mock_run:
            ga.split_video_chunks(video)

        # ffprobe call + ffmpeg for chunk1 and chunk2 only (chunk0 reused)
        ffmpeg_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "ffmpeg"]
        assert len(ffmpeg_calls) == 2  # chunk1, chunk2 — not chunk0

    def test_existing_small_chunk_recreated(self, tmp_path):
        """If a chunk file exists but is too small, it is deleted and re-created."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"\x00" * 1000)

        # Pre-create a too-small chunk0 file (<100KB)
        chunk0 = tmp_path / "lecture.chunk0.mp4"
        chunk0.write_bytes(b"\x00" * 50)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return MagicMock(returncode=0, stdout="6000.0\n", stderr="")
            out_path = Path(cmd[-1])
            out_path.write_bytes(b"\x00" * 200_000)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools.gemini_analyzer.subprocess.run", side_effect=fake_run) as mock_run:
            ga.split_video_chunks(video)

        # All 3 chunks should have ffmpeg calls (chunk0 was recreated)
        ffmpeg_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "ffmpeg"]
        assert len(ffmpeg_calls) == 3

    def test_ffprobe_failure_raises_runtime_error(self, tmp_path):
        video = tmp_path / "bad.mp4"
        video.write_bytes(b"\x00" * 100)

        with patch("tools.gemini_analyzer.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="No such file")
            with pytest.raises(RuntimeError, match="ffprobe failed"):
                ga.split_video_chunks(video)

    def test_ffmpeg_failure_raises_runtime_error(self, tmp_path):
        video = tmp_path / "fail.mp4"
        video.write_bytes(b"\x00" * 100)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return MagicMock(returncode=0, stdout="6000.0\n", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="encoding error")

        with patch("tools.gemini_analyzer.subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="ffmpeg chunk"):
                ga.split_video_chunks(video)


# ===========================================================================
# 8. upload_video
# ===========================================================================


class TestUploadVideo:
    """upload_video uploads a file, handles quota errors, and waits for processing."""

    def setup_method(self):
        _reset_client_caches()

    def test_successful_upload(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00" * 5000)

        fake_uploaded = MagicMock(name="uploaded_file")
        fake_uploaded.name = "files/abc123"
        fake_client = MagicMock()
        fake_client.files.upload.return_value = fake_uploaded

        fake_processed = MagicMock(name="processed_file")

        with patch.object(ga, "_get_client", return_value=fake_client), \
             patch.object(ga, "wait_for_processing", return_value=fake_processed):
            result_file, result_free = ga.upload_video(video, use_free=False)

        assert result_file is fake_processed
        assert result_free is False

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            ga.upload_video(Path("/nonexistent/video.mp4"))

    def test_quota_error_retries_with_free_tier(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00" * 5000)

        paid_client = MagicMock()
        paid_client.files.upload.side_effect = Exception("429 quota exceeded")

        free_client = MagicMock()
        fake_uploaded = MagicMock()
        fake_uploaded.name = "files/free123"
        free_client.files.upload.return_value = fake_uploaded

        fake_processed = MagicMock()

        def mock_get_client(use_free=False):
            if use_free:
                return free_client
            return paid_client

        with patch.object(ga, "_get_client", side_effect=mock_get_client), \
             patch.object(ga, "GEMINI_API_KEY", "free-key"), \
             patch.object(ga, "wait_for_processing", return_value=fake_processed):
            result_file, result_free = ga.upload_video(video, use_free=False)

        assert result_free is True
        assert result_file is fake_processed

    def test_non_quota_error_raises(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00" * 5000)

        fake_client = MagicMock()
        fake_client.files.upload.side_effect = Exception("authentication failed")

        with patch.object(ga, "_get_client", return_value=fake_client), \
             patch.object(ga, "GEMINI_API_KEY", "free-key"):
            with pytest.raises(Exception, match="authentication failed"):
                ga.upload_video(video, use_free=False)


# ===========================================================================
# 9. wait_for_processing
# ===========================================================================


class TestWaitForProcessing:
    """wait_for_processing polls until ACTIVE, FAILED, or timeout."""

    def setup_method(self):
        _reset_client_caches()

    def test_file_becomes_active(self):
        file_info = MagicMock()
        file_info.state = "ACTIVE"
        client = MagicMock()
        client.files.get.return_value = file_info

        result = ga.wait_for_processing(client, "files/abc")
        assert result is file_info

    def test_file_fails_raises_runtime_error(self):
        file_info = MagicMock()
        file_info.state = "FAILED"
        client = MagicMock()
        client.files.get.return_value = file_info

        with pytest.raises(RuntimeError, match="processing failed"):
            ga.wait_for_processing(client, "files/abc")

    def test_timeout_raises_timeout_error(self):
        file_info = MagicMock()
        file_info.state = "PROCESSING"
        client = MagicMock()
        client.files.get.return_value = file_info

        with patch.object(ga, "FILE_POLL_TIMEOUT", 20), \
             patch.object(ga, "FILE_POLL_INTERVAL", 10), \
             patch("tools.gemini_analyzer.time.sleep"):
            with pytest.raises(TimeoutError, match="timed out"):
                ga.wait_for_processing(client, "files/abc")

    def test_consecutive_errors_raises_runtime_error(self):
        client = MagicMock()
        client.files.get.side_effect = Exception("network error")

        with patch.object(ga, "FILE_POLL_TIMEOUT", 600), \
             patch.object(ga, "FILE_POLL_INTERVAL", 1), \
             patch("tools.gemini_analyzer.time.sleep"):
            with pytest.raises(RuntimeError, match="Too many consecutive errors"):
                ga.wait_for_processing(client, "files/abc")

    def test_transient_errors_recover(self):
        """A few errors followed by ACTIVE should succeed."""
        file_info_active = MagicMock()
        file_info_active.state = "ACTIVE"

        client = MagicMock()
        client.files.get.side_effect = [
            Exception("network hiccup"),
            Exception("network hiccup"),
            file_info_active,
        ]

        with patch("tools.gemini_analyzer.time.sleep"):
            result = ga.wait_for_processing(client, "files/abc")

        assert result is file_info_active


# ===========================================================================
# 10. _generate_with_retry
# ===========================================================================


class TestGenerateWithRetry:
    """_generate_with_retry handles retries, quota fallback, and error conditions."""

    def setup_method(self):
        _reset_client_caches()

    def _make_response(self, text="generated text"):
        resp = MagicMock()
        resp.text = text
        return resp

    def test_successful_generation(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._make_response("hello")

        with patch("tools.gemini_analyzer.types.GenerateContentConfig", MagicMock):
            result = ga._generate_with_retry(
                client, "model-x", ["content"], "test purpose"
            )
        assert result == "hello"

    def test_empty_response_raises_value_error(self):
        resp = MagicMock()
        resp.text = ""
        client = MagicMock()
        client.models.generate_content.return_value = resp

        with patch("tools.gemini_analyzer.time.sleep"), \
             patch("tools.gemini_analyzer.types.GenerateContentConfig", MagicMock):
            with pytest.raises(RuntimeError, match="failed after"):
                ga._generate_with_retry(
                    client, "model-x", ["content"], "test purpose"
                )

    def test_blocked_response_raises_value_error(self):
        resp = MagicMock()
        type(resp).text = property(lambda self: (_ for _ in ()).throw(ValueError("blocked")))
        client = MagicMock()
        client.models.generate_content.return_value = resp

        with patch("tools.gemini_analyzer.time.sleep"), \
             patch("tools.gemini_analyzer.types.GenerateContentConfig", MagicMock):
            with pytest.raises(RuntimeError, match="failed after"):
                ga._generate_with_retry(
                    client, "model-x", ["content"], "test purpose"
                )

    def test_quota_error_falls_back_to_free_tier(self):
        paid_client = MagicMock()
        paid_client.models.generate_content.side_effect = Exception("429 rate limit")

        free_client = MagicMock()
        free_client.models.generate_content.return_value = self._make_response("free result")

        with patch.object(ga, "_get_client", return_value=free_client), \
             patch.object(ga, "GEMINI_API_KEY", "free-key"), \
             patch("tools.gemini_analyzer.types.GenerateContentConfig", MagicMock):
            result = ga._generate_with_retry(
                paid_client, "model-x", ["content"], "test",
                use_free=False,
            )

        assert result == "free result"

    def test_max_retries_exhausted_raises_runtime_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("server error")

        with patch("tools.gemini_analyzer.time.sleep"), \
             patch.object(ga, "GEMINI_API_KEY", ""), \
             patch("tools.gemini_analyzer.types.GenerateContentConfig", MagicMock):
            with pytest.raises(RuntimeError, match="failed after"):
                ga._generate_with_retry(
                    client, "model-x", ["content"], "test",
                    use_free=True,
                )


# ===========================================================================
# 11. transcribe_video
# ===========================================================================


class TestTranscribeVideo:
    """transcribe_video picks the right prompt based on chunk_number."""

    def setup_method(self):
        _reset_client_caches()

    def test_chunk_0_uses_transcription_prompt(self):
        file_ref = MagicMock()

        with patch.object(ga, "_get_client", return_value=MagicMock()), \
             patch.object(ga, "_generate_with_retry", return_value="transcript") as mock_gen:
            ga.transcribe_video(file_ref, use_free=False, chunk_number=0, total_chunks=1)

        call_args = mock_gen.call_args
        contents = call_args[1].get("contents") or call_args[0][2]
        # The prompt should be TRANSCRIPTION_PROMPT (not continuation)
        assert contents[1] is ga.TRANSCRIPTION_PROMPT

    def test_chunk_1_uses_continuation_prompt(self):
        file_ref = MagicMock()

        with patch.object(ga, "_get_client", return_value=MagicMock()), \
             patch.object(ga, "_generate_with_retry", return_value="transcript") as mock_gen:
            ga.transcribe_video(file_ref, use_free=False, chunk_number=1, total_chunks=3)

        call_args = mock_gen.call_args
        contents = call_args[1].get("contents") or call_args[0][2]
        prompt = contents[1]
        assert prompt is not ga.TRANSCRIPTION_PROMPT
        # Should contain chunk_number+1 = 2
        assert "2" in prompt


# ===========================================================================
# 12. _claude_reason — rate limit retry
# ===========================================================================


class TestClaudeReasonRateLimit:
    """_claude_reason retries on RateLimitError with exponential backoff."""

    def setup_method(self):
        _reset_client_caches()

    def _make_fake_response(self, text: str):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = text
        response = MagicMock()
        response.content = [text_block]
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50
        return response

    def test_rate_limit_retries_then_succeeds(self):
        fake_response = self._make_fake_response("analysis result")
        fake_client = MagicMock()
        # Fail twice with RateLimitError, then succeed
        fake_client.messages.create.side_effect = [
            ga.anthropic.RateLimitError("rate limited"),
            ga.anthropic.RateLimitError("rate limited"),
            fake_response,
        ]

        with patch.object(ga, "ANTHROPIC_API_KEY", "test-key"), \
             patch.object(ga, "_anthropic_client_cache", fake_client), \
             patch("tools.gemini_analyzer.time.sleep") as mock_sleep:
            result = ga._claude_reason("transcript", "prompt", "test")

        assert result == "analysis result"
        # Should have slept twice (65s, 130s)
        assert mock_sleep.call_count == 2

    def test_rate_limit_max_attempts_raises(self):
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = ga.anthropic.RateLimitError("rate limited")

        with patch.object(ga, "ANTHROPIC_API_KEY", "test-key"), \
             patch.object(ga, "_anthropic_client_cache", fake_client), \
             patch("tools.gemini_analyzer.time.sleep"):
            with pytest.raises(RuntimeError, match="rate limit"):
                ga._claude_reason("transcript", "prompt", "test")


# ===========================================================================
# 13. _gemini_write_georgian
# ===========================================================================


class TestGeminiWriteGeorgian:
    """_gemini_write_georgian passes Claude's analysis to Gemini for Georgian output."""

    def setup_method(self):
        _reset_client_caches()

    def test_calls_generate_with_retry(self):
        fake_client = MagicMock()

        with patch.object(ga, "_get_client", return_value=fake_client), \
             patch.object(ga, "_generate_with_retry", return_value="Georgian text") as mock_gen:
            result = ga._gemini_write_georgian(
                "Claude analysis", "base prompt", "summary", use_free=False
            )

        assert result == "Georgian text"
        call_args = mock_gen.call_args
        assert call_args[1]["model"] == ga.GEMINI_MODEL_ANALYSIS
        assert call_args[1]["max_output_tokens"] == 32768

    def test_prompt_contains_claude_analysis(self):
        fake_client = MagicMock()

        with patch.object(ga, "_get_client", return_value=fake_client), \
             patch.object(ga, "_generate_with_retry", return_value="output") as mock_gen:
            ga._gemini_write_georgian(
                "My analysis text", "base prompt", "test", use_free=True
            )

        call_args = mock_gen.call_args
        contents = call_args[1].get("contents") or call_args[0][2]
        assert "My analysis text" in contents[0]


# ===========================================================================
# 14. generate_summary / generate_gap_analysis / generate_deep_analysis
# ===========================================================================


class TestGenerateFunctions:
    """Each generate_* function calls _claude_reason then _gemini_write_georgian."""

    def setup_method(self):
        _reset_client_caches()

    def test_generate_summary(self):
        with patch.object(ga, "_claude_reason", return_value="english analysis") as mock_cr, \
             patch.object(ga, "_gemini_write_georgian", return_value="Georgian summary") as mock_gw:
            result = ga.generate_summary("transcript text", use_free=False)

        assert result == "Georgian summary"
        mock_cr.assert_called_once()
        mock_gw.assert_called_once()
        # Check purpose
        assert mock_cr.call_args[1].get("purpose") or mock_cr.call_args[0][2] == "summary"

    def test_generate_gap_analysis(self):
        with patch.object(ga, "_claude_reason", return_value="english gap") as mock_cr, \
             patch.object(ga, "_gemini_write_georgian", return_value="Georgian gap") as mock_gw:
            result = ga.generate_gap_analysis("transcript text", use_free=True)

        assert result == "Georgian gap"
        mock_cr.assert_called_once()
        mock_gw.assert_called_once()

    def test_generate_deep_analysis(self):
        with patch.object(ga, "_claude_reason", return_value="english deep") as mock_cr, \
             patch.object(ga, "_gemini_write_georgian", return_value="Georgian deep") as mock_gw:
            result = ga.generate_deep_analysis("transcript text", use_free=False)

        assert result == "Georgian deep"
        mock_cr.assert_called_once()
        mock_gw.assert_called_once()

    def test_generate_summary_passes_summarization_prompt_to_gemini(self):
        with patch.object(ga, "_claude_reason", return_value="analysis"), \
             patch.object(ga, "_gemini_write_georgian", return_value="output") as mock_gw:
            ga.generate_summary("transcript")

        # Second positional arg to _gemini_write_georgian is the prompt
        call_args = mock_gw.call_args
        prompt_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("prompt")
        assert prompt_arg is ga.SUMMARIZATION_PROMPT

    def test_generate_gap_passes_gap_prompt_to_gemini(self):
        with patch.object(ga, "_claude_reason", return_value="analysis"), \
             patch.object(ga, "_gemini_write_georgian", return_value="output") as mock_gw:
            ga.generate_gap_analysis("transcript")

        call_args = mock_gw.call_args
        prompt_arg = call_args[0][1]
        assert prompt_arg is ga.GAP_ANALYSIS_PROMPT

    def test_generate_deep_passes_deep_prompt_to_gemini(self):
        with patch.object(ga, "_claude_reason", return_value="analysis"), \
             patch.object(ga, "_gemini_write_georgian", return_value="output") as mock_gw:
            ga.generate_deep_analysis("transcript")

        call_args = mock_gw.call_args
        prompt_arg = call_args[0][1]
        assert prompt_arg is ga.DEEP_ANALYSIS_PROMPT


# ===========================================================================
# 15. _get_client — paid key not configured, fallback to free (lines 91-95)
# ===========================================================================


class TestGetClientPaidFallback:
    """When GEMINI_API_KEY_PAID is empty but GEMINI_API_KEY is set,
    _get_client(use_free=False) falls back to the free key with a warning."""

    def setup_method(self):
        _reset_client_caches()

    def test_paid_key_missing_falls_back_to_free(self):
        fake_client = MagicMock()
        mock_genai_client = MagicMock(return_value=fake_client)

        with patch.object(ga, "GEMINI_API_KEY_PAID", ""), \
             patch.object(ga, "GEMINI_API_KEY", "free-key-123"), \
             patch("tools.gemini_analyzer.genai.Client", mock_genai_client, create=True):
            result = ga._get_client(use_free=False)

        assert result is fake_client
        mock_genai_client.assert_called_once_with(api_key="free-key-123")

    def test_paid_key_missing_caches_free_client(self):
        fake_client = MagicMock()
        mock_genai_client = MagicMock(return_value=fake_client)

        with patch.object(ga, "GEMINI_API_KEY_PAID", ""), \
             patch.object(ga, "GEMINI_API_KEY", "free-key-123"), \
             patch("tools.gemini_analyzer.genai.Client", mock_genai_client, create=True):
            first = ga._get_client(use_free=False)
            second = ga._get_client(use_free=False)

        assert first is second
        assert mock_genai_client.call_count == 1


# ===========================================================================
# 16. _claude_reason — generic exception retry (lines 534-546)
# ===========================================================================


class TestClaudeReasonGenericRetry:
    """_claude_reason retries on generic exceptions with exponential backoff."""

    def setup_method(self):
        _reset_client_caches()

    def _make_fake_response(self, text: str):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = text
        response = MagicMock()
        response.content = [text_block]
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50
        return response

    def test_generic_error_retries_then_succeeds(self):
        fake_response = self._make_fake_response("result")
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            Exception("network blip"),
            Exception("timeout"),
            fake_response,
        ]

        with patch.object(ga, "ANTHROPIC_API_KEY", "test-key"), \
             patch.object(ga, "_anthropic_client_cache", fake_client), \
             patch("tools.gemini_analyzer.time.sleep") as mock_sleep:
            result = ga._claude_reason("transcript", "prompt", "test")

        assert result == "result"
        assert mock_sleep.call_count == 2

    def test_generic_error_max_attempts_raises(self):
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = Exception("persistent failure")

        with patch.object(ga, "ANTHROPIC_API_KEY", "test-key"), \
             patch.object(ga, "_anthropic_client_cache", fake_client), \
             patch("tools.gemini_analyzer.time.sleep"):
            with pytest.raises(RuntimeError, match="failed after 5 attempts"):
                ga._claude_reason("transcript", "prompt", "test")


# ===========================================================================
# 17. transcribe_chunked_video (lines 399-465)
# ===========================================================================


class TestTranscribeChunkedVideo:
    """transcribe_chunked_video: split -> upload -> transcribe -> cleanup."""

    def setup_method(self):
        _reset_client_caches()

    def test_single_chunk_flow(self, tmp_path):
        """Short video: no splitting, single upload+transcribe."""
        video = tmp_path / "short.mp4"
        video.write_bytes(b"\x00" * 1000)

        fake_file_ref = MagicMock()
        fake_file_ref.name = "files/abc"

        fake_client = MagicMock()

        with patch.object(ga, "split_video_chunks", return_value=[video]), \
             patch.object(ga, "upload_video", return_value=(fake_file_ref, False)), \
             patch.object(ga, "transcribe_video", return_value="transcript chunk 1"), \
             patch.object(ga, "_get_client", return_value=fake_client):
            transcript, use_free = ga.transcribe_chunked_video(video, use_free=False)

        assert transcript == "transcript chunk 1"
        assert use_free is False

    def test_multi_chunk_concatenation(self, tmp_path):
        """Two chunks should be concatenated with double newline."""
        video = tmp_path / "long.mp4"
        video.write_bytes(b"\x00" * 1000)
        chunk0 = tmp_path / "long.chunk0.mp4"
        chunk0.write_bytes(b"\x00" * 1000)
        chunk1 = tmp_path / "long.chunk1.mp4"
        chunk1.write_bytes(b"\x00" * 1000)

        file_ref0 = MagicMock()
        file_ref0.name = "files/chunk0"
        file_ref1 = MagicMock()
        file_ref1.name = "files/chunk1"

        fake_client = MagicMock()

        upload_returns = iter([(file_ref0, False), (file_ref1, False)])
        transcribe_returns = iter(["Part one text", "Part two text"])

        with patch.object(ga, "split_video_chunks", return_value=[chunk0, chunk1]), \
             patch.object(ga, "upload_video", side_effect=lambda p, **kw: next(upload_returns)), \
             patch.object(ga, "transcribe_video", side_effect=lambda *a, **kw: next(transcribe_returns)), \
             patch.object(ga, "_get_client", return_value=fake_client):
            transcript, use_free = ga.transcribe_chunked_video(video, use_free=False)

        assert transcript == "Part one text\n\nPart two text"

    def test_cleanup_on_failure(self, tmp_path):
        """On transcription failure, Gemini files and local chunks are cleaned up."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"\x00" * 1000)
        chunk0 = tmp_path / "lecture.chunk0.mp4"
        chunk0.write_bytes(b"\x00" * 1000)

        file_ref0 = MagicMock()
        file_ref0.name = "files/chunk0"

        fake_client = MagicMock()

        with patch.object(ga, "split_video_chunks", return_value=[chunk0]), \
             patch.object(ga, "upload_video", return_value=(file_ref0, False)), \
             patch.object(ga, "transcribe_video", side_effect=RuntimeError("transcription failed")), \
             patch.object(ga, "_get_client", return_value=fake_client):
            with pytest.raises(RuntimeError, match="transcription failed"):
                ga.transcribe_chunked_video(video, use_free=False)

        # Gemini file cleanup was attempted (in finally block)
        fake_client.files.delete.assert_called()

    def test_tier_switch_propagated(self, tmp_path):
        """If upload switches to free tier, transcribe uses free tier too."""
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 1000)

        file_ref = MagicMock()
        file_ref.name = "files/f"
        fake_client = MagicMock()

        with patch.object(ga, "split_video_chunks", return_value=[video]), \
             patch.object(ga, "upload_video", return_value=(file_ref, True)), \
             patch.object(ga, "transcribe_video", return_value="text") as mock_tv, \
             patch.object(ga, "_get_client", return_value=fake_client):
            transcript, use_free = ga.transcribe_chunked_video(video, use_free=False)

        assert use_free is True
        # transcribe_video should have been called with use_free=True
        assert mock_tv.call_args[1].get("use_free") is True


# ===========================================================================
# 18. analyze_lecture — alert_operator import path (lines 720-749)
# ===========================================================================


class TestAnalyzeLectureAlertImport:
    """Exercise the try/except import of alert_operator inside analyze_lecture,
    ensuring failures in the alert import itself don't crash the pipeline."""

    def setup_method(self):
        _reset_client_caches()

    def test_alert_operator_import_failure_swallowed(self):
        """If importing alert_operator itself fails, analyze_lecture still returns."""
        with patch.object(ga, "transcribe_chunked_video", return_value=("transcript", False)), \
             patch.object(ga, "generate_summary", side_effect=RuntimeError("fail")), \
             patch.object(ga, "generate_gap_analysis", side_effect=RuntimeError("fail")), \
             patch.object(ga, "generate_deep_analysis", side_effect=RuntimeError("fail")), \
             patch("builtins.__import__", side_effect=_selective_import_error):
            result = ga.analyze_lecture(Path("/fake/video.mp4"))

        assert result["transcript"] == "transcript"
        assert result["summary"] == ""
        assert result["gap_analysis"] == ""
        assert result["deep_analysis"] == ""


def _selective_import_error(name, *args, **kwargs):
    """Allow all imports except tools.whatsapp_sender."""
    if name == "tools.whatsapp_sender":
        raise ImportError("simulated import failure")
    return __builtins__.__import__(name, *args, **kwargs)  # type: ignore[attr-defined]
