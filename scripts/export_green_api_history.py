"""Rescue export — pull full Green API chat history for both groups to JSON.

Non-destructive. Writes to data/greenapi_backfill_YYYYMMDD_HHMMSS.json.
Intended as the **last line of defense** before Green API rotates out messages.

Usage:
  python -m scripts.export_green_api_history                    # default count=1000
  python -m scripts.export_green_api_history --count 2000       # deeper probe
  python -m scripts.export_green_api_history --include-dm       # also pull Tornike DM
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed", file=sys.stderr)
    sys.exit(1)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> dict[str, str]:
    if load_dotenv:
        load_dotenv(PROJECT_ROOT / ".env")
    keys = [
        "GREEN_API_INSTANCE_ID",
        "GREEN_API_TOKEN",
        "WHATSAPP_GROUP1_ID",
        "WHATSAPP_GROUP2_ID",
        "WHATSAPP_TORNIKE_PHONE",
    ]
    env = {k: os.environ.get(k, "") for k in keys}
    missing = [k for k in ("GREEN_API_INSTANCE_ID", "GREEN_API_TOKEN", "WHATSAPP_GROUP1_ID", "WHATSAPP_GROUP2_ID") if not env[k]]
    if missing:
        print(f"ERROR: missing env vars {missing}", file=sys.stderr)
        sys.exit(2)
    return env


def _fetch_history(instance_id: str, token: str, chat_id: str, count: int) -> list[dict]:
    url = f"https://api.green-api.com/waInstance{instance_id}/getChatHistory/{token}"
    with httpx.Client(timeout=60) as client:
        r = client.post(url, json={"chatId": chat_id, "count": count})
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    data = r.json()
    return data if isinstance(data, list) else []


def _summarize(messages: list[dict]) -> dict:
    if not messages:
        return {"count": 0}
    ts = [m.get("timestamp") for m in messages if m.get("timestamp")]
    types: dict[str, int] = {}
    senders: dict[str, int] = {}
    for m in messages:
        t = m.get("typeMessage", "unknown")
        types[t] = types.get(t, 0) + 1
        s = str(m.get("senderName") or m.get("senderId") or "?")
        senders[s] = senders.get(s, 0) + 1
    return {
        "count": len(messages),
        "oldest_iso": datetime.fromtimestamp(min(ts), tz=timezone.utc).isoformat() if ts else None,
        "newest_iso": datetime.fromtimestamp(max(ts), tz=timezone.utc).isoformat() if ts else None,
        "days_span": round((max(ts) - min(ts)) / 86400.0, 2) if ts else None,
        "by_type": dict(sorted(types.items(), key=lambda x: -x[1])),
        "by_sender": dict(sorted(senders.items(), key=lambda x: -x[1])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--include-dm", action="store_true", help="also fetch Tornike DM chat")
    args = parser.parse_args()

    env = _load_env()
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = PROJECT_ROOT / "data" / "greenapi_backfill"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"export_{ts}.json"

    chats: list[tuple[str, str]] = [
        ("group_1", env["WHATSAPP_GROUP1_ID"]),
        ("group_2", env["WHATSAPP_GROUP2_ID"]),
    ]
    if args.include_dm and env["WHATSAPP_TORNIKE_PHONE"]:
        chats.append(("tornike_dm", f"{env['WHATSAPP_TORNIKE_PHONE']}@c.us"))

    export: dict = {
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "count_requested_per_chat": args.count,
        "chats": {},
    }

    total = 0
    for label, chat_id in chats:
        print(f"Fetching {label} ({chat_id}, count={args.count})...", flush=True)
        msgs = _fetch_history(env["GREEN_API_INSTANCE_ID"], env["GREEN_API_TOKEN"], chat_id, args.count)
        summary = _summarize(msgs)
        export["chats"][label] = {
            "chat_id": chat_id,
            "summary": summary,
            "messages": msgs,
        }
        total += len(msgs)
        print(f"  got {summary.get('count', 0)} messages, span={summary.get('days_span')} days")

    out_path.write_text(json.dumps(export, indent=2, ensure_ascii=False), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"\nWrote {total} messages to {out_path}  ({size_kb:.1f} KB)")
    print("Summary per chat:")
    for label, info in export["chats"].items():
        s = info["summary"]
        print(f"  {label}: {s.get('count', 0)} msgs, {s.get('days_span')} days, {len(s.get('by_sender', {}))} senders")

    return 0


if __name__ == "__main__":
    sys.exit(main())
