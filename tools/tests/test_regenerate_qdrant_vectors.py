"""Smoke tests for scripts/regenerate_qdrant_vectors.py.

Verifies the script:
  * imports cleanly without contacting Gemini or Qdrant
  * --dry-run does not call the Qdrant upsert path
  * --group / --lecture filters narrow the iteration correctly
  * the per-lecture content-collection function handles missing artifacts

External services (Gemini, Qdrant, Google Drive) are mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import regenerate_qdrant_vectors as regen  # noqa: E402 — sys.path setup above must run first


# ---------------------------------------------------------------------------
# _iter_target_lectures — CLI filter logic
# ---------------------------------------------------------------------------


class TestIterTargetLectures:
    def test_all_groups_all_lectures(self, monkeypatch):
        fake_groups = {3: {}, 4: {}}
        monkeypatch.setattr(
            "tools.core.config.GROUPS", fake_groups, raising=False
        )
        monkeypatch.setattr(
            "tools.core.config.TOTAL_LECTURES", 15, raising=False
        )
        pairs = regen._iter_target_lectures(only_group=None, only_lecture=None)
        # 2 groups × 15 lectures = 30 pairs
        assert len(pairs) == 30
        assert (3, 1) in pairs
        assert (4, 15) in pairs

    def test_single_group(self, monkeypatch):
        fake_groups = {3: {}, 4: {}}
        monkeypatch.setattr(
            "tools.core.config.GROUPS", fake_groups, raising=False
        )
        monkeypatch.setattr(
            "tools.core.config.TOTAL_LECTURES", 15, raising=False
        )
        pairs = regen._iter_target_lectures(only_group=4, only_lecture=None)
        assert all(g == 4 for g, _ in pairs)
        assert len(pairs) == 15

    def test_single_lecture(self, monkeypatch):
        fake_groups = {3: {}, 4: {}}
        monkeypatch.setattr(
            "tools.core.config.GROUPS", fake_groups, raising=False
        )
        monkeypatch.setattr(
            "tools.core.config.TOTAL_LECTURES", 15, raising=False
        )
        pairs = regen._iter_target_lectures(only_group=None, only_lecture=5)
        assert pairs == [(3, 5), (4, 5)]

    def test_single_group_and_lecture(self, monkeypatch):
        fake_groups = {3: {}, 4: {}}
        monkeypatch.setattr(
            "tools.core.config.GROUPS", fake_groups, raising=False
        )
        monkeypatch.setattr(
            "tools.core.config.TOTAL_LECTURES", 15, raising=False
        )
        pairs = regen._iter_target_lectures(only_group=3, only_lecture=7)
        assert pairs == [(3, 7)]

    def test_unknown_group_returns_empty(self, monkeypatch):
        fake_groups = {3: {}}
        monkeypatch.setattr(
            "tools.core.config.GROUPS", fake_groups, raising=False
        )
        monkeypatch.setattr(
            "tools.core.config.TOTAL_LECTURES", 15, raising=False
        )
        pairs = regen._iter_target_lectures(only_group=99, only_lecture=None)
        assert pairs == []


# ---------------------------------------------------------------------------
# regenerate_lecture — dry-run path never calls index_lecture_content
# ---------------------------------------------------------------------------


class TestRegenerateLecture:
    def test_dry_run_skips_indexing(self, monkeypatch):
        monkeypatch.setattr(
            regen,
            "collect_lecture_content",
            lambda g, lec: {"summary": "x" * 500},
        )

        with patch(
            "tools.integrations.knowledge_indexer.index_lecture_content"
        ) as mock_index:
            stats = regen.regenerate_lecture(3, 5, dry_run=True)

        assert mock_index.call_count == 0
        assert stats.content_types_processed == 1
        # Dry-run still reports chunk counts so the operator can see scale.
        assert stats.vectors_uploaded >= 1
        assert stats.skipped is False

    def test_skipped_when_no_content(self, monkeypatch):
        monkeypatch.setattr(
            regen, "collect_lecture_content", lambda g, lec: {}
        )
        stats = regen.regenerate_lecture(3, 5, dry_run=True)
        assert stats.skipped is True
        assert "no content" in (stats.error or "")
        assert stats.vectors_uploaded == 0

    def test_live_run_calls_indexer(self, monkeypatch):
        monkeypatch.setattr(
            regen,
            "collect_lecture_content",
            lambda g, lec: {"summary": "x" * 200},
        )

        with patch(
            "tools.integrations.knowledge_indexer.index_lecture_content",
            return_value=3,
        ) as mock_index:
            stats = regen.regenerate_lecture(3, 5, dry_run=False)

        mock_index.assert_called_once()
        kwargs = mock_index.call_args.kwargs
        assert kwargs["group_number"] == 3
        assert kwargs["lecture_number"] == 5
        assert kwargs["content_type"] == "summary"
        assert stats.vectors_uploaded == 3

    def test_content_type_filter_narrows_uploads(self, monkeypatch):
        monkeypatch.setattr(
            regen,
            "collect_lecture_content",
            lambda g, lec: {
                "summary": "summary text",
                "deep_analysis": "deep analysis text",
                "gap_analysis": "gap text",
            },
        )

        called_types: list[str] = []

        def _fake_index(*, content_type, **_kw):
            called_types.append(content_type)
            return 1

        with patch(
            "tools.integrations.knowledge_indexer.index_lecture_content",
            side_effect=_fake_index,
        ):
            regen.regenerate_lecture(
                3, 5, content_types=("summary",), dry_run=False
            )

        assert called_types == ["summary"]


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def test_estimate_cost_is_proportional_to_chars():
    # 1M chars × $0.025 / 1M = $0.025
    assert regen._estimate_cost(1_000_000) == pytest.approx(0.025)
    # 2M chars should be twice as expensive
    assert regen._estimate_cost(2_000_000) == pytest.approx(0.050)
    # Zero chars → zero cost
    assert regen._estimate_cost(0) == 0.0


# ---------------------------------------------------------------------------
# CLI smoke test — main() runs without crashing under --dry-run
# ---------------------------------------------------------------------------


def test_main_dry_run_smoke(monkeypatch, capsys):
    """main() with --dry-run must complete without hitting any external API."""
    fake_groups = {3: {"drive_folder_id": "x", "analysis_folder_id": "y"}}
    monkeypatch.setattr(
        "tools.core.config.GROUPS", fake_groups, raising=False
    )
    monkeypatch.setattr(
        "tools.core.config.TOTAL_LECTURES", 1, raising=False
    )

    # collect_lecture_content is the I/O surface — stub it so no real
    # Drive calls happen.
    monkeypatch.setattr(
        regen, "collect_lecture_content", lambda g, lec: {"summary": "x" * 100}
    )

    monkeypatch.setattr(sys, "argv", ["regen", "--dry-run", "--group", "3"])
    exit_code = regen.main()
    assert exit_code == 0


def test_main_returns_error_on_failure(monkeypatch):
    """main() returns non-zero when at least one lecture fails."""
    fake_groups = {3: {"drive_folder_id": "x"}}
    monkeypatch.setattr(
        "tools.core.config.GROUPS", fake_groups, raising=False
    )
    monkeypatch.setattr(
        "tools.core.config.TOTAL_LECTURES", 1, raising=False
    )
    monkeypatch.setattr(
        regen, "collect_lecture_content", lambda g, lec: {"summary": "x" * 100}
    )

    with patch(
        "tools.integrations.knowledge_indexer.index_lecture_content",
        side_effect=RuntimeError("simulated Qdrant outage"),
    ):
        monkeypatch.setattr(sys, "argv", ["regen", "--group", "3"])
        exit_code = regen.main()

    assert exit_code != 0
