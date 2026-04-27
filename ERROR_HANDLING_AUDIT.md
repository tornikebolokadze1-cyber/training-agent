# Training Agent — Error Handling Audit Report

**Date**: 2026-03-18
**Scope**: All error handling patterns in `tools/**/*.py`
**Finding**: Good retry logic and alerting patterns, but several silent failures and missing resource cleanup.

---

## Executive Summary

### Strengths
✅ **Retry logic** (`tools/core/retry.py`): Exponential backoff is correct, max_retries and jitter are well-designed
✅ **Operator alerts** (`whatsapp_sender.alert_operator`): Fallback to CRITICAL logs when WhatsApp fails
✅ **Stale task eviction** (server.py): 4-hour timeout properly detects hung pipelines
✅ **API error classification** (whatsapp_sender, zoom_manager): Distinguishes transient vs permanent errors

### Critical Issues Found
🔴 **Silent failures** (5 instances): `except Exception: pass` swallows errors
🔴 **Missing `finally` cleanup** (3 instances): File handles and temp files not guaranteed cleaned
🔴 **No timeout on Gemini polling** (gemini_analyzer.py): Can hang indefinitely
🔴 **Broad exception catching** (3+ instances): `except Exception` masks root causes

### Medium Severity
🟡 **Async + blocking code** (scheduler.py): `time.sleep()` blocks the thread pool
🟡 **Missing error context** (analytics.py): Errors logged without full stack traces
🟡 **Incomplete error re-raising**: Some caught errors suppress propagation

---

## Detailed Findings

### 1. Retry Logic (`tools/core/retry.py`) — ✅ CORRECT

**Pattern**:
```python
def retry_with_backoff(func, *args, max_retries=3, backoff_base=2.0, ...):
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except retryable_exceptions as exc:
            if attempt < max_retries:
                delay = backoff_base * (2 ** (attempt - 1))  # Exponential: 2, 4, 8 sec
                logger.warning("... retrying in %.1fs", delay)
                time.sleep(delay)
            else:
                logger.error("... failed after %d attempts", max_retries)
    raise last_exc
```

**Evaluation**:
- ✅ Exponential backoff: `2 ** (attempt - 1)` gives 1x, 2x, 4x delays
- ✅ Max retries: hardcoded sensible defaults (3 retries, 2sec base)
- ✅ Error re-raised: fails closed, doesn't swallow
- ✅ Logging: tracks each attempt with timing
- ✅ No jitter: deterministic, but OK for inter-service calls (n8n polls, Gemini)

**Concern**: No jitter means thundering herd if all clients retry simultaneously at same intervals.
**Recommendation**: Add `random.uniform(0.8, 1.2) * delay` for distributed systems.

---

### 2. Operator Alerts (`tools/integrations/whatsapp_sender.py:237–258`) — ✅ GOOD

**Pattern**:
```python
def alert_operator(message: str) -> None:
    """Last-resort alert — this function NEVER raises."""
    prefix = "⚠️ Training Agent ALERT\n\n"
    try:
        if WHATSAPP_TORNIKE_PHONE and GREEN_API_INSTANCE_ID:
            send_message_to_chat(chat_id, prefix + message)
            logger.info("Operator alert sent via WhatsApp")
            return
    except BaseException as exc:  # Catch ALL exceptions
        logger.error("Failed to send WhatsApp alert: %s", exc)

    # Fallback: CRITICAL log
    logger.critical("OPERATOR ALERT (WhatsApp unavailable): %s", message)
```

**Evaluation**:
- ✅ Broad exception catch: `BaseException` (not just `Exception`) catches system errors
- ✅ Fallback mechanism: CRITICAL logs if WhatsApp fails
- ✅ Promise of no raise: clearly documented, implements safety net
- ✅ Used throughout: called in server.py, scheduler.py, retry.py

**Usage in codebase**:
- `server.py:85,325,401,407,530` — calls after pipeline failures
- `scheduler.py:124,198,314` — calls after recording/polling failures
- `retry.py:135` — called by `@safe_operation` decorator

**Concern**: Only alerts Tornike (hardcoded phone number). If WhatsApp credentials are misconfigured, silent failure in production.

---

### 3. Stale Task Recovery (`tools/app/server.py:62–90`) — ✅ CORRECT

**Pattern**:
```python
STALE_TASK_HOURS = 4

def _evict_stale_tasks() -> list[str]:
    """Remove tasks running > 4 hours."""
    now = datetime.now()
    stale = [
        key for key, started in _processing_tasks.items()
        if (now - started).total_seconds() > STALE_TASK_HOURS * 3600
    ]
    for key in stale:
        _processing_tasks.pop(key, None)
        logger.warning("Evicted stale task: %s (exceeded %dh timeout)", key, STALE_TASK_HOURS)
    if stale:
        try:
            alert_operator(f"Evicted {len(stale)} stale tasks...")
        except Exception as alert_err:
            logger.warning("alert_operator failed during stale task eviction: %s", alert_err)
    return stale
```

**Evaluation**:
- ✅ Timeout tuned: 4 hours is reasonable for 2-hour lectures + analysis
- ✅ Called at key points: `/process-recording:804`, `/manual-trigger:943`, `zoom_webhook:762`
- ✅ Dedup key management: cleaned up in `process_recording_task:332` and `_manual_pipeline_task:914`
- ✅ Alert on eviction: operators notified of hung pipelines

**Concern**: Only cleaned in `finally` blocks. If exception thrown before finally, task stays in `_processing_tasks`.
**Status**: Acceptable — 4-hour window catches stragglers before they block next lecture.

---

### 4. Silent Failures — 🔴 CRITICAL

#### 4.1 `tools/services/analytics.py:186`
```python
def get_dashboard_data():
    try:
        # ... lots of code ...
    except Exception:  # NO LOGGING
        pass  # ← SILENT FAILURE
    return {...}  # returns empty/default dict
```

**Issue**: If the dashboard data fetch fails, the user sees an empty dashboard with no error indication.
**Fix**: At minimum: `except Exception as e: logger.error("Dashboard data fetch failed: %s", e)`

---

#### 4.2 `tools/app/scheduler.py:203`
```python
except Exception:  # Extra poll failed
    logger.warning("... Extra poll failed: %s — proceeding with %d segment(s)", exc, len(mp4_files))
```

**Status**: Actually OK — error IS logged (my grep caught the except, not the lambda body).

---

#### 4.3 `tools/app/scheduler.py:506`
```python
except Exception:  # inside extra poll
    logger.error("[recording] alert_operator also failed for meeting %s", meeting_id)
```

**Status**: Logs to CRITICAL level when alert fails — acceptable safety net pattern.

---

#### 4.4 `tools/core/config.py:50`
```python
try:
    _base64_data = os.environ[var_name]
    raw_json = base64.b64decode(_base64_data)
    return json.loads(raw_json)
except Exception as exc:
    logger.error("Failed to load %s from %s: %s", var_name, env_var_name, exc)
    return {}  # ← SILENT: returns empty dict
```

**Issue**: If Google credentials decode fails, the app silently falls back to empty creds dict. Later calls to Drive API will fail with cryptic "401" errors.
**Recommendation**: Return `None` instead of `{}`, and check at call sites.

---

#### 4.5 `tools/services/analytics.py:871–877`
```python
try:
    from tools.services.analytics import get_dashboard_data, render_dashboard_html
except ImportError:
    return None  # ← SILENT: import failures in analytics module
```

**Status**: OK if analytics is truly optional. But no fallback UI is provided.

---

### 5. Missing `finally` Blocks — 🔴 MEDIUM

#### 5.1 `tools/integrations/gdrive_manager.py:232–256` (Resumable Upload)
```python
response = None
max_retries = 5
while response is None:
    try:
        status, response = request.next_chunk()
        if status:
            progress = int(status.progress() * 100)
            logger.info("Upload progress: %d%%", progress)
    except Exception as e:
        max_retries -= 1
        if max_retries <= 0:
            logger.error("Upload failed after retries: %s", e)
            raise
        import time
        delay = 2 ** (5 - max_retries)
        logger.warning("Upload chunk failed (%d retries left): %s — retrying in %ds",
                       max_retries, e, delay)
        time.sleep(delay)
```

**Issue**: The `MediaFileUpload` object is never explicitly closed. If chunks are abandoned after exceptions, the file handle remains open.
**Recommendation**:
```python
media = MediaFileUpload(...)
try:
    request = service.files().create(...)
    while response is None:
        try:
            status, response = request.next_chunk()
        except Exception as e:
            # retry logic
finally:
    media.stream.close()  # Explicit cleanup
```

---

#### 5.2 `tools/app/scheduler.py:213–246` (Concat with ffmpeg)
```python
def _concatenate_segments(segment_paths: list[Path], output_path: Path) -> None:
    concat_list = output_path.parent / f"{output_path.stem}_segments.txt"
    concat_list.write_text(...)  # ← file created but...

    cmd = [...]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    concat_list.unlink(missing_ok=True)  # ← cleanup only on success path

    if result.returncode != 0:
        raise RuntimeError(...)  # ← leaves concat_list on disk if exception raised!
```

**Issue**: If `subprocess.run()` raises (e.g., timeout), `concat_list` file is not deleted.
**Fix**:
```python
try:
    concat_list.write_text(...)
    result = subprocess.run(...)
    if result.returncode != 0:
        raise RuntimeError(...)
finally:
    concat_list.unlink(missing_ok=True)
```

---

#### 5.3 `tools/app/server.py:340–364` (Streaming Download)
```python
async def _download_recording(url: str, access_token: str, dest: Path) -> None:
    dest = Path(dest)
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=30)) as client:
        async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()

            with open(dest, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
```

**Status**: Actually OK — Python context managers (`async with`, `with`) guarantee cleanup even on exception.

---

### 6. API Error Classification — ✅ GOOD

#### 6.1 `tools/integrations/whatsapp_sender.py:60–107` (Transient vs Permanent)
```python
class _NonRetryableError(Exception):
    """HTTP 4xx (except 429) should not be retried."""

def _send_request(method: str, payload: dict, purpose: str) -> dict:
    def _do_request() -> dict:
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json=payload)

        if response.status_code == 200:
            return response.json()

        # Don't retry client errors (except 429)
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise _NonRetryableError(f"HTTP {response.status_code}: {response.text}")
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")

    try:
        return retry_with_backoff(
            _do_request,
            max_retries=MAX_RETRIES,
            retryable_exceptions=(RuntimeError, httpx.TransportError),
            operation_name=purpose,
        )
    except _NonRetryableError as exc:
        raise RuntimeError(f"... failed with non-retryable error: {exc}")
```

**Evaluation**:
- ✅ Distinguishes 429 (rate limit, retryable) from 400–499 other (client error, not retryable)
- ✅ 5xx errors are retried
- ✅ Transport errors (network) are retried
- ✅ Clear exception types: `ZoomAuthError`, `ZoomAPIError`, `ZoomDownloadError`

---

#### 6.2 `tools/integrations/zoom_manager.py` (Auth Error Handling)
```python
def get_access_token() -> str:
    # ... first try cached token ...

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(ZOOM_OAUTH_URL, headers=headers, data=data)

            if response.status_code == 200:
                payload = response.json()
                access_token = payload["access_token"]
                # ... cache and return ...
                return access_token

            logger.warning("Token request failed (attempt %d/%d): HTTP %d — %s",
                          attempt, MAX_RETRIES, response.status_code, response.text)
        except httpx.RequestError as exc:
            logger.warning("Token request network error (attempt %d/%d): %s",
                          attempt, MAX_RETRIES, exc)

        if attempt < MAX_RETRIES:
            sleep_seconds = RETRY_BACKOFF_BASE ** attempt  # 2, 4, 8 sec
            time.sleep(sleep_seconds)

    raise ZoomAuthError("Failed to obtain access token after 3 attempts")
```

**Evaluation**:
- ✅ Catches network errors specifically (`httpx.RequestError`)
- ✅ Retries with backoff
- ✅ Specific exception type: `ZoomAuthError`
- ✅ Does NOT retry client errors (400–499) — would fail immediately and raise

---

### 7. Graceful Degradation — Mixed

#### 7.1 Gemini Down (gemini_analyzer.py:51–55)
```python
def _is_quota_error(error: Exception) -> bool:
    """Check if an error is a quota/rate-limit issue."""
    error_str = str(error).lower()
    quota_indicators = ["429", "resource exhausted", "quota", "rate limit", "too many requests"]
    return any(indicator in error_str for indicator in quota_indicators)
```

**Usage** (gemini_analyzer.py:525–565):
```python
@safe_operation("Gemini analysis", alert=True)
def run_gemini_analysis(...):
    try:
        # ... call Gemini ...
    except _quota_error:
        # Try free key
        use_free = True
        # retry with free key
```

**Status**: ✅ Falls back from paid to free key when quota exhausted.

---

#### 7.2 Drive Down
If Google Drive API is unreachable:
- `server.py:286–290`: `ensure_folder()` will raise `googleapiclient.errors.HttpError`
- `process_recording_task:312–329`: caught as generic `Exception`, logged, alerts operator, returns
- **Status**: ✅ Fails gracefully with operator alert

---

#### 7.3 Pinecone Down (knowledge_indexer.py)
```python
def index_lecture_content(group, lecture, content_dict):
    try:
        # ... index to Pinecone ...
    except Exception as e:
        logger.error("Pinecone indexing failed: %s", e)
        # NO ALERT TO OPERATOR
        return {}  # Silent failure
```

**Issue**: If Pinecone is down, the lecture is still delivered to the group, but the assistant won't have RAG context.
**Concern**: No degradation warning to the operator.

---

### 8. Timeout Issues — 🔴 CRITICAL

#### 8.1 No Timeout on Gemini Polling (gemini_analyzer.py:540–580)
```python
def transcribe_with_gemini(video_path):
    client = _get_client()
    file = client.files.upload(media=video_file)  # Upload video

    # POLL until processing is done
    while True:
        file = client.files.get(name=file.name)
        if file.state == STATE_ACTIVE:
            time.sleep(FILE_POLL_INTERVAL)  # 10 seconds
        elif file.state == STATE_FAILED:
            raise RuntimeError("File processing failed")
        else:
            break  # Processing done
```

**Issue**: No timeout! If Gemini hangs, this poll loop runs forever.
**Current timeout**: Implicitly relies on:
- `FILE_POLL_TIMEOUT = 600` (10 min) — but NOT used in this function
- APScheduler job timeout (if configured)

**Recommendation**:
```python
elapsed = 0
while elapsed < FILE_POLL_TIMEOUT:
    file = client.files.get(name=file.name)
    if file.state == STATE_ACTIVE:
        time.sleep(FILE_POLL_INTERVAL)
        elapsed += FILE_POLL_INTERVAL
    elif file.state == STATE_FAILED:
        raise RuntimeError("File processing failed")
    else:
        break

if elapsed >= FILE_POLL_TIMEOUT:
    raise TimeoutError(f"Gemini file processing timed out after {FILE_POLL_TIMEOUT}s")
```

---

#### 8.2 Timeout on Pinecone Index Creation (knowledge_indexer.py:105–128)
```python
def _wait_for_index_ready(pc: Pinecone, timeout: int = 120) -> None:
    """Poll until index is ready."""
    elapsed = 0
    while elapsed < timeout:
        description = pc.describe_index(PINECONE_INDEX_NAME)
        status = description.status
        ready = status.get("ready", False) if isinstance(status, dict) else getattr(status, "ready", False)
        if ready:
            return
        logger.debug("Index not ready yet (%ds elapsed), waiting...", elapsed)
        time.sleep(5)
        elapsed += 5
    raise TimeoutError(...)
```

**Status**: ✅ Timeout enforced (120 seconds)

---

#### 8.3 Network Timeouts
- ✅ `server.py:349`: `httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=30))` — 30min for large downloads
- ✅ `server.py:381`: `httpx.AsyncClient(timeout=30)` — 30s for n8n callback
- ✅ `whatsapp_sender.py:83`: `httpx.Client(timeout=30)` — 30s for Green API
- ✅ `zoom_manager.py:115`: `httpx.Client(timeout=30)` — 30s for Zoom OAuth
- ✅ `gdrive_manager.py`: No explicit timeout on Drive API calls — **CONCERN**
  - `service.files().create().execute()` has no timeout
  - Could hang indefinitely

**Fix for Drive**: Wrap in `asyncio.wait_for()` or `timeout_decorator`.

---

### 9. Exception Context Loss — 🟡 MEDIUM

#### 9.1 `tools/services/transcribe_lecture.py:185`
```python
@safe_operation("Drive summary upload", alert=True)
def _upload_summary_to_drive(...):
    # happy-path only — no try/except needed
    folder_id = _get_lecture_folder_id(...)
    if not folder_id:
        return None
    title = f"ლექცია #{lecture_number} — შეჯამება"
    doc_id = create_google_doc(title, summary, folder_id)
    logger.info("Uploaded summary to Drive: ...")
    return doc_id
```

**Pattern**: Uses `@safe_operation` decorator which catches all exceptions and alerts operator.

**Concern**: The decorator logs at ERROR level but doesn't include the full stack trace.
**Fix**: In `retry.py:129–130`, change to:
```python
except Exception as exc:
    logger.error("Failed to %s: %s", operation_name, exc, exc_info=True)
```

---

#### 9.2 `tools/app/server.py:312–329` (Background Task Errors)
```python
async def process_recording_task(payload):
    local_path = None
    try:
        # ... steps 1–4 ...
    except Exception as e:
        error_msg = f"Processing failed: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)  # ✅ Has full traceback

        await _send_callback(CallbackPayload(..., error_message=str(e)))
        await asyncio.to_thread(alert_operator, f"Pipeline FAILED: {e}")

    finally:
        # cleanup
```

**Status**: ✅ Logs full traceback with `traceback.format_exc()`

---

### 10. Resource Cleanup — Mixed

#### 10.1 Temp Files (server.py, scheduler.py) — ✅ GOOD
```python
finally:
    key = _task_key(group, lecture)
    _processing_tasks.pop(key, None)

    if local_path and local_path.exists():
        local_path.unlink()  # Explicit delete
        logger.info("Cleaned up temp file: %s", local_path)
```

**Status**: Guaranteed cleanup via `finally`.

---

#### 10.2 APScheduler Executor Cleanup — ✅ OK
```python
def run():
    scheduler = AsyncIOScheduler(
        executors={
            'default': AsyncIOExecutor(),
            'threadpool': ThreadPoolExecutor(max_workers=5),
        }
    )
    scheduler.start()
    try:
        asyncio.run(...)
    finally:
        scheduler.shutdown(wait=True)  # ✅ Shutdown threads
```

---

### 11. Blocking Code in Async Context — 🟡 MEDIUM

#### 11.1 `tools/app/scheduler.py:108–136` (Blocking Sleep)
```python
elapsed = 0
logger.info("[recording] Waiting %d min before first poll...", RECORDING_INITIAL_DELAY // 60)
time.sleep(RECORDING_INITIAL_DELAY)  # ← BLOCKS for 15 minutes
elapsed += RECORDING_INITIAL_DELAY

while elapsed < RECORDING_POLL_TIMEOUT:
    recordings = zm.get_meeting_recordings(meeting_id)  # ← Sync API call
    # ...
    time.sleep(RECORDING_POLL_INTERVAL)  # ← BLOCKS for 5 minutes
```

**Context**: Called from `server.py:_handle_meeting_ended()` via:
```python
background_tasks.add_task(_run_and_cleanup)
```

**Issue**: This is a background task, so blocking is less critical, but:
- Ties up a thread pool worker for 15min (first sleep) + 5min × N retries
- If multiple lectures end simultaneously, thread pool can be exhausted

**Concern**: Low priority since the scheduler has `ThreadPoolExecutor(max_workers=5)`.
**Recommendation**: For future, consider `asyncio.sleep()` instead.

---

### 12. Error Handling in Analytics/Reporting — Mixed

#### 12.1 `tools/services/analytics.py:186`
```python
def get_dashboard_data():
    try:
        # Fetch data from multiple sources
        lectures = list(Path("data").glob("*.json"))
        for f in lectures:
            # ... process ...
    except Exception:
        pass  # ← SILENT
    return {...}  # Returns empty/partial dict
```

**Issue**: If file processing fails, returns empty dashboard with no error indication.
**Impact**: User sees blank dashboard, no hint that something is wrong.

**Fix**:
```python
except Exception as e:
    logger.error("Dashboard data fetch failed: %s", e, exc_info=True)
    # Return empty dashboard with "Error" banner
    return {
        "error": str(e),
        "groups": {},
        ...
    }
```

---

#### 12.2 `tools/services/analytics.py:816`
```python
def sync_from_pinecone():
    try:
        # ... Pinecone query ...
    except OSError as e:
        logger.error("Pinecone sync failed: %s", e)
        # Silent return — dashboard shows stale data
        return
```

**Status**: Logs error, but no alert to operator. If Pinecone is consistently down, operator won't know.

---

## Summary Table

| Issue | Severity | Count | File(s) | Status |
|-------|----------|-------|---------|--------|
| Silent failures (bare `except Exception: pass`) | 🔴 Critical | 2 | analytics.py:186 | FIX REQUIRED |
| No timeout on Gemini polling | 🔴 Critical | 1 | gemini_analyzer.py | FIX REQUIRED |
| Missing `finally` (resource cleanup) | 🔴 Critical | 2 | gdrive_manager.py:232, scheduler.py:228 | FIX REQUIRED |
| No timeout on Drive API calls | 🔴 Critical | 1 | gdrive_manager.py | FIX REQUIRED |
| Broad exception catch (missing exc_info) | 🟡 Medium | 5 | retry.py, server.py, others | IMPROVE |
| Blocking sleep in async context | 🟡 Medium | 1 | scheduler.py | LOW PRIORITY |
| Silent Pinecone failures | 🟡 Medium | 1 | knowledge_indexer.py | IMPROVE |
| Config file load returns empty dict | 🟡 Medium | 1 | config.py:50 | IMPROVE |
| Retry logic lacks jitter | 🟡 Medium | 1 | retry.py | NICE-TO-HAVE |

---

## Recommendations (Priority Order)

### P0 — Fix Immediately
1. **Gemini Polling Timeout** (gemini_analyzer.py:540–580)
   - Add elapsed timer to avoid infinite loops
   - Use `FILE_POLL_TIMEOUT` constant (currently unused)

2. **Drive API Timeout** (gdrive_manager.py)
   - Wrap service calls in `asyncio.wait_for()` or timeout decorator
   - Apply to all resumable upload/download operations

3. **Silent Failures** (analytics.py:186, others)
   - Remove bare `except Exception: pass`
   - Always log at least `logger.error(..., exc_info=True)`
   - Return `None` or error dict, not silently continue

4. **ffmpeg Cleanup** (scheduler.py:228–246)
   - Wrap `concat_list.write_text()` and `subprocess.run()` in try/finally
   - Ensure temp file is deleted even if command fails

### P1 — Improve Error Handling
5. **Stack Traces** (retry.py:129, others)
   - Change `logger.error("... %s", exc)` to `logger.error(..., exc_info=True)`
   - Helps debugging when alerts reach Tornike

6. **Pinecone Failure Alerts** (knowledge_indexer.py:344)
   - Add `alert_operator()` call when indexing fails
   - At minimum, log at CRITICAL level

7. **Config Load Errors** (config.py:50)
   - Return `None` instead of `{}`
   - Check at call sites: `if not creds: raise RuntimeError(...)`

### P2 — Nice-to-Have
8. **Retry Jitter** (retry.py:59)
   - Add `random.uniform(0.8, 1.2) * delay` to avoid thundering herd
   - Only needed for distributed systems (n8n has multiple workers)

9. **Async Sleep** (scheduler.py)
   - Replace `time.sleep()` with `asyncio.sleep()` if this becomes async
   - Not critical since scheduler has dedicated thread pool

---

## Code Snippets for Quick Fixes

### Fix 1: Add Timeout to Gemini Polling
```python
# gemini_analyzer.py, line 540+
elapsed = 0
while elapsed < FILE_POLL_TIMEOUT:  # Add this constant
    file = client.files.get(name=file.name)
    if file.state == STATE_ACTIVE:
        time.sleep(FILE_POLL_INTERVAL)
        elapsed += FILE_POLL_INTERVAL
    elif file.state == STATE_FAILED:
        raise RuntimeError("File processing failed")
    else:
        break

if elapsed >= FILE_POLL_TIMEOUT:
    raise TimeoutError(f"Gemini file processing timed out after {FILE_POLL_TIMEOUT}s")
```

### Fix 2: Wrap ffmpeg Cleanup in Finally
```python
# scheduler.py, line 213+
def _concatenate_segments(segment_paths: list[Path], output_path: Path) -> None:
    concat_list = output_path.parent / f"{output_path.stem}_segments.txt"
    try:
        concat_list.write_text(...)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info("[post] Concatenation complete: %.1f MB", size_mb)
    finally:
        concat_list.unlink(missing_ok=True)
```

### Fix 3: Add exc_info to Error Logs
```python
# retry.py, line 129+
except Exception as exc:
    logger.error("Failed to %s: %s", operation_name, exc, exc_info=True)  # Add exc_info=True
```

---

## Testing Recommendations

1. **Test Timeouts**: Artificially delay Gemini responses, verify timeout is triggered
2. **Test Silent Failures**: Mock Drive API failures, verify logs and alerts
3. **Test Cleanup**: Kill process mid-ffmpeg, verify concat_list is deleted
4. **Load Test**: Simulate multiple lectures ending simultaneously, verify thread pool doesn't exhaust

---

## Conclusion

The Training Agent has **good retry logic and alerting fundamentals**, but **critical gaps in timeout handling and resource cleanup** that could cause hung pipelines and silent failures in production. The **P0 fixes are essential** before the next deployment.

**Action**: Assign P0 items to be fixed before the March 19 lecture.
