"""Unified cross-layer query facade.

Binds the 5 memory layers into a single query surface:

    1. messages.db       — raw WhatsApp archive (per-student, per-lecture)
    2. scores.db         — 5-dim lecture ratings
    3. Obsidian vault    — deep analyses + concept entities
    4. Pinecone          — lecture content embeddings (RAG)  [optional]
    5. Mem0              — semantic memory fragments          [optional]

Philosophy: a single function per question-type, not a generic search.
"Ask me something, I go fetch from the right layers, I synthesise."
Pinecone and Mem0 are lazy-imported — this module works with only the
local DBs available, which is the common case in dev/analysis.

Primary entry points:
    student_journey(name)           → everything about one person
    lecture_context(group, lecture) → content + scores + chat around it
    topic_scan(pattern)             → keyword hits across all layers
    confusion_map(group=None)       → where students struggle, ranked
    silent_students(group)          → at-risk participants

All functions return plain dicts/lists — JSON-serialisable, easy to feed
to a prompt or render as Markdown.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MESSAGES_DB = PROJECT_ROOT / "data" / "messages.db"
SCORES_DB = PROJECT_ROOT / "data" / "scores.db"
OBSIDIAN_ROOT = PROJECT_ROOT / "obsidian-vault"

CONFUSION_TOKENS = [
    "ვერ", "რატომ", "არ მესმი", "არ მუშაობ", "დახმარე",
    "რას ნიშნავს", "ვერ ვხვდები", "ვერ ვიგებ", "გაუგებ",
    "შეცდომა", "პრობლემა", "არ გამომდის", "არ გამოდის",
    "დაბლოკა", "რა ვქნა",
]

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


# --------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------- #

def _relpath(p: Path) -> str:
    """Best-effort relative path; falls back to str(p) for paths outside root."""
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def _msg_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(MESSAGES_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _scores_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SCORES_DB))
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------- #
# Data classes for typed returns
# --------------------------------------------------------------------- #

@dataclass
class StudentSummary:
    sender_display: Optional[str]
    sender_hash: str
    group_numbers: list[int]
    total_messages: int
    first_seen: str
    last_seen: str
    confusion_count: int
    question_count: int
    top_lectures: list[tuple[int, int]]  # [(lecture_number, msg_count), ...]

    def to_dict(self) -> dict:
        return {
            "sender_display": self.sender_display,
            "sender_hash": self.sender_hash[:12] + "...",
            "groups": self.group_numbers,
            "total_messages": self.total_messages,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "confusion_count": self.confusion_count,
            "question_count": self.question_count,
            "most_active_lectures": self.top_lectures,
        }


# --------------------------------------------------------------------- #
# Public queries
# --------------------------------------------------------------------- #

def student_journey(name_or_substring: str, include_samples: int = 10) -> dict:
    """Everything we know about one student.

    Matches case-insensitively against sender_display. Returns summary +
    sample messages, confusion-signal messages, and per-lecture activity.
    """
    with _msg_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE sender_display LIKE ? COLLATE NOCASE
                 AND is_bot = 0
               ORDER BY ts_message""",
            (f"%{name_or_substring}%",),
        ).fetchall()

    if not rows:
        return {"error": f"no messages found for {name_or_substring!r}"}

    # Build summary
    by_hash: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_hash.setdefault(r["sender_hash"], []).append(r)

    journeys: list[dict] = []
    for sh, msgs in by_hash.items():
        groups = sorted({m["group_number"] for m in msgs if m["group_number"]})
        lect_counts: dict[int, int] = {}
        confusion = 0
        questions = 0
        samples: list[dict] = []
        confusion_samples: list[dict] = []

        for m in msgs:
            lec = m["lecture_context"]
            if lec:
                lect_counts[lec] = lect_counts.get(lec, 0) + 1
            content = m["content"] or ""
            is_conf = _contains_confusion(content)
            is_q = "?" in content
            if is_conf:
                confusion += 1
                if len(confusion_samples) < include_samples:
                    confusion_samples.append({
                        "ts": m["ts_message"],
                        "group": m["group_number"],
                        "lecture": m["lecture_context"],
                        "text": content[:240],
                    })
            if is_q:
                questions += 1
            if len(samples) < include_samples and content:
                samples.append({
                    "ts": m["ts_message"],
                    "group": m["group_number"],
                    "lecture": m["lecture_context"],
                    "text": content[:240],
                })

        summary = StudentSummary(
            sender_display=msgs[0]["sender_display"],
            sender_hash=sh,
            group_numbers=groups,
            total_messages=len(msgs),
            first_seen=msgs[0]["ts_message"],
            last_seen=msgs[-1]["ts_message"],
            confusion_count=confusion,
            question_count=questions,
            top_lectures=sorted(lect_counts.items(), key=lambda x: -x[1])[:5],
        )
        journeys.append({
            "summary": summary.to_dict(),
            "sample_messages": samples,
            "confusion_samples": confusion_samples,
        })
    return {"matches": journeys, "matched_hashes": len(journeys)}


def lecture_context(group_number: int, lecture_number: int) -> dict:
    """Full context for one lecture: score + WhatsApp activity + analysis file."""
    result: dict = {"group": group_number, "lecture": lecture_number}

    # 1. Score
    with _scores_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lecture_scores WHERE group_number=? AND lecture_number=?",
            (group_number, lecture_number),
        ).fetchone()
        if row:
            result["score"] = {
                "overall": row["overall_score"],
                "composite": row["composite"],
                "dimensions": {
                    "content_depth": row["content_depth"],
                    "practical_value": row["practical_value"],
                    "engagement": row["engagement"],
                    "technical_accuracy": row["technical_accuracy"],
                    "market_relevance": row["market_relevance"],
                },
            }
        else:
            result["score"] = None

    # 2. WhatsApp window stats
    with _msg_conn() as conn:
        window_row = conn.execute(
            """SELECT started_at, ends_at FROM lecture_windows
               WHERE group_number=? AND lecture_number=?""",
            (group_number, lecture_number),
        ).fetchone()
        if window_row:
            result["window"] = {
                "started_at": window_row["started_at"],
                "ends_at": window_row["ends_at"],
            }

        rows = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN is_bot=0 THEN 1 ELSE 0 END) student_msgs,
                      SUM(CASE WHEN is_bot=1 THEN 1 ELSE 0 END) bot_msgs,
                      COUNT(DISTINCT sender_hash) unique_senders
               FROM messages
               WHERE group_number=? AND lecture_context=?""",
            (group_number, lecture_number),
        ).fetchone()
        result["chat_stats"] = dict(rows) if rows else None

        # Confusion samples — SQL LIKE broad-matches, then Python regex
        # post-filters to drop word-boundary false positives.
        sql_samples = conn.execute(
            """SELECT sender_display, ts_message, content
               FROM messages
               WHERE group_number=? AND lecture_context=? AND is_bot=0
                 AND (content LIKE '%ვერ%' OR content LIKE '%რატომ%'
                      OR content LIKE '%არ მესმი%' OR content LIKE '%დახმარე%')
               ORDER BY ts_message LIMIT 50""",
            (group_number, lecture_number),
        ).fetchall()
        result["confusion_samples"] = [
            {"sender": r["sender_display"], "ts": r["ts_message"], "text": (r["content"] or "")[:240]}
            for r in sql_samples
            if _contains_confusion(r["content"] or "")
        ][:10]

    # 3. Obsidian analysis file
    analysis_path = (
        OBSIDIAN_ROOT / "ანალიზი" / f"ჯგუფი {group_number}"
        / f"ლექცია {lecture_number} -- ანალიზი.md"
    )
    if analysis_path.exists():
        text = analysis_path.read_text(encoding="utf-8")
        result["analysis_file"] = {
            "path": _relpath(analysis_path),
            "size_bytes": len(text.encode("utf-8")),
            "first_500": text[:500],
        }
    else:
        result["analysis_file"] = None

    return result


def topic_scan(pattern: str, limit: int = 20) -> dict:
    """Keyword scan across messages + analysis files.

    Returns up to `limit` hits per layer. Not semantic — that's Pinecone's job.
    """
    like_pattern = f"%{pattern}%"
    result: dict = {"pattern": pattern, "messages": [], "analyses": []}

    with _msg_conn() as conn:
        rows = conn.execute(
            """SELECT group_number, lecture_context, sender_display,
                      DATE(ts_message) AS d, content
               FROM messages
               WHERE content LIKE ? AND is_bot = 0
               ORDER BY ts_message DESC LIMIT ?""",
            (like_pattern, limit),
        ).fetchall()
        result["messages"] = [
            {
                "group": r["group_number"],
                "lecture": r["lecture_context"],
                "sender": r["sender_display"],
                "date": r["d"],
                "snippet": (r["content"] or "")[:200],
            }
            for r in rows
        ]

    # Grep through Obsidian analysis markdowns
    pat_lower = pattern.lower()
    for md in (OBSIDIAN_ROOT / "ანალიზი").rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        lower = text.lower()
        idx = lower.find(pat_lower)
        if idx >= 0:
            snippet_start = max(0, idx - 80)
            result["analyses"].append({
                "file": _relpath(md),
                "snippet": text[snippet_start:snippet_start + 240].replace("\n", " "),
            })
            if len(result["analyses"]) >= limit:
                break

    return result


def confusion_map(group_number: Optional[int] = None, top_n: int = 15) -> dict:
    """Ranked list of students by confusion-signal message count.

    SQL LIKE performs a broad first pass; Python regex post-filters each
    candidate message to drop word-boundary false positives before counting.
    """
    clause = ""
    group_params: list[Any] = []
    if group_number is not None:
        clause = " AND group_number = ?"
        group_params.append(group_number)

    # Broad SQL pass — fetch matching rows (not just counts) so we can
    # post-filter with the regex before aggregating.
    tok_clause = " OR ".join("content LIKE ?" for _ in CONFUSION_TOKENS)
    like_params: list[Any] = [f"%{t}%" for t in CONFUSION_TOKENS]

    sql = f"""SELECT COALESCE(sender_display, substr(sender_hash,1,10)) AS s,
                     sender_hash, group_number, content
              FROM messages
              WHERE is_bot = 0 AND ({tok_clause}){clause}"""

    with _msg_conn() as conn:
        rows = conn.execute(sql, like_params + group_params).fetchall()

    # Aggregate after regex post-filter
    counts: dict[tuple[str, int | None], tuple[str, int]] = {}
    for r in rows:
        if not _contains_confusion(r["content"] or ""):
            continue
        key = (r["sender_hash"], r["group_number"])
        sender_label, n = counts.get(key, (r["s"], 0))
        counts[key] = (sender_label, n + 1)

    ranking = sorted(counts.values(), key=lambda x: -x[1])[:top_n]
    return {
        "filter": {"group": group_number, "top_n": top_n},
        "ranking": [
            {"sender": s, "group": group_number, "confusion_msgs": n}
            for s, n in ranking
        ],
    }


def silent_students(group_number: int, threshold: int = 10) -> dict:
    """Students with fewer than `threshold` messages (excluding bots)."""
    with _msg_conn() as conn:
        rows = conn.execute(
            """SELECT COALESCE(sender_display, substr(sender_hash,1,10)) AS s,
                      COUNT(*) AS n,
                      MIN(ts_message) AS first_seen,
                      MAX(ts_message) AS last_seen
               FROM messages
               WHERE group_number = ? AND is_bot = 0
               GROUP BY sender_hash
               HAVING n < ?
               ORDER BY n""",
            (group_number, threshold),
        ).fetchall()
    return {
        "group": group_number,
        "threshold": threshold,
        "silent": [
            {"sender": r["s"], "msg_count": r["n"],
             "first_seen": r["first_seen"][:10], "last_seen": r["last_seen"][:10]}
            for r in rows
        ],
    }


def course_overview() -> dict:
    """High-level snapshot — for any consumer needing the stack in one call."""
    with _scores_conn() as conn:
        scores = [dict(r) for r in conn.execute(
            "SELECT group_number, lecture_number, overall_score, composite "
            "FROM lecture_scores ORDER BY group_number, lecture_number"
        )]

    with _msg_conn() as conn:
        stats = dict(conn.execute(
            "SELECT COUNT(*) total, "
            "       SUM(CASE WHEN group_number=1 THEN 1 ELSE 0 END) g1, "
            "       SUM(CASE WHEN group_number=2 THEN 1 ELSE 0 END) g2, "
            "       COUNT(DISTINCT sender_hash) unique_senders "
            "FROM messages"
        ).fetchone())
        windows = [dict(r) for r in conn.execute(
            "SELECT * FROM lecture_windows ORDER BY group_number, lecture_number"
        )]

    return {
        "messages": stats,
        "scores": scores,
        "lecture_windows": len(windows),
    }
