"""Recover NULL overall_score values in lecture_scores.

The Claude-generated Georgian deep-analysis markdowns include a row for
"საერთო შეფასება" (overall score), but 5 rows in scores.db have this
column NULL. All 5 dimensions AND composite are populated — only the
LLM-written overall score is missing.

This script:
  1. Reads the Obsidian analysis markdown for each NULL row.
  2. Attempts multiple regex patterns to extract "საერთო შეფასება".
  3. Falls back to `composite` (computed average of 5 dimensions) if no
     match — composite is a fair numerical equivalent.
  4. DRY-RUN by default. Print proposed SQL without touching the DB.

Usage:
  python -m scripts.recover_null_scores                 # dry-run
  python -m scripts.recover_null_scores --apply         # actually write
  python -m scripts.recover_null_scores --verbose       # show text snippets
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "scores.db"
VAULT_ROOT = PROJECT_ROOT / "obsidian-vault" / "ანალიზი"


# Multiple patterns the LLM may produce; ordered by specificity.
OVERALL_PATTERNS = [
    # Table row: | **საერთო შეფასება** | 6.4/10 | ...
    re.compile(
        r"\|\s*\**\s*საერთო\s+შეფასება\s*\**\s*\|\s*\**\s*(\d+(?:\.\d+)?)\s*/\s*10\s*\**",
        re.UNICODE,
    ),
    # Prose: საერთო შეფასება: 6.4/10
    re.compile(
        r"საერთო\s+შეფასება[\s:—–-]+\**(\d+(?:\.\d+)?)\s*/\s*10\**",
        re.UNICODE,
    ),
    # Bold prose: **საერთო შეფასება**: **6.4**
    re.compile(
        r"\*\*საერთო\s+შეფასება\*\*[\s:—–-]+\**(\d+(?:\.\d+)?)",
        re.UNICODE,
    ),
    # Header style: ### საერთო შეფასება — 6.4
    re.compile(
        r"#{1,6}\s*საერთო\s+შეფასება[\s:—–-]+\**(\d+(?:\.\d+)?)",
        re.UNICODE,
    ),
]


def analysis_path(group: int, lecture: int) -> Path:
    return (
        VAULT_ROOT
        / f"ჯგუფი {group}"
        / f"ლექცია {lecture} -- ანალიზი.md"
    )


def try_extract(text: str) -> tuple[float | None, str | None]:
    """Return (score, pattern_name) or (None, None)."""
    for i, pat in enumerate(OVERALL_PATTERNS):
        m = pat.search(text)
        if m:
            try:
                val = float(m.group(1))
                if 0.0 <= val <= 10.0:
                    return val, f"pattern_{i}"
            except ValueError:
                continue
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually UPDATE the DB")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        return 2

    with closing(sqlite3.connect(str(DB_PATH))) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(
            """SELECT id, group_number, lecture_number, composite
               FROM lecture_scores
               WHERE overall_score IS NULL
               ORDER BY group_number, lecture_number"""
        )
        null_rows = cur.fetchall()

        if not null_rows:
            print("No NULL overall_score rows. Nothing to do.")
            return 0

        print(f"{'DRY-RUN' if not args.apply else 'APPLYING'} — {len(null_rows)} rows to process")
        print("=" * 70)

        proposed: list[tuple[int, float, str]] = []
        missing_files: list[str] = []

        for row in null_rows:
            g = row["group_number"]
            lec = row["lecture_number"]
            composite = row["composite"]
            path = analysis_path(g, lec)
            label = f"G{g} L{lec}"

            if not path.exists():
                print(f"  {label}: missing analysis file → {path}")
                missing_files.append(label)
                continue

            text = path.read_text(encoding="utf-8")
            extracted, pattern = try_extract(text)

            if extracted is not None:
                source = f"markdown ({pattern})"
                value = extracted
            else:
                source = "composite fallback"
                value = composite

            proposed.append((row["id"], value, f"{label} [{source}]"))
            print(f"  {label}: proposed overall_score={value:.2f}  (via {source}, composite={composite:.2f})")

            if args.verbose and extracted is not None:
                # Show 80-char snippet around match
                snippet_start = max(0, text.find(pattern.split('_')[0]) - 40) if pattern else 0
                snippet = text[snippet_start:snippet_start + 160].replace("\n", " ")
                print(f"    snippet: {snippet!r}")

        print("=" * 70)
        print(f"Summary: {len(proposed)} updates proposed, {len(missing_files)} missing analysis files")

        if missing_files:
            print(f"Missing files: {missing_files}")

        if not args.apply:
            print("\nDry-run complete. Re-run with --apply to write these values.")
            print("SQL that would be executed:")
            for row_id, val, label in proposed:
                print(f"  UPDATE lecture_scores SET overall_score={val:.2f} WHERE id={row_id};  -- {label}")
            return 0

        # Apply writes
        for row_id, val, label in proposed:
            cur.execute(
                "UPDATE lecture_scores SET overall_score = ? WHERE id = ? AND overall_score IS NULL",
                (val, row_id),
            )
            if cur.rowcount == 0:
                print(f"  WARNING: no update for id={row_id} ({label}) — row may have been set concurrently")
        conn.commit()
        print(f"\nApplied {len(proposed)} updates.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
