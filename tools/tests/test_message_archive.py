"""Tests for tools.services.message_archive.

Covers:
    * deterministic hashing
    * Green API payload normalization
    * INSERT / idempotency
    * bulk_insert
    * query helpers
    * retrospective analysis sanity
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.services import message_archive as ma  # noqa: E402


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #

@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """A fresh empty messages.db applied from the migration SQL."""
    db = tmp_path / "messages.db"
    sql = (PROJECT_ROOT / "scripts" / "migrate_001_messages.sql").read_text()
    conn = sqlite3.connect(str(db))
    conn.executescript(sql)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _deterministic_pepper(monkeypatch):
    """Use a fixed pepper so hash assertions are reproducible."""
    monkeypatch.setenv("SENDER_HASH_PEPPER", "unit-test-pepper")
    # reset module-level caches so each test starts clean
    ma._PEPPER_WARNED = False
    ma._GROUP_ID_MAP = None
    ma._SCHEMA_CHECKED = False  # reset so schema check runs against each temp_db


# --------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------- #

class TestSenderHash:
    def test_deterministic_same_input(self):
        a = ma.sender_hash("995551234567")
        b = ma.sender_hash("995551234567")
        assert a == b

    def test_different_input_different_hash(self):
        a = ma.sender_hash("995551234567")
        b = ma.sender_hash("995557654321")
        assert a != b

    def test_sha256_hex_length(self):
        assert len(ma.sender_hash("xxx")) == 64

    def test_empty_input_raises(self):
        with pytest.raises(ValueError):
            ma.sender_hash("")

    def test_case_and_whitespace_normalized(self):
        assert ma.sender_hash("  ABC  ") == ma.sender_hash("abc")


# --------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------- #

class TestNormalize:
    def test_happy_path(self, sample_payload):
        m = ma.normalize_green_api_message(sample_payload, "120363@g.us", 1)
        assert m.green_api_id == "BAE5F00D1234"
        assert m.content == "როგორ გავიგო ეს?"
        assert m.direction == "incoming"
        assert m.msg_type == "textMessage"
        assert m.group_number == 1
        assert m.is_bot is False
        # Fix 6: assert exact ISO timestamp rather than fragile startswith check
        expected = datetime.fromtimestamp(1_713_900_000, tz=timezone.utc).isoformat()
        assert m.ts_message == expected

    def test_missing_id_raises(self):
        with pytest.raises(ValueError):
            ma.normalize_green_api_message(
                {"timestamp": 1, "typeMessage": "textMessage"},
                "chat",
                1,
            )

    def test_missing_timestamp_raises(self, sample_payload):
        del sample_payload["timestamp"]
        with pytest.raises(ValueError):
            ma.normalize_green_api_message(sample_payload, "c", 1)

    def test_outgoing_detected_as_bot(self, sample_payload):
        sample_payload["type"] = "outgoing"
        m = ma.normalize_green_api_message(sample_payload, "c", 1)
        assert m.direction == "outgoing"
        assert m.is_bot is True

    def test_image_caption_captured_as_content(self, sample_payload):
        sample_payload["typeMessage"] = "imageMessage"
        sample_payload["textMessage"] = None
        sample_payload["caption"] = "სურათის აღწერა"
        m = ma.normalize_green_api_message(sample_payload, "c", 1)
        assert m.content == "სურათის აღწერა"

    def test_group_inferred_from_env(self, sample_payload, monkeypatch):
        monkeypatch.setenv("WHATSAPP_GROUP1_ID", "120363XX@g.us")
        ma._GROUP_ID_MAP = None  # reset cache
        m = ma.normalize_green_api_message(sample_payload, "120363XX@g.us")
        assert m.group_number == 1

    def test_extended_text_message_content(self):
        """Fix 1: extendedTextMessage should be extracted correctly."""
        payload = {
            "idMessage": "EXT001",
            "timestamp": 1_713_900_000,
            "typeMessage": "extendedTextMessage",
            "extendedTextMessage": {"text": "გაფართოებული ტექსტი"},
            "senderId": "995551234567@c.us",
            "type": "incoming",
        }
        m = ma.normalize_green_api_message(payload, "c", 1)
        assert m.content == "გაფართოებული ტექსტი"

    def test_reaction_message_uses_reaction_field(self):
        """Fix 1: reactionMessage should use the reaction field."""
        payload = {
            "idMessage": "REACT001",
            "timestamp": 1_713_900_000,
            "typeMessage": "reactionMessage",
            "reaction": "👍",
            "senderId": "995551234567@c.us",
            "type": "incoming",
        }
        m = ma.normalize_green_api_message(payload, "c", 1)
        assert m.content == "👍"

    def test_no_false_extended_text_when_not_dict(self):
        """Fix 1: extendedTextMessage=None should not crash and content falls back."""
        payload = {
            "idMessage": "NOEXT001",
            "timestamp": 1_713_900_000,
            "typeMessage": "textMessage",
            "textMessage": "hello",
            "extendedTextMessage": None,
            "senderId": "995551234567@c.us",
            "type": "incoming",
        }
        m = ma.normalize_green_api_message(payload, "c", 1)
        assert m.content == "hello"


# --------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------- #

class TestPersistence:
    def test_insert_new_returns_true(self, temp_db, sample_payload):
        m = ma.normalize_green_api_message(sample_payload, "c", 1)
        with ma.connect(temp_db) as conn:
            assert ma.insert_message(conn, m) is True

    def test_duplicate_insert_returns_false(self, temp_db, sample_payload):
        m = ma.normalize_green_api_message(sample_payload, "c", 1)
        with ma.connect(temp_db) as conn:
            assert ma.insert_message(conn, m) is True
            assert ma.insert_message(conn, m) is False  # idempotent

    def test_bulk_insert_counts(self, temp_db, sample_payload):
        msgs = []
        for i in range(5):
            p = dict(sample_payload)
            p["idMessage"] = f"ID_{i}"
            msgs.append(ma.normalize_green_api_message(p, "c", 1))
        # insert twice to verify idempotency
        with ma.connect(temp_db) as conn:
            r1 = ma.bulk_insert(conn, msgs)
            r2 = ma.bulk_insert(conn, msgs)
        assert r1 == {"inserted": 5, "skipped": 0}
        assert r2 == {"inserted": 0, "skipped": 5}


# --------------------------------------------------------------------- #
# Schema check
# --------------------------------------------------------------------- #

class TestSchemaCheck:
    def test_missing_schema_raises(self, tmp_path):
        """Fix 5: connecting to a DB without migration 001 must raise RuntimeError."""
        empty_db = tmp_path / "empty.db"
        sqlite3.connect(str(empty_db)).close()  # create empty file
        with pytest.raises(RuntimeError, match="schema_migrations"):
            with ma.connect(empty_db):
                pass

    def test_schema_check_passes_with_valid_migration(self, temp_db):
        """Fix 5: a properly migrated DB connects without error."""
        with ma.connect(temp_db) as conn:
            assert conn is not None


# --------------------------------------------------------------------- #
# Pepper fail-closed in production
# --------------------------------------------------------------------- #

class TestPepperProduction:
    def test_raises_in_railway_env_without_pepper(self, monkeypatch):
        """Fix 4: missing pepper in production should raise RuntimeError."""
        monkeypatch.delenv("SENDER_HASH_PEPPER", raising=False)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        with pytest.raises(RuntimeError, match="SENDER_HASH_PEPPER must be set"):
            ma.sender_hash("any-phone")

    def test_pepper_fingerprint_returns_16_hex_chars(self):
        """Fix 4: fingerprint is 16 hex chars."""
        fp = ma._pepper_fingerprint()
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)


# --------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------- #

@pytest.fixture
def sample_payload() -> dict:
    return {
        "idMessage": "BAE5F00D1234",
        "timestamp": 1_713_900_000,
        "typeMessage": "textMessage",
        "textMessage": "როგორ გავიგო ეს?",
        "senderId": "995551234567@c.us",
        "senderName": "ვანო",
        "type": "incoming",
    }


class TestQueries:
    def _seed(self, db: Path, group: int, n: int, prefix: str = "q") -> None:
        with ma.connect(db) as conn:
            for i in range(n):
                payload = {
                    "idMessage": f"{prefix}_{group}_{i}",
                    "timestamp": 1_712_000_000 + i * 3600,  # +1h each
                    "typeMessage": "textMessage",
                    "textMessage": f"მაგალითი {i} რატომ" if i % 3 == 0 else f"ტექსტი {i}",
                    "senderId": f"9955000000{group:02d}@c.us",
                    "senderName": f"sender{group}",
                    "type": "incoming",
                }
                m = ma.normalize_green_api_message(payload, f"g{group}@g.us", group)
                ma.insert_message(conn, m)

    def test_count_by_group(self, temp_db):
        self._seed(temp_db, 1, 7)
        self._seed(temp_db, 2, 3)
        with ma.connect(temp_db) as conn:
            counts = ma.count_by_group(conn)
        assert counts == {"group_1": 7, "group_2": 3}

    def test_search_content(self, temp_db):
        self._seed(temp_db, 1, 9)  # every 3rd contains "რატომ"
        with ma.connect(temp_db) as conn:
            hits = ma.search_content(conn, "რატომ", group_number=1)
        assert len(hits) == 3

    def test_search_content_is_group_scoped(self, temp_db):
        self._seed(temp_db, 1, 6)
        self._seed(temp_db, 2, 6)
        with ma.connect(temp_db) as conn:
            g1 = ma.search_content(conn, "რატომ", group_number=1)
            g2 = ma.search_content(conn, "რატომ", group_number=2)
        assert len(g1) == 2 and len(g2) == 2
        assert all(r["group_number"] == 1 for r in g1)
        assert all(r["group_number"] == 2 for r in g2)
