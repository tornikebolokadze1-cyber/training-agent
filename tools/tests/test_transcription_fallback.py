"""Tests for the transcript-degradation fallback path in gemini_analyzer.

Covers the patch landed 2026-05-15 in response to G3 L2 chunk 6/21 prompt-echo:
- `transcribe_video()` now accepts an optional `model` argument that overrides
  the default `GEMINI_MODEL_TRANSCRIPTION`.
- `transcribe_video()` automatically derives `disable_thinking` from the
  chosen model (Pro models require thinking mode and reject `Budget 0`).
- The constant `GEMINI_FALLBACK_TRANSCRIPTION_MODEL` is now actually wired
  up — previously defined but unused.

External API clients are fully stubbed via conftest.py.

Run with:
    pytest tools/tests/test_transcription_fallback.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import tools.integrations.gemini_analyzer as ga


# ===========================================================================
# 1. transcribe_video — `model` argument and `disable_thinking` derivation
# ===========================================================================


class TestTranscribeVideoModelOverride:
    """The new `model` parameter and the auto-derived `disable_thinking`."""

    def _capture_generate_kwargs(self) -> dict:
        """Patch `_generate_with_retry` and return the kwargs it was called with."""
        captured: dict = {}

        def _fake_generate(client, **kwargs):
            captured.update(kwargs)
            return "transcript text"

        return captured, _fake_generate

    def test_default_model_is_primary_transcription_model(self):
        captured, fake = self._capture_generate_kwargs()
        with patch.object(ga, "_generate_with_retry", side_effect=fake):
            with patch.object(ga, "_get_client", return_value=MagicMock()):
                ga.transcribe_video(MagicMock(), chunk_number=0, total_chunks=1)
        assert captured["model"] == ga.GEMINI_MODEL_TRANSCRIPTION

    def test_model_override_is_passed_through(self):
        captured, fake = self._capture_generate_kwargs()
        with patch.object(ga, "_generate_with_retry", side_effect=fake):
            with patch.object(ga, "_get_client", return_value=MagicMock()):
                ga.transcribe_video(
                    MagicMock(),
                    chunk_number=0,
                    total_chunks=1,
                    model="gemini-2.5-flash",
                )
        assert captured["model"] == "gemini-2.5-flash"

    def test_disable_thinking_true_for_flash_lite(self):
        captured, fake = self._capture_generate_kwargs()
        with patch.object(ga, "_generate_with_retry", side_effect=fake):
            with patch.object(ga, "_get_client", return_value=MagicMock()):
                ga.transcribe_video(
                    MagicMock(),
                    chunk_number=0,
                    total_chunks=1,
                    model="gemini-2.5-flash-lite",
                )
        assert captured["disable_thinking"] is True

    def test_disable_thinking_true_for_flash_full(self):
        captured, fake = self._capture_generate_kwargs()
        with patch.object(ga, "_generate_with_retry", side_effect=fake):
            with patch.object(ga, "_get_client", return_value=MagicMock()):
                ga.transcribe_video(
                    MagicMock(),
                    chunk_number=0,
                    total_chunks=1,
                    model="gemini-2.5-flash",
                )
        assert captured["disable_thinking"] is True

    def test_disable_thinking_false_for_pro(self):
        """Pro models reject Budget 0 — disable_thinking must be False for them."""
        captured, fake = self._capture_generate_kwargs()
        with patch.object(ga, "_generate_with_retry", side_effect=fake):
            with patch.object(ga, "_get_client", return_value=MagicMock()):
                ga.transcribe_video(
                    MagicMock(),
                    chunk_number=0,
                    total_chunks=1,
                    model="gemini-2.5-pro",
                )
        assert captured["disable_thinking"] is False

    def test_disable_thinking_false_for_pro_preview(self):
        captured, fake = self._capture_generate_kwargs()
        with patch.object(ga, "_generate_with_retry", side_effect=fake):
            with patch.object(ga, "_get_client", return_value=MagicMock()):
                ga.transcribe_video(
                    MagicMock(),
                    chunk_number=0,
                    total_chunks=1,
                    model="gemini-3.1-pro-preview",
                )
        assert captured["disable_thinking"] is False


# ===========================================================================
# 2. Module-level constants
# ===========================================================================


class TestFallbackConstants:
    """The fallback model default and its safety properties."""

    def test_fallback_model_constant_exists_and_is_truthy(self):
        assert ga.GEMINI_FALLBACK_TRANSCRIPTION_MODEL
        assert isinstance(ga.GEMINI_FALLBACK_TRANSCRIPTION_MODEL, str)

    def test_fallback_model_is_distinct_from_primary(self):
        """Otherwise the retry would re-call the same broken path."""
        assert (
            ga.GEMINI_FALLBACK_TRANSCRIPTION_MODEL
            != ga.GEMINI_MODEL_TRANSCRIPTION
        )

    def test_default_fallback_does_not_require_thinking_mode(self):
        """Pro models reject Budget 0; the default fallback must not be pro.

        If someone changes the default to a pro variant in the future, this
        test will fail and force them to also fix the disable_thinking path.
        """
        assert "pro" not in ga.GEMINI_FALLBACK_TRANSCRIPTION_MODEL


# ===========================================================================
# 3. _is_safety_or_recitation_error — safety-filter detection
# ===========================================================================


class TestIsSafetyOrRecitationError:
    """Detect Gemini safety/recitation blocks so fallback model can retry."""

    def test_safety_filter_phrase_detected(self):
        exc = Exception("Gemini SAFETY FILTER blocked transcription (chunk 13/21)")
        assert ga._is_safety_or_recitation_error(exc)

    def test_recitation_phrase_detected(self):
        exc = ValueError(
            "Gemini SAFETY FILTER blocked transcription "
            "(finish_reason=FinishReason.RECITATION)"
        )
        assert ga._is_safety_or_recitation_error(exc)

    def test_recitation_lowercase_only(self):
        exc = Exception("blocked due to recitation policy")
        assert ga._is_safety_or_recitation_error(exc)

    def test_quota_error_not_matched(self):
        assert not ga._is_safety_or_recitation_error(
            Exception("429 resource exhausted")
        )

    def test_network_error_not_matched(self):
        assert not ga._is_safety_or_recitation_error(
            Exception("Connection reset by peer")
        )

    def test_generic_error_not_matched(self):
        assert not ga._is_safety_or_recitation_error(
            ValueError("Something went wrong")
        )

    def test_empty_message_not_matched(self):
        assert not ga._is_safety_or_recitation_error(Exception(""))


# ===========================================================================
# 4. Prompt-echo phrase list — chatbot-style refusals
# ===========================================================================


class TestPromptEchoPhraseList:
    """The G3 L2 chunk 0 incident added phrases for 'please upload' refusals."""

    def test_contains_original_three_phrases(self):
        """The pre-2026-05-15 phrases must still be present."""
        for phrase in (
            "გადმოეცი ყველაფერი ზუსტად",
            "მონიშნე ვინ ლაპარაკობს",
            "შენ ხარ პროფესიონალი ტრანსკრიპტორი",
        ):
            assert phrase in ga._PROMPT_ECHO_PHRASES

    def test_detects_please_upload_audio_refusal(self):
        """Chunk-0 refusal pattern: 'please upload the audio file'."""
        text = (
            "x" * 60 + "\n"
            "რა თქმა უნდა, სიამოვნებით მოგისმენთ. "
            "გთხოვთ, ატვირთოთ აუდიო ფაილი, რათა დავიწყო მუშაობა."
        )
        result = ga._detect_transcript_degradation(text, "chunk 1/21")
        assert result is not None
        assert "prompt-echo" in result

    def test_detects_glad_to_listen_refusal(self):
        """Variant: 'I'd be happy to listen' chatbot reply."""
        text = (
            "x" * 60 + "\n"
            "სიამოვნებით მოგისმენთ ლექციას, თუ მომაწოდებთ აუდიო ფაილს."
        )
        result = ga._detect_transcript_degradation(text, "chunk 1/21")
        assert result is not None
        assert "prompt-echo" in result
