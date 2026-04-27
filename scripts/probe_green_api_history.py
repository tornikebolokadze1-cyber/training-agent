"""Green API message history probe — read-only diagnostic.

Verifies:
  1. Auth works (getSettings endpoint).
  2. getChatHistory depth: how many days of messages Green API still holds.
  3. Message type breakdown (text / media / system).
  4. Unique senders.

Output:
  • Human-readable summary to stdout.
  • Full JSON report to data/green_api_probe_report.json.

Usage:
  python -m scripts.probe_green_api_history              # default count=100
  python -m scripts.probe_green_api_history --count 500  # probe deeper
  python -m scripts.probe_green_api_history --json-only  # machine-readable
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
    print("ERROR: httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "green_api_probe_report.json"


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _load_env() -> dict[str, str]:
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")
    required = [
        "GREEN_API_INSTANCE_ID",
        "GREEN_API_TOKEN",
        "WHATSAPP_GROUP1_ID",
        "WHATSAPP_GROUP2_ID",
    ]
    env = {k: os.environ.get(k, "") for k in required}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print(f"ERROR: missing env vars: {missing}", file=sys.stderr)
        print("Ensure .env contains these values.", file=sys.stderr)
        sys.exit(2)
    return env


def _probe_auth(instance_id: str, token: str) -> dict:
    url = f"https://api.green-api.com/waInstance{instance_id}/getSettings/{token}"
    with httpx.Client(timeout=15) as client:
        r = client.get(url)
    return {
        "status_code": r.status_code,
        "ok": r.status_code == 200,
        "body": r.json() if r.status_code == 200 else r.text[:500],
    }


def _probe_chat_history(
    instance_id: str,
    token: str,
    chat_id: str,
    count: int,
) -> dict:
    url = (
        f"https://api.green-api.com/waInstance{instance_id}"
        f"/getChatHistory/{token}"
    )
    with httpx.Client(timeout=30) as client:
        r = client.post(url, json={"chatId": chat_id, "count": count})

    result: dict = {
        "chat_id": chat_id,
        "requested_count": count,
        "status_code": r.status_code,
    }

    if r.status_code != 200:
        result["error"] = r.text[:500]
        return result

    msgs = r.json()
    if not isinstance(msgs, list) or not msgs:
        result["returned_count"] = 0
        result["warning"] = "empty or unexpected response"
        return result

    timestamps = [m.get("timestamp") for m in msgs if m.get("timestamp")]
    types: dict[str, int] = {}
    senders: set[str] = set()

    for m in msgs:
        t = m.get("typeMessage", "unknown")
        types[t] = types.get(t, 0) + 1
        sender = m.get("senderId") or m.get("chatId") or "?"
        senders.add(str(sender))

    oldest = min(timestamps) if timestamps else None
    newest = max(timestamps) if timestamps else None
    days_span = (newest - oldest) / 86400.0 if oldest and newest else None

    result.update({
        "returned_count": len(msgs),
        "oldest_ts": _iso(oldest),
        "newest_ts": _iso(newest),
        "days_of_history_returned": round(days_span, 2) if days_span else None,
        "message_types": types,
        "unique_senders": len(senders),
        "first_sample": {
            k: v for k, v in msgs[0].items() if k in ("senderName", "typeMessage", "timestamp")
        } if msgs else None,
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100, help="messages per chat (max ~1000)")
    parser.add_argument("--json-only", action="store_true", help="suppress human output")
    args = parser.parse_args()

    env = _load_env()
    report = {
        "probed_at": datetime.now(tz=timezone.utc).isoformat(),
        "requested_count_per_chat": args.count,
    }

    report["auth"] = _probe_auth(env["GREEN_API_INSTANCE_ID"], env["GREEN_API_TOKEN"])
    if not report["auth"]["ok"]:
        report["fatal"] = "auth failed — stopping before more calls"
        _write(report, args.json_only)
        return 3

    report["group_1"] = _probe_chat_history(
        env["GREEN_API_INSTANCE_ID"],
        env["GREEN_API_TOKEN"],
        env["WHATSAPP_GROUP1_ID"],
        args.count,
    )
    report["group_2"] = _probe_chat_history(
        env["GREEN_API_INSTANCE_ID"],
        env["GREEN_API_TOKEN"],
        env["WHATSAPP_GROUP2_ID"],
        args.count,
    )

    _write(report, args.json_only)
    return 0


def _write(report: dict, json_only: bool) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if json_only:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    print("=" * 60)
    print("GREEN API MESSAGE HISTORY PROBE")
    print("=" * 60)
    print(f"Probed at: {report['probed_at']}")
    print(f"Count requested per chat: {report['requested_count_per_chat']}")
    print()
    print(f"AUTH: {'OK' if report['auth']['ok'] else 'FAILED'}  (HTTP {report['auth']['status_code']})")
    print()

    for key in ("group_1", "group_2"):
        g = report.get(key, {})
        print(f"[{key.upper()}]")
        if g.get("error"):
            print(f"  ERROR: HTTP {g['status_code']}  {g['error'][:200]}")
        else:
            print(f"  messages returned:     {g.get('returned_count')}")
            print(f"  oldest timestamp:      {g.get('oldest_ts')}")
            print(f"  newest timestamp:      {g.get('newest_ts')}")
            print(f"  days of history:       {g.get('days_of_history_returned')}")
            print(f"  unique senders:        {g.get('unique_senders')}")
            print(f"  message types:         {g.get('message_types')}")
        print()

    print(f"Full JSON report: {OUTPUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
