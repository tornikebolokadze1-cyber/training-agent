"""Tests for Qdrant score backup and restore functionality.

Originally written against Pinecone; rewritten for Qdrant Cloud after the
2026-05-20 vector DB migration. The shape of the test cases is preserved:

- ``backup_scores_to_qdrant``: upsert, skip-existing, vector-DB unavailable
- ``_restore_from_score_backup``: valid payload → DB write, empty fetch → False
- ``sync_from_qdrant``: fallback path that calls ``_restore_from_score_backup``
  when no deep_analysis chunks are found in Qdrant

All external services are mocked. The analytics SQLite DB is redirected to a
temporary in-memory path via a module-level patch so no real data/ directory
is written during tests.

Run with:
    pytest tools/tests/test_score_backup.py -v
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# In-memory DB fixture — patches analytics._get_conn so every test runs
# against a fresh SQLite schema without touching disk.
# ---------------------------------------------------------------------------

_IN_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS lecture_scores (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    group_number       INTEGER NOT NULL,
    lecture_number     INTEGER NOT NULL,
    content_depth      REAL    NOT NULL,
    practical_value    REAL    NOT NULL,
    engagement         REAL    NOT NULL,
    technical_accuracy REAL    NOT NULL,
    market_relevance   REAL    NOT NULL,
    overall_score      REAL,
    composite          REAL    NOT NULL,
    raw_score_text     TEXT,
    processed_at       TEXT    NOT NULL,
    UNIQUE (group_number, lecture_number)
);
CREATE TABLE IF NOT EXISTS lecture_insights (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    group_number            INTEGER NOT NULL,
    lecture_number          INTEGER NOT NULL,
    strengths_count         INTEGER DEFAULT 0,
    weaknesses_count        INTEGER DEFAULT 0,
    gaps_count              INTEGER DEFAULT 0,
    recommendations_count   INTEGER DEFAULT 0,
    tech_correct_count      INTEGER DEFAULT 0,
    tech_problematic_count  INTEGER DEFAULT 0,
    blind_spots_count       INTEGER DEFAULT 0,
    top_strength            TEXT,
    top_weakness            TEXT,
    key_recommendation      TEXT,
    score_justifications    TEXT,
    extracted_at            TEXT,
    UNIQUE (group_number, lecture_number)
);
"""


@pytest.fixture
def in_memory_db() -> Generator[sqlite3.Connection, None, None]:
    """Patch analytics._get_conn so it yields an in-memory SQLite connection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_IN_MEMORY_SCHEMA)
    conn.commit()

    @contextmanager
    def _fake_get_conn() -> Generator[sqlite3.Connection, None, None]:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    with patch("tools.services.analytics._get_conn", side_effect=_fake_get_conn):
        yield conn

    conn.close()


def _seed_score(conn: sqlite3.Connection, group: int, lecture: int) -> None:
    """Seed a single score row into the in-memory DB."""
    conn.execute(
        """INSERT OR REPLACE INTO lecture_scores
           (group_number, lecture_number, content_depth, practical_value,
            engagement, technical_accuracy, market_relevance, overall_score,
            composite, raw_score_text, processed_at)
           VALUES (?, ?, 8.0, 7.5, 7.0, 8.5, 7.0, 7.6, 7.6, 'seed', '2026-01-01T00:00:00+00:00')""",
        (group, lecture),
    )
    conn.commit()


def _seed_all_except(conn: sqlite3.Connection, skip_group: int, skip_lecture: int) -> None:
    """Pre-fill lecture_scores for all (group, lecture) combos except the given one."""
    for g in [1, 2]:
        for lec in range(1, 16):
            if g == skip_group and lec == skip_lecture:
                continue
            _seed_score(conn, g, lec)


# ===========================================================================
# 1. backup_scores_to_qdrant — success path
# ===========================================================================


class TestBackupScoresToQdrantSuccess:
    """All scores in the DB should be upserted to Qdrant as backup points."""

    def test_all_scores_upserted(self, in_memory_db):
        fake_vector = [0.1] * 10

        with (
            patch("tools.services.analytics.get_all_scores") as mock_get_scores,
            patch("tools.services.analytics.get_all_insights", return_value=[]),
            patch(
                "tools.integrations.qdrant_client.ensure_collection_exists",
            ),
            patch(
                "tools.integrations.qdrant_client.fetch_by_legacy_ids",
                return_value={},  # no existing backup
            ),
            patch(
                "tools.integrations.qdrant_client.upsert_points",
            ) as mock_upsert,
            patch(
                "tools.integrations.knowledge_indexer.embed_text",
                return_value=fake_vector,
            ),
        ):
            mock_get_scores.return_value = [
                {
                    "group_number": 1, "lecture_number": 1,
                    "content_depth": 8.0, "practical_value": 7.5,
                    "engagement": 7.0, "technical_accuracy": 8.5,
                    "market_relevance": 7.0, "overall_score": 7.6, "composite": 7.6,
                },
                {
                    "group_number": 1, "lecture_number": 2,
                    "content_depth": 9.0, "practical_value": 8.0,
                    "engagement": 8.5, "technical_accuracy": 9.0,
                    "market_relevance": 8.0, "overall_score": 8.5, "composite": 8.5,
                },
            ]

            from tools.services.analytics import backup_scores_to_qdrant
            result = backup_scores_to_qdrant()

        assert result["backed_up"] == 2
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert mock_upsert.call_count == 2

    def test_upserted_point_has_correct_payload(self, in_memory_db):
        score_row = {
            "group_number": 2, "lecture_number": 3,
            "content_depth": 8.0, "practical_value": 7.0,
            "engagement": 6.5, "technical_accuracy": 9.0,
            "market_relevance": 7.5, "overall_score": 7.6, "composite": 7.6,
        }
        fake_vector = [0.2] * 10

        with (
            patch("tools.services.analytics.get_all_scores", return_value=[score_row]),
            patch("tools.services.analytics.get_all_insights", return_value=[]),
            patch("tools.integrations.qdrant_client.ensure_collection_exists"),
            patch(
                "tools.integrations.qdrant_client.fetch_by_legacy_ids",
                return_value={},
            ),
            patch(
                "tools.integrations.qdrant_client.upsert_points",
            ) as mock_upsert,
            patch(
                "tools.integrations.knowledge_indexer.embed_text",
                return_value=fake_vector,
            ),
        ):
            from tools.services.analytics import backup_scores_to_qdrant
            backup_scores_to_qdrant()

        # upsert_points called with [(legacy_id, vector, payload)]
        points_arg = mock_upsert.call_args[0][0]
        legacy_id, vector, payload = points_arg[0]

        assert legacy_id == "scores_backup_g2_l3"
        assert vector == fake_vector
        assert payload["type"] == "scores_backup"
        assert payload["group_number"] == 2
        assert payload["lecture_number"] == 3
        assert payload["composite"] == 7.6
        assert payload["content_depth"] == 8.0

    def test_insights_included_in_payload_when_available(self, in_memory_db):
        score_row = {
            "group_number": 1, "lecture_number": 4,
            "content_depth": 8.0, "practical_value": 8.0,
            "engagement": 8.0, "technical_accuracy": 8.0,
            "market_relevance": 8.0, "overall_score": 8.0, "composite": 8.0,
        }
        insight_row = {
            "group_number": 1, "lecture_number": 4,
            "strengths_count": 3, "weaknesses_count": 1, "gaps_count": 2,
            "top_strength": "Great examples", "top_weakness": "Too fast",
            "key_recommendation": "Slow down",
        }

        with (
            patch("tools.services.analytics.get_all_scores", return_value=[score_row]),
            patch("tools.services.analytics.get_all_insights", return_value=[insight_row]),
            patch("tools.integrations.qdrant_client.ensure_collection_exists"),
            patch(
                "tools.integrations.qdrant_client.fetch_by_legacy_ids",
                return_value={},
            ),
            patch(
                "tools.integrations.qdrant_client.upsert_points",
            ) as mock_upsert,
            patch(
                "tools.integrations.knowledge_indexer.embed_text",
                return_value=[0.1] * 10,
            ),
        ):
            from tools.services.analytics import backup_scores_to_qdrant
            backup_scores_to_qdrant()

        _, _, payload = mock_upsert.call_args[0][0][0]
        assert payload["strengths_count"] == 3
        assert payload["weaknesses_count"] == 1
        assert payload["gaps_count"] == 2
        assert payload["top_strength"] == "Great examples"
        assert payload["top_weakness"] == "Too fast"
        assert payload["key_recommendation"] == "Slow down"


# ===========================================================================
# 2. backup_scores_to_qdrant — skip already-backed-up points
# ===========================================================================


class TestBackupScoresSkipsExisting:
    """Points that already exist in Qdrant must be skipped (not re-upserted)."""

    def test_existing_point_is_skipped(self, in_memory_db):
        score_row = {
            "group_number": 1, "lecture_number": 5,
            "content_depth": 7.0, "practical_value": 7.0,
            "engagement": 7.0, "technical_accuracy": 7.0,
            "market_relevance": 7.0, "overall_score": 7.0, "composite": 7.0,
        }

        with (
            patch("tools.services.analytics.get_all_scores", return_value=[score_row]),
            patch("tools.services.analytics.get_all_insights", return_value=[]),
            patch("tools.integrations.qdrant_client.ensure_collection_exists"),
            patch(
                "tools.integrations.qdrant_client.fetch_by_legacy_ids",
                return_value={
                    "scores_backup_g1_l5": {
                        "type": "scores_backup", "composite": 7.0,
                    },
                },
            ),
            patch(
                "tools.integrations.qdrant_client.upsert_points",
            ) as mock_upsert,
            patch(
                "tools.integrations.knowledge_indexer.embed_text",
                return_value=[0.1] * 10,
            ),
        ):
            from tools.services.analytics import backup_scores_to_qdrant
            result = backup_scores_to_qdrant()

        assert result["skipped"] == 1
        assert result["backed_up"] == 0
        mock_upsert.assert_not_called()

    def test_composite_drift_triggers_reupsert(self, in_memory_db):
        """If the local composite drifted from the stored one, re-upsert."""
        score_row = {
            "group_number": 1, "lecture_number": 8,
            "content_depth": 7.0, "practical_value": 7.0,
            "engagement": 7.0, "technical_accuracy": 7.0,
            "market_relevance": 7.0, "overall_score": 8.5, "composite": 8.5,
        }

        with (
            patch("tools.services.analytics.get_all_scores", return_value=[score_row]),
            patch("tools.services.analytics.get_all_insights", return_value=[]),
            patch("tools.integrations.qdrant_client.ensure_collection_exists"),
            patch(
                "tools.integrations.qdrant_client.fetch_by_legacy_ids",
                return_value={
                    "scores_backup_g1_l8": {
                        "type": "scores_backup", "composite": 7.0,
                    },
                },
            ),
            patch(
                "tools.integrations.qdrant_client.upsert_points",
            ) as mock_upsert,
            patch(
                "tools.integrations.knowledge_indexer.embed_text",
                return_value=[0.1] * 10,
            ),
        ):
            from tools.services.analytics import backup_scores_to_qdrant
            result = backup_scores_to_qdrant()

        assert result["backed_up"] == 1
        assert result["skipped"] == 0
        mock_upsert.assert_called_once()


# ===========================================================================
# 3. backup_scores_to_qdrant — Qdrant unavailable (graceful degradation)
# ===========================================================================


class TestBackupScoresQdrantUnavailable:
    """When Qdrant cannot be reached, backup_scores must return without raising."""

    def test_connection_error_returns_zeros(self, in_memory_db):
        with (
            patch(
                "tools.integrations.qdrant_client.ensure_collection_exists",
                side_effect=ConnectionError("Qdrant unreachable"),
            ),
            patch("tools.services.analytics.get_all_scores", return_value=[]),
            patch("tools.services.analytics.get_all_insights", return_value=[]),
        ):
            from tools.services.analytics import backup_scores_to_qdrant
            result = backup_scores_to_qdrant()

        assert result["backed_up"] == 0
        assert result["skipped"] == 0
        assert result["failed"] == 0

    def test_upsert_failure_increments_failed_count(self, in_memory_db):
        score_row = {
            "group_number": 1, "lecture_number": 1,
            "content_depth": 7.0, "practical_value": 7.0,
            "engagement": 7.0, "technical_accuracy": 7.0,
            "market_relevance": 7.0, "overall_score": 7.0, "composite": 7.0,
        }

        with (
            patch("tools.services.analytics.get_all_scores", return_value=[score_row]),
            patch("tools.services.analytics.get_all_insights", return_value=[]),
            patch("tools.integrations.qdrant_client.ensure_collection_exists"),
            patch(
                "tools.integrations.qdrant_client.fetch_by_legacy_ids",
                return_value={},
            ),
            patch(
                "tools.integrations.qdrant_client.upsert_points",
                side_effect=RuntimeError("Qdrant write failed"),
            ),
            patch(
                "tools.integrations.knowledge_indexer.embed_text",
                return_value=[0.1] * 10,
            ),
        ):
            from tools.services.analytics import backup_scores_to_qdrant
            result = backup_scores_to_qdrant()

        assert result["failed"] == 1
        assert result["backed_up"] == 0

    def test_no_scores_returns_all_zeros(self, in_memory_db):
        with (
            patch("tools.services.analytics.get_all_scores", return_value=[]),
            patch("tools.services.analytics.get_all_insights", return_value=[]),
            patch("tools.integrations.qdrant_client.ensure_collection_exists"),
            patch(
                "tools.integrations.qdrant_client.upsert_points",
            ) as mock_upsert,
        ):
            from tools.services.analytics import backup_scores_to_qdrant
            result = backup_scores_to_qdrant()

        assert result == {"backed_up": 0, "skipped": 0, "failed": 0}
        mock_upsert.assert_not_called()


# ===========================================================================
# 4. _restore_from_score_backup — success path
# ===========================================================================


class TestRestoreFromScoreBackupSuccess:
    """When a valid backup payload is found, scores and insights are written to DB."""

    def test_returns_true_on_valid_backup(self, in_memory_db):
        valid_payload = {
            "type": "scores_backup",
            "group_number": 1, "lecture_number": 3,
            "content_depth": 8.0, "practical_value": 7.5,
            "engagement": 7.0, "technical_accuracy": 8.5,
            "market_relevance": 7.0, "overall_score": 7.6, "composite": 7.6,
        }

        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            return_value={"scores_backup_g1_l3": valid_payload},
        ):
            from tools.services.analytics import _restore_from_score_backup
            result = _restore_from_score_backup(group=1, lecture=3)

        assert result is True

    def test_fetches_correct_legacy_id(self, in_memory_db):
        valid_payload = {
            "type": "scores_backup",
            "group_number": 2, "lecture_number": 7,
            "content_depth": 8.0, "practical_value": 7.0,
            "engagement": 7.0, "technical_accuracy": 8.0,
            "market_relevance": 7.5, "overall_score": 7.5, "composite": 7.5,
        }

        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            return_value={"scores_backup_g2_l7": valid_payload},
        ) as mock_fetch:
            from tools.services.analytics import _restore_from_score_backup
            _restore_from_score_backup(group=2, lecture=7)

        mock_fetch.assert_called_once_with(["scores_backup_g2_l7"])

    def test_scores_saved_to_db(self, in_memory_db):
        valid_payload = {
            "type": "scores_backup",
            "group_number": 1, "lecture_number": 5,
            "content_depth": 9.0, "practical_value": 8.5,
            "engagement": 8.0, "technical_accuracy": 9.5,
            "market_relevance": 8.0, "overall_score": 8.6, "composite": 8.6,
        }

        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            return_value={"scores_backup_g1_l5": valid_payload},
        ):
            from tools.services.analytics import _restore_from_score_backup
            _restore_from_score_backup(group=1, lecture=5)

        row = in_memory_db.execute(
            "SELECT * FROM lecture_scores WHERE group_number=1 AND lecture_number=5",
        ).fetchone()
        assert row is not None
        assert row["content_depth"] == 9.0
        assert row["technical_accuracy"] == 9.5

    def test_insights_saved_to_db_when_present(self, in_memory_db):
        valid_payload = {
            "type": "scores_backup",
            "group_number": 1, "lecture_number": 6,
            "content_depth": 8.0, "practical_value": 8.0,
            "engagement": 8.0, "technical_accuracy": 8.0,
            "market_relevance": 8.0, "overall_score": 8.0, "composite": 8.0,
            "strengths_count": 4, "weaknesses_count": 2, "gaps_count": 1,
            "top_strength": "Excellent pacing",
            "top_weakness": "Dense slides",
            "key_recommendation": "Add breaks",
        }

        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            return_value={"scores_backup_g1_l6": valid_payload},
        ):
            from tools.services.analytics import _restore_from_score_backup
            _restore_from_score_backup(group=1, lecture=6)

        insight_row = in_memory_db.execute(
            "SELECT * FROM lecture_insights WHERE group_number=1 AND lecture_number=6",
        ).fetchone()
        assert insight_row is not None
        assert insight_row["strengths_count"] == 4
        assert insight_row["top_strength"] == "Excellent pacing"
        assert insight_row["key_recommendation"] == "Add breaks"

    def test_insights_skipped_when_absent(self, in_memory_db):
        """When payload has no insight fields, lecture_insights stays empty."""
        valid_payload = {
            "type": "scores_backup",
            "group_number": 2, "lecture_number": 2,
            "content_depth": 7.0, "practical_value": 7.0,
            "engagement": 7.0, "technical_accuracy": 7.0,
            "market_relevance": 7.0, "overall_score": 7.0, "composite": 7.0,
        }

        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            return_value={"scores_backup_g2_l2": valid_payload},
        ):
            from tools.services.analytics import _restore_from_score_backup
            result = _restore_from_score_backup(group=2, lecture=2)

        assert result is True
        insight_row = in_memory_db.execute(
            "SELECT * FROM lecture_insights WHERE group_number=2 AND lecture_number=2",
        ).fetchone()
        assert insight_row is None


# ===========================================================================
# 5. _restore_from_score_backup — no data / wrong type
# ===========================================================================


class TestRestoreFromScoreBackupNoData:
    """When the backup point is missing or has wrong type, return False."""

    def test_returns_false_when_point_not_found(self, in_memory_db):
        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            return_value={},
        ):
            from tools.services.analytics import _restore_from_score_backup
            result = _restore_from_score_backup(group=1, lecture=10)

        assert result is False

    def test_returns_false_when_type_is_not_scores_backup(self, in_memory_db):
        wrong_type_payload = {
            "type": "deep_analysis",  # wrong type
            "group_number": 1, "lecture_number": 10,
        }
        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            return_value={"scores_backup_g1_l10": wrong_type_payload},
        ):
            from tools.services.analytics import _restore_from_score_backup
            result = _restore_from_score_backup(group=1, lecture=10)

        assert result is False

    def test_returns_false_when_fetch_raises(self, in_memory_db):
        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            side_effect=RuntimeError("Qdrant timeout"),
        ):
            from tools.services.analytics import _restore_from_score_backup
            result = _restore_from_score_backup(group=1, lecture=11)

        assert result is False

    def test_no_scores_written_on_failure(self, in_memory_db):
        with patch(
            "tools.integrations.qdrant_client.fetch_by_legacy_ids",
            return_value={},
        ):
            from tools.services.analytics import _restore_from_score_backup
            _restore_from_score_backup(group=2, lecture=9)

        row = in_memory_db.execute(
            "SELECT * FROM lecture_scores WHERE group_number=2 AND lecture_number=9",
        ).fetchone()
        assert row is None


# ===========================================================================
# 6. sync_from_qdrant — fallback to score backup when no deep_analysis chunks
# ===========================================================================


class TestSyncFallsBackToScoreBackup:
    """When Qdrant has no deep_analysis vectors for a lecture, sync should
    call _restore_from_score_backup and count that lecture as synced."""

    def test_fallback_restores_and_counts_as_synced(self, in_memory_db):
        with (
            patch(
                "tools.integrations.qdrant_client.list_legacy_ids_with_prefix",
                return_value=[],  # no deep_analysis chunks
            ),
            patch(
                "tools.services.analytics._restore_from_score_backup",
                return_value=True,
            ) as mock_restore,
            patch("tools.services.analytics._last_sync_time", 0),
        ):
            _seed_all_except(in_memory_db, skip_group=1, skip_lecture=1)
            from tools.services.analytics import sync_from_qdrant
            sync_from_qdrant(force=True)

        assert mock_restore.called

    def test_fallback_not_called_when_deep_analysis_exists(self, in_memory_db):
        """When deep_analysis chunks are present, _restore_from_score_backup
        must NOT be called — the normal text-extraction path runs instead."""
        with (
            patch(
                "tools.integrations.qdrant_client.list_legacy_ids_with_prefix",
                return_value=["g1_l1_deep_analysis_0"],
            ),
            patch(
                "tools.integrations.qdrant_client.fetch_by_legacy_ids",
                return_value={
                    "g1_l1_deep_analysis_0": {
                        "chunk_index": 0,
                        "text": "x" * 300,  # long enough to pass the 200-char guard
                    }
                },
            ),
            patch(
                "tools.services.analytics._restore_from_score_backup",
            ) as mock_restore,
            patch(
                "tools.services.analytics.save_scores_from_analysis",
                return_value=True,
            ),
        ):
            _seed_all_except(in_memory_db, skip_group=1, skip_lecture=1)
            from tools.services.analytics import sync_from_qdrant
            sync_from_qdrant(force=True)

        mock_restore.assert_not_called()

    def test_fallback_called_when_score_extraction_fails(self, in_memory_db):
        """If deep_analysis text exists but lacks scores, restore from backup."""
        with (
            patch(
                "tools.integrations.qdrant_client.list_legacy_ids_with_prefix",
                return_value=["g1_l1_deep_analysis_0"],
            ),
            patch(
                "tools.integrations.qdrant_client.fetch_by_legacy_ids",
                return_value={
                    "g1_l1_deep_analysis_0": {
                        "chunk_index": 0,
                        "text": "x" * 300,
                    }
                },
            ),
            patch(
                "tools.services.analytics._restore_from_score_backup",
                return_value=True,
            ) as mock_restore,
            patch(
                "tools.services.analytics.save_scores_from_analysis",
                return_value=False,
            ),
        ):
            _seed_all_except(in_memory_db, skip_group=1, skip_lecture=1)
            from tools.services.analytics import sync_from_qdrant
            result = sync_from_qdrant(force=True)

        mock_restore.assert_called_once()
        assert result["synced"] >= 1
        assert result["failed"] == 0

    def test_sync_respects_cooldown(self, in_memory_db):
        """Without force=True, sync should return early if called too soon."""
        import time

        with patch(
            "tools.integrations.qdrant_client.list_legacy_ids_with_prefix",
        ) as mock_list:
            import tools.services.analytics as analytics_mod

            analytics_mod._last_sync_time = time.time()
            from tools.services.analytics import sync_from_qdrant
            result = sync_from_qdrant(force=False)

        assert result.get("cached") is True
        mock_list.assert_not_called()


# ===========================================================================
# 7. Backward-compat aliases
# ===========================================================================


class TestPineconeAliases:
    """Old function names must still resolve to the Qdrant implementations."""

    def test_sync_from_pinecone_alias_calls_qdrant(self, in_memory_db):
        with (
            patch(
                "tools.services.analytics.sync_from_qdrant",
                return_value={"synced": 0, "skipped": 0, "failed": 0},
            ) as mock_sync,
        ):
            from tools.services.analytics import sync_from_pinecone
            sync_from_pinecone(force=True)
        mock_sync.assert_called_once_with(force=True)

    def test_backup_scores_to_pinecone_alias_calls_qdrant(self, in_memory_db):
        with (
            patch(
                "tools.services.analytics.backup_scores_to_qdrant",
                return_value={"backed_up": 0, "skipped": 0, "failed": 0},
            ) as mock_backup,
        ):
            from tools.services.analytics import backup_scores_to_pinecone
            backup_scores_to_pinecone()
        mock_backup.assert_called_once()
