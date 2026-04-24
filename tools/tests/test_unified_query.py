"""Tests for tools.services.unified_query.

Covers all public entry points with temp DBs so tests are hermetic.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.services import unified_query as uq  # noqa: E402


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #

MESSAGES_SQL = (PROJECT_ROOT / "scripts" / "migrate_001_messages.sql").read_text()

SCORES_SCHEMA = """
CREATE TABLE lecture_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_number INTEGER NOT NULL,
    lecture_number INTEGER NOT NULL,
    content_depth REAL NOT NULL,
    practical_value REAL NOT NULL,
    engagement REAL NOT NULL,
    technical_accuracy REAL NOT NULL,
    market_relevance REAL NOT NULL,
    overall_score REAL,
    composite REAL NOT NULL,
    raw_score_text TEXT,
    processed_at TEXT NOT NULL,
    UNIQUE (group_number, lecture_number)
);
"""


@pytest.fixture
def temp_databases(tmp_path, monkeypatch):
    """Point unified_query module at hermetic temp DBs + vault."""
    messages_db = tmp_path / "messages.db"
    scores_db = tmp_path / "scores.db"
    vault_root = tmp_path / "obsidian-vault"

    # Messages schema
    conn = sqlite3.connect(str(messages_db))
    conn.executescript(MESSAGES_SQL)
    # seed: G1 with 3 students, various signal patterns
    msgs = [
        # sender Shorena — active & asks questions
        ("m1", "g1@g.us", "h_shorena", "Shorena", "2026-04-01T10:00:00Z", "incoming", "textMessage",
         "როგორ გავიგო ეს?", None, "{}", 1, 3, 0, 0),
        ("m2", "g1@g.us", "h_shorena", "Shorena", "2026-04-01T11:00:00Z", "incoming", "textMessage",
         "ვერ ვხვდები რა ხდება", None, "{}", 1, 3, 0, 0),
        ("m3", "g1@g.us", "h_shorena", "Shorena", "2026-04-01T12:00:00Z", "incoming", "textMessage",
         "უკვე ვიცი, მადლობა", None, "{}", 1, 3, 0, 0),
        # sender Koba — silent
        ("m4", "g1@g.us", "h_koba", "Koba", "2026-04-02T10:00:00Z", "incoming", "textMessage",
         "კარგია", None, "{}", 1, 3, 0, 0),
        # bot reply
        ("m5", "g1@g.us", "h_bot", None, "2026-04-01T10:30:00Z", "outgoing", "textMessage",
         "პასუხი ბოტიდან", None, "{}", 1, 3, 1, 0),
        # G2 Misho
        ("m6", "g2@g.us", "h_misho", "Misho", "2026-04-03T10:00:00Z", "incoming", "textMessage",
         "skill რა არის? ვერ ვხვდები", None, "{}", 2, 4, 0, 0),
        ("m7", "g2@g.us", "h_misho", "Misho", "2026-04-03T11:00:00Z", "incoming", "textMessage",
         "რატომ არ მუშაობს?", None, "{}", 2, 4, 0, 0),
    ]
    conn.executemany(
        """INSERT INTO messages
           (green_api_id, chat_id, sender_hash, sender_display,
            ts_message, direction, msg_type, content, quoted_green_id,
            raw_payload, group_number, lecture_context, is_bot, redacted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        msgs,
    )
    conn.execute(
        "INSERT INTO lecture_windows (group_number, lecture_number, started_at, ends_at) VALUES (1, 3, '2026-04-01', '2026-04-03')"
    )
    conn.execute(
        "INSERT INTO lecture_windows (group_number, lecture_number, started_at, ends_at) VALUES (2, 4, '2026-04-03', '2026-04-06')"
    )
    conn.commit()
    conn.close()

    # Scores DB
    conn = sqlite3.connect(str(scores_db))
    conn.executescript(SCORES_SCHEMA)
    conn.execute(
        """INSERT INTO lecture_scores
           (group_number, lecture_number, content_depth, practical_value,
            engagement, technical_accuracy, market_relevance, overall_score,
            composite, raw_score_text, processed_at)
           VALUES (1, 3, 6.0, 7.0, 5.0, 6.0, 7.0, 6.2, 6.2, '', '2026-04-01T12:00:00Z')"""
    )
    conn.commit()
    conn.close()

    # Obsidian vault — one analysis file with a pattern
    analysis = vault_root / "ანალიზი" / "ჯგუფი 1"
    analysis.mkdir(parents=True)
    (analysis / "ლექცია 3 -- ანალიზი.md").write_text(
        "# ლექცია 3\n\nამ ლექციაზე სტუდენტებმა skill-ები ისწავლეს.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(uq, "MESSAGES_DB", messages_db)
    monkeypatch.setattr(uq, "SCORES_DB", scores_db)
    monkeypatch.setattr(uq, "OBSIDIAN_ROOT", vault_root)
    return tmp_path


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #

class TestStudentJourney:
    def test_matches_by_display_name(self, temp_databases):
        r = uq.student_journey("Shorena")
        assert r["matched_hashes"] == 1
        summary = r["matches"][0]["summary"]
        assert summary["sender_display"] == "Shorena"
        assert summary["total_messages"] == 3
        assert summary["confusion_count"] == 1
        assert summary["question_count"] == 1

    def test_case_insensitive(self, temp_databases):
        r = uq.student_journey("SHORENA")
        assert r["matched_hashes"] == 1

    def test_partial_match(self, temp_databases):
        r = uq.student_journey("Shor")
        assert r["matched_hashes"] == 1

    def test_no_match(self, temp_databases):
        r = uq.student_journey("DoesNotExist")
        assert "error" in r

    def test_excludes_bot(self, temp_databases):
        # bot sender_display is None, will match nothing
        r = uq.student_journey("ბოტი")
        assert "error" in r

    def test_includes_samples(self, temp_databases):
        r = uq.student_journey("Shorena", include_samples=5)
        match = r["matches"][0]
        assert len(match["sample_messages"]) <= 3  # we have 3 Shorena msgs


class TestLectureContext:
    def test_known_lecture(self, temp_databases):
        r = uq.lecture_context(1, 3)
        assert r["group"] == 1 and r["lecture"] == 3
        assert r["score"]["overall"] == 6.2
        assert r["chat_stats"]["n"] == 5  # 3 Shorena + 1 Koba + 1 bot reply
        assert r["chat_stats"]["student_msgs"] == 4
        assert r["chat_stats"]["bot_msgs"] == 1
        assert r["analysis_file"] is not None
        assert "skill" in r["analysis_file"]["first_500"]

    def test_lecture_without_score(self, temp_databases):
        r = uq.lecture_context(2, 99)
        assert r["score"] is None
        # chat_stats may still be returned with n=0
        assert r["analysis_file"] is None


class TestTopicScan:
    def test_message_hits(self, temp_databases):
        r = uq.topic_scan("skill")
        assert r["pattern"] == "skill"
        # only Misho's message contains "skill" (English)
        assert any("skill" in m["snippet"] for m in r["messages"])

    def test_analysis_hits(self, temp_databases):
        r = uq.topic_scan("skill")
        assert len(r["analyses"]) >= 1

    def test_no_hits(self, temp_databases):
        r = uq.topic_scan("xyzzynonexistent")
        assert r["messages"] == []
        assert r["analyses"] == []


class TestConfusionMap:
    def test_global_ranking(self, temp_databases):
        r = uq.confusion_map()
        ranking = r["ranking"]
        # Misho has 2 confusion (both messages match), Shorena has 1
        names = [row["sender"] for row in ranking]
        assert "Misho" in names
        # Misho should rank higher
        assert ranking[0]["sender"] == "Misho"

    def test_group_filter(self, temp_databases):
        r = uq.confusion_map(group_number=1)
        names = [row["sender"] for row in r["ranking"]]
        assert "Shorena" in names
        assert "Misho" not in names


class TestSilentStudents:
    def test_threshold_filter(self, temp_databases):
        r = uq.silent_students(1, threshold=2)
        # Shorena has 3 (not silent), Koba has 1 (silent at <2)
        names = [row["sender"] for row in r["silent"]]
        assert "Koba" in names
        assert "Shorena" not in names

    def test_higher_threshold_includes_more(self, temp_databases):
        r = uq.silent_students(1, threshold=10)
        names = [row["sender"] for row in r["silent"]]
        assert "Shorena" in names
        assert "Koba" in names


class TestCourseOverview:
    def test_returns_counts(self, temp_databases):
        r = uq.course_overview()
        assert r["messages"]["total"] == 7
        assert r["messages"]["g1"] == 5
        assert r["messages"]["g2"] == 2
        assert len(r["scores"]) == 1
        assert r["lecture_windows"] == 2
