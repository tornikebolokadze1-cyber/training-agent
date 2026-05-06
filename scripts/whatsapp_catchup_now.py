"""One-shot WhatsApp archive catch-up — operator-runnable.

When the live webhook path or the nightly APScheduler catch-up has been down
for an extended period (e.g. local server stopped, Railway redeploy gap), the
DB falls behind Green API. This CLI replays the same logic the nightly job
runs (see :func:`tools.app.scheduler._run_whatsapp_archive_catchup`) without
needing to wait for 04:30 Tbilisi.

It calls ``getChatHistory`` for both training groups and the operator DM
(if configured) and INSERT-IGNOREs into ``messages.db`` via the canonical
:func:`tools.services.message_archive.bulk_insert`. Idempotent — re-running
silently drops dupes. Honors ``MESSAGE_ARCHIVE_DB_PATH`` so it writes to
the same file the live webhook does.

Usage
-----
    python scripts/whatsapp_catchup_now.py
    python scripts/whatsapp_catchup_now.py --count 1000        # per chat
    python scripts/whatsapp_catchup_now.py --skip-dm           # groups only

Required env vars: ``GREEN_API_INSTANCE_ID``, ``GREEN_API_TOKEN``,
``WHATSAPP_GROUP1_ID``, ``WHATSAPP_GROUP2_ID``. Optional:
``WHATSAPP_TORNIKE_PHONE`` for the operator DM.

Why this exists
---------------
The nightly catch-up at 04:30 Tbilisi is the canonical safety net (PR #19).
But operators also need a manual lever for "I just realized the DB is days
behind, run the catch-up NOW". This script is that lever — same code path,
no schedule wait.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import dotenv
import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

dotenv.load_dotenv(PROJECT_ROOT / ".env")

from tools.services.message_archive import (  # noqa: E402
    bulk_insert,
    connect,
    normalize_green_api_message,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("whatsapp_catchup_now")


def _fetch(instance_id: str, token: str, chat_id: str, count: int) -> list[dict]:
    url = (
        f"https://api.green-api.com/waInstance{instance_id}"
        f"/getChatHistory/{token}"
    )
    with httpx.Client(timeout=120) as client:
        r = client.post(url, json={"chatId": chat_id, "count": count})
    r.raise_for_status()
    payload = r.json()
    return payload if isinstance(payload, list) else []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Force-run the WhatsApp archive catch-up now"
    )
    parser.add_argument(
        "--count", type=int, default=1000,
        help="Messages per chat to fetch (Green API max ~1000). Default: 1000",
    )
    parser.add_argument(
        "--skip-dm", action="store_true",
        help="Skip the operator DM, fetch only the two training groups",
    )
    args = parser.parse_args()

    instance_id = os.environ.get("GREEN_API_INSTANCE_ID")
    token = os.environ.get("GREEN_API_TOKEN")
    g1 = os.environ.get("WHATSAPP_GROUP1_ID")
    g2 = os.environ.get("WHATSAPP_GROUP2_ID")
    dm_phone = os.environ.get("WHATSAPP_TORNIKE_PHONE")

    missing = [
        n for n, v in (
            ("GREEN_API_INSTANCE_ID", instance_id),
            ("GREEN_API_TOKEN", token),
            ("WHATSAPP_GROUP1_ID", g1),
            ("WHATSAPP_GROUP2_ID", g2),
        ) if not v
    ]
    if missing:
        logger.error("Missing required env vars: %s", missing)
        return 2

    chats = [(g1, "group_1"), (g2, "group_2")]
    if dm_phone and not args.skip_dm:
        dm_id = dm_phone if dm_phone.endswith("@c.us") else f"{dm_phone}@c.us"
        chats.append((dm_id, "tornike_dm"))

    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        before_max = conn.execute(
            "SELECT MAX(ts_message) FROM messages"
        ).fetchone()[0]
    logger.info("Baseline: %d messages, max_ts=%s", before, before_max)

    totals = {"inserted": 0, "skipped": 0}
    for chat_id, label in chats:
        logger.info("[%s] fetching ...", label)
        try:
            msgs = _fetch(instance_id, token, chat_id, args.count)
        except httpx.HTTPError as exc:
            logger.error("[%s] HTTP error: %s", label, exc)
            continue
        normalized = []
        for m in msgs:
            try:
                normalized.append(normalize_green_api_message(m, chat_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] skip: %s", label, exc)
        with connect() as conn:
            r = bulk_insert(conn, normalized)
        logger.info(
            "[%s] inserted=%d skipped=%d (raw=%d, normalized=%d)",
            label, r["inserted"], r["skipped"], len(msgs), len(normalized),
        )
        totals["inserted"] += r["inserted"]
        totals["skipped"] += r["skipped"]

    with connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        after_max = conn.execute(
            "SELECT MAX(ts_message) FROM messages"
        ).fetchone()[0]
    logger.info(
        "DONE — %d -> %d (+%d), max_ts %s -> %s",
        before, after, after - before, before_max, after_max,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
