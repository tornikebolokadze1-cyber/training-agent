# Logging & Monitoring Audit — Training Agent

**Date**: 2026-03-18
**Scope**: Production-grade observability assessment
**Status**: ⚠️ **Partially Implemented** — Good foundation, significant gaps

---

## Executive Summary

The Training Agent has **solid core logging infrastructure** (structured JSON, correlation IDs, proper level usage) but **lacks production-grade monitoring**:

### Strengths ✅
- JSON structured logging configured for Railway
- Correlation IDs on all HTTP requests
- Proper log levels (DEBUG/INFO/WARNING/ERROR/CRITICAL)
- Rotating file handler locally (10MB × 5 backups)
- In-flight task deduplication + stale task eviction (4h timeout)
- Alert mechanism (`alert_operator`) for critical failures

### Critical Gaps ❌
- **No distributed tracing** — single correlation ID insufficient for multi-service flows
- **No metrics/instrumentation** — pipeline timing, API quotas, error rates unmeasurable
- **Health checks are shallow** — don't validate external service availability (Zoom, Drive, Pinecone)
- **Sensitive data partially exposed** — API tokens logged in error messages
- **No request/response logging middleware** — API calls invisible to observability
- **No performance metrics endpoint** — can't query latency/throughput from outside
- **Missing audit trail** — no record of API mutations (Drive doc creation, Pinecone upserts)

---

## 1. Logging Configuration

### Current State
**File**: `tools/core/logging_config.py`

✅ **Strengths**:
- **JSONFormatter** emits structured logs (timestamp, level, logger, message, exception, correlation_id)
- **Environment detection**: Local → human-readable format; Railway (RAILWAY_ENVIRONMENT env var) → JSON lines
- **Rotating file handler**: 10MB per file, 5 backups locally (10 files = ~50MB max)
- **Suppressions for noisy loggers**: apscheduler, httpx, httpcore, uvicorn.access

❌ **Issues**:
- Log level hardcoded to `INFO` (no DEBUG in production)
- No log sampling (could flood in high-volume scenarios)
- File rotation only local (Railway captures stdout, but no structured file fallback)
- No custom fields beyond correlation_id (could add: request_duration, memory_usage, task_id)

### Recommendation
```python
# Add to JSONFormatter for production observability
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": ...,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # NEW:
            "service": "training-agent",
            "environment": "railway" if os.getenv("RAILWAY_ENVIRONMENT") else "local",
            "version": "1.0.0",  # from pyproject.toml
        }
        # Extract structured context if present
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        if hasattr(record, "duration_ms"):
            log_entry["duration_ms"] = record.duration_ms
        if hasattr(record, "group_id"):
            log_entry["group_id"] = record.group_id
        # ... etc
```

---

## 2. Logging Usage Across Codebase

### Coverage Analysis
**291 logger calls** across 14 files. Breakdown:

| Module | Calls | Coverage |
|--------|-------|----------|
| `server.py` | 46 | High — webhook validation, background tasks |
| `gemini_analyzer.py` | 38 | High — transcription + API calls |
| `transcribe_lecture.py` | 26 | High — pipeline lifecycle |
| `analytics.py` | 18 | Medium — Drive operations |
| `knowledge_indexer.py` | 19 | Medium — Pinecone operations |
| `zoom_manager.py` | 17 | Medium — Zoom OAuth + polling |
| `whatsapp_sender.py` | 9 | **Low** — only 9 calls in 500+ line file |
| `whatsapp_assistant.py` | 17 | Medium |
| `scheduler.py` | 48 | High |
| `gdrive_manager.py` | 16 | Medium |
| Others | 41 | Medium-High |

### Issue 1: Sensitive Data Exposure

❌ **Problem**: API tokens/keys logged in error messages.

Examples:
```python
# ❌ BAD (in retry.py or error handlers):
logger.error("API call failed: %s", str(exception))  # May include token in traceback
logger.error("Zoom error: %s", response.text)  # Could contain auth info

# ✅ GOOD:
logger.error("Zoom API error (status=%d)", response.status_code)
logger.debug("Detailed error: %s", response.text if is_debug_mode else "redacted")
```

**Locations**:
- `whatsapp_sender.py:94` — logs raw HTTP response
- `gemini_analyzer.py` — error handling for Gemini quota errors
- `zoom_manager.py` — token operations logged at INFO level
- `server.py:313-314` — traceback includes request details (could expose paths)

**Fix**:
```python
def redact_sensitive(text: str) -> str:
    """Remove API keys, tokens, phone numbers from text."""
    import re
    redactions = [
        (r'Bearer\s+[a-zA-Z0-9\-_.]+', 'Bearer [REDACTED]'),
        (r'api[_-]key["\']?\s*[:=]\s*["\'][^"\']+["\']', 'api_key=[REDACTED]'),
        (r'\b\d{10,12}\b', '[PHONE]'),  # phone numbers
    ]
    result = text
    for pattern, replacement in redactions:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result

# In error handlers:
logger.error("API error: %s", redact_sensitive(str(error)))
```

### Issue 2: Incomplete API Instrumentation

❌ **Problem**: API calls lack start/duration/bytes logged. Can't measure:
- Which APIs are slow?
- How many retries are happening?
- How much data is being transferred?

**Missing instrumentation**:
- Zoom recording download → no transfer speed, chunk count
- Gemini transcription → no input token count, output length
- Google Drive uploads → no chunk count, resumable offset tracking
- Pinecone upserts → no vector count, batch size details

**Fix**:
```python
@contextmanager
def log_api_call(name: str, **context):
    """Context manager: logs API call with timing + success/failure."""
    import time, logging
    logger = logging.getLogger(__name__)
    start = time.monotonic()
    try:
        logger.info(f"[API] Starting {name}", extra=context)
        yield
    except Exception as e:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        logger.error(
            f"[API] {name} failed after {elapsed_ms}ms: {redact_sensitive(str(e))}",
            extra={**context, "duration_ms": elapsed_ms, "status": "error"}
        )
        raise
    else:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        logger.info(
            f"[API] {name} succeeded in {elapsed_ms}ms",
            extra={**context, "duration_ms": elapsed_ms, "status": "success"}
        )

# Usage:
with log_api_call("gemini_transcription", group=1, lecture=1, video_size_mb=2500):
    result = transcribe_with_gemini(video_path)
```

### Issue 3: Correlation Capability

✅ **Good**: Correlation ID generated per HTTP request (server.py:154)
❌ **Gap**: Correlation ID not propagated to:
- Background tasks (APScheduler jobs have no correlation ID)
- Async operations spawned from FastAPI
- n8n callbacks (no correlation header sent back)

**Fix**:
```python
# In server.py background task setup:
@app.post("/process-recording")
async def process_recording(...):
    correlation_id = request.state.correlation_id
    # Pass through to background task
    background_tasks.add_task(
        process_recording_task,
        payload,
        correlation_id=correlation_id
    )

# In scheduler.py:
def pre_meeting_job(group_number: int):
    correlation_id = str(uuid.uuid4())[:8]
    logger.info("[%s] Pre-meeting job started", correlation_id, extra={
        "correlation_id": correlation_id,
        "group": group_number,
    })
```

---

## 3. Health Checks

### Current State
**Endpoints**: `/health`, `/status`

#### `/health` (server.py:417-450)
✅ **Good**:
- Checks tmp dir is writable
- Reports env var presence (webhook_secret, n8n_callback)
- Reports in-flight task count
- Returns 200/503 based on overall health

❌ **Incomplete**:
```python
checks: dict[str, str] = {}

# Only checks:
- tmp_dir writable (filesystem only)
- webhook_secret configured
- n8n_callback configured
- tasks_in_progress count

# Missing external service checks:
- Zoom API reachable + token valid? ❌
- Google Drive API reachable? ❌
- Gemini API reachable + quota OK? ❌
- Pinecone index reachable? ❌
- Green API reachable? ❌
```

#### `/status` (orchestrator.py:104-168)
✅ **Good**:
- Uptime, scheduler state
- Next scheduled job times
- Recent execution results

❌ **Incomplete**:
- Last execution results array never populated (set in startup, never appended)
- No error rate tracking
- No resource usage (memory, CPU)

### Recommendation: Production Health Check

```python
# tools/integrations/health_check.py
import asyncio
from typing import dict, str

class HealthChecker:
    """Check external service availability."""

    @staticmethod
    async def check_zoom() -> dict[str, str]:
        """Verify Zoom OAuth works."""
        try:
            token = await asyncio.to_thread(get_access_token)
            return {"status": "ok", "has_token": "yes"}
        except Exception as e:
            return {"status": "error", "error": str(type(e).__name__)}

    @staticmethod
    async def check_gdrive() -> dict[str, str]:
        """Verify Google Drive API works."""
        try:
            service = await asyncio.to_thread(get_drive_service)
            # Simple list operation
            await asyncio.to_thread(service.files().list, q="trashed=false", pageSize=1)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "error": str(type(e).__name__)}

    @staticmethod
    async def check_gemini() -> dict[str, str]:
        """Verify Gemini API works."""
        try:
            client = _get_client()
            # Do a lightweight operation
            result = await asyncio.to_thread(
                lambda: client.models.get("models/gemini-2.5-pro")
            )
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "error": str(type(e).__name__)}

    @staticmethod
    async def check_pinecone() -> dict[str, str]:
        """Verify Pinecone index is reachable."""
        try:
            index = await asyncio.to_thread(get_pinecone_index)
            await asyncio.to_thread(index.describe_index_stats)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "error": str(type(e).__name__)}

    @staticmethod
    async def check_whatsapp() -> dict[str, str]:
        """Verify Green API is reachable."""
        try:
            # Just check the API endpoint is alive
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE_ID}/getSettings/{GREEN_API_TOKEN}",
                    timeout=5
                )
                return {"status": "ok" if resp.status_code < 500 else "error"}
        except Exception as e:
            return {"status": "error", "error": str(type(e).__name__)}

# In server.py:
@app.get("/health/deep")
@limiter.limit("10/minute")
async def health_check_deep(request: Request, authorization: str | None = Header(None)):
    """Full health check including external services."""
    verify_webhook_secret(authorization)

    checker = HealthChecker()
    results = {
        "timestamp": datetime.now().isoformat(),
        "local": {
            "tmp_dir": "ok",  # existing check
            "webhook_secret": "configured" if WEBHOOK_SECRET else "MISSING",
            "tasks_in_progress": len(_processing_tasks),
        },
        "external": {
            "zoom": await checker.check_zoom(),
            "gdrive": await checker.check_gdrive(),
            "gemini": await checker.check_gemini(),
            "pinecone": await checker.check_pinecone(),
            "whatsapp": await checker.check_whatsapp(),
        },
    }

    # Overall health: OK if all are ok, degraded if some error, critical if >50% fail
    external_status = [v.get("status", "error") for v in results["external"].values()]
    error_count = sum(1 for s in external_status if s != "ok")

    if error_count == 0:
        overall = "healthy"
        code = 200
    elif error_count < len(external_status) / 2:
        overall = "degraded"
        code = 200
    else:
        overall = "critical"
        code = 503

    results["overall"] = overall
    return JSONResponse(content=results, status_code=code)
```

---

## 4. Metrics & Instrumentation

### Current State
❌ **No metrics collection** — everything is logged, nothing is quantified.

**Unmeasurable**:
1. **Pipeline timing**: How long does transcription take? Gap analysis? Drive upload?
2. **API quota usage**: How close to Gemini/Zoom/Drive quotas are we?
3. **Error rates**: What % of lectures fail at each stage?
4. **Throughput**: How many lectures/week are we processing?
5. **Resource usage**: Memory growth over time?

### Recommendation: Prometheus Metrics

```python
# tools/services/metrics.py
from prometheus_client import Counter, Histogram, Gauge
from contextlib import contextmanager
import time

# Counters
processing_total = Counter(
    "training_agent_processing_total",
    "Total lectures processed",
    ["group", "status"],  # labels: success, failed
)

api_calls_total = Counter(
    "training_agent_api_calls_total",
    "Total API calls",
    ["service", "method", "status"],  # service: zoom, drive, gemini, etc
)

# Histograms (timing + size)
pipeline_duration_seconds = Histogram(
    "training_agent_pipeline_duration_seconds",
    "Lecture processing pipeline duration",
    ["group", "stage"],  # stage: transcription, analysis, upload
    buckets=[10, 30, 60, 300, 600, 1800],  # 10s to 30min
)

api_response_size_bytes = Histogram(
    "training_agent_api_response_bytes",
    "API response size",
    ["service"],
    buckets=[1e3, 1e4, 1e5, 1e6, 1e7],  # 1KB to 10MB
)

# Gauges (instantaneous)
tasks_in_flight = Gauge(
    "training_agent_tasks_in_flight",
    "Currently processing lectures",
    ["group"],
)

@contextmanager
def track_pipeline_stage(group: int, stage: str):
    """Context manager: track timing for a pipeline stage."""
    start = time.monotonic()
    try:
        yield
        elapsed = time.monotonic() - start
        pipeline_duration_seconds.labels(group=group, stage=stage).observe(elapsed)
    except Exception:
        raise

# Usage:
with track_pipeline_stage(group=1, stage="transcription"):
    result = transcribe_with_gemini(video_path)
```

**Metrics endpoint**:
```python
@app.get("/metrics")
@limiter.limit("60/minute")
async def prometheus_metrics(request: Request, authorization: str | None = Header(None)):
    """Expose Prometheus metrics (operator-only)."""
    verify_webhook_secret(authorization)
    from prometheus_client import generate_latest, REGISTRY
    return Response(
        content=generate_latest(REGISTRY),
        media_type="text/plain; version=0.0.4",
    )
```

---

## 5. Alerting

### Current State

**Mechanism**: `alert_operator()` in `whatsapp_sender.py:114+`

✅ **Strengths**:
- WhatsApp-based (Tornike checks it immediately)
- Wraps all critical failures (stale task eviction, pipeline failures, callback failures)
- Last-resort channel (not reliant on email/Slack)

❌ **Issues**:
```
# Called in:
- server.py:84 (stale task eviction)
- server.py:325 (pipeline failed)
- server.py:401, 407 (n8n callback failed)
- server.py:530 (WhatsApp assistant crashed)
- scheduler.py (various pipeline stages)

# Problem 1: Alert fatigue — no deduplication
If Gemini quota is exhausted, EVERY pipeline fails → 30+ identical alerts/day
```

**Problem 2: Non-actionable alerts**
```
# Example:
"Pipeline FAILED for Group 1, Lecture #5. Error: Connection timeout"

# Missing context:
- How many retries? (if 3, don't auto-retry)
- Retry backoff? (is next attempt in 30s or 5min?)
- Which stage failed? (transcription vs. upload?)
- Estimated recovery time? (is Drive down? Zoom quota?)
```

**Problem 3: No alert window**
```
# What if Zoom is down for 30min? Current behavior:
- 18:00 — alert_operator("Zoom API down")
- 18:05 — alert_operator("Zoom API down") [duplicate]
- 18:10 — alert_operator("Zoom API down") [duplicate]
- ... repeat every 5 minutes for 30 minutes = 6 identical alerts
```

### Recommendation: Smart Alerting

```python
# tools/core/alerts.py
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import json

class AlertSeverity(Enum):
    INFO = "info"          # FYI, doesn't need action
    WARNING = "warning"    # Degraded, monitor closely
    ERROR = "error"        # Failed, investigate
    CRITICAL = "critical"  # System unusable, immediate action needed

@dataclass
class Alert:
    severity: AlertSeverity
    service: str  # "zoom", "gemini", "drive", etc.
    title: str
    message: str
    context: dict  # group, lecture, retry_count, etc.
    timestamp: datetime = None

    def __post_init__(self):
        self.timestamp = self.timestamp or datetime.now()

class AlertBuffer:
    """Deduplicates and batches alerts to avoid spamming."""

    def __init__(self, window_minutes: int = 5):
        self.window = timedelta(minutes=window_minutes)
        self.alerts: dict[str, Alert] = {}  # key: f"{service}:{title}"
        self.last_sent: dict[str, datetime] = {}

    def add(self, alert: Alert) -> bool:
        """Add alert to buffer. Returns True if should be sent immediately."""
        key = f"{alert.service}:{alert.title}"

        # Check if we've recently sent identical alert
        last = self.last_sent.get(key)
        if last and (datetime.now() - last) < self.window:
            # Same alert within window — just update, don't send
            self.alerts[key] = alert
            return False

        # New alert or window expired — send it
        self.alerts[key] = alert
        self.last_sent[key] = datetime.now()
        return True

_alert_buffer = AlertBuffer(window_minutes=5)

async def send_alert(alert: Alert) -> None:
    """Queue alert, respecting deduplication window."""
    if not _alert_buffer.add(alert):
        return  # Recently sent identical alert

    # Format message
    if alert.severity == AlertSeverity.INFO:
        icon = "ℹ️"
    elif alert.severity == AlertSeverity.WARNING:
        icon = "⚠️"
    elif alert.severity == AlertSeverity.ERROR:
        icon = "❌"
    else:
        icon = "🚨"

    message = f"""{icon} {alert.severity.value.upper()}: {alert.title}

{alert.message}

Context:
{json.dumps(alert.context, indent=2, default=str)}

Timestamp: {alert.timestamp.isoformat()}
"""

    await asyncio.to_thread(alert_operator, message)

# Usage in server.py:
except Exception as e:
    await send_alert(Alert(
        severity=AlertSeverity.ERROR,
        service="gemini",
        title="Transcription failed",
        message=f"Group {group}, Lecture #{lecture}: {str(e)[:100]}",
        context={
            "group": group,
            "lecture": lecture,
            "error_type": type(e).__name__,
            "retry_count": attempt,
            "next_retry_in_seconds": 30,
        }
    ))
```

---

## 6. Missing Observability Features

### 6.1 Distributed Tracing

❌ **Gap**: Single correlation ID insufficient for multi-service flows.

**Example flow**:
1. Zoom webhook → server.py (correlation_id = "abc123")
2. Background task spawned (no correlation_id)
3. Calls transcribe_lecture.py (no correlation_id)
4. Calls gdrive_manager.py (no correlation_id)
5. Calls pinecone (no correlation_id)

**All logs are separate** — can't reconstruct flow from log aggregation.

**Solution**: Trace context propagation
```python
# tools/core/tracing.py
import contextvars

trace_context = contextvars.ContextVar('trace_context', default=None)

def set_trace_context(trace_id: str, span_id: str = None, parent_span: str = None):
    """Set current trace context."""
    trace_context.set({
        "trace_id": trace_id,
        "span_id": span_id or str(uuid.uuid4())[:8],
        "parent_span": parent_span,
    })

def get_trace_context() -> dict | None:
    """Get current trace context."""
    return trace_context.get()

# In JSONFormatter:
def format(self, record: logging.LogRecord) -> str:
    log_entry = {...}
    ctx = get_trace_context()
    if ctx:
        log_entry.update(ctx)
    return json.dumps(log_entry)

# In server.py background task:
async def process_recording_task(payload, correlation_id=None):
    trace_id = correlation_id or str(uuid.uuid4())[:8]
    set_trace_context(trace_id)
    # All logs now include trace_id automatically
```

### 6.2 Request/Response Logging

❌ **Gap**: HTTP calls to external APIs are invisible.

**Currently logged**:
- ✅ High-level success/failure
- ❌ Request headers (auth redacted)
- ❌ Request body size
- ❌ Response status + headers
- ❌ Response body (sampled, first 200 chars)

**Solution**:
```python
# Wrap httpx client
import logging
import httpx

class LoggingHTTPClient(httpx.Client):
    def request(self, method, url, **kwargs):
        logger = logging.getLogger(__name__)
        logger.debug(
            f"[HTTP] {method} {url}",
            extra={"request_size": len(str(kwargs.get('json', {})))}
        )
        response = super().request(method, url, **kwargs)
        logger.debug(
            f"[HTTP] {response.status_code} (size={response.headers.get('content-length', '?')})"
        )
        return response
```

### 6.3 Performance Metrics Endpoint

❌ **Gap**: No `/metrics` endpoint for external monitoring (Prometheus, DataDog, New Relic).

**Solution**: Add metrics endpoint (see section 4 above).

### 6.4 Audit Trail

❌ **Gap**: No record of API mutations:
- Which user/operator created a Drive doc?
- Which lecture triggered Pinecone upsert?
- What changes were sent back to n8n?

**Solution**:
```python
# tools/services/audit_log.py
import sqlite3
from datetime import datetime

class AuditLog:
    def __init__(self, db_path: str = ".tmp/audit.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY,
                    timestamp TEXT,
                    service TEXT,
                    action TEXT,
                    resource_id TEXT,
                    details JSON,
                    status TEXT
                )
            """)
            conn.commit()

    def log_action(self, service: str, action: str, resource_id: str,
                   details: dict, status: str = "success"):
        """Record an API mutation."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO audit_log (timestamp, service, action, resource_id, details, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                service,
                action,
                resource_id,
                json.dumps(details),
                status
            ))
            conn.commit()

# Usage:
audit = AuditLog()
audit.log_action(
    service="gdrive",
    action="create_doc",
    resource_id=doc_id,
    details={"group": 1, "lecture": 5, "title": "ლექცია #5 — შეჯამება"},
    status="success"
)

# Endpoint:
@app.get("/api/audit-log")
async def get_audit_log(...):
    """Return recent audit trail (operator-only)."""
    verify_webhook_secret(authorization)
    audit = AuditLog()
    rows = audit.query_recent(limit=100)
    return {"audit": rows}
```

---

## 7. Implementation Roadmap

### Phase 1: Immediate (Week 1)
**Priority**: Prevent alert fatigue + fix sensitive data leaks

1. Add `redact_sensitive()` function to all error logging
2. Implement AlertBuffer deduplication (5-min window)
3. Add Severity enum to alerts
4. Log all API call start/end with duration

**Est. effort**: 4-6 hours

### Phase 2: Short-term (Week 2-3)
**Priority**: Make pipelines observable

1. Add correlation ID propagation to background tasks
2. Implement `/health/deep` endpoint with external service checks
3. Add Prometheus metrics (Counter + Histogram)
4. Add `/metrics` endpoint
5. Populate `/status` endpoint's execution_results array

**Est. effort**: 12-16 hours

### Phase 3: Medium-term (Month 2)
**Priority**: Production-grade observability

1. Implement distributed tracing via contextvars
2. Add HTTPClient logging wrapper
3. Implement AuditLog for API mutations
4. Set up log aggregation (e.g., Loki on Railway)
5. Set up metrics scraping (e.g., Prometheus)

**Est. effort**: 20-24 hours

### Phase 4: Long-term (Month 3+)
**Priority**: Predictive analytics + cost control

1. Cost tracking per API (Gemini tokens, Drive storage, Pinecone vectors)
2. Quota usage forecasting (will we hit Zoom API limit this month?)
3. Pipeline failure prediction (if X happens, expect Y% failure rate)
4. Slow-query tracking (which lectures take >2 hours to process?)

**Est. effort**: Ongoing

---

## 8. Code Quality Issues

### Issue 1: Noisy Logs
Some logs are too frequent and low-value:
```python
# In scheduler.py (every 5 minutes):
logger.debug("check_recording_ready: polling...")  # 288 logs/day per group

# Should be:
logger.debug("polling", extra={"poll_count": attempt, "next_poll_in": 300})
```

### Issue 2: Missing Structured Logging
Avoid string interpolation; use structured fields:
```python
# ❌ BAD:
logger.info(f"Group {group}, Lecture #{lecture}: {status}")

# ✅ GOOD:
logger.info("Lecture processing complete", extra={
    "group": group,
    "lecture": lecture,
    "status": status,
    "duration_ms": elapsed_ms,
})
```

### Issue 3: Exception Context Lost
Some errors don't include the original exception:
```python
# ❌ BAD:
except Exception as e:
    logger.error("Failed: %s", str(e))

# ✅ GOOD:
except Exception as e:
    logger.error("Failed", exc_info=True)  # includes traceback
    # OR:
    logger.error("Failed: %s", str(e), exc_info=True)
```

---

## 9. Testing Observability

### Current Test Coverage
✅ Tests exist for:
- Config loading
- Zoom OAuth
- Drive operations
- Gemini analyzer
- Whatsapp sender

❌ Missing:
- Logging output validation
- Metric collection validation
- Health check responses
- Alert deduplication

### Recommended Tests

```python
# tools/tests/test_observability.py
import logging
from unittest.mock import patch

def test_correlation_id_propagated(client):
    """Verify correlation ID is included in responses."""
    response = client.post("/process-recording", ...)
    assert "X-Correlation-ID" in response.headers

def test_sensitive_data_redacted(caplog):
    """Verify API tokens are redacted from logs."""
    caplog.set_level(logging.DEBUG)
    # Trigger an error with token in message
    ...
    assert "Bearer" not in caplog.text
    assert "[REDACTED]" in caplog.text

def test_alert_deduplication():
    """Verify duplicate alerts are not sent within window."""
    buffer = AlertBuffer(window_minutes=5)
    alert1 = Alert(service="zoom", title="API down", ...)
    assert buffer.add(alert1) == True
    assert buffer.add(alert1) == False  # Within window
```

---

## 10. Summary Table

| Feature | Status | Priority | Effort (hrs) |
|---------|--------|----------|-------------|
| Logging config | ✅ Good | — | — |
| Correlation IDs | ⚠️ Partial | High | 4 |
| Sensitive data redaction | ❌ Missing | High | 3 |
| Health checks (shallow) | ✅ OK | — | — |
| Health checks (deep) | ❌ Missing | High | 6 |
| Metrics collection | ❌ Missing | High | 8 |
| Distributed tracing | ❌ Missing | Medium | 6 |
| Request/response logging | ❌ Missing | Medium | 4 |
| Alert deduplication | ❌ Missing | Critical | 3 |
| Audit trail | ❌ Missing | Low | 8 |
| Testing observability | ⚠️ Partial | Medium | 6 |

**Total estimated effort to production-grade**: ~52 hours over 4 weeks

---

## References

- [12-Factor App: Logs](https://12factor.net/logs)
- [Observability Engineering](https://www.oreilly.com/library/view/observability-engineering/9781492076438/)
- [Prometheus Best Practices](https://prometheus.io/docs/practices/instrumentation/)
- [Structured Logging](https://www.kartar.net/2015/12/structured-logging/)
