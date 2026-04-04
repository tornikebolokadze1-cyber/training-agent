"""Tests for zero-coverage functions in tools/integrations/gemini_analyzer.py.

Covers:
- _validate_media_path: path traversal rejection, prefix-collision safety,
  valid paths inside TMP_DIR
- split_video_chunks: short-video single-element return, long-video 3-chunk
  split with correct ffmpeg call count and chunk naming
- _is_quota_error: every quoted indicator string, and non-matching errors

These tests are independent of the test_gemini_analyzer.py and
test_gemini_analyzer_new.py files — they focus exclusively on the three
low-level helpers that had zero direct unit-test coverage.

All subprocess calls are mocked; no real ffprobe/ffmpeg or API calls are made.

Run with:
    pytest tools/tests/test_gemini_analyzer_core.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are registered in tools/tests/conftest.py before this file
# is imported.  We simply import the module under test here.
# ---------------------------------------------------------------------------
import tools.integrations.gemini_analyzer as ga


# ===========================================================================
# 1. _validate_media_path
# ===========================================================================


class TestValidateMediaPath:
    """_validate_media_path must accept paths inside TMP_DIR and reject
    anything that resolves outside it — including path-traversal sequences
    and prefix-collision tricks."""

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _make_video(tmp_path: Path, name: str = "lecture.mp4") -> Path:
        """Write a small file so .resolve() works on real filesystem."""
        p = tmp_path / name
        p.write_bytes(b"fake-video-data")
        return p

    # -----------------------------------------------------------------------
    # Valid paths
    # -----------------------------------------------------------------------

    def test_valid_path_inside_tmp_dir_is_accepted(self, tmp_path: Path) -> None:
        """A file directly inside TMP_DIR must be returned unchanged (resolved)."""
        video = self._make_video(tmp_path)
        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._validate_media_path(video)
        assert result == video.resolve()

    def test_valid_nested_path_inside_tmp_dir_is_accepted(self, tmp_path: Path) -> None:
        """A file in a subdirectory of TMP_DIR must also be accepted."""
        subdir = tmp_path / "group1" / "lecture3"
        subdir.mkdir(parents=True)
        video = subdir / "recording.mp4"
        video.write_bytes(b"data")
        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._validate_media_path(video)
        assert result == video.resolve()

    def test_returns_resolved_path_object(self, tmp_path: Path) -> None:
        """Return type must be a Path object."""
        video = self._make_video(tmp_path)
        with patch.object(ga, "TMP_DIR", tmp_path):
            result = ga._validate_media_path(video)
        assert isinstance(result, Path)

    # -----------------------------------------------------------------------
    # Path traversal rejection
    # -----------------------------------------------------------------------

    def test_absolute_path_traversal_rejected(self, tmp_path: Path) -> None:
        """An absolute path outside TMP_DIR (/etc/passwd) must raise ValueError."""
        evil_path = Path("/etc/passwd")
        with patch.object(ga, "TMP_DIR", tmp_path):
            with pytest.raises(ValueError, match="outside TMP_DIR"):
                ga._validate_media_path(evil_path)

    def test_relative_traversal_sequence_rejected(self, tmp_path: Path) -> None:
        """A path with '../' that escapes TMP_DIR must raise ValueError."""
        # Construct a path that starts inside tmp_path but uses '..' to escape
        escape_path = tmp_path / ".." / "etc" / "shadow"
        with patch.object(ga, "TMP_DIR", tmp_path):
            with pytest.raises(ValueError, match="outside TMP_DIR"):
                ga._validate_media_path(escape_path)

    def test_home_directory_path_rejected(self, tmp_path: Path) -> None:
        """Any path in the user home directory must be rejected."""
        home_path = Path.home() / "Documents" / "private.mp4"
        with patch.object(ga, "TMP_DIR", tmp_path):
            with pytest.raises(ValueError, match="outside TMP_DIR"):
                ga._validate_media_path(home_path)

    def test_tmp_dir_parent_path_rejected(self, tmp_path: Path) -> None:
        """The *parent* of TMP_DIR itself must be rejected."""
        parent_file = tmp_path.parent / "sibling.mp4"
        with patch.object(ga, "TMP_DIR", tmp_path):
            with pytest.raises(ValueError, match="outside TMP_DIR"):
                ga._validate_media_path(parent_file)

    # -----------------------------------------------------------------------
    # Prefix-collision safety
    # -----------------------------------------------------------------------

    def test_prefix_collision_rejected(self, tmp_path: Path) -> None:
        """A path like /tmp/agent_evil/file.mp4 when TMP_DIR=/tmp/agent must
        be rejected even though /tmp/agent is a prefix of /tmp/agent_evil.

        This verifies that the implementation uses relative_to() (which
        checks path components, not string prefixes) rather than str.startswith().
        """
        # Create a sibling directory with a name that starts with tmp_path.name
        collision_dir = tmp_path.parent / (tmp_path.name + "_evil")
        collision_dir.mkdir(exist_ok=True)
        evil_file = collision_dir / "payload.mp4"
        evil_file.write_bytes(b"evil")

        with patch.object(ga, "TMP_DIR", tmp_path):
            with pytest.raises(ValueError, match="outside TMP_DIR"):
                ga._validate_media_path(evil_file)

        # Cleanup
        evil_file.unlink(missing_ok=True)
        try:
            collision_dir.rmdir()
        except OSError:
            pass

    def test_same_name_different_parent_rejected(self, tmp_path: Path) -> None:
        """A file with the same name as TMP_DIR but in a different location
        must be rejected.  e.g. TMP_DIR=/tmp/agent, path=/var/agent/file.mp4
        """
        other_dir = tmp_path.parent / "other_agent_dir"
        other_dir.mkdir(exist_ok=True)
        other_file = other_dir / "video.mp4"
        other_file.write_bytes(b"data")

        with patch.object(ga, "TMP_DIR", tmp_path):
            with pytest.raises(ValueError, match="outside TMP_DIR"):
                ga._validate_media_path(other_file)

        # Cleanup
        other_file.unlink(missing_ok=True)
        try:
            other_dir.rmdir()
        except OSError:
            pass


# ===========================================================================
# 2. split_video_chunks
# ===========================================================================


class TestSplitVideoChunks:
    """split_video_chunks splits long videos via ffmpeg and returns a list of
    chunk Paths.  All subprocess calls are mocked; no real binaries invoked."""

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _ffprobe_result(duration_seconds: float) -> MagicMock:
        return MagicMock(returncode=0, stdout=f"{duration_seconds}\n", stderr="")

    @staticmethod
    def _ffmpeg_ok(cmd: list, **kwargs) -> MagicMock:
        """Create the output chunk file so stat().st_size checks pass."""
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"\x00" * 200_000)  # >100 KB — valid chunk
        return MagicMock(returncode=0, stdout="", stderr="")

    # -----------------------------------------------------------------------
    # Short video — single element
    # -----------------------------------------------------------------------

    def test_30_min_video_returns_original_as_single_element(
        self, tmp_path: Path
    ) -> None:
        """A 30-minute video (1800 s < 2700 s threshold) must be returned as
        a single-element list pointing to the original file, no ffmpeg call."""
        video = tmp_path / "short.mp4"
        video.write_bytes(b"\x00" * 1000)

        def fake_run(cmd, **kwargs):
            return self._ffprobe_result(30 * 60)  # 30 minutes

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            result = ga.split_video_chunks(video)

        assert len(result) == 1
        assert result[0] == video.resolve()

    def test_exactly_45_min_video_is_not_split(self, tmp_path: Path) -> None:
        """A 45-minute video (exactly the chunk boundary) is a single chunk."""
        video = tmp_path / "exact.mp4"
        video.write_bytes(b"\x00" * 1000)

        def fake_run(cmd, **kwargs):
            return self._ffprobe_result(45 * 60)  # exactly 2700 s

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            result = ga.split_video_chunks(video)

        assert len(result) == 1

    def test_single_chunk_no_ffmpeg_invoked(self, tmp_path: Path) -> None:
        """For short video, ffmpeg must NOT be called — only ffprobe."""
        video = tmp_path / "short.mp4"
        video.write_bytes(b"\x00" * 1000)

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run") as mock_run,
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            mock_run.return_value = self._ffprobe_result(20 * 60)
            ga.split_video_chunks(video)

        calls = mock_run.call_args_list
        ffmpeg_calls = [c for c in calls if c[0][0][0] == "ffmpeg"]
        assert len(ffmpeg_calls) == 0

    # -----------------------------------------------------------------------
    # Long video — 3 chunks
    # -----------------------------------------------------------------------

    def test_100_min_video_smart_merge_produces_2_chunks(self, tmp_path: Path) -> None:
        """A 100-minute video (45+45+10): 10 min < 15 min threshold → merged → 2 chunks."""
        video = tmp_path / "long.mp4"
        video.write_bytes(b"\x00" * 1000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return self._ffprobe_result(100 * 60)
            return self._ffmpeg_ok(cmd, **kwargs)

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            result = ga.split_video_chunks(video)

        assert len(result) == 2

    def test_120_min_video_no_merge_produces_3_chunks(self, tmp_path: Path) -> None:
        """A 120-minute video (45+45+30): 30 min >= 15 min → no merge → 3 chunks."""
        video = tmp_path / "long.mp4"
        video.write_bytes(b"\x00" * 1000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return self._ffprobe_result(120 * 60)
            return self._ffmpeg_ok(cmd, **kwargs)

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            result = ga.split_video_chunks(video)

        assert len(result) == 3

    def test_100_min_video_chunk_names_follow_convention(self, tmp_path: Path) -> None:
        """Smart-merged 100-min video: chunk0.mp4, chunk1.mp4."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"\x00" * 1000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return self._ffprobe_result(100 * 60)
            return self._ffmpeg_ok(cmd, **kwargs)

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            result = ga.split_video_chunks(video)

        names = [p.name for p in result]
        assert "lecture.chunk0.mp4" in names
        assert "lecture.chunk1.mp4" in names

    def test_100_min_video_invokes_ffmpeg_2_times(self, tmp_path: Path) -> None:
        """Smart-merged 100-min: ffmpeg called 2 times (not 3)."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"\x00" * 1000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return self._ffprobe_result(100 * 60)
            return self._ffmpeg_ok(cmd, **kwargs)

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run) as mock_run,
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            ga.split_video_chunks(video)

        ffmpeg_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "ffmpeg"]
        assert len(ffmpeg_calls) == 2

    def test_last_chunk_has_no_t_flag(self, tmp_path: Path) -> None:
        """Last chunk must NOT have -t flag (runs to end of video for smart merge)."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"\x00" * 1000)
        captured_cmds: list[list] = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return self._ffprobe_result(100 * 60)
            captured_cmds.append(list(cmd))
            return self._ffmpeg_ok(cmd, **kwargs)

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            ga.split_video_chunks(video)

        # First chunk has -t, last chunk does not
        assert "-t" in captured_cmds[0], "First chunk should have -t flag"
        assert "-t" not in captured_cmds[-1], "Last chunk should NOT have -t flag (smart merge)"
        # All chunks must have -ss
        for cmd in captured_cmds:
            assert "-ss" in cmd, f"-ss flag missing from ffmpeg cmd: {cmd}"

    def test_chunk_start_offsets_are_multiples_of_chunk_duration(
        self, tmp_path: Path
    ) -> None:
        """The -ss value for chunk N must be N * CHUNK_DURATION_MINUTES * 60."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"\x00" * 1000)
        captured_cmds: list[list] = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return self._ffprobe_result(100 * 60)
            captured_cmds.append(list(cmd))
            return self._ffmpeg_ok(cmd, **kwargs)

        with (
            patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            ga.split_video_chunks(video)

        chunk_secs = ga.CHUNK_DURATION_MINUTES * 60
        for i, cmd in enumerate(captured_cmds):
            ss_index = cmd.index("-ss")
            start = float(cmd[ss_index + 1])
            assert start == i * chunk_secs, (
                f"Chunk {i} start offset {start} != expected {i * chunk_secs}"
            )

    # -----------------------------------------------------------------------
    # Edge cases
    # -----------------------------------------------------------------------

    def test_ffprobe_failure_raises_runtime_error(self, tmp_path: Path) -> None:
        """If ffprobe returns non-zero, RuntimeError must be raised."""
        video = tmp_path / "corrupt.mp4"
        video.write_bytes(b"\x00" * 100)

        with (
            patch(
                "tools.integrations.gemini_analyzer.subprocess.run",
                return_value=MagicMock(returncode=1, stdout="", stderr="No such file or directory"),
            ),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            with pytest.raises(RuntimeError, match="ffprobe failed"):
                ga.split_video_chunks(video)

    def test_zero_duration_raises_value_error(self, tmp_path: Path) -> None:
        """A video with duration=0 must raise ValueError."""
        video = tmp_path / "zero.mp4"
        video.write_bytes(b"\x00" * 100)

        with (
            patch(
                "tools.integrations.gemini_analyzer.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="0.0\n", stderr=""),
            ),
            patch.object(ga, "TMP_DIR", tmp_path),
        ):
            with pytest.raises(ValueError, match="zero or negative"):
                ga.split_video_chunks(video)

    def test_path_outside_tmp_dir_raises_value_error(self, tmp_path: Path) -> None:
        """Passing a path outside TMP_DIR must fail at _validate_media_path."""
        evil = Path("/etc/hosts")
        with patch.object(ga, "TMP_DIR", tmp_path):
            with pytest.raises(ValueError, match="outside TMP_DIR"):
                ga.split_video_chunks(evil)


# ===========================================================================
# 3. _is_quota_error
# ===========================================================================


class TestIsQuotaError:
    """_is_quota_error must return True for all quota/rate-limit indicator
    strings (case-insensitive) and False for unrelated errors."""

    # -----------------------------------------------------------------------
    # True cases — all five quota indicators
    # -----------------------------------------------------------------------

    def test_429_status_code_detected(self) -> None:
        assert ga._is_quota_error(Exception("429 Too Many Requests")) is True

    def test_429_anywhere_in_message(self) -> None:
        assert ga._is_quota_error(Exception("HTTP error code: 429")) is True

    def test_resource_exhausted_lowercase(self) -> None:
        assert ga._is_quota_error(Exception("resource exhausted")) is True

    def test_resource_exhausted_uppercase(self) -> None:
        assert ga._is_quota_error(Exception("RESOURCE EXHAUSTED")) is True

    def test_resource_exhausted_mixed_case(self) -> None:
        assert ga._is_quota_error(Exception("Resource Exhausted for project")) is True

    def test_quota_keyword_detected(self) -> None:
        assert ga._is_quota_error(Exception("Quota exceeded for billing account")) is True

    def test_quota_keyword_uppercase(self) -> None:
        assert ga._is_quota_error(Exception("QUOTA LIMIT HIT")) is True

    def test_rate_limit_lowercase(self) -> None:
        assert ga._is_quota_error(Exception("rate limit exceeded")) is True

    def test_rate_limit_mixed_case(self) -> None:
        assert ga._is_quota_error(Exception("Rate Limit: slow down")) is True

    def test_too_many_requests_lowercase(self) -> None:
        assert ga._is_quota_error(Exception("too many requests — please retry")) is True

    def test_too_many_requests_mixed_case(self) -> None:
        assert ga._is_quota_error(Exception("Too Many Requests")) is True

    # -----------------------------------------------------------------------
    # False cases — non-quota errors must not be misidentified
    # -----------------------------------------------------------------------

    def test_normal_network_error_returns_false(self) -> None:
        assert ga._is_quota_error(Exception("Connection reset by peer")) is False

    def test_authentication_error_returns_false(self) -> None:
        assert ga._is_quota_error(Exception("401 Unauthorized")) is False

    def test_timeout_error_returns_false(self) -> None:
        assert ga._is_quota_error(Exception("Request timed out after 30s")) is False

    def test_file_not_found_returns_false(self) -> None:
        assert ga._is_quota_error(Exception("FileNotFoundError: no such file")) is False

    def test_json_decode_error_returns_false(self) -> None:
        assert ga._is_quota_error(Exception("JSONDecodeError: Expecting value")) is False

    def test_empty_message_returns_false(self) -> None:
        assert ga._is_quota_error(Exception("")) is False

    def test_generic_api_error_returns_false(self) -> None:
        assert ga._is_quota_error(Exception("Internal Server Error")) is False

    def test_google_403_forbidden_returns_false(self) -> None:
        """403 Forbidden is an auth error, not a quota error."""
        assert ga._is_quota_error(Exception("403 Forbidden")) is False

    def test_value_error_subclass_detected_if_message_matches(self) -> None:
        """Exception subclass with a quota indicator in the message."""
        assert ga._is_quota_error(ValueError("429 billing limit")) is True

    def test_runtime_error_without_quota_indicator(self) -> None:
        assert ga._is_quota_error(RuntimeError("Something broke")) is False
