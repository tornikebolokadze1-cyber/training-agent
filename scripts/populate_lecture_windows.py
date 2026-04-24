"""Populate lecture_windows + messages.lecture_context.

Derives lecture dates from the known cron pattern (G1 Tue+Fri, G2 Mon+Thu)
starting from the first observed lecture week. Each window spans from a
lecture's timestamp to the next lecture's timestamp (so any WhatsApp
message in that window belongs to that lecture's "context").

Idempotent: re-running replaces the windows cleanly and re-tags messages.

Usage:
    python -m scripts.populate_lecture_windows              # apply
    python -m scripts.populate_lecture_windows --dry-run    # preview
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "messages.db"

# --------------------------------------------------------------------- #
# Schedule: known from scheduler.py cron
# --------------------------------------------------------------------- #
# Group 1: Tuesday + Friday
# Group 2: Monday + Thursday
# Derived first-lecture date from earliest messages + processed_at:
#   G1 first message 2026-03-21 (Saturday) → L1 on Fri 2026-03-20
#   G2 first message 2026-03-29 (Sunday)   → L1 on Mon 2026-03-23
# Tbilisi lectures are evening; we use UTC 16:00 as a conservative start.

G1_FIRST_LECTURE = datetime(2026, 3, 20, 16, 0, tzinfo=timezone.utc)  # Fri
G1_WEEKDAYS = {1, 4}  # Tuesday=1, Friday=4 (Mon=0)

G2_FIRST_LECTURE = datetime(2026, 3, 23, 16, 0, tzinfo=timezone.utc)  # Mon
G2_WEEKDAYS = {0, 3}  # Monday=0, Thursday=3


def generate_lecture_dates(
    start: datetime, weekdays: set[int], count: int
) -> list[datetime]:
    """Step forward day by day, collect dates whose weekday is in set."""
    dates: list[datetime] = []
    day = start
    # Ensure start itself is included if it matches
    while len(dates) < count:
        if day.weekday() in weekdays:
            dates.append(day)
        day += timedelta(days=1)
        if len(dates) >= count:
            break
    return dates[:count]


def build_windows() -> list[tuple[int, int, str, str]]:
    """Return [(group, lecture, started_at_iso, ends_at_iso), ...]"""
    rows: list[tuple[int, int, str, str]] = []

    for group, first, weekdays, count in (
        (1, G1_FIRST_LECTURE, G1_WEEKDAYS, 11),
        (2, G2_FIRST_LECTURE, G2_WEEKDAYS, 12),
    ):
        dates = generate_lecture_dates(first, weekdays, count + 1)
        # the +1 gives us the NEXT lecture start so we can bound the last window
        for i in range(count):
            started = dates[i]
            # end = next lecture start OR +7 days for the last lecture
            if i + 1 < len(dates):
                ends = dates[i + 1]
            else:
                ends = started + timedelta(days=7)
            rows.append((group, i + 1, started.isoformat(), ends.isoformat()))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = build_windows()
    print(f"Generated {len(rows)} lecture windows:")
    for g, lec, s, e in rows:
        print(f"  G{g} L{lec:>2}: {s[:10]} → {e[:10]}")

    if args.dry_run:
        print("\nDRY-RUN — no changes written.")
        return 0

    with closing(sqlite3.connect(str(DB_PATH))) as conn:
        # Replace all windows — idempotent
        conn.execute("DELETE FROM lecture_windows")
        conn.executemany(
            """INSERT INTO lecture_windows
               (group_number, lecture_number, started_at, ends_at)
               VALUES (?, ?, ?, ?)""",
            rows,
        )

        # Backfill lecture_context in messages (null out first to re-tag cleanly)
        conn.execute("UPDATE messages SET lecture_context = NULL")
        conn.execute(
            """UPDATE messages
                  SET lecture_context = (
                    SELECT lw.lecture_number
                    FROM lecture_windows lw
                    WHERE lw.group_number = messages.group_number
                      AND messages.ts_message >= lw.started_at
                      AND messages.ts_message <  lw.ends_at
                    LIMIT 1
                  )
                WHERE group_number IS NOT NULL"""
        )
        conn.commit()

        # Report coverage
        print("\nMessage → lecture coverage:")
        cur = conn.execute(
            """SELECT group_number, lecture_context, COUNT(*) AS n
               FROM messages WHERE group_number IS NOT NULL
               GROUP BY group_number, lecture_context
               ORDER BY group_number, lecture_context"""
        )
        for row in cur:
            g, lec, n = row
            tag = f"L{lec}" if lec is not None else "UNTAGGED"
            print(f"  G{g} {tag:>10}: {n} messages")

        row = conn.execute(
            """SELECT COUNT(*) FROM messages
               WHERE group_number IS NOT NULL AND lecture_context IS NULL"""
        ).fetchone()
        untagged = row[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE group_number IS NOT NULL"
        ).fetchone()[0]
        pct = 100 * (total - untagged) / total if total else 0
        print(f"\nTagged: {total - untagged}/{total} ({pct:.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
