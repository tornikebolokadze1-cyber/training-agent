"""Tests for the three new health checks added to health_monitor."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools.core.config import TBILISI_TZ
from tools.core.health_monitor import (
    Severity,
    check_oauth_token_lifetime,
    check_pipeline_state_drift,
    check_pinecone_scores_consistency,
)


# ---------------------------------------------------------------------------
# check_oauth_token_lifetime
# ---------------------------------------------------------------------------


def test_check_oauth_token_lifetime_healthy_and_critical():
    # Healthy: 10 days
    with patch(
        "tools.core.token_manager.check_token_health",
        return_value={
            "valid": True,
            "expires_in_hours": 240.0,
            "needs_refresh": False,
            "has_refresh_token": True,
            "error": None,
        },
    ):
        result = check_oauth_token_lifetime()
        assert result.severity == Severity.OK

    # Critical: 5 hours
    with patch(
        "tools.core.token_manager.check_token_health",
        return_value={
            "valid": True,
            "expires_in_hours": 5.0,
            "needs_refresh": True,
            "has_refresh_token": True,
            "error": None,
        },
    ):
        result = check_oauth_token_lifetime()
        assert result.severity == Severity.CRITICAL
        assert "refresh required" in result.message

    # Warning: 3 days
    with patch(
        "tools.core.token_manager.check_token_health",
        return_value={
            "valid": True,
            "expires_in_hours": 72.0,
            "needs_refresh": False,
            "has_refresh_token": True,
            "error": None,
        },
    ):
        result = check_oauth_token_lifetime()
        assert result.severity == Severity.WARNING

    # Invalid token
    with patch(
        "tools.core.token_manager.check_token_health",
        return_value={
            "valid": False,
            "expires_in_hours": None,
            "needs_refresh": True,
            "has_refresh_token": False,
            "error": "refresh_token missing",
        },
    ):
        result = check_oauth_token_lifetime()
        assert result.severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# check_pipeline_state_drift
# ---------------------------------------------------------------------------


def _fake_pipeline(group: int, lecture: int, heartbeat_ago_hours: float):
    ts = datetime.now(TBILISI_TZ) - timedelta(hours=heartbeat_ago_hours)
    return SimpleNamespace(
        group=group,
        lecture=lecture,
        state="transcribing",
        last_heartbeat=ts.isoformat(),
        updated_at=ts.isoformat(),
        started_at=ts.isoformat(),
    )


def test_check_pipeline_state_drift_ok_and_critical():
    # Healthy: recent heartbeat
    with patch(
        "tools.core.pipeline_state.list_active_pipelines",
        return_value=[_fake_pipeline(1, 5, 0.5)],
    ):
        result = check_pipeline_state_drift()
        assert result.severity == Severity.OK

    # Warning: 6h stale
    with patch(
        "tools.core.pipeline_state.list_active_pipelines",
        return_value=[_fake_pipeline(1, 5, 6.0)],
    ):
        result = check_pipeline_state_drift()
        assert result.severity == Severity.WARNING

    # Critical: 15h stale
    with patch(
        "tools.core.pipeline_state.list_active_pipelines",
        return_value=[_fake_pipeline(2, 7, 15.0)],
    ):
        result = check_pipeline_state_drift()
        assert result.severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# check_pinecone_scores_consistency
# ---------------------------------------------------------------------------


def test_check_pinecone_scores_consistency_ok_and_drift():
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = [(1, 1), (1, 2), (2, 3)]

    # Healthy: all present
    with (
        patch(
            "tools.core.health_monitor.sqlite3.connect", return_value=fake_conn
        ),
        patch(
            "tools.integrations.knowledge_indexer.lecture_exists_in_index",
            return_value=True,
        ),
    ):
        result = check_pinecone_scores_consistency()
        assert result.severity == Severity.OK

    # Drift: one missing
    def _exists(group, lecture):
        return not (group == 1 and lecture == 2)

    fake_conn2 = MagicMock()
    fake_conn2.execute.return_value.fetchall.return_value = [(1, 1), (1, 2), (2, 3)]
    with (
        patch(
            "tools.core.health_monitor.sqlite3.connect", return_value=fake_conn2
        ),
        patch(
            "tools.integrations.knowledge_indexer.lecture_exists_in_index",
            side_effect=_exists,
        ),
    ):
        result = check_pinecone_scores_consistency()
        assert result.severity == Severity.WARNING
        assert "G1 L2" in result.message
