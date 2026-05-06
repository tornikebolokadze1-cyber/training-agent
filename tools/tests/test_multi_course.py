"""Contract tests for the multi-course refactor of config.py and scheduler.py.

REFACTOR CONTRACT BEING VERIFIED
=================================
This file pins the behaviour that the concurrent edits to
``tools/core/config.py`` and ``tools/app/scheduler.py`` must satisfy.
Running the suite NOW (before the refactor lands) produces a mix of
xfail and skip results.  After the refactor, every test must turn green.

Key changes under test
-----------------------
1. ``GroupConfig`` gains a new optional ``course_completed: bool`` field
   (default ``False`` if absent — backward-compatible).  Groups 1 and 2
   will be set to ``course_completed=True`` once the course finishes.

2. ``tools.core.config`` exports two new helpers:
   - ``iter_active_groups() -> Iterator[tuple[int, GroupConfig]]``
     Yields only entries where ``course_completed`` is False or absent.
   - ``iter_all_groups() -> Iterator[tuple[int, GroupConfig]]``
     Yields every entry regardless of flag.

3. ``start_scheduler()`` in ``tools.app.scheduler`` iterates
   ``iter_active_groups()`` and registers pre-meeting cron jobs
   *dynamically*, deriving the cron ``day_of_week`` string from each
   group's ``meeting_days`` list.  The hardcoded
   ``pre_group1_tuesday`` / ``pre_group1_friday`` / ``pre_group2_monday``
   / ``pre_group2_thursday`` calls are gone.

4. Job ID convention for dynamically-registered jobs:
   ``pre_group{N}_{three_letter_weekday}``
   e.g. Group 3 with ``meeting_days=[2, 5]`` → ``pre_group3_wed`` and
   ``pre_group3_sat``.

5. ``is_course_completed()`` still works as the global env-var gate AND
   now also returns ``True`` when every entry in ``GROUPS`` carries
   ``course_completed=True``.

Skipping strategy
-----------------
- Tests for functions that do not yet exist use a ``try/except ImportError``
  guard and call ``pytest.skip()`` so the file commits cleanly.
- Tests for config behaviour that requires the dict edit use
  ``@pytest.mark.xfail(reason="config edit pending", strict=False)``
  so they are informative now and flip to PASSED once the edit lands.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import tools.app.scheduler as sched

# ---------------------------------------------------------------------------
# Helper: build a minimal GroupConfig-compatible dict for test injection.
# ``course_completed`` defaults to False to match the new back-compat rule.
# ---------------------------------------------------------------------------
_BASE_GROUP: dict[str, Any] = {
    "name": "Test Group",
    "folder_name": "Test Folder",
    "drive_folder_id": "drive-id",
    "analysis_folder_id": "analysis-id",
    "zoom_meeting_id": "zoom-id",
    "start_date": date(2026, 1, 1),
    "attendee_emails": [],
    # course_completed intentionally omitted here — back-compat default test
}


def _make_group(meeting_days: list[int], course_completed: bool | None = None) -> dict:
    """Return a GroupConfig-compatible dict with the given schedule and flag."""
    g = {**_BASE_GROUP, "meeting_days": meeting_days}
    if course_completed is not None:
        g["course_completed"] = course_completed
    return g


# ---------------------------------------------------------------------------
# Attempt to import the new helpers; skip if they don't exist yet.
# ---------------------------------------------------------------------------
try:
    from tools.core.config import iter_active_groups, iter_all_groups  # type: ignore[attr-defined]
    _HELPERS_AVAILABLE = True
except ImportError:
    iter_active_groups = None  # type: ignore[assignment]
    iter_all_groups = None  # type: ignore[assignment]
    _HELPERS_AVAILABLE = False

_SKIP_HELPERS = not _HELPERS_AVAILABLE


# ===========================================================================
# 1. TestIterActiveGroups
# ===========================================================================


class TestIterActiveGroups:
    """iter_active_groups() must yield only non-completed groups."""

    def _require_helpers(self):
        if _SKIP_HELPERS:
            pytest.skip("iter_active_groups / iter_all_groups not yet exported from config")

    def _patch_groups(self, monkeypatch, groups: dict) -> None:
        """Replace GROUPS in config *and* in scheduler's imported reference."""
        import tools.core.config as cfg
        monkeypatch.setattr(cfg, "GROUPS", groups)
        # scheduler imports GROUPS at module level; patch there too
        monkeypatch.setattr(sched, "GROUPS", groups)

    # ------------------------------------------------------------------
    def test_returns_groups_with_course_completed_false(self, monkeypatch):
        """Groups explicitly set to course_completed=False are yielded."""
        self._require_helpers()
        groups = {
            1: _make_group([1, 4], course_completed=False),
            2: _make_group([0, 3], course_completed=False),
        }
        self._patch_groups(monkeypatch, groups)

        result = dict(iter_active_groups())
        assert 1 in result
        assert 2 in result

    def test_skips_groups_with_course_completed_true(self, monkeypatch):
        """Groups with course_completed=True must NOT appear in iter_active_groups."""
        self._require_helpers()
        groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=False),
        }
        self._patch_groups(monkeypatch, groups)

        result = dict(iter_active_groups())
        assert 1 not in result, "Completed group 1 must be excluded"
        assert 2 in result, "Active group 2 must be included"

    def test_treats_missing_field_as_active(self, monkeypatch):
        """A GroupConfig without course_completed key must be treated as active (back-compat)."""
        self._require_helpers()
        groups = {
            99: _make_group([2], course_completed=None),  # key absent
        }
        self._patch_groups(monkeypatch, groups)

        result = dict(iter_active_groups())
        assert 99 in result, (
            "Group missing course_completed field must appear in active groups "
            "(backward-compat requirement)"
        )

    def test_iter_all_groups_returns_everything(self, monkeypatch):
        """iter_all_groups must yield every entry regardless of course_completed."""
        self._require_helpers()
        groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=False),
            3: _make_group([2], course_completed=None),  # missing field
        }
        self._patch_groups(monkeypatch, groups)

        result = dict(iter_all_groups())
        assert set(result.keys()) == {1, 2, 3}, (
            "iter_all_groups must return all group IDs including completed ones"
        )


# ===========================================================================
# 2. TestSchedulerDynamicRegistration
# ===========================================================================

# Mapping weekday int → expected three-letter lowercase token used in job IDs.
# Monday=0 follows Python's date.weekday() convention which matches
# APScheduler's CronTrigger day_of_week numbering.
_WEEKDAY_NAMES = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}

# Support jobs that must always be registered regardless of course state.
_ADVISOR_SUPPORT_JOBS = {
    "pinecone_score_backup",
    "google_token_health",
    "proactive_token_check",
    "nightly_reconciliation",
    "whatsapp_archive_catchup",
}


def _run_start_scheduler(monkeypatch, groups: dict) -> tuple[MagicMock, set[str]]:
    """Run start_scheduler() with mocked GROUPS and APScheduler.

    Returns (mock_scheduler_instance, set_of_registered_job_ids).
    Pattern mirrors TestStartScheduler in test_scheduler.py.
    """
    # Replace GROUPS in every module that holds a reference
    import tools.core.config as cfg
    monkeypatch.setattr(cfg, "GROUPS", groups)
    monkeypatch.setattr(sched, "GROUPS", groups)

    # Ensure COURSE_COMPLETED env var is unset so global flag doesn't override
    monkeypatch.delenv("COURSE_COMPLETED", raising=False)

    mock_scheduler_instance = MagicMock()
    mock_scheduler_instance.get_jobs.return_value = []

    with patch("tools.app.scheduler.AsyncIOScheduler", return_value=mock_scheduler_instance):
        sched.start_scheduler()

    job_ids: set[str] = set()
    for c in mock_scheduler_instance.add_job.call_args_list:
        # add_job is always called with id= as a keyword argument
        jid = c[1].get("id") or (c[0][1] if len(c[0]) > 1 else None)
        if jid:
            job_ids.add(jid)

    return mock_scheduler_instance, job_ids


class TestSchedulerDynamicRegistration:
    """After the refactor, start_scheduler must build job IDs from group data."""

    # ------------------------------------------------------------------
    def test_no_lecture_jobs_when_all_courses_completed(self, monkeypatch):
        """When every group has course_completed=True, no pre_group* jobs appear."""
        groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=True),
        }
        _, job_ids = _run_start_scheduler(monkeypatch, groups)

        pre_lecture_ids = {j for j in job_ids if j.startswith("pre_group")}
        assert pre_lecture_ids == set(), (
            f"Expected no pre_group* jobs when all courses completed, "
            f"but found: {pre_lecture_ids}"
        )

    def test_registers_jobs_per_active_group_meeting_day(self, monkeypatch):
        """A Group 3 with meeting_days=[2, 5] must produce pre_group3_wed and pre_group3_sat."""
        # Group 3 is active; groups 1 and 2 are complete so they don't clutter the check.
        groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=True),
            3: _make_group([2, 5], course_completed=False),  # Wednesday=2, Saturday=5
        }
        _, job_ids = _run_start_scheduler(monkeypatch, groups)

        assert "pre_group3_wed" in job_ids, (
            f"Expected pre_group3_wed in job IDs, got: {job_ids}"
        )
        assert "pre_group3_sat" in job_ids, (
            f"Expected pre_group3_sat in job IDs, got: {job_ids}"
        )
        # Groups 1 and 2 are complete — no pre_group1/2 jobs expected
        assert "pre_group1_tuesday" not in job_ids
        assert "pre_group2_monday" not in job_ids

    def test_advisor_supporting_jobs_always_registered(self, monkeypatch):
        """Advisor support jobs must be registered regardless of course_completed state."""
        # All groups completed — only advisor jobs should remain
        groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=True),
        }
        _, job_ids = _run_start_scheduler(monkeypatch, groups)

        # We check the subset of advisor jobs that are directly wired in
        # start_scheduler (not via register_* helpers which may not be present)
        minimum_expected = {
            "pinecone_score_backup",
            "google_token_health",
        }
        missing = minimum_expected - job_ids
        assert not missing, (
            f"Advisor-support jobs missing when all courses complete: {missing}\n"
            f"Registered jobs: {job_ids}"
        )

    def test_drive_pinecone_audit_skipped_when_no_active_courses(self, monkeypatch):
        """drive_pinecone_audit must NOT appear when all courses are completed."""
        groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=True),
        }
        _, job_ids = _run_start_scheduler(monkeypatch, groups)

        assert "drive_pinecone_audit" not in job_ids, (
            "drive_pinecone_audit should not run when no active courses exist"
        )

    def test_drive_pinecone_audit_present_when_course_active(self, monkeypatch):
        """drive_pinecone_audit must appear when at least one group is still active."""
        groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=False),  # still active
        }
        _, job_ids = _run_start_scheduler(monkeypatch, groups)

        assert "drive_pinecone_audit" in job_ids, (
            "drive_pinecone_audit must run while at least one group is active"
        )

    def test_nightly_catch_all_skipped_when_no_active_courses(self, monkeypatch):
        """nightly_catch_all is a lecture-only job; it must not fire post-course."""
        groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=True),
        }
        _, job_ids = _run_start_scheduler(monkeypatch, groups)

        assert "nightly_catch_all" not in job_ids, (
            "nightly_catch_all must be suppressed when all courses are completed"
        )

    def test_all_meeting_days_of_active_group_get_jobs(self, monkeypatch):
        """Every meeting day in an active group must produce exactly one pre_group job."""
        # Group with three meeting days to stress-test the loop
        groups = {
            5: _make_group([0, 2, 4], course_completed=False),  # Mon, Wed, Fri
        }
        _, job_ids = _run_start_scheduler(monkeypatch, groups)

        expected = {"pre_group5_mon", "pre_group5_wed", "pre_group5_fri"}
        missing = expected - job_ids
        assert not missing, (
            f"Missing dynamic job IDs for Group 5: {missing}\n"
            f"Registered: {job_ids}"
        )


# ===========================================================================
# 3. TestCourseCompletedFlag (back-compat)
# ===========================================================================


class TestCourseCompletedFlag:
    """Verify the updated is_course_completed() contract after the refactor."""

    # ------------------------------------------------------------------
    # Existing env-var behaviour must still work unchanged
    # ------------------------------------------------------------------
    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "YES"])
    def test_global_env_flag_truthy_values(self, monkeypatch, value):
        """COURSE_COMPLETED env var truthy values still return True."""
        monkeypatch.setenv("COURSE_COMPLETED", value)
        assert sched.is_course_completed() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_global_env_flag_falsy_values(self, monkeypatch, value):
        """COURSE_COMPLETED env var falsy values yield False as long as
        at least one group is still active. The aggregate branch of
        is_course_completed() returns True whenever every group in
        ``GROUPS`` is flagged completed, so we synthesize one active
        group here to isolate the env-flag behaviour."""
        from datetime import date
        active = {
            99: {
                "name": "synthetic active",
                "meeting_days": [1],
                "start_date": date(2026, 1, 1),
                "course_completed": False,
            }
        }
        monkeypatch.setattr(sched, "GROUPS", active)
        monkeypatch.setenv("COURSE_COMPLETED", value)
        assert sched.is_course_completed() is False

    def test_global_env_flag_unset_returns_false(self, monkeypatch):
        """Same isolation as above: with one active group and env unset,
        is_course_completed() must return False."""
        from datetime import date
        active = {
            99: {
                "name": "synthetic active",
                "meeting_days": [1],
                "start_date": date(2026, 1, 1),
                "course_completed": False,
            }
        }
        monkeypatch.setattr(sched, "GROUPS", active)
        monkeypatch.delenv("COURSE_COMPLETED", raising=False)
        assert sched.is_course_completed() is False

    # ------------------------------------------------------------------
    # New per-group completion behaviour (xfail until config edit lands)
    # ------------------------------------------------------------------
    @pytest.mark.xfail(
        reason="config edit pending: GROUPS[1]['course_completed'] not yet True",
        strict=False,  # non-strict: flips to xpass once the config edit lands (already happened)
    )
    def test_groups_1_and_2_marked_course_completed_in_config_after_refactor(self):
        """Groups 1 and 2 must carry course_completed=True in the live GROUPS dict."""
        from tools.core.config import GROUPS

        assert GROUPS[1].get("course_completed") is True, (
            "Group 1 must have course_completed=True after the config edit"
        )
        assert GROUPS[2].get("course_completed") is True, (
            "Group 2 must have course_completed=True after the config edit"
        )

    @pytest.mark.xfail(
        reason=(
            "is_course_completed() aggregate logic not yet implemented: "
            "currently checks only COURSE_COMPLETED env var, not per-group flags"
        ),
        strict=False,
    )
    def test_is_course_completed_returns_true_when_all_groups_flagged(self, monkeypatch):
        """is_course_completed() must return True when all GROUPS have course_completed=True.

        This supersedes the global env-var gate for the per-group case.
        """
        import tools.core.config as cfg

        monkeypatch.delenv("COURSE_COMPLETED", raising=False)
        patched_groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=True),
        }
        monkeypatch.setattr(cfg, "GROUPS", patched_groups)
        monkeypatch.setattr(sched, "GROUPS", patched_groups)

        assert sched.is_course_completed() is True, (
            "is_course_completed() must be True when every group has course_completed=True "
            "even without the global env flag"
        )

    @pytest.mark.xfail(
        reason=(
            "is_course_completed() aggregate logic not yet implemented: "
            "must return False when at least one group is still active"
        ),
        strict=False,  # non-strict: xpass once the per-group aggregation is implemented
    )
    def test_is_course_completed_returns_false_when_any_group_active(self, monkeypatch):
        """is_course_completed() must be False when any group lacks course_completed=True."""
        import tools.core.config as cfg

        monkeypatch.delenv("COURSE_COMPLETED", raising=False)
        patched_groups = {
            1: _make_group([1, 4], course_completed=True),
            2: _make_group([0, 3], course_completed=False),  # still active
        }
        monkeypatch.setattr(cfg, "GROUPS", patched_groups)
        monkeypatch.setattr(sched, "GROUPS", patched_groups)

        assert sched.is_course_completed() is False, (
            "is_course_completed() must remain False while any group is active"
        )


# ===========================================================================
# 4. TestGroupConfigBackwardCompat
# ===========================================================================


class TestGroupConfigBackwardCompat:
    """The new course_completed field must not break existing code that reads GROUPS."""

    def test_existing_required_fields_still_present(self):
        """All original GroupConfig keys must still exist on groups 1 and 2."""
        from tools.core.config import GROUPS

        required_keys = {
            "name", "folder_name", "drive_folder_id", "analysis_folder_id",
            "zoom_meeting_id", "meeting_days", "start_date", "attendee_emails",
        }
        for group_num in (1, 2):
            missing = required_keys - set(GROUPS[group_num].keys())
            assert not missing, (
                f"Group {group_num} is missing required keys after refactor: {missing}"
            )

    def test_meeting_days_are_valid_weekday_integers(self):
        """meeting_days must only contain integers in [0, 6]."""
        from tools.core.config import GROUPS

        for group_num, group in GROUPS.items():
            for day in group["meeting_days"]:
                assert isinstance(day, int), (
                    f"Group {group_num} meeting_days contains non-int: {day!r}"
                )
                assert 0 <= day <= 6, (
                    f"Group {group_num} meeting_days contains out-of-range day: {day}"
                )

    def test_weekday_names_map_covers_all_possible_days(self):
        """Internal _WEEKDAY_NAMES constant in this file covers Mon-Sun (0-6)."""
        assert set(_WEEKDAY_NAMES.keys()) == set(range(7))
        expected_names = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        assert set(_WEEKDAY_NAMES.values()) == expected_names
