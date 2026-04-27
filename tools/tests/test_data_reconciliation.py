"""Tests for tools.services.data_reconciliation."""

from unittest.mock import patch

from tools.services import data_reconciliation
from tools.services.data_reconciliation import (
    ReconciliationReport,
    alert_on_drift,
    reconcile_state_drift,
)


def _exists_factory(indexed: set[tuple[int, int]]):
    def _exists(group: int, lecture: int, *args, **kwargs) -> bool:
        return (group, lecture) in indexed
    return _exists


def test_reconcile_no_drift():
    """All three sources agree -> empty report, has_drift=False."""
    with patch(
        "tools.integrations.knowledge_indexer.lecture_exists_in_index",
        side_effect=_exists_factory({(1, 1)}),
    ), patch.object(
        data_reconciliation, "_scan_scores_db", return_value={(1, 1)}
    ), patch.object(
        data_reconciliation, "_scan_state_files", return_value=[]
    ):
        report = reconcile_state_drift()

    assert report.error is None
    assert report.has_drift is False
    assert report.in_both == [(1, 1)]
    assert report.in_pinecone_only == []
    assert report.in_scores_db_only == []
    assert report.state_file_orphans == []


def test_reconcile_scores_only_drift():
    """Lecture in scores DB but missing from Pinecone (the dangerous drift)."""
    with patch(
        "tools.integrations.knowledge_indexer.lecture_exists_in_index",
        side_effect=_exists_factory(set()),
    ), patch.object(
        data_reconciliation, "_scan_scores_db", return_value={(2, 7)}
    ), patch.object(
        data_reconciliation, "_scan_state_files", return_value=[]
    ):
        report = reconcile_state_drift()

    assert report.has_drift is True
    assert report.in_scores_db_only == [(2, 7)]
    assert report.in_pinecone_only == []
    assert report.total_drift_count == 1


def test_alert_on_drift_sends_message():
    """A report with drift triggers alert_operator with a useful message."""
    report = ReconciliationReport(in_scores_db_only=[(2, 7)])

    with patch(
        "tools.integrations.whatsapp_sender.alert_operator"
    ) as mock_alert:
        sent = alert_on_drift(report)

    assert sent is True
    assert mock_alert.call_count == 1
    message = mock_alert.call_args[0][0]
    assert "G2 L7" in message
