# Analytics System

Automated lecture quality scoring, statistical analysis, and dashboard generation for the Training Agent.

**Source file:** `tools/services/analytics.py`

---

## Overview

The analytics system extracts numerical scores from Georgian-language deep analysis reports (produced by the Gemini + Claude pipeline), stores them in a local SQLite database, computes descriptive statistics and trend metrics, and renders an interactive HTML dashboard using Chart.js.

### Integration Point

Called from `tools/services/transcribe_lecture.py` as **step 1.5** in the recording processing pipeline:

```python
from tools.services.analytics import save_scores_from_analysis

save_scores_from_analysis(group_number, lecture_number, deep_analysis_text)
```

This function is the primary entry point. It extracts scores, persists them, and also extracts qualitative insights — all in one call. Returns `True` on success, `False` if extraction failed (non-fatal to the caller).

---

## 5-Dimensional Scoring System

Each lecture is evaluated on five dimensions, scored 1-10:

| Dimension Key | English Label | Georgian Label |
|---|---|---|
| `content_depth` | Content Depth | შინაარსის სიღრმე |
| `practical_value` | Practical Value | პრაქტიკული ღირებულება |
| `engagement` | Engagement | მონაწილეების ჩართულობა |
| `technical_accuracy` | Technical Accuracy | ტექნიკური სიზუსტე |
| `market_relevance` | Market Relevance | ბაზრის რელევანტურობა |

An optional sixth value, `overall_score` (საერთო შეფასება), may also be present in the analysis text.

### Composite Score

The **composite score** is the simple average of the five dimensions:

```
composite = (content_depth + practical_value + engagement + technical_accuracy + market_relevance) / 5
```

Rounded to 2 decimal places.

### Label Mappings

Two dictionaries provide label lookups:

- `DIMENSION_LABELS_KA` — Georgian labels (includes `"composite": "კომპოზიტური ქულა"`)
- `DIMENSION_LABELS_EN` — English labels (includes `"composite": "Composite"`)

---

## Score Extraction (Regex)

Scores are extracted from Georgian deep analysis text via regex. The analysis contains a markdown table in this format:

```
| **შინაარსის სიღრმე** | 4/10 | justification text here |
| **პრაქტიკული ღირებულება** | 7/10 | justification text here |
...
```

### Pattern Matching

Each dimension has a Georgian regex pattern defined in `_DIMENSION_PATTERNS`:

| DB Column | Georgian Pattern |
|---|---|
| `content_depth` | `შინაარსის\s+სიღრმე` |
| `practical_value` | `პრაქტიკული\s+ღირებულება` |
| `engagement` | `მონაწილეე?ბ[ი]?\s*[სთ]?\s*ჩართულობა` |
| `technical_accuracy` | `ტექნიკური\s+სიზუსტე` |
| `market_relevance` | `ბაზრის\s+(?:რელევანტ\w+\|შესაბამისობა)` |
| `overall_score` | `საერთო\s+შეფასება` |

The row template pattern is:

```regex
\|[^\|]*{label}[^\|]*\|\s*\**(\d+(?:\.\d+)?)/10\**\s*\|
```

This handles both plain `N/10` and bold `**N/10**` formatting.

### Extraction Logic (`extract_scores`)

1. For each dimension pattern, search the full text for a matching table row.
2. Capture the numeric value before `/10`.
3. If all 5 required dimensions are found, return the scores dict.
4. If any required dimension is missing, log an error and return `None`.

The `overall_score` (6th pattern) is optional — its absence does not cause failure.

### Raw Score Table Capture (`_capture_score_table`)

For audit purposes, the raw score table substring is also captured and stored:
- Primary: regex matches the table header containing "ქულ" followed by 4-8 rows.
- Fallback: collects all lines containing `/10`.
- Truncated to 2000 characters maximum.

---

## SQLite Database

**Path:** `data/scores.db` (relative to project root, defined as `PROJECT_ROOT / "data" / "scores.db"`)

Uses WAL journal mode for concurrent reads. Connection managed via a context manager with auto-commit/rollback.

### Table: `lecture_scores`

```sql
CREATE TABLE IF NOT EXISTS lecture_scores (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    group_number       INTEGER NOT NULL CHECK (group_number IN (1, 2)),
    lecture_number     INTEGER NOT NULL CHECK (lecture_number BETWEEN 1 AND 15),
    content_depth      REAL    NOT NULL,
    practical_value    REAL    NOT NULL,
    engagement         REAL    NOT NULL,
    technical_accuracy REAL    NOT NULL,
    market_relevance   REAL    NOT NULL,
    overall_score      REAL,              -- optional (may be NULL)
    composite          REAL    NOT NULL,   -- average of 5 dimensions
    raw_score_text     TEXT,               -- raw markdown table for audit
    processed_at       TEXT    NOT NULL,    -- ISO 8601 UTC timestamp
    UNIQUE (group_number, lecture_number)
);

CREATE INDEX IF NOT EXISTS idx_group_lecture
    ON lecture_scores (group_number, lecture_number);
```

**Schema diagram:**

```
lecture_scores
├── id (PK, autoincrement)
├── group_number (1 or 2)
├── lecture_number (1-15)
├── content_depth (REAL)
├── practical_value (REAL)
├── engagement (REAL)
├── technical_accuracy (REAL)
├── market_relevance (REAL)
├── overall_score (REAL, nullable)
├── composite (REAL, calculated)
├── raw_score_text (TEXT, nullable)
└── processed_at (TEXT, ISO 8601)

UNIQUE constraint: (group_number, lecture_number)
```

### Table: `lecture_insights`

Stores qualitative analysis metadata extracted from the deep analysis text.

```sql
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
    top_strength            TEXT,             -- first bullet from strengths section
    top_weakness            TEXT,             -- first bullet from weaknesses section
    key_recommendation      TEXT,             -- first recommendation item
    score_justifications    TEXT,             -- JSON: {dimension: justification_text}
    extracted_at            TEXT NOT NULL,     -- ISO 8601 UTC timestamp
    UNIQUE (group_number, lecture_number)
);
```

**Schema diagram:**

```
lecture_insights
├── id (PK, autoincrement)
├── group_number
├── lecture_number
├── strengths_count (INT)
├── weaknesses_count (INT)
├── gaps_count (INT)
├── recommendations_count (INT)
├── tech_correct_count (INT, checkmarks in analysis)
├── tech_problematic_count (INT, warning items)
├── blind_spots_count (INT)
├── top_strength (TEXT, first item)
├── top_weakness (TEXT, first item)
├── key_recommendation (TEXT, first item)
├── score_justifications (JSON TEXT)
└── extracted_at (TEXT, ISO 8601)

UNIQUE constraint: (group_number, lecture_number)
```

### Write Operations

- **`upsert_scores()`** — `INSERT OR REPLACE` into `lecture_scores`. Calculates composite automatically.
- **`extract_and_save_insights()`** — Parses Georgian section headers (ძლიერი მხარეები, სუსტი მხარეები, ხარვეზები, რეკომენდაციები, etc.), counts bullet items, extracts top items, and `INSERT OR REPLACE` into `lecture_insights`.
- **`save_scores_from_analysis()`** — Orchestrates both: calls `extract_scores()`, then `upsert_scores()`, then `extract_and_save_insights()`.

### Query Operations

- `get_scores_for_lecture(group, lecture)` — single row or `None`
- `get_group_scores(group)` — all scores for a group, ordered by lecture number
- `get_all_scores(group=None)` — all scores, optionally filtered by group
- `get_lecture_insights(group, lecture)` — single insights row or `None`
- `get_all_insights()` — all insights, ordered by group and lecture

---

## Insight Extraction

The `extract_insights()` function parses Georgian section headers in the deep analysis text to count items and extract representative quotes.

### Sections Parsed

| Insight | Georgian Section Headers Searched |
|---|---|
| Strengths | `ძლიერი მხარეები` |
| Weaknesses | `სუსტი მხარეები`, `განვითარება`, `გასაუმჯობესებელი`, `სისუსტ` |
| Content gaps | `კრიტიკული ხარვეზ`, `ხარვეზ` |
| Blind spots | `ბრმა წერტილ` |
| Recommendations | `რეკომენდაცი`, `გაუმჯობესებ`, `სარეკომენდაციო` |
| Tech correct | Lines starting with `✅` or items under `სწორია` section |
| Tech problematic | Lines starting with `⚠️` or items under `პრობლემური` section |

### Counting Strategy

Items are counted using multiple fallback patterns:
1. Bullet items (`* item` or `- item`)
2. Numbered items (`1. **item**`)
3. Lettered items (`a)`, `b)`)
4. Last resort for strengths/weaknesses: count score dimensions >= 6/10 or <= 5/10 in the score table

### Section Detection (`_get_section`)

Handles multiple Georgian header formats:
- Markdown headers: `## ძლიერი მხარეები`, `### 10. კრიტიკული ბრმა წერტილები`
- Bold headers: `**ძლიერი მხარეები:**`
- Numbered parts: `ნაწილი II — ძლიერი მხარეები`

### Score Justifications

Per-dimension justifications are extracted from the third column of the score table and stored as JSON in `score_justifications`:

```json
{
  "content_depth": "justification text...",
  "practical_value": "justification text...",
  ...
}
```

---

## Statistical Calculations

All statistics are computed using Python's **stdlib only** (`math` module) — no numpy or scipy dependencies.

### `calculate_statistics(scores: list[float])` returns:

| Metric | Calculation |
|---|---|
| `mean` | Simple arithmetic mean |
| `median` | Middle value (or average of two middle values) |
| `std_dev` | Sample standard deviation (`n-1` denominator) |
| `min`, `max` | Minimum and maximum values |
| `p25`, `p75` | 25th and 75th percentiles (ceiling index method) |
| `trend_slope` | Linear regression slope (least squares, x=1..n) |
| `rolling_avg_3` | Mean of last 3 lectures |
| `improvement_rate` | Mean of consecutive deltas |
| `trend_label` | Georgian label with emoji: `📈 იზრდება` / `📉 მცირდება` / `➡️ სტაბილური` (threshold: slope > 0.05 / < -0.05) |

Returns a dict of `None` values if the input list is empty.

### Derived Metrics (per group, in `_build_group_data`)

| Metric | Formula |
|---|---|
| `pedagogy_score` | (engagement + content_depth) / 2 |
| `content_quality` | (technical_accuracy + content_depth + market_relevance) / 3 |
| `impact_score` | (practical_value + market_relevance) / 2 |
| `balance_score` | 10 - (max_dim_avg - min_dim_avg) |
| `target_gap` | 7.0 - composite_avg |
| `consistency` | 10 - mean(std_devs across dimensions) |

### Research-Backed Metrics

| Metric | Description |
|---|---|
| **Kirkpatrick Levels** | L1=Reaction(engagement), L2=Learning(depth+accuracy avg), L3=Behavior(practical_value), L4=Results(market_relevance) |
| **Velocity** | Composite trend slope; labeled "აჩქარებს" (>0.3) / "ანელებს" (<-0.3) / "სტაბილური" |
| **Volatility** | Per-dimension: average absolute delta between consecutive lectures |
| **Recommendation Follow-through** | Count of dimensions that improved between last two lectures |
| **Theory/Practice Ratio** | (depth+accuracy)/2 divided by (practical+engagement+market)/3; ideal ~0.43 |
| **Benchmark** | Gap from industry benchmark (7.0/10); percentile = composite/10 * 100 |
| **At-Risk Dimensions** | Dimensions that dropped >= 1.0 between last two lectures |
| **Lectures to Target** | Estimated lectures to reach 7.0 composite based on current velocity |

### Cross-Group Metrics (in `get_dashboard_data`)

The dashboard aggregates metrics across both groups:
- `trainer_performance_index` (TPI) — weighted mean of all composites
- `dimension_rankings` — all dimensions ranked by mean across both groups
- Cross-group versions of: pedagogy, content quality, impact, balance, Kirkpatrick, velocity, theory/practice ratio, benchmark gap

---

## Backfill Mechanism

### `backfill_from_tmp()`

Scans the `.tmp/` directory for analysis text files matching the pattern:

```
g{group}_l{lecture}_deep_analysis.txt
```

For each file found:
1. Check if the lecture already has scores in the DB (skip if yes — preserves manual corrections).
2. Read the file (UTF-8).
3. Skip if text is shorter than 100 characters.
4. Call `save_scores_from_analysis()` to extract and persist scores + insights.

Returns: `{"processed": N, "failed": M, "skipped": K}`

### `sync_from_pinecone()`

Reconstructs scores from the persistent Pinecone vector database (the source of truth on Railway where SQLite is ephemeral):

1. Lists all vector IDs with prefix `g{N}_l{N}_deep_analysis_` in Pinecone.
2. Fetches vector chunks and reconstructs the full text by sorting on `chunk_index` metadata.
3. Calls `save_scores_from_analysis()` on the reconstructed text.
4. Includes a **5-minute cooldown** between syncs (bypass with `force=True`).
5. Seeds approximate G1L1 scores (corrupted video — derived from equivalent G2L1 delivery).

Returns: `{"synced": N, "skipped": M, "failed": K}`

### Railway Deployment Note

The SQLite database at `data/scores.db` is **ephemeral on Railway** — it is lost on every redeploy or restart. The backfill mechanisms restore scores:

1. `backfill_from_tmp()` — from local `.tmp/` analysis files (if present)
2. `sync_from_pinecone()` — from persistent Pinecone vectors (primary recovery path on Railway)

Both are designed to be called at application startup.

---

## Dashboard

### `render_dashboard_html(data: dict) -> str`

Generates a complete, self-contained HTML page with:

- **Chart.js** loaded from CDN (`https://cdn.jsdelivr.net/npm/chart.js`) — no local assets needed
- Composite trend line charts per group
- Per-dimension line charts
- Heatmap grid (lecture x dimension)
- Radar charts for dimension comparison
- Competency gauges (pedagogy, content quality, impact, balance)
- Kirkpatrick level visualization
- AI insights digest cards (strengths/weaknesses/gaps per lecture)
- Performance narrative (Georgian-language data story)
- Cross-group comparison tables

### `generate_performance_narrative(dashboard_data) -> str`

Produces a 3-4 sentence Georgian-language "Data Story" covering:
- Composite trend (improving/declining/stable) with percentage change
- Biggest dimension improvement and decline
- Strongest and weakest dimensions overall
- Milestone hints (e.g., "7.0 is only 0.3 points away")

### Color Scheme

Five chart colors map to the five dimensions:

| Dimension | Color |
|---|---|
| content_depth | Indigo `rgba(99, 102, 241, 1)` |
| practical_value | Cyan `rgba(34, 211, 238, 1)` |
| engagement | Emerald `rgba(52, 211, 153, 1)` |
| technical_accuracy | Amber `rgba(251, 191, 36, 1)` |
| market_relevance | Red `rgba(248, 113, 113, 1)` |

Fill variants use 0.15 alpha for area charts.

### Score Classification (CSS classes)

| Score Range | Class | Visual |
|---|---|---|
| >= 7.0 | `good` | Green |
| >= 5.0 | `mid` | Yellow/amber |
| < 5.0 | `bad` | Red |
| null | `na` | Dash (—) |

---

## Data Flow

```
Zoom Recording
    ↓
transcribe_lecture.py (main pipeline)
    ↓
Gemini + Claude analysis → deep_analysis_text (Georgian)
    ↓
save_scores_from_analysis(group, lecture, text)    ← step 1.5
    ├── extract_scores(text)         → 5 dimension scores
    ├── upsert_scores(...)           → lecture_scores table
    ├── extract_insights(text)       → qualitative counts
    └── extract_and_save_insights()  → lecture_insights table
    ↓
data/scores.db (SQLite)
    ↓
get_dashboard_data()  → aggregated stats, cross-group metrics
    ↓
render_dashboard_html(data)  → self-contained HTML page
```

---

## API Surface (Key Functions)

| Function | Purpose |
|---|---|
| `init_db()` | Create `data/` directory and tables if absent |
| `extract_scores(text)` | Regex extraction of 5 dimension scores from Georgian text |
| `save_scores_from_analysis(group, lecture, text)` | Main entry point: extract + persist scores + insights |
| `upsert_scores(...)` | Direct score insert/replace |
| `extract_insights(text)` | Parse qualitative sections from analysis |
| `extract_and_save_insights(group, lecture, text)` | Persist qualitative insights |
| `get_scores_for_lecture(group, lecture)` | Query single lecture scores |
| `get_group_scores(group)` | Query all scores for a group |
| `get_all_scores(group=None)` | Query all scores |
| `get_lecture_insights(group, lecture)` | Query single lecture insights |
| `get_all_insights()` | Query all insights |
| `calculate_statistics(scores)` | Descriptive stats + trend for a score series |
| `get_dashboard_data()` | Assemble full dashboard payload |
| `render_dashboard_html(data)` | Generate Chart.js HTML dashboard |
| `generate_performance_narrative(data)` | Georgian-language data story |
| `backfill_from_tmp()` | Restore scores from `.tmp/` text files |
| `sync_from_pinecone()` | Restore scores from Pinecone vectors |
