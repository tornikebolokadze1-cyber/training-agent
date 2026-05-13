"""Regression tests for atomic + locked pending-jobs file operations.

US-011 — prevents JSON corruption when the scheduler tick and the
post-meeting pipeline (or webhook handler) write to
``_PENDING_JOBS_FILE`` concurrently.

Both ``_save_pending_job`` and ``_remove_pending_job`` must:
  * Acquire the module-level ``_pending_jobs_lock``.
  * Write atomically via a temp file + ``Path.replace``.
  * Handle the missing-file case gracefully.

Run with:
    python -m pytest tools/tests/test_pending_jobs_atomic.py -v
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import tools.app.scheduler as sched


@pytest.fixture
def isolated_pending_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _PENDING_JOBS_FILE into a per-test tmp directory.

    Prevents tests from clobbering each other or the real .tmp/ directory.
    """
    target = tmp_path / "pending_post_meeting_jobs.json"
    monkeypatch.setattr(sched, "_PENDING_JOBS_FILE", target)
    return target


# ---------------------------------------------------------------------------
# Behavioural tests
# ---------------------------------------------------------------------------


def test_remove_when_file_missing(isolated_pending_file: Path) -> None:
    """_remove_pending_job must NOT raise when the file does not exist."""
    assert not isolated_pending_file.exists()
    # Should be a silent no-op
    sched._remove_pending_job(1, 5)
    # And must not have created an empty/garbage file as a side effect
    assert not isolated_pending_file.exists()


def test_save_then_remove_consistency(isolated_pending_file: Path) -> None:
    """Save 3 jobs, remove 1, verify the remaining 2 are present."""
    sched._save_pending_job(1, 1, "meeting-a", "2026-05-13T20:00:00+04:00")
    sched._save_pending_job(1, 2, "meeting-b", "2026-05-14T20:00:00+04:00")
    sched._save_pending_job(2, 1, "meeting-c", "2026-05-15T20:00:00+04:00")

    data = json.loads(isolated_pending_file.read_text())
    assert len(data) == 3

    sched._remove_pending_job(1, 2)

    remaining = json.loads(isolated_pending_file.read_text())
    keys = {(j["group"], j["lecture"]) for j in remaining}
    assert keys == {(1, 1), (2, 1)}
    # And the removed entry is gone
    assert (1, 2) not in keys
    # File ends with a valid JSON array, parseable in full
    assert isinstance(remaining, list)


def test_save_replaces_existing_group_lecture_pair(
    isolated_pending_file: Path,
) -> None:
    """Saving the same (group, lecture) twice must overwrite, not duplicate."""
    sched._save_pending_job(1, 7, "meeting-old", "2026-05-13T20:00:00+04:00")
    sched._save_pending_job(1, 7, "meeting-new", "2026-05-13T20:00:00+04:00")

    data = json.loads(isolated_pending_file.read_text())
    matches = [j for j in data if j["group"] == 1 and j["lecture"] == 7]
    assert len(matches) == 1
    assert matches[0]["meeting_id"] == "meeting-new"


def test_remove_when_file_is_corrupt(
    isolated_pending_file: Path,
) -> None:
    """A corrupt JSON file should be tolerated, not crash the pipeline."""
    isolated_pending_file.write_text("{not-json", encoding="utf-8")
    # Should not raise
    sched._remove_pending_job(1, 1)


# ---------------------------------------------------------------------------
# Concurrency regression — the bug US-011 fixes
# ---------------------------------------------------------------------------


def test_concurrent_save_remove_no_corruption(
    isolated_pending_file: Path,
) -> None:
    """Spawn parallel save+remove threads; final file must remain parseable.

    Without the lock + atomic write on ``_remove_pending_job`` this test
    is flaky/fails — a partially-written ``write_text`` could leave the
    JSON truncated (parse error) or with mixed-state contents from an
    interleaved save.
    """
    iterations = 50
    errors: list[BaseException] = []

    def saver() -> None:
        try:
            for i in range(iterations):
                sched._save_pending_job(
                    1, i, f"meeting-{i}", f"2026-05-13T20:{i:02d}:00+04:00",
                )
        except BaseException as exc:  # noqa: BLE001 — capture for assertion
            errors.append(exc)

    def remover() -> None:
        try:
            for i in range(iterations):
                # Remove a key that may or may not exist yet — both paths
                # exercise the read-modify-write sequence.
                sched._remove_pending_job(1, i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=saver)
    t2 = threading.Thread(target=remover)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not t1.is_alive() and not t2.is_alive(), "threads hung"
    assert not errors, f"thread raised: {errors}"

    # File must exist and contain valid JSON (a list)
    assert isolated_pending_file.exists()
    final = json.loads(isolated_pending_file.read_text())
    assert isinstance(final, list)
    # Every entry must have the documented shape
    for entry in final:
        assert "group" in entry
        assert "lecture" in entry
        assert "meeting_id" in entry
        assert "fire_time" in entry


def test_many_savers_no_lost_entries(isolated_pending_file: Path) -> None:
    """N parallel savers with distinct keys → all N keys are present.

    The lock must serialize the read-modify-write so no saver's append
    is dropped by another saver's stale read.
    """
    n_threads = 8
    per_thread = 10

    def saver(group: int) -> None:
        for lec in range(per_thread):
            sched._save_pending_job(
                group, lec, f"m-{group}-{lec}",
                f"2026-05-13T20:{lec:02d}:00+04:00",
            )

    threads = [
        threading.Thread(target=saver, args=(g,))
        for g in range(1, n_threads + 1)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    data = json.loads(isolated_pending_file.read_text())
    keys = {(j["group"], j["lecture"]) for j in data}
    expected = {(g, lec) for g in range(1, n_threads + 1) for lec in range(per_thread)}
    assert keys == expected, (
        f"missing {expected - keys}, extra {keys - expected}"
    )
