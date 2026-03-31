"""Unit tests for tools/transcribe_lecture.py.

Covers the Wave 1 alerting fixes and transcript-resume threshold:
- Drive upload failure alerts operator
- Drive private report failure alerts operator
- WhatsApp notification failure alerts operator
- Private report failure logs CRITICAL (not alert_operator)
- Pinecone indexing failure alerts operator
- Quality gate: empty analyses alert operator
- Transcript resume threshold is 2000 chars (not 500)

Run with:
    pytest tools/tests/test_transcribe.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.services.transcribe_lecture as tl

# ===========================================================================
# Helpers
# ===========================================================================

# The safe_operation decorator does a lazy import of alert_operator via the
# module (tools.integrations.whatsapp_sender.alert_operator), so we must
# patch at the module level. Direct calls in transcribe_lecture also go
# through the local import, so we patch both for full coverage.
_PATCH_ALERT = "tools.integrations.whatsapp_sender.alert_operator"
_PATCH_ALERT_LOCAL = "tools.services.transcribe_lecture.alert_operator"
_PATCH_SEND_GROUP = "tools.services.transcribe_lecture.send_group_upload_notification"
_PATCH_SEND_PRIVATE = "tools.services.transcribe_lecture.send_private_report"
_PATCH_CREATE_DOC = "tools.services.transcribe_lecture.create_google_doc"
_PATCH_ENSURE_FOLDER = "tools.services.transcribe_lecture.ensure_folder"
_PATCH_GET_SERVICE = "tools.services.transcribe_lecture.get_drive_service"
_PATCH_INDEX = "tools.services.transcribe_lecture.index_lecture_content"
_PATCH_ANALYZE = "tools.services.transcribe_lecture.analyze_lecture"
_PATCH_GET_FOLDER_ID = "tools.services.transcribe_lecture._get_lecture_folder_id"


# ===========================================================================
# 1. Drive summary upload failure alerts operator
# ===========================================================================

class TestUploadSummaryDriveFailure:
    """_upload_summary_to_drive must call alert_operator when create_google_doc raises."""

    def test_drive_create_doc_failure_calls_alert_operator(self):
        with (
            patch(_PATCH_GET_FOLDER_ID, return_value="folder-id-123"),
            patch(_PATCH_CREATE_DOC, side_effect=RuntimeError("Drive quota exceeded")),
            patch(_PATCH_ALERT) as mock_alert,
        ):
            result = tl._upload_summary_to_drive(1, 2, "lecture summary text")

        assert result is None
        mock_alert.assert_called_once()
        alert_message = mock_alert.call_args[0][0]
        assert "Drive" in alert_message or "FAILED" in alert_message

    def test_alert_message_contains_operation_and_error(self):
        with (
            patch(_PATCH_GET_FOLDER_ID, return_value="folder-id-abc"),
            patch(_PATCH_CREATE_DOC, side_effect=OSError("network error")),
            patch(_PATCH_ALERT) as mock_alert,
        ):
            tl._upload_summary_to_drive(2, 5, "summary")

        alert_message = mock_alert.call_args[0][0]
        # safe_operation formats alert as "{operation_name} FAILED: {error}"
        assert "Drive summary upload" in alert_message
        assert "network error" in alert_message

    def test_successful_upload_does_not_call_alert_operator(self):
        with (
            patch(_PATCH_GET_FOLDER_ID, return_value="folder-id-xyz"),
            patch(_PATCH_CREATE_DOC, return_value="doc-id-999"),
            patch(_PATCH_ALERT) as mock_alert,
        ):
            result = tl._upload_summary_to_drive(1, 1, "summary")

        assert result == "doc-id-999"
        mock_alert.assert_not_called()


# ===========================================================================
# 2. Drive private report upload failure alerts operator
# ===========================================================================

class TestUploadPrivateReportDriveFailure:
    """_upload_private_report_to_drive must call alert_operator on failure."""

    def test_drive_create_doc_failure_calls_alert_operator(self):
        with (
            patch(_PATCH_CREATE_DOC, side_effect=ConnectionError("timeout")),
            patch(_PATCH_ALERT) as mock_alert,
        ):
            # Group must have an analysis_folder_id set; patch GROUPS directly
            with patch.dict(
                tl.GROUPS,
                {1: {"analysis_folder_id": "analysis-folder-1", "name": "ჯგუფი #1",
                     "drive_folder_id": "root-folder-1", "meeting_days": [1, 4],
                     "start_date": None, "whatsapp_group_id": ""}},
            ):
                result = tl._upload_private_report_to_drive(1, 3, "gap text", "deep text")

        assert result is None
        mock_alert.assert_called_once()

    def test_alert_message_mentions_private_report(self):
        with (
            patch(_PATCH_CREATE_DOC, side_effect=ValueError("serialization error")),
            patch(_PATCH_ALERT) as mock_alert,
        ):
            with patch.dict(
                tl.GROUPS,
                {2: {"analysis_folder_id": "analysis-folder-2", "name": "ჯგუფი #2",
                     "drive_folder_id": "root-folder-2", "meeting_days": [0, 3],
                     "start_date": None, "whatsapp_group_id": ""}},
            ):
                tl._upload_private_report_to_drive(2, 7, "gaps", "deep")

        alert_message = mock_alert.call_args[0][0]
        assert "private" in alert_message.lower() or "FAILED" in alert_message

    def test_missing_analysis_folder_returns_none_without_alert(self):
        """When analysis_folder_id is absent the function returns None silently — no crash."""
        with patch(_PATCH_ALERT) as mock_alert:
            with patch.dict(
                tl.GROUPS,
                {1: {"analysis_folder_id": None, "name": "ჯგუფი #1",
                     "drive_folder_id": "root", "meeting_days": [1, 4],
                     "start_date": None, "whatsapp_group_id": ""}},
            ):
                result = tl._upload_private_report_to_drive(1, 1, "gaps", "deep")

        assert result is None
        mock_alert.assert_not_called()


# ===========================================================================
# 3. WhatsApp group notification failure alerts operator
# ===========================================================================

class TestNotifyGroupWhatsAppFailure:
    """_notify_group_whatsapp must call alert_operator when send_group_upload_notification raises."""

    def test_whatsapp_send_failure_calls_alert_operator(self):
        with (
            patch(_PATCH_SEND_GROUP, side_effect=RuntimeError("Green API down")),
            patch(_PATCH_ALERT) as mock_alert,
        ):
            tl._notify_group_whatsapp(1, 4, "file-id-abc", "doc-id-xyz")

        mock_alert.assert_called_once()
        alert_message = mock_alert.call_args[0][0]
        assert "WhatsApp" in alert_message or "FAILED" in alert_message

    def test_no_summary_doc_id_skips_without_alerting(self):
        """If summary_doc_id is None the notification is skipped — no alert, no send."""
        with (
            patch(_PATCH_SEND_GROUP) as mock_send,
            patch(_PATCH_ALERT) as mock_alert,
        ):
            tl._notify_group_whatsapp(1, 1, "file-id", None)

        mock_send.assert_not_called()
        mock_alert.assert_not_called()

    def test_successful_notification_does_not_alert(self):
        with (
            patch(_PATCH_SEND_GROUP),
            patch(_PATCH_ALERT) as mock_alert,
        ):
            tl._notify_group_whatsapp(2, 3, "rec-id", "doc-id")

        mock_alert.assert_not_called()


# ===========================================================================
# 4. Private report delivery failure logs CRITICAL (not alert_operator)
# ===========================================================================

class TestSendPrivateReportToTornike:
    """_send_private_report_to_tornike uses @safe_operation(alert=False).

    On failure it logs ERROR (via safe_operation) and does NOT call
    alert_operator — because this IS the private channel to Tornike.
    """

    def test_send_failure_logs_error_not_alert(self):
        """Failure should log error via safe_operation, not call alert_operator."""
        with (
            patch(_PATCH_SEND_PRIVATE, side_effect=ConnectionError("WhatsApp offline")),
            patch(_PATCH_ALERT) as mock_alert,
        ):
            # Should NOT raise — safe_operation catches it
            tl._send_private_report_to_tornike(1, 3, "doc-id-123")

        # Operator alert must NOT be used — alert=False in decorator
        mock_alert.assert_not_called()

    def test_failure_does_not_propagate(self):
        """safe_operation ensures failure doesn't propagate to caller."""
        with (
            patch(_PATCH_SEND_PRIVATE, side_effect=OSError("send failed")),
            patch(_PATCH_ALERT),
        ):
            # Must not raise
            result = tl._send_private_report_to_tornike(2, 8, None)

        assert result is None  # safe_operation returns default=None

    def test_successful_delivery_does_not_log_critical(self):
        with (
            patch(_PATCH_SEND_PRIVATE),
            patch.object(tl.logger, "critical") as mock_critical,
        ):
            tl._send_private_report_to_tornike(1, 2, "doc-id")

        mock_critical.assert_not_called()


# ===========================================================================
# 5. Pinecone indexing failure alerts operator
# ===========================================================================

class TestPineconeIndexingFailure:
    """transcribe_and_index must call alert_operator when index_lecture_content raises."""

    def _make_results(self, **overrides) -> dict:
        base = {
            "transcript": "t" * 3000,
            "summary": "s" * 600,
            "gap_analysis": "g" * 400,
            "deep_analysis": "d" * 400,
        }
        base.update(overrides)
        return base

    def test_indexing_failure_calls_alert_operator(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        with (
            patch(_PATCH_ANALYZE, return_value=self._make_results()),
            patch(_PATCH_INDEX, side_effect=RuntimeError("Pinecone unavailable")),
            patch(_PATCH_ALERT) as mock_alert,
            patch("tools.services.transcribe_lecture._upload_summary_to_drive", return_value="doc-1"),
            patch("tools.services.transcribe_lecture._upload_private_report_to_drive", return_value="doc-2"),
            patch("tools.services.transcribe_lecture._notify_group_whatsapp"),
            patch("tools.services.transcribe_lecture._send_private_report_to_tornike"),
            patch("tools.services.transcribe_lecture._find_recording_in_drive", return_value="rec-1"),
        ):
            counts = tl.transcribe_and_index(1, 2, video)

        # alert_operator must have been called at least once for an indexing failure
        assert mock_alert.call_count >= 1
        # All index counts should be 0 after failure
        assert all(v == 0 for v in counts.values())

    def test_alert_message_mentions_pinecone(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        alert_messages: list[str] = []

        def capture_alert(msg: str) -> None:
            alert_messages.append(msg)

        with (
            patch(_PATCH_ANALYZE, return_value=self._make_results()),
            patch(_PATCH_INDEX, side_effect=ValueError("dimension mismatch")),
            patch(_PATCH_ALERT, side_effect=capture_alert),
            patch("tools.services.transcribe_lecture._upload_summary_to_drive", return_value="doc-1"),
            patch("tools.services.transcribe_lecture._upload_private_report_to_drive", return_value="doc-2"),
            patch("tools.services.transcribe_lecture._notify_group_whatsapp"),
            patch("tools.services.transcribe_lecture._send_private_report_to_tornike"),
            patch("tools.services.transcribe_lecture._find_recording_in_drive", return_value=None),
        ):
            tl.transcribe_and_index(1, 3, video)

        # At least one message should mention Pinecone
        pinecone_alerts = [m for m in alert_messages if "Pinecone" in m or "pinecone" in m.lower()]
        assert len(pinecone_alerts) >= 1


# ===========================================================================
# 6. Quality gate — empty analyses trigger alert_operator
# ===========================================================================

class TestQualityGate:
    """transcribe_and_index must call alert_operator when key analyses are empty."""

    def test_empty_summary_triggers_alert(self, tmp_path):
        """Quality gate fires BEFORE delivery when summary is empty."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        results = {
            "transcript": "t" * 3000,
            "summary": "",           # empty — triggers quality gate
            "gap_analysis": "g" * 500,
            "deep_analysis": "d" * 500,
        }

        with (
            patch(_PATCH_ANALYZE, return_value=results),
            patch(_PATCH_ALERT),
            patch(_PATCH_ALERT_LOCAL) as mock_alert_local,
        ):
            with pytest.raises(ValueError, match="Quality gate FAILED"):
                tl.transcribe_and_index(1, 1, video)

        mock_alert_local.assert_called()
        alert_args = [c[0][0] for c in mock_alert_local.call_args_list]
        quality_alerts = [a for a in alert_args if "summary" in a or "EMPTY" in a]
        assert len(quality_alerts) >= 1

    def test_all_analyses_empty_triggers_alert(self, tmp_path):
        """Quality gate fires BEFORE delivery when all analyses are empty."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        results = {
            "transcript": "t" * 3000,
            "summary": "",
            "gap_analysis": "",
            "deep_analysis": "",
        }

        with (
            patch(_PATCH_ANALYZE, return_value=results),
            patch(_PATCH_ALERT),
            patch(_PATCH_ALERT_LOCAL) as mock_alert_local,
        ):
            with pytest.raises(ValueError, match="Quality gate FAILED"):
                tl.transcribe_and_index(1, 1, video)

        mock_alert_local.assert_called()

    def test_full_analyses_do_not_trigger_quality_gate_alert(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        results = {
            "transcript": "t" * 3000,
            "summary": "s" * 600,
            "gap_analysis": "g" * 400,
            "deep_analysis": "d" * 400,
        }

        alert_messages: list[str] = []

        def capture_alert(msg: str) -> None:
            alert_messages.append(msg)

        with (
            patch(_PATCH_ANALYZE, return_value=results),
            patch(_PATCH_INDEX, return_value=10),
            patch(_PATCH_ALERT, side_effect=capture_alert),
            patch("tools.services.transcribe_lecture._upload_summary_to_drive", return_value="doc-1"),
            patch("tools.services.transcribe_lecture._upload_private_report_to_drive", return_value="doc-2"),
            patch("tools.services.transcribe_lecture._notify_group_whatsapp"),
            patch("tools.services.transcribe_lecture._send_private_report_to_tornike"),
            patch("tools.services.transcribe_lecture._find_recording_in_drive", return_value="rec-1"),
        ):
            tl.transcribe_and_index(1, 2, video)

        # No quality gate alerts should fire when all analyses are present
        quality_alerts = [a for a in alert_messages if "EMPTY" in a or "Quality gate" in a]
        assert len(quality_alerts) == 0


# ===========================================================================
# 7. Transcript resume threshold is 2000 chars (not 500)
# ===========================================================================

class TestTranscriptResumeThreshold:
    """Existing transcript under 2000 chars must NOT be reused — it must be re-transcribed."""

    def _run_pipeline(self, tmp_path: Path, transcript_content: str) -> dict:
        """Helper: write a transcript file then run the pipeline, capturing analyze_lecture calls."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        # Override TMP_DIR to use tmp_path so tests are self-contained
        fake_tmp = tmp_path / "tmp"
        fake_tmp.mkdir()

        transcript_file = fake_tmp / "g1_l1_transcript.txt"
        transcript_file.write_text(transcript_content, encoding="utf-8")

        analyze_calls: list = []

        def capture_analyze(video_path, existing_transcript=None, **kwargs):
            analyze_calls.append(existing_transcript)
            return {
                "transcript": "t" * 3000,
                "summary": "s" * 600,
                "gap_analysis": "g" * 400,
                "deep_analysis": "d" * 400,
            }

        with (
            patch("tools.services.transcribe_lecture.TMP_DIR", fake_tmp),
            patch(_PATCH_ANALYZE, side_effect=capture_analyze),
            patch(_PATCH_INDEX, return_value=5),
            patch(_PATCH_ALERT),
            patch("tools.services.transcribe_lecture._upload_summary_to_drive", return_value="doc-1"),
            patch("tools.services.transcribe_lecture._upload_private_report_to_drive", return_value="doc-2"),
            patch("tools.services.transcribe_lecture._notify_group_whatsapp"),
            patch("tools.services.transcribe_lecture._send_private_report_to_tornike"),
            patch("tools.services.transcribe_lecture._find_recording_in_drive", return_value=None),
        ):
            tl.transcribe_and_index(1, 1, video)

        return {"analyze_calls": analyze_calls}

    def test_transcript_under_2000_chars_is_not_reused(self, tmp_path):
        short_transcript = "x" * 1999  # 1 char under threshold
        outcome = self._run_pipeline(tmp_path, short_transcript)

        # analyze_lecture should have been called with existing_transcript=None
        assert outcome["analyze_calls"][0] is None, (
            "Short transcript (1999 chars) must NOT be passed to analyze_lecture"
        )

    def test_transcript_at_exactly_500_chars_is_not_reused(self, tmp_path):
        """Old threshold was 500 — confirm 500-char transcripts are still rejected."""
        transcript_500 = "y" * 500
        outcome = self._run_pipeline(tmp_path, transcript_500)

        assert outcome["analyze_calls"][0] is None, (
            "500-char transcript must NOT be reused (threshold is now 2000)"
        )

    def test_transcript_at_exactly_2000_chars_is_reused(self, tmp_path):
        long_transcript = "z" * 2000
        outcome = self._run_pipeline(tmp_path, long_transcript)

        assert outcome["analyze_calls"][0] == long_transcript, (
            "2000-char transcript must be reused (meets the threshold)"
        )

    def test_transcript_over_2000_chars_is_reused(self, tmp_path):
        long_transcript = "a" * 5000
        outcome = self._run_pipeline(tmp_path, long_transcript)

        assert outcome["analyze_calls"][0] == long_transcript, (
            "Transcript over 2000 chars must be reused"
        )


# ===========================================================================
# 8. Pipeline resume — skip stages based on persisted state
# ===========================================================================

class TestPipelineResume:
    """Tests for resume logic: skip already-completed stages when state is past TRANSCRIBING."""

    def test_skip_analysis_when_state_past_transcribing(self, tmp_path):
        """When pipeline state is UPLOADING_DOCS with cached results, analyze_lecture should NOT be called."""
        from tools.core.pipeline_state import PipelineState, UPLOADING_DOCS

        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        fake_tmp = tmp_path / "tmp"
        fake_tmp.mkdir()

        # Create cached results files with enough content to pass validation
        for content_type, content in [
            ("transcript", "t" * 3000),
            ("summary", "s" * 600),
            ("gap_analysis", "g" * 400),
            ("deep_analysis", "d" * 400),
        ]:
            path = fake_tmp / f"g1_l1_{content_type}.txt"
            path.write_text(content, encoding="utf-8")

        # Mock pipeline state: already past TRANSCRIBING
        mock_state = PipelineState(
            group=1, lecture=1, state=UPLOADING_DOCS,
            summary_doc_id="existing-doc-id",
        )

        with (
            patch("tools.services.transcribe_lecture.TMP_DIR", fake_tmp),
            patch("tools.services.transcribe_lecture.load_state", return_value=mock_state),
            patch(_PATCH_ANALYZE) as mock_analyze,
            patch(_PATCH_INDEX, return_value=5),
            patch(_PATCH_ALERT),
            patch("tools.services.transcribe_lecture._upload_summary_to_drive", return_value="doc-1"),
            patch("tools.services.transcribe_lecture._upload_private_report_to_drive", return_value="doc-2"),
            patch("tools.services.transcribe_lecture._notify_group_whatsapp"),
            patch("tools.services.transcribe_lecture._send_private_report_to_tornike"),
            patch("tools.services.transcribe_lecture._find_recording_in_drive", return_value=None),
            patch("tools.services.transcribe_lecture.transition", return_value=mock_state),
            patch("tools.services.transcribe_lecture.mark_complete", return_value=mock_state),
            patch("tools.services.transcribe_lecture.cleanup_checkpoints", return_value=0),
        ):
            tl.transcribe_and_index(1, 1, video)

        mock_analyze.assert_not_called()

    def test_fallback_to_full_analysis_when_cache_missing(self, tmp_path):
        """When pipeline state is past TRANSCRIBING but no cached files exist, analyze_lecture IS called."""
        from tools.core.pipeline_state import PipelineState, UPLOADING_DOCS

        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        fake_tmp = tmp_path / "tmp"
        fake_tmp.mkdir()
        # NO cached result files — .tmp/ is empty

        mock_state = PipelineState(
            group=1, lecture=1, state=UPLOADING_DOCS,
        )

        full_results = {
            "transcript": "t" * 3000,
            "summary": "s" * 600,
            "gap_analysis": "g" * 400,
            "deep_analysis": "d" * 400,
        }

        with (
            patch("tools.services.transcribe_lecture.TMP_DIR", fake_tmp),
            patch("tools.services.transcribe_lecture.load_state", return_value=mock_state),
            patch(_PATCH_ANALYZE, return_value=full_results) as mock_analyze,
            patch(_PATCH_INDEX, return_value=5),
            patch(_PATCH_ALERT),
            patch("tools.services.transcribe_lecture._upload_summary_to_drive", return_value="doc-1"),
            patch("tools.services.transcribe_lecture._upload_private_report_to_drive", return_value="doc-2"),
            patch("tools.services.transcribe_lecture._notify_group_whatsapp"),
            patch("tools.services.transcribe_lecture._send_private_report_to_tornike"),
            patch("tools.services.transcribe_lecture._find_recording_in_drive", return_value=None),
            patch("tools.services.transcribe_lecture.transition", return_value=mock_state),
            patch("tools.services.transcribe_lecture.mark_complete", return_value=mock_state),
            patch("tools.services.transcribe_lecture.cleanup_checkpoints", return_value=0),
        ):
            tl.transcribe_and_index(1, 1, video)

        mock_analyze.assert_called_once()

    def test_resume_skips_completed_delivery_stages(self, tmp_path):
        """When pipeline has group_notified=True, _notify_group_whatsapp should NOT be called."""
        from tools.core.pipeline_state import PipelineState, NOTIFYING

        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake video")

        fake_tmp = tmp_path / "tmp"
        fake_tmp.mkdir()

        # Create cached results
        for content_type, content in [
            ("transcript", "t" * 3000),
            ("summary", "s" * 600),
            ("gap_analysis", "g" * 400),
            ("deep_analysis", "d" * 400),
        ]:
            path = fake_tmp / f"g1_l1_{content_type}.txt"
            path.write_text(content, encoding="utf-8")

        # Pipeline state: past notification, with group_notified=True
        mock_state = PipelineState(
            group=1, lecture=1, state=NOTIFYING,
            summary_doc_id="doc-1",
            report_doc_id="doc-2",
            group_notified=True,
            private_notified=True,
        )

        with (
            patch("tools.services.transcribe_lecture.TMP_DIR", fake_tmp),
            patch("tools.services.transcribe_lecture.load_state", return_value=mock_state),
            patch(_PATCH_ANALYZE),
            patch(_PATCH_INDEX, return_value=5),
            patch(_PATCH_ALERT),
            patch("tools.services.transcribe_lecture._upload_summary_to_drive", return_value="doc-1"),
            patch("tools.services.transcribe_lecture._upload_private_report_to_drive", return_value="doc-2"),
            patch("tools.services.transcribe_lecture._notify_group_whatsapp") as mock_notify_group,
            patch("tools.services.transcribe_lecture._send_private_report_to_tornike") as mock_notify_private,
            patch("tools.services.transcribe_lecture._find_recording_in_drive", return_value=None),
            patch("tools.services.transcribe_lecture.transition", return_value=mock_state),
            patch("tools.services.transcribe_lecture.mark_complete", return_value=mock_state),
            patch("tools.services.transcribe_lecture.cleanup_checkpoints", return_value=0),
        ):
            tl.transcribe_and_index(1, 1, video)

        # Group and private notifications should have been skipped
        mock_notify_group.assert_not_called()
        mock_notify_private.assert_not_called()
