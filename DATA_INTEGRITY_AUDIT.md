# Data Integrity & Persistence Audit — Training Agent
**Date:** 2026-03-18 | **Scope:** SQLite, File caching, Google Drive, Pinecone, State management

---

## Executive Summary

The Training Agent has **solid foundational persistence** with **critical gaps in Railway ephemeral storage recovery** and **Pinecone deduplication edge cases**. Current architecture survives local development restarts and single-instance crashes, but is vulnerable to data loss during Railway restarts mid-pipeline and incomplete cleanup after Pinecone upserts.

**Risk Level:** MEDIUM (non-critical for 2 groups × 15 lectures, but compounds on scale)

---

## 1. SQLite Database (data/scores.db)

### Schema Design ✅
- **Normalized:** Two tables (`lecture_scores`, `lecture_insights`) with proper relationships
- **Constraints:** CHECK constraints on group_number (1-2) and lecture_number (1-15)
- **Unique constraints:** UNIQUE(group_number, lecture_number) prevents duplicates
- **Indexes:** idx_group_lecture on both group_number and lecture_number (good)

```sql
CREATE TABLE lecture_scores (
    UNIQUE (group_number, lecture_number)  -- ← enforces 1 score per lecture
);
```

### Write Patterns ✅
- **WAL mode enabled:** `PRAGMA journal_mode=WAL`
- **Transaction safety:** Context manager with commit/rollback
- **Idempotency:** Uses `INSERT OR REPLACE` for upserts
- **Atomic operations:** All writes through `_get_conn()` context manager

```python
# From analytics.py:179-190
@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()  # ← only on success
    except Exception:
        conn.rollback()  # ← on failure
    finally:
        conn.close()
```

### Read Patterns ✅
- Queries use indexed columns (group_number, lecture_number)
- No N+1 queries in hot paths
- Efficient backfill with skip-if-exists logic

### Railway Ephemeral Filesystem ⚠️ **RISK**

**Problem:** SQLite database lives in `/app/data/` which is **NOT persisted** on Railway restarts.
- On Railway restart → `.db`, `.db-wal`, `.db-shm` files are lost
- No backup/snapshot of scores or insights
- Backfill from `.tmp/` partially mitigates (see File Caching section)

**Current Recovery:**
```python
# From orchestrator.py: startup cleanup
_run_analytics_backfill()  # Restores scores from .tmp/ deep_analysis files
```

**Gap:** If a crash occurs:
1. **Before** analysis is written to `.tmp/` → score is permanently lost
2. **During** analysis (partial .tmp/ file) → extraction may fail, no retry
3. **After** restart, .tmp/ cache may be older than last processed lecture

**Impact:** 4 scores are currently in DB (verified: `sqlite3 data/scores.db "SELECT COUNT(*) FROM lecture_scores;"` → 4 rows)

### Backup Strategy ❌ **MISSING**
- No export to Google Drive (nightly snapshot)
- No CSV backup
- No version control of scores history
- Pinecone metadata (date field) is the only audit trail

### Data Validation ⚠️ **INCOMPLETE**
- Scores are extracted via regex from Georgian text
- Regex patterns are flexible (whitespace handling, bold formatting)
- **No validation before insert:**
  - No check that composite = average of 5 dimensions
  - No check that scores are in [0, 10] range
  - No check for NaN or null dimensions

---

## 2. File-Based Caching (.tmp/ directory)

### Current State
- **Size:** 1.6 GB (multiple test runs, large MP4 files, logs)
- **Contents:** Transcripts, summaries, gap analyses, deep analyses, video segments, frame logs
- **Files:** 40+ files from 3+ completed lectures, plus 500+ MB of video

### Transcript Cache ⚠️ **INVALIDATION MISSING**
Transcripts are never re-fetched; if Gemini API changes or transcript is corrupted:

```python
# From transcribe_lecture.py:
# "Resumes from existing transcript if found in .tmp/"
# NO validation that transcript is complete or valid
```

**Collision Risk:** If two webhooks arrive for the same (group, lecture) simultaneously:
- Both start transcription from the same `.tmp/` file
- No locking mechanism
- In-memory deduplication (`_processing_tasks`) only prevents **submission** of duplicate work, not file-level race conditions

### Analysis Results Collision ⚠️ **RACE CONDITION**

```python
# tools/services/transcribe_lecture.py (pseudo)
transcript_path = TMP_DIR / f"g{group}_l{lecture}_transcript.txt"
if transcript_path.exists():
    transcript = transcript_path.read_text()  # ← NO locking
else:
    transcript = transcribe_video(video_path)
    transcript_path.write_text(transcript)  # ← RACE CONDITION WINDOW
```

**Scenario:** Two concurrent `POST /recording` requests for the same lecture:
1. Thread A: reads/analyzes g1_l1_transcript.txt, starts analysis
2. Thread B: reads/analyzes same file, starts analysis
3. Both write g1_l1_summary.txt, g1_l1_gap_analysis.txt (LAST WRITE WINS)

**Mitigation:** In-memory deduplication prevents submission but not file writes:
```python
key = f"g{group}_l{lecture}"
if key in _processing_tasks:
    return 409  # Conflict
_processing_tasks[key] = datetime.now()
```

### Video Segment Cleanup ⚠️ **INCOMPLETE**

```python
# From orchestrator.py: startup
"""Remove .mp4 files older than 6 hours from .tmp/ on startup."""
```

**Current logic:** Deletes MP4s >6 hours old only on startup.
- Disk can fill during long upload to Drive
- Large MP4 files remain until next restart
- No periodic cleanup during normal operation

**Observed:** 350 MB g1_l1_video2.mp4 from Mar 18 17:41 (stale, should be cleaned)

### Transcript/Analysis Resume Logic ⚠️ **RECOVERY INCOMPLETE**

Current behavior on crash:
1. Video is partially downloaded to `.tmp/`
2. Gemini transcription fails mid-stream
3. **Incomplete transcript file remains**
4. On retry, system reads incomplete file and assumes work is done
5. **No error is raised, pipeline silently fails**

**Missing:** Validation that transcript/summary/etc are complete before resume.

---

## 3. Google Drive as Storage

### Folder Structure ✅
- Consistent naming: `ლექცია #1`, `ლექცია #2`, ... `ლექცია #15`
- Parent folder IDs configured in `config.py` GROUPS dict
- Separate private analysis folders per group (`კურსი #N ანალიზი`)

### File Naming ✅
- Collision-free format: `<lecture_num>_<type>` (e.g., "ლექცია #1 — შეჯამება")
- No date/timestamp conflicts (Google Doc title is unique key)

### Upload Idempotency ⚠️ **PARTIAL**

Google Docs use **title-based idempotency:**

```python
# From gdrive_manager.py:327-348
def create_google_doc(title: str, content: str, folder_id: str) -> str:
    safe_title = title.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name = '{safe_title}' and '{folder_id}' in parents"
    existing = service.files().list(q=query).execute().get("files", [])
    if existing:
        doc_id = existing[0]["id"]
        # UPDATE existing doc in place
        service.files().update(fileId=doc_id, media_body=media).execute()
        return doc_id
    # CREATE new doc
```

✅ **Good:** If same title is uploaded twice, existing doc is overwritten (idempotent).
⚠️ **Risk:** If title generation changes (e.g., lecture number format), duplicate docs created.

### File Upload Retry Logic ✅

```python
# From gdrive_manager.py:233-252
max_retries = 5
while response is None:
    try:
        status, response = request.next_chunk()
    except Exception as e:
        max_retries -= 1
        if max_retries <= 0:
            raise
        delay = 2 ** (5 - max_retries)
        time.sleep(delay)  # exponential backoff
```

✅ Resumable uploads with exponential backoff (good for large files >50MB)

### Link Sharing Permissions ✅
- **Public lecture folders:** Shared with entire group (query validation in `send_group_upload_notification`)
- **Private analysis folders:** Restricted to owner only via `restrict_to_owner()`

```python
def restrict_to_owner(file_or_folder_id: str) -> None:
    # Lists all permissions, deletes non-owner ones
    permissions = service.permissions().list(fileId=file_or_folder_id).execute()
    for perm in permissions:
        if perm["role"] != "owner":
            service.permissions().delete(fileId=file_or_folder_id, permissionId=perm["id"])
```

✅ Correctly removes group access from private reports.

### Permission Validation ⚠️ **MISSING**
No verification that permissions were actually changed. If deletion fails silently:
- Private report uploaded with group members still having read access
- **No alert or retry**

```python
# From gdrive_manager.py:403-411
except Exception as e:
    logger.warning("Failed to remove permission %s: %s", perm["id"], e)
    try:
        alert_operator(f"Drive permission removal FAILED...")
    except Exception as alert_err:
        logger.error("alert_operator also failed: %s", alert_err)
```

Alert-only; process continues. Better would be to retry or raise on permission errors.

---

## 4. Pinecone Vector Store

### Namespace Strategy ❌ **MISSING**

Currently **no namespace filtering** — all lectures indexed in single flat namespace:
- Vectors: `g1_l1_transcript_0`, `g1_l1_transcript_1`, ... `g2_l15_deep_analysis_N`
- Metadata filtering used at query time:
  ```python
  filter_dict = {"group_number": {"$eq": group_number}}
  ```

**Risk:** Scale to 100+ lectures or 10+ groups → large metadata filter overhead; no isolation.

**Recommendation:** Use Pinecone **projects** (if available) or embed namespace in vector ID prefix.

### Upsert Idempotency ✅

Vector IDs are deterministic:
```python
vector_id = f"g{group_number}_l{lecture_number}_{content_type}_{chunk_index}"
```

Re-upsertation with same ID replaces old vector (idempotent).

✅ **Good:** Multiple upserts of same content won't create duplicates.

### Duplicate Vector Prevention ⚠️ **EDGE CASE**

When re-indexing a lecture (e.g., corrected summary), old vectors are deleted:

```python
# From knowledge_indexer.py:335-345
index.delete(
    filter={
        "group_number": {"$eq": group_number},
        "lecture_number": {"$eq": lecture_number},
        "content_type": {"$eq": content_type},
    },
)
logger.info("Cleaned stale vectors with prefix '%s'", id_prefix)
```

⚠️ **Risk:** Delete operation may fail (network timeout, quota exceeded), but upsert proceeds anyway:
- Old vectors remain
- New vectors added → duplicate data under same (group, lecture, type)

**Current behavior:** No validation that delete succeeded before proceeding.

```python
except Exception as e:
    logger.warning("Failed to clean stale vectors: %s — proceeding with upsert", e)
```

Proceeds despite error (good for resilience, bad for data consistency).

### Metadata Completeness ✅

All required fields present:
```python
metadata = {
    "group_number": group_number,
    "lecture_number": lecture_number,
    "content_type": content_type,
    "date": today_iso,
    "chunk_index": chunk_index,
    "text": chunk,  # ← stored for retrieval
}
```

✅ Allows retrieval, filtering, and audit.

### Index Capacity 📊

- **Embedding:** gemini-embedding-001 (3072 dimensions)
- **Current data:** 4 lectures × 4 content types × ~100-200 chunks each = ~1,600-3,200 vectors
- **Capacity:** Pinecone serverless can handle 100M+ vectors in practice

✅ No scaling issues for current scope (30 lectures max = ~24,000 vectors worst case).

---

## 5. State Management

### In-Flight Task Tracking ✅
```python
# From server.py:61-62
_processing_tasks: dict[str, datetime] = {}  # key: "g{group}_l{lecture}"
STALE_TASK_HOURS = 4
```

✅ Prevents duplicate webhook submissions
✅ Auto-evicts tasks running >4 hours (Railway restart resilience)

**Cleanup on completion:**
```python
def _run_and_cleanup() -> None:
    try:
        _run_post_meeting_pipeline(group_number, lecture_number, poll_id)
    finally:
        _processing_tasks.pop(key, None)  # ← always clean up
```

✅ Even on exception, task is removed from tracking.

### Crash Recovery ⚠️ **PARTIAL**

On Railway restart or crash:
1. `.tmp/` files may be partially written
2. Pinecone may have partial upserts
3. SQLite DB is **lost entirely** (ephemeral filesystem)
4. In-memory `_processing_tasks` is lost
5. APScheduler job queue is preserved (checks `misfire_grace_time: 55 min`)

**Recovery sequence:**
1. Orchestrator starts
2. `validate_credentials()` checks all API keys (good)
3. `_run_analytics_backfill()` restores scores from `.tmp/` (good)
4. `.tmp/ cleanup` deletes stale MP4s (good)
5. If `.tmp/` files are corrupted → backfill fails silently

**Missing:** Validation that backfill actually restored data.

### Single Source of Truth ❌ **FRAGMENTED**

There is **no canonical state tracker** for lecture processing:
- **Pinecone metadata:** Has `date` field (when indexed)
- **SQLite scores:** Has `processed_at` (when scores extracted)
- **Google Drive folder:** Has file timestamps (when uploaded)
- **.tmp/ files:** Have modification times (when analysis completed)
- **n8n workflow logs:** Store webhook delivery history

**Problem:** If a Pipeline fails mid-way (e.g., Pinecone upsert succeeds but Drive upload fails), there's no unified log of what completed and what didn't.

**Recommendation:** Implement a **processing_status** table in SQLite:
```sql
CREATE TABLE lecture_processing_status (
    group_number INT,
    lecture_number INT,
    video_downloaded BOOLEAN,
    transcribed BOOLEAN,
    summary_uploaded BOOLEAN,
    gap_analysis_completed BOOLEAN,
    deep_analysis_completed BOOLEAN,
    indexed_in_pinecone BOOLEAN,
    whatsapp_notified BOOLEAN,
    completed_at TEXT,
    UNIQUE (group_number, lecture_number)
);
```

---

## 6. Orphaned Resources

### Incomplete Uploads ⚠️

If Drive upload fails mid-chunk:
- Resumable upload session left open on Google Drive
- Eventually cleaned up by Drive (default: 1 week)
- No local tracking of partial uploads

**Mitigation:** Drive's resumable session timeout is automatic; acceptable for low-frequency uploads.

### Partial Transcripts ⚠️

If Gemini API times out mid-transcription:
- `.tmp/g{group}_l{lecture}_transcript.txt` contains partial text
- File exists → subsequent retries assume work is complete
- **System silently skips re-transcription**

**Current behavior:** No validation of transcript completeness (no checksum, length check, etc.)

### Stale Vectors in Pinecone ⚠️

If upsert fails after delete:
- Stale vectors remain in index
- New vectors added
- **Duplicate entries under same (group, lecture, content_type)**
- Query results return both old and new → duplicate context in assistant

---

## 7. Scaling Concerns

### At 30 lectures (2 groups × 15 each):

| Component | Current | Max Comfortable | Risk |
|-----------|---------|-----------------|------|
| SQLite (scores) | 4 rows | 1,000+ | ✅ Low |
| .tmp/ size | 1.6 GB | 10 GB (disk limit) | ⚠️ Medium |
| Pinecone vectors | ~1,600 | 100M+ | ✅ Low |
| Drive files | ~12 docs + 12 videos | 1,000s | ✅ Low |
| In-flight tasks | 1-2 | 10+ | ⚠️ Medium |

**Bottleneck:** Disk space (Railway: 1 GB default) → `.tmp/` cleanup is critical.

---

## Risk Summary

| Risk | Severity | Mitigation | Owner |
|------|----------|-----------|-------|
| SQLite data loss on Railway restart | MEDIUM | Backfill from .tmp/ + Drive export | Persistence |
| Partial transcript resume | MEDIUM | Add transcript validation/checksum | File Cache |
| Pinecone duplicate vectors after failed delete | LOW | Add delete verification before upsert | Pinecone |
| File-level race conditions (.tmp/) | LOW | Add file-level locking or queue single writer | Concurrency |
| No unified processing status | MEDIUM | Implement status table + update atomically | State |
| Permission removal silent failures | MEDIUM | Retry on permission error or alert+block | Drive |
| .tmp/ disk filling | LOW | Add periodic cleanup, monitor size | Cleanup |

---

## Recommendations (Priority Order)

### 🔴 High Priority (Deploy within 1 sprint)
1. **Add .tmp/ cleanup during runtime** (not just on startup)
   - Cron job to delete MP4s >4 hours old every 30 minutes
   - Log cleanup events

2. **Implement transcript validation**
   - Min length check (e.g., >100 chars)
   - Checksum or last-modified time stamp
   - Skip if valid, re-transcribe if corrupted

3. **Add processing status tracking**
   - Create `lecture_processing_status` table
   - Update atomically at each pipeline stage
   - Query to determine "which lectures are complete?"

### 🟡 Medium Priority (Deploy within 2 sprints)
4. **Export SQLite scores to Google Drive daily**
   - Create Google Sheet with scores history
   - Append row after each lecture

5. **Add Pinecone delete verification**
   - Check that vectors are gone before upsert
   - If delete failed, retry with exponential backoff

6. **Implement file-level locking for .tmp/**
   - Use `fcntl` (Unix) or `msvcrt` (Windows) file locks
   - Lock during transcription/analysis writes

### 🟢 Low Priority (Long-term)
7. **Migrate to PostgreSQL on Railway**
   - Persistent storage via Railway's PostgreSQL add-on
   - Eliminate backfill complexity
   - Enables real ACID transactions

8. **Use Pinecone projects/namespaces**
   - Isolate groups into separate projects
   - Better scaling for 10+ groups

9. **Implement unified audit log**
   - Single source of truth for all pipeline events
   - CloudWatch/Datadog integration

---

## Testing Checklist

- [ ] Verify backfill restores all scores on Railway restart
- [ ] Confirm duplicate webhook submission is rejected
- [ ] Test Drive upload resumption after network failure
- [ ] Verify private reports are inaccessible to group members
- [ ] Check Pinecone queries return correct group-filtered results
- [ ] Confirm .tmp/ cleanup runs without deleting in-progress files
- [ ] Validate stale task eviction after 4 hours
- [ ] Test crash recovery during each pipeline stage

---

## Conclusion

The Training Agent has **solid local persistence** and **good crash resilience** for the current 2-group, 15-lecture scope. The main vulnerability is **Railway's ephemeral filesystem** combined with **incomplete validation and recovery mechanisms**. Implementing the high-priority recommendations (status tracking, .tmp/ cleanup, transcript validation) will improve reliability to production-grade.

**Current readiness:** ✅ **70% — suitable for MVP, needs hardening for scale.**
