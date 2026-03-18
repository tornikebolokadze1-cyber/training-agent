"""Unit tests for tools/process_recording.py.

Covers:
- process(): validation (missing file, invalid group), pipeline delegation
- process(): skip_drive flag behavior
- process(): result dict structure

Run with:
    pytest tools/tests/test_process_recording.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.app.process_recording as pr

# ===========================================================================
# 1. process() — input validation
# ===========================================================================


class TestProcessValidation:
    def test_raises_file_not_found_for_missing_video(self, tmp_path):
        missing = tmp_path / "nonexistent.mp4"

        with pytest.raises(FileNotFoundError, match="not found"):
            pr.process(str(missing), 1, 1)

    def test_raises_value_error_for_invalid_group(self, tmp_path):
        fake_video = tmp_path / "test.mp4"
        fake_video.write_bytes(b"\x00" * 10)

        with patch.object(pr, "GROUPS", {1: {"name": "g1"}, 2: {"name": "g2"}}):
            with pytest.raises(ValueError, match="Invalid group"):
                pr.process(str(fake_video), 99, 1)


# ===========================================================================
# 2. process() — skip_drive flag
# ===========================================================================


class TestProcessSkipDrive:
    def test_skip_drive_does_not_call_upload(self, tmp_path):
        fake_video = tmp_path / "test.mp4"
        fake_video.write_bytes(b"\x00" * 10)

        mock_groups = {1: {"name": "g1", "drive_folder_id": "folder-1"}}

        with patch.object(pr, "GROUPS", mock_groups), \
             patch.object(pr, "get_drive_service") as mock_drive, \
             patch.object(pr, "transcribe_and_index", return_value={"summary": 5}):
            result = pr.process(str(fake_video), 1, 3, skip_drive=True)

        mock_drive.assert_not_called()
        assert "drive_recording_url" not in result

    def test_with_drive_calls_upload(self, tmp_path):
        fake_video = tmp_path / "test.mp4"
        fake_video.write_bytes(b"\x00" * 10)

        mock_groups = {1: {"name": "g1", "drive_folder_id": "folder-1"}}
        mock_svc = MagicMock()

        with patch.object(pr, "GROUPS", mock_groups), \
             patch.object(pr, "get_drive_service", return_value=mock_svc), \
             patch.object(pr, "ensure_folder", return_value="lec-folder-id"), \
             patch.object(pr, "upload_file", return_value="file-id-123"), \
             patch.object(pr, "get_lecture_folder_name", return_value="ლექცია #3"), \
             patch.object(pr, "transcribe_and_index", return_value={"summary": 5}):
            result = pr.process(str(fake_video), 1, 3, skip_drive=False)

        assert "drive_recording_url" in result
        assert "file-id-123" in result["drive_recording_url"]


# ===========================================================================
# 3. process() — result structure
# ===========================================================================


class TestProcessResult:
    def test_result_contains_expected_keys(self, tmp_path):
        fake_video = tmp_path / "test.mp4"
        fake_video.write_bytes(b"\x00" * 10)

        mock_groups = {1: {"name": "g1", "drive_folder_id": "f"}}

        with patch.object(pr, "GROUPS", mock_groups), \
             patch.object(pr, "transcribe_and_index", return_value={"summary": 3, "transcript": 7}):
            result = pr.process(str(fake_video), 1, 5, skip_drive=True)

        assert result["group"] == 1
        assert result["lecture"] == 5
        assert result["index_counts"] == {"summary": 3, "transcript": 7}
        assert result["total_vectors"] == 10

    def test_delegates_to_transcribe_and_index(self, tmp_path):
        fake_video = tmp_path / "lecture.mp4"
        fake_video.write_bytes(b"\x00" * 10)

        mock_groups = {2: {"name": "g2", "drive_folder_id": "f"}}

        with patch.object(pr, "GROUPS", mock_groups), \
             patch.object(pr, "transcribe_and_index", return_value={"a": 1}) as mock_tai:
            pr.process(str(fake_video), 2, 7, skip_drive=True)

        mock_tai.assert_called_once()
        args = mock_tai.call_args[0]
        assert args[0] == 2  # group_number
        assert args[1] == 7  # lecture_number


# ===========================================================================
# 4. main() — CLI entrypoint
# ===========================================================================


class TestMainCli:
    def test_main_calls_process(self, tmp_path, capsys):
        fake_video = tmp_path / "test.mp4"
        fake_video.write_bytes(b"\x00" * 10)

        mock_result = {
            "group": 1,
            "lecture": 3,
            "index_counts": {"summary": 5},
            "total_vectors": 5,
        }

        with patch("sys.argv", ["prog", str(fake_video), "--group", "1", "--lecture", "3"]), \
             patch.object(pr, "process", return_value=mock_result):
            pr.main()

        output = capsys.readouterr().out
        assert "Group 1" in output
        assert "Lecture #3" in output
        assert "RESULTS" in output

    def test_main_with_skip_drive(self, tmp_path, capsys):
        fake_video = tmp_path / "test.mp4"
        fake_video.write_bytes(b"\x00" * 10)

        with patch("sys.argv", ["prog", str(fake_video), "--group", "2", "--lecture", "7", "--skip-drive"]), \
             patch.object(pr, "process", return_value={"group": 2}) as mock_proc:
            pr.main()

        mock_proc.assert_called_once()
        assert mock_proc.call_args[1]["skip_drive"] is True
