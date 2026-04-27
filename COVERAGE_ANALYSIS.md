# Test Coverage Gap Analysis — Training Agent

**Date**: March 18, 2026
**Current Test Suite Status**: 320 passing tests, 2 failing (unrelated to coverage gaps)

---

## Executive Summary

| Module | Coverage | Status | Priority |
|--------|----------|--------|----------|
| **analytics.py** | 14% | 🔴 **CRITICAL** | P0 |
| **server.py** | 70% | 🟡 **HIGH** | P1 |
| **knowledge_indexer.py** | 87% | 🟢 OK | P2 |
| **transcribe_lecture.py** | 82% | 🟢 OK | P2 |
| **zoom_manager.py** | 83% | 🟢 OK | P2 |

---

## 1. analytics.py (14% coverage) — **CRITICAL PRIORITY**

**File**: `/Users/tornikebolokadze/Desktop/Training Agent/tools/services/analytics.py`
**Current Status**: NO TEST FILE EXISTS — 0 tests, 571/662 lines untested

### Missing Coverage by Category

#### A. **Core Score Extraction** (PUBLIC API — HIGH PRIORITY)

| Function | Lines | What It Does | Risk Level | Test Approach |
|----------|-------|-----------|-----------|---|
| `extract_scores()` | 77–109 | Parse Georgian score table from deep analysis MD → dict | HIGH | Regex pattern matching, edge cases |
| `extract_insights()` | 299–375 | Parse 5 insight categories (strengths, weaknesses, gaps, etc.) | HIGH | Fixture: multi-section deep_analysis text |
| `extract_and_save_insights()` | 399–439 | Extract + upsert to DB in one call | HIGH | DB mocking + error handling |

**Why it matters**: These functions are the backbone of the analytics pipeline. A broken regex pattern silently loses all lecture scores.

**Test ideas**:
- Fixture: Georgian deep_analysis with complete score table
- Edge cases: missing dimensions, malformed /10 syntax, Unicode variations
- Fixtures for 5 insight categories separately + combined

---

#### B. **Database Operations** (PUBLIC API — HIGH PRIORITY)

| Function | Lines | What It Does | Risk Level | Test Approach |
|----------|-------|-----------|-----------|---|
| `init_db()` | 170–175 | Create tables if missing | MEDIUM | Path mocking, schema validation |
| `upsert_scores()` | 197–230 | INSERT OR REPLACE score row | HIGH | SQLite in-memory DB `:memory:` |
| `save_scores_from_analysis()` | 236–269 | Full pipeline: extract → validate → upsert | HIGH | E2E with mocked `extract_scores()` |
| `get_scores_for_lecture()` | 463–469 | Fetch one row by (group, lecture) | MEDIUM | Query validation |
| `get_group_scores()` | 472–478 | Fetch all rows for a group | MEDIUM | ORDER BY verification |
| `get_all_scores()` | 481–493 | Fetch with optional group filter | MEDIUM | Filter logic |

**Why it matters**: Silent DB failures (missed constraints, wrong query order) break the analytics dashboard.

**Test ideas**:
- In-memory SQLite setup/teardown fixture
- UNIQUE constraint validation (group, lecture)
- Composite score calculation correctness
- Edge case: upsert same lecture twice (should update, not insert)

---

#### C. **Statistical Calculations** (PUBLIC API — MEDIUM PRIORITY)

| Function | Lines | What It Does | Risk Level | Test Approach |
|----------|-------|-----------|-----------|---|
| `calculate_statistics()` | 500–556 | Mean, median, stddev per dimension | MEDIUM | Numerical edge cases |

**Why it matters**: Wrong percentiles mislead instructors on performance trends.

**Test ideas**:
- Perfect scores (all 10s), terrible scores (all 1s)
- Single value list (stddev = 0)
- Outliers: [1, 1, 1, 10, 10, 10]
- Compare against manual calculation

---

#### D. **Dashboard & Reporting** (PUBLIC API — HIGH PRIORITY)

| Function | Lines | What It Does | Risk Level | Test Approach |
|----------|-------|-----------|-----------|---|
| `get_dashboard_data()` | 737–774 | Aggregate all stats for both groups | HIGH | Mock `_build_group_data()` calls |
| `_build_group_data()` | 571–704 | Build single group's stats dict | HIGH | Complex nested dict structure |
| `generate_performance_narrative()` | 965–1076 | Generate Georgian performance summary | MEDIUM | Template validation |
| `render_dashboard_html()` | 1079–1251 | Generate Chart.js HTML page | MEDIUM | HTML structure (minimal parsing) |

**Why it matters**: Dashboard is the UX; broken HTML or wrong data display defeats entire analytics system.

**Test ideas**:
- Mock analytics DB with 3 lectures per group
- Verify nested dict keys (no missing stats)
- Performance narrative: check Georgian text formatting
- HTML: basic sanity checks (contains `<canvas>`, chart IDs)

---

#### E. **Backfill & Sync** (UTILITIES — MEDIUM PRIORITY)

| Function | Lines | What It Does | Risk Level | Test Approach |
|----------|-------|-----------|-----------|---|
| `backfill_from_tmp()` | 791–831 | Scan .tmp/ for deep_analysis files → index | MEDIUM | Fixture: temp files + mocked `save_scores_from_analysis()` |
| `sync_from_pinecone()` | 844–948 | Pull scores from Pinecone metadata → DB | MEDIUM | Mocked Pinecone client |

**Why it matters**: Recovery after Railway restart depends on these; if they fail silently, analytics state is lost.

**Test ideas**:
- Fixture: `.tmp/deep_analysis_*.txt` files
- Mocked `Path.glob()` results
- Test deduplication: same file processed twice should skip second
- Test error resilience: one bad file shouldn't stop others

---

#### F. **Internal Helpers** (PRIVATE — LOW PRIORITY for now)

| Function | Lines | What It Does |
|----------|-------|-----------|
| `_capture_score_table()` | 112–123 | Extract raw score table substring |
| `_count_pattern_items()` | 276–284 | Count occurrences of a section pattern |
| `_extract_first_item()` | 287–296 | Extract first bullet point from section |
| `_get_section()` | 390–396 | Extract text between headers |

**These are already partially tested implicitly via `extract_insights()` tests.**

---

### Recommended Test File Structure

```python
# tools/tests/test_analytics.py

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from tools.services import analytics

# ============================================================================
# FIXTURES — reusable test data
# ============================================================================

@pytest.fixture
def in_memory_db(monkeypatch):
    """Replace DB_PATH with in-memory SQLite for tests."""
    with patch.object(analytics, "DB_PATH", ":memory:"):
        analytics.init_db()
        yield
    # Cleanup

@pytest.fixture
def deep_analysis_complete():
    """Georgian deep analysis with complete score table."""
    return """
## დიპ ანალიზი

| მეტრიკა | ქულა | კომენტარი |
|---------|------|----------|
| **შინაარსის სიღრმე** | **8/10** | დაფარული |
| **პრაქტიკული ღირებულება** | **7/10** | კარგი |
| **მონაწილეების ჩართულობა** | **9/10** | ღია |
| **ტექნიკური სიზუსტე** | **8/10** | კორექტი |
| **ბაზრის რელევანტურობა** | **6/10** | ბაზე |

## Strengths (უძლიერესი ეტაპები)
- Point 1
- Point 2

## Weaknesses (სუსტი მხარეები)
- Issue 1

## Gaps (უფსკელი)
- Gap 1

## Recommendations (რეკომენდაციები)
- Rec 1

## Technical Issues (ტექნიკური პრობლემები)
- Tech 1

## Blind Spots (მიუხედელი ფაქტორები)
- Blind 1
"""

# ============================================================================
# 1. Score Extraction Tests
# ============================================================================

class TestExtractScores:
    def test_complete_score_table_parses_all_five_dimensions(self, deep_analysis_complete):
        result = analytics.extract_scores(deep_analysis_complete)
        assert result is not None
        assert result["content_depth"] == 8.0
        assert result["practical_value"] == 7.0
        assert result["engagement"] == 9.0
        assert result["technical_accuracy"] == 8.0
        assert result["market_relevance"] == 6.0

    def test_returns_none_if_dimension_missing(self):
        incomplete = """
        | შინაარსის სიღრმე | 8/10 |
        | პრაქტიკული ღირებულება | 7/10 |
        """
        result = analytics.extract_scores(incomplete)
        assert result is None

    def test_decimal_scores_parsed(self):
        text = "| შინაარსის სიღრმე | 7.5/10 |"
        # (requires full table structure, mock regex)
        pass

    def test_bold_wrapped_scores(**X/10**)(self):
        # Bold scores: **8/10** should be parsed same as 8/10
        pass

    def test_whitespace_variations_georgian(self):
        # ე.g., "მონაწილე ბების ჩართულობა" (extra space)
        pass

    def test_empty_text_returns_none(self):
        assert analytics.extract_scores("") is None

    def test_score_extraction_logs_missing_dimension(self):
        # Verify logger.warning is called with missing dimension name
        pass

class TestExtractInsights:
    def test_extracts_all_five_categories(self, deep_analysis_complete):
        insights = analytics.extract_insights(deep_analysis_complete)
        assert insights["strengths_count"] >= 0
        assert insights["weaknesses_count"] >= 0
        # ... other categories

    def test_counts_bullet_points_in_each_section(self):
        pass

    def test_extracts_top_strength_text(self):
        pass

    def test_handles_missing_sections(self):
        # e.g., no ## Strengths section
        pass

# ============================================================================
# 2. Database Tests
# ============================================================================

class TestInitDb:
    def test_creates_data_directory(self, tmp_path, monkeypatch):
        # Patch DB_PATH to tmp_path
        pass

    def test_creates_tables_if_missing(self, in_memory_db):
        # Verify tables exist with correct schema
        pass

class TestUpsertScores:
    def test_inserts_new_score_row(self, in_memory_db):
        analytics.upsert_scores(1, 1, 8.0, 7.0, 9.0, 8.0, 6.0)
        result = analytics.get_scores_for_lecture(1, 1)
        assert result["group_number"] == 1
        assert result["content_depth"] == 8.0

    def test_updates_existing_row(self, in_memory_db):
        # Insert, then insert again with different scores
        # Should update, not create duplicate
        pass

    def test_rejects_invalid_group(self, in_memory_db):
        with pytest.raises(sqlite3.IntegrityError):
            analytics.upsert_scores(3, 1, 8, 7, 9, 8, 6)

    def test_rejects_lecture_out_of_range(self, in_memory_db):
        with pytest.raises(sqlite3.IntegrityError):
            analytics.upsert_scores(1, 0, 8, 7, 9, 8, 6)

class TestSaveScoresFromAnalysis:
    def test_calls_extract_scores_and_upserts(self, in_memory_db):
        deep = "| შინაარსის სიღრმე | 8/10 | ... | (complete table)"
        with patch.object(analytics, "extract_scores", return_value={"content_depth": 8, ...}):
            result = analytics.save_scores_from_analysis(1, 1, deep)
            assert result is not None

    def test_skips_if_extraction_fails(self, in_memory_db):
        with patch.object(analytics, "extract_scores", return_value=None):
            result = analytics.save_scores_from_analysis(1, 1, "bad text")
            assert result is None

# ============================================================================
# 3. Statistics Tests
# ============================================================================

class TestCalculateStatistics:
    def test_mean_calculation(self):
        scores = [5.0, 7.0, 9.0]
        stats = analytics.calculate_statistics(scores)
        assert stats["mean"] == pytest.approx(7.0)

    def test_median_odd_count(self):
        scores = [1.0, 5.0, 9.0]
        stats = analytics.calculate_statistics(scores)
        assert stats["median"] == 5.0

    def test_median_even_count(self):
        scores = [1.0, 5.0, 8.0, 10.0]
        stats = analytics.calculate_statistics(scores)
        assert stats["median"] == 6.5

    def test_stddev_single_value(self):
        stats = analytics.calculate_statistics([5.0])
        assert stats["stddev"] == 0.0

    def test_handles_all_same_scores(self):
        stats = analytics.calculate_statistics([8.0] * 10)
        assert stats["stddev"] == 0.0
        assert stats["mean"] == 8.0

# ============================================================================
# 4. Dashboard Tests
# ============================================================================

class TestGetDashboardData:
    def test_returns_dict_with_both_groups(self, in_memory_db):
        # Insert 3 scores for group 1, 2 for group 2
        # Call get_dashboard_data()
        # Verify structure and counts
        pass

    def test_includes_cross_group_stats(self, in_memory_db):
        # Verify "cross_group" key exists with combined stats
        pass

class TestRenderDashboardHtml:
    def test_html_contains_canvas_elements(self):
        data = {"groups": {...}, "cross_group": {...}}
        html = analytics.render_dashboard_html(data)
        assert "<canvas" in html
        assert "Chart" in html  # Chart.js reference

    def test_html_valid_utf8_georgian(self):
        # Verify Georgian text is preserved, not mojibaked
        pass

# ============================================================================
# 5. Backfill Tests
# ============================================================================

class TestBackfillFromTmp:
    def test_scans_tmp_for_deep_analysis_files(self, tmp_path, monkeypatch):
        # Create .tmp/deep_analysis_g1_l1_*.txt
        # Patch TMP_DIR → tmp_path
        # Call backfill_from_tmp()
        # Verify file was processed
        pass

    def test_skips_already_indexed_lectures(self, in_memory_db, tmp_path):
        # Insert g1_l1 → score already exists
        # Create g1_l1 file in tmp
        # backfill_from_tmp() should skip (or update based on policy)
        pass

    def test_handles_parse_error_in_one_file(self, tmp_path):
        # Create one good file, one bad file
        # Should process good, log bad, return counts
        pass
```

---

### Priority 1 Tests to Implement First

**Target: 50% → 80% coverage in analytics.py**

1. **test_extract_scores** (5 tests)
   - Complete table parsing
   - Missing dimensions → None
   - Whitespace/unicode variations
   - Bold wrapping (**8/10**)

2. **test_upsert_scores** (4 tests)
   - Insert new row
   - Update existing (UNIQUE constraint)
   - Reject invalid group (CHECK constraint)
   - Reject out-of-range lecture (CHECK constraint)

3. **test_get_dashboard_data** (2 tests)
   - Both groups aggregated
   - cross_group stats present

4. **test_calculate_statistics** (5 tests)
   - Mean/median/stddev
   - Edge cases: all same, single value, outliers

**Estimated effort**: 4–6 hours
**Impact**: Unlocks dashboard visibility, catch silent score loss bugs

---

## 2. server.py (70% coverage) — **HIGH PRIORITY**

**File**: `/Users/tornikebolokadze/Desktop/Training Agent/tools/app/server.py`
**Current Lines Tested**: 345/494 (70%)
**Untested Lines**: 149

### Coverage Gaps by Endpoint/Function

| Line Range | Function | Status | Why Untested | Priority |
|----------|----------|--------|-------------|----------|
| 88–89 | Logging setup | ✅ Trivial | Pass-through | LOW |
| **116, 119–122, 132** | **Host middleware config** | ❌ | Railway domain logic | P2 |
| **197** | **ProcessRecordingRequest validation** | ❌ | Invalid Drive ID regex | P1 |
| **402–403, 408–409** | **_send_callback() retry** | ✅ Partial | Only covers success/timeout | P1 |
| **429–430** | **/health endpoint edge case** | ❌ | tmp_dir write failure | P1 |
| **499–500** | **/whatsapp-incoming** | ❌ | Message type routing | P1 |
| **531–532, 539** | **Zoom CRC challenge** | ✅ Partial | Invalid plainToken edge case | P2 |
| **566** | **Zoom timestamp validation** | ✅ Partial | Boundary: exactly 300s old | P2 |
| **611–612, 616** | **_extract_recording_context** | ❌ | No MP4 in event | P1 |
| **634–718** | **_handle_meeting_ended** | ⚠️ Partial | Duration gate not tested | P1 |
| **749, 855–863** | **POST /trigger-pre-meeting** | ❌ | Manual trigger endpoint | P2 |
| **880–917** | **_manual_pipeline_task** | ❌ | Drive file download flow | P1 |
| **934–968** | **POST /manual-trigger** | ⚠️ Partial | Validation only, not full flow | P1 |
| **992–1004** | **GET /dashboard** | ❌ | Cache logic, HTML rendering | P1 |
| **1018–1024** | **GET /api/scores** | ❌ | API response format | P1 |
| **1038–1054** | **GET /api/stats** | ❌ | API response format | P1 |
| **1081–1085** | **POST /api/backfill-scores** | ❌ | Trigger backfill | P1 |

### High-Priority Gaps (P1)

#### **1. ProcessRecordingRequest.drive_folder_id validation** (Line 197)

**What**: Regex validation of Drive folder ID format
**Current test**: Only happy path tested
**Missing**:
- Invalid format (too short: `abc`, too long: 100+ chars, special chars)
- Should raise ValueError

```python
def test_invalid_drive_folder_id_raises_value_error():
    with pytest.raises(ValueError):
        ProcessRecordingRequest(
            download_url="...",
            access_token="...",
            group_number=1,
            lecture_number=1,
            drive_folder_id="abc",  # Too short
        )
```

---

#### **2. /health endpoint — tmp_dir write failure** (Line 429–430)

**What**: Health check should report degraded if tmp_dir is not writable
**Current test**: Only success path
**Missing**:
- Mock `Path.write_text()` to raise PermissionError
- Verify response status is 503, checks["tmp_dir"] contains "error"

```python
async def test_health_degraded_if_tmp_dir_not_writable():
    with patch("pathlib.Path.write_text", side_effect=PermissionError):
        response = await client.get("/health")
        assert response.status_code == 503
        assert "error" in response.json()["checks"]["tmp_dir"]
```

---

#### **3. /whatsapp-incoming — message type routing** (Line 499–500)

**What**: Filter by message type (textMessage, extendedTextMessage, quotedMessage)
**Current test**: Only happy path
**Missing**:
- Unknown type → ignored
- Missing text data → ignored
- Muted message (fromMe=True) → ignored

```python
async def test_whatsapp_incoming_ignores_unknown_type():
    body = {"typeWebhook": "incomingMessageReceived", "messageData": {"typeMessage": "imageMessage"}}
    response = await client.post("/whatsapp-incoming", json=body, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

async def test_whatsapp_incoming_ignores_own_message():
    body = {..., "messageData": {"fromMe": True, ...}}
    response = await client.post("/whatsapp-incoming", ...)
    assert response.json()["status"] == "ignored"
```

---

#### **4. _extract_recording_context — no MP4 found** (Line 611–612)

**What**: When Zoom webhook has no MP4 files, return None
**Current test**: Only happy path (MP4 present)
**Missing**:
- Only M4A files → None
- Empty recording_files → None
- Duplicate handling of shared_screen + speaker_view priority

```python
def test_extract_recording_context_no_mp4():
    body = {
        "payload": {
            "object": {
                "topic": "ჯგუფი #1",
                "recording_files": [{"file_type": "M4A"}],
            }
        }
    }
    result = _extract_recording_context(body)
    assert result is None
```

---

#### **5. _handle_meeting_ended — duration gate** (Line 667–678)

**What**: Only process if meeting lasted ≥ 120 minutes
**Current test**: NOT TESTED
**Missing**:
- Meeting < 120 min → ignored
- Meeting ≥ 120 min → processed
- Duration calculation from start_time / end_time

```python
def test_handle_meeting_ended_ignores_short_meeting():
    body = {
        "payload": {
            "object": {
                "id": "123",
                "topic": "ჯგუფი #1",
                "start_time": "2026-03-18T20:00:00Z",
                "end_time": "2026-03-18T20:45:00Z",  # 45 min
                "duration": 45,
            }
        }
    }
    result = _handle_meeting_ended(body, MagicMock())
    assert result["status"] == "ignored"
    assert result["reason"] == "duration_below_threshold"

def test_handle_meeting_ended_processes_long_meeting():
    # Same, but duration=130 min
    result = _handle_meeting_ended(body, ...)
    assert result["status"] == "accepted"
```

---

#### **6. _send_callback() — retry backoff logic** (Line 367–410)

**What**: Callback to n8n with exponential backoff (5s, 10s, 15s)
**Current test**: Only success and timeout cases
**Missing**:
- HTTP 500 → retry (not permanent)
- Network error → retry (not permanent)
- HTTP 400 → NO retry (permanent)
- Verify sleep durations (5s, 10s, 15s)

```python
async def test_send_callback_retries_on_500():
    # Mock first 2 calls → 500, third → 200
    # Verify client.post called 3 times
    pass

async def test_send_callback_no_retry_on_400():
    # Mock → 400 (bad request)
    # Verify client.post called only once
    pass
```

---

#### **7. Dashboard & API endpoints** (Lines 992–1085)

**What**: `/dashboard`, `/api/scores`, `/api/stats`, `/api/backfill-scores`
**Current test**: NOT TESTED
**Missing**:
- Response structure validation
- Filter by group param
- Cache TTL behavior (5 min)
- Authorization (WEBHOOK_SECRET)

```python
async def test_dashboard_returns_html():
    response = await client.get("/dashboard", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<canvas" in response.text

async def test_api_scores_filters_by_group():
    response = await client.get("/api/scores?group=1", headers=AUTH_HEADER)
    data = response.json()
    assert all(s["group_number"] == 1 for s in data["scores"])

async def test_api_stats_requires_auth():
    response = await client.get("/api/stats")  # No auth header
    assert response.status_code == 401
```

---

### Recommended Tests (Priority 1)

**Target: 70% → 85% coverage**

1. **test_process_recording_request_validation** (2 tests)
   - Invalid Drive ID regex
   - Valid Drive ID accepted

2. **test_health_endpoint_degraded** (1 test)
   - tmp_dir write failure → 503

3. **test_whatsapp_incoming_filtering** (3 tests)
   - Unknown message type → ignored
   - Own message → ignored
   - Empty text → ignored

4. **test_extract_recording_context_edge_cases** (3 tests)
   - No MP4 found → None
   - Priority: shared_screen_with_speaker_view > generic MP4

5. **test_handle_meeting_ended_duration_gate** (2 tests)
   - < 120 min → ignored
   - ≥ 120 min → accepted

6. **test_send_callback_retry_logic** (4 tests)
   - Retry on 500, 503
   - No retry on 400, 401
   - Alert operator on final failure

7. **test_api_endpoints** (5 tests)
   - `/api/scores` response format
   - `/api/stats` group filter
   - `/api/backfill-scores` trigger
   - All require WEBHOOK_SECRET

**Estimated effort**: 6–8 hours
**Impact**: Catch HTTP header misconfigurations, wrong response formats, security issues

---

## 3. knowledge_indexer.py (87% coverage) — **MEDIUM PRIORITY**

**File**: `/Users/tornikebolokadze/Desktop/Training Agent/tools/integrations/knowledge_indexer.py`
**Current Lines Tested**: 133/153 (87%)
**Untested Lines**: 20

### Coverage Gaps

| Line Range | Function | Gap | Priority |
|----------|----------|-----|----------|
| 115–126 | _wait_for_index_ready() | Timeout case (>120s) | P2 |
| 144–149 | embed_text() | Retry exhaustion | P2 |
| 344–345 | index_lecture_content() | Delete stale vectors failure | P2 |
| 499 | index_all_existing_content() | Placeholder function | N/A |

### Quick Wins

**1. _wait_for_index_ready timeout** (10 min)
```python
def test_wait_for_index_ready_timeout():
    mock_pc = MagicMock()
    mock_pc.describe_index.return_value.status = {"ready": False}
    with patch.object(ki, "time.sleep"):
        with pytest.raises(TimeoutError):
            ki._wait_for_index_ready(mock_pc, timeout=1)
```

**2. embed_text retry failure** (10 min)
```python
def test_embed_text_exhausts_retries():
    mock_client = MagicMock()
    mock_client.models.embed_content.side_effect = RuntimeError("Rate limited")
    with patch.object(ki, "_get_embed_client", return_value=mock_client):
        with pytest.raises(RuntimeError):
            ki.embed_text("test text")
```

---

## 4. transcribe_lecture.py (82% coverage) — **MEDIUM PRIORITY**

**File**: `/Users/tornikebolokadze/Desktop/Training Agent/tools/services/transcribe_lecture.py`
**Untested**: 26 lines (mostly error paths)

### Gaps

| Lines | Function | What | Priority |
|-------|----------|------|----------|
| 34–42 | _get_lecture_folder_id() | Config missing | P2 |
| 57 | _find_recording_in_drive() | No video in folder | P2 |
| 101–102 | transcribe_and_index() | Transcript resume (>2000 chars) | P1 |

**Quick win**: Test transcript resume threshold
```python
def test_transcript_resume_threshold_2000_chars():
    # Verify 2000 char transcript is resumed, 1999 is not
    pass
```

---

## 5. zoom_manager.py (83% coverage) — **MEDIUM PRIORITY**

**File**: `/Users/tornikebolokadze/Desktop/Training Agent/tools/integrations/zoom_manager.py`
**Untested**: 26 lines (mostly edge cases)

### Gaps

| Lines | What | Priority |
|-------|------|----------|
| 137–138 | Token request network error (attempt 3/3) | P2 |
| 234–248 | Zoom API retry after 401 | P2 |
| 424 | Download error (non-retryable) | P2 |
| 459–498 | download_all_recordings() — mixed status | P2 |

**Quick win**: Test download_all_recordings with mixed statuses
```python
def test_download_all_recordings_skips_incomplete():
    # Mock recordings with status in [completed, pending, failed]
    # Verify only completed are downloaded
    pass
```

---

## Prioritized Test Backlog

### Phase 1: Critical (Week 1) — 12–16 hours

**Target**: analytics.py (14% → 50%)

1. [ ] Create `tools/tests/test_analytics.py`
2. [ ] Score extraction tests (10 tests, 2h)
3. [ ] Database persistence tests (8 tests, 2h)
4. [ ] Statistics calculation tests (5 tests, 1h)
5. [ ] Dashboard aggregation tests (4 tests, 1h)
6. [ ] Backfill/sync tests (4 tests, 1h)

**Blockers**: None
**Impact**: Unlock analytics pipeline visibility; catch score loss bugs

---

### Phase 2: High Priority (Week 1-2) — 8–10 hours

**Target**: server.py (70% → 85%)

1. [ ] ProcessRecordingRequest validation (2 tests, 0.5h)
2. [ ] /health endpoint degraded state (1 test, 0.5h)
3. [ ] /whatsapp-incoming routing (3 tests, 1h)
4. [ ] Zoom webhook recording extraction (3 tests, 1h)
5. [ ] Duration gate in meeting.ended (2 tests, 1h)
6. [ ] Callback retry logic (4 tests, 2h)
7. [ ] API endpoints (/api/*) (5 tests, 2h)

**Blockers**: None
**Impact**: Catch webhook misconfigurations, API contract violations

---

### Phase 3: Medium Priority (Week 2-3) — 4–6 hours

**Target**: knowledge_indexer.py (87% → 95%), transcribe_lecture.py (82% → 90%)

1. [ ] Pinecone index creation timeout (1 test, 0.5h)
2. [ ] Embedding retry exhaustion (1 test, 0.5h)
3. [ ] Transcript resume threshold (1 test, 0.5h)
4. [ ] Download error handling (2 tests, 1h)
5. [ ] Mixed recording statuses (1 test, 0.5h)

**Blockers**: Depends on Phase 1-2 fixtures
**Impact**: Catch infrastructure timeout edge cases

---

## Test Execution Strategy

### 1. Setup analytics test infrastructure first

- In-memory SQLite fixture (`:memory:` DB)
- Georgian text fixtures (deep_analysis_complete)
- Mock Pinecone client fixture

### 2. Use existing test patterns

The project already has:
- `tools/tests/conftest.py` with stub modules (FastAPI, pydantic, etc.)
- `unittest.mock` for API mocking
- pytest fixtures for database/file setup

### 3. Run coverage after each phase

```bash
pytest tools/tests/ --cov=tools.services.analytics --cov=tools.app.server \
  --cov-report=term-missing -v
```

---

## Success Criteria

| Milestone | Coverage Target | Timeline |
|-----------|-----------------|----------|
| Phase 1 complete | analytics.py: 50%+ | End of Week 1 |
| Phase 2 complete | server.py: 85%+ | End of Week 1-2 |
| Phase 3 complete | Overall: 75%+ | End of Week 2-3 |
| **Final goal** | **Overall: 80%+** | **End of March** |

---

## Notes

- **analytics.py is critical**: The 14% coverage masks silent score loss bugs. Dashboard queries may succeed with incomplete data.
- **No analytics tests exist**: This is a greenfield opportunity. Start with score extraction (most critical).
- **server.py has good coverage for core flows**: The gaps are mostly edge cases (degraded health, message filtering, cache invalidation).
- **Coverage does NOT measure code quality**: 100% test coverage with bad tests is worse than 70% coverage with good tests. Focus on high-risk, high-impact code paths first.

---

**Analysis Date**: March 18, 2026
**Analyzed By**: Coverage Gap Tool
**Project**: Training Agent (AI trening session automation)
