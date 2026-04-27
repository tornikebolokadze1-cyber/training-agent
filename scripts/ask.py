"""CLI for the unified memory layer.

Answers common questions about the training course by combining
messages.db, scores.db, lecture_windows, and Obsidian analyses.

Usage:
    # Find everything about one student
    python -m scripts.ask student "Shorena"

    # Full context for a lecture
    python -m scripts.ask lecture 1 7

    # Keyword scan across messages + analyses
    python -m scripts.ask topic "skills"

    # Who struggles most?
    python -m scripts.ask confusion --group 2 --top 10

    # Who's quiet?
    python -m scripts.ask silent --group 1 --threshold 5

    # Quick snapshot of the stack
    python -m scripts.ask overview

    # Output format: --json emits raw structures
    python -m scripts.ask student "Misho" --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.services import unified_query as uq


def _as_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------- #
# Pretty printers
# --------------------------------------------------------------------- #

def print_student(r: dict) -> None:
    if "error" in r:
        print(r["error"])
        return
    for m in r["matches"]:
        s = m["summary"]
        print(f"\n=== {s['sender_display']} ===")
        print(f"  Total messages:   {s['total_messages']}")
        print(f"  Groups:           {s['groups']}")
        print(f"  Active window:    {s['first_seen'][:10]} → {s['last_seen'][:10]}")
        print(f"  Confusion signals: {s['confusion_count']}")
        print(f"  Questions asked:  {s['question_count']}")
        print(f"  Most active lectures: {s['most_active_lectures']}")
        if m["sample_messages"]:
            print("  Recent sample:")
            for sm in m["sample_messages"][:3]:
                print(f"    [L{sm['lecture']} / {sm['ts'][:10]}] {sm['text'][:100]}")
        if m["confusion_samples"]:
            print("  Confusion quotes:")
            for cs in m["confusion_samples"][:3]:
                print(f"    [L{cs['lecture']}] {cs['text'][:100]}")


def print_lecture(r: dict) -> None:
    print(f"\n=== Group {r['group']} Lecture {r['lecture']} ===")
    if r.get("score"):
        s = r["score"]
        print(f"  Score: overall={s['overall']}  composite={s['composite']}")
        print(f"    dims: {s['dimensions']}")
    if r.get("window"):
        w = r["window"]
        print(f"  Window: {w['started_at'][:10]} → {w['ends_at'][:10]}")
    if r.get("chat_stats"):
        cs = r["chat_stats"]
        print(f"  Chat: {cs['n']} messages ({cs['student_msgs']} student / {cs['bot_msgs']} bot), {cs['unique_senders']} senders")
    if r.get("confusion_samples"):
        print("  Confusion quotes:")
        for cs in r["confusion_samples"][:5]:
            print(f"    [{cs['sender']}] {cs['text'][:100]}")
    if r.get("analysis_file"):
        af = r["analysis_file"]
        print(f"  Analysis: {af['path']}  ({af['size_bytes']:,} bytes)")


def print_topic(r: dict) -> None:
    print(f"\n=== Topic: '{r['pattern']}' ===")
    print(f"\nMessage hits ({len(r['messages'])}):")
    for m in r["messages"]:
        lect = f"L{m['lecture']}" if m['lecture'] else "no-lec"
        print(f"  [G{m['group']} {lect} / {m['date']} / {m['sender']}]: {m['snippet'][:120]}")
    print(f"\nAnalysis hits ({len(r['analyses'])}):")
    for a in r["analyses"]:
        print(f"  [{a['file']}]")
        print(f"    {a['snippet'][:200]}")


def print_confusion(r: dict) -> None:
    print(f"\n=== Confusion ranking (group={r['filter']['group']}) ===")
    for i, row in enumerate(r["ranking"], 1):
        print(f"  {i:>2}. G{row['group']} {row['sender']}: {row['confusion_msgs']} confusion msgs")


def print_silent(r: dict) -> None:
    print(f"\n=== Silent in G{r['group']} (<{r['threshold']} messages) ===")
    for row in r["silent"]:
        print(f"  {row['sender']}: {row['msg_count']} msgs  ({row['first_seen']} → {row['last_seen']})")


def print_overview(r: dict) -> None:
    print("\n=== Course Overview ===")
    m = r["messages"]
    print(f"  Messages: {m['total']} total ({m['g1']} G1 / {m['g2']} G2), {m['unique_senders']} unique senders")
    print(f"  Scores:   {len(r['scores'])} lectures")
    print(f"  Windows:  {r['lecture_windows']} lecture windows")
    print()
    print("  Per-lecture overall scores:")
    for s in r["scores"]:
        val = s.get("overall_score") or s.get("composite") or 0
        bar = "█" * int(val * 1.5)
        print(f"    G{s['group_number']} L{s['lecture_number']:>2}: {val:>4.1f}  {bar}")


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="output raw JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("student", help="find everything about a student")
    sp.add_argument("name")

    sp = sub.add_parser("lecture", help="full context of a lecture")
    sp.add_argument("group", type=int)
    sp.add_argument("lecture", type=int)

    sp = sub.add_parser("topic", help="keyword scan across messages + analyses")
    sp.add_argument("pattern")
    sp.add_argument("--limit", type=int, default=10)

    sp = sub.add_parser("confusion", help="ranked confusion signals by sender")
    sp.add_argument("--group", type=int, default=None)
    sp.add_argument("--top", type=int, default=10)

    sp = sub.add_parser("silent", help="students below message threshold")
    sp.add_argument("--group", type=int, required=True)
    sp.add_argument("--threshold", type=int, default=10)

    sub.add_parser("overview", help="course stack snapshot")

    args = p.parse_args()

    if args.cmd == "student":
        r = uq.student_journey(args.name)
        printer = print_student
    elif args.cmd == "lecture":
        r = uq.lecture_context(args.group, args.lecture)
        printer = print_lecture
    elif args.cmd == "topic":
        r = uq.topic_scan(args.pattern, limit=args.limit)
        printer = print_topic
    elif args.cmd == "confusion":
        r = uq.confusion_map(group_number=args.group, top_n=args.top)
        printer = print_confusion
    elif args.cmd == "silent":
        r = uq.silent_students(args.group, threshold=args.threshold)
        printer = print_silent
    elif args.cmd == "overview":
        r = uq.course_overview()
        printer = print_overview
    else:
        p.print_help()
        return 2

    if args.json:
        print(_as_json(r))
    else:
        printer(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
