"""Durable delivery-artifact tracker for pipeline idempotency.

The pipeline state file (``pipeline_state_g{N}_l{M}.json``) is deleted by
``reset_failed()`` whenever a retry is scheduled, which means the
``summary_doc_id``, ``report_doc_id``, ``group_notified`` and
``private_notified`` flags carried inside ``PipelineState`` do NOT survive a
retry.  Before this module existed, every retry of a partially-successful
pipeline would:

  • Re-upload identical summary / private-report Google Docs (creating
    orphan documents in Drive and burning quota).
  • Re-send the WhatsApp group notification — students saw the same
    lecture link 2-3 times in a row.
  • Re-index identical vectors into Pinecone.

This module persists the **delivery artifacts** that must NOT be redone,
keyed by ``g{group}_l{lecture}``.  Records survive ``reset_failed()`` and
``cleanup_completed()``; they are intentionally append-only for a lecture
slot until the operator clears them via ``clear_delivery()`` (used by the
admin "reprocess from scratch" path).

JSON shape (``.tmp/delivery_tracker.json``)::

    {
      "g3_l2": {
        "group": 3,
        "lecture": 2,
        "summary_doc_id": "1AbC...",
        "report_doc_id": "1XyZ...",
        "whatsapp_notification_sent_at": "2026-05-19T20:34:11+04:00",
        "private_report_sent_at": "2026-05-19T20:34:18+04:00",
        "pinecone_indexed_at": "2026-05-19T20:35:02+04:00",
        "updated_at": "2026-05-19T20:35:02+04:00"
      }
    }

All writes are atomic via temp-file + ``os.replace``.  All reads are
tolerant of corrupt JSON (a warning is logged and an empty dict returned).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

from tools.core.config import TBILISI_TZ, TMP_DIR

logger = logging.getLogger(__name__)

DELIVERY_TRACKER_PATH = TMP_DIR / "delivery_tracker.json"

# Per-key locks serialise concurrent writers for the SAME lecture; different
# lectures still write in parallel.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _now_iso() -> str:
    return datetime.now(tz=TBILISI_TZ).isoformat()


def _record_key(group: int, lecture: int) -> str:
    return f"g{group}_l{lecture}"


def _get_lock(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _load_all() -> dict[str, dict[str, Any]]:
    if not DELIVERY_TRACKER_PATH.exists():
        return {}
    try:
        raw = DELIVERY_TRACKER_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load delivery tracker: %s", exc)
    return {}


def _save_all(data: dict[str, dict[str, Any]]) -> None:
    """Atomically persist the tracker to disk."""
    tmp = DELIVERY_TRACKER_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, DELIVERY_TRACKER_PATH)
    except OSError as exc:
        logger.error("Failed to save delivery tracker: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def load_delivery(group: int, lecture: int) -> dict[str, Any]:
    """Return the delivery-artifact record for one lecture (empty dict if none)."""
    return dict(_load_all().get(_record_key(group, lecture), {}))


def record_delivery(group: int, lecture: int, **fields: Any) -> dict[str, Any]:
    """Merge new delivery fields into the persistent record.

    Only non-empty values overwrite existing fields — this guarantees that a
    later partial run cannot blank out an earlier success.  ``updated_at`` is
    always stamped to the current Tbilisi time.

    Args:
        group: Training group number.
        lecture: Lecture number.
        **fields: Arbitrary fields to persist (e.g. ``summary_doc_id``,
            ``whatsapp_notification_sent_at``).

    Returns:
        The merged record after the write.
    """
    key = _record_key(group, lecture)
    with _get_lock(key):
        data = _load_all()
        existing = data.get(key, {})
        merged: dict[str, Any] = {**existing}
        merged["group"] = group
        merged["lecture"] = lecture
        for name, value in fields.items():
            # Skip empty values so a partial later pass cannot null out
            # an earlier success.
            if value in (None, "", False):
                continue
            merged[name] = value
        merged["updated_at"] = _now_iso()
        data[key] = merged
        _save_all(data)
        return dict(merged)


def has_delivered(group: int, lecture: int, field_name: str) -> bool:
    """Return True if *field_name* is set and truthy for this lecture."""
    record = load_delivery(group, lecture)
    return bool(record.get(field_name))


def get_field(group: int, lecture: int, field_name: str) -> Any:
    """Return *field_name* from the delivery record (or None if absent)."""
    return load_delivery(group, lecture).get(field_name)


def clear_delivery(group: int, lecture: int) -> bool:
    """Remove the delivery record entirely (admin reprocess path).

    Returns:
        True if a record was removed, False if none existed.
    """
    key = _record_key(group, lecture)
    with _get_lock(key):
        data = _load_all()
        if key not in data:
            return False
        del data[key]
        _save_all(data)
        logger.info(
            "[delivery] Cleared delivery record for G%d L%d", group, lecture,
        )
        return True


def list_all() -> dict[str, dict[str, Any]]:
    """Return a copy of the full tracker (used by status endpoints / tests)."""
    return _load_all()
