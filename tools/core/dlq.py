"""Dead Letter Queue (DLQ) for failed pipeline side-effects.

When a non-critical side-effect (Drive summary upload, WhatsApp notification,
Pinecone indexing) fails during the main pipeline, the operation is enqueued
here for later retry. The orchestrator registers handlers at startup that
consume DLQ entries on a periodic schedule.

Each DLQ entry is a JSON file in TMP_DIR/dlq/ containing:
    - operation: string identifying the handler (e.g. "drive_summary")
    - payload: dict with all data needed to retry the operation
    - created_at: ISO 8601 timestamp
    - retry_count: number of retry attempts so far
    - last_error: most recent error message (if any)
    - max_retries: maximum retry attempts before permanent failure

Usage::

    from tools.core.dlq import enqueue, process_all, register_handler

    # Register a handler for an operation type
    register_handler("drive_summary", my_retry_function)

    # Enqueue a failed operation for later retry
    enqueue("drive_summary", {"group": 1, "lecture": 3, "title": "...", ...})

    # Process all pending DLQ entries (called periodically by orchestrator)
    results = process_all()
"""

from __future__ import annotations

import itertools
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.core.config import TBILISI_TZ, TMP_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DLQ directory
# ---------------------------------------------------------------------------

DLQ_DIR: Path = TMP_DIR / "dlq"
DLQ_DIR.mkdir(parents=True, exist_ok=True)

# Default max retries before an entry is marked as permanently failed.
DEFAULT_MAX_RETRIES: int = 5

# Monotonic counter to guarantee unique filenames even when two enqueue()
# calls happen within the same microsecond (common in rapid sequential calls).
_seq_counter: itertools.count[int] = itertools.count()

# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

_handlers: dict[str, Callable[[dict[str, Any]], None]] = {}


def register_handler(operation: str, handler: Callable[[dict[str, Any]], None]) -> None:
    """Register a retry handler for a DLQ operation type.

    Args:
        operation: Unique string identifying the operation (e.g. "drive_summary").
        handler: Callable that accepts a payload dict and performs the retry.
            Must raise an exception on failure so the DLQ can track retry count.
    """
    _handlers[operation] = handler
    logger.debug("DLQ handler registered: %s", operation)


# ---------------------------------------------------------------------------
# Enqueue / dequeue
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=TBILISI_TZ).isoformat()


def enqueue(
    operation: str,
    payload: dict[str, Any],
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Path:
    """Add a failed operation to the DLQ for later retry.

    Args:
        operation: Handler name (must match a registered handler).
        payload: All data needed to retry the operation.
        max_retries: Maximum number of retry attempts.

    Returns:
        Path to the created DLQ entry file.
    """
    timestamp = datetime.now(tz=TBILISI_TZ).strftime("%Y%m%d_%H%M%S_%f")
    seq = next(_seq_counter)
    entry = {
        "operation": operation,
        "payload": payload,
        "created_at": _now_iso(),
        "retry_count": 0,
        "last_error": "",
        "max_retries": max_retries,
    }

    filename = f"{operation}_{timestamp}_{seq:04d}.json"
    path = DLQ_DIR / filename

    # Atomic write
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(
            json.dumps(entry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    logger.info(
        "DLQ enqueued: %s (payload keys: %s)",
        operation,
        list(payload.keys()),
    )
    return path


def _load_entry(path: Path) -> dict[str, Any] | None:
    """Load and validate a DLQ entry file. Returns None if corrupt."""
    try:
        raw = path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
        if "operation" not in data or "payload" not in data:
            logger.warning("DLQ entry missing required fields: %s", path.name)
            return None
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Corrupt DLQ entry %s: %s", path.name, exc)
        return None


def _save_entry(path: Path, entry: dict[str, Any]) -> None:
    """Write an updated DLQ entry back to disk atomically."""
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(
            json.dumps(entry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def process_all() -> dict[str, int]:
    """Process all pending DLQ entries, calling registered handlers.

    Returns:
        Dict with counts: {"processed": N, "failed": N, "expired": N, "skipped": N}
    """
    counts = {"processed": 0, "failed": 0, "expired": 0, "skipped": 0}

    for path in sorted(DLQ_DIR.glob("*.json")):
        entry = _load_entry(path)
        if entry is None:
            counts["skipped"] += 1
            continue

        operation = entry["operation"]
        handler = _handlers.get(operation)

        if handler is None:
            logger.warning(
                "DLQ: no handler registered for operation %r — skipping %s",
                operation,
                path.name,
            )
            counts["skipped"] += 1
            continue

        # Check if max retries exceeded
        if entry["retry_count"] >= entry.get("max_retries", DEFAULT_MAX_RETRIES):
            logger.error(
                "DLQ: entry %s exceeded max retries (%d) — moving to failed/",
                path.name,
                entry["retry_count"],
            )
            _move_to_failed(path)
            counts["expired"] += 1
            continue

        # Attempt retry
        try:
            handler(entry["payload"])
            # Success — remove the entry
            path.unlink(missing_ok=True)
            logger.info("DLQ: successfully processed %s (%s)", path.name, operation)
            counts["processed"] += 1
        except Exception as exc:
            entry["retry_count"] += 1
            entry["last_error"] = str(exc)[:500]
            _save_entry(path, entry)
            logger.warning(
                "DLQ: retry %d failed for %s (%s): %s",
                entry["retry_count"],
                path.name,
                operation,
                exc,
            )
            counts["failed"] += 1

    if any(v > 0 for v in counts.values()):
        logger.info("DLQ processing results: %s", counts)

    return counts


def _move_to_failed(path: Path) -> None:
    """Move a DLQ entry to the failed/ subdirectory for post-mortem."""
    failed_dir = DLQ_DIR / "failed"
    failed_dir.mkdir(exist_ok=True)
    dest = failed_dir / path.name
    try:
        path.rename(dest)
    except OSError as exc:
        logger.warning("Could not move %s to failed/: %s", path.name, exc)


def pending_count() -> int:
    """Return the number of pending DLQ entries."""
    return len(list(DLQ_DIR.glob("*.json")))


def list_pending() -> list[dict[str, Any]]:
    """Return all pending DLQ entries as dicts (for status endpoint)."""
    entries: list[dict[str, Any]] = []
    for path in sorted(DLQ_DIR.glob("*.json")):
        entry = _load_entry(path)
        if entry is not None:
            entry["_filename"] = path.name
            entries.append(entry)
    return entries
