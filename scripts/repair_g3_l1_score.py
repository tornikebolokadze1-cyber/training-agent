"""One-shot repair for the G3 / Lecture 1 row in ``data/scores.db``.

Background (ralph 2026-05-13 production hardening, US-002):

  The G3 first-lecture row was persisted with ``overall_score=NULL`` because the
  Georgian deep-analysis text used "კომპოზიტური ქულა" (composite) instead of
  "საერთო შეფასება" (overall) — the regex in ``tools/services/analytics.py``
  only knows the latter. All five dimension scores AND the composite are
  present and correct; only ``overall_score`` is missing.

  Composite (9.4) IS the analysis's stated overall in this case — the same
  fallback ``scripts/recover_null_scores.py`` already uses when no markdown
  match is found. This script does the same thing but targeted at G3/L1, with
  a Pinecone-first attempt so a freshly-written ``საერთო შეფასება`` row
  (if Codex re-runs analysis later) supersedes the fallback.

Usage::

    python -m scripts.repair_g3_l1_score                # dry-run (default)
    python -m scripts.repair_g3_l1_score --execute      # actually UPDATE the DB

The script is idempotent: re-running after a successful repair finds
``overall_score IS NOT NULL`` and exits 0 with "nothing to do".
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "scores.db"

TARGET_GROUP = 3
TARGET_LECTURE = 1

logger = logging.getLogger("repair_g3_l1_score")


# Same overall-score patterns the analytics module knows about, expanded to
# also tolerate the bold-prose form some Claude outputs use.
_OVERALL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\|\s*\**\s*საერთო\s+შეფასება\s*\**\s*\|\s*\**\s*(\d+(?:\.\d+)?)\s*/\s*10",
        re.UNICODE,
    ),
    re.compile(
        r"საერთო\s+შეფასება[\s:—–-]+\**(\d+(?:\.\d+)?)\s*/\s*10",
        re.UNICODE,
    ),
    re.compile(
        r"\*\*საერთო\s+შეფასება\*\*[\s:—–-]+\**(\d+(?:\.\d+)?)",
        re.UNICODE,
    ),
]


def _extract_overall(text: str) -> float | None:
    """Try each overall-score pattern; return the first valid 0–10 match."""
    for pat in _OVERALL_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        if 0.0 <= val <= 10.0:
            return val
    return None


def _try_pinecone_text(group: int, lecture: int) -> str | None:
    """Reconstruct deep_analysis text from Pinecone, if available.

    Returns None when Pinecone is unreachable, the lecture is not indexed,
    or the reconstructed text is implausibly short.
    """
    try:
        from tools.integrations.knowledge_indexer import get_pinecone_index
    except Exception as exc:
        logger.info("Pinecone module unavailable (%s) — skipping Pinecone lookup", exc)
        return None

    try:
        idx = get_pinecone_index()
    except Exception as exc:
        logger.info("Pinecone connection failed (%s) — skipping Pinecone lookup", exc)
        return None

    prefix = f"g{group}_l{lecture}_deep_analysis_"
    try:
        all_ids: list[str] = []
        for page in idx.list(prefix=prefix, limit=99):
            all_ids.extend(page)
    except Exception as exc:
        logger.info("Pinecone list error (%s) — skipping", exc)
        return None

    if not all_ids:
        logger.info("Pinecone has no deep_analysis chunks for G%dL%d", group, lecture)
        return None

    try:
        fetched = idx.fetch(ids=all_ids)
    except Exception as exc:
        logger.info("Pinecone fetch error (%s) — skipping", exc)
        return None

    chunks: list[tuple[int, str]] = []
    for _vid, vec in fetched.vectors.items():
        meta = vec.metadata or {}
        chunks.append((int(meta.get("chunk_index", 0)), str(meta.get("text", ""))))
    chunks.sort(key=lambda x: x[0])
    full_text = "\n".join(t for _, t in chunks)

    if len(full_text.strip()) < 200:
        logger.info("Reconstructed text too short (%d chars)", len(full_text))
        return None

    return full_text


def _fetch_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(
        "SELECT id, group_number, lecture_number, composite, overall_score, raw_score_text "
        "FROM lecture_scores WHERE group_number = ? AND lecture_number = ?",
        (TARGET_GROUP, TARGET_LECTURE),
    ).fetchone()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repair NULL overall_score for G3 lecture 1 in scores.db."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually UPDATE the DB. Without this flag the script is a dry-run.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable DEBUG-level logging."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not DB_PATH.exists():
        logger.error("scores.db not found at %s", DB_PATH)
        return 2

    with closing(sqlite3.connect(str(DB_PATH))) as conn:
        conn.row_factory = sqlite3.Row
        row = _fetch_row(conn)

        if row is None:
            logger.error(
                "G%d/L%d row does NOT exist in scores.db — cannot repair a missing row",
                TARGET_GROUP,
                TARGET_LECTURE,
            )
            return 3

        before = row["overall_score"]
        composite = row["composite"]
        raw_text = row["raw_score_text"] or ""

        logger.info(
            "BEFORE: G%d/L%d  overall_score=%s  composite=%s  raw_score_text_len=%d",
            TARGET_GROUP,
            TARGET_LECTURE,
            before,
            composite,
            len(raw_text),
        )

        if before is not None:
            logger.info("Nothing to do — overall_score is already %.2f", before)
            return 0

        # Step 1: try Pinecone for a fresh deep_analysis text.
        new_value: float | None = None
        source = ""
        pine_text = _try_pinecone_text(TARGET_GROUP, TARGET_LECTURE)
        if pine_text:
            extracted = _extract_overall(pine_text)
            if extracted is not None:
                new_value = extracted
                source = "pinecone:საერთო შეფასება pattern"

        # Step 2: try the raw_score_text already stored on the row.
        if new_value is None:
            extracted = _extract_overall(raw_text)
            if extracted is not None:
                new_value = extracted
                source = "scores.db.raw_score_text:საერთო შეფასება pattern"

        # Step 3: fall back to composite (same policy as recover_null_scores.py).
        if new_value is None:
            new_value = float(composite)
            source = "composite fallback (analysis text used კომპოზიტური ქულა)"

        logger.info(
            "PROPOSED: overall_score=%.2f  via %s",
            new_value,
            source,
        )

        if not args.execute:
            logger.warning("DRY-RUN — pass --execute to write. No changes made.")
            return 0

        conn.execute(
            "UPDATE lecture_scores SET overall_score = ? "
            "WHERE group_number = ? AND lecture_number = ?",
            (new_value, TARGET_GROUP, TARGET_LECTURE),
        )
        conn.commit()

        after_row = _fetch_row(conn)
        after = after_row["overall_score"] if after_row else None
        logger.info(
            "AFTER:  G%d/L%d  overall_score=%s  (was %s)",
            TARGET_GROUP,
            TARGET_LECTURE,
            after,
            before,
        )

        if after is None:
            logger.error("Repair appears to have failed — overall_score still NULL")
            return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
