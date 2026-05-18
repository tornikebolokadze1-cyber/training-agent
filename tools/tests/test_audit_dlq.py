"""Tests for scripts/audit_dlq.py (US-022)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_dlq  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_entry(dlq_dir: Path, filename: str, data: dict | str) -> Path:
    dlq_dir.mkdir(parents=True, exist_ok=True)
    path = dlq_dir / filename
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _synthetic_entry() -> dict:
    """Shape that matches the test/fixture DLQ entries we found in production."""
    return {
        "operation": "drive_summary",
        "payload": {
            "args": ["1", "2", "'lecture summary text'"],
            "operation": "Drive summary upload",
        },
        "created_at": "2026-04-25T22:00:07.438073+04:00",
        "retry_count": 0,
        "last_error": "",
        # max_retries-as-string is a synthetic marker
        "max_retries": "Drive quota exceeded",
    }


def _real_entry(group: int = 1, lecture: int = 4) -> dict:
    return {
        "operation": "whatsapp_group",
        "payload": {
            "operation": "WhatsApp group notification",
            "group_number": group,
            "lecture_number": lecture,
            "recording_file_id": "1AbcDefGhi_realdriveid",
            "summary_doc_id": "1XyzDocRealId",
        },
        "created_at": "2026-05-13T23:05:50.473972+04:00",
        "retry_count": 1,
        "last_error": "Connection refused",
        "max_retries": 5,
    }


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------


def test_classify_synthetic_marker(tmp_path: Path) -> None:
    path = _write_entry(tmp_path, "drive_summary_synthetic.json", _synthetic_entry())
    entry = audit_dlq.classify_entry(path)
    assert entry.status == "synthetic"
    assert entry.operation == "drive_summary"
    assert entry.date == "2026-04-25"


def test_classify_real_entry(tmp_path: Path) -> None:
    path = _write_entry(tmp_path, "whatsapp_group_real.json", _real_entry())
    entry = audit_dlq.classify_entry(path)
    assert entry.status == "real"
    assert entry.operation == "whatsapp_group"
    assert entry.group == "1"
    assert entry.date == "2026-05-13"
    assert "Connection refused" in entry.error_class


def test_classify_corrupt_json(tmp_path: Path) -> None:
    path = _write_entry(tmp_path, "corrupt.json", "{this is not valid json")
    entry = audit_dlq.classify_entry(path)
    assert entry.status == "corrupt"


def test_classify_non_object_json(tmp_path: Path) -> None:
    """A JSON file containing an array (or scalar) at the top level is corrupt
    in our context — every real DLQ entry is an object."""
    path = _write_entry(tmp_path, "scalar.json", "[1, 2, 3]")
    entry = audit_dlq.classify_entry(path)
    assert entry.status == "corrupt"


# ---------------------------------------------------------------------------
# Aggregation tests
# ---------------------------------------------------------------------------


def test_aggregation_by_date(tmp_path: Path) -> None:
    # Three entries on three different dates
    e1 = _real_entry(group=1)
    e1["created_at"] = "2026-05-10T10:00:00+04:00"
    e2 = _real_entry(group=2)
    e2["created_at"] = "2026-05-11T11:00:00+04:00"
    e3 = _real_entry(group=3)
    e3["created_at"] = "2026-05-12T12:00:00+04:00"

    _write_entry(tmp_path, "a.json", e1)
    _write_entry(tmp_path, "b.json", e2)
    _write_entry(tmp_path, "c.json", e3)

    entries = audit_dlq.scan_dlq(tmp_path)
    report = audit_dlq.aggregate(entries)

    assert report.total == 3
    assert report.by_date == {
        "2026-05-10": 1,
        "2026-05-11": 1,
        "2026-05-12": 1,
    }
    assert report.by_group == {"1": 1, "2": 1, "3": 1}
    assert report.by_status.get("real", 0) == 3


def test_aggregation_mixed_statuses(tmp_path: Path) -> None:
    _write_entry(tmp_path, "syn.json", _synthetic_entry())
    _write_entry(tmp_path, "real.json", _real_entry())
    _write_entry(tmp_path, "bad.json", "{not json")

    entries = audit_dlq.scan_dlq(tmp_path)
    report = audit_dlq.aggregate(entries)

    assert report.total == 3
    assert report.by_status == {"synthetic": 1, "real": 1, "corrupt": 1}


# ---------------------------------------------------------------------------
# Execute / dry-run tests
# ---------------------------------------------------------------------------


def test_dry_run_does_not_move_files(tmp_path: Path) -> None:
    syn_path = _write_entry(tmp_path, "syn.json", _synthetic_entry())
    real_path = _write_entry(tmp_path, "real.json", _real_entry())
    corrupt_path = _write_entry(tmp_path, "corrupt.json", "garbage")

    rc = audit_dlq.main(
        ["--dlq-dir", str(tmp_path), "--report-dir", str(tmp_path / "reports")]
    )
    assert rc == 0

    # Nothing moved
    assert syn_path.exists()
    assert real_path.exists()
    assert corrupt_path.exists()
    # No archive dirs created
    assert not (tmp_path / "archive").exists()


def test_execute_moves_synthetic_to_archive(tmp_path: Path) -> None:
    syn_path = _write_entry(tmp_path, "syn.json", _synthetic_entry())
    real_path = _write_entry(tmp_path, "real.json", _real_entry())
    corrupt_path = _write_entry(tmp_path, "corrupt.json", "garbage")

    rc = audit_dlq.main(
        [
            "--execute",
            "--dlq-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert rc == 0

    # Synthetic moved
    assert not syn_path.exists()
    assert (tmp_path / "archive" / "synthetic" / "syn.json").exists()

    # Corrupt moved
    assert not corrupt_path.exists()
    assert (tmp_path / "archive" / "corrupt" / "corrupt.json").exists()

    # Real left in place
    assert real_path.exists()


def test_empty_dlq_does_not_crash(tmp_path: Path) -> None:
    """Pointing at an empty (or non-existent) DLQ should print a clean
    'No DLQ entries found' message and return 0."""
    rc = audit_dlq.main(
        [
            "--dlq-dir",
            str(tmp_path / "does-not-exist"),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert rc == 0


def test_filter_synthetic_only(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_entry(tmp_path, "syn.json", _synthetic_entry())
    _write_entry(tmp_path, "real.json", _real_entry())

    rc = audit_dlq.main(
        [
            "--filter",
            "synthetic",
            "--dlq-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Aggregate counts should still show both statuses
    assert "real=1" in out
    assert "synthetic=1" in out
