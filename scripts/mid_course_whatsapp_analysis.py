"""Mid-course WhatsApp pattern diagnostic.

Extracts high-signal content from local messages.db, layers in lecture scores
and concept entities, then asks Claude Opus to synthesise a Georgian
mid-course diagnostic report — actionable BEFORE the last 7 lectures run.

Design:
    1. SQL aggregation (no LLM cost) — weekly confusion counts, top
       askers, message type mix, time-of-day patterns.
    2. Signal-message extraction — pull the actual text of every
       confusion- or question-tagged message for Claude to read directly.
    3. Per-lecture scoring from scores.db joined with date windows.
    4. Single Claude Opus call with a structured analytical prompt.
    5. Output: obsidian-vault/ანალიზი/MID_COURSE_WHATSAPP_PATTERNS.md

Cost budget (rough): ~130k input tokens + ~20k output tokens on Opus 4.6
≈ $1.95 + $1.50 = ~$3.45 per run. Safe inside $16 balance.

Usage:
    python -m scripts.mid_course_whatsapp_analysis
    python -m scripts.mid_course_whatsapp_analysis --dry-run   # skip Claude call
    python -m scripts.mid_course_whatsapp_analysis --model haiku  # cheap mode
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MESSAGES_DB = PROJECT_ROOT / "data" / "messages.db"
SCORES_DB = PROJECT_ROOT / "data" / "scores.db"
OUT_DIR = PROJECT_ROOT / "obsidian-vault" / "ანალიზი"
OUT_PATH = OUT_DIR / "MID_COURSE_WHATSAPP_PATTERNS.md"

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# -------------------------------------------------------------------- #
# Signal lexicon — Georgian
# -------------------------------------------------------------------- #

CONFUSION_TOKENS = [
    "ვერ", "რატომ", "არ მესმი", "არ მუშაობ", "დახმარე",
    "რას ნიშნავს", "ვერ ვხვდები", "ვერ ვიგებ", "გაუგებ",
    "შეცდომა", "პრობლემა", "არ გამომდის", "არ გამოდის",
    "დაბლოკა", "რა ვქნა",
]

QUESTION_TOKEN = "?"

# Compiled regex for word-boundary-aware matching of Georgian confusion tokens.
# Georgian chars are ა–ჰ (U+10D0–U+10FF). We treat any Georgian letter or \w
# adjacent to a token as "inside a word" and skip the match, preventing false
# positives like "ვერ" matching inside "ვერცხლი" or "ვერანდა".
_CONFUSION_RE = re.compile(
    r"(?<![ა-ჰ\w])(?:" + "|".join(re.escape(t) for t in CONFUSION_TOKENS) + r")(?![ა-ჰ\w])",
    re.UNICODE,
)


def _contains_confusion(s: str) -> bool:
    """Return True if s contains a confusion token at a word boundary."""
    if not s:
        return False
    return bool(_CONFUSION_RE.search(s))


# -------------------------------------------------------------------- #
# Stats
# -------------------------------------------------------------------- #

def aggregate_stats(conn: sqlite3.Connection) -> dict:
    """Group-level volume, direction, sender diversity, message-type mix."""
    stats: dict = {"groups": {}}

    for g in (1, 2):
        group = {}
        rows = conn.execute(
            """SELECT direction, COUNT(*) AS n
               FROM messages WHERE group_number = ? GROUP BY direction""",
            (g,),
        ).fetchall()
        group["direction"] = {r["direction"]: r["n"] for r in rows}

        rows = conn.execute(
            """SELECT msg_type, COUNT(*) AS n
               FROM messages WHERE group_number = ?
               GROUP BY msg_type ORDER BY n DESC""",
            (g,),
        ).fetchall()
        group["msg_types"] = {r["msg_type"]: r["n"] for r in rows}

        rows = conn.execute(
            """SELECT COALESCE(sender_display, substr(sender_hash,1,10)) AS s,
                      COUNT(*) AS n
               FROM messages
               WHERE group_number = ? AND is_bot = 0
               GROUP BY sender_hash ORDER BY n DESC LIMIT 15""",
            (g,),
        ).fetchall()
        group["top_senders"] = [(r["s"], r["n"]) for r in rows]

        row = conn.execute(
            """SELECT MIN(DATE(ts_message)) AS first_day,
                      MAX(DATE(ts_message)) AS last_day,
                      COUNT(DISTINCT DATE(ts_message)) AS active_days
               FROM messages WHERE group_number = ?""",
            (g,),
        ).fetchone()
        group["date_range"] = dict(row)

        rows = conn.execute(
            """SELECT strftime('%Y-W%W', ts_message) AS week, COUNT(*) AS n
               FROM messages WHERE group_number = ?
               GROUP BY week ORDER BY week""",
            (g,),
        ).fetchall()
        group["messages_per_week"] = {r["week"]: r["n"] for r in rows}

        stats["groups"][g] = group

    return stats


def confusion_timeline(conn: sqlite3.Connection) -> dict:
    """Per-week counts of confusion- and question-signal messages."""
    result: dict = {"groups": {}}
    for g in (1, 2):
        weekly: dict[str, dict] = defaultdict(lambda: {"confusion": 0, "questions": 0, "total": 0})
        rows = conn.execute(
            """SELECT strftime('%Y-W%W', ts_message) AS week, content
               FROM messages
               WHERE group_number = ? AND is_bot = 0
                 AND content IS NOT NULL AND length(content) > 2""",
            (g,),
        ).fetchall()
        for r in rows:
            w = r["week"]
            c = r["content"]
            weekly[w]["total"] += 1
            if _contains_confusion(c):
                weekly[w]["confusion"] += 1
            if QUESTION_TOKEN in c:
                weekly[w]["questions"] += 1
        result["groups"][g] = dict(weekly)
    return result


def extract_signal_messages(conn: sqlite3.Connection, limit_per_group: int = 40) -> dict:
    """Sample the actual TEXT of the most-signalling messages per group.

    Heuristic: messages that contain a confusion token AND a question mark
    rank highest. Falls back to either one alone. Limits payload.
    """
    result: dict = {"groups": {}}
    for g in (1, 2):
        rows = conn.execute(
            """SELECT sender_display, DATE(ts_message) AS d,
                      content, ts_message
               FROM messages
               WHERE group_number = ? AND is_bot = 0
                 AND content IS NOT NULL
                 AND length(content) > 8
               ORDER BY ts_message""",
            (g,),
        ).fetchall()

        scored: list[tuple[int, sqlite3.Row]] = []
        for r in rows:
            c = r["content"]
            score = 0
            if _contains_confusion(c):
                score += 2
            if "?" in c:
                score += 1
            if score > 0:
                scored.append((score, r))

        # highest score first, then chronological within equal scores
        scored.sort(key=lambda x: (-x[0], x[1]["ts_message"]))
        picks = scored[:limit_per_group]
        result["groups"][g] = [
            {
                "date": r["d"],
                "sender": r["sender_display"],
                "text": r["content"][:280],
                "signal_score": s,
            }
            for s, r in picks
        ]
    return result


def load_scores() -> list[dict]:
    with closing(sqlite3.connect(str(SCORES_DB))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT group_number, lecture_number, content_depth, practical_value,
                       engagement, technical_accuracy, market_relevance,
                       overall_score, composite
               FROM lecture_scores ORDER BY group_number, lecture_number"""
        ).fetchall()
    return [dict(r) for r in rows]


# -------------------------------------------------------------------- #
# Prompt builder
# -------------------------------------------------------------------- #

PROMPT_TEMPLATE = """\
შენ ხარ ხარისხის ანალიტიკოსი საქართველოს AI კურსისთვის.

კურსის მდგომარეობა:
- ჯგუფი 1: 11 ლექცია გავლილი, 4 დარჩენილი
- ჯგუფი 2: 12 ლექცია გავლილი, 3 დარჩენილი
- სულ: 23/30 ლექცია

შენი მიზანი: გააანალიზო ქვემოთ მოცემული WhatsApp მონაცემები + ლექციების ქულები
და მომიმზადო **საფუძვლიანი mid-course დიაგნოსტიკა ქართულ ენაზე**, რომელიც:

1. აღმოაჩენს mesmayebi patterns ახლავე — სანამ დარჩენილი 7 ლექცია გავლილია
2. მიუთითებს რომელი კონკრეტული სტუდენტი ვინ საჭიროებს დახმარებას
3. გვეტყვის რა **კონკრეტული ცვლილება** უნდა შევიტანოთ დარჩენილ 7 ლექციაში

რეპორტის სტრუქტურა — ზუსტად ამ რიგით:

# სათაური: მიმდინარე დიაგნოსტიკა — ჯგუფი 1 და 2 (23 ლექციის შემდეგ)

## 1. TL;DR — 5 ყველაზე კრიტიკული მიგნება
მოკლედ, ბულეტებით, ყველაზე მნიშვნელოვანი რაც აღმოაჩინე.

## 2. აქტივობის და ჩართულობის სურათი
ჯგუფების შედარება, გამოიყენე რეალური ციფრები.

## 3. Confusion Peak Analysis
რატომ W13-ში (2026-04-01-დან 2026-04-07-მდე) ორივე ჯგუფმა პიკი აჩვენა. დაადარე მერე ქულებს. რომელი ლექცია იწვევდა ამ აღელვებას?

## 4. სტუდენტების სეგმენტაცია
- **Power users** — ყველაზე აქტიურები (კონკრეტული სახელები)
- **Silent majority** — ვინ ჩუმადაა
- **At-risk** — ვინ სვამს ბევრ confusion-სიგნალს
- **Dominators** — ვინ ზედმეტად დომინირებს (G2-ში Misho 231 და Nikoloz 145)

## 5. ძირითადი საკონცეფციო ხარვეზები
რა თემები ვერ გაიგეს სტუდენტებმა? მოიყვანე კონკრეტული მესიჯების ციტატები (2-3 თითოეულზე).

## 6. ჯგუფებს შორის განსხვავებები
G1 (bot dominant) vs G2 (student dominant) — რას ნიშნავს ეს? რა ხდება თითოეულში?

## 7. კონკრეტული რეკომენდაციები დარჩენილი 7 ლექციისთვის
5-8 **actionable** ცვლილება, რომლებიც ახლავე შესაძლებელია — ე.ი. ტრენერმა შეცვალოს მიდგომა კონკრეტულ რამეზე.

## 8. დარჩენილ 7 ლექციაზე რას უნდა მივაქციოთ ყურადღება
რომელი თემები უნდა გავიმეოროთ? რომელი სტუდენტები უნდა შევიცვრათ?

## 9. რისკების ნუსხა
რა გვემუქრება კურსის წარმატებას? რა ხდება თუ არ გავასწორებთ?

---

წესები:
- არანაირი ზოგადი pattern-ი. ყოველი მტკიცება მონაცემებით დამტკიცდება.
- გამოიყენე სტუდენტების ნამდვილი სახელები.
- გადმოიტანე კონკრეტული ციტატები როცა შესაფერისია.
- ტონი: პროფესიონალი, არა ქედმაღალი. ტრენერი (თორნიკე) თავად ხედავს ციფრებს — შენი ამოცანაა **სინთეზი და insight**, არა monitoring.

---

## მონაცემები

### აგრეგატული სტატისტიკა
{aggregates}

### კვირობრივი confusion & question timeline
{timeline}

### ლექციების ქულები (scores.db)
{scores}

### მაღალი signal-ის მესიჯების ნიმუში (ჯგუფი 1)
{signal_g1}

### მაღალი signal-ის მესიჯების ნიმუში (ჯგუფი 2)
{signal_g2}

---

დაწერე რეპორტი. ქართულად. markdown-ში. ამბიციური, მაგრამ რეალისტური.
"""


def build_prompt(stats: dict, timeline: dict, signals: dict, scores: list[dict]) -> str:
    def pretty(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)

    def sig_block(rows):
        return "\n".join(
            f"- [{r['date']} / {r['sender']}] ({r['signal_score']}): {r['text']}"
            for r in rows
        )

    return PROMPT_TEMPLATE.format(
        aggregates=pretty(stats),
        timeline=pretty(timeline),
        scores=pretty(scores),
        signal_g1=sig_block(signals["groups"][1]),
        signal_g2=sig_block(signals["groups"][2]),
    )


# -------------------------------------------------------------------- #
# Claude call
# -------------------------------------------------------------------- #

MODEL_MAP = {
    "opus": "claude-opus-4-5-20250514",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def call_claude(prompt: str, model_alias: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError:
        print("anthropic package missing. pip install anthropic", file=sys.stderr)
        sys.exit(2)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    model = MODEL_MAP.get(model_alias, model_alias)
    client = Anthropic(api_key=api_key)
    print(f"Calling Claude {model} ({len(prompt):,} char prompt)...", flush=True)

    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = resp.usage
    print(
        f"  tokens: in={usage.input_tokens:,} out={usage.output_tokens:,}",
        flush=True,
    )
    return "".join(block.text for block in resp.content if block.type == "text")


# -------------------------------------------------------------------- #
# Main
# -------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="opus", choices=list(MODEL_MAP.keys()))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with closing(sqlite3.connect(str(MESSAGES_DB))) as conn:
        conn.row_factory = sqlite3.Row
        print("Aggregating stats...", flush=True)
        stats = aggregate_stats(conn)
        timeline = confusion_timeline(conn)
        signals = extract_signal_messages(conn, limit_per_group=40)

    scores = load_scores()
    print(f"  stats loaded: G1={stats['groups'][1]['date_range']['active_days']}d, "
          f"G2={stats['groups'][2]['date_range']['active_days']}d, "
          f"scores={len(scores)}, signals G1={len(signals['groups'][1])} / G2={len(signals['groups'][2])}")

    prompt = build_prompt(stats, timeline, signals, scores)
    print(f"  prompt: {len(prompt):,} characters / ~{len(prompt)//4:,} tokens estimate")

    # Save prompt for reference
    prompt_path = OUT_DIR / ".mid_course_prompt.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    print(f"  prompt saved to {prompt_path}")

    if args.dry_run:
        print("DRY-RUN — skipping Claude call.")
        return 0

    report = call_claude(prompt, args.model)

    header = (
        f"> Generated by mid_course_whatsapp_analysis.py on "
        f"{datetime.now(tz=timezone.utc).isoformat()}\n"
        f"> Model: {MODEL_MAP[args.model]}\n"
        f"> Data source: data/messages.db (3000 msgs) + data/scores.db (23 lectures)\n\n---\n\n"
    )
    OUT_PATH.write_text(header + report, encoding="utf-8")
    print(f"\nReport written: {OUT_PATH}  ({len(report):,} chars)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
