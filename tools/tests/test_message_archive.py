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
    def test_empty_db_auto_bootstraps(self, tmp_path):
        """Fresh DB (Railway volume just mounted) must auto-apply migration 001.

        This replaces the old behavior where connect() raised on a schemaless DB.
        Production volumes start empty — the first webhook hit needs to bootstrap
        the schema transparently rather than wait for a separate migration step.
        """
        empty_db = tmp_path / "fresh.db"
        # Note: not even a `sqlite3.connect(...).close()` precreate — the
        # parent dir mkdir + lazy file creation must work end-to-end.
        with ma.connect(empty_db) as conn:
            row = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            assert row[0] >= 1
            # Verify tables actually exist after auto-bootstrap
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "messages" in tables
            assert "senders" in tables

    def test_auto_bootstrap_creates_parent_dir(self, tmp_path):
        """connect() must create the parent directory on a fresh volume."""
        nested = tmp_path / "nonexistent_subdir" / "messages.db"
        with ma.connect(nested) as conn:
            assert conn is not None
        assert nested.parent.is_dir()
        assert nested.exists()

    def test_idempotent_second_connect_does_not_reapply(self, tmp_path):
        """A bootstrapped DB does not re-run migration on subsequent connects."""
        db = tmp_path / "x.db"
        # First connect — applies migration 001 (version=1)
        with ma.connect(db) as conn:
            initial = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
        # Reset module-level cache to force a re-check this connect
        ma._SCHEMA_CHECKED = False
        # Second connect — must NOT re-run migration (would CREATE TABLE
        # IF NOT EXISTS skip, but INSERT in the migration would duplicate
        # the row, doubling schema_migrations row count).
        with ma.connect(db) as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
        assert after == initial, (
            f"migration ran twice: {initial} rows after first connect, "
            f"{after} after second"
        )

    def test_schema_check_passes_with_valid_migration(self, temp_db):
        """A properly migrated DB connects without error (regression guard)."""
        with ma.connect(temp_db) as conn:
            assert conn is not None


class TestEnvDbPath:
    def test_env_override_resolves_at_import(self, monkeypatch, tmp_path):
        """MESSAGE_ARCHIVE_DB_PATH env var must override the default at import time.

        Verifies that on Railway, where the env var points the archive at the
        mounted volume (e.g. /app/.tmp/messages.db), a fresh import picks it up.
        """
        custom = tmp_path / "custom_archive.db"
        monkeypatch.setenv("MESSAGE_ARCHIVE_DB_PATH", str(custom))
        # Re-import to re-evaluate the module-level constant
        import importlib
        importlib.reload(ma)
        assert ma.DEFAULT_DB_PATH == custom
        # Restore module to baseline so other tests aren't affected
        monkeypatch.delenv("MESSAGE_ARCHIVE_DB_PATH", raising=False)
        importlib.reload(ma)

    def test_default_path_when_env_not_set(self, monkeypatch):
        """Falls back to PROJECT_ROOT / data / messages.db when env unset."""
        monkeypatch.delenv("MESSAGE_ARCHIVE_DB_PATH", raising=False)
        import importlib
        importlib.reload(ma)
        assert str(ma.DEFAULT_DB_PATH).endswith("data" + str(Path("/messages.db"))[-12:])
        # Restore baseline pepper after reload
        monkeypatch.setenv("SENDER_HASH_PEPPER", "unit-test-pepper")
        ma._PEPPER_WARNED = False
        ma._SCHEMA_CHECKED = False


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


# --------------------------------------------------------------------- #
# Webhook normalization + archive helper (Phase 3 wiring)
# --------------------------------------------------------------------- #

@pytest.fixture
def webhook_text_payload() -> dict:
    """Realistic Green API webhook for an incoming text message."""
    return {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": 9999, "wid": "995123@c.us"},
        "timestamp": 1745800000,
        "idMessage": "WEBHOOK_TEXT_001",
        "senderData": {
            "chatId": "120363425514041539@g.us",
            "sender": "995577123456@c.us",
            "senderName": "Test Student",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": "გამარჯობა, რატომ ვერ ვიგებ?"},
        },
    }


@pytest.fixture
def webhook_extended_payload() -> dict:
    """Webhook for an extendedTextMessage (reply / quoted)."""
    return {
        "typeWebhook": "incomingMessageReceived",
        "timestamp": 1745800100,
        "idMessage": "WEBHOOK_EXT_002",
        "senderData": {
            "chatId": "120363407739933658@g.us",
            "sender": "995599887766@c.us",
            "senderName": "Reply User",
        },
        "messageData": {
            "typeMessage": "extendedTextMessage",
            "extendedTextMessageData": {
                "text": "ეს არის ციტატა-პასუხი",
                "quotedMessage": {
                    "stanzaId": "ORIGINAL_MSG_ID",
                    "idMessage": "ORIGINAL_MSG_ID",
                },
            },
        },
    }


class TestWebhookNormalize:
    def test_text_message_basic(self, webhook_text_payload):
        msg = ma.normalize_webhook_message(webhook_text_payload)
        assert msg.green_api_id == "WEBHOOK_TEXT_001"
        assert msg.chat_id == "120363425514041539@g.us"
        assert msg.msg_type == "textMessage"
        assert msg.content == "გამარჯობა, რატომ ვერ ვიგებ?"
        assert msg.direction == "incoming"
        assert msg.is_bot is False
        assert msg.sender_display == "Test Student"
        assert len(msg.sender_hash) == 64

    def test_extended_text_with_quoted_id(self, webhook_extended_payload):
        msg = ma.normalize_webhook_message(webhook_extended_payload)
        assert msg.green_api_id == "WEBHOOK_EXT_002"
        assert msg.content == "ეს არის ციტატა-პასუხი"
        assert msg.quoted_green_id == "ORIGINAL_MSG_ID"
        assert msg.direction == "incoming"

    def test_from_me_flag_marks_outgoing(self, webhook_text_payload):
        webhook_text_payload["messageData"]["fromMe"] = True
        msg = ma.normalize_webhook_message(webhook_text_payload)
        assert msg.direction == "outgoing"
        assert msg.is_bot is True

    def test_outgoing_type_webhook_marks_outgoing(self, webhook_text_payload):
        webhook_text_payload["typeWebhook"] = "outgoingMessageReceived"
        msg = ma.normalize_webhook_message(webhook_text_payload)
        assert msg.direction == "outgoing"
        assert msg.is_bot is True

    def test_missing_id_raises(self, webhook_text_payload):
        webhook_text_payload["idMessage"] = ""
        with pytest.raises(ValueError, match="missing idMessage"):
            ma.normalize_webhook_message(webhook_text_payload)

    def test_missing_timestamp_raises(self, webhook_text_payload):
        webhook_text_payload["timestamp"] = 0
        with pytest.raises(ValueError, match="missing timestamp"):
            ma.normalize_webhook_message(webhook_text_payload)

    def test_unknown_type_keeps_raw_payload(self, webhook_text_payload):
        webhook_text_payload["messageData"]["typeMessage"] = "stickerMessage"
        msg = ma.normalize_webhook_message(webhook_text_payload)
        assert msg.msg_type == "stickerMessage"
        # raw_payload preserves the entire body for forensics
        assert msg.raw_payload["messageData"]["typeMessage"] == "stickerMessage"
        assert msg.content is None

    def test_image_with_caption(self, webhook_text_payload):
        webhook_text_payload["messageData"] = {
            "typeMessage": "imageMessage",
            "fileMessageData": {"caption": "სქრინი"},
        }
        msg = ma.normalize_webhook_message(webhook_text_payload)
        assert msg.msg_type == "imageMessage"
        assert msg.content == "სქრინი"

    def test_group_inferred_from_env(
        self, monkeypatch, webhook_text_payload
    ):
        monkeypatch.setenv("WHATSAPP_GROUP1_ID", "120363425514041539@g.us")
        ma._GROUP_ID_MAP = None  # reset cache
        msg = ma.normalize_webhook_message(webhook_text_payload)
        assert msg.group_number == 1


class TestArchiveWebhookPayload:
    def test_inserts_new_message(self, temp_db, webhook_text_payload):
        result = ma.archive_webhook_payload(webhook_text_payload, db_path=temp_db)
        assert result["inserted"] is True
        assert result["green_api_id"] == "WEBHOOK_TEXT_001"
        assert result["reason"] is None

    def test_idempotent_second_call_marks_duplicate(
        self, temp_db, webhook_text_payload
    ):
        ma.archive_webhook_payload(webhook_text_payload, db_path=temp_db)
        result = ma.archive_webhook_payload(webhook_text_payload, db_path=temp_db)
        assert result["inserted"] is False
        assert result["reason"] == "duplicate"

    def test_normalize_failure_is_swallowed(self, temp_db):
        # missing idMessage triggers normalize ValueError; the helper
        # MUST return error dict, not raise — webhook handlers rely on this.
        bad_payload = {"typeWebhook": "incomingMessageReceived"}
        result = ma.archive_webhook_payload(bad_payload, db_path=temp_db)
        assert result["inserted"] is False
        assert "normalize_error" in (result.get("reason") or "")

    def test_persists_through_connect(self, temp_db, webhook_text_payload):
        ma.archive_webhook_payload(webhook_text_payload, db_path=temp_db)
        with ma.connect(temp_db) as conn:
            row = conn.execute(
                "SELECT green_api_id, content, direction, msg_type "
                "FROM messages WHERE green_api_id=?",
                ("WEBHOOK_TEXT_001",),
            ).fetchone()
        assert row is not None
        assert row["content"] == "გამარჯობა, რატომ ვერ ვიგებ?"
        assert row["direction"] == "incoming"
        assert row["msg_type"] == "textMessage"
