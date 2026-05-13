"""Regression tests for US-003: dynamic GROUPS iteration in analytics.py.

Background:
    PR #37 (2026-05-12) claimed multi-cohort support, but ``tools/services/
    analytics.py`` retained five hard-pair ``[1, 2]`` sites. Commit
    ``fabb5fa`` fixed them on a side branch but was never merged. These
    tests pin the dynamic behaviour so the fix cannot silently regress.

Coverage:
    * ``sync_from_pinecone`` iterates every key in ``GROUPS``.
    * ``_build_insights_html`` (via the rendered dashboard HTML) surfaces
      every group from the dashboard data, not just 1 and 2.
    * The radar-chart JavaScript template injects ``sorted(GROUPS.keys())``
      as a literal array — not a hardcoded ``[1, 2]``.

All tests stub external services so they run without Pinecone / DB state.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tools.services import analytics


def _fake_group_cfg(name: str) -> dict[str, Any]:
    """Minimal GroupConfig-shaped dict for tests."""
    return {
        "name": name,
        "folder_name": name,
        "drive_folder_id": "drive-x",
        "analysis_folder_id": "drive-a",
        "zoom_meeting_id": "0",
        "meeting_days": [0],
        "start_date": None,
        "attendee_emails": [],
        "whatsapp_chat_id": "0@g.us",
        "course_completed": False,
    }


# ---------------------------------------------------------------------------
# sync_from_pinecone iterates every configured group
# ---------------------------------------------------------------------------

def test_sync_from_pinecone_iterates_all_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """sync_from_pinecone must visit every key in GROUPS, not just [1, 2]."""

    fake_groups = {
        3: _fake_group_cfg("მაისის ჯგუფი #1"),
        4: _fake_group_cfg("მაისის ჯგუფი #2"),
    }
    monkeypatch.setattr(analytics, "GROUPS", fake_groups)

    # Force the cooldown to elapse so the function actually runs.
    monkeypatch.setattr(analytics, "_last_sync_time", 0.0)

    # Stub Pinecone connection — list() returns nothing, so the inner loop
    # short-circuits but we still capture which (group, lecture) pairs were
    # *probed*. That's what we care about for the iteration test.
    probed_groups: set[int] = set()

    class _FakeIndex:
        def list(self, prefix: str, limit: int = 99):  # noqa: ARG002
            # Prefix format: g{group}_l{lecture}_deep_analysis_
            m = re.match(r"^g(\d+)_l\d+_deep_analysis_$", prefix)
            if m:
                probed_groups.add(int(m.group(1)))
            return iter([])

        def fetch(self, ids: list[str]):  # pragma: no cover - never reached when list is empty
            return type("F", (), {"vectors": {}})()

    # Patch the indexer import so we hit the fake.
    import tools.integrations.knowledge_indexer as ki

    monkeypatch.setattr(ki, "get_pinecone_index", lambda: _FakeIndex())

    # Patch _get_conn to return an empty existing set so every (group, lecture)
    # pair is treated as missing and therefore probed.
    class _FakeCursor:
        def execute(self, _sql: str):
            return self

        def fetchall(self):
            return []

    class _FakeConn:
        def execute(self, sql: str):
            return _FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    from contextlib import contextmanager

    @contextmanager
    def _fake_get_conn():
        yield _FakeConn()

    monkeypatch.setattr(analytics, "_get_conn", _fake_get_conn)

    # Also stub the G1L1 seed branch (it queries get_scores_for_lecture).
    monkeypatch.setattr(analytics, "get_scores_for_lecture", lambda *_a, **_kw: {"composite": 1.0})
    monkeypatch.setattr(analytics, "get_lecture_insights", lambda *_a, **_kw: {"strengths_count": 1})
    # And the fallback restore — return False so we don't try to write.
    monkeypatch.setattr(analytics, "_restore_from_score_backup", lambda *_a, **_kw: False)

    analytics.sync_from_pinecone(force=True)

    assert probed_groups == {3, 4}, (
        "sync_from_pinecone must probe every configured group; "
        f"probed only {sorted(probed_groups)}"
    )


# ---------------------------------------------------------------------------
# _build_insights_html (via render_dashboard_html) includes every group
# ---------------------------------------------------------------------------

def _minimal_group_block(group_number: int, lecture_number: int) -> dict[str, Any]:
    """Shape of the dict each ``data["groups"][N]`` must satisfy for rendering."""
    empty_stat = {
        "mean": 7.0,
        "median": 7.0,
        "std_dev": 0.0,
        "min": 7.0,
        "max": 7.0,
        "p25": 7.0,
        "p75": 7.0,
        "trend_slope": 0.0,
        "rolling_avg_3": 7.0,
        "improvement_rate": 0.0,
        "trend_label": "stable",
    }
    score_row = {
        "group_number": group_number,
        "lecture_number": lecture_number,
        "content_depth": 7.0,
        "practical_value": 7.0,
        "engagement": 7.0,
        "technical_accuracy": 7.0,
        "market_relevance": 7.0,
        "composite": 7.0,
        "overall_score": 7.0,
    }
    return {
        "lecture_count": 1,
        "scores": [score_row],
        "stats": {d: empty_stat for d in analytics.DIMENSIONS + ["composite"]},
        "best_lecture": score_row,
        "worst_lecture": score_row,
        "composite_series": [7.0],
        "lecture_labels": [f"ლექცია #{lecture_number}"],
        "dimension_series": {d: [7.0] for d in analytics.DIMENSIONS},
        "heatmap": [],
        "strengths": [],
        "weaknesses": [],
        "consistency": 9.5,
        "insights": [
            {
                "lecture_number": lecture_number,
                "strengths_count": 2,
                "weaknesses_count": 1,
                "gaps_count": 0,
                "recommendations_count": 0,
                "tech_correct_count": 0,
                "tech_problematic_count": 0,
                "blind_spots_count": 0,
                "top_strength": f"strength-for-group-{group_number}",
                "top_weakness": "",
                "key_recommendation": "",
                "score_justifications": None,
            },
        ],
    }


def _make_dashboard_data(group_ids: list[int]) -> dict[str, Any]:
    groups = {gn: _minimal_group_block(gn, 1) for gn in group_ids}
    return {
        "generated_at": "2026-05-13 22:00 UTC",
        "total_processed": len(group_ids),
        "groups": groups,
        "cross_group": {
            d: {"g1_mean": None, "g2_mean": None, "delta": None}
            for d in analytics.DIMENSIONS + ["composite"]
        },
        "dimension_labels_ka": analytics.DIMENSION_LABELS_KA,
        "dimension_labels_en": analytics.DIMENSION_LABELS_EN,
        "total_lectures": 15,
        "trainer_performance_index": 7.0,
        "dimension_rankings": [{"dim": d, "mean": 7.0} for d in analytics.DIMENSIONS],
        "cross_pedagogy": 7.0,
        "cross_content_quality": 7.0,
        "cross_impact": 7.0,
        "cross_balance": 9.0,
        "cross_kirkpatrick": {"L1_reaction": 7, "L2_learning": 7, "L3_behavior": 7, "L4_results": 7},
        "cross_bench_gap": 0.0,
        "cross_tp_ratio": 1.0,
        "cross_velocity": 0.0,
        "cross_vel_label": "სტაბილური",
        "cross_ltt": None,
        "cross_rec_ft": 0,
        "cross_at_risk": [],
    }


def test_build_insights_html_includes_all_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendered dashboard HTML must surface insights for every group in data."""
    fake_groups = {
        1: _fake_group_cfg("მარტის ჯგუფი #1"),
        2: _fake_group_cfg("მარტის ჯგუფი #2"),
        3: _fake_group_cfg("მაისის ჯგუფი #1"),
        4: _fake_group_cfg("მაისის ჯგუფი #2"),
    }
    monkeypatch.setattr(analytics, "GROUPS", fake_groups)

    data = _make_dashboard_data([1, 2, 3, 4])
    html = analytics.render_dashboard_html(data)

    # Each group's strength text must appear so we know its insights card was rendered.
    for gn in (1, 2, 3, 4):
        assert f"strength-for-group-{gn}" in html, (
            f"insights HTML missing group {gn} content"
        )

    # Each group must get a CSS dot class generated dynamically.
    for gn in (1, 2, 3, 4):
        assert f".g{gn}-dot" in html, f"CSS dot class g{gn}-dot missing"


# ---------------------------------------------------------------------------
# Radar-chart JavaScript emits a dynamic group list, not literal [1, 2]
# ---------------------------------------------------------------------------

def test_chart_js_emits_dynamic_group_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """The radar-chart template must inject sorted GROUPS keys as a literal."""
    fake_groups = {
        3: _fake_group_cfg("მაისის ჯგუფი #1"),
        4: _fake_group_cfg("მაისის ჯგუფი #2"),
        5: _fake_group_cfg("ივნისის ჯგუფი #1"),
    }
    monkeypatch.setattr(analytics, "GROUPS", fake_groups)

    data = _make_dashboard_data([3, 4, 5])
    html = analytics.render_dashboard_html(data)

    # Literal [1, 2] / [1,2] iteration must not appear in the rendered output
    # for the radar chart. (Other charts may retain TODO scaffolding.)
    assert "[1,2].forEach" not in html
    assert "[1, 2].forEach" not in html

    # The dynamic injection must be present.
    assert "COHORT_GROUPS" in html
    assert "COHORT_GROUPS.forEach" in html

    # The actual array literal must contain every group, in sorted order.
    m = re.search(r"var\s+COHORT_GROUPS\s*=\s*(\[[^\]]+\])", html)
    assert m, "COHORT_GROUPS array literal not found in rendered HTML"
    injected = m.group(1)
    assert "3" in injected and "4" in injected and "5" in injected, (
        f"COHORT_GROUPS literal should list 3, 4, 5; got {injected}"
    )


def test_get_dashboard_data_includes_all_configured_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_dashboard_data must build a block for every key in GROUPS."""
    fake_groups = {
        1: _fake_group_cfg("მარტის ჯგუფი #1"),
        2: _fake_group_cfg("მარტის ჯგუფი #2"),
        3: _fake_group_cfg("მაისის ჯგუფი #1"),
        4: _fake_group_cfg("მაისის ჯგუფი #2"),
    }
    monkeypatch.setattr(analytics, "GROUPS", fake_groups)

    # Stub _build_group_data so we can record which group numbers were asked for
    # without hitting SQLite.
    requested: list[int] = []

    def _fake_build(group_number: int) -> dict[str, Any]:
        requested.append(group_number)
        return _minimal_group_block(group_number, 1)

    monkeypatch.setattr(analytics, "_build_group_data", _fake_build)

    data = analytics.get_dashboard_data()

    assert sorted(data["groups"].keys()) == [1, 2, 3, 4]
    assert set(requested) >= {1, 2, 3, 4}
