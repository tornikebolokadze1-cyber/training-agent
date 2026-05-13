"""Regression tests for drive_audit dynamic GROUPS iteration (US-005).

Previously ``run_full_audit`` hardcoded G1/G2 via ``DRIVE_GROUP1_FOLDER_ID``
and ``DRIVE_GROUP2_FOLDER_ID`` env vars. After the fix it iterates
``tools.core.config.GROUPS`` dynamically so new cohorts (G3, G4, ...) are
visited automatically by the 09:00 Drive vs Pinecone reconciliation.

These tests pin that behaviour by monkeypatching GROUPS and asserting:

  * every non-completed cohort is visited
  * completed cohorts are skipped (frozen archives)
  * groups without ``drive_folder_id`` emit a warning instead of crashing
"""

from __future__ import annotations

import logging

import pytest

from tools.services import drive_audit


@pytest.fixture
def fake_groups(monkeypatch):
    """Replace GROUPS with a minimal G3+G4 fixture for these tests."""
    fake = {
        3: {
            "name": "მაისის ჯგუფი #1",
            "drive_folder_id": "fake_g3_folder_id",
            "course_completed": False,
        },
        4: {
            "name": "მაისის ჯგუფი #2",
            "drive_folder_id": "fake_g4_folder_id",
            "course_completed": False,
        },
    }
    monkeypatch.setattr(drive_audit, "GROUPS", fake)
    return fake


def _stub_audit_group(monkeypatch, visited: list[tuple[int, str]]):
    """Replace audit_group with a recorder that returns no lectures."""

    def fake_audit_group(group: int, root_folder_id: str):
        visited.append((group, root_folder_id))
        return []

    monkeypatch.setattr(drive_audit, "audit_group", fake_audit_group)


def test_audit_iterates_all_groups(monkeypatch, fake_groups):
    """run_full_audit must visit every active cohort in GROUPS."""
    visited: list[tuple[int, str]] = []
    _stub_audit_group(monkeypatch, visited)

    report = drive_audit.run_full_audit()

    visited_group_nums = {g for g, _ in visited}
    assert visited_group_nums == {3, 4}, (
        f"expected G3+G4 visited, got {visited_group_nums}"
    )
    visited_folders = {f for _, f in visited}
    assert visited_folders == {"fake_g3_folder_id", "fake_g4_folder_id"}

    assert report["all_clean"] is True
    assert report["total_lectures_checked"] == 0
    assert set(report["groups"].keys()) == {"3", "4"}


def test_audit_skips_completed_courses(monkeypatch):
    """Completed cohorts must NOT be visited (frozen archives)."""
    fake = {
        1: {
            "name": "მარტის ჯგუფი #1",
            "drive_folder_id": "fake_g1_folder_id",
            "course_completed": True,  # completed — skip
        },
        3: {
            "name": "მაისის ჯგუფი #1",
            "drive_folder_id": "fake_g3_folder_id",
            "course_completed": False,
        },
    }
    monkeypatch.setattr(drive_audit, "GROUPS", fake)

    visited: list[tuple[int, str]] = []
    _stub_audit_group(monkeypatch, visited)

    report = drive_audit.run_full_audit()

    visited_group_nums = {g for g, _ in visited}
    assert visited_group_nums == {3}, (
        "completed G1 must be skipped; only active G3 should be visited"
    )
    assert "3" in report["groups"]
    assert "1" not in report["groups"]


def test_audit_warns_on_missing_folder_id(monkeypatch, caplog):
    """A group with no drive_folder_id must emit a warning and be skipped."""
    fake = {
        3: {
            "name": "მაისის ჯგუფი #1",
            "drive_folder_id": "fake_g3_folder_id",
            "course_completed": False,
        },
        4: {
            "name": "მაისის ჯგუფი #2",
            # drive_folder_id intentionally missing
            "course_completed": False,
        },
    }
    monkeypatch.setattr(drive_audit, "GROUPS", fake)

    visited: list[tuple[int, str]] = []
    _stub_audit_group(monkeypatch, visited)

    with caplog.at_level(logging.WARNING, logger=drive_audit.logger.name):
        report = drive_audit.run_full_audit()

    visited_group_nums = {g for g, _ in visited}
    assert visited_group_nums == {3}, (
        "group 4 should be skipped (no drive_folder_id); group 3 visited"
    )

    warned = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "drive_folder_id" in rec.getMessage()
        and "4" in rec.getMessage()
    ]
    assert warned, (
        f"expected a WARNING about GROUPS[4] missing drive_folder_id; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )

    # The audit should still produce a valid report despite the skip
    assert report["all_clean"] is True
    assert "3" in report["groups"]
