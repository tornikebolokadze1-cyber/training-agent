"""Tests for tools.core.dlq — Dead Letter Queue."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.core import dlq


@pytest.fixture(autouse=True)
def _clean_dlq(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect DLQ_DIR to a temp directory and clear handlers between tests."""
    monkeypatch.setattr(dlq, "DLQ_DIR", tmp_path)
    dlq._handlers.clear()
    yield
    dlq._handlers.clear()


class TestEnqueue:
    def test_creates_json_file(self, tmp_path: Path) -> None:
        path = dlq.enqueue("drive_summary", {"title": "test"})
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["operation"] == "drive_summary"
        assert data["payload"] == {"title": "test"}
        assert data["retry_count"] == 0
        assert data["max_retries"] == dlq.DEFAULT_MAX_RETRIES

    def test_custom_max_retries(self) -> None:
        path = dlq.enqueue("test_op", {"key": "val"}, max_retries=2)
        data = json.loads(path.read_text())
        assert data["max_retries"] == 2

    def test_multiple_entries_have_unique_filenames(self) -> None:
        p1 = dlq.enqueue("op_a", {"n": 1})
        p2 = dlq.enqueue("op_a", {"n": 2})
        assert p1.name != p2.name


class TestRegisterHandler:
    def test_registers_handler(self) -> None:
        handler = MagicMock()
        dlq.register_handler("test_op", handler)
        assert "test_op" in dlq._handlers
        assert dlq._handlers["test_op"] is handler


class TestProcessAll:
    def test_calls_handler_and_removes_entry_on_success(self, tmp_path: Path) -> None:
        handler = MagicMock()
        dlq.register_handler("test_op", handler)
        path = dlq.enqueue("test_op", {"key": "value"})

        results = dlq.process_all()

        handler.assert_called_once_with({"key": "value"})
        assert results["processed"] == 1
        assert not path.exists()

    def test_increments_retry_count_on_failure(self, tmp_path: Path) -> None:
        handler = MagicMock(side_effect=RuntimeError("API down"))
        dlq.register_handler("test_op", handler)
        path = dlq.enqueue("test_op", {"key": "value"})

        results = dlq.process_all()

        assert results["failed"] == 1
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["retry_count"] == 1
        assert "API down" in data["last_error"]

    def test_moves_to_failed_after_max_retries(self, tmp_path: Path) -> None:
        handler = MagicMock(side_effect=RuntimeError("permanent fail"))
        dlq.register_handler("test_op", handler)
        path = dlq.enqueue("test_op", {"key": "value"}, max_retries=2)

        # Simulate 2 failed retries
        data = json.loads(path.read_text())
        data["retry_count"] = 2
        path.write_text(json.dumps(data))

        results = dlq.process_all()

        assert results["expired"] == 1
        assert not path.exists()
        failed_dir = tmp_path / "failed"
        assert failed_dir.exists()
        assert len(list(failed_dir.glob("*.json"))) == 1

    def test_skips_entries_with_no_handler(self, tmp_path: Path) -> None:
        dlq.enqueue("unknown_op", {"key": "value"})

        results = dlq.process_all()

        assert results["skipped"] == 1

    def test_processes_multiple_entries(self, tmp_path: Path) -> None:
        handler = MagicMock()
        dlq.register_handler("test_op", handler)
        dlq.enqueue("test_op", {"n": 1})
        dlq.enqueue("test_op", {"n": 2})

        results = dlq.process_all()

        assert results["processed"] == 2
        assert handler.call_count == 2


class TestPendingCount:
    def test_returns_zero_when_empty(self) -> None:
        assert dlq.pending_count() == 0

    def test_counts_pending_entries(self) -> None:
        dlq.enqueue("op_a", {"n": 1})
        dlq.enqueue("op_b", {"n": 2})
        assert dlq.pending_count() == 2


class TestListPending:
    def test_returns_all_entries(self) -> None:
        dlq.enqueue("op_a", {"n": 1})
        dlq.enqueue("op_b", {"n": 2})
        entries = dlq.list_pending()
        assert len(entries) == 2
        ops = {e["operation"] for e in entries}
        assert ops == {"op_a", "op_b"}


class TestDLQHandlers:
    """Test the actual DLQ handler functions in orchestrator."""

    def test_retry_drive_summary_calls_create_google_doc(self) -> None:
        from tools.app.orchestrator import _retry_drive_summary

        payload = {
            "title": "Test Doc",
            "content": "Some content",
            "folder_id": "folder123",
            "group": 1,
            "lecture": 3,
        }
        with patch(
            "tools.integrations.gdrive_manager.create_google_doc",
            return_value="doc_abc",
        ) as mock_create:
            _retry_drive_summary(payload)
            mock_create.assert_called_once_with("Test Doc", "Some content", "folder123")

    def test_retry_whatsapp_group_calls_send_notification(self) -> None:
        from tools.app.orchestrator import _retry_whatsapp_group

        payload = {
            "group_number": 1,
            "lecture_number": 5,
            "drive_recording_url": "https://drive.google.com/file/abc",
            "summary_doc_url": "https://docs.google.com/doc/xyz",
        }
        with patch(
            "tools.integrations.whatsapp_sender.send_group_upload_notification",
            return_value={"sent": True},
        ) as mock_send:
            _retry_whatsapp_group(payload)
            mock_send.assert_called_once_with(
                group_number=1,
                lecture_number=5,
                drive_recording_url="https://drive.google.com/file/abc",
                summary_doc_url="https://docs.google.com/doc/xyz",
            )

    def test_retry_pinecone_calls_index_lecture_content(self) -> None:
        from tools.app.orchestrator import _retry_pinecone

        payload = {
            "group_number": 2,
            "lecture_number": 7,
            "content": "Lecture transcript text here...",
            "content_type": "transcript",
        }
        with patch(
            "tools.integrations.knowledge_indexer.index_lecture_content",
            return_value=15,
        ) as mock_index:
            _retry_pinecone(payload)
            mock_index.assert_called_once_with(
                group_number=2,
                lecture_number=7,
                content="Lecture transcript text here...",
                content_type="transcript",
            )

    def test_retry_drive_summary_propagates_error(self) -> None:
        from tools.app.orchestrator import _retry_drive_summary

        payload = {"title": "T", "content": "C", "folder_id": "F"}
        with patch(
            "tools.integrations.gdrive_manager.create_google_doc",
            side_effect=RuntimeError("Drive API error"),
        ):
            with pytest.raises(RuntimeError, match="Drive API error"):
                _retry_drive_summary(payload)


class TestRegisterDLQHandlers:
    def test_registers_all_three_handlers(self) -> None:
        from tools.app.orchestrator import _register_dlq_handlers

        with patch("tools.core.dlq.register_handler") as mock_reg:
            _register_dlq_handlers()
            assert mock_reg.call_count == 3
            registered_ops = {call.args[0] for call in mock_reg.call_args_list}
            assert registered_ops == {"drive_summary", "whatsapp_group", "pinecone_index"}
