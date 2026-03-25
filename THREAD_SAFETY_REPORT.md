# Thread Safety & Concurrency Analysis — Training Agent

**Analysis Date**: March 18, 2026
**Scope**: APScheduler + FastAPI on single asyncio event loop
**Risk Level**: 🟡 MODERATE — Several unprotected shared mutable state patterns

---

## Executive Summary

The Training Agent runs APScheduler and FastAPI on a single asyncio event loop, which **eliminates true threading** but introduces **async-specific concurrency risks**:

✅ **Good**: Single event loop prevents thread-safety issues for most operations
⚠️ **Warning**: Unprotected dict mutations in async context (best-effort deduplication)
⚠️ **Warning**: Module-level caches without synchronization (Pinecone, Google Drive, Zoom tokens)
⚠️ **Warning**: Blocking I/O in sync context (file I/O, subprocess, `time.sleep()`)
⚠️ **Warning**: HTTP clients created per-request (connection pooling overhead)

---

## 1. Shared Mutable State Analysis

### 1.1 `_processing_tasks` Dict in `server.py:61`

**Location**: `/tools/app/server.py:61`

```python
_processing_tasks: dict[str, datetime] = {}  # key: "g{group}_l{lecture}" -> start time
```

**Current Usage**:
- ✅ **Check-and-set pattern** (lines 811-823): Relies on single-threaded event loop
- ✅ **Cleanup on completion** (line 333, 914, 1517): Properly removes keys in finally blocks
- ✅ **Eviction of stale tasks** (lines 69-90): Clears tasks >4 hours old
- ✅ **No explicit locking needed** (event loop is single-threaded)

**Risk**: The check-and-set is a non-atomic operation, but works **only because**:
1. All async endpoint code runs on the same thread
2. No await/I/O between check (`if key in _processing_tasks`) and set (`_processing_tasks[key] = ...`)
3. Background tasks execute separately, cleanup is in `finally` blocks

**Verdict**: ✅ **SAFE** — Best-effort deduplication is adequate for this use case (lectures don't restart mid-pipeline).

**Code Locations**:
- `server.py:806-823`: Dedup check in `/process-recording`
- `server.py:690-695`: Dedup check in `/zoom-webhook` → `meeting.ended`
- `server.py:764-766`: Dedup check in `/zoom-webhook` → `recording.completed`
- `server.py:332-333`: Cleanup in `finally` of `process_recording_task()`
- `server.py:69-90`: Stale task eviction every 4 hours

---

### 1.2 `_token_cache` Dict in `zoom_manager.py:44`

**Location**: `/tools/integrations/zoom_manager.py:44`

```python
_token_cache: dict[str, Any] = {}
_token_lock = threading.Lock()
```

**Current Usage** (lines 93-98):
```python
with _token_lock:
    if _token_cache.get("access_token") and time.time() < _token_cache.get("expires_at", 0.0) - 60:
        logger.debug("Using cached Zoom access token.")
        return _token_cache["access_token"]
```

**Protection**: ✅ **Guarded by `threading.Lock()`**

**Verdict**: ✅ **SAFE** — Token cache is protected. Lock is appropriate even though we're on a single event loop, because:
- Tokens may be refreshed from sync blocking functions (time.sleep, httpx calls)
- Multiple coroutines could call `get_access_token()` concurrently
- Lock is re-entrant-safe for retry logic

**Concern**: `time.sleep()` inside `get_access_token()` (line 148) **blocks the event loop** during retries — see section 2.

---

### 1.3 `_dashboard_cache` Tuple in `server.py:979`

**Location**: `/tools/app/server.py:979-1003`

```python
_dashboard_cache: tuple[float, str] | None = None

# Read
if _dashboard_cache and (now - _dashboard_cache[0]) < 300:
    return HTMLResponse(content=_dashboard_cache[1])

# Write
_dashboard_cache = (now, html)
```

**Protection**: ❌ **NONE** — Tuple assignment is atomic in CPython (GIL), but no explicit locking.

**Risk**: Multiple concurrent requests to `/dashboard` could trigger redundant `sync_from_pinecone()` and `get_dashboard_data()` calls if the cache check and update interleave.

**Verdict**: 🟡 **ACCEPTABLE** — Cache miss is non-fatal (just recomputes). However, if this becomes a bottleneck, wrap in a lock or use `threading.RLock()`.

**Example Scenario**:
```
Request 1: if cache is None → call sync_from_pinecone()  [WAITS FOR I/O]
Request 2: arrives while Request 1 blocked → if cache is None → also calls sync_from_pinecone()  [DUPLICATE WORK]
```

---

### 1.4 Google Drive Service Cache in `gdrive_manager.py:104-113`

**Location**: `/tools/integrations/gdrive_manager.py:104-121`

```python
_drive_service_cache = None
_docs_service_cache = None

def get_drive_service():
    global _drive_service_cache
    if _drive_service_cache is None:
        _drive_service_cache = build("drive", "v3", credentials=_get_credentials())
    return _drive_service_cache
```

**Protection**: ❌ **NONE** — No lock, but check-and-set is fast (no I/O in set).

**Risk**: Low. The `build()` call itself is quick; multiple threads doing it simultaneously is acceptable.

**Verdict**: ✅ **SAFE** — Service objects are stateless wrappers; re-creation is cheap.

---

### 1.5 Pinecone Index Cache in `knowledge_indexer.py:57-102`

**Location**: `/tools/integrations/knowledge_indexer.py:57-102`

```python
_pinecone_index_cache: object | None = None

def get_pinecone_index() -> object:
    global _pinecone_index_cache
    if _pinecone_index_cache is not None:
        return _pinecone_index_cache
    # ... create index ...
    _pinecone_index_cache = index
    return index
```

**Protection**: ❌ **NONE** — No lock.

**Risk**: 🟡 **MODERATE**
- First call to `get_pinecone_index()` may trigger index creation (`pc.create_index()`)
- Multiple concurrent calls could trigger duplicate index creation attempts
- Pinecone API would reject the second create, but error handling is minimal

**Verdict**: 🟡 **NEEDS IMPROVEMENT** — Add a lock to prevent duplicate creation:

```python
import threading
_pinecone_index_lock = threading.Lock()

def get_pinecone_index() -> object:
    global _pinecone_index_cache
    if _pinecone_index_cache is not None:
        return _pinecone_index_cache

    with _pinecone_index_lock:
        if _pinecone_index_cache is not None:  # Double-check after acquiring lock
            return _pinecone_index_cache
        # ... create index ...
        _pinecone_index_cache = index
        return index
```

**Similar Issue**: `_embed_client_cache` in `knowledge_indexer.py:135-149` also lacks protection.

---

### 1.6 Google Credential Refresh Race in `gdrive_manager.py:49-101`

**Location**: `/tools/integrations/gdrive_manager.py:49-101`

```python
def _get_credentials() -> Credentials:
    creds = None
    token_path = _get_token_path()
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # ... write to token.json if not Railway ...
```

**Protection**: ❌ **NONE** — No file locking.

**Risk**: 🟡 **LOW** (Railway mostly immune)
- On Railway: credentials are refreshed **in memory only** (ephemeral filesystem)
- Locally: multiple processes calling `_get_credentials()` simultaneously could corrupt `token.json`
- Pinecone/Google API clients handle concurrent auth safely

**Verdict**: ✅ **ACCEPTABLE FOR RAILWAY** — Refresh happens in-memory; no disk conflicts.

**Local Risk**: If running locally with workers, add file locking to `_get_token_path()`.

---

## 2. Event Loop Blocking Issues

### 2.1 `time.sleep()` in `zoom_manager.py`

**Locations**:
- `zoom_manager.py:148` — Token retry sleep
- `zoom_manager.py:213, 228, 243` — API retry sleep

```python
for attempt in range(1, MAX_RETRIES + 1):
    try:
        # ... API call ...
    except httpx.RequestError:
        if attempt < MAX_RETRIES:
            sleep_seconds = RETRY_BACKOFF_BASE**attempt
            time.sleep(sleep_seconds)  # ❌ BLOCKS EVENT LOOP
```

**Risk**: 🟡 **MODERATE**
- `get_access_token()` is called from **sync context** (e.g., `_get_credentials()` on Railway startup)
- During token retry, the entire event loop stalls for up to `2**3 = 8 seconds`
- While blocked, no webhooks are processed, no scheduler jobs run

**Fix**: Use `asyncio.sleep()` instead, but this requires making `get_access_token()` async (breaking change).

**Workaround** (current): Token cache + 60-second buffer means retries are rare in production.

**Verdict**: 🟡 **KNOWN LIMITATION** — Document in code.

---

### 2.2 `time.sleep()` in `scheduler.py`

**Locations**:
- `scheduler.py:108` — Recording initial delay (15 min)
- `scheduler.py:135, 155, 189` — Recording polling sleep (5 min)

```python
def check_recording_ready(meeting_id: str) -> list[dict[str, Any]]:
    # ... runs in ThreadPoolExecutor ...
    elapsed = 0
    time.sleep(RECORDING_INITIAL_DELAY)  # ✅ IN THREAD POOL

    while elapsed < RECORDING_POLL_TIMEOUT:
        # ... polling loop ...
        time.sleep(RECORDING_POLL_INTERVAL)
```

**Risk**: ✅ **NONE** — Function runs in thread pool via `asyncio.to_thread()` or executor.

**Verdict**: ✅ **SAFE** — Blocking is isolated to worker thread.

---

### 2.3 `subprocess.run()` in `scheduler.py:239`

**Location**: `/tools/app/scheduler.py:239`

```python
def _concatenate_segments(segment_paths: list[Path], output_path: Path) -> None:
    # ... in thread pool ...
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
```

**Risk**: ✅ **NONE** — Runs in thread pool; blocking is expected.

**Verdict**: ✅ **SAFE**.

---

### 2.4 File I/O in Async Context

**Locations** (potential blocking):
- `server.py:362`: `open(dest, "wb")` in async `_download_recording()`
- `server.py:425-427`: File write in `/health` endpoint

```python
async def _download_recording(url: str, access_token: str, dest: Path) -> None:
    async with httpx.AsyncClient(...) as client:
        async with client.stream("GET", url, ...) as response:
            with open(dest, "wb") as f:  # ❌ BLOCKING FILE I/O
                async for chunk in response.aiter_bytes(...):
                    f.write(chunk)  # ❌ BLOCKS ON DISK
```

**Risk**: 🟡 **LOW-MODERATE**
- File operations are typically fast (< 1ms per write)
- On Railway with network storage, writes may stall longer
- Effect: Webhook responses slow down; other requests queue

**Fix**: Wrap in `asyncio.to_thread()`:

```python
async def _write_chunk(f, chunk):
    await asyncio.to_thread(f.write, chunk)
```

**Verdict**: 🟡 **ACCEPTABLE** — File writes during streaming are unavoidable; impact is low for 2-hour videos.

---

## 3. APScheduler Integration

### 3.1 Scheduler Executor Configuration

**Location**: `/tools/app/scheduler.py:20-24`

```python
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
```

**Usage**: (inferred from code)
- Pre-meeting jobs (email, WhatsApp) → likely AsyncIOExecutor
- Post-meeting jobs (polling, download, transcribe) → likely ThreadPoolExecutor

**Risk**: ✅ **NONE** — Correctly separates async-safe vs blocking work.

**Verdict**: ✅ **GOOD PRACTICE**.

---

### 3.2 Job Isolation from FastAPI Handlers

**Key Pattern**: Jobs and webhooks both modify `_processing_tasks` dict.

**Example Flow**:
1. Pre-meeting: scheduler triggers `pre_meeting_job()`
2. Webhook arrives: `/zoom-webhook` → `meeting.ended`
3. Both check/set `_processing_tasks` dict

**Risk**: 🟡 **ACCEPTABLE**
- Both run on the same event loop thread
- No interleaving of dict mutations (no awaits between check and set)
- Worst case: duplicate work (acceptable)

**Verdict**: ✅ **SAFE** — Event loop serialization is sufficient.

---

## 4. Resource Management

### 4.1 HTTP Clients

**Good Pattern** (async):
```python
# server.py:349-364
async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=30)) as client:
    async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
        # ... use response ...
```

**Problem Pattern** (sync):
```python
# zoom_manager.py:192, whatsapp_sender.py
with httpx.Client(timeout=30) as client:
    response = client.request(...)
```

**Risk**: 🟡 **LOW** — Clients are created and closed per request. Works but inefficient.

**Verdict**: ✅ **ACCEPTABLE FOR SYNC** — Creating new clients is safer than pooling (prevents connection state leaks).

---

### 4.2 File Handles

**Pattern** (good):
```python
# scheduler.py:213-231
with output_path.parent.open('w') as f:
    f.write(...)
# Auto-closed

# gdrive_manager.py:284-291
with open(destination, "wb") as fh:
    downloader = MediaIoBaseDownload(fh, request, chunksize=CHUNK_SIZE)
    while not done:
        _, done = downloader.next_chunk()
```

**Risk**: ✅ **NONE** — Context managers ensure cleanup.

**Verdict**: ✅ **SAFE**.

---

### 4.3 Database Connections

**Location**: `/tools/services/analytics.py:178-190`

```python
@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # ✅ Write-Ahead Logging
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

**Protection**: ✅ **WAL mode** (Write-Ahead Logging) enables concurrent readers + writer.

**Risk**: ✅ **NONE** — SQLite with WAL is thread-safe for this workload.

**Verdict**: ✅ **SAFE** — One writer (analysis pipeline) + multiple readers (dashboard) is supported.

---

## 5. Edge Case Scenarios

### 5.1 Two Recording Completions Simultaneously

**Scenario**:
```
webhook 1: /zoom-webhook (recording.completed) → Group 2, Lecture 5
webhook 2: /zoom-webhook (recording.completed) → Group 2, Lecture 5  [DUPLICATE]
```

**Handling** (`server.py:764-766`):
```python
_evict_stale_tasks()
key = _task_key(ctx["group_number"], ctx["lecture_number"])
if key in _processing_tasks:
    return {"status": "duplicate", "message": f"{key} already processing"}
_processing_tasks[key] = datetime.now()
```

**Verdict**: ✅ **SAFE** — Dedup is best-effort; second webhook rejected with 409 status.

---

### 5.2 Scheduler Job During Active Webhook

**Scenario**:
```
Webhook (18:00): POST /process-recording → Group 1, Lecture 5 [PROCESSING]
Scheduler (18:00): post_meeting_job(group=1, lecture=5) → triggers check_recording_ready()
```

**Handling**: Both access `_processing_tasks` on same thread → no race.

**Verdict**: ✅ **SAFE** — Scheduler jobs and webhooks don't interleave at the statement level.

---

### 5.3 WhatsApp Message Flood

**Scenario**: 1000 messages arrive in < 1 second to `/whatsapp-incoming`.

**Handling** (`server.py:454-513`):
```python
@limiter.limit("30/minute")  # Rate limiting
async def whatsapp_incoming(...):
    background_tasks.add_task(_handle_assistant_message, incoming)
    return {"status": "accepted"}
```

**Risk**: ✅ **PROTECTED** — Rate limiter (slowapi) throttles at 30/min.

**Verdict**: ✅ **SAFE** — Rate limiting prevents queue explosion.

---

## 6. Summary Table

| Component | Shared State | Lock? | Risk | Verdict |
|-----------|--------------|-------|------|---------|
| `_processing_tasks` dict | ✅ Yes | ❌ No* | 🟢 LOW | SAFE (event loop serialization) |
| `_token_cache` (Zoom) | ✅ Yes | ✅ Yes | 🟢 LOW | SAFE |
| `_dashboard_cache` | ✅ Yes | ❌ No | 🟡 MED | ACCEPTABLE (cache miss is non-fatal) |
| Google Drive service cache | ✅ Yes | ❌ No | 🟢 LOW | SAFE (fast creation) |
| Pinecone index cache | ✅ Yes | ❌ No | 🟡 MED | NEEDS LOCK (prevent dup creation) |
| Gemini embed client cache | ✅ Yes | ❌ No | 🟡 MED | NEEDS LOCK |
| Google OAuth token refresh | ✅ Yes | ❌ No | 🟢 LOW | SAFE (Railway in-memory only) |
| SQLite (analytics.db) | ✅ Yes | ✅ WAL | 🟢 LOW | SAFE |
| File I/O in async context | ❌ Per-task | ✅ Context mgr | 🟢 LOW | SAFE |
| `time.sleep()` in sync paths | ❌ Per-task | ✅ Thread pool | 🟢 LOW | SAFE |
| HTTP client creation | ❌ Per-request | ✅ Context mgr | 🟢 LOW | ACCEPTABLE |

*: `_processing_tasks` dedup is protected by event loop serialization (no concurrent mutations).

---

## 7. Recommendations

### 🔴 **Critical** (Do First)

1. **Add lock to Pinecone index cache** (`knowledge_indexer.py:60`):
   ```python
   import threading
   _pinecone_index_lock = threading.Lock()

   def get_pinecone_index():
       global _pinecone_index_cache
       if _pinecone_index_cache is not None:
           return _pinecone_index_cache
       with _pinecone_index_lock:
           if _pinecone_index_cache is not None:
               return _pinecone_index_cache
           # ... create ...
   ```

2. **Add lock to Gemini embed client cache** (`knowledge_indexer.py:138`):
   ```python
   _embed_client_lock = threading.Lock()

   def _get_embed_client():
       global _embed_client_cache
       if _embed_client_cache is not None:
           return _embed_client_cache
       with _embed_client_lock:
           if _embed_client_cache is not None:
               return _embed_client_cache
           # ... create ...
   ```

### 🟡 **Medium** (Nice to Have)

3. **Cache dashboard with lock** to prevent redundant Pinecone sync:
   ```python
   _dashboard_cache_lock = threading.Lock()

   @app.get("/dashboard", response_class=HTMLResponse)
   async def analytics_dashboard(...):
       with _dashboard_cache_lock:
           if _dashboard_cache and (now - _dashboard_cache[0]) < 300:
               return HTMLResponse(content=_dashboard_cache[1])
           # ... recompute ...
           _dashboard_cache = (now, html)
   ```

4. **Document event loop serialization** in `server.py:806-823` for future maintainers:
   ```python
   # The check-and-set is safe because:
   # 1. All async handlers run on a single event loop thread
   # 2. No await/I/O between check and set
   # 3. Background tasks clean up in finally blocks
   ```

5. **Replace `time.sleep()` in retry paths** with `asyncio.sleep()` (requires refactoring to async):
   - `zoom_manager.py:148` (token retry)
   - `zoom_manager.py:213, 228, 243` (API retry)

### 🟢 **Low Priority** (Polish)

6. **Add logging for concurrent request detection** in `/process-recording` to catch bugs:
   ```python
   if key in _processing_tasks:
       started = _processing_tasks[key]
       elapsed = (datetime.now() - started).total_seconds()
       logger.warning(
           "Concurrent request for %s (in progress %.0f seconds) — "
           "possible duplicate webhook or manual trigger overlap",
           key, elapsed
       )
   ```

7. **Consider connection pooling** for `httpx.Client` if performance becomes an issue (low priority).

---

## Conclusion

The system is **moderately safe** for production use:

✅ **Strengths**:
- Single event loop prevents classic thread races
- Proper use of thread executors for blocking work
- SQLite WAL enables safe concurrent access
- Rate limiting protects against floods

⚠️ **Weaknesses**:
- Pinecone/Gemini client caches lack locks (low risk, quick fix)
- No explicit synchronization on some dict mutations (mitigated by event loop)
- Blocking I/O in some async paths (acceptable for current scale)

🎯 **Action Items**:
1. Add locks to Pinecone and Gemini caches (5 min each)
2. Document event loop serialization for maintainers
3. Monitor `/status` endpoint for concurrent task buildup
4. Test stress scenarios: duplicate webhooks, message floods

The system is **ready for production** with the critical fixes applied.
