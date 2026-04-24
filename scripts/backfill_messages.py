"""Backfill the local messages.db from a Green API export JSON.

Reads the most recent `data/greenapi_backfill/export_*.json` (or an
explicit path) and inserts every message via message_archive. Idempotent:
re-running is safe because green_api_id is UNIQUE.

Usage:
  python -m scripts.backfill_messages                        # newest export
  python -m scripts.backfill_messages --file PATH            # specific export
  python -m scripts.backfill_messages --dry-run              # parse only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root import
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from tools.services.message_archive import (
    DEFAULT_DB_PATH,
    connect,
    normalize_green_api_message,
    insert_message,
    count_by_group,
)


EXPORT_DIR = PROJECT_ROOT / "data" / "greenapi_backfill"

CHAT_LABEL_TO_GROUP = {
    "group_1": 1,
    "group_2": 2,
    "tornike_dm": None,
}


def _latest_export() -> Path | None:
    if not EXPORT_DIR.exists():
        return None
    candidates = sorted(EXPORT_DIR.glob("export_*.json"))
    return candidates[-1] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="path to export JSON")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    export_path = args.file or _latest_export()
    if not export_path or not export_path.exists():
        print(f"ERROR: no export file found in {EXPORT_DIR}", file=sys.stderr)
        return 2

    print(f"Loading export: {export_path}")
    data = json.loads(export_path.read_text(encoding="utf-8"))
    chats = data.get("chats", {})
    if not chats:
        print("No chats in export.", file=sys.stderr)
        return 3

    total_inserted = 0
    total_skipped = 0
    total_errors = 0
    per_chat_stats: list[dict] = []

    if args.dry_run:
        print("DRY-RUN — parsing only, no DB writes\n")
        for label, info in chats.items():
            msgs = info.get("messages", [])
            chat_id = info.get("chat_id", "")
            group = CHAT_LABEL_TO_GROUP.get(label)
            parsed = 0
            errors = 0
            for raw in msgs:
                try:
                    normalize_green_api_message(raw, chat_id, group)
                    parsed += 1
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        print(f"  [{label}] parse error: {e}")
            print(f"  {label}: parsed {parsed}, errors {errors}")
            total_inserted += parsed
            total_errors += errors
        print(f"\nTotal parsed: {total_inserted}, errors: {total_errors}")
        return 0

    with connect(DEFAULT_DB_PATH) as conn:
        for label, info in chats.items():
            msgs = info.get("messages", [])
            chat_id = info.get("chat_id", "")
            group = CHAT_LABEL_TO_GROUP.get(label)
            inserted = 0
            skipped = 0
            errors = 0

            print(f"Backfilling {label} ({len(msgs)} messages)...", flush=True)
            for raw in msgs:
                try:
                    m = normalize_green_api_message(raw, chat_id, group)
                    if insert_message(conn, m):
                        inserted += 1
                    else:
                        skipped += 1
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        print(f"  [{label}] error: {e}  payload keys={list(raw.keys())}")

            per_chat_stats.append({
                "label": label, "inserted": inserted,
                "skipped": skipped, "errors": errors,
            })
            total_inserted += inserted
            total_skipped += skipped
            total_errors += errors
            print(f"  done: inserted={inserted}, skipped(dup)={skipped}, errors={errors}")

    print(f"\n{'=' * 60}")
    print(f"TOTAL: inserted={total_inserted}, skipped={total_skipped}, errors={total_errors}")

    with connect(DEFAULT_DB_PATH) as conn:
        counts = count_by_group(conn)
        print(f"DB state: {counts}")

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
