"""Tests for backup_messages_db / _prune_old_backups in message_archive.

US-027 (ralph 2026-05-13): messages.db needs a scheduled SQLite-backup-API
copy with retention so a Railway volume wipe does not lose chat history.

Covers:
    * backup file is created with timestamped name
    * the backup is a valid SQLite database
    * missing source DB is handled gracefully (no crash)
    * retention prunes to 7 most recent backups
    * sqlite3.backup() handles concurrent open connections (not file copy)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.services import message_archive as ma  # noqa: E402


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #

@pytest.fixture
def sample_db(tmp_path: Path) -> Path:
    """Create a small SQLite database with a known table+row for tests."""
    db = tmp_path / "messages.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)"
        )
        conn.execute(
            "INSERT INTO messages (id, content) VALUES (1, 'hello world')"
        )
        conn.commit()
    finally:
        conn.close()
    return db


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #

def test_backup_creates_timestamped_file(sample_db: Path, tmp_path: Path) -> None:
    """backup_messages_db produces a file matching messages_YYYYMMDD_HHMMSS.db."""
    dst_dir = tmp_path / "backups"
    result = ma.backup_messages_db(destination_dir=dst_dir, db_path=sample_db)

    assert result.exists(), "Backup file should exist on disk"
    assert result.parent == dst_dir
    # Filename pattern: messages_YYYYMMDD_HHMMSS.db
    name = result.name
    assert name.startswith("messages_") and name.endswith(".db")
    # Strip prefix + suffix → "YYYYMMDD_HHMMSS"
    stamp = name[len("messages_"):-len(".db")]
    assert len(stamp) == 15, f"Timestamp portion wrong length: {stamp!r}"
    assert stamp[8] == "_"
    date_part, time_part = stamp.split("_")
    assert date_part.isdigit() and len(date_part) == 8
    assert time_part.isdigit() and len(time_part) == 6


def test_backup_is_valid_sqlite(sample_db: Path, tmp_path: Path) -> None:
    """The backup file is a valid SQLite DB whose contents match the source."""
    dst_dir = tmp_path / "backups"
    backup_path = ma.backup_messages_db(destination_dir=dst_dir, db_path=sample_db)

    conn = sqlite3.connect(str(backup_path))
    try:
        row = conn.execute(
            "SELECT id, content FROM messages WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "Backup should preserve the source row"
    assert row[0] == 1
    assert row[1] == "hello world"


def test_backup_handles_missing_source(tmp_path: Path) -> None:
    """When the source DB does not exist, return gracefully without crashing.

    Returns the missing source path; no backup file is created.
    """
    missing_src = tmp_path / "does_not_exist.db"
    dst_dir = tmp_path / "backups"

    result = ma.backup_messages_db(destination_dir=dst_dir, db_path=missing_src)

    assert result == missing_src
    # No backup files should be left behind
    if dst_dir.exists():
        assert list(dst_dir.glob("messages_*.db")) == []


def test_retention_keeps_7_most_recent(tmp_path: Path) -> None:
    """_prune_old_backups deletes all but the 7 most recent files by mtime."""
    dst_dir = tmp_path / "backups"
    dst_dir.mkdir(parents=True)

    # Create 10 backup files with strictly increasing mtimes so ordering
    # is unambiguous on platforms with coarse mtime resolution.
    files: list[Path] = []
    base_mtime = time.time() - 10_000
    for i in range(10):
        p = dst_dir / f"messages_2026010{i}_120000.db"
        p.write_bytes(b"fake-sqlite-bytes")
        # Spread mtimes 100 seconds apart so newest = highest index
        mtime = base_mtime + (i * 100)
        os.utime(p, (mtime, mtime))
        files.append(p)

    ma._prune_old_backups(dst_dir, keep=7)

    remaining = sorted(
        dst_dir.glob("messages_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    assert len(remaining) == 7, (
        f"Expected exactly 7 backups to remain, got {len(remaining)}"
    )
    # The 7 newest (indices 3..9) survived; 0..2 were pruned.
    survivor_names = {p.name for p in remaining}
    for i in range(3, 10):
        assert files[i].name in survivor_names, (
            f"Newest backup {files[i].name} should have survived"
        )
    for i in range(0, 3):
        assert files[i].name not in survivor_names, (
            f"Oldest backup {files[i].name} should have been pruned"
        )


def test_backup_uses_sqlite_api_not_file_copy(sample_db: Path, tmp_path: Path) -> None:
    """SQLite backup API handles an open write transaction on the source.

    A plain file copy would race or error on Windows when the source has
    an active write transaction. sqlite3.Connection.backup() coordinates
    via the SQLite locking protocol and must succeed in this scenario.
    """
    dst_dir = tmp_path / "backups"

    # Open a second connection and BEGIN a write transaction without
    # committing. SQLite holds a RESERVED lock; a naive shutil.copy
    # on some platforms would race, but the backup API must cope.
    writer = sqlite3.connect(str(sample_db))
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO messages (id, content) VALUES (2, 'pending insert')"
        )
        # Do NOT commit — the transaction is intentionally left open.

        backup_path = ma.backup_messages_db(
            destination_dir=dst_dir, db_path=sample_db
        )
        assert backup_path.exists()

        # The backup must be a valid SQLite database — at minimum, the
        # original committed row (id=1) must be present. Whether id=2
        # (uncommitted) appears depends on SQLite's snapshot semantics,
        # but the file must NOT be corrupt.
        verify = sqlite3.connect(str(backup_path))
        try:
            row = verify.execute(
                "SELECT id, content FROM messages WHERE id = 1"
            ).fetchone()
            assert row is not None
            assert row[1] == "hello world"
        finally:
            verify.close()
    finally:
        try:
            writer.rollback()
        except sqlite3.Error:
            pass
        writer.close()


def test_backup_default_destination_dir(sample_db: Path) -> None:
    """When destination_dir is None, backups land in <db parent>/backups/messages/."""
    result = ma.backup_messages_db(db_path=sample_db)
    try:
        assert result.parent == sample_db.parent / "backups" / "messages"
        assert result.exists()
    finally:
        # Tidy up created tree so we don't leak state into tmp_path teardown
        if result.exists():
            result.unlink()
        parent = sample_db.parent / "backups" / "messages"
        if parent.exists():
            parent.rmdir()
        grand = sample_db.parent / "backups"
        if grand.exists():
            grand.rmdir()
