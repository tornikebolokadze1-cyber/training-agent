"""Regression guards for multi-cohort paths that previously stopped at G2."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch


def test_backfill_auto_detect_scans_all_configured_groups(monkeypatch):
    """Deep-analysis auto-detect must include G3/G4, not just G1/G2."""
    from tools.app import admin_routes

    groups = {1: {}, 2: {}, 3: {}, 4: {}}
    calls: list[tuple[int, int, str | None]] = []

    def fake_exists(group: int, lecture: int, content_type: str | None = None) -> bool:
        calls.append((group, lecture, content_type))
        return content_type == "transcript"

    monkeypatch.setattr(admin_routes, "GROUPS", groups)
    with patch(
        "tools.integrations.knowledge_indexer.lecture_exists_in_index",
        side_effect=fake_exists,
    ):
        missing = admin_routes._auto_detect_missing_deep_analysis()

    assert sorted({group for group, _, _ in calls}) == [1, 2, 3, 4]
    assert len(missing) == 4 * admin_routes.MAX_LECTURES
    assert "g3_l1" in missing
    assert "g4_l15" in missing


def test_drive_audit_runs_every_configured_group(monkeypatch):
    """Drive audit should cover all GROUPS entries and report config gaps."""
    from tools.services import drive_audit

    groups = {
        1: {"name": "March #1", "drive_folder_id": "drive-1"},
        3: {"name": "May #1", "drive_folder_id": "drive-3"},
        4: {"name": "May #2", "drive_folder_id": ""},
    }
    calls: list[tuple[int, str]] = []

    def fake_audit_group(group: int, root_folder_id: str) -> list[drive_audit.LectureAudit]:
        calls.append((group, root_folder_id))
        audit = drive_audit.LectureAudit(group=group, lecture=1)
        if group == 3:
            audit.issues.append("PINECONE_EMPTY")
        return [audit]

    monkeypatch.setattr(drive_audit, "GROUPS", groups)
    monkeypatch.setattr(drive_audit, "audit_group", fake_audit_group)

    report = drive_audit.run_full_audit()

    assert calls == [(1, "drive-1"), (3, "drive-3")]
    assert set(report["groups"]) == {"group_1", "group_3", "group_4"}
    assert report["group_3"][0]["issues"] == ["PINECONE_EMPTY"]
    assert report["config_issues_found"] == 1
    assert "DRIVE_GROUP4_FOLDER_ID" in report["config_issues"][0]
    assert report["all_clean"] is False


def test_drive_audit_alert_includes_config_issues():
    from tools.services import drive_audit

    report = {
        "all_clean": False,
        "issues_found": 0,
        "config_issues_found": 1,
        "issues": [],
        "config_issues": ["Missing DRIVE_GROUP4_FOLDER_ID for May #2"],
    }

    with patch("tools.integrations.whatsapp_sender.alert_operator") as mock_alert:
        drive_audit.alert_on_issues(report)

    message = mock_alert.call_args.args[0]
    assert "1 issue" in message
    assert "CONFIG" in message
    assert "DRIVE_GROUP4_FOLDER_ID" in message


def test_course_overview_reports_message_counts_for_groups_beyond_g2(tmp_path, monkeypatch):
    from tools.services import unified_query

    scores_db = tmp_path / "scores.db"
    messages_db = tmp_path / "messages.db"

    with sqlite3.connect(scores_db) as conn:
        conn.execute(
            "CREATE TABLE lecture_scores ("
            "group_number INTEGER, lecture_number INTEGER, "
            "overall_score REAL, composite REAL)"
        )
        conn.execute(
            "INSERT INTO lecture_scores VALUES (3, 1, 8.0, 8.0)"
        )

    with sqlite3.connect(messages_db) as conn:
        conn.execute(
            "CREATE TABLE messages (group_number INTEGER, sender_hash TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?)",
            [(1, "a"), (3, "b"), (3, "c")],
        )
        conn.execute(
            "CREATE TABLE lecture_windows (group_number INTEGER, lecture_number INTEGER)"
        )

    monkeypatch.setattr(unified_query, "SCORES_DB", scores_db)
    monkeypatch.setattr(unified_query, "MESSAGES_DB", messages_db)

    overview = unified_query.course_overview()

    assert overview["messages"]["g1"] == 1
    assert overview["messages"]["g2"] == 0
    assert overview["messages"]["g3"] == 2
    assert overview["messages"]["by_group"] == {"g1": 1, "g3": 2}
