"""Regression tests for multi-cohort support in analytics.py.

Verifies that the five previously-hardcoded [1, 2] / {1: g1, 2: g2} sites
in analytics.py now iterate ALL configured GROUPS, not just Groups 1 and 2.

Run with:
    python -m pytest tools/tests/test_analytics_multi_cohort.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Ensure project root is on sys.path (mirrors conftest.py approach)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Minimal GROUPS fixture — 4 groups including two course-completed ones.
# We import GROUPS from config and then monkeypatch analytics.GROUPS in tests.
# ---------------------------------------------------------------------------

_MOCK_GROUPS: dict = {
    1: {
        "name": "მარტის ჯგუფი #1",
        "folder_name": "AI კურსი (მარტის ჯგუფი #1. 2026)",
        "drive_folder_id": "fake_drive_1",
        "analysis_folder_id": "fake_analysis_1",
        "zoom_meeting_id": "1111",
        "meeting_days": [1, 4],
        "start_date": date(2026, 3, 13),
        "attendee_emails": [],
        "whatsapp_chat_id": "120363001@g.us",
        "course_completed": True,
    },
    2: {
        "name": "მარტის ჯგუფი #2",
        "folder_name": "AI კურსი (მარტის ჯგუფი #2. 2026)",
        "drive_folder_id": "fake_drive_2",
        "analysis_folder_id": "fake_analysis_2",
        "zoom_meeting_id": "2222",
        "meeting_days": [0, 3],
        "start_date": date(2026, 3, 12),
        "attendee_emails": [],
        "whatsapp_chat_id": "120363002@g.us",
        "course_completed": True,
    },
    3: {
        "name": "მაისის ჯგუფი #3",
        "folder_name": "AI კურსი (მაისის ჯგუფი #3. 2026)",
        "drive_folder_id": "fake_drive_3",
        "analysis_folder_id": "fake_analysis_3",
        "zoom_meeting_id": "3333",
        "meeting_days": [2, 5],
        "start_date": date(2026, 5, 13),
        "attendee_emails": [],
        "whatsapp_chat_id": "120363003@g.us",
        "course_completed": False,
    },
    4: {
        "name": "მაისის ჯგუფი #4",
        "folder_name": "AI კურსი (მაისის ჯგუფი #4. 2026)",
        "drive_folder_id": "fake_drive_4",
        "analysis_folder_id": "fake_analysis_4",
        "zoom_meeting_id": "4444",
        "meeting_days": [1, 4],
        "start_date": date(2026, 5, 14),
        "attendee_emails": [],
        "whatsapp_chat_id": "120363004@g.us",
        "course_completed": False,
    },
}


def _make_empty_group_data(group_number: int) -> dict:
    """Return a minimal _build_group_data-shaped dict for the given group."""
    from tools.services.analytics import DIMENSIONS, calculate_statistics

    empty_stats = {d: calculate_statistics([]) for d in DIMENSIONS + ["composite"]}
    return {
        "lecture_count": 0,
        "scores": [],
        "stats": empty_stats,
        "best_lecture": {"number": 0, "composite": 0},
        "worst_lecture": {"number": 0, "composite": 0},
        "composite_series": [],
        "lecture_labels": [],
        "dimension_series": {d: [] for d in DIMENSIONS},
        "heatmap": [],
        "strengths": [],
        "weaknesses": [],
        "consistency": 0.0,
        "pedagogy_score": 0.0,
        "content_quality": 0.0,
        "impact_score": 0.0,
        "balance_score": 0.0,
        "target_gap": 0.0,
        "insights": [],
        "kirkpatrick": {"L1_reaction": 0, "L2_learning": 0, "L3_behavior": 0, "L4_results": 0},
        "velocity": 0.0,
        "velocity_label": "სტაბილური",
        "volatility": {d: 0.0 for d in DIMENSIONS},
        "recommendation_followthrough": 0,
        "theory_practice_ratio": None,
        "benchmark_gap": 0.0,
        "benchmark_percentile": 1,
        "at_risk_dims": [],
        "lectures_to_target": None,
    }


# ===========================================================================
# Test 1: get_dashboard_data includes ALL configured groups
# ===========================================================================

class TestGetDashboardDataMultiCohort:
    """get_dashboard_data() must build data for every group in GROUPS."""

    def test_includes_all_configured_groups(self, monkeypatch):
        """groups dict returned by get_dashboard_data has keys for all groups."""
        import tools.services.analytics as analytics_mod

        # Patch GROUPS in the analytics module
        monkeypatch.setattr(analytics_mod, "GROUPS", _MOCK_GROUPS)

        # Patch _build_group_data to return a sentinel per group without DB access
        def fake_build(group_number: int) -> dict:
            d = _make_empty_group_data(group_number)
            d["_group_number"] = group_number  # sentinel for assertion
            return d

        with patch.object(analytics_mod, "_build_group_data", side_effect=fake_build):
            result = analytics_mod.get_dashboard_data()

        assert set(result["groups"].keys()) == {1, 2, 3, 4}, (
            f"Expected keys {{1,2,3,4}}, got {set(result['groups'].keys())}"
        )

    def test_total_processed_sums_all_groups(self, monkeypatch):
        """total_processed is the sum of lecture_count across ALL groups."""
        import tools.services.analytics as analytics_mod

        monkeypatch.setattr(analytics_mod, "GROUPS", _MOCK_GROUPS)

        call_counter = {"n": 0}

        def fake_build(group_number: int) -> dict:
            call_counter["n"] += 1
            d = _make_empty_group_data(group_number)
            d["lecture_count"] = group_number  # G1→1, G2→2, G3→3, G4→4
            return d

        with patch.object(analytics_mod, "_build_group_data", side_effect=fake_build):
            result = analytics_mod.get_dashboard_data()

        assert call_counter["n"] == 4, (
            f"_build_group_data should be called 4 times, was called {call_counter['n']}"
        )
        assert result["total_processed"] == 1 + 2 + 3 + 4, (
            f"total_processed should be 10, got {result['total_processed']}"
        )

    def test_groups_key_is_full_dict_not_hardcoded_two(self, monkeypatch):
        """Regression: 'groups' value must not be the old {1: g1, 2: g2} literal."""
        import tools.services.analytics as analytics_mod

        monkeypatch.setattr(analytics_mod, "GROUPS", _MOCK_GROUPS)

        def fake_build(group_number: int) -> dict:
            return _make_empty_group_data(group_number)

        with patch.object(analytics_mod, "_build_group_data", side_effect=fake_build):
            result = analytics_mod.get_dashboard_data()

        # Must include G3 and G4 — the previously missing cohorts
        assert 3 in result["groups"], "Group 3 missing from dashboard data"
        assert 4 in result["groups"], "Group 4 missing from dashboard data"


# ===========================================================================
# Test 2: sync_from_pinecone iterates ALL groups
# ===========================================================================

class TestSyncFromPineconeMultiCohort:
    """sync_from_pinecone() must iterate all GROUPS.keys(), not just [1, 2].

    get_pinecone_index is a local import inside sync_from_pinecone, so we
    patch it at its source module (tools.integrations.knowledge_indexer).
    """

    def test_iterates_all_groups(self, monkeypatch):
        """Pinecone list() is called for each group, not just groups 1 and 2."""
        import tools.services.analytics as analytics_mod

        three_groups = {1: _MOCK_GROUPS[1], 2: _MOCK_GROUPS[2], 3: _MOCK_GROUPS[3]}
        monkeypatch.setattr(analytics_mod, "GROUPS", three_groups)

        # Mock the Pinecone index
        mock_idx = MagicMock()
        mock_idx.list.return_value = iter([])  # empty pages → no vectors to fetch

        # Mock DB to return no existing lectures → all 3×15 = 45 combos are tried
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("tools.integrations.knowledge_indexer.get_pinecone_index", return_value=mock_idx), \
             patch.object(analytics_mod, "_get_conn", return_value=mock_conn):
            analytics_mod.sync_from_pinecone(force=True)

        # With 3 groups × 15 lectures = 45 list() calls (none skipped since DB is empty)
        assert mock_idx.list.call_count == 3 * 15, (
            f"Expected 45 Pinecone list() calls (3 groups × 15 lectures), "
            f"got {mock_idx.list.call_count}"
        )

    def test_does_not_stop_at_group_2(self, monkeypatch):
        """Regression: with 4 groups, sync_from_pinecone must iterate all 4.

        We patch analytics.GROUPS to 4 entries and verify that idx.list()
        is called 4*15=60 times (not 2*15=30 which was the old bug).

        Note: get_pinecone_index is a local import inside sync_from_pinecone,
        so we patch it at the knowledge_indexer source module. We also need
        the module to be freshly imported so the local-import binding is live.
        """
        import tools.services.analytics as analytics_mod

        # Force the four-group mapping directly on the module's GROUPS dict
        # (monkeypatch replaces the binding; the sorted() call inside
        #  sync_from_pinecone reads from analytics_mod.GROUPS)
        four_groups = {k: _MOCK_GROUPS[k] for k in (1, 2, 3, 4)}
        monkeypatch.setattr(analytics_mod, "GROUPS", four_groups)

        mock_idx = MagicMock()
        mock_idx.list.return_value = iter([])

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("tools.integrations.knowledge_indexer.get_pinecone_index", return_value=mock_idx), \
             patch.object(analytics_mod, "_get_conn", return_value=mock_conn), \
             patch.object(analytics_mod, "get_scores_for_lecture", return_value=None), \
             patch.object(analytics_mod, "get_lecture_insights", return_value=None):
            analytics_mod.sync_from_pinecone(force=True)

        # 4 groups × 15 lectures = 60 list() calls
        # Old bug: hardcoded [1,2] → max 30 calls
        assert mock_idx.list.call_count == 4 * 15, (
            f"Expected 60 Pinecone list() calls (4 groups × 15 lectures), "
            f"got {mock_idx.list.call_count}. "
            f"If 30, Groups 3+4 are still skipped by the old [1,2] literal."
        )


# ===========================================================================
# Test 3: _build_insights_html iterates all groups
# ===========================================================================

class TestBuildInsightsHtmlMultiCohort:
    """_build_insights_html must collect insights from all groups in dashboard_data."""

    def _make_dashboard_with_insights(self, group_keys: list[int]) -> dict:
        """Build a minimal dashboard_data dict with one insight per group."""
        groups = {}
        for gn in group_keys:
            gd = _make_empty_group_data(gn)
            gd["insights"] = [{
                "lecture_number": 1,
                "strengths_count": gn,
                "weaknesses_count": 0,
                "gaps_count": 0,
                "blind_spots_count": 0,
                "tech_correct_count": 0,
                "tech_problematic_count": 0,
                "top_strength": f"ძლიერი მხარე — ჯგუფი {gn}",
                "top_weakness": None,
                "score_justifications": None,
            }]
            groups[gn] = gd

        from tools.services.analytics import DIMENSION_LABELS_KA, DIMENSION_LABELS_EN
        return {
            "generated_at": "2026-01-01 00:00 UTC",
            "total_processed": len(group_keys),
            "groups": groups,
            "cross_group": {},
            "dimension_labels_ka": DIMENSION_LABELS_KA,
            "dimension_labels_en": DIMENSION_LABELS_EN,
            "total_lectures": 15,
            "trainer_performance_index": None,
            "dimension_rankings": [],
            "cross_pedagogy": 0,
            "cross_content_quality": 0,
            "cross_impact": 0,
            "cross_balance": 0,
            "cross_velocity": 0,
            "cross_vel_label": "სტაბილური",
            "cross_ltt": None,
            "cross_rec_ft": 0,
            "cross_at_risk": [],
            "cross_kirkpatrick": {"L1_reaction": 0, "L2_learning": 0, "L3_behavior": 0, "L4_results": 0},
            "cross_bench_gap": 0,
            "cross_tp_ratio": None,
        }

    def test_all_groups_insights_collected(self, monkeypatch):
        """Insights from groups 3 and 4 must appear in the rendered HTML."""
        import tools.services.analytics as analytics_mod

        monkeypatch.setattr(analytics_mod, "GROUPS", _MOCK_GROUPS)

        # We need render_dashboard_html to call _build_insights_html internally.
        # Instead test the iteration directly by inspecting what _build_insights_html
        # produces when given a 4-group dashboard_data.
        dashboard_data = self._make_dashboard_with_insights([1, 2, 3, 4])

        # _build_insights_html is a closure inside render_dashboard_html.
        # We call render_dashboard_html with our test data and check the output.
        with patch.object(analytics_mod, "GROUPS", _MOCK_GROUPS):
            html = analytics_mod.render_dashboard_html(dashboard_data)

        # Each group's top_strength text should appear in the HTML
        assert "ძლიერი მხარე — ჯგუფი 3" in html, \
            "Group 3 insights not found in rendered HTML — _build_insights_html skipped G3"
        assert "ძლიერი მხარე — ჯგუფი 4" in html, \
            "Group 4 insights not found in rendered HTML — _build_insights_html skipped G4"

    def test_two_group_case_still_works(self, monkeypatch):
        """Backward-compat: 2-group dashboard still renders correctly."""
        import tools.services.analytics as analytics_mod

        two_groups = {1: _MOCK_GROUPS[1], 2: _MOCK_GROUPS[2]}
        monkeypatch.setattr(analytics_mod, "GROUPS", two_groups)

        dashboard_data = self._make_dashboard_with_insights([1, 2])

        html = analytics_mod.render_dashboard_html(dashboard_data)

        assert "ძლიერი მხარე — ჯგუფი 1" in html
        assert "ძლიერი მხარე — ჯგუფი 2" in html


# ===========================================================================
# Test 4: Dashboard JS emits configured group list (not hardcoded [1,2])
# ===========================================================================

class TestDashboardJsGroupList:
    """render_dashboard_html must emit the actual GROUPS keys in the JS."""

    def _minimal_dashboard(self, group_keys: list[int]) -> dict:
        return TestBuildInsightsHtmlMultiCohort()._make_dashboard_with_insights(group_keys)

    def test_js_contains_all_four_groups(self, monkeypatch):
        """When GROUPS has 4 entries, emitted JS must reference all four."""
        import tools.services.analytics as analytics_mod

        monkeypatch.setattr(analytics_mod, "GROUPS", _MOCK_GROUPS)

        dashboard_data = self._minimal_dashboard([1, 2, 3, 4])

        with patch.object(analytics_mod, "GROUPS", _MOCK_GROUPS):
            html = analytics_mod.render_dashboard_html(dashboard_data)

        # COHORT_GROUPS JS var must contain all 4 group numbers
        assert "COHORT_GROUPS" in html, "COHORT_GROUPS variable not emitted in JS"
        assert "1, 2, 3, 4" in html or "[1, 2, 3, 4]" in html, (
            "JS COHORT_GROUPS does not contain all four group numbers.\n"
            "Snippet: " + html[html.find("COHORT_GROUPS"):html.find("COHORT_GROUPS") + 80]
        )

    def test_js_does_not_hardcode_1_2(self, monkeypatch):
        """Regression: the old [1,2].forEach must not appear in the HTML output."""
        import tools.services.analytics as analytics_mod

        monkeypatch.setattr(analytics_mod, "GROUPS", _MOCK_GROUPS)

        dashboard_data = self._minimal_dashboard([1, 2, 3, 4])

        with patch.object(analytics_mod, "GROUPS", _MOCK_GROUPS):
            html = analytics_mod.render_dashboard_html(dashboard_data)

        assert "[1,2].forEach" not in html, \
            "Old hardcoded [1,2].forEach still present in rendered HTML"

    def test_cohort_names_injected_in_js(self, monkeypatch):
        """COHORT_NAMES must include an entry for group key 3.

        json.dumps uses ASCII escapes by default, so Georgian characters
        appear as \\uXXXX sequences. We check for the JSON key "3" inside
        the COHORT_NAMES block rather than the raw Georgian string.
        """
        import tools.services.analytics as analytics_mod

        monkeypatch.setattr(analytics_mod, "GROUPS", _MOCK_GROUPS)

        dashboard_data = self._minimal_dashboard([1, 2, 3, 4])

        with patch.object(analytics_mod, "GROUPS", _MOCK_GROUPS):
            html = analytics_mod.render_dashboard_html(dashboard_data)

        assert "COHORT_NAMES" in html, "COHORT_NAMES variable not emitted in JS"

        # Extract the COHORT_NAMES JSON literal from the HTML
        start = html.find("COHORT_NAMES")
        snippet = html[start:start + 300]

        # The JSON object must contain key "3" (group 3 must be present)
        # json.dumps({3: "..."}) produces {"3": "..."} in JSON
        assert '"3"' in snippet, (
            f"Group 3 key not found in COHORT_NAMES JS snippet:\n{snippet}"
        )
        # And the Group 3 name (may be ASCII-escaped) must be non-empty
        # Easiest: parse the JS variable back from the HTML
        import re
        m = re.search(r"var COHORT_NAMES\s*=\s*(\{[^;]+\});", html)
        assert m, "Could not find COHORT_NAMES = {...}; in HTML"
        cohort_names = json.loads(m.group(1))
        assert "3" in cohort_names, f"Key '3' missing from COHORT_NAMES: {cohort_names}"
        assert cohort_names["3"], f"Empty name for group 3 in COHORT_NAMES: {cohort_names}"

    def test_dot_css_generated_for_all_groups(self, monkeypatch):
        """Dynamic .gN-dot CSS must be emitted for groups 3 and 4."""
        import tools.services.analytics as analytics_mod

        monkeypatch.setattr(analytics_mod, "GROUPS", _MOCK_GROUPS)

        dashboard_data = self._minimal_dashboard([1, 2, 3, 4])

        with patch.object(analytics_mod, "GROUPS", _MOCK_GROUPS):
            html = analytics_mod.render_dashboard_html(dashboard_data)

        assert ".g3-dot" in html, ".g3-dot CSS not generated for Group 3"
        assert ".g4-dot" in html, ".g4-dot CSS not generated for Group 4"
        # Old hardcoded ones must still be there (via dynamic generation)
        assert ".g1-dot" in html, ".g1-dot CSS missing"
        assert ".g2-dot" in html, ".g2-dot CSS missing"
