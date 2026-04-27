"""Unit tests for tools/services/whatsapp_catchup.py.

Covers the catch-up replay service that recovers messages the live
``/whatsapp-incoming`` webhook missed. All external dependencies
(Green API, the assistant pipeline) are mocked.

Run with:
    pytest tools/tests/test_whatsapp_catchup.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Bypass the conftest NoOp stub for the catchup module only — tests for
# the assistant elsewhere already pop and import the real
# tools.services.whatsapp_assistant, and re-popping it here would create a
# second class identity that breaks instances built by earlier test files
# (TestHandleMessage / TestInitSmoke) when this file runs after them.
# ---------------------------------------------------------------------------
sys.modules.pop("tools.services.whatsapp_catchup", None)

import tools.services.whatsapp_catchup as catchup  # noqa: E402
from tools.services.whatsapp_assistant import IncomingMessage, WhatsAppAssistant  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_history_entry(
    *,
    id_message: str,
    timestamp: int,
    text: str = "",
    direction: str = "incoming",
    sender_id: str = "995599000001@c.us",
    sender_name: str = "Student",
    quoted_text: str = "",
    type_message: str = "textMessage",
) -> dict:
    """Build a Green API getChatHistory-style dict entry."""
    entry: dict = {
        "idMessage": id_message,
        "timestamp": timestamp,
        "type": direction,
        "typeMessage": type_message,
        "senderId": sender_id,
        "senderName": sender_name,
    }
    if quoted_text:
        entry["typeMessage"] = "extendedTextMessage"
        entry["extendedTextMessage"] = {
            "text": text,
            "quotedMessage": {"textMessage": quoted_text},
        }
    elif text and type_message == "textMessage":
        entry["textMessage"] = text
    return entry


def _make_assistant_mock() -> MagicMock:
    """A mock that quacks like a WhatsAppAssistant for replay_recent."""
    mock = MagicMock(spec=WhatsAppAssistant)
    mock._respond_to_missed = AsyncMock(return_value="replied!")
    return mock


# ---------------------------------------------------------------------------
# 1. _extract_text_and_quote
# ---------------------------------------------------------------------------


class TestExtractTextAndQuote:
    def test_plain_text_message(self):
        raw = {"textMessage": "hello world"}
        text, quote = catchup._extract_text_and_quote(raw)
        assert text == "hello world"
        assert quote == ""

    def test_extended_text_with_quote(self):
        raw = {
            "extendedTextMessage": {
                "text": "what about this?",
                "quotedMessage": {"textMessage": "🤖 AI ასისტენტი - მრჩეველი\n---\nLLM..."},
            },
        }
        text, quote = catchup._extract_text_and_quote(raw)
        assert text == "what about this?"
        assert "მრჩეველი" in quote

    def test_top_level_quoted_message_fallback(self):
        raw = {
            "textMessage": "follow up",
            "quotedMessage": {"textMessage": "previous"},
        }
        text, quote = catchup._extract_text_and_quote(raw)
        assert text == "follow up"
        assert quote == "previous"

    def test_empty_returns_empty_strings(self):
        text, quote = catchup._extract_text_and_quote({})
        assert text == ""
        assert quote == ""


# ---------------------------------------------------------------------------
# 2. _has_trigger_word + _quoted_looks_like_bot
# ---------------------------------------------------------------------------


class TestTriggerHelpers:
    def test_trigger_word_georgian_vocative(self):
        assert catchup._has_trigger_word("მრჩეველო, რა არის LLM?") is True

    def test_trigger_word_georgian_alternative(self):
        assert catchup._has_trigger_word("მრჩეველი, ახსენი") is True

    def test_no_trigger_word(self):
        assert catchup._has_trigger_word("რა ჩანს დღეს ლექციაზე?") is False

    def test_quoted_bot_signature_triggers(self):
        bot_msg = "🤖 AI ასისტენტი - მრჩეველი\n---\nLLM means..."
        assert catchup._quoted_looks_like_bot(bot_msg) is True

    def test_quoted_user_message_does_not_trigger(self):
        assert catchup._quoted_looks_like_bot("რა მაგარი თემაა") is False

    def test_quoted_message_with_trigger_word_inside_triggers(self):
        # Chained reply: student quoted another student who used the trigger.
        assert catchup._quoted_looks_like_bot("მრჩეველო, ახსენი") is True


# ---------------------------------------------------------------------------
# 3. _bot_already_replied — chronology
# ---------------------------------------------------------------------------


class TestBotAlreadyReplied:
    """Green API returns history newest-first, so lower indices are NEWER."""

    def test_no_outgoing_after_trigger_means_unreplied(self):
        history = [
            _make_history_entry(id_message="b", timestamp=2000, text="another", direction="incoming"),
            _make_history_entry(id_message="a", timestamp=1000, text="მრჩეველო", direction="incoming"),
        ]
        # Trigger is at index 1; index 0 is newer but also incoming.
        assert catchup._bot_already_replied(history, 1) is False

    def test_outgoing_bot_within_3min_after_trigger_marks_replied(self):
        # Bot reply must carry the assistant signature; a plain outgoing
        # message from the operator's phone would not count.
        history = [
            _make_history_entry(
                id_message="bot", timestamp=1060,
                text="🤖 AI ასისტენტი - მრჩეველი\n---\nreply body",
                direction="outgoing", sender_id="bot",
            ),
            _make_history_entry(
                id_message="trigger", timestamp=1000, text="მრჩეველო", direction="incoming",
            ),
        ]
        assert catchup._bot_already_replied(history, 1) is True

    def test_outgoing_without_signature_does_not_mark_replied(self):
        # Operator typing a follow-up to their own trigger must not be
        # mistaken for a bot reply.
        history = [
            _make_history_entry(
                id_message="op_followup", timestamp=1060,
                text="დავამატე — სასწავლო თემაა",
                direction="outgoing",
            ),
            _make_history_entry(
                id_message="trigger", timestamp=1000, text="მრჩეველო",
                direction="incoming",
            ),
        ]
        assert catchup._bot_already_replied(history, 1) is False

    def test_outgoing_more_than_3min_later_does_not_count(self):
        history = [
            _make_history_entry(
                id_message="bot", timestamp=1500,
                text="🤖 AI ასისტენტი - მრჩეველი\n---\nlate reply",
                direction="outgoing", sender_id="bot",
            ),
            _make_history_entry(
                id_message="trigger", timestamp=1000, text="მრჩეველო", direction="incoming",
            ),
        ]
        # 500s > RESPONSE_LOOKAHEAD_SECONDS (180)
        assert catchup._bot_already_replied(history, 1) is False


# ---------------------------------------------------------------------------
# 4. dedup ledger persistence
# ---------------------------------------------------------------------------


class TestDedupLedger:
    def test_round_trip(self, tmp_path: Path, monkeypatch):
        ledger = tmp_path / "responded.json"
        monkeypatch.setattr(catchup, "DEDUP_FILE", ledger)

        catchup._save_responded_ids({"id_1", "id_2"})
        loaded = catchup._load_responded_ids()
        assert loaded == {"id_1", "id_2"}

    def test_missing_file_returns_empty_set(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "absent.json")
        assert catchup._load_responded_ids() == set()

    def test_corrupt_file_returns_empty_set(self, tmp_path: Path, monkeypatch):
        bad = tmp_path / "bad.json"
        bad.write_text("not-json", encoding="utf-8")
        monkeypatch.setattr(catchup, "DEDUP_FILE", bad)
        assert catchup._load_responded_ids() == set()


# ---------------------------------------------------------------------------
# 5. replay_recent — end-to-end with mocked Green API + assistant
# ---------------------------------------------------------------------------


class TestReplayRecent:
    """Verify the full replay pipeline behaves correctly under mocked I/O."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_no_chats_configured_returns_empty(self, monkeypatch):
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: [])
        result = self._run(catchup.replay_recent(_make_assistant_mock()))
        assert result["replied"] == 0
        assert result["checked"] == 0

    def test_replays_unanswered_direct_trigger(self, monkeypatch, tmp_path):
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        # Newest-first: only an unanswered direct trigger.
        history = [
            _make_history_entry(
                id_message="t1", timestamp=now - 60, text="მრჩეველო, რა არის LLM?",
            ),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        assistant._respond_to_missed.assert_awaited_once()
        sent_msg = assistant._respond_to_missed.await_args.args[0]
        assert isinstance(sent_msg, IncomingMessage)
        assert sent_msg.text == "მრჩეველო, რა არის LLM?"
        assert result["replied"] == 1
        assert result["already_responded"] == 0

    def test_skips_message_already_responded_to_via_chronology(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        # Outgoing bot message must carry the signature; a plain outgoing
        # follow-up from the operator's phone would not be a valid "reply".
        history = [
            _make_history_entry(
                id_message="bot", timestamp=now - 50,
                text="🤖 AI ასისტენტი - მრჩეველი\n---\nhere is the answer",
                direction="outgoing", sender_id="bot",
            ),
            _make_history_entry(
                id_message="t1", timestamp=now - 100, text="მრჩეველო, ახსენი",
            ),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        assistant._respond_to_missed.assert_not_awaited()
        assert result["already_responded"] >= 1
        assert result["replied"] == 0

    def test_persisted_dedup_skips_known_id(self, monkeypatch, tmp_path):
        ledger = tmp_path / "ledger.json"
        ledger.write_text(json.dumps({"ids": ["t1"], "updated_at": 0}))
        monkeypatch.setattr(catchup, "DEDUP_FILE", ledger)
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        history = [
            _make_history_entry(id_message="t1", timestamp=now - 60, text="მრჩეველო, hi"),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        assistant._respond_to_missed.assert_not_awaited()
        assert result["already_responded"] == 1

    def test_replays_reply_to_bot_message(self, monkeypatch, tmp_path):
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        # Reply to a bot message — quoted_text has the assistant signature.
        history = [
            _make_history_entry(
                id_message="t1", timestamp=now - 60,
                text="გასაგებია, კიდევ მითხარი",
                quoted_text="🤖 AI ასისტენტი - მრჩეველი\n---\nLLM არის...",
            ),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        assistant._respond_to_missed.assert_awaited_once()
        sent_msg = assistant._respond_to_missed.await_args.args[0]
        assert "მრჩეველი" in sent_msg.quoted_text
        assert result["replied"] == 1

    def test_skips_outgoing_messages(self, monkeypatch, tmp_path):
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        history = [
            _make_history_entry(
                id_message="bot1", timestamp=now - 60, text="🤖 ...",
                direction="outgoing",
            ),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        assistant._respond_to_missed.assert_not_awaited()
        assert result["replied"] == 0

    def test_stops_at_cutoff_window(self, monkeypatch, tmp_path):
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        history = [
            _make_history_entry(
                id_message="old", timestamp=now - 4 * 3600,  # 4h old
                text="მრჩეველო, ძველი", direction="incoming",
            ),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        assistant._respond_to_missed.assert_not_awaited()
        assert result["out_of_window"] >= 1

    def test_per_chat_cap_limits_replies(self, monkeypatch, tmp_path):
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        # Build 8 unanswered triggers within window (newest-first).
        history = [
            _make_history_entry(
                id_message=f"t{i}", timestamp=now - (10 + i) * 60,
                text="მრჩეველო, q",
            )
            for i in range(8)
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(
                catchup.replay_recent(assistant, since_minutes=120, max_per_chat=3),
            )

        assert assistant._respond_to_missed.await_count == 3
        assert result["replied"] == 3

    def test_operator_outgoing_trigger_is_replayed(self, monkeypatch, tmp_path):
        """The Green API account holder typing manually appears as 'outgoing'.

        Without distinguishing bot replies from operator manual messages
        the catch-up service would silently skip every "მრჩეველო" the
        operator sends — the exact bug surfaced on 2026-04-26.
        """
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        history = [
            _make_history_entry(
                id_message="op1", timestamp=now - 60,
                text="მრჩეველო, ეს მე ვწერ",
                direction="outgoing",  # operator's own phone
            ),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        assistant._respond_to_missed.assert_awaited_once()
        assert result["replied"] == 1

    def test_outgoing_bot_message_is_skipped(self, monkeypatch, tmp_path):
        """Bot's own outgoing messages (signature-prefixed) must NOT trigger replay."""
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        history = [
            _make_history_entry(
                id_message="bot1", timestamp=now - 60,
                text="🤖 AI ასისტენტი - მრჩეველი\n---\nresponse body",
                direction="outgoing",
            ),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        assistant._respond_to_missed.assert_not_awaited()
        assert result["replied"] == 0

    def test_operator_manual_reply_does_not_count_as_bot_reply(
        self, monkeypatch, tmp_path,
    ):
        """A manual operator message right after a trigger must NOT mark it answered.

        Otherwise every time the operator types "მრჩეველო" and immediately
        adds a clarification, the clarification would suppress the actual
        bot response by being treated as one.
        """
        monkeypatch.setattr(catchup, "DEDUP_FILE", tmp_path / "ledger.json")
        monkeypatch.setattr(catchup, "_allowed_chats", lambda: ["test@g.us"])

        now = int(time.time())
        # Newest-first: operator clarification, then operator trigger.
        history = [
            _make_history_entry(
                id_message="op_followup", timestamp=now - 50,
                text="დავამატე — სასწავლო თემაა",
                direction="outgoing",
            ),
            _make_history_entry(
                id_message="op_trigger", timestamp=now - 100,
                text="მრჩეველო, ერთი კითხვა მაქვს",
                direction="outgoing",
            ),
        ]

        with patch.object(catchup, "get_chat_history", return_value=history):
            assistant = _make_assistant_mock()
            result = self._run(catchup.replay_recent(assistant, since_minutes=120))

        # Trigger must still be replayed even though a manual outgoing
        # message followed it within the lookahead window.
        assistant._respond_to_missed.assert_awaited()
        assert result["replied"] >= 1


# ---------------------------------------------------------------------------
# 6. _looks_like_bot_message — signature detection
# ---------------------------------------------------------------------------


class TestLooksLikeBotMessage:
    def test_emoji_prefix_triggers(self):
        assert catchup._looks_like_bot_message("🤖 AI ასისტენტი - მრჩეველი\n---\nbody") is True

    def test_signature_without_emoji_triggers(self):
        assert catchup._looks_like_bot_message("AI ასისტენტი - მრჩეველი\n---\nbody") is True

    def test_plain_user_message_does_not_trigger(self):
        assert catchup._looks_like_bot_message("მრჩეველო, რა არის LLM?") is False

    def test_empty_does_not_trigger(self):
        assert catchup._looks_like_bot_message("") is False

    def test_signature_far_into_long_text_does_not_trigger(self):
        # Beyond the 80-char leading window — counts as user content
        # that happens to mention the assistant rather than a bot reply.
        text = ("x" * 100) + " AI ასისტენტი - მრჩეველი"
        assert catchup._looks_like_bot_message(text) is False
