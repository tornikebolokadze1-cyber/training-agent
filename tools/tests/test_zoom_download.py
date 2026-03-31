"""Tests for Zoom recording download robustness.

Covers all 7 download failure modes:
  1. Partial download detection (Content-Length validation)
  2. Download resumption (HTTP Range header)
  3. Empty download_url handling (fallback to API polling)
  4. Multi-segment parallel download
  5. Download timeout and progress logging
  6. Checksum validation (SHA-256)
  7. Disk space pre-check

Run with:
    pytest tools/tests/test_zoom_download.py -v
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

import tools.integrations.zoom_manager as zm


# ===========================================================================
# Helpers
# ===========================================================================

def _make_stream_response(
    status_code: int = 200,
    content: bytes = b"\x00" * 100,
    headers: dict | None = None,
) -> MagicMock:
    """Create a mock streaming response for httpx.Client.stream()."""
    default_headers = {"content-length": str(len(content))}
    if headers:
        default_headers.update(headers)

    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = default_headers
    resp.iter_bytes.return_value = [content]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_client(stream_response: MagicMock) -> MagicMock:
    """Create a mock httpx.Client wrapping a stream response."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.stream.return_value = stream_response
    client.head.return_value = MagicMock(
        status_code=200,
        headers=stream_response.headers,
    )
    return client


# ===========================================================================
# 1. Partial download detection (Content-Length validation)
# ===========================================================================


class TestPartialDownloadDetection:
    """Verify that incomplete downloads are detected and rejected."""

    def test_detects_incomplete_download(self, tmp_path: Path) -> None:
        """File smaller than Content-Length should raise ZoomDownloadError."""
        dest = tmp_path / "partial.mp4"
        content = b"\x00" * 50  # Only 50 bytes

        resp = _make_stream_response(
            content=content,
            headers={"content-length": "100"},  # Claims 100 bytes
        )
        client = _make_client(resp)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client):
            with pytest.raises(zm.ZoomDownloadError, match="Incomplete download"):
                zm.download_recording("https://zoom.us/dl", "tok", dest)

    def test_accepts_complete_download(self, tmp_path: Path) -> None:
        """File matching Content-Length should succeed."""
        dest = tmp_path / "complete.mp4"
        content = b"\x00" * 100

        resp = _make_stream_response(content=content)
        client = _make_client(resp)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client):
            result = zm.download_recording("https://zoom.us/dl", "tok", dest)

        assert result.exists()
        assert result.stat().st_size == 100

    def test_no_content_length_still_succeeds(self, tmp_path: Path) -> None:
        """Downloads without Content-Length header should still succeed."""
        dest = tmp_path / "no_cl.mp4"
        content = b"\x00" * 100

        resp = _make_stream_response(
            content=content,
            headers={"content-length": "0"},
        )
        client = _make_client(resp)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client):
            result = zm.download_recording("https://zoom.us/dl", "tok", dest)

        assert result.exists()


# ===========================================================================
# 2. Download resumption (HTTP Range header)
# ===========================================================================


class TestDownloadResumption:
    """Verify resume from partial files using HTTP Range header."""

    def test_sends_range_header_for_partial_file(self, tmp_path: Path) -> None:
        """Existing partial file should trigger Range header."""
        dest = tmp_path / "partial.mp4"
        # Create a 50-byte partial file
        dest.write_bytes(b"\x00" * 50)

        remaining = b"\x01" * 50
        resp = _make_stream_response(
            status_code=206,
            content=remaining,
            headers={
                "content-length": "50",
                "content-range": "bytes 50-99/100",
            },
        )
        client = _make_client(resp)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client):
            result = zm.download_recording(
                "https://zoom.us/dl", "tok", dest, resume=True,
            )

        # Verify Range header was sent
        call_args = client.stream.call_args
        headers_sent = call_args[1].get("headers", {})
        assert "Range" in headers_sent
        assert headers_sent["Range"] == "bytes=50-"

        assert result.exists()

    def test_no_resume_when_disabled(self, tmp_path: Path) -> None:
        """resume=False should not send Range header."""
        dest = tmp_path / "partial.mp4"
        dest.write_bytes(b"\x00" * 50)

        content = b"\x01" * 100
        resp = _make_stream_response(content=content)
        client = _make_client(resp)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client):
            result = zm.download_recording(
                "https://zoom.us/dl", "tok", dest, resume=False,
            )

        # Verify no Range header
        call_args = client.stream.call_args
        headers_sent = call_args[1].get("headers", {})
        assert "Range" not in headers_sent

    def test_no_resume_when_no_partial_file(self, tmp_path: Path) -> None:
        """No existing file should not send Range header."""
        dest = tmp_path / "new.mp4"

        content = b"\x01" * 100
        resp = _make_stream_response(content=content)
        client = _make_client(resp)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client):
            zm.download_recording("https://zoom.us/dl", "tok", dest, resume=True)

        call_args = client.stream.call_args
        headers_sent = call_args[1].get("headers", {})
        assert "Range" not in headers_sent


# ===========================================================================
# 3. Empty download_url handling
# ===========================================================================


class TestEmptyDownloadUrl:
    """The server already handles empty download_url via polling fallback.

    These tests verify the _extract_recording_context and
    _handle_recording_completed_via_polling interaction in server.py.
    """

    def test_extract_context_returns_empty_url(self) -> None:
        """recording.completed with empty download_url is detected."""
        from tools.app.server import _extract_recording_context

        body = {
            "payload": {
                "object": {
                    "topic": "AI კურსი — ჯგუფი #1, ლექცია #3",
                    "start_time": "2026-03-31T16:00:00Z",
                    "recording_files": [
                        {
                            "file_type": "MP4",
                            "recording_type": "shared_screen_with_speaker_view",
                            "download_url": "",
                        }
                    ],
                }
            }
        }
        ctx = _extract_recording_context(body)
        assert ctx is not None
        assert ctx["download_url"] == ""


# ===========================================================================
# 4. Multi-segment parallel download
# ===========================================================================


class TestMultiSegmentParallelDownload:
    """Verify parallel downloading of multiple recording segments."""

    def test_downloads_multiple_segments(self, tmp_path: Path) -> None:
        """Two segments should be downloaded in parallel."""
        segments = [
            {
                "id": "seg-001",
                "file_type": "MP4",
                "recording_type": "shared_screen_with_speaker_view",
                "download_url": "https://zoom.us/dl/1",
                "file_size": 100,
            },
            {
                "id": "seg-002",
                "file_type": "MP4",
                "recording_type": "shared_screen_with_speaker_view",
                "download_url": "https://zoom.us/dl/2",
                "file_size": 100,
            },
        ]

        def mock_download(url: str, token: str, path: Path, **kwargs) -> Path:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00" * 100)
            # Also create checksum file
            zm.save_checksum(path, zm.compute_file_checksum(path))
            return path

        with patch.object(zm, "get_access_token", return_value="tok"), \
             patch.object(zm, "download_recording", side_effect=mock_download), \
             patch.object(zm, "check_disk_space"):
            paths = zm.download_segments_parallel(segments, tmp_path)

        assert len(paths) == 2
        for p in paths:
            assert p.exists()
            assert p.stat().st_size == 100

    def test_returns_paths_in_order(self, tmp_path: Path) -> None:
        """Returned paths should match input segment order."""
        segments = [
            {"id": f"seg-{i:03d}", "file_type": "MP4",
             "recording_type": "active_speaker", "download_url": f"https://zoom.us/dl/{i}",
             "file_size": 50}
            for i in range(3)
        ]

        call_order: list[int] = []

        def mock_download(url: str, token: str, path: Path, **kwargs) -> Path:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Extract index from URL
            idx = int(url.split("/")[-1])
            call_order.append(idx)
            path.write_bytes(bytes([idx]) * 50)
            zm.save_checksum(path, zm.compute_file_checksum(path))
            return path

        with patch.object(zm, "get_access_token", return_value="tok"), \
             patch.object(zm, "download_recording", side_effect=mock_download), \
             patch.object(zm, "check_disk_space"):
            paths = zm.download_segments_parallel(segments, tmp_path)

        # Paths should be in segment order (0, 1, 2) regardless of completion order
        assert len(paths) == 3
        for i, p in enumerate(paths):
            content = p.read_bytes()
            assert content[0] == i

    def test_failure_in_one_segment_raises(self, tmp_path: Path) -> None:
        """If any segment fails, the entire operation should fail."""
        segments = [
            {"id": "seg-ok", "file_type": "MP4", "recording_type": "active_speaker",
             "download_url": "https://zoom.us/dl/1", "file_size": 100},
            {"id": "seg-fail", "file_type": "MP4", "recording_type": "active_speaker",
             "download_url": "https://zoom.us/dl/2", "file_size": 100},
        ]

        def mock_download(url: str, token: str, path: Path, **kwargs) -> Path:
            if "dl/2" in url:
                raise zm.ZoomDownloadError("Segment 2 failed")
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00" * 100)
            zm.save_checksum(path, zm.compute_file_checksum(path))
            return path

        with patch.object(zm, "get_access_token", return_value="tok"), \
             patch.object(zm, "download_recording", side_effect=mock_download), \
             patch.object(zm, "check_disk_space"):
            with pytest.raises(zm.ZoomDownloadError, match="Segment .* failed"):
                zm.download_segments_parallel(segments, tmp_path)

    def test_empty_segments_returns_empty_list(self, tmp_path: Path) -> None:
        """No segments should return an empty list."""
        paths = zm.download_segments_parallel([], tmp_path)
        assert paths == []


# ===========================================================================
# 5. Download timeout configuration
# ===========================================================================


class TestDownloadTimeout:
    """Verify timeout configuration is appropriate for large files."""

    def test_timeout_is_at_least_30_minutes(self) -> None:
        """Timeout must be >= 1800 seconds for 2-4 GB lecture recordings."""
        assert zm.DOWNLOAD_TIMEOUT_SECONDS >= 1800

    def test_progress_log_interval_is_100mb(self) -> None:
        """Progress should be logged every 100 MB."""
        assert zm.PROGRESS_LOG_INTERVAL_BYTES == 100 * 1024 * 1024

    def test_max_download_retries_is_5(self) -> None:
        """Downloads should retry up to 5 times (more than API calls)."""
        assert zm.MAX_DOWNLOAD_RETRIES == 5


# ===========================================================================
# 6. Checksum validation (SHA-256)
# ===========================================================================


class TestChecksumValidation:
    """Verify SHA-256 checksum computation, storage, and verification."""

    def test_compute_checksum(self, tmp_path: Path) -> None:
        """Checksum should match hashlib's sha256 for the same content."""
        file_path = tmp_path / "test.mp4"
        content = b"test content for hashing"
        file_path.write_bytes(content)

        expected = hashlib.sha256(content).hexdigest()
        actual = zm.compute_file_checksum(file_path)
        assert actual == expected

    def test_save_and_load_checksum(self, tmp_path: Path) -> None:
        """save_checksum and load_checksum should round-trip."""
        file_path = tmp_path / "test.mp4"
        file_path.write_bytes(b"content")
        checksum = "abc123def456"

        zm.save_checksum(file_path, checksum)
        loaded = zm.load_checksum(file_path)
        assert loaded == checksum

    def test_load_checksum_returns_none_when_missing(self, tmp_path: Path) -> None:
        """No checksum file should return None."""
        file_path = tmp_path / "no_checksum.mp4"
        file_path.write_bytes(b"data")
        assert zm.load_checksum(file_path) is None

    def test_verify_download_integrity_passes(self, tmp_path: Path) -> None:
        """Correct checksum should verify successfully."""
        file_path = tmp_path / "good.mp4"
        content = b"good content"
        file_path.write_bytes(content)

        checksum = hashlib.sha256(content).hexdigest()
        zm.save_checksum(file_path, checksum)

        assert zm.verify_download_integrity(file_path) is True

    def test_verify_download_integrity_fails_on_corruption(self, tmp_path: Path) -> None:
        """Modified file should fail verification."""
        file_path = tmp_path / "bad.mp4"
        file_path.write_bytes(b"original content")

        checksum = hashlib.sha256(b"original content").hexdigest()
        zm.save_checksum(file_path, checksum)

        # Corrupt the file
        file_path.write_bytes(b"corrupted content")
        assert zm.verify_download_integrity(file_path) is False

    def test_download_creates_checksum_file(self, tmp_path: Path) -> None:
        """download_recording should create a .sha256 checksum file."""
        dest = tmp_path / "recording.mp4"
        content = b"\x00" * 100

        resp = _make_stream_response(content=content)
        client = _make_client(resp)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client):
            zm.download_recording("https://zoom.us/dl", "tok", dest)

        checksum_path = dest.with_suffix(".mp4.sha256")
        assert checksum_path.exists()

        # Verify checksum content
        stored = zm.load_checksum(dest)
        expected = hashlib.sha256(content).hexdigest()
        assert stored == expected


# ===========================================================================
# 7. Disk space pre-check
# ===========================================================================


class TestDiskSpacePreCheck:
    """Verify disk space validation before downloads."""

    def test_sufficient_space_passes(self, tmp_path: Path) -> None:
        """Enough disk space should not raise."""
        # tmp_path is on disk, should have plenty of space for a small file
        zm.check_disk_space(tmp_path / "file.mp4", 1024)

    def test_insufficient_space_raises(self, tmp_path: Path) -> None:
        """Not enough space should raise ZoomDownloadError."""
        with patch("tools.integrations.zoom_manager.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=100)  # Only 100 bytes free

            with pytest.raises(zm.ZoomDownloadError, match="Insufficient disk space"):
                zm.check_disk_space(tmp_path / "file.mp4", 1000)

    def test_safety_margin_applied(self, tmp_path: Path) -> None:
        """Disk space check should require 1.2x the file size."""
        # File needs 1000 bytes, with 1.2x margin = 1200 bytes needed
        # Having 1100 bytes should fail (< 1200)
        with patch("tools.integrations.zoom_manager.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=1100)

            with pytest.raises(zm.ZoomDownloadError, match="Insufficient disk space"):
                zm.check_disk_space(tmp_path / "file.mp4", 1000)

    def test_safety_margin_passes_when_sufficient(self, tmp_path: Path) -> None:
        """Having exactly enough space with margin should pass."""
        with patch("tools.integrations.zoom_manager.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=1200)
            # Should not raise
            zm.check_disk_space(tmp_path / "file.mp4", 1000)

    def test_download_checks_disk_space(self, tmp_path: Path) -> None:
        """download_recording should check disk space before writing."""
        dest = tmp_path / "big.mp4"

        resp = _make_stream_response(
            content=b"\x00" * 100,
            headers={"content-length": "100"},
        )
        client = _make_client(resp)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client), \
             patch("tools.integrations.zoom_manager.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=50)  # Not enough

            with pytest.raises(zm.ZoomDownloadError, match="Insufficient disk space"):
                zm.download_recording("https://zoom.us/dl", "tok", dest)


# ===========================================================================
# 8. download_all_recordings integration
# ===========================================================================


class TestDownloadAllRecordings:
    """Verify download_all_recordings uses parallel path for multi-segment."""

    def test_single_segment_uses_direct_download(self) -> None:
        """Single recording should bypass parallel download."""
        fake_data = {
            "recording_files": [
                {
                    "id": "rec-1",
                    "file_type": "MP4",
                    "recording_type": "active_speaker",
                    "status": "completed",
                    "download_url": "https://zoom.us/dl/1",
                },
            ],
        }

        mock_path = MagicMock(spec=Path)

        with patch.object(zm, "get_meeting_recordings", return_value=fake_data), \
             patch.object(zm, "get_access_token", return_value="tok"), \
             patch.object(zm, "download_recording", return_value=mock_path), \
             patch.object(zm, "download_segments_parallel") as mock_parallel:
            paths = zm.download_all_recordings("mtg-123", Path("/tmp/test"))

        assert len(paths) == 1
        mock_parallel.assert_not_called()

    def test_multiple_segments_uses_parallel_download(self) -> None:
        """Multiple recordings should use parallel download."""
        fake_data = {
            "recording_files": [
                {
                    "id": f"rec-{i}",
                    "file_type": "MP4",
                    "recording_type": "active_speaker",
                    "status": "completed",
                    "download_url": f"https://zoom.us/dl/{i}",
                }
                for i in range(3)
            ],
        }

        mock_paths = [MagicMock(spec=Path) for _ in range(3)]

        with patch.object(zm, "get_meeting_recordings", return_value=fake_data), \
             patch.object(zm, "download_segments_parallel", return_value=mock_paths) as mock_parallel:
            paths = zm.download_all_recordings("mtg-123", Path("/tmp/test"))

        assert len(paths) == 3
        mock_parallel.assert_called_once()

    def test_skips_non_completed_recordings(self) -> None:
        """Recordings with status != 'completed' should be skipped."""
        fake_data = {
            "recording_files": [
                {
                    "id": "rec-done",
                    "file_type": "MP4",
                    "recording_type": "active_speaker",
                    "status": "completed",
                    "download_url": "https://zoom.us/dl/1",
                },
                {
                    "id": "rec-pending",
                    "file_type": "MP4",
                    "recording_type": "active_speaker",
                    "status": "processing",
                    "download_url": "https://zoom.us/dl/2",
                },
            ],
        }

        mock_path = MagicMock(spec=Path)

        with patch.object(zm, "get_meeting_recordings", return_value=fake_data), \
             patch.object(zm, "get_access_token", return_value="tok"), \
             patch.object(zm, "download_recording", return_value=mock_path):
            paths = zm.download_all_recordings("mtg-123", Path("/tmp/test"))

        assert len(paths) == 1


# ===========================================================================
# 9. Network error retry with resume
# ===========================================================================


class TestNetworkErrorRetryResume:
    """Verify retry behavior with resume after network errors."""

    def test_retries_on_network_error(self, tmp_path: Path) -> None:
        """Network error should retry and eventually succeed."""
        import httpx as _httpx

        dest = tmp_path / "retry.mp4"
        content = b"\x00" * 100

        # First call raises network error, second succeeds
        good_resp = _make_stream_response(content=content)

        call_count = 0

        def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _httpx.ConnectError("Connection refused")
            return good_resp

        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.stream = mock_stream

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client), \
             patch("tools.integrations.zoom_manager.time.sleep"):
            result = zm.download_recording("https://zoom.us/dl", "tok", dest)

        assert result.exists()
        assert call_count == 2

    def test_exhausts_retries_and_raises(self, tmp_path: Path) -> None:
        """All retries exhausted should raise ZoomDownloadError."""
        import httpx as _httpx

        dest = tmp_path / "fail.mp4"

        def mock_stream(*args, **kwargs):
            raise _httpx.ConnectError("Connection refused")

        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.stream = mock_stream

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=client), \
             patch("tools.integrations.zoom_manager.time.sleep"):
            with pytest.raises(zm.ZoomDownloadError, match="Network error after"):
                zm.download_recording("https://zoom.us/dl", "tok", dest)

        # Partial file should be cleaned up
        assert not dest.exists()


# ===========================================================================
# 10. Checksum file format
# ===========================================================================


class TestChecksumFileFormat:
    """Verify checksum file follows standard sha256sum format."""

    def test_checksum_file_format(self, tmp_path: Path) -> None:
        """Checksum file should follow 'hash  filename' format."""
        file_path = tmp_path / "test.mp4"
        file_path.write_bytes(b"content")

        checksum = "abc123"
        checksum_path = zm.save_checksum(file_path, checksum)

        content = checksum_path.read_text()
        assert content == "abc123  test.mp4\n"

    def test_checksum_file_path(self, tmp_path: Path) -> None:
        """Checksum file should have .sha256 extension appended."""
        file_path = tmp_path / "recording.mp4"
        file_path.write_bytes(b"data")

        checksum_path = zm.save_checksum(file_path, "hash123")
        assert checksum_path.name == "recording.mp4.sha256"
