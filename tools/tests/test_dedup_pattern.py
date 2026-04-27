"""Tests for the recording-name regex and pattern dedup helper.

These tests prevent the dedup logic from accidentally widening or
narrowing what it considers a "lecture recording" filename, which would
either leave duplicates behind or trash the wrong files.
"""

from __future__ import annotations

import pytest

from tools.integrations.gdrive_manager import _RECORDING_NAME_RE


# ---------------------------------------------------------------------------
# _RECORDING_NAME_RE — must accept all real pipeline filenames and reject
#                     any non-recording filename it might be called with.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, group, lecture",
    [
        ("group1_lecture8_20260408_065110.mp4", "1", "8"),
        ("group1_lecture8_20260408_065110_seg0.mp4", "1", "8"),
        ("group2_lecture6_20260406_000500.mp4", "2", "6"),
        # Two-digit lecture numbers must work for full course coverage.
        ("group1_lecture10_20260520_180000.mp4", "1", "10"),
        ("group2_lecture15_20260530_180000.mp4", "2", "15"),
        # Case insensitive — defensive against future tooling.
        ("Group1_Lecture8_20260408.MP4", "1", "8"),
    ],
)
def test_recording_name_re_accepts_pipeline_filenames(filename, group, lecture):
    match = _RECORDING_NAME_RE.match(filename)
    assert match is not None, f"regex should match {filename!r}"
    assert match.group("group") == group
    assert match.group("lecture") == lecture


@pytest.mark.parametrize(
    "filename",
    [
        # Summary docs and transcripts must NOT be treated as recordings,
        # otherwise the dedup helper would happily trash them.
        "g1_l8_summary.txt",
        "g1_l8_full_transcript.txt",
        "ლექცია #8 — შეჯამება",
        # PDFs uploaded as supplemental materials.
        "ლექცია #1-ის პრეზენტაცია.pdf",
        # Wrong extension.
        "group1_lecture8_20260408.mov",
        # Missing required prefix structure.
        "lecture8.mp4",
        "group_lecture8.mp4",
        "group1_8.mp4",
        # Empty / whitespace.
        "",
        "   ",
    ],
)
def test_recording_name_re_rejects_non_recordings(filename):
    assert _RECORDING_NAME_RE.match(filename) is None, (
        f"regex must NOT match {filename!r}"
    )


def test_lecture_10_does_not_collide_with_lecture_1():
    """Critical edge case: lecture10 must not be confused with lecture1.

    Without anchoring, `lecture1.*` would greedily match `lecture10_...`
    and the dedup helper would trash lecture 10 files when uploading
    lecture 1 (or vice versa).
    """
    l1_match = _RECORDING_NAME_RE.match("group1_lecture1_20260315_180000.mp4")
    l10_match = _RECORDING_NAME_RE.match("group1_lecture10_20260515_180000.mp4")
    assert l1_match.group("lecture") == "1"
    assert l10_match.group("lecture") == "10"
    assert l1_match.group("lecture") != l10_match.group("lecture")
