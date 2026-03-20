"""Analytics module: lecture score storage, statistics, and dashboard.

Provides:
- Score extraction from Georgian deep analysis text (regex)
- SQLite persistence (data/scores.db)
- Statistical calculations (stdlib only — no numpy/scipy)
- Dashboard HTML generation (Chart.js via CDN)
- Backfill from .tmp/ files and Pinecone metadata

The SQLite DB is ephemeral on Railway restarts; backfill_from_tmp()
restores scores from analysis text files on startup.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape as _esc

from tools.core.config import PROJECT_ROOT, TMP_DIR

logger = logging.getLogger(__name__)

DB_PATH = PROJECT_ROOT / "data" / "scores.db"

DIMENSIONS = [
    "content_depth",
    "practical_value",
    "engagement",
    "technical_accuracy",
    "market_relevance",
]

DIMENSION_LABELS_KA = {
    "content_depth": "შინაარსის სიღრმე",
    "practical_value": "პრაქტიკული ღირებულება",
    "engagement": "მონაწილეების ჩართულობა",
    "technical_accuracy": "ტექნიკური სიზუსტე",
    "market_relevance": "ბაზრის რელევანტურობა",
    "composite": "კომპოზიტური ქულა",
}

DIMENSION_LABELS_EN = {
    "content_depth": "Content Depth",
    "practical_value": "Practical Value",
    "engagement": "Engagement",
    "technical_accuracy": "Technical Accuracy",
    "market_relevance": "Market Relevance",
    "composite": "Composite",
}

# ---------------------------------------------------------------------------
# Regex patterns for score extraction
# ---------------------------------------------------------------------------

# Maps DB column name → Georgian label pattern as it appears in the score table.
# Order matters: we match each row independently, so order only affects logs.
_DIMENSION_PATTERNS: list[tuple[str, str]] = [
    ("content_depth",       r"შინაარსის\s+სიღრმე"),
    ("practical_value",     r"პრაქტიკული\s+ღირებულება"),
    ("engagement",          r"მონაწილეე?ბ[ი]?\s*[სთ]?\s*ჩართულობა"),
    ("technical_accuracy",  r"ტექნიკური\s+სიზუსტე"),
    ("market_relevance",    r"ბაზრის\s+(?:რელევანტ\w+|შესაბამისობა)"),
    ("overall_score",       r"საერთო\s+შეფასება"),
]

# Matches a table row: | anything containing label | N/10 or **N/10** | anything |
_ROW_TEMPLATE = r"\|[^\|]*{label}[^\|]*\|\s*\**(\d+(?:\.\d+)?)/10\**\s*\|"


def extract_scores(deep_analysis_text: str) -> dict[str, float] | None:
    """Extract 5-dimensional scores from Georgian deep analysis text.

    The score table format is:
        | **შინაარსის სიღრმე** | 4/10 | justification |

    Returns:
        Dict with keys matching DB columns (content_depth, practical_value,
        engagement, technical_accuracy, market_relevance, and optionally
        overall_score), or None if any required dimension is missing.
    """
    results: dict[str, float] = {}

    for col, label_pattern in _DIMENSION_PATTERNS:
        pattern = _ROW_TEMPLATE.format(label=label_pattern)
        match = re.search(pattern, deep_analysis_text, re.UNICODE | re.IGNORECASE)
        if match:
            results[col] = float(match.group(1))
        else:
            logger.warning("Score extraction: could not find '%s' in deep analysis", col)

    required = set(DIMENSIONS)
    if not required.issubset(results.keys()):
        missing = required - results.keys()
        logger.error(
            "Score extraction failed: missing dimensions %s. "
            "Text snippet (first 300 chars): %.300s",
            missing,
            deep_analysis_text,
        )
        return None

    return results


def _capture_score_table(text: str) -> str | None:
    """Extract the raw score table substring for audit storage."""
    match = re.search(
        r"(\|[^\n]*ქულ[^\n]*\n(?:\|[^\n]*\n){4,8})",
        text,
        re.UNICODE,
    )
    if match:
        return match.group(0)[:2000]
    # Fallback: grab lines containing /10
    lines = [ln for ln in text.splitlines() if "/10" in ln]
    return "\n".join(lines[:10]) if lines else None


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lecture_scores (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    group_number       INTEGER NOT NULL CHECK (group_number IN (1, 2)),
    lecture_number     INTEGER NOT NULL CHECK (lecture_number BETWEEN 1 AND 15),
    content_depth      REAL    NOT NULL,
    practical_value    REAL    NOT NULL,
    engagement         REAL    NOT NULL,
    technical_accuracy REAL    NOT NULL,
    market_relevance   REAL    NOT NULL,
    overall_score      REAL,
    composite          REAL    NOT NULL,
    raw_score_text     TEXT,
    processed_at       TEXT    NOT NULL,
    UNIQUE (group_number, lecture_number)
);
CREATE INDEX IF NOT EXISTS idx_group_lecture
    ON lecture_scores (group_number, lecture_number);

CREATE TABLE IF NOT EXISTS lecture_insights (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    group_number            INTEGER NOT NULL,
    lecture_number           INTEGER NOT NULL,
    strengths_count         INTEGER DEFAULT 0,
    weaknesses_count        INTEGER DEFAULT 0,
    gaps_count              INTEGER DEFAULT 0,
    recommendations_count   INTEGER DEFAULT 0,
    tech_correct_count      INTEGER DEFAULT 0,
    tech_problematic_count  INTEGER DEFAULT 0,
    blind_spots_count       INTEGER DEFAULT 0,
    top_strength            TEXT,
    top_weakness            TEXT,
    key_recommendation      TEXT,
    score_justifications    TEXT,
    extracted_at            TEXT NOT NULL,
    UNIQUE (group_number, lecture_number)
);
"""


def init_db() -> None:
    """Create data/ directory and lecture_scores table if absent."""
    DB_PATH.parent.mkdir(exist_ok=True)
    with _get_conn() as conn:
        conn.executescript(_SCHEMA)
    logger.info("Analytics DB initialized at %s", DB_PATH)


@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def upsert_scores(
    group_number: int,
    lecture_number: int,
    content_depth: float,
    practical_value: float,
    engagement: float,
    technical_accuracy: float,
    market_relevance: float,
    overall_score: float | None = None,
    raw_score_text: str | None = None,
) -> None:
    """Insert or replace score row for a lecture."""
    composite = round(
        (content_depth + practical_value + engagement + technical_accuracy + market_relevance) / 5,
        2,
    )
    processed_at = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO lecture_scores
                (group_number, lecture_number, content_depth, practical_value,
                 engagement, technical_accuracy, market_relevance,
                 overall_score, composite, raw_score_text, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_number, lecture_number,
                content_depth, practical_value, engagement,
                technical_accuracy, market_relevance,
                overall_score, composite, raw_score_text, processed_at,
            ),
        )
    logger.info(
        "Saved scores — Group %d, Lecture #%d | composite=%.1f",
        group_number, lecture_number, composite,
    )


def save_scores_from_analysis(
    group_number: int,
    lecture_number: int,
    deep_analysis_text: str,
) -> bool:
    """Extract scores from Georgian deep analysis text and persist.

    Returns True on success, False if extraction failed (non-fatal caller).
    """
    if not deep_analysis_text or len(deep_analysis_text.strip()) < 100:
        logger.warning(
            "save_scores_from_analysis: text too short (%d chars) for Group %d Lecture #%d",
            len(deep_analysis_text), group_number, lecture_number,
        )
        return False

    scores = extract_scores(deep_analysis_text)
    if scores is None:
        return False

    upsert_scores(
        group_number=group_number,
        lecture_number=lecture_number,
        content_depth=scores["content_depth"],
        practical_value=scores["practical_value"],
        engagement=scores["engagement"],
        technical_accuracy=scores["technical_accuracy"],
        market_relevance=scores["market_relevance"],
        overall_score=scores.get("overall_score"),
        raw_score_text=_capture_score_table(deep_analysis_text),
    )
    # Also extract and persist qualitative insights
    extract_and_save_insights(group_number, lecture_number, deep_analysis_text)
    return True


# ---------------------------------------------------------------------------
# Insight extraction from deep analysis text
# ---------------------------------------------------------------------------

def _count_pattern_items(text: str, section_pattern: str) -> int:
    """Count numbered/bulleted items in a section of the analysis."""
    match = re.search(section_pattern, text, re.UNICODE | re.DOTALL)
    if not match:
        return 0
    section = match.group(0)
    # Count lines starting with * or numbered items (1. 2. etc) or lettered (a) b))
    items = re.findall(r"(?m)^[\s]*(?:\*|\d+\.|[a-z]\))", section)
    return len(items)


def _extract_first_item(text: str, section_pattern: str) -> str | None:
    """Extract first bullet/numbered item from a section."""
    match = re.search(section_pattern, text, re.UNICODE | re.DOTALL)
    if not match:
        return None
    section = match.group(0)
    item_match = re.search(r"(?m)^\s*(?:\*\*?|\d+\.)\s*\**(.+?)(?:\*\*|\n)", section)
    if item_match:
        return item_match.group(1).strip()[:200]
    return None


def extract_insights(deep_analysis_text: str) -> dict:
    """Extract qualitative insight counts from Georgian deep analysis text.

    Parses section headers and counts items in:
    - Strengths (ძლიერი მხარეები)
    - Weaknesses (სუსტი მხარეები)
    - Content gaps (ხარვეზები/ბრმა წერტილები)
    - Recommendations (რეკომენდაციები/გაუმჯობესებები)
    - Technical accuracy (correct ✅ vs problematic ⚠️)
    """
    # Strengths: look for bullet items after "ძლიერი მხარეები" header
    str_section = _get_section(deep_analysis_text, r"\*?\*?ძლიერი\s+მხარეები")
    if not str_section:
        # Try direct pattern: **ძლიერი მხარეები:**\n* item\n* item
        m = re.search(
            r"\*\*ძლიერი\s+მხარეები:?\*\*\s*\n((?:\s*\*.+\n?)+)",
            deep_analysis_text, re.UNICODE,
        )
        str_section = m.group(1) if m else ""
    if not str_section:
        # Fallback: count "ძლიერი მხარე" or "ძლიერი:" anywhere as bullet items
        str_section = _get_section(deep_analysis_text, r"ძლიერი") or ""
    strengths_count = len(re.findall(r"(?m)^\s*[\*\-]\s+.+", str_section or ""))
    if strengths_count == 0:
        # Numbered items fallback: 1. **item**
        strengths_count = len(re.findall(r"(?m)^\s*\d+\.\s+\*\*", str_section or ""))
    if strengths_count == 0:
        # Last resort: count score dimensions >= 6 as relative strengths
        high_scores = re.findall(
            r"\|\s*\*{0,2}(?:[6789]|10)(?:\.\d+)?/10\*{0,2}\s*\|",
            deep_analysis_text,
        )
        strengths_count = len(high_scores)

    # Weaknesses
    wk_section = _get_section(deep_analysis_text, r"\*?\*?სუსტი\s+მხარეები")
    if not wk_section:
        m = re.search(
            r"\*\*სუსტი\s+მხარეები:?\*\*\s*\n((?:\s*\*.+\n?)+)",
            deep_analysis_text, re.UNICODE,
        )
        wk_section = m.group(1) if m else ""
    if not wk_section:
        # Fallback: look for "განვითარება" or "გასაუმჯობესებელი" sections
        wk_section = _get_section(deep_analysis_text, r"განვითარებ|გასაუმჯობესებელ|სისუსტ") or ""
    weaknesses_count = len(re.findall(r"(?m)^\s*[\*\-]\s+.+", wk_section or ""))
    if weaknesses_count == 0:
        weaknesses_count = len(re.findall(r"(?m)^\s*\d+\.\s+\*\*", wk_section or ""))
    if weaknesses_count == 0:
        # Last resort: count score dimensions <= 5 as weaknesses
        low_scores = re.findall(
            r"\|\s*\*{0,2}[1-5](?:\.\d+)?/10\*{0,2}\s*\|",
            deep_analysis_text,
        )
        weaknesses_count = len(low_scores)

    # Gaps: try multiple section names
    gap_section = (
        _get_section(deep_analysis_text, r"კრიტიკული\s+ხარვეზ")
        or _get_section(deep_analysis_text, r"ხარვეზ")
        or ""
    )
    gaps_count = len(re.findall(r"(?m)^\s*[a-z]\)\s+", gap_section))
    if gaps_count == 0:
        # Also count numbered items or bullets in gap section
        gaps_count = len(re.findall(r"(?m)^\s*[\*\-]\s+.+", gap_section))
    if gaps_count == 0:
        gaps_count = len(re.findall(r"(?m)^\s*\d+\.\s+", gap_section))

    # Blind spots: try both "ბრმა წერტილი" header and "ბრმა წერტილები" section
    blind_spots = len(re.findall(
        r"(?m)^###?\s+.*ბრმა\s+წერტილ",
        deep_analysis_text
    ))
    if blind_spots == 0:
        bs_section = _get_section(deep_analysis_text, r"ბრმა\s+წერტილ") or ""
        blind_spots = len(re.findall(r"(?m)^\s*[\*\-]\s+.+", bs_section))
        if blind_spots == 0:
            blind_spots = len(re.findall(r"(?m)^\s*\d+\.\s+", bs_section))

    # Recommendations
    rec_section = _get_section(deep_analysis_text, r"რეკომენდაცი|გაუმჯობესებ|სარეკომენდაციო") or ""
    recs_count = len(re.findall(r"(?m)^\s*\d+\.\s+\*\*", rec_section))
    if recs_count == 0:
        recs_count = len(re.findall(r"(?m)^\s*[\*\-]\s+\*\*", rec_section))
    if recs_count == 0:
        # Count any numbered steps: "ნაბიჯი N:"
        recs_count = len(re.findall(r"(?m)ნაბიჯი\s+\d+", rec_section))
    # Count ✅ items (technically correct) and ⚠️ items (problematic)
    tech_correct = len(re.findall(r"(?m)^\s*✅\s+", deep_analysis_text))
    tech_problematic = len(re.findall(r"(?m)^\s*[⚠️]\s*\*\*", deep_analysis_text))
    if tech_correct == 0:
        # Fallback: count bullet items under "სწორია" section
        correct_section = _get_section(deep_analysis_text, r"სწორია") or ""
        tech_correct = len(re.findall(r"(?m)^\s*\*\s+.+", correct_section))
    if tech_problematic == 0:
        # Fallback: count items under "პრობლემური" section
        prob_section = _get_section(deep_analysis_text, r"პრობლემური") or ""
        tech_problematic = len(re.findall(r"(?m)^\s*\*\s+.+", prob_section))

    # Extract top items
    top_strength = _extract_first_item(
        deep_analysis_text,
        r"ძლიერი\s+მხარეები.+?(?=\n##|\n\*\*სუსტი|\Z)"
    )
    if not top_strength:
        # Fallback: get justification from highest-scored dimension in table
        best = re.findall(
            r"\|[^\|]+\|\s*\*{0,2}(\d+)(?:\.\d+)?/10\*{0,2}\s*\|([^\|]+)\|",
            deep_analysis_text, re.UNICODE,
        )
        if best:
            best.sort(key=lambda x: -float(x[0]))
            top_strength = best[0][1].strip()[:300]
    top_weakness = _extract_first_item(
        deep_analysis_text,
        r"სუსტი\s+მხარეები.+?(?=\n##|\Z)"
    )
    if not top_weakness:
        # Fallback: get justification from lowest-scored dimension in table
        worst = re.findall(
            r"\|[^\|]+\|\s*\*{0,2}([1-5])(?:\.\d+)?/10\*{0,2}\s*\|([^\|]+)\|",
            deep_analysis_text, re.UNICODE,
        )
        if worst:
            worst.sort(key=lambda x: float(x[0]))
            top_weakness = worst[0][1].strip()[:300]

    # If strengths/weaknesses counts are still 0, derive from score table
    if strengths_count == 0 and top_strength:
        # Count dimensions scoring >= 7 as strengths
        high_dims = re.findall(
            r"\|\s*\*{0,2}([789]|10)(?:\.\d+)?/10\*{0,2}\s*\|",
            deep_analysis_text,
        )
        strengths_count = max(len(high_dims), 1)  # at least 1 if we have top_strength
    if weaknesses_count == 0 and top_weakness:
        # Count dimensions scoring <= 5 as weaknesses
        low_dims = re.findall(
            r"\|\s*\*{0,2}[1-5](?:\.\d+)?/10\*{0,2}\s*\|",
            deep_analysis_text,
        )
        weaknesses_count = max(len(low_dims), 1)  # at least 1 if we have top_weakness
    key_rec = _extract_first_item(
        deep_analysis_text,
        r"(?:რეკომენდაცი|გაუმჯობესებ|სარეკომენდაციო).+?(?=\n##|\Z)"
    )

    # Extract score justifications as JSON
    justifications = {}
    for col, label_pattern in _DIMENSION_PATTERNS:
        # Handle both plain "N/10" and bold "**N/10**" formats
        pattern = (
            r"\|[^\|]*" + label_pattern
            + r"[^\|]*\|\s*\**\d+(?:\.\d+)?/10\**\s*\|([^\|]+)\|"
        )
        m = re.search(pattern, deep_analysis_text, re.UNICODE | re.IGNORECASE)
        if m:
            justifications[col] = m.group(1).strip()[:300]

    return {
        "strengths_count": strengths_count,
        "weaknesses_count": weaknesses_count,
        "gaps_count": gaps_count,
        "recommendations_count": recs_count,
        "tech_correct_count": tech_correct,
        "tech_problematic_count": tech_problematic,
        "blind_spots_count": blind_spots,
        "top_strength": top_strength,
        "top_weakness": top_weakness,
        "key_recommendation": key_rec,
        "score_justifications": json.dumps(justifications, ensure_ascii=False) if justifications else None,
    }


def _get_section(text: str, header_pattern: str) -> str | None:
    """Extract text from a section header to the next ## header.

    Handles multiple header formats:
    - ## ძლიერი მხარეები
    - ### 10. კრიტიკული ბრმა წერტილები
    - **ძლიერი მხარეები:**
    - ნაწილი II — ძლიერი მხარეები
    """
    # Try markdown headers first (##, ###)
    match = re.search(
        rf"(?:##?#?\s*(?:\d+\.?\s*)?(?:ნაწილი\s+[IVX]+\s*[—\-]?\s*)?){header_pattern}.*?\n(.*?)(?=\n##|\Z)",
        text, re.UNICODE | re.DOTALL | re.IGNORECASE,
    )
    if match:
        return match.group(1)
    # Try bold headers: **header:**
    match = re.search(
        rf"\*\*[^*]*{header_pattern}[^*]*\*\*:?\s*\n(.*?)(?=\n\*\*[^*]+\*\*:?\s*\n|\n##|\Z)",
        text, re.UNICODE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) if match else None


def extract_and_save_insights(
    group_number: int,
    lecture_number: int,
    deep_analysis_text: str,
) -> bool:
    """Extract and persist qualitative insights from deep analysis text."""
    if not deep_analysis_text or len(deep_analysis_text.strip()) < 500:
        return False

    insights = extract_insights(deep_analysis_text)
    now = datetime.now(timezone.utc).isoformat()

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO lecture_insights
                (group_number, lecture_number, strengths_count, weaknesses_count,
                 gaps_count, recommendations_count, tech_correct_count,
                 tech_problematic_count, blind_spots_count,
                 top_strength, top_weakness, key_recommendation,
                 score_justifications, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_number, lecture_number,
                insights["strengths_count"], insights["weaknesses_count"],
                insights["gaps_count"], insights["recommendations_count"],
                insights["tech_correct_count"], insights["tech_problematic_count"],
                insights["blind_spots_count"],
                insights["top_strength"], insights["top_weakness"],
                insights["key_recommendation"],
                insights["score_justifications"], now,
            ),
        )
    logger.info(
        "Saved insights — Group %d, Lecture #%d | strengths=%d weaknesses=%d gaps=%d recs=%d",
        group_number, lecture_number,
        insights["strengths_count"], insights["weaknesses_count"],
        insights["gaps_count"], insights["recommendations_count"],
    )
    return True


def get_lecture_insights(group_number: int, lecture_number: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lecture_insights WHERE group_number=? AND lecture_number=?",
            (group_number, lecture_number),
        ).fetchone()
    return dict(row) if row else None


def get_all_insights() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lecture_insights ORDER BY group_number, lecture_number"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Query path
# ---------------------------------------------------------------------------

def get_scores_for_lecture(group_number: int, lecture_number: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lecture_scores WHERE group_number=? AND lecture_number=?",
            (group_number, lecture_number),
        ).fetchone()
    return dict(row) if row else None


def get_group_scores(group_number: int) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lecture_scores WHERE group_number=? ORDER BY lecture_number",
            (group_number,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_scores(group_number: int | None = None) -> list[dict]:
    with _get_conn() as conn:
        if group_number is not None:
            rows = conn.execute(
                "SELECT * FROM lecture_scores WHERE group_number=? "
                "ORDER BY group_number, lecture_number",
                (group_number,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM lecture_scores ORDER BY group_number, lecture_number"
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def calculate_statistics(scores: list[float]) -> dict:
    """Compute descriptive statistics and trend metrics for a score series.

    Args:
        scores: List of scores in lecture order (index 0 = lecture 1).

    Returns dict with:
        mean, median, std_dev, min, max, p25, p75,
        trend_slope (linear regression slope per lecture),
        rolling_avg_3 (last 3 lectures),
        improvement_rate (mean of consecutive deltas),
        trend_label (emoji + Georgian text)
    """
    n = len(scores)
    nulls: dict = {k: None for k in [
        "mean", "median", "std_dev", "min", "max",
        "p25", "p75", "trend_slope", "rolling_avg_3",
        "improvement_rate", "trend_label",
    ]}
    if n == 0:
        return nulls

    mean = sum(scores) / n
    sorted_s = sorted(scores)

    # median
    mid = n // 2
    median = sorted_s[mid] if n % 2 else (sorted_s[mid - 1] + sorted_s[mid]) / 2

    # sample std dev
    variance = sum((x - mean) ** 2 for x in scores) / max(n - 1, 1)
    std_dev = math.sqrt(variance)

    def _percentile(p: float) -> float:
        idx = max(0, math.ceil(p / 100 * n) - 1)
        return sorted_s[idx]

    # linear regression slope (no numpy)
    # x = 1..n, y = scores
    x_mean = (n + 1) / 2
    num = sum((i + 1 - x_mean) * (y - mean) for i, y in enumerate(scores))
    den = sum((i + 1 - x_mean) ** 2 for i in range(n))
    slope = num / den if den else 0.0

    rolling_avg_3 = sum(scores[-3:]) / min(3, n)

    deltas = [scores[i] - scores[i - 1] for i in range(1, n)]
    improvement_rate = sum(deltas) / len(deltas) if deltas else 0.0

    if slope > 0.05:
        trend_label = "📈 იზრდება"
    elif slope < -0.05:
        trend_label = "📉 მცირდება"
    else:
        trend_label = "➡️ სტაბილური"

    return {
        "mean": round(mean, 2),
        "median": round(median, 2),
        "std_dev": round(std_dev, 2),
        "min": min(scores),
        "max": max(scores),
        "p25": round(_percentile(25), 2),
        "p75": round(_percentile(75), 2),
        "trend_slope": round(slope, 4),
        "rolling_avg_3": round(rolling_avg_3, 2),
        "improvement_rate": round(improvement_rate, 4),
        "trend_label": trend_label,
    }


def _build_group_data(group_number: int) -> dict:
    rows = get_group_scores(group_number)

    empty_stats = {d: calculate_statistics([]) for d in DIMENSIONS + ["composite"]}
    if not rows:
        return {
            "lecture_count": 0,
            "scores": [],
            "stats": empty_stats,
            "best_lecture": None,
            "worst_lecture": None,
            "composite_series": [],
            "lecture_labels": [],
            "dimension_series": {d: [] for d in DIMENSIONS},
            "heatmap": [],
            "strengths": [],
            "weaknesses": [],
            "consistency": None,
        }

    composite_series = [r["composite"] for r in rows]
    dimension_series = {d: [r[d] for r in rows] for d in DIMENSIONS}
    lecture_labels = [f"ლექცია #{r['lecture_number']}" for r in rows]

    stats = {d: calculate_statistics(dimension_series[d]) for d in DIMENSIONS}
    stats["composite"] = calculate_statistics(composite_series)

    best = max(rows, key=lambda r: r["composite"])
    worst = min(rows, key=lambda r: r["composite"])

    # Heatmap: list of {lecture, dimension_scores} for grid rendering
    heatmap = []
    for r in rows:
        heatmap.append({
            "lecture": r["lecture_number"],
            "scores": {d: r[d] for d in DIMENSIONS},
            "composite": r["composite"],
        })

    # Strengths/weaknesses: rank dimensions by mean score
    dim_means = []
    for d in DIMENSIONS:
        m = stats[d].get("mean")
        if m is not None:
            dim_means.append((d, m))
    dim_means.sort(key=lambda x: x[1], reverse=True)
    strengths = [{"dim": d, "mean": m} for d, m in dim_means[:2]] if dim_means else []
    weaknesses = [{"dim": d, "mean": m} for d, m in dim_means[-2:]] if len(dim_means) >= 2 else []

    # Consistency: avg std_dev across dimensions (lower = more consistent)
    std_devs = [stats[d].get("std_dev") for d in DIMENSIONS if stats[d].get("std_dev") is not None]
    consistency = round(10 - sum(std_devs) / len(std_devs), 2) if std_devs else None

    # Computed trainer sub-scores (derived from 5 dimensions)
    last_row = rows[-1]
    pedagogy_score = round((last_row["engagement"] + last_row["content_depth"]) / 2, 1)
    content_quality = round(
        (last_row["technical_accuracy"] + last_row["content_depth"] + last_row["market_relevance"]) / 3, 1
    )
    impact_score = round((last_row["practical_value"] + last_row["market_relevance"]) / 2, 1)
    dim_vals = [last_row[d] for d in DIMENSIONS]
    balance_score = round(10 - (max(dim_vals) - min(dim_vals)), 1)
    target_gap = round(7.0 - last_row["composite"], 1)

    # Load qualitative insights for this group's lectures
    insights_list = []
    with _get_conn() as conn:
        ins_rows = conn.execute(
            "SELECT * FROM lecture_insights WHERE group_number=? ORDER BY lecture_number",
            (group_number,),
        ).fetchall()
    insights_list = [dict(r) for r in ins_rows]

    # ── NEW: Research-backed insightful metrics ──

    # 1. Kirkpatrick Level Scores (from Trainer KPI research)
    #    L1=Reaction(engagement), L2=Learning(depth+accuracy), L3=Behavior(practical), L4=Results(market)
    kirkpatrick = {
        "L1_reaction": last_row["engagement"],
        "L2_learning": round((last_row["content_depth"] + last_row["technical_accuracy"]) / 2, 1),
        "L3_behavior": last_row["practical_value"],
        "L4_results": last_row["market_relevance"],
    }

    # 2. Improvement Velocity (slope per lecture — from trend analysis)
    velocity = stats["composite"].get("trend_slope", 0) or 0
    velocity_label = "აჩქარებს" if velocity > 0.3 else ("ანელებს" if velocity < -0.3 else "სტაბილური")

    # 3. Dimension Volatility (which dimensions swing most between lectures)
    volatility = {}
    for d in DIMENSIONS:
        series = dimension_series[d]
        if len(series) >= 2:
            deltas = [abs(series[i] - series[i-1]) for i in range(1, len(series))]
            volatility[d] = round(sum(deltas) / len(deltas), 2)
        else:
            volatility[d] = 0.0

    # 4. Cross-Lecture Learning (did recommendations get addressed?)
    recommendation_followthrough = 0
    if len(rows) >= 2:
        prev, curr = rows[-2], rows[-1]
        # Count dimensions that improved
        improved = sum(1 for d in DIMENSIONS if curr[d] > prev[d])
        recommendation_followthrough = improved  # proxy for follow-through

    # 5. Theory vs Practice Balance (from AI training research)
    #    Ideal ratio: 30% theory (depth+accuracy) / 70% practice (practical+engagement+market)
    theory = (last_row["content_depth"] + last_row["technical_accuracy"]) / 2
    practice = (last_row["practical_value"] + last_row["engagement"] + last_row["market_relevance"]) / 3
    theory_practice_ratio = round(theory / practice, 2) if practice > 0 else None
    # Ideal ~0.43 (30/70). Lower = more practical, higher = too theoretical

    # 6. Global Benchmark Position (from industry research: 4.3/5 = 8.6/10 satisfaction benchmark)
    industry_benchmark = 7.0  # ATD/SHRM average for "good" trainers
    benchmark_gap = round((tpi_local := last_row["composite"]) - industry_benchmark, 1)
    benchmark_percentile = min(99, max(1, round(tpi_local / 10 * 100)))

    # 7. At-Risk Dimensions (from lecture analysis research — regression detection)
    at_risk_dims = []
    for d in DIMENSIONS:
        series = dimension_series[d]
        if len(series) >= 2 and series[-1] < series[-2]:
            drop = round(series[-2] - series[-1], 1)
            if drop >= 1.0:
                at_risk_dims.append({"dim": d, "drop": drop, "current": series[-1]})

    # 8. Lectures Until Target (estimated based on velocity)
    lectures_to_target = None
    if velocity > 0 and last_row["composite"] < 7.0:
        gap = 7.0 - last_row["composite"]
        lectures_to_target = math.ceil(gap / velocity) if velocity > 0.01 else None

    return {
        "lecture_count": len(rows),
        "scores": rows,
        "stats": stats,
        "best_lecture": {"number": best["lecture_number"], "composite": best["composite"]},
        "worst_lecture": {"number": worst["lecture_number"], "composite": worst["composite"]},
        "composite_series": composite_series,
        "lecture_labels": lecture_labels,
        "dimension_series": dimension_series,
        "heatmap": heatmap,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "consistency": consistency,
        "pedagogy_score": pedagogy_score,
        "content_quality": content_quality,
        "impact_score": impact_score,
        "balance_score": balance_score,
        "target_gap": target_gap,
        "insights": insights_list,
        # New research-backed metrics
        "kirkpatrick": kirkpatrick,
        "velocity": velocity,
        "velocity_label": velocity_label,
        "volatility": volatility,
        "recommendation_followthrough": recommendation_followthrough,
        "theory_practice_ratio": theory_practice_ratio,
        "benchmark_gap": benchmark_gap,
        "benchmark_percentile": benchmark_percentile,
        "at_risk_dims": at_risk_dims,
        "lectures_to_target": lectures_to_target,
    }


def get_dashboard_data() -> dict:
    """Assemble all data needed to render the analytics dashboard."""
    g1 = _build_group_data(1)
    g2 = _build_group_data(2)

    cross_group: dict[str, dict] = {}
    for dim in DIMENSIONS + ["composite"]:
        s1 = g1["stats"][dim]
        s2 = g2["stats"][dim]
        cross_group[dim] = {
            "g1_mean": s1["mean"],
            "g2_mean": s2["mean"],
            "delta": (
                round(s2["mean"] - s1["mean"], 2)
                if s1["mean"] is not None and s2["mean"] is not None
                else None
            ),
        }

    # Trainer Performance Index: weighted mean of all composites across groups
    all_composites = g1["composite_series"] + g2["composite_series"]
    tpi = round(sum(all_composites) / len(all_composites), 2) if all_composites else None

    # Overall dimension ranking across both groups
    overall_dim_means: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
    for g in [g1, g2]:
        for d in DIMENSIONS:
            m = g["stats"][d].get("mean")
            if m is not None:
                overall_dim_means[d].append(m)
    dim_rankings = []
    for d in DIMENSIONS:
        vals = overall_dim_means[d]
        avg = round(sum(vals) / len(vals), 2) if vals else None
        dim_rankings.append({"dim": d, "mean": avg})
    dim_rankings.sort(key=lambda x: x["mean"] if x["mean"] is not None else 0, reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total_processed": g1["lecture_count"] + g2["lecture_count"],
        "groups": {1: g1, 2: g2},
        "cross_group": cross_group,
        "dimension_labels_ka": DIMENSION_LABELS_KA,
        "dimension_labels_en": DIMENSION_LABELS_EN,
        "total_lectures": 15,
        "trainer_performance_index": tpi,
        "dimension_rankings": dim_rankings,
    }


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def backfill_from_tmp() -> dict[str, int]:
    """Scan .tmp/ for existing deep_analysis files and extract scores.

    Skips lectures already present in the DB (INSERT OR REPLACE would
    overwrite, so we skip to preserve manual corrections).

    Returns:
        {"processed": N, "failed": M, "skipped": K}
    """
    pattern = re.compile(r"^g(\d+)_l(\d+)_deep_analysis\.txt$")
    processed = failed = skipped = 0

    for f in sorted(TMP_DIR.glob("g*_l*_deep_analysis.txt")):
        m = pattern.match(f.name)
        if not m:
            continue
        group, lecture = int(m.group(1)), int(m.group(2))

        # Skip if already indexed
        if get_scores_for_lecture(group, lecture) is not None:
            skipped += 1
            continue

        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("backfill: cannot read %s: %s", f.name, e)
            failed += 1
            continue

        if len(text.strip()) < 100:
            logger.warning("backfill: file too short (%d chars): %s", len(text), f.name)
            failed += 1
            continue

        if save_scores_from_analysis(group, lecture, text):
            processed += 1
            logger.info("backfill: processed %s", f.name)
        else:
            failed += 1
            logger.warning("backfill: score extraction failed for %s", f.name)

    return {"processed": processed, "failed": failed, "skipped": skipped}


# ---------------------------------------------------------------------------
# Pinecone sync — reconstruct scores from persistent vector DB
# ---------------------------------------------------------------------------

_last_sync_time: float = 0.0
_SYNC_COOLDOWN = 300  # 5 minutes between syncs


def sync_from_pinecone(force: bool = False) -> dict[str, int]:
    """Sync lecture scores from Pinecone deep_analysis vectors.

    Pinecone is the persistent source of truth (Railway DB is ephemeral).
    This function:
    1. Lists all g{N}_l{N}_deep_analysis_ prefixes in Pinecone
    2. Checks which lectures are missing from local DB
    3. Reconstructs deep_analysis text from vector chunks
    4. Extracts scores and saves to local DB

    Uses a 5-minute cooldown to avoid hammering Pinecone on every request.
    Pass force=True to bypass the cooldown.

    Returns:
        {"synced": N, "skipped": M, "failed": K}
    """
    import time

    global _last_sync_time
    now = time.time()
    if not force and (now - _last_sync_time) < _SYNC_COOLDOWN:
        return {"synced": 0, "skipped": 0, "failed": 0, "cached": True}

    _last_sync_time = now

    try:
        from tools.integrations.knowledge_indexer import get_pinecone_index
    except ImportError:
        logger.warning("sync_from_pinecone: knowledge_indexer not available")
        return {"synced": 0, "skipped": 0, "failed": 0}

    try:
        idx = get_pinecone_index()
    except Exception as e:
        logger.warning("sync_from_pinecone: cannot connect to Pinecone: %s", e)
        return {"synced": 0, "skipped": 0, "failed": 0}

    synced = failed = skipped = 0

    # Batch query: get all existing lectures in one query (not N+1)
    with _get_conn() as conn:
        existing = set(
            (r["group_number"], r["lecture_number"])
            for r in conn.execute("SELECT group_number, lecture_number FROM lecture_scores").fetchall()
        )

    for group in [1, 2]:
        for lecture in range(1, 16):
            if (group, lecture) in existing:
                skipped += 1
                continue

            # Check if deep_analysis exists in Pinecone
            prefix = f"g{group}_l{lecture}_deep_analysis_"
            try:
                all_ids: list[str] = []
                for page in idx.list(prefix=prefix, limit=99):
                    all_ids.extend(page)
            except Exception as e:
                logger.warning("sync: Pinecone list error for %s: %s", prefix, e)
                continue

            if not all_ids:
                continue  # No deep analysis for this lecture

            # Fetch chunks and reconstruct text
            try:
                fetched = idx.fetch(ids=all_ids)
                chunks: list[tuple[int, str]] = []
                for vid, vec in fetched.vectors.items():
                    meta = vec.metadata
                    chunk_idx = meta.get("chunk_index", 0)
                    text = meta.get("text", "")
                    chunks.append((chunk_idx, text))

                chunks.sort(key=lambda x: x[0])
                full_text = "\n".join(t for _, t in chunks)

                if len(full_text.strip()) < 200:
                    logger.warning(
                        "sync: reconstructed text too short (%d chars) for G%dL%d",
                        len(full_text), group, lecture,
                    )
                    failed += 1
                    continue

                # Save scores + insights
                if save_scores_from_analysis(group, lecture, full_text):
                    synced += 1
                    logger.info(
                        "sync: synced G%dL%d from Pinecone (%d chunks, %d chars)",
                        group, lecture, len(chunks), len(full_text),
                    )
                else:
                    failed += 1
                    logger.warning("sync: score extraction failed for G%dL%d", group, lecture)

            except Exception as e:
                logger.warning("sync: fetch/reconstruct error for G%dL%d: %s", group, lecture, e)
                failed += 1

    # Seed G1L1 approximate scores (video was corrupted, scores derived from
    # equivalent G2L1 delivery analysis — cannot be re-processed).
    if (1, 1) not in existing and not get_scores_for_lecture(1, 1):
        try:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO lecture_scores
                       (group_number, lecture_number, content_depth, practical_value,
                        engagement, technical_accuracy, market_relevance,
                        overall_score, composite, raw_score_text, processed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (1, 1, 4.0, 5.0, 5.0, 5.0, 7.0, 5.2, 5.2,
                     "Approximate scores based on G2L1 deep analysis "
                     "(same lecture content). Video recording corrupted.",
                     datetime.now(timezone.utc).isoformat()),
                )
                # Also seed insights for G1L1
                conn.execute(
                    """INSERT OR IGNORE INTO lecture_insights
                       (group_number, lecture_number, strengths_count, weaknesses_count,
                        gaps_count, recommendations_count, tech_correct_count,
                        tech_problematic_count, blind_spots_count,
                        top_strength, top_weakness, key_recommendation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (1, 1, 2, 3, 3, 0, 0, 0, 0,
                     "ლექტორს აქვს გულწრფელი ვნება AI ტექნოლოგიების მიმართ და პრაქტიკული გამოცდილება აგენტების შექმნაში.",
                     "ლექცია არასტრუქტურირებულია — არ არის დღის წესრიგი, სასწავლო მიზნები და შეჯამება.",
                     None),
                )
                conn.commit()
            synced += 1
            logger.info("sync: seeded G1L1 approximate scores + insights (corrupted video)")
        except Exception as e:
            logger.warning("sync: failed to seed G1L1: %s", e)

    if synced:
        logger.info("Pinecone sync complete: synced=%d skipped=%d failed=%d", synced, skipped, failed)

    return {"synced": synced, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

_CHART_COLORS = [
    "rgba(99,  102, 241, 1)",    # indigo   — content_depth
    "rgba(34,  211, 238, 1)",    # cyan     — practical_value
    "rgba(52,  211, 153, 1)",    # emerald  — engagement
    "rgba(251, 191,  36, 1)",    # amber    — technical_accuracy
    "rgba(248,  113, 113, 1)",   # red      — market_relevance
]
_CHART_COLORS_FILL = [c.replace(", 1)", ", 0.15)") for c in _CHART_COLORS]


def generate_performance_narrative(dashboard_data: dict) -> str:
    """Generate a Georgian-language narrative summary of trainer performance.

    Reads the dashboard_data dict (from get_dashboard_data()) and produces
    a 3-4 sentence human-readable "Data Story" covering:
    - Composite trend (improving / declining / stable) with % change
    - Biggest improvement and biggest decline across dimensions
    - Strongest and weakest dimensions overall
    - Next milestone hint if composite is close to a round target

    Returns:
        Georgian markdown-formatted narrative string with emoji markers.
    """
    labels = dashboard_data.get("dimension_labels_ka", DIMENSION_LABELS_KA)
    dim_rankings = dashboard_data.get("dimension_rankings", [])
    groups = dashboard_data.get("groups", {})

    # ── Collect all lecture rows chronologically across both groups ──
    all_rows: list[dict] = []
    for gnum in sorted(groups.keys()):
        g = groups[gnum]
        for row in g.get("scores", []):
            all_rows.append(row)

    if not all_rows:
        return "📊 ჯერ არცერთი ლექციის ქულა არ არის დაფიქსირებული."

    # Sort by processed_at timestamp for true chronological order
    all_rows.sort(key=lambda r: r.get("processed_at", ""))

    n = len(all_rows)

    # ── Composite trend ──
    composites = [r["composite"] for r in all_rows]
    first_c, last_c = composites[0], composites[-1]
    pct_change = round((last_c - first_c) / first_c * 100, 1) if first_c else 0.0

    if pct_change > 5:
        trend_emoji = "📈"
        trend_word = "გაიზარდა"
    elif pct_change < -5:
        trend_emoji = "📉"
        trend_word = "შემცირდა"
    else:
        trend_emoji = "➡️"
        trend_word = "სტაბილურია"

    sign = "+" if pct_change >= 0 else ""
    narrative_parts: list[str] = []

    narrative_parts.append(
        f"{trend_emoji} **შენი პროგრესი:** ბოლო {n} ლექციაში "
        f"კომპოზიტური ქულა {first_c}-დან {last_c}-მდე {trend_word} "
        f"({sign}{pct_change}%)."
    )

    # ── Per-dimension changes (first → last lecture) ──
    dim_changes: list[tuple[str, float, float, float]] = []  # (dim, first, last, pct)
    first_row, last_row = all_rows[0], all_rows[-1]
    for d in DIMENSIONS:
        v0, v1 = first_row[d], last_row[d]
        if v0 and v0 > 0:
            dpct = round((v1 - v0) / v0 * 100, 1)
        else:
            dpct = 0.0
        dim_changes.append((d, v0, v1, dpct))

    # Biggest improvement
    dim_changes_sorted = sorted(dim_changes, key=lambda x: x[3], reverse=True)
    best_dim, best_v0, best_v1, best_pct = dim_changes_sorted[0]
    if best_pct > 0:
        narrative_parts.append(
            f"ყველაზე დიდი გაუმჯობესება **{labels.get(best_dim, best_dim)}**ში "
            f"მოხდა ({best_v0}→{best_v1}, +{best_pct}%)."
        )

    # Biggest decline (only mention if actually negative)
    worst_dim, worst_v0, worst_v1, worst_pct = dim_changes_sorted[-1]
    if worst_pct < -5:
        narrative_parts.append(
            f"⚠️ **{labels.get(worst_dim, worst_dim)}** შემცირდა "
            f"({worst_v0}→{worst_v1}, {worst_pct}%)."
        )

    # ── Strongest and weakest dimensions (overall means) ──
    if len(dim_rankings) >= 2:
        strong = dim_rankings[0]
        weak = dim_rankings[-1]
        narrative_parts.append(
            f"**{labels.get(strong['dim'], strong['dim'])}** შენი "
            f"ყველაზე ძლიერი მხარეა (საშ. {strong['mean']}/10). "
            f"ფოკუსი საჭიროა **{labels.get(weak['dim'], weak['dim'])}**ზე "
            f"(საშ. {weak['mean']}/10) — ეს ყველაზე დაბალი განზომილებაა."
        )

    # ── Milestone hint ──
    tpi = dashboard_data.get("trainer_performance_index")
    if tpi is not None:
        next_milestone = math.ceil(tpi)
        gap = round(next_milestone - tpi, 2)
        if 0 < gap <= 0.5:
            narrative_parts.append(
                f"🎯 მიზანი ახლოსაა: კიდევ {gap} ქულა და TPI "
                f"{next_milestone}.0-ს მიაღწევს!"
            )
        elif last_c < 7.0 and last_c >= 6.5:
            narrative_parts.append(
                f"🎯 შემდეგი მიზანი: კომპოზიტური 7.0 — აკლია მხოლოდ "
                f"{round(7.0 - last_c, 1)} ქულა."
            )

    return " ".join(narrative_parts)


def render_dashboard_html(data: dict) -> str:
    """Generate the complete premium analytics dashboard HTML page.

    All data rendered into the HTML comes from our own analytics SQLite DB
    (trusted, server-side only). No user-supplied input is interpolated.
    """
    json_data = json.dumps(data, ensure_ascii=False, default=str)

    g1 = data["groups"][1]
    g2 = data["groups"][2]
    total = data["total_lectures"]
    generated_at = data["generated_at"]
    tpi = data.get("trainer_performance_index")
    dim_rankings = data.get("dimension_rankings", [])

    def _sc(score) -> str:
        if score is None:
            return "na"
        if score >= 7:
            return "good"
        if score >= 5:
            return "mid"
        return "bad"

    def _fmt(val) -> str:
        if val is None:
            return "\u2014"
        if isinstance(val, float):
            return f"{val:.1f}"
        return str(val)

    def _build_insights_html(dashboard_data: dict) -> str:
        """Build AI insights digest section from all groups' insights."""
        all_insights = []
        for gn_val in [1, 2]:
            for ins in dashboard_data["groups"][gn_val].get("insights", []):
                all_insights.append({"group": gn_val, **ins})

        if not all_insights:
            return '<div class="card"><p class="empty-state">AI \u10d0\u10dc\u10d0\u10da\u10d8\u10d6\u10d8 \u10ef\u10d4\u10e0 \u10d0\u10e0 \u10d0\u10e0\u10d8\u10e1 \u10ee\u10d4\u10da\u10db\u10d8\u10e1\u10d0\u10ec\u10d5\u10d3\u10dd\u10db\u10d8</p></div>'

        h = '<div class="ins-cards-grid">'
        for ins in all_insights:
            gn = ins["group"]
            ln = ins["lecture_number"]
            dot_cls = "g1-dot" if gn == 1 else "g2-dot"

            # Score justifications — show full text (no truncation)
            just_html = ""
            if ins.get("score_justifications"):
                try:
                    justs = json.loads(ins["score_justifications"])
                    for dim_key, text_val in justs.items():
                        dim_label = dashboard_data["dimension_labels_ka"].get(dim_key, dim_key)
                        just_html += f'<div class="just-item"><span class="just-dim">{_esc(dim_label)}:</span> {_esc(str(text_val))}</div>'
                except (json.JSONDecodeError, TypeError):
                    pass

            h += f'''<div class="card ins-card">
              <div class="group-header"><div class="group-dot {dot_cls}"></div>
                <span class="group-name">\u10ef\u10d2 #{gn} \u2014 \u10da\u10d4\u10e5\u10ea\u10d8\u10d0 #{ln}</span></div>
              <div class="insights-grid">
                <div class="ins-stat"><span class="ins-num sc-good">{ins.get("strengths_count", 0)}</span><span class="ins-lbl">\u10eb\u10da\u10d8\u10d4\u10e0\u10d8</span></div>
                <div class="ins-stat"><span class="ins-num sc-bad">{ins.get("weaknesses_count", 0)}</span><span class="ins-lbl">\u10e1\u10e3\u10e1\u10e2\u10d8</span></div>
                <div class="ins-stat"><span class="ins-num sc-mid">{ins.get("gaps_count", 0)}</span><span class="ins-lbl">\u10ee\u10d0\u10e0\u10d5\u10d4\u10d6\u10d8</span></div>
                <div class="ins-stat"><span class="ins-num" style="color:var(--rose)">{ins.get("blind_spots_count", 0)}</span><span class="ins-lbl">\u10d1\u10e0\u10db\u10d0 \u10ec\u10d4\u10e0\u10e2\u10d8\u10da\u10d8</span></div>
                <div class="ins-stat"><span class="ins-num sc-good">{ins.get("tech_correct_count", 0)}</span><span class="ins-lbl">\u10e2\u10d4\u10e5. \u2713</span></div>
                <div class="ins-stat"><span class="ins-num sc-bad">{ins.get("tech_problematic_count", 0)}</span><span class="ins-lbl">\u10e2\u10d4\u10e5. \u26a0</span></div>
              </div>'''

            if ins.get("top_strength"):
                h += f'<div class="ins-quote good expandable"><strong>\u10eb\u10da\u10d8\u10d4\u10e0\u10d8:</strong> <span class="expand-text">{_esc(str(ins["top_strength"]))}</span></div>'
            if ins.get("top_weakness"):
                h += f'<div class="ins-quote growth expandable"><strong>\u10d2\u10d0\u10dc\u10d5\u10d8\u10d7\u10d0\u10e0\u10d4\u10d1\u10d0:</strong> <span class="expand-text">{_esc(str(ins["top_weakness"]))}</span></div>'
            if just_html:
                h += f'<details class="just-details"><summary>\u10e5\u10e3\u10da\u10d4\u10d1\u10d8\u10e1 \u10d3\u10d0\u10e1\u10d0\u10d1\u10e3\u10d7\u10d4\u10d1\u10d0</summary>{just_html}</details>'
            h += '</div>'

        h += '</div>'
        return h

    # Generate narrative HTML (convert **bold** to <strong>)
    if data.get("total_processed"):
        raw_narrative = generate_performance_narrative(data)
        def _bold_to_strong(m):
            return "<strong>" + _esc(m.group(1)) + "</strong>"
        narrative_html = re.sub(r"\*\*(.+?)\*\*", _bold_to_strong, raw_narrative)
    else:
        narrative_html = '<span style="color:var(--muted)">\u10db\u10dd\u10dc\u10d0\u10ea\u10d4\u10db\u10d4\u10d1\u10d8 \u10ef\u10d4\u10e0 \u10d0\u10e0 \u10d0\u10e0\u10d8\u10e1</span>'

    # ── Pre-compute new research metrics for template (can't use {{}} in f-strings) ──
    _kirk = g2.get("kirkpatrick") or {}
    _kirk_l1 = _kirk.get("L1_reaction")
    _kirk_l2 = _kirk.get("L2_learning")
    _kirk_l3 = _kirk.get("L3_behavior")
    _kirk_l4 = _kirk.get("L4_results")
    _bench_gap = g2.get("benchmark_gap", 0) or 0
    _tp_ratio = g2.get("theory_practice_ratio")
    _velocity = g2.get("velocity", 0) or 0
    _vel_label = g2.get("velocity_label", "—")
    _ltt = g2.get("lectures_to_target")
    _rec_ft = g2.get("recommendation_followthrough", 0)
    _at_risk = g2.get("at_risk_dims", [])
    _at_risk_names = ", ".join(_esc(DIMENSION_LABELS_KA.get(d["dim"], d["dim"])) for d in _at_risk) if _at_risk else "არ არის"

    # ── TPI gauge arc calculation ──
    tpi_val = tpi if tpi is not None else 0
    tpi_pct = min(tpi_val / 10, 1.0)
    tpi_arc_len = 251.2  # 2 * pi * 40
    tpi_offset = tpi_arc_len * (1 - tpi_pct)
    tpi_color = "#34d399" if tpi_val >= 7 else ("#fbbf24" if tpi_val >= 5 else "#f87171")

    # ── Level determination ──
    levels = [
        (8, "Master", "\U0001F451"),
        (7, "Advanced", "\U0001F525"),
        (6, "Proficient", "\u2B50"),
        (5, "Developing", "\U0001F33F"),
        (0, "Beginner", "\U0001F331"),
    ]
    level_name = "Beginner"
    level_icon = "\U0001F331"
    for threshold, name, icon in levels:
        if tpi_val >= threshold:
            level_name = name
            level_icon = icon
            break

    # ── Streak calculation (consecutive improvements across all lectures) ──
    all_composites_ordered: list[float] = []
    for gn_val in [1, 2]:
        for row in data["groups"][gn_val].get("scores", []):
            all_composites_ordered.append(row["composite"])
    streak = 0
    if len(all_composites_ordered) >= 2:
        for i in range(len(all_composites_ordered) - 1, 0, -1):
            if all_composites_ordered[i] > all_composites_ordered[i - 1]:
                streak += 1
            else:
                break

    # ── Progress timeline dots for each group ──
    def _timeline_dots(group_data: dict, group_num: int) -> str:
        scores = group_data.get("scores", [])
        score_map = {r["lecture_number"]: r["composite"] for r in scores}
        # Find the last completed lecture number and the next upcoming one
        completed_nums = sorted(score_map.keys())
        last_completed = completed_nums[-1] if completed_nums else 0
        next_lecture = last_completed + 1
        dots = ""
        for i in range(1, total + 1):
            if i in score_map:
                sc = score_map[i]
                color = "#34d399" if sc >= 7 else ("#fbbf24" if sc >= 5 else "#f87171")
                cls = "tl-dot completed"
                if i == last_completed:
                    cls += " current"
                dots += f'<div class="{cls}" style="background:{color}" title="\u10da#{i}: {sc}">{i}</div>'
            elif i == next_lecture and next_lecture <= total:
                dots += f'<div class="tl-dot next" title="\u10e8\u10d4\u10db\u10d3\u10d4\u10d2\u10d8">{i}</div>'
            else:
                dots += f'<div class="tl-dot upcoming">{i}</div>'
        fill_pct = (last_completed / total * 100) if total > 0 else 0
        return f'''<div class="tl-track">
            <div class="tl-line"></div>
            <div class="tl-line-fill" style="width:{fill_pct}%"></div>
            {dots}
        </div>'''

    # ── Mini gauge SVG helper ──
    def _mini_gauge(score, label: str) -> str:
        if score is None:
            return f'''<div class="sub-card">
                <div class="mg-wrap"><svg viewBox="0 0 44 44"><circle cx="22" cy="22" r="18" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="3"/></svg>
                <span class="mg-val" style="color:var(--muted)">\u2014</span></div>
                <div class="mg-label">{label}</div></div>'''
        r = 18
        circ = 2 * math.pi * r
        pct = min(score / 10, 1.0)
        offset = circ * (1 - pct)
        color = "#34d399" if score >= 7 else ("#fbbf24" if score >= 5 else "#f87171")
        return f'''<div class="sub-card">
            <div class="mg-wrap"><svg viewBox="0 0 44 44" style="transform:rotate(-90deg)">
                <circle cx="22" cy="22" r="{r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="3"/>
                <circle cx="22" cy="22" r="{r}" fill="none" stroke="{color}" stroke-width="3"
                    stroke-linecap="round" stroke-dasharray="{circ:.1f}" stroke-dashoffset="{offset:.1f}"
                    style="transition:stroke-dashoffset 1s cubic-bezier(0.4,0,0.2,1)"/>
            </svg><span class="mg-val" style="color:{color}">{_fmt(score)}</span></div>
            <div class="mg-label">{label}</div></div>'''

    # ── Lecture cards ──
    all_rows = sorted(
        g1["scores"] + g2["scores"],
        key=lambda r: (r["group_number"], r["lecture_number"]),
    )

    def _lecture_card(r: dict, prev_composite: float | None) -> str:
        comp = r["composite"]
        gn = r["group_number"]
        ln = r["lecture_number"]
        sc_cls = _sc(comp)

        # Delta badge
        if prev_composite is not None:
            delta = round(comp - prev_composite, 1)
            if delta > 0:
                delta_html = f'<span class="delta-badge delta-up">\u2191+{delta}</span>'
            elif delta < 0:
                delta_html = f'<span class="delta-badge delta-down">\u2193{delta}</span>'
            else:
                delta_html = '<span class="delta-badge delta-flat">\u2013 0.0</span>'
        else:
            delta_html = '<span class="delta-badge delta-flat">\u2014</span>'

        # Sparkline SVG from dimension scores
        dims_vals = [r[d] for d in DIMENSIONS]
        spark_points = []
        for idx, v in enumerate(dims_vals):
            x = 4 + idx * 18
            y = 24 - (v / 10 * 20)
            spark_points.append(f"{x},{y:.1f}")
        spark_line = " ".join(spark_points)
        spark_color = "#34d399" if comp >= 7 else ("#fbbf24" if comp >= 5 else "#f87171")

        # Mini heatmap row
        hm_cells = ""
        for d in DIMENSIONS:
            v = r[d]
            hm_color = "rgba(52,211,153,0.6)" if v >= 7 else ("rgba(251,191,36,0.5)" if v >= 5 else "rgba(248,113,113,0.5)")
            hm_cells += f'<div class="lc-hm-cell" style="background:{hm_color}" title="{data["dimension_labels_ka"].get(d, d)}: {v}">{v}</div>'

        return f'''<div class="lec-card card">
            <div class="lc-header">
                <span class="lc-num">\u10da\u10d4\u10e5\u10ea\u10d8\u10d0 #{ln}</span>
                <span class="grp-badge g{gn}">\u10ef\u10d2 #{gn}</span>
            </div>
            <div class="lc-body">
                <div class="lc-spark">
                    <svg viewBox="0 0 80 28" preserveAspectRatio="none" role="img" aria-label="\u10e5\u10e3\u10da\u10d4\u10d1\u10d8\u10e1 \u10e2\u10e0\u10d4\u10dc\u10d3\u10d8">
                        <polyline points="{spark_line}" fill="none" stroke="{spark_color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        <circle cx="{spark_points[-1].split(",")[0]}" cy="{spark_points[-1].split(",")[1]}" r="2" fill="{spark_color}"/>
                    </svg>
                </div>
                <div class="lc-score">
                    <span class="score-pill sc-bg-{sc_cls}">{comp}</span>
                    {delta_html}
                </div>
            </div>
            <div class="lc-heatmap">{hm_cells}</div>
        </div>'''

    lecture_cards_html = ""
    prev_composites: dict[int, float] = {}
    for r in all_rows:
        gn = r["group_number"]
        prev = prev_composites.get(gn)
        lecture_cards_html += _lecture_card(r, prev)
        prev_composites[gn] = r["composite"]

    # ── Dimension ranking with bullet chart style ──
    def _bullet_row(dr: dict) -> str:
        d = dr["dim"]
        m = dr["mean"]
        label = data["dimension_labels_ka"].get(d, d)
        if m is None:
            return ""
        bar_w = m * 10
        target_left = 70  # 7.0/10 = 70%
        sc = _sc(m)
        bar_color = "#34d399" if m >= 7 else ("#fbbf24" if m >= 5 else "#f87171")
        gap = round(7.0 - m, 1)
        gap_html = ""
        if gap > 0:
            gap_html = f'<span class="bullet-gap">-{gap}</span>'
        elif gap < 0:
            gap_html = f'<span class="bullet-over">+{abs(gap)}</span>'
        return f'''<div class="bullet-row">
            <span class="bullet-label">{label}</span>
            <div class="bullet-track">
                <div class="bullet-range-low"></div>
                <div class="bullet-range-mid"></div>
                <div class="bullet-range-high"></div>
                <div class="bullet-bar" style="width:{bar_w}%;background:{bar_color}"></div>
                <div class="bullet-target" style="left:{target_left}%"></div>
            </div>
            <span class="bullet-val sc-{sc}">{_fmt(m)}</span>
            {gap_html}
        </div>'''

    bullet_html = "".join(_bullet_row(dr) for dr in dim_rankings if dr["mean"] is not None)

    # ── Achievement badges ──
    def _check_achievements() -> str:
        badges_def = [
            ("first_lecture", "\U0001F3AC", "\u10de\u10d8\u10e0\u10d5\u10d4\u10da\u10d8 \u10da\u10d4\u10e5\u10ea\u10d8\u10d0",
             "\u10de\u10d8\u10e0\u10d5\u10d4\u10da\u10d8 \u10da\u10d4\u10e5\u10ea\u10d8\u10d8\u10e1 \u10d0\u10dc\u10d0\u10da\u10d8\u10d6\u10d8",
             data["total_processed"] >= 1),
            ("rising_star", "\U0001F31F", "\u10d0\u10db\u10dd\u10db\u10d0\u10d5\u10d0\u10da\u10d8 \u10d5\u10d0\u10e0\u10e1\u10d9\u10d5\u10da\u10d0\u10d5\u10d8",
             "2+ \u10d6\u10d4\u10d3\u10d8\u10d6\u10d4\u10d3 \u10d2\u10d0\u10e3\u10db\u10ef\u10dd\u10d1\u10d4\u10e1\u10d4\u10d1\u10d0",
             streak >= 2),
            ("target_hit", "\U0001F3C6", "\u10e1\u10d0\u10db\u10d8\u10d6\u10dc\u10d4 \u10db\u10d8\u10e6\u10ec\u10d4\u10e3\u10da\u10d8\u10d0",
             "\u10d9\u10dd\u10db\u10de\u10dd\u10d6\u10d8\u10e2\u10d8 >= 7.0",
             any(r["composite"] >= 7 for r in all_rows)),
            ("engagement_master", "\U0001F3AF", "\u10e9\u10d0\u10e0\u10d7\u10e3\u10da\u10dd\u10d1\u10d8\u10e1 \u10db\u10d0\u10e1\u10e2\u10d4\u10e0\u10d8",
             "\u10e9\u10d0\u10e0\u10d7\u10e3\u10da\u10dd\u10d1\u10d0 > 8",
             any(r["engagement"] > 8 for r in all_rows)),
            ("marathon", "\U0001F3C3", "\u10db\u10d0\u10e0\u10d0\u10d7\u10dd\u10dc\u10d4\u10da\u10d8",
             "5+ \u10da\u10d4\u10e5\u10ea\u10d8\u10d0 \u10d3\u10d0\u10db\u10e3\u10e8\u10d0\u10d5\u10d4\u10d1\u10e3\u10da\u10d8",
             data["total_processed"] >= 5),
            ("consistency", "\U0001F4CA", "\u10d7\u10d0\u10dc\u10db\u10d8\u10db\u10d3\u10d4\u10d5\u10e0\u10e3\u10da\u10d8",
             "\u10e1\u10e2\u10d3. \u10d2\u10d0\u10d3\u10d0\u10ee\u10e0\u10d0 < 1.0",
             any(
                 g.get("consistency") is not None and g["consistency"] >= 9
                 for g in [g1, g2]
             )),
        ]
        html_parts = ""
        for badge_id, icon, name, desc, earned in badges_def:
            cls = "badge earned" if earned else "badge locked"
            check = '<div class="badge-check">\u2713</div>' if earned else '<div class="badge-lock">\U0001F512</div>'
            html_parts += f'''<div class="{cls}">
                {check}
                <div class="badge-icon">{icon}</div>
                <div class="badge-name">{name}</div>
                <div class="badge-desc">{desc}</div>
            </div>'''
        return html_parts

    achievements_html = _check_achievements()

    # ── Cross-group radar + delta summary ──
    cross_group = data.get("cross_group", {})
    # ── Pre-compute streak flames HTML ──
    streak_flames_html = ""
    for i in range(7):
        if i < streak:
            flame_h = 20 + (i + 1) * 5
            delay = f";animation-delay:{i * 0.15}s"
            streak_flames_html += f'<div class="streak-flame active" style="height:{flame_h}px{delay}"></div>'
        else:
            streak_flames_html += '<div class="streak-flame" style="height:12px"></div>'

    # ── Pre-compute strings that contain \u escapes (Python 3.9 disallows them in f-string exprs) ──
    _tpi_display = _fmt(tpi) if tpi else "\u2014"
    _tpi_display2 = _fmt(tpi) if tpi else "\u2014"
    _gauge_pedagogy = _mini_gauge(g2.get('pedagogy_score') if g2['lecture_count'] else None, "\u10de\u10d4\u10d3\u10d0\u10d2\u10dd\u10d2\u10d8\u10d9\u10d0")
    _gauge_content = _mini_gauge(g2.get('content_quality') if g2['lecture_count'] else None, "\u10d9\u10dd\u10dc\u10e2\u10d4\u10dc\u10e2\u10d8\u10e1 \u10ee\u10d0\u10e0\u10d8\u10e1\u10ee\u10d8")
    _gauge_impact = _mini_gauge(g2.get('impact_score') if g2['lecture_count'] else None, "\u10de\u10e0\u10d0\u10e5\u10e2. \u10d6\u10d4\u10d2\u10d0\u10d5\u10da\u10d4\u10dc\u10d0")
    _gauge_balance = _mini_gauge(g2.get('balance_score') if g2['lecture_count'] else None, "\u10d1\u10d0\u10da\u10d0\u10dc\u10e1\u10d8")
    _no_lectures_msg = '<div class="card"><p class="empty-state">\u10ef\u10d4\u10e0 \u10d0\u10e0 \u10d0\u10e0\u10d8\u10e1 \u10da\u10d4\u10e5\u10ea\u10d8\u10d4\u10d1\u10d8 \u10d3\u10d0\u10db\u10e3\u10e8\u10d0\u10d5\u10d4\u10d1\u10e3\u10da\u10d8</p></div>'
    _no_data_msg = '<p class="empty-state">\u10db\u10dd\u10dc\u10d0\u10ea\u10d4\u10db\u10d4\u10d1\u10d8 \u10ef\u10d4\u10e0 \u10d0\u10e0 \u10d0\u10e0\u10d8\u10e1</p>'

    # ── Lecture Comparison Table HTML ──
    def _cell_cls(v: float | None) -> str:
        if v is None:
            return ""
        if v >= 7:
            return "cell-good"
        if v >= 5:
            return "cell-mid"
        return "cell-bad"

    cmp_table_rows = ""
    for r in all_rows:
        gn = r["group_number"]
        ln = r["lecture_number"]
        comp = r["composite"]
        cmp_table_rows += f'<tr><td><span class="grp-badge g{gn}">\u10ef\u10d2 #{gn}</span> \u10da\u10d4\u10e5\u10ea\u10d8\u10d0 #{ln}</td>'
        for d in DIMENSIONS:
            v = r[d]
            cmp_table_rows += f'<td><span class="sc-cell {_cell_cls(v)}">{_fmt(v)}</span></td>'
        cmp_table_rows += f'<td><span class="sc-cell cell-comp {_cell_cls(comp)}">{_fmt(comp)}</span></td></tr>'

    dim_labels_ka_short = {
        "content_depth": "\u10e8\u10d8\u10dc\u10d0\u10d0\u10e0\u10e1\u10d8\u10e1 \u10e1\u10d8\u10e6\u10e0\u10db\u10d4",
        "practical_value": "\u10de\u10e0\u10d0\u10e5\u10e2\u10d8\u10d9\u10e3\u10da\u10d8 \u10e6\u10d8\u10e0\u10d4\u10d1\u10e3\u10da\u10d4\u10d1\u10d0",
        "engagement": "\u10e9\u10d0\u10e0\u10d7\u10e3\u10da\u10dd\u10d1\u10d0",
        "technical_accuracy": "\u10e2\u10d4\u10e5\u10dc\u10d8\u10d9\u10e3\u10e0\u10d8 \u10e1\u10d8\u10d6\u10e3\u10e1\u10e2\u10d4",
        "market_relevance": "\u10d1\u10d0\u10d6\u10e0\u10d8\u10e1 \u10e0\u10d4\u10da\u10d4\u10d5\u10d0\u10dc\u10e2\u10e3\u10e0\u10dd\u10d1\u10d0",
    }

    cmp_table_header = "<tr><th>\u10da\u10d4\u10e5\u10ea\u10d8\u10d0</th>"
    for d in DIMENSIONS:
        cmp_table_header += f"<th>{dim_labels_ka_short.get(d, d)}</th>"
    cmp_table_header += "<th>\u10d9\u10dd\u10db\u10de\u10dd\u10d6\u10d8\u10e2\u10e3\u10e0\u10d8</th></tr>"

    cross_summary_html = ""
    for dim in DIMENSIONS:
        cg = cross_group.get(dim, {})
        delta = cg.get("delta")
        if delta is not None:
            label = data["dimension_labels_ka"].get(dim, dim)
            cls = "delta-up" if delta > 0 else ("delta-down" if delta < 0 else "delta-flat")
            sign = "+" if delta > 0 else ""
            cross_summary_html += f'<div class="cross-item"><span class="cross-dim">{label}</span><span class="delta-badge {cls}">{sign}{delta}</span></div>'

    html = f"""<!DOCTYPE html>
<html lang="ka">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI \u10d9\u10e3\u10e0\u10e1\u10d8 \u2014 Trainer Analytics</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Georgian:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js" defer></script>
<style>
:root {{
  --bg: #06080f;
  --card: rgba(17,24,39,0.7);
  --card-border: rgba(99,102,241,0.12);
  --card-hover: rgba(99,102,241,0.25);
  --accent: #6366f1;
  --accent-glow: rgba(99,102,241,0.25);
  --cyan: #22d3ee;
  --good: #34d399;
  --mid: #fbbf24;
  --bad: #f87171;
  --rose: #fb7185;
  --text: #f1f5f9;
  --text2: #cbd5e1;
  --muted: #94a3b8;
  --dim: #475569;
  --surface: rgba(30,36,52,0.6);
  --radius: 16px;
}}
*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
html {{ scroll-behavior:smooth; }}
body {{
  background: var(--bg); color: var(--text);
  font-family: 'Noto Sans Georgian', 'Inter', system-ui, -apple-system, sans-serif;
  min-height: 100vh; -webkit-font-smoothing: antialiased;
  font-size: 14px; line-height: 1.5;
}}
body::before {{
  content:''; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:
    radial-gradient(ellipse 80% 60% at 50% -20%, rgba(99,102,241,0.1), transparent 70%),
    radial-gradient(ellipse 60% 50% at 80% 100%, rgba(52,211,153,0.05), transparent 60%);
}}

/* ── Layout ── */
.wrap {{ max-width:1200px; margin:0 auto; padding:2rem clamp(1rem,3vw,2rem); display:flex; flex-direction:column; gap:1.5rem; }}

/* ── Cards ── */
.card {{
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: var(--radius);
  padding: 1.5rem;
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  transition: border-color 0.3s, box-shadow 0.3s;
  position: relative; overflow: visible;
}}
.card:hover {{ border-color: var(--card-hover); }}

.card-label {{
  font-size: 0.65rem; font-weight: 600; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--dim); margin-bottom: 0.75rem;
}}

/* ── Section headings ── */
h2.sec {{
  font-size: 0.95rem; font-weight: 700; color: var(--text);
  display: flex; align-items: center; gap: 0.5rem; margin: 0;
}}
h2.sec::before {{
  content:''; width:3px; height:1.1em; border-radius:2px;
  background: linear-gradient(135deg, var(--accent), #8b5cf6);
}}

/* ── Header ── */
.dash-header {{
  display:flex; align-items:flex-end; justify-content:space-between; flex-wrap:wrap; gap:1rem;
  padding-bottom:0.5rem; border-bottom:1px solid rgba(99,102,241,0.08);
}}
.dash-header h1 {{
  font-size:1.5rem; font-weight:800; letter-spacing:-0.02em;
}}
.dash-header h1 span {{
  background: linear-gradient(135deg, var(--accent), #8b5cf6);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
}}
.dash-header p {{ color:var(--muted); font-size:0.78rem; margin-top:0.2rem; }}
.header-right {{ display:flex; align-items:center; gap:0.75rem; }}
.live-dot {{ width:8px; height:8px; border-radius:50%; background:var(--good); }}
.live-dot {{ animation: livePulse 2s ease-in-out infinite; }}
@keyframes livePulse {{ 0%,100%{{opacity:1;transform:scale(1)}} 50%{{opacity:0.4;transform:scale(0.8)}} }}
.refresh-label {{ font-size:0.72rem; color:var(--muted); }}

/* ── Hero: TPI Gauge + Level + Streak ── */
.hero-row {{
  display:grid; grid-template-columns: 280px 200px 1fr; gap:1.25rem; align-items:stretch;
}}
@media (max-width:900px) {{ .hero-row {{ grid-template-columns:1fr 1fr; }} .hero-row .card:last-child {{ grid-column:1/-1; }} }}
@media (max-width:640px) {{ .hero-row {{ grid-template-columns:1fr; }} }}

.tpi-card {{ display:flex; flex-direction:column; align-items:center; text-align:center; padding:1.75rem 1rem; }}
.tpi-gauge {{ position:relative; width:140px; height:140px; margin-bottom:0.75rem; }}
.tpi-gauge svg {{ width:100%; height:100%; transform:rotate(-90deg); }}
.tpi-gauge .gauge-bg {{ fill:none; stroke:rgba(99,102,241,0.1); stroke-width:6; }}
.tpi-gauge .gauge-fg {{
  fill:none; stroke-width:6; stroke-linecap:round;
  transition: stroke-dashoffset 1.5s cubic-bezier(0.4,0,0.2,1);
}}
.tpi-center {{
  position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
  text-align:center;
}}
.tpi-value {{ font-size:2.2rem; font-weight:800; line-height:1; }}
.tpi-sub {{ font-size:0.65rem; color:var(--muted); margin-top:2px; text-transform:uppercase; letter-spacing:0.05em; }}
.tpi-label {{ font-size:0.72rem; color:var(--muted); font-weight:500; }}

.level-card {{ display:flex; flex-direction:column; align-items:center; text-align:center; padding:1.5rem 1rem; }}
.level-icon {{ font-size:2.5rem; margin-bottom:0.5rem; }}
.level-name {{ font-size:1rem; font-weight:700; color:var(--accent); }}
.level-score {{ font-size:0.72rem; color:var(--muted); margin-top:0.2rem; }}

.streak-card {{ display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; }}
.streak-flames {{ display:flex; align-items:flex-end; gap:3px; height:50px; margin-bottom:0.5rem; }}
.streak-flame {{
  width:16px; border-radius:50% 50% 50% 50% / 60% 60% 40% 40%;
  background: linear-gradient(to top, var(--bad), var(--mid));
  opacity:0.12;
}}
.streak-flame.active {{
  opacity:1; box-shadow: 0 0 10px rgba(251,191,36,0.4);
  animation: flicker 2s ease-in-out infinite alternate;
}}
@keyframes flicker {{
  0% {{ transform:scaleY(1) scaleX(1); }}
  50% {{ transform:scaleY(1.06) scaleX(0.96); }}
  100% {{ transform:scaleY(0.95) scaleX(1.03); }}
}}
.streak-number {{
  font-size:2rem; font-weight:900; line-height:1;
  background: linear-gradient(135deg, var(--mid), var(--bad));
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
}}
.streak-label {{ font-size:0.7rem; color:var(--muted); margin-top:0.2rem; }}

/* ── Data Story ── */
.narrative-card {{
  border-left:3px solid var(--accent);
  padding:1rem 1.25rem;
}}
.narrative-card .narrative-text {{ font-size:0.82rem; line-height:1.75; color:var(--text2); }}

/* ── Progress Timeline ── */
.tl-section {{ display:flex; flex-direction:column; gap:1rem; }}
.tl-group-label {{ font-size:0.72rem; font-weight:600; color:var(--muted); margin-bottom:0.25rem; }}
.tl-track {{
  display:flex; align-items:center; gap:0; position:relative; padding:8px 0;
}}
.tl-line {{
  position:absolute; top:50%; left:14px; right:14px; height:2px;
  background:rgba(71,85,105,0.3); transform:translateY(-50%); z-index:0;
}}
.tl-line-fill {{
  position:absolute; top:50%; left:14px; height:2px;
  background:linear-gradient(90deg, var(--accent), var(--good));
  transform:translateY(-50%); z-index:0; border-radius:1px;
  transition:width 1s cubic-bezier(0.4,0,0.2,1);
}}
.tl-dot {{
  flex:1; display:flex; align-items:center; justify-content:center;
  width:26px; height:26px; min-width:26px; border-radius:50%;
  font-size:0.58rem; font-weight:700; position:relative; z-index:1;
  border:2px solid var(--dim); color:var(--dim); background:transparent;
  transition:all 0.3s ease; cursor:default;
}}
.tl-dot.completed {{
  border-color:transparent; color:var(--bg); font-weight:800;
}}
.tl-dot.current {{
  animation: dotPulse 2s ease-in-out infinite;
}}
@keyframes dotPulse {{
  0%,100% {{ box-shadow:0 0 0 0 rgba(99,102,241,0.4); }}
  50% {{ box-shadow:0 0 0 6px rgba(99,102,241,0); }}
}}
.tl-dot.next {{
  border-color:var(--accent); color:var(--accent);
  animation: dotPulse 2s ease-in-out infinite;
}}
.tl-dot.upcoming {{
  border-color:rgba(100,116,139,0.3); color:rgba(100,116,139,0.4);
}}
.tl-dot:hover {{ transform:scale(1.15); }}

/* ── Trainer Sub-Scores (mini gauges) ── */
.sub-scores {{ display:grid; grid-template-columns:repeat(4,1fr); gap:1rem; }}
@media (max-width:640px) {{ .sub-scores {{ grid-template-columns:repeat(2,1fr); }} }}
.sub-card {{
  display:flex; flex-direction:column; align-items:center; text-align:center;
  background:rgba(255,255,255,0.02); border-radius:12px; padding:1.25rem 0.5rem;
  transition:background 0.2s;
}}
.sub-card:hover {{ background:rgba(255,255,255,0.04); }}
.mg-wrap {{
  position:relative; width:44px; height:44px;
  display:flex; align-items:center; justify-content:center;
  margin-bottom:0.5rem;
}}
.mg-wrap svg {{ position:absolute; inset:0; width:100%; height:100%; }}
.mg-val {{ position:relative; z-index:1; font-size:0.82rem; font-weight:700; }}
.mg-label {{ font-size:0.68rem; color:var(--muted); font-weight:500; overflow-wrap:break-word; word-break:break-word; line-height:1.3; max-width:100%; }}

/* ── Lecture Cards ── */
.lec-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:1rem; }}
.lec-card {{ padding:1rem 1.25rem; }}
.lc-header {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:0.75rem; }}
.lc-num {{ font-size:0.88rem; font-weight:700; }}
.grp-badge {{ font-size:0.6rem; font-weight:700; padding:0.1rem 0.45rem; border-radius:99px; white-space:nowrap; }}
.grp-badge.g1 {{ background:rgba(99,102,241,0.15); color:var(--accent); }}
.grp-badge.g2 {{ background:rgba(34,211,238,0.15); color:var(--cyan); }}
.lc-body {{ display:flex; align-items:center; gap:0.75rem; margin-bottom:0.75rem; }}
.lc-spark {{ flex:1; height:28px; }}
.lc-spark svg {{ width:100%; height:100%; overflow:visible; }}
.lc-score {{ display:flex; align-items:center; gap:0.4rem; flex-shrink:0; }}
.score-pill {{
  display:inline-flex; align-items:center; justify-content:center;
  padding:0.2rem 0.6rem; border-radius:99px; font-size:0.82rem; font-weight:700;
  color:var(--bg);
}}
.sc-bg-good {{ background:var(--good); }}
.sc-bg-mid {{ background:var(--mid); }}
.sc-bg-bad {{ background:var(--bad); }}
.sc-bg-na {{ background:var(--dim); color:var(--muted); }}
.delta-badge {{
  display:inline-flex; align-items:center; gap:1px;
  padding:0.1rem 0.45rem; border-radius:99px;
  font-size:0.68rem; font-weight:600; white-space:nowrap;
}}
.delta-up {{ color:var(--good); background:rgba(52,211,153,0.12); }}
.delta-down {{ color:var(--bad); background:rgba(248,113,113,0.12); }}
.delta-flat {{ color:var(--muted); background:rgba(100,116,139,0.12); }}
.lc-heatmap {{
  display:grid; grid-template-columns:repeat(5,1fr); gap:3px;
}}
.lc-hm-cell {{
  border-radius:4px; text-align:center; font-size:0.62rem; font-weight:600;
  padding:3px 0; color:var(--bg);
}}

/* ── Bullet Chart / Dimension Ranking ── */
.bullet-row {{ display:flex; align-items:center; gap:0.5rem; padding:0.4rem 0; }}
.bullet-label {{ font-size:0.72rem; color:var(--text2); width:140px; flex-shrink:0; text-align:right; overflow-wrap:break-word; word-break:break-word; hyphens:auto; line-height:1.3; }}
.bullet-track {{
  flex:1; height:16px; border-radius:4px; position:relative; overflow:visible;
  display:flex; background:rgba(30,41,59,0.6);
}}
.bullet-range-low {{ width:40%; height:100%; background:rgba(248,113,113,0.08); }}
.bullet-range-mid {{ width:30%; height:100%; background:rgba(251,191,36,0.06); }}
.bullet-range-high {{ width:30%; height:100%; background:rgba(52,211,153,0.05); }}
.bullet-bar {{
  position:absolute; top:4px; left:0; height:8px; border-radius:2px;
  transition:width 0.8s cubic-bezier(0.4,0,0.2,1);
}}
.bullet-target {{
  position:absolute; top:1px; width:2px; height:14px;
  background:var(--text); border-radius:1px; opacity:0.5;
}}
.bullet-val {{ font-size:0.78rem; font-weight:700; width:32px; }}
.bullet-gap {{ font-size:0.62rem; font-weight:600; color:var(--bad); width:32px; }}
.bullet-over {{ font-size:0.62rem; font-weight:600; color:var(--good); width:32px; }}

/* ── Insights ── */
.ins-cards-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:1rem; }}
@media (max-width:640px) {{ .ins-cards-grid {{ grid-template-columns:1fr; }} }}
.ins-card {{ padding:1.25rem; }}
.group-header {{ display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem; }}
.group-dot {{ width:10px; height:10px; border-radius:50%; }}
.g1-dot {{ background:var(--accent); }}
.g2-dot {{ background:var(--cyan); }}
.group-name {{ font-size:0.85rem; font-weight:700; }}
.insights-grid {{ display:grid; grid-template-columns:repeat(6,1fr); gap:0.4rem; margin:0.5rem 0; }}
@media (max-width:640px) {{ .insights-grid {{ grid-template-columns:repeat(3,1fr); }} }}
.ins-stat {{ text-align:center; padding:0.4rem 0.15rem; background:rgba(255,255,255,0.02); border-radius:8px; }}
.ins-num {{ font-size:1.3rem; font-weight:800; display:block; line-height:1; }}
.ins-lbl {{ font-size:0.55rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; margin-top:0.15rem; display:block; overflow-wrap:break-word; word-break:break-word; line-height:1.3; }}
.ins-quote {{ font-size:0.72rem; color:var(--text2); padding:0.5rem 0.65rem; margin:0.4rem 0; border-radius:8px; line-height:1.5; border-left:3px solid; }}
.ins-quote.good {{ border-color:var(--good); background:rgba(52,211,153,0.04); }}
.ins-quote.growth {{ border-color:var(--mid); background:rgba(251,191,36,0.04); }}

/* ── Expand/Collapse for insights text ── */
.ins-quote.expandable {{ cursor:pointer; position:relative; }}
.ins-quote.expandable .expand-text {{ display:block; }}
.ins-quote.expandable.collapsed .expand-text {{
  display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden;
}}
.ins-quote.expandable::after {{
  content:'ვრცლად \u25BC'; display:block; margin-top:0.35rem;
  font-size:0.62rem; font-weight:600; color:var(--accent); text-align:right;
}}
.ins-quote.expandable.collapsed::after {{ content:'ვრცლად \u25BC'; }}
.ins-quote.expandable:not(.collapsed)::after {{ content:'შეკვეცა \u25B2'; }}

/* ── Lecture Comparison Table ── */
.cmp-table-wrap {{ overflow-x:auto; -webkit-overflow-scrolling:touch; margin-top:0.75rem; }}
.cmp-table {{
  width:100%; border-collapse:collapse; font-size:0.78rem;
  background:var(--card); border-radius:12px; overflow:hidden;
}}
.cmp-table thead th {{
  background:var(--surface); color:var(--muted); font-weight:700;
  font-size:0.65rem; text-transform:uppercase; letter-spacing:0.05em;
  padding:0.65rem 0.5rem; text-align:center; white-space:normal;
  border-bottom:1px solid var(--card-border); overflow-wrap:break-word; word-break:break-word;
}}
.cmp-table thead th:first-child {{ text-align:left; padding-left:1rem; }}
.cmp-table tbody tr {{ border-bottom:1px solid rgba(255,255,255,0.03); transition:background 0.15s; }}
.cmp-table tbody tr:hover {{ background:rgba(99,102,241,0.06); }}
.cmp-table tbody td {{
  padding:0.55rem 0.5rem; text-align:center; font-weight:600;
}}
.cmp-table tbody td:first-child {{ text-align:left; padding-left:1rem; font-weight:700; }}
.cmp-table .sc-cell {{
  display:inline-block; min-width:32px; padding:0.15rem 0.4rem;
  border-radius:6px; font-size:0.72rem; font-weight:700; text-align:center;
}}
.cmp-table .sc-cell.cell-good {{ background:rgba(52,211,153,0.18); color:var(--good); }}
.cmp-table .sc-cell.cell-mid {{ background:rgba(251,191,36,0.18); color:var(--mid); }}
.cmp-table .sc-cell.cell-bad {{ background:rgba(248,113,113,0.18); color:var(--bad); }}
.cmp-table .sc-cell.cell-comp {{ font-size:0.82rem; padding:0.2rem 0.55rem; }}

/* ── Charts: bar chart section ── */
.charts-2col {{ display:grid; grid-template-columns:1fr 1fr; gap:1.25rem; }}
@media (max-width:900px) {{ .charts-2col {{ grid-template-columns:1fr; }} }}

.just-details {{ margin-top:0.4rem; }}
.just-details summary {{ font-size:0.68rem; color:var(--accent); cursor:pointer; font-weight:600; }}
.just-details summary:hover {{ text-decoration:underline; }}
.just-item {{ font-size:0.68rem; color:var(--text2); padding:0.25rem 0; border-bottom:1px solid rgba(255,255,255,0.03); line-height:1.4; }}
.just-dim {{ font-weight:600; color:var(--muted); }}

.sc-good {{ color:var(--good)!important; }}
.sc-mid {{ color:var(--mid)!important; }}
.sc-bad {{ color:var(--bad)!important; }}
.sc-na {{ color:var(--muted)!important; }}

.sw-section {{ margin-bottom:0.75rem; }}
.sw-section h4 {{ font-size:0.65rem; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:0.35rem; }}
.sw-good {{ color:var(--good); }}
.sw-growth {{ color:var(--mid); }}
.sw-item {{ display:flex; align-items:center; gap:0.35rem; font-size:0.78rem; padding:0.2rem 0; color:var(--text2); }}
.sw-dot {{ width:6px; height:6px; border-radius:50%; flex-shrink:0; }}
.sw-dot.good {{ background:var(--good); }}
.sw-dot.bad {{ background:var(--bad); }}
.empty-state {{ color:var(--muted); font-size:0.78rem; text-align:center; padding:1.5rem 1rem; }}

/* ── Achievement Badges ── */
.badges-grid {{ display:grid; grid-template-columns:repeat(6,1fr); gap:0.75rem; }}
@media (max-width:900px) {{ .badges-grid {{ grid-template-columns:repeat(3,1fr); }} }}
@media (max-width:640px) {{ .badges-grid {{ grid-template-columns:repeat(2,1fr); }} }}
.badge {{
  background:var(--card); border:1px solid var(--card-border); border-radius:14px;
  padding:1.25rem 0.75rem; text-align:center; position:relative; overflow:hidden;
  transition:all 0.3s cubic-bezier(0.4,0,0.2,1); cursor:default;
}}
.badge::before {{
  content:''; position:absolute; inset:0;
  background:radial-gradient(circle at 50% 0%, var(--accent-glow), transparent 70%);
  opacity:0; transition:opacity 0.3s;
}}
.badge:hover::before {{ opacity:1; }}
.badge.earned {{ border-color:rgba(99,102,241,0.25); }}
.badge.earned:hover {{ border-color:var(--accent); transform:translateY(-2px); box-shadow:0 6px 24px rgba(99,102,241,0.15); }}
.badge.locked {{ opacity:0.35; }}
.badge.locked:hover {{ opacity:0.5; }}
.badge-icon {{
  width:44px; height:44px; margin:0 auto 0.5rem; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  font-size:1.3rem; position:relative; z-index:1; transition:transform 0.3s;
}}
.badge.earned .badge-icon {{ background:linear-gradient(135deg, rgba(99,102,241,0.2), rgba(99,102,241,0.05)); box-shadow:0 0 16px var(--accent-glow); }}
.badge.locked .badge-icon {{ background:var(--surface); filter:grayscale(1); }}
.badge.earned:hover .badge-icon {{ transform:scale(1.1); }}
.badge-name {{ font-size:0.68rem; font-weight:700; position:relative; z-index:1; margin-bottom:2px; }}
.badge-desc {{ font-size:0.58rem; color:var(--muted); position:relative; z-index:1; line-height:1.4; }}
.badge.locked .badge-name, .badge.locked .badge-desc {{ color:var(--dim); }}
.badge-check {{
  position:absolute; top:8px; right:8px;
  width:16px; height:16px; border-radius:50%;
  background:var(--good); display:flex; align-items:center; justify-content:center;
  font-size:0.5rem; color:var(--bg); font-weight:900;
  box-shadow:0 0 8px rgba(52,211,153,0.4); z-index:2;
}}
.badge-lock {{
  position:absolute; top:8px; right:8px;
  font-size:0.6rem; color:var(--dim); z-index:2;
}}

/* ── Kirkpatrick Levels ── */
.kirk-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:0.75rem; margin-top:0.75rem; }}
@media (max-width:640px) {{ .kirk-grid {{ grid-template-columns:repeat(2,1fr); }} }}
.kirk-level {{ text-align:center; padding:1rem 0.5rem; background:var(--surface); border-radius:12px; border:1px solid var(--glass-border); }}
.kirk-num {{ font-size:0.6rem; font-weight:700; color:var(--accent); letter-spacing:0.1em; text-transform:uppercase; }}
.kirk-name {{ font-size:0.75rem; font-weight:600; color:var(--text); margin:0.25rem 0; overflow-wrap:break-word; word-break:break-word; }}
.kirk-score {{ font-size:1.8rem; font-weight:800; line-height:1; }}
.kirk-desc {{ font-size:0.6rem; color:var(--muted); margin-top:0.25rem; }}

/* ── Research Metrics Grid ── */
.metrics-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:0.75rem; margin-top:0.75rem; }}
@media (max-width:900px) {{ .metrics-grid {{ grid-template-columns:repeat(2,1fr); }} }}
@media (max-width:640px) {{ .metrics-grid {{ grid-template-columns:1fr; }} }}
.metric-card {{
  background:var(--surface); border:1px solid var(--glass-border); border-radius:12px;
  padding:1rem; display:flex; flex-direction:column; align-items:center; text-align:center;
  transition: border-color 0.2s, transform 0.2s;
}}
.metric-card:hover {{ border-color:rgba(99,102,241,0.2); transform:translateY(-2px); }}
.metric-icon {{ font-size:1.5rem; margin-bottom:0.4rem; }}
.metric-value {{ font-size:1.6rem; font-weight:800; line-height:1; }}
.metric-name {{ font-size:0.7rem; font-weight:600; color:var(--text); margin-top:0.4rem; text-transform:uppercase; letter-spacing:0.05em; overflow-wrap:break-word; word-break:break-word; line-height:1.3; }}
.metric-detail {{ font-size:0.62rem; color:var(--muted); margin-top:0.2rem; line-height:1.3; }}

/* ── Cross-Group Comparison ── */
.cross-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:1.25rem; }}
@media (max-width:640px) {{ .cross-grid {{ grid-template-columns:1fr; }} }}
.cross-summary {{ display:flex; flex-direction:column; gap:0.4rem; }}
.cross-item {{ display:flex; align-items:center; justify-content:space-between; padding:0.35rem 0; border-bottom:1px solid rgba(255,255,255,0.04); }}
.cross-dim {{ font-size:0.78rem; color:var(--text2); overflow-wrap:break-word; word-break:break-word; }}

/* ── Charts ── */
.chart-wrap {{ position:relative; height:300px; }}
@media (max-width:640px) {{ .chart-wrap {{ height:220px; }} }}

/* ── Footer ── */
.dash-footer {{
  padding:1rem 0; border-top:1px solid rgba(255,255,255,0.06);
  color:var(--muted); font-size:0.68rem; text-align:center;
  display:flex; justify-content:space-between; flex-wrap:wrap; gap:0.5rem;
}}

/* ── Animations ── */
@keyframes fadeUp {{
  from {{ opacity:0; transform:translateY(16px); }}
  to {{ opacity:1; transform:translateY(0); }}
}}
.card {{ animation:fadeUp 0.5s ease both; }}
.card:nth-child(2) {{ animation-delay:0.05s; }}
.card:nth-child(3) {{ animation-delay:0.1s; }}
.card:nth-child(4) {{ animation-delay:0.15s; }}
.card:nth-child(5) {{ animation-delay:0.2s; }}
.card:nth-child(6) {{ animation-delay:0.25s; }}

/* ── Reduced motion ── */
@media (prefers-reduced-motion:reduce) {{
  *,*::before,*::after {{ animation-duration:0.01ms!important; transition-duration:0.01ms!important; }}
}}

/* ── Tablet ── */
@media (max-width:900px) {{
  .charts-2col {{ grid-template-columns:1fr; }}
  .cmp-table {{ font-size:0.72rem; }}
}}

/* ── Mobile ── */
@media (max-width:640px) {{
  .wrap {{ padding:1rem 0.75rem; }}
  .dash-header {{ flex-direction:column; align-items:flex-start; }}
  .card {{ padding:1rem; }}
  h2.sec {{ font-size:0.85rem; }}
  .tl-dot {{ width:20px; height:20px; min-width:20px; font-size:0.5rem; }}
  .dash-footer {{ flex-direction:column; text-align:center; }}
  .bullet-label {{ width:80px; font-size:0.65rem; }}
  .cmp-table {{ font-size:0.65rem; }}
  .cmp-table thead th {{ padding:0.4rem 0.3rem; font-size:0.58rem; }}
  .cmp-table tbody td {{ padding:0.4rem 0.3rem; }}
  .cmp-table .sc-cell {{ min-width:24px; padding:0.1rem 0.25rem; font-size:0.62rem; }}
  .lec-grid {{ grid-template-columns:1fr; }}
  .chart-wrap {{ height:200px; }}
  .ins-quote {{ font-size:0.68rem; }}
}}

/* ── Focus ── */
:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; border-radius:4px; }}

/* ── Touch devices ── */
@media (hover:none) and (pointer:coarse) {{
  .card {{ backdrop-filter:none; -webkit-backdrop-filter:none; background:rgba(17,24,39,0.92); }}
}}

/* ── Print ── */
@media print {{
  body {{ background:#fff!important; color:#1e293b!important; }}
  body::before {{ display:none!important; }}
  .card {{ background:#fff!important; border:1px solid #e2e8f0!important; backdrop-filter:none!important; box-shadow:none!important; animation:none!important; break-inside:avoid; }}
  .sc-good {{ color:#059669!important; }} .sc-mid {{ color:#d97706!important; }} .sc-bad {{ color:#dc2626!important; }}
  * {{ animation:none!important; transition:none!important; }}
  .chart-print-img {{ width:100%; max-height:280px; object-fit:contain; display:block; }}
  .chart-wrap {{ height:auto!important; }}
}}
</style>
</head>
<body>
<div class="wrap">

<!-- ════════ HEADER ════════ -->
<div class="dash-header">
  <div>
    <h1><span>AI \u10d9\u10e3\u10e0\u10e1\u10d8</span> \u2014 Trainer Analytics</h1>
    <p>\u10e2\u10e0\u10d4\u10dc\u10d4\u10e0\u10d8\u10e1 \u10de\u10d4\u10e0\u10e4\u10dd\u10e0\u10db\u10d0\u10dc\u10e1\u10d8\u10e1 \u10d0\u10dc\u10d0\u10da\u10d8\u10e2\u10d8\u10d9\u10d0 &middot; {generated_at}</p>
  </div>
  <div class="header-right">
    <div class="live-dot"></div>
    <span class="refresh-label">Auto-sync &middot; {data['total_processed']}/{total * 2} \u10da\u10d4\u10e5\u10ea\u10d8\u10d0</span>
  </div>
</div>

<!-- ════════ 1. HERO: TPI GAUGE + LEVEL + STREAK ════════ -->
<div class="hero-row">
  <div class="card tpi-card">
    <div class="card-label">Trainer Performance Index</div>
    <div class="tpi-gauge">
      <svg viewBox="0 0 100 100">
        <defs>
          <linearGradient id="gaugeGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="{tpi_color}"/>
            <stop offset="100%" stop-color="var(--accent)"/>
          </linearGradient>
        </defs>
        <circle class="gauge-bg" cx="50" cy="50" r="40"/>
        <circle class="gauge-fg" cx="50" cy="50" r="40"
          stroke="url(#gaugeGrad)"
          stroke-dasharray="{tpi_arc_len:.1f}"
          stroke-dashoffset="{tpi_offset:.1f}"/>
      </svg>
      <div class="tpi-center">
        <div class="tpi-value" style="color:{tpi_color}">{_tpi_display}</div>
        <div class="tpi-sub">/10</div>
      </div>
    </div>
    <div class="tpi-label">\u10e7\u10d5\u10d4\u10da\u10d0 \u10da\u10d4\u10e5\u10ea\u10d8\u10d8\u10e1 \u10d9\u10dd\u10db\u10de\u10dd\u10d6\u10d8\u10e2\u10d8</div>
  </div>

  <div class="card level-card">
    <div class="card-label">\u10e2\u10e0\u10d4\u10dc\u10d4\u10e0\u10d8\u10e1 \u10d3\u10dd\u10dc\u10d4</div>
    <div class="level-icon">{level_icon}</div>
    <div class="level-name">{level_name}</div>
    <div class="level-score">TPI: {_tpi_display2}</div>
  </div>

  <div class="card streak-card">
    <div class="card-label">\u10d2\u10d0\u10e3\u10db\u10ef\u10dd\u10d1\u10d4\u10e1\u10d4\u10d1\u10d8\u10e1 \u10e1\u10d4\u10e0\u10d8\u10d0</div>
    <div class="streak-flames">
      {streak_flames_html}
    </div>
    <div class="streak-number">{streak}</div>
    <div class="streak-label">\u10d6\u10d4\u10d3\u10d8\u10d6\u10d4\u10d3 \u10d2\u10d0\u10e3\u10db\u10ef\u10dd\u10d1\u10d4\u10e1\u10d4\u10d1\u10d0</div>
  </div>
</div>

<!-- ════════ 2. DATA STORY ════════ -->
<div class="card narrative-card">
  <div class="card-label">\u10db\u10dd\u10dc\u10d0\u10ea\u10d4\u10db\u10d7\u10d0 \u10d8\u10e1\u10e2\u10dd\u10e0\u10d8\u10d0</div>
  <div class="narrative-text">{narrative_html}</div>
</div>

<!-- ════════ 3. PROGRESS TIMELINE ════════ -->
<div class="card tl-section">
  <h2 class="sec">\u10de\u10e0\u10dd\u10d2\u10e0\u10d4\u10e1\u10d8\u10e1 \u10d3\u10e0\u10dd\u10e8\u10d0</h2>
  <div>
    <div class="tl-group-label">\u10ef\u10d2\u10e3\u10e4\u10d8 #1 &mdash; {g1['lecture_count']}/{total}</div>
    {_timeline_dots(g1, 1)}
  </div>
  <div>
    <div class="tl-group-label">\u10ef\u10d2\u10e3\u10e4\u10d8 #2 &mdash; {g2['lecture_count']}/{total}</div>
    {_timeline_dots(g2, 2)}
  </div>
</div>

<!-- ════════ 4. TRAINER SUB-SCORES (MINI GAUGES) ════════ -->
<div class="card">
  <h2 class="sec">\u10e2\u10e0\u10d4\u10dc\u10d4\u10e0\u10d8\u10e1 \u10d9\u10dd\u10db\u10de\u10d4\u10e2\u10d4\u10dc\u10ea\u10d8\u10d8\u10e1 \u10de\u10e0\u10dd\u10e4\u10d8\u10da\u10d8</h2>
  <div class="sub-scores">
    {_gauge_pedagogy}
    {_gauge_content}
    {_gauge_impact}
    {_gauge_balance}
  </div>
</div>

<!-- ════════ 5. LECTURE-BY-LECTURE CARDS ════════ -->
<h2 class="sec">\u10da\u10d4\u10e5\u10ea\u10d8\u10d4\u10d1\u10d8\u10e1 \u10d3\u10d4\u10e2\u10d0\u10da\u10e3\u10e0\u10d8 \u10e5\u10e3\u10da\u10d4\u10d1\u10d8</h2>
<div class="lec-grid">
  {lecture_cards_html if lecture_cards_html else _no_lectures_msg}
</div>

<!-- ════════ 6. DIMENSION RANKING (BULLET CHART) ════════ -->
<div class="card">
  <h2 class="sec">\u10d2\u10d0\u10dc\u10d6\u10dd\u10db\u10d8\u10da\u10d4\u10d1\u10d4\u10d1\u10d8\u10e1 \u10e0\u10d4\u10d8\u10e2\u10d8\u10dc\u10d2\u10d8 vs \u10e1\u10d0\u10db\u10d8\u10d6\u10dc\u10d4 7.0</h2>
  {bullet_html if bullet_html else _no_data_msg}
  <div style="font-size:0.58rem;color:var(--muted);text-align:right;margin-top:0.35rem">| = \u10e1\u10d0\u10db\u10d8\u10d6\u10dc\u10d4 7.0/10</div>
</div>

<!-- ════════ 7. AI INSIGHTS ════════ -->
<h2 class="sec">AI \u10e3\u10d9\u10e3\u10d9\u10d0\u10d5\u10e8\u10d8\u10e0\u10d8\u10e1 \u10d0\u10dc\u10d0\u10da\u10d8\u10d6\u10d8</h2>
{_build_insights_html(data)}

<!-- ════════ 8. ACHIEVEMENT BADGES ════════ -->
<div class="card">
  <h2 class="sec">\u10db\u10d8\u10e6\u10ec\u10d4\u10d5\u10d4\u10d1\u10d8</h2>
  <div class="badges-grid">
    {achievements_html}
  </div>
</div>

<!-- ════════ NEW: RESEARCH-BACKED INSIGHTS ════════ -->
<h2 class="sec">სიღრმისეული ანალიზი (კვლევაზე დაფუძნებული)</h2>

<!-- Kirkpatrick 4-Level Assessment -->
<div class="card" style="margin-bottom:1rem">
  <div class="card-label">Kirkpatrick-ის 4-დონიანი შეფასება</div>
  <div class="kirk-grid">
    <div class="kirk-level">
      <div class="kirk-num">L1</div>
      <div class="kirk-name">რეაქცია</div>
      <div class="kirk-score sc-{_sc(_kirk_l1)}">{_fmt(_kirk_l1)}</div>
      <div class="kirk-desc">ჩართულობა</div>
    </div>
    <div class="kirk-level">
      <div class="kirk-num">L2</div>
      <div class="kirk-name">სწავლება</div>
      <div class="kirk-score sc-{_sc(_kirk_l2)}">{_fmt(_kirk_l2)}</div>
      <div class="kirk-desc">სიღრმე + სიზუსტე</div>
    </div>
    <div class="kirk-level">
      <div class="kirk-num">L3</div>
      <div class="kirk-name">ქცევა</div>
      <div class="kirk-score sc-{_sc(_kirk_l3)}">{_fmt(_kirk_l3)}</div>
      <div class="kirk-desc">პრაქტიკული ღირ.</div>
    </div>
    <div class="kirk-level">
      <div class="kirk-num">L4</div>
      <div class="kirk-name">შედეგი</div>
      <div class="kirk-score sc-{_sc(_kirk_l4)}">{_fmt(_kirk_l4)}</div>
      <div class="kirk-desc">ბაზრის რელევანტ.</div>
    </div>
  </div>
</div>

<!-- Research Metrics Grid -->
<div class="metrics-grid">
  <div class="metric-card">
    <div class="metric-icon">🎯</div>
    <div class="metric-value sc-{_sc(7.0 + (_bench_gap))}">{'+' if (_bench_gap or 0) > 0 else ''}{_fmt(_bench_gap)}</div>
    <div class="metric-name">ინდუსტრიის ბენჩმარკი</div>
    <div class="metric-detail">vs ATD/SHRM სტანდარტი (7.0)</div>
  </div>
  <div class="metric-card">
    <div class="metric-icon">📐</div>
    <div class="metric-value">{_fmt(_tp_ratio)}</div>
    <div class="metric-name">თეორია/პრაქტიკა</div>
    <div class="metric-detail">იდეალური: 0.43 (30/70)</div>
  </div>
  <div class="metric-card">
    <div class="metric-icon">🚀</div>
    <div class="metric-value">{_fmt(_velocity)}</div>
    <div class="metric-name">გაუმჯობ. სიჩქარე</div>
    <div class="metric-detail">{_vel_label} / ლექციაზე</div>
  </div>
  <div class="metric-card">
    <div class="metric-icon">📊</div>
    <div class="metric-value">{_ltt or '—'}</div>
    <div class="metric-name">ლექცია სამიზნემდე</div>
    <div class="metric-detail">{'დარჩა ' + str(_ltt) + ' ლექცია 7.0-მდე' if _ltt else 'ვერ ითვლება'}</div>
  </div>
  <div class="metric-card">
    <div class="metric-icon">✅</div>
    <div class="metric-value sc-good">{_rec_ft}/5</div>
    <div class="metric-name">რეკომენდაციის შესრულება</div>
    <div class="metric-detail">განზომილება გაუმჯობესდა ბოლო ლექციაში</div>
  </div>
  <div class="metric-card">
    <div class="metric-icon">⚠️</div>
    <div class="metric-value sc-{'bad' if _at_risk else 'good'}">{len(_at_risk)}</div>
    <div class="metric-name">რისკის ზონა</div>
    <div class="metric-detail">{_at_risk_names}</div>
  </div>
</div>

<!-- ════════ 8b. LECTURE COMPARISON TABLE ════════ -->
<div class="card">
  <h2 class="sec">\u10da\u10d4\u10e5\u10ea\u10d8\u10d4\u10d1\u10d8\u10e1 \u10e8\u10d4\u10d3\u10d0\u10e0\u10d4\u10d1\u10d8\u10e1 \u10ea\u10ee\u10e0\u10d8\u10da\u10d8</h2>
  <div class="cmp-table-wrap">
    <table class="cmp-table">
      <thead>{cmp_table_header}</thead>
      <tbody>{cmp_table_rows if cmp_table_rows else '<tr><td colspan="7" class="empty-state">\u10ef\u10d4\u10e0 \u10d0\u10e0 \u10d0\u10e0\u10d8\u10e1 \u10db\u10dd\u10dc\u10d0\u10ea\u10d4\u10db\u10d4\u10d1\u10d8</td></tr>'}</tbody>
    </table>
  </div>
</div>

<!-- ════════ 8c. BAR CHART: DIMENSION COMPARISON ════════ -->
<div class="card">
  <h2 class="sec">\u10d2\u10d0\u10dc\u10d6\u10dd\u10db\u10d8\u10da\u10d4\u10d1\u10d4\u10d1\u10d8\u10e1 \u10e8\u10d4\u10d3\u10d0\u10e0\u10d4\u10d1\u10d0 (\u10ef\u10d2\u10e3\u10e4\u10d4\u10d1\u10d8)</h2>
  <div class="card-label">\u10e1\u10d0\u10e8\u10e3\u10d0\u10da\u10dd \u10e5\u10e3\u10da\u10d4\u10d1\u10d8 \u10d2\u10d0\u10dc\u10d6\u10dd\u10db\u10d8\u10da\u10d4\u10d1\u10d4\u10d1\u10d8\u10e1 \u10db\u10d8\u10ee\u10d4\u10d3\u10d5\u10d8\u10d7</div>
  <div class="chart-wrap"><canvas id="barDimensions" role="img" aria-label="\u10d2\u10d0\u10dc\u10d6\u10dd\u10db\u10d8\u10da\u10d4\u10d1\u10d4\u10d1\u10d8\u10e1 \u10e8\u10d4\u10d3\u10d0\u10e0\u10d4\u10d1\u10d0"></canvas></div>
</div>

<!-- ════════ 9. CROSS-GROUP COMPARISON ════════ -->
<h2 class="sec">\u10ef\u10d2\u10e3\u10e4\u10d4\u10d1\u10d8\u10e1 \u10e8\u10d4\u10d3\u10d0\u10e0\u10d4\u10d1\u10d0</h2>
<div class="cross-grid">
  <div class="card">
    <div class="card-label">\u10d9\u10dd\u10db\u10de\u10d4\u10e2\u10d4\u10dc\u10ea\u10d8\u10d8\u10e1 \u10e0\u10d0\u10d3\u10d0\u10e0\u10d8</div>
    <div class="chart-wrap"><canvas id="radar" role="img" aria-label="\u10d9\u10dd\u10db\u10de\u10d4\u10e2\u10d4\u10dc\u10ea\u10d8\u10d8\u10e1 \u10e0\u10d0\u10d3\u10d0\u10e0\u10d8"></canvas></div>
  </div>
  <div class="card">
    <div class="card-label">\u10d3\u10d4\u10da\u10e2\u10d0 \u10ef\u10d2\u10e3\u10e4\u10d4\u10d1\u10e8 (\u10ef\u10d2#2 - \u10ef\u10d2#1)</div>
    <div class="cross-summary">
      {cross_summary_html if cross_summary_html else _no_data_msg}
    </div>
  </div>
</div>

<!-- ════════ 10. TREND CHART (COMBINED) ════════ -->
<div class="card">
  <h2 class="sec">\u10d2\u10d0\u10dc\u10d5\u10d8\u10d7\u10d0\u10e0\u10d4\u10d1\u10d8\u10e1 \u10e2\u10d4\u10dc\u10d3\u10d4\u10dc\u10ea\u10d8\u10d0</h2>
  <div class="card-label">\u10d9\u10dd\u10db\u10de\u10dd\u10d6\u10d8\u10e2\u10e3\u10e0\u10d8 \u10e5\u10e3\u10da\u10d0 \u2014 \u10dd\u10e0\u10d8\u10d5\u10d4 \u10ef\u10d2\u10e3\u10e4\u10d8</div>
  <div class="chart-wrap"><canvas id="trendCombined" role="img" aria-label="\u10d2\u10d0\u10dc\u10d5\u10d8\u10d7\u10d0\u10e0\u10d4\u10d1\u10d8\u10e1 \u10e2\u10d4\u10dc\u10d3\u10d4\u10dc\u10ea\u10d8\u10d0"></canvas></div>
</div>

<!-- ════════ FOOTER ════════ -->
<div class="dash-footer">
  <span>Training Agent Analytics v4.0</span>
  <span>Auto-refresh: 5 \u10ec\u10d7 &middot; {generated_at}</span>
</div>

</div><!-- .wrap -->

<script>
const DATA = {json_data};
const DIMS = ["content_depth","practical_value","engagement","technical_accuracy","market_relevance"];
const DIM_LABELS = ["\u10e8\u10d8\u10dc\u10d0\u10d0\u10e0\u10e1\u10d8\u10e1 \u10e1\u10d8\u10e6\u10e0\u10db\u10d4","\u10de\u10e0\u10d0\u10e5\u10e2\u10d8\u10d9\u10e3\u10da\u10d8 \u10e6\u10d8\u10e0\u10d4\u10d1\u10e3\u10da\u10d4\u10d1\u10d0","\u10e9\u10d0\u10e0\u10d7\u10e3\u10da\u10dd\u10d1\u10d0","\u10e2\u10d4\u10e5\u10dc\u10d8\u10d9\u10e3\u10e0\u10d8 \u10e1\u10d8\u10d6\u10e3\u10e1\u10e2\u10d4","\u10d1\u10d0\u10d6\u10e0\u10d8\u10e1 \u10e0\u10d4\u10da\u10d4\u10d5\u10d0\u10dc\u10e2\u10e3\u10e0\u10dd\u10d1\u10d0"];
const COLORS = {json.dumps(_CHART_COLORS)};
const FILLS  = {json.dumps(_CHART_COLORS_FILL)};

document.addEventListener("DOMContentLoaded", function() {{
  if (typeof Chart === "undefined") return;
  Chart.defaults.font.family = "'Noto Sans Georgian', 'Inter', system-ui, sans-serif";
  Chart.defaults.color = "#94a3b8";

  var GC = "rgba(255,255,255,0.04)";

  /* ── Radar Chart ── */
  (function() {{
    var ds = [];
    var rc = ["rgba(99,102,241,0.85)", "rgba(34,211,238,0.85)"];
    var rf = ["rgba(99,102,241,0.12)", "rgba(34,211,238,0.08)"];
    [1,2].forEach(function(gn, i) {{
      var g = DATA.groups[gn];
      if (!g.lecture_count) return;
      var last = g.scores[g.scores.length - 1];
      ds.push({{
        label: "\u10ef\u10d2 #" + gn + " (\u10da#" + last.lecture_number + ")",
        data: DIMS.map(function(d) {{ return last[d]; }}),
        borderColor: rc[i], backgroundColor: rf[i], pointBackgroundColor: rc[i], borderWidth: 2.5
      }});
    }});
    if (!ds.length) return;
    new Chart(document.getElementById("radar"), {{
      type: "radar",
      data: {{ labels: DIM_LABELS, datasets: ds }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        scales: {{ r: {{
          min: 0, max: 10,
          ticks: {{ stepSize: 2, color: "#64748b", backdropColor: "transparent", font: {{ size: 9 }} }},
          grid: {{ color: "rgba(255,255,255,0.06)" }},
          pointLabels: {{ color: "#f1f5f9", font: {{ size: 11, weight: "500" }} }},
          angleLines: {{ color: "rgba(255,255,255,0.04)" }}
        }} }},
        plugins: {{
          legend: {{ position: "bottom", labels: {{ color: "#94a3b8", usePointStyle: true, padding: 16 }} }},
          tooltip: {{
            backgroundColor: "rgba(15,23,42,0.95)", titleColor: "#f1f5f9", bodyColor: "#cbd5e1",
            borderColor: "rgba(99,102,241,0.2)", borderWidth: 1, padding: 12, cornerRadius: 8,
          }}
        }}
      }}
    }});
  }})();

  /* ── Bar Chart: Dimension Comparison ── */
  (function() {{
    var g1 = DATA.groups[1];
    var g2 = DATA.groups[2];
    if (!g1.lecture_count && !g2.lecture_count) return;
    var dimLabels = ["\u10e8\u10d8\u10dc\u10d0\u10d0\u10e0\u10e1\u10d8\u10e1 \u10e1\u10d8\u10e6\u10e0\u10db\u10d4","\u10de\u10e0\u10d0\u10e5\u10e2\u10d8\u10d9\u10e3\u10da\u10d8 \u10e6\u10d8\u10e0\u10d4\u10d1\u10e3\u10da\u10d4\u10d1\u10d0","\u10e9\u10d0\u10e0\u10d7\u10e3\u10da\u10dd\u10d1\u10d0","\u10e2\u10d4\u10e5\u10dc\u10d8\u10d9\u10e3\u10e0\u10d8 \u10e1\u10d8\u10d6\u10e3\u10e1\u10e2\u10d4","\u10d1\u10d0\u10d6\u10e0\u10d8\u10e1 \u10e0\u10d4\u10da\u10d4\u10d5\u10d0\u10dc\u10e2\u10e3\u10e0\u10dd\u10d1\u10d0"];
    var ds = [];
    function avgDims(g) {{
      return DIMS.map(function(d) {{
        var vals = g.scores.map(function(s) {{ return s[d]; }}).filter(function(v) {{ return v != null; }});
        return vals.length ? vals.reduce(function(a,b) {{ return a+b; }},0) / vals.length : 0;
      }});
    }}
    if (g1.lecture_count) {{
      ds.push({{
        label: "\u10ef\u10d2\u10e3\u10e4\u10d8 #1",
        data: avgDims(g1),
        backgroundColor: "rgba(99,102,241,0.7)",
        borderColor: "rgba(99,102,241,1)",
        borderWidth: 1.5, borderRadius: 6
      }});
    }}
    if (g2.lecture_count) {{
      ds.push({{
        label: "\u10ef\u10d2\u10e3\u10e4\u10d8 #2",
        data: avgDims(g2),
        backgroundColor: "rgba(34,211,238,0.7)",
        borderColor: "rgba(34,211,238,1)",
        borderWidth: 1.5, borderRadius: 6
      }});
    }}
    new Chart(document.getElementById("barDimensions"), {{
      type: "bar",
      data: {{ labels: dimLabels, datasets: ds }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        interaction: {{ mode: "index", intersect: false }},
        plugins: {{
          legend: {{ position: "bottom", labels: {{ color: "#94a3b8", font: {{ size: 11 }}, usePointStyle: true, padding: 16 }} }},
          tooltip: {{
            backgroundColor: "rgba(15,23,42,0.95)", titleColor: "#f1f5f9", bodyColor: "#cbd5e1",
            borderColor: "rgba(99,102,241,0.2)", borderWidth: 1, padding: 12, cornerRadius: 8,
            callbacks: {{ label: function(ctx) {{ return " " + ctx.dataset.label + ": " + ctx.parsed.y.toFixed(1) + "/10"; }} }}
          }}
        }},
        scales: {{
          x: {{ ticks: {{ color: "#94a3b8", font: {{ size: 10 }}, maxRotation: 0 }}, grid: {{ display: false }} }},
          y: {{ min: 0, max: 10, ticks: {{ color: "#64748b", stepSize: 2 }}, grid: {{ color: GC }} }}
        }}
      }}
    }});
  }})();

  /* ── Combined Trend Chart ── */
  (function() {{
    var g1 = DATA.groups[1];
    var g2 = DATA.groups[2];
    var maxLen = Math.max(g1.lecture_count || 0, g2.lecture_count || 0);
    if (!maxLen) return;
    var labels = [];
    for (var i = 1; i <= maxLen; i++) labels.push("\u10da#" + i);

    var datasets = [];
    if (g1.lecture_count) {{
      datasets.push({{
        label: "\u10ef\u10d2\u10e3\u10e4\u10d8 #1",
        data: g1.composite_series,
        borderColor: "rgba(99,102,241,1)",
        backgroundColor: "rgba(99,102,241,0.08)",
        tension: 0.35, pointRadius: 5, pointHoverRadius: 8, borderWidth: 2.5, fill: true
      }});
    }}
    if (g2.lecture_count) {{
      datasets.push({{
        label: "\u10ef\u10d2\u10e3\u10e4\u10d8 #2",
        data: g2.composite_series,
        borderColor: "rgba(34,211,238,1)",
        backgroundColor: "rgba(34,211,238,0.06)",
        tension: 0.35, pointRadius: 5, pointHoverRadius: 8, borderWidth: 2.5, fill: true
      }});
    }}
    /* Target line at 7.0 */
    datasets.push({{
      label: "\u10e1\u10d0\u10db\u10d8\u10d6\u10dc\u10d4 7.0",
      data: Array(maxLen).fill(7.0),
      borderColor: "rgba(248,113,113,0.4)",
      borderWidth: 1.5, borderDash: [6, 4],
      pointRadius: 0, fill: false
    }});

    new Chart(document.getElementById("trendCombined"), {{
      type: "line",
      data: {{ labels: labels, datasets: datasets }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        interaction: {{ mode: "index", intersect: false }},
        plugins: {{
          legend: {{ position: "bottom", labels: {{ color: "#94a3b8", font: {{ size: 11 }}, usePointStyle: true, padding: 16 }} }},
          tooltip: {{
            backgroundColor: "rgba(15,23,42,0.95)", titleColor: "#f1f5f9", bodyColor: "#cbd5e1",
            borderColor: "rgba(99,102,241,0.2)", borderWidth: 1, padding: 12, cornerRadius: 8,
            callbacks: {{ label: function(ctx) {{ return " " + ctx.dataset.label + ": " + (ctx.parsed.y != null ? ctx.parsed.y.toFixed(1) : "-") + "/10"; }} }}
          }}
        }},
        scales: {{
          x: {{ ticks: {{ color: "#64748b", font: {{ size: 10 }} }}, grid: {{ color: GC }} }},
          y: {{ min: 0, max: 10, ticks: {{ color: "#64748b", stepSize: 2 }}, grid: {{ color: GC }} }}
        }}
      }}
    }});
  }})();
}});

/* ── Expand/Collapse for insights text ── */
document.querySelectorAll('.ins-quote.expandable').forEach(function(el) {{
  el.classList.add('collapsed');
  el.addEventListener('click', function() {{
    this.classList.toggle('collapsed');
  }});
}});

/* Auto-refresh every 5 minutes */
setTimeout(function() {{ location.reload(); }}, 5 * 60 * 1000);
</script>
</body>
</html>"""

    return html
