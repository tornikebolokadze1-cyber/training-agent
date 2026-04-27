# Test Coverage Backlog — Quick Reference

Generated: March 18, 2026

---

## Priority P0: CRITICAL — Analytics (14% coverage)

**Create**: `tools/tests/test_analytics.py`

### Must Have (Implement First)

| # | Test | Lines | Impact | Effort |
|---|------|-------|--------|--------|
| 1 | `test_extract_scores_complete_table` | 77–109 | Parse all 5 score dimensions from Georgian markdown | 1h |
| 2 | `test_extract_scores_missing_dimension_returns_none` | 77–109 | Handle incomplete score tables gracefully | 0.5h |
| 3 | `test_upsert_scores_insert_and_update` | 197–230 | Verify UNIQUE constraint (group, lecture) works | 1h |
| 4 | `test_get_dashboard_data_aggregates_groups` | 737–774 | Both groups present in output | 1h |
| 5 | `test_calculate_statistics_mean_median_stddev` | 500–556 | Numerical accuracy (edge cases: all same, outliers) | 1h |
| 6 | `test_extract_insights_all_categories` | 299–375 | Count strengths, weaknesses, gaps, recommendations | 1.5h |
| 7 | `test_backfill_from_tmp_processes_files` | 791–831 | Scan .tmp/ for deep_analysis files | 1h |

**Subtotal**: 7.5 hours → **analytics: 14% → 50%+**

---

## Priority P1: HIGH — Server Endpoints (70% coverage)

**Update**: `tools/tests/test_server.py`

### Must Have (Add to existing file)

| # | Test | Lines | Impact | Effort |
|---|------|-------|--------|--------|
| 8 | `test_process_recording_request_invalid_folder_id` | 197 | Regex validation of Drive ID | 0.5h |
| 9 | `test_health_endpoint_degraded_tmp_not_writable` | 429–430 | Return 503 if tmp_dir inaccessible | 0.5h |
| 10 | `test_whatsapp_incoming_ignores_unknown_type` | 499–500 | Filter by message type correctly | 1h |
| 11 | `test_whatsapp_incoming_ignores_own_message` | 495 | Skip messages from bot (fromMe=True) | 0.5h |
| 12 | `test_extract_recording_context_no_mp4_returns_none` | 611–612 | Gracefully handle missing MP4 files | 0.5h |
| 13 | `test_handle_meeting_ended_duration_gate_ignore_short` | 667–678 | Meetings <120 min ignored (temporary disconnect) | 1h |
| 14 | `test_handle_meeting_ended_duration_gate_accept_long` | 667–678 | Meetings ≥120 min → start pipeline | 1h |
| 15 | `test_send_callback_retries_on_500` | 390–399 | Callback retries with backoff (5s, 10s, 15s) | 1h |
| 16 | `test_api_scores_returns_json` | 1018–1024 | `/api/scores` response format | 0.5h |
| 17 | `test_api_stats_filters_by_group` | 1038–1054 | `/api/stats?group=1` filter works | 0.5h |
| 18 | `test_dashboard_requires_auth_returns_html` | 992–1004 | `/dashboard` needs WEBHOOK_SECRET, returns HTML | 1h |
| 19 | `test_api_backfill_scores_triggers_backfill` | 1081–1085 | `/api/backfill-scores` works | 0.5h |

**Subtotal**: 9 hours → **server: 70% → 85%+**

---

## Priority P2: MEDIUM — Integrations & Services

### Quick Wins (4–5 hours total)

| # | Test | Module | Lines | Effort |
|---|------|--------|-------|--------|
| 20 | `test_wait_for_index_ready_timeout` | knowledge_indexer | 115–126 | 0.5h |
| 21 | `test_embed_text_exhausts_retries` | knowledge_indexer | 144–149 | 0.5h |
| 22 | `test_index_lecture_content_stale_vector_cleanup_fails` | knowledge_indexer | 344–345 | 0.5h |
| 23 | `test_transcript_resume_threshold_2000_chars` | transcribe_lecture | 101–102 | 0.5h |
| 24 | `test_get_lecture_folder_id_missing_config` | transcribe_lecture | 34–42 | 0.5h |
| 25 | `test_download_all_recordings_skips_incomplete` | zoom_manager | 459–498 | 1h |
| 26 | `test_zoom_api_retry_after_401` | zoom_manager | 234–248 | 1h |

**Subtotal**: 5 hours → **knowledge_indexer: 87% → 92%+**, **transcribe: 82% → 90%+**, **zoom: 83% → 90%+**

---

## Implementation Roadmap

### Week 1 (Target: 16–18 hours)

**Phase 1: Analytics Foundation** (7.5 hours)
- [ ] Create `tools/tests/test_analytics.py` with fixtures
- [ ] Implement tests #1–7 (score extraction, DB, stats, dashboard, backfill)
- [ ] Verify analytics.py coverage: 14% → 50%+

**Phase 2: Server Endpoints** (9 hours)
- [ ] Implement tests #8–19 in `test_server.py`
- [ ] Verify server.py coverage: 70% → 85%+

**Cumulative Progress**: Overall coverage 50%+ (from ~45%)

---

### Week 2 (Target: 5–6 hours)

**Phase 3: Integrations** (5 hours)
- [ ] Implement tests #20–26 (quick wins)
- [ ] Verify knowledge_indexer, transcribe, zoom coverage: 87%+ → 92%+

**Final state**: Overall coverage 75%+

---

## Fixture Dependencies

```python
# tools/tests/test_analytics.py — new fixtures needed

@pytest.fixture
def in_memory_db():
    """SQLite :memory: DB for tests."""
    with patch.object(analytics, "DB_PATH", ":memory:"):
        analytics.init_db()
        yield

@pytest.fixture
def deep_analysis_complete():
    """Georgian deep analysis with complete 5D score table."""
    return """
## დიპ ანალიზი

| მეტრიკა | ქულა | კომენტარი |
|---------|------|----------|
| **შინაარსის სიღრმე** | **8/10** | ✓ |
| **პრაქტიკული ღირებულება** | **7/10** | ✓ |
| **მონაწილეების ჩართულობა** | **9/10** | ✓ |
| **ტექნიკური სიზუსტე** | **8/10** | ✓ |
| **ბაზრის რელევანტურობა** | **6/10** | ✓ |

## Strengths
- Point 1
- Point 2

## Weaknesses
- Issue 1

## Gaps
- Gap 1

## Recommendations
- Rec 1

## Technical Issues
- Tech 1

## Blind Spots
- Blind 1
"""
```

---

## Running Tests

```bash
# Run all tests with coverage
pytest tools/tests/ --cov=tools --cov-report=term-missing -v

# Run only analytics tests
pytest tools/tests/test_analytics.py -v

# Run only server tests
pytest tools/tests/test_server.py -v

# Check coverage progress
pytest tools/tests/ --cov=tools.services.analytics --cov-report=term-missing -q
```

---

## Coverage Goals

| Module | Current | Phase 1 | Phase 2 | Phase 3 |
|--------|---------|---------|---------|---------|
| analytics.py | 14% | 50% | 50% | 60% |
| server.py | 70% | 70% | 85% | 85% |
| knowledge_indexer.py | 87% | 87% | 87% | 92% |
| transcribe_lecture.py | 82% | 82% | 82% | 90% |
| zoom_manager.py | 83% | 83% | 83% | 90% |
| **Overall** | ~50% | ~55% | ~70% | **75%+** |

---

## Risk Mitigation

- **Silent score loss**: Tests #1–5 directly prevent this (analytics extraction + DB)
- **Webhook misconfigurations**: Tests #10–14 catch message filtering + recording context bugs
- **API contract violations**: Tests #16–18 verify response format/auth
- **Infrastructure timeouts**: Tests #20–22 catch Pinecone/Gemini edge cases

---

## Notes

- All tests use **existing pytest/mock patterns** from conftest.py
- Analytics tests are **greenfield** (no test file exists) — start there first
- Server tests are **incremental** (add to existing test_server.py)
- No external API calls needed (all mocked)

---

**Last Updated**: March 18, 2026
**Status**: Ready to implement
