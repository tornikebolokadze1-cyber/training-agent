"""Dead Letter Queue for failed pipeline deliveries.

File-backed retry queue in TMP_DIR/dlq/. Each failed operation is saved
as a JSON file with exponential backoff scheduling. A periodic processor
(run by APScheduler) retries pending items.

Usage:
    # Enqueue a failed delivery
    enqueue("whatsapp_group_notify", {"group": 1, "lecture": 5, "message": "..."}, "Connection refused")

    # Process ready items (called by scheduler cron)
    process_pending_items()
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from tools.core.config import TBILISI_TZ, TMP_DIR

logger = logging.getLogger(__name__)

DLQ_DIR = TMP_DIR / "dlq"
DLQ_DEAD_DIR = DLQ_DIR / "dead"  # Permanently failed items
MAX_RETRIES = 5
BACKOFF_BASE_MINUTES = 5  # 5, 10, 20, 40, 80 minutes


@dataclass
class DeadLetter:
    id: str
    operation: str
    created_at: str
    retry_count: int
    max_retries: int
    next_retry_at: str
    payload: dict[str, Any]
    last_error: str


# Registry of operation handlers -- populated by register_handler()
_handlers: dict[str, Callable[[dict[str, Any]], None]] = {}


def register_handler(operation: str, handler: Callable[[dict[str, Any]], None]) -> None:
    """Register a retry handler for a DLQ operation type."""
    _handlers[operation] = handler
    logger.debug("DLQ handler registered: %s", operation)


def enqueue(operation: str, payload: dict[str, Any], error: str) -> str:
    """Add a failed operation to the dead letter queue.

    Returns the dead letter ID.
    """
    DLQ_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(tz=TBILISI_TZ)
    letter_id = str(uuid.uuid4())[:8]
    next_retry = now + timedelta(minutes=BACKOFF_BASE_MINUTES)

    letter = DeadLetter(
        id=letter_id,
        operation=operation,
        created_at=now.isoformat(),
        retry_count=0,
        max_retries=MAX_RETRIES,
        next_retry_at=next_retry.isoformat(),
        payload=payload,
        last_error=str(error)[:500],  # Truncate long errors
    )

    path = DLQ_DIR / f"{letter_id}.json"
    path.write_text(
        json.dumps(asdict(letter), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.warning(
        "DLQ enqueued: %s op=%s retry_at=%s error=%s",
        letter_id,
        operation,
        next_retry.isoformat(),
        str(error)[:100],
    )
    return letter_id


def process_pending_items() -> dict[str, int]:
    """Process all DLQ items that are past their next_retry_at.

    Returns dict with counts: {"processed": N, "succeeded": N, "failed": N, "dead": N}
    """
    if not DLQ_DIR.exists():
        return {"processed": 0, "succeeded": 0, "failed": 0, "dead": 0}

    now = datetime.now(tz=TBILISI_TZ)
    stats: dict[str, int] = {"processed": 0, "succeeded": 0, "failed": 0, "dead": 0}

    for path in sorted(DLQ_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            letter = DeadLetter(**data)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Corrupt DLQ file %s: %s -- removing", path.name, exc)
            path.unlink(missing_ok=True)
            continue

        # Check if ready for retry
        try:
            next_retry = datetime.fromisoformat(letter.next_retry_at)
        except (ValueError, TypeError):
            next_retry = now  # Retry immediately if timestamp is bad

        if next_retry > now:
            continue  # Not yet due

        stats["processed"] += 1
        handler = _handlers.get(letter.operation)

        if handler is None:
            logger.error(
                "No DLQ handler for operation '%s' -- item %s",
                letter.operation,
                letter.id,
            )
            stats["failed"] += 1
            continue

        try:
            handler(letter.payload)
            # Success -- remove from queue
            path.unlink(missing_ok=True)
            stats["succeeded"] += 1
            logger.info(
                "DLQ retry succeeded: %s op=%s (attempt %d)",
                letter.id,
                letter.operation,
                letter.retry_count + 1,
            )
        except Exception as exc:
            letter_dict = asdict(letter)
            letter_dict["retry_count"] += 1
            letter_dict["last_error"] = str(exc)[:500]

            if letter_dict["retry_count"] >= letter.max_retries:
                # Permanently failed -- move to dead dir
                DLQ_DEAD_DIR.mkdir(parents=True, exist_ok=True)
                dead_path = DLQ_DEAD_DIR / path.name
                dead_path.write_text(
                    json.dumps(letter_dict, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                path.unlink(missing_ok=True)
                stats["dead"] += 1
                logger.error(
                    "DLQ permanently failed: %s op=%s after %d attempts. "
                    "Last error: %s",
                    letter.id,
                    letter.operation,
                    letter.max_retries,
                    str(exc)[:200],
                )
                # Send email as absolute last resort
                try:
                    from tools.integrations.whatsapp_sender import (
                        send_email_fallback,
                    )

                    send_email_fallback(
                        subject=f"DLQ permanently failed: {letter.operation}",
                        body=(
                            f"Operation: {letter.operation}\n"
                            f"Payload: {json.dumps(letter.payload, indent=2)}\n"
                            f"Error: {exc}\n"
                            f"Attempts: {letter.max_retries}"
                        ),
                    )
                except Exception:
                    pass
            else:
                # Schedule next retry with exponential backoff
                backoff = BACKOFF_BASE_MINUTES * (2 ** letter_dict["retry_count"])
                next_at = now + timedelta(minutes=backoff)
                letter_dict["next_retry_at"] = next_at.isoformat()
                path.write_text(
                    json.dumps(letter_dict, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                stats["failed"] += 1
                logger.warning(
                    "DLQ retry failed: %s op=%s attempt %d/%d, next at %s. "
                    "Error: %s",
                    letter.id,
                    letter.operation,
                    letter_dict["retry_count"],
                    letter.max_retries,
                    next_at.isoformat(),
                    str(exc)[:100],
                )

    if stats["processed"]:
        logger.info("DLQ processed: %s", stats)
    return stats


def get_queue_status() -> dict[str, Any]:
    """Return DLQ status for /status endpoint."""
    pending = 0
    dead = 0
    if DLQ_DIR.exists():
        pending = len(list(DLQ_DIR.glob("*.json")))
    if DLQ_DEAD_DIR.exists():
        dead = len(list(DLQ_DEAD_DIR.glob("*.json")))
    return {"pending": pending, "permanently_failed": dead}


def cleanup_old_dead_letters(max_age_days: int = 7) -> int:
    """Remove permanently failed items older than max_age_days."""
    if not DLQ_DEAD_DIR.exists():
        return 0
    deleted = 0
    now = time.time()
    for path in DLQ_DEAD_DIR.glob("*.json"):
        if (now - path.stat().st_mtime) > max_age_days * 86400:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted
