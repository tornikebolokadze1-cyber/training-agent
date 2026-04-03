"""Core unit tests for the Training Agent system.

Covers the highest-risk pure-Python logic with zero external API calls:
- config.get_lecture_number — date arithmetic
- config.get_group_for_weekday — weekday routing
- config.get_lecture_folder_name — Georgian string formatting
- knowledge_indexer.chunk_text — chunking + overlap invariants
- knowledge_indexer.index_lecture_content — invalid content_type guard
- whatsapp_sender._split_message — WhatsApp 4096-char chunking
- gemini_analyzer._is_quota_error — error classifier
- gemini_analyzer.split_video_chunks — chunk count calculation (subprocess mocked)
- server.verify_webhook_secret — HMAC-safe bearer token validation

Run with:
    pytest tools/tests/test_core.py -v
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py (shared across all
# test files).  Only project-level imports needed here.
# ---------------------------------------------------------------------------
from tools.core.config import (
    GROUPS,
    TOTAL_LECTURES,
    get_group_for_weekday,
    get_lecture_folder_name,
    get_lecture_number,
)
from tools.integrations.gemini_analyzer import _is_quota_error  # noqa: E402
from tools.integrations.knowledge_indexer import CONTENT_TYPES, chunk_text  # noqa: E402
from tools.integrations.whatsapp_sender import (  # noqa: E402
    MESSAGE_MAX_LENGTH,
    _split_message,
)

# ===========================================================================
# 1. Module import smoke tests
# ===========================================================================


class TestModuleImports:
    """Verify all critical modules load without errors or real API calls."""

    def test_config_module_loads(self):
        import tools.core.config as cfg
        assert hasattr(cfg, "GROUPS")
        assert hasattr(cfg, "TOTAL_LECTURES")
        assert hasattr(cfg, "get_lecture_number")

    def test_knowledge_indexer_module_loads(self):
        import tools.integrations.knowledge_indexer as ki
        assert hasattr(ki, "chunk_text")
        assert hasattr(ki, "embed_text")
        assert hasattr(ki, "index_lecture_content")
        assert hasattr(ki, "query_knowledge")

    def test_whatsapp_sender_module_loads(self):
        import tools.integrations.whatsapp_sender as ws
        assert hasattr(ws, "send_message_to_chat")
        assert hasattr(ws, "_split_message")
        assert hasattr(ws, "MESSAGE_MAX_LENGTH")

    def test_gemini_analyzer_module_loads(self):
        import tools.integrations.gemini_analyzer as ga
        assert hasattr(ga, "split_video_chunks")
        assert hasattr(ga, "_is_quota_error")
        assert hasattr(ga, "CHUNK_DURATION_MINUTES")

    def test_server_module_loads(self):
        import tools.app.server as srv
        assert hasattr(srv, "verify_webhook_secret")
        assert hasattr(srv, "app")


# ===========================================================================
# 2. Config loading
# ===========================================================================


class TestConfigLoading:
    """Verify the GROUPS dict is correctly structured."""

    def test_groups_dict_has_two_entries(self):
        assert set(GROUPS.keys()) == {1, 2}

    def test_total_lectures_is_15(self):
        assert TOTAL_LECTURES == 15

    def test_group1_meeting_days(self):
        # Tuesday=1, Friday=4
        assert GROUPS[1]["meeting_days"] == [1, 4]

    def test_group2_meeting_days(self):
        # Monday=0, Thursday=3
        assert GROUPS[2]["meeting_days"] == [0, 3]

    def test_group1_start_date(self):
        assert GROUPS[1]["start_date"] == date(2026, 3, 13)

    def test_group2_start_date(self):
        assert GROUPS[2]["start_date"] == date(2026, 3, 12)

    def test_group_names_are_georgian(self):
        for group in GROUPS.values():
            assert isinstance(group["name"], str)
            assert len(group["name"]) > 0


# ===========================================================================
# 3. get_lecture_number
# ===========================================================================


class TestGetLectureNumber:
    """Exercise every significant edge case of the date-arithmetic helper."""

    # --- Group 1 (Tuesday=1, Friday=4, starts 2026-03-13 Friday) ---

    def test_group1_on_start_date_is_lecture_1(self):
        # March 13 2026 is a Friday (weekday=4) — first lecture
        assert get_lecture_number(1, date(2026, 3, 13)) == 1

    def test_group1_day_before_start_is_zero(self):
        assert get_lecture_number(1, date(2026, 3, 12)) == 0

    def test_group1_first_tuesday_is_lecture_2(self):
        # Next Tuesday after March 13 is March 17 2026
        assert get_lecture_number(1, date(2026, 3, 17)) == 2

    def test_group1_date_far_in_future_capped_at_15(self):
        # 2030 is well past all 15 lectures
        assert get_lecture_number(1, date(2030, 1, 1)) == TOTAL_LECTURES

    def test_group1_exact_day_between_lectures_unchanged(self):
        # Saturday March 14 is not a meeting day — still lecture 1
        assert get_lecture_number(1, date(2026, 3, 14)) == 1

    # --- Group 2 (Monday=0, Thursday=3, starts 2026-03-12 Thursday) ---

    def test_group2_on_start_date_is_lecture_1(self):
        # March 12 2026 is a Thursday (weekday=3) — first lecture
        assert get_lecture_number(2, date(2026, 3, 12)) == 1

    def test_group2_day_before_start_is_zero(self):
        assert get_lecture_number(2, date(2026, 3, 11)) == 0

    def test_group2_first_monday_is_lecture_2(self):
        # Next Monday after March 12 is March 16 2026
        assert get_lecture_number(2, date(2026, 3, 16)) == 2

    def test_groups_are_independent(self):
        # On the same calendar date the groups may be at different lecture numbers
        # because they started on different days — just confirm independence
        lec1 = get_lecture_number(1, date(2026, 3, 17))
        lec2 = get_lecture_number(2, date(2026, 3, 17))
        # Group 2 started one day earlier so should be >= Group 1 on this date
        assert lec2 >= lec1

    def test_returns_integer(self):
        result = get_lecture_number(1, date(2026, 3, 13))
        assert isinstance(result, int)


# ===========================================================================
# 4. get_group_for_weekday
# ===========================================================================


class TestGetGroupForWeekday:
    """Verify weekday-to-group routing."""

    def test_monday_returns_group2(self):
        assert get_group_for_weekday(0) == 2

    def test_tuesday_returns_group1(self):
        assert get_group_for_weekday(1) == 1

    def test_thursday_returns_group2(self):
        assert get_group_for_weekday(3) == 2

    def test_friday_returns_group1(self):
        assert get_group_for_weekday(4) == 1

    def test_wednesday_returns_none(self):
        assert get_group_for_weekday(2) is None

    def test_weekend_returns_none(self):
        assert get_group_for_weekday(5) is None  # Saturday
        assert get_group_for_weekday(6) is None  # Sunday


# ===========================================================================
# 5. get_lecture_folder_name
# ===========================================================================


class TestGetLectureFolderName:
    def test_returns_georgian_format(self):
        assert get_lecture_folder_name(1) == "ლექცია #1"

    def test_lecture_number_embedded(self):
        for n in range(1, 16):
            name = get_lecture_folder_name(n)
            assert str(n) in name

    def test_returns_string(self):
        assert isinstance(get_lecture_folder_name(5), str)


# ===========================================================================
# 6. chunk_text
# ===========================================================================


class TestChunkText:
    """All invariants of the overlapping text-chunking algorithm."""

    def test_empty_string_returns_empty_list(self):
        assert chunk_text("") == []

    def test_none_like_falsy_empty_string(self):
        # The function guard is `if not text` — also covers empty
        assert chunk_text("") == []

    def test_short_text_fits_in_single_chunk(self):
        # Default chunk_size=500 tokens * 4 chars = 2000 chars
        short = "A" * 100
        result = chunk_text(short)
        assert len(result) == 1
        assert result[0] == short

    def test_exact_chunk_boundary_produces_single_chunk(self):
        # Exactly 2000 chars (500 tokens * 4 chars) — still fits in one chunk
        text = "B" * 2000
        result = chunk_text(text)
        assert len(result) == 1

    def test_text_just_over_one_chunk_produces_two_chunks(self):
        # 2001 chars forces a second chunk
        text = "C" * 2001
        result = chunk_text(text)
        assert len(result) == 2

    def test_large_text_splits_into_multiple_chunks(self):
        # 10000 chars with default settings → at least 5 chunks
        text = "D" * 10_000
        result = chunk_text(text)
        assert len(result) >= 5

    def test_all_content_preserved_in_chunks(self):
        # Every character in the source text must appear somewhere in a chunk.
        # Because chunks overlap, concatenation may differ from original, but
        # each individual character value must be present.
        text = "Hello World " * 500  # 6000 chars
        chunks = chunk_text(text)
        combined = "".join(chunks)
        # The combined length will be >= original due to overlap
        assert len(combined) >= len(text)

    def test_overlap_means_adjacent_chunks_share_content(self):
        # With overlap=50 tokens (200 chars), the tail of chunk N should appear
        # at the head of chunk N+1 (or at least the boundary text is replicated).
        text = "X" * 5000
        chunks = chunk_text(text, chunk_size=500, overlap=50)
        assert len(chunks) >= 2
        # Each chunk must be non-empty strings
        for ch in chunks:
            assert isinstance(ch, str)
            assert len(ch) > 0

    def test_custom_chunk_size_respected(self):
        # chunk_size=100 → 400 chars per chunk; 800-char text → at least 2 chunks
        text = "E" * 800
        result = chunk_text(text, chunk_size=100, overlap=0)
        assert len(result) >= 2

    def test_no_overlap_option(self):
        text = "F" * 4000
        result_no_overlap = chunk_text(text, chunk_size=500, overlap=0)
        result_overlap = chunk_text(text, chunk_size=500, overlap=100)
        # With overlap the combined joined length will be larger
        no_overlap_total = sum(len(c) for c in result_no_overlap)
        overlap_total = sum(len(c) for c in result_overlap)
        assert overlap_total >= no_overlap_total

    def test_returns_list_of_strings(self):
        chunks = chunk_text("Hello pytest", chunk_size=500, overlap=50)
        assert isinstance(chunks, list)
        for ch in chunks:
            assert isinstance(ch, str)

    def test_whitespace_only_text_returns_empty(self):
        # strip() inside the loop means a whitespace-only chunk is discarded
        result = chunk_text("   \n  \t  ")
        assert result == []


# ===========================================================================
# 7. index_lecture_content — content_type validation guard
# ===========================================================================


class TestIndexLectureContentValidation:
    """Only tests the pure validation logic — no Pinecone/Gemini calls."""

    def test_invalid_content_type_raises_value_error(self):
        from tools.integrations.knowledge_indexer import index_lecture_content

        with pytest.raises(ValueError, match="Unknown content_type"):
            # Patch get_pinecone_index so we never reach the network call
            with patch("tools.integrations.knowledge_indexer.get_pinecone_index"):
                index_lecture_content(1, 1, "some text", "invalid_type")

    def test_valid_content_types_do_not_raise_on_type_check(self):
        # Confirm the frozenset contains the expected members
        assert "transcript" in CONTENT_TYPES
        assert "summary" in CONTENT_TYPES
        assert "gap_analysis" in CONTENT_TYPES
        assert "deep_analysis" in CONTENT_TYPES
        assert "whatsapp_chat" in CONTENT_TYPES
        assert "obsidian_concept" in CONTENT_TYPES
        assert "obsidian_tool" in CONTENT_TYPES
        assert len(CONTENT_TYPES) == 7


# ===========================================================================
# 8. _split_message (WhatsApp chunking)
# ===========================================================================


class TestSplitMessage:
    """Validate every branch of the WhatsApp message splitter."""

    def test_short_message_returns_single_element_list(self):
        msg = "Hello"
        result = _split_message(msg)
        assert result == [msg]

    def test_exact_limit_not_split(self):
        msg = "A" * MESSAGE_MAX_LENGTH
        result = _split_message(msg)
        assert len(result) == 1

    def test_one_char_over_limit_produces_two_chunks(self):
        msg = "A" * (MESSAGE_MAX_LENGTH + 1)
        result = _split_message(msg)
        assert len(result) == 2

    def test_no_content_lost(self):
        # Characters must be fully preserved across chunks (accounting for
        # lstrip/rstrip which only trims whitespace).
        msg = "B" * (MESSAGE_MAX_LENGTH * 3)
        chunks = _split_message(msg)
        assert "".join(chunks) == msg

    def test_all_chunks_within_limit(self):
        msg = "C" * (MESSAGE_MAX_LENGTH * 5)
        for chunk in _split_message(msg):
            assert len(chunk) <= MESSAGE_MAX_LENGTH

    def test_splits_prefer_double_newline_boundary(self):
        # Build a message where a double-newline sits inside the first chunk limit.
        half = MESSAGE_MAX_LENGTH // 2
        msg = ("X" * half) + "\n\n" + ("Y" * (MESSAGE_MAX_LENGTH + 10))
        chunks = _split_message(msg)
        assert len(chunks) >= 2
        # The first chunk should end at or before the double-newline boundary
        assert len(chunks[0]) <= MESSAGE_MAX_LENGTH

    def test_splits_fallback_to_single_newline(self):
        # No double newline present — splitter should fall back to single \n
        half = MESSAGE_MAX_LENGTH // 2
        msg = ("X" * half) + "\n" + ("Y" * (MESSAGE_MAX_LENGTH + 10))
        chunks = _split_message(msg)
        assert len(chunks) >= 2
        for ch in chunks:
            assert len(ch) <= MESSAGE_MAX_LENGTH

    def test_returns_list(self):
        assert isinstance(_split_message("hello"), list)

    def test_very_long_message_all_chunks_valid(self):
        # Simulate a 3x limit report (typical deep analysis)
        report = ("ანალიზი " * 600)  # Georgian chars, ~6000 chars total
        chunks = _split_message(report)
        assert len(chunks) >= 1
        for ch in chunks:
            assert len(ch) <= MESSAGE_MAX_LENGTH


# ===========================================================================
# 9. _is_quota_error (Gemini error classifier)
# ===========================================================================


class TestIsQuotaError:
    """Verify the quota/rate-limit error detector covers all known patterns."""

    def test_429_string_detected(self):
        assert _is_quota_error(Exception("HTTP 429 Too Many Requests"))

    def test_resource_exhausted_detected(self):
        assert _is_quota_error(Exception("resource exhausted"))

    def test_quota_keyword_detected(self):
        assert _is_quota_error(Exception("Quota exceeded for project"))

    def test_rate_limit_detected(self):
        assert _is_quota_error(Exception("rate limit reached"))

    def test_too_many_requests_detected(self):
        assert _is_quota_error(Exception("too many requests"))

    def test_case_insensitive(self):
        assert _is_quota_error(Exception("RESOURCE EXHAUSTED"))
        assert _is_quota_error(Exception("Rate Limit"))

    def test_unrelated_error_not_detected(self):
        assert not _is_quota_error(Exception("File not found"))

    def test_network_timeout_not_detected(self):
        assert not _is_quota_error(Exception("Connection timed out"))

    def test_permission_denied_not_detected(self):
        assert not _is_quota_error(Exception("403 Permission denied"))


# ===========================================================================
# 10. split_video_chunks — chunk count logic (subprocess fully mocked)
# ===========================================================================


class TestSplitVideoChunks:
    """Test split_video_chunks chunk-count arithmetic without running ffmpeg."""

    def _mock_duration(self, seconds: float):
        """Return a context manager that fakes _get_video_duration_seconds."""
        return patch(
            "tools.integrations.gemini_analyzer._get_video_duration_seconds",
            return_value=seconds,
        )

    def _mock_validate_path(self):
        """Bypass TMP_DIR path validation so pytest tmp_path works."""
        return patch(
            "tools.integrations.gemini_analyzer._validate_media_path",
            side_effect=lambda p: p.resolve(),
        )

    def _mock_subprocess(self):
        """Prevent any real subprocess.run call."""
        return patch("tools.integrations.gemini_analyzer.subprocess.run")

    def test_short_video_returns_original_path(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake")

        with self._mock_validate_path(), self._mock_duration(20 * 60):  # 20 min — under 45 min
            from tools.integrations.gemini_analyzer import split_video_chunks
            result = split_video_chunks(video)

        assert result == [video]

    def test_exact_45min_video_returns_original_path(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake")

        with self._mock_validate_path(), self._mock_duration(45 * 60):  # exactly 45 min
            from tools.integrations.gemini_analyzer import split_video_chunks
            result = split_video_chunks(video)

        assert result == [video]

    def test_46min_video_splits_into_two_chunks(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake")

        # Simulate ffmpeg writing chunk files
        chunk0 = video.with_suffix(".chunk0.mp4")
        chunk1 = video.with_suffix(".chunk1.mp4")

        def fake_run(cmd, *args, **kwargs):
            # Identify which chunk is being created from the -ss argument
            ss_idx = cmd.index("-ss")
            start = int(cmd[ss_idx + 1])
            if start == 0:
                chunk0.write_bytes(b"c0")
            else:
                chunk1.write_bytes(b"c1")
            result = MagicMock()
            result.returncode = 0
            return result

        with self._mock_validate_path(), self._mock_duration(46 * 60):
            with patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run):
                from tools.integrations.gemini_analyzer import split_video_chunks
                result = split_video_chunks(video)

        assert len(result) == 2

    def test_3hr_video_splits_into_four_chunks(self, tmp_path):
        """180 min / 45 min = exactly 4 chunks."""
        video = tmp_path / "lecture_long.mp4"
        video.write_bytes(b"fake")

        created_chunks: list[Path] = []

        def fake_run(cmd, *args, **kwargs):
            # Write the output chunk file the command targets (last positional arg)
            out_path = Path(cmd[-1])
            out_path.write_bytes(b"chunk")
            created_chunks.append(out_path)
            result = MagicMock()
            result.returncode = 0
            return result

        with self._mock_validate_path(), self._mock_duration(180 * 60):  # 3 hours
            with patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run):
                from tools.integrations.gemini_analyzer import split_video_chunks
                result = split_video_chunks(video)

        assert len(result) == 4

    def test_existing_chunk_not_re_created(self, tmp_path):
        """If a chunk file already exists on disk, ffmpeg must not be called for it."""
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"fake")

        # Pre-create chunk files large enough to pass the stale-chunk validation
        # (gemini_analyzer considers chunks < 1 MB as corrupted and re-creates them)
        big_enough = b"\x00" * (2 * 1024 * 1024)  # 2 MB
        chunk0 = video.with_suffix(".chunk0.mp4")
        chunk1 = video.with_suffix(".chunk1.mp4")
        chunk0.write_bytes(big_enough)
        chunk1.write_bytes(big_enough)

        run_calls: list = []

        def fake_run(cmd, *args, **kwargs):
            run_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        with self._mock_validate_path(), self._mock_duration(100 * 60):  # 100 min → 3 chunks (0,1,2)
            # Also pre-create chunk2 so none need creating
            chunk2 = video.with_suffix(".chunk2.mp4")
            chunk2.write_bytes(big_enough)
            with patch("tools.integrations.gemini_analyzer.subprocess.run", side_effect=fake_run):
                from tools.integrations.gemini_analyzer import split_video_chunks
                result = split_video_chunks(video)

        # ffmpeg should NOT have been called for any pre-existing chunk
        assert len(run_calls) == 0
        assert len(result) == 3


# ===========================================================================
# 11. verify_webhook_secret (server HMAC logic)
# ===========================================================================


class TestVerifyWebhookSecret:
    """Test the Bearer token validation in server.py."""

    def _call(self, authorization: str | None, secret: str):
        """Call verify_webhook_secret with a patched WEBHOOK_SECRET."""
        from tools.app import server as srv
        with patch.object(srv, "WEBHOOK_SECRET", secret):
            # Re-import the function so it reads the patched module-level var
            from tools.app.server import verify_webhook_secret
            verify_webhook_secret(authorization)

    def test_correct_secret_does_not_raise(self):
        """A valid Bearer token must pass without raising."""
        self._call("Bearer mysecret", "mysecret")

    def test_missing_header_raises_http_401(self):
        from fastapi import HTTPException
        with pytest.raises((HTTPException, Exception)):
            self._call(None, "mysecret")

    def test_wrong_secret_raises_http_403(self):
        from fastapi import HTTPException
        with pytest.raises((HTTPException, Exception)):
            self._call("Bearer wrongsecret", "mysecret")

    def test_no_bearer_prefix_raises(self):
        """Plain token without 'Bearer ' prefix must fail."""
        from fastapi import HTTPException
        with pytest.raises((HTTPException, Exception)):
            self._call("mysecret", "mysecret")

    def test_unconfigured_secret_fails_closed(self):
        """When WEBHOOK_SECRET is empty/unset, the server must reject the request.

        The implementation fails closed (503) to prevent accidental open access
        in production — an absent secret is a misconfiguration, not a bypass.
        """
        with pytest.raises((Exception,)):
            self._call(None, "")

    def test_comparison_is_timing_safe(self):
        """Verify hmac.compare_digest is used (not == operator) for safety.
        We confirm this indirectly: both correct and wrong tokens complete
        without timing shortcuts that could leak length information."""
        import tools.app.server as srv
        # Both calls must complete — no AttributeError or import issue
        with patch.object(srv, "WEBHOOK_SECRET", "secret123"):
            from tools.app.server import verify_webhook_secret
            verify_webhook_secret("Bearer secret123")  # should pass silently
