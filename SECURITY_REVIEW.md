# Security Review: Training Agent Authentication & Authorization Flows

**Date:** 2026-03-18
**Scope:** All authentication mechanisms, token lifecycle management, webhook validation, and credential handling
**Reviewer:** Security Team
**Overall Risk Level:** **MEDIUM** (mostly good practices with a few improvement areas)

---

## Executive Summary

The Training Agent implements a hybrid authentication system across multiple services (Zoom S2S OAuth, Google OAuth2, Pinecone API key, WhatsApp Green API, custom webhook secrets). The codebase demonstrates solid security fundamentals:

✅ **Strengths:**
- Thread-safe token caching with explicit locking (Zoom)
- HMAC-SHA256 signature verification with constant-time comparison
- Fail-closed webhook authentication (rejects if WEBHOOK_SECRET unset)
- SSRF protection on Zoom download URLs
- Proper Railway credential materialization from base64 env vars
- Input validation on folder IDs and group/lecture numbers

⚠️ **Areas for Improvement:**
- Race condition potential in Pinecone API key handling (in-memory only, no validation)
- Token refresh flow on Railway lacks explicit refresh validation before use
- Rate limiting uses IP-based key_func which can be bypassed in reverse proxy scenarios
- Webhook timestamp validation has a 5-minute window (acceptable but could be tighter)
- Missing HSTS header for HTTPS enforcement
- No explicit memory clearing of sensitive data after use

---

## 1. Zoom S2S OAuth (tools/integrations/zoom_manager.py)

### Token Caching Strategy

**Status:** ✅ **GOOD** — Thread-safe with proper locking

**Implementation:**
- In-memory token cache: `_token_cache: dict[str, Any] = {}`
- Global threading lock: `_token_lock = threading.Lock()`
- **Cache hit logic (lines 93–98):**
  ```python
  with _token_lock:
      if _token_cache.get("access_token") and time.time() < _token_cache.get("expires_at", 0.0) - 60:
          return _token_cache["access_token"]
  ```

**Strengths:**
1. Lock acquired before ALL cache operations (read + write)
2. 60-second safety margin before expiry prevents token age issues
3. Cache miss forces token refresh, avoiding stale token use

**Weaknesses:**
- Tokens stored in plaintext in memory (acceptable for in-process use, but unencrypted)
- Cache never explicitly cleared between deployments (memory only, so reset on restart)
- No cache size limit (single token, not an issue)

**Recommendation:** Add explicit cleanup on shutdown/error:
```python
def clear_token_cache() -> None:
    """Explicitly clear cached tokens on shutdown or error recovery."""
    with _token_lock:
        _token_cache.clear()
        logger.info("Token cache cleared")
```

---

### Token Expiration Handling

**Status:** ✅ **GOOD** — Handles 401 responses correctly, no race conditions

**Implementation (lines 208–214):**
```python
if response.status_code == 401:
    logger.warning("Received 401; clearing token cache and retrying.")
    with _token_lock:
        _token_cache.clear()
    if attempt < MAX_RETRIES:
        time.sleep(RETRY_BACKOFF_BASE**attempt)
        continue
```

**Strengths:**
1. Detects invalidated tokens (401 status)
2. Clears cache under lock before retry
3. Exponential backoff prevents thundering herd
4. Fresh token acquired on next `get_access_token()` call

**Weaknesses:**
- No explicit error code discrimination (e.g., 403 "insufficient scope" vs 401 "expired token")
- Retry loop retries all HTTP errors (could retry non-idempotent operations)

**Recommendation:** Add scope validation at startup:
```python
def validate_token_scopes(token: str) -> bool:
    """Verify token has required scopes by making a test API call."""
    try:
        response = client.post(ZOOM_API_BASE + "/users/me", headers={...})
        return response.status_code in (200, 401)  # 401 = bad token, not bad scopes
    except:
        return False  # Network error, defer validation
```

---

### Scope Validation

**Status:** ⚠️ **MINIMUM** — No explicit scope validation

**Current Implementation:**
- Scopes hardcoded in code: `account_credentials` grant type (lines 108–111)
- Zoom account-level credentials grant these scopes automatically
- No runtime scope verification

**Recommendation:** Add explicit scope assertion:
```python
# In tools/core/config.py
ZOOM_REQUIRED_SCOPES = {
    "meeting:write",      # create_meeting
    "recording:read",     # get_meeting_recordings + download
}

# In zoom_manager.py
def verify_token_has_scopes(token: str) -> bool:
    """Verify token supports required operations (best-effort)."""
    try:
        response = client.get(
            f"{ZOOM_API_BASE}/users/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        # Zoom doesn't expose scopes in user endpoint, so this is a connectivity check
        return response.status_code == 200
    except:
        return False  # Assume valid if we can't verify (safe fail)
```

---

## 2. Google OAuth2 (tools/integrations/gdrive_manager.py)

### Refresh Token Flow

**Status:** ⚠️ **ACCEPTABLE WITH CAUTION** — Works, but Railway behavior differs from local

**Implementation (lines 49–101):**

**Local flow:**
1. Load credentials from `token.json`
2. If expired and refresh_token exists → refresh in-memory, write back to disk
3. If no refresh_token → prompt for browser re-authorization

**Railway flow:**
1. Load credentials from `GOOGLE_TOKEN_JSON_B64` env var
2. If expired and refresh_token exists → refresh in-memory ONLY (no disk write)
3. If no refresh_token → raise error with manual re-auth instructions

**Strengths:**
1. Refresh token kept in memory during Railway session
2. Prevents errors from attempting disk writes on ephemeral filesystem
3. Refresh error handling is explicit

**Weaknesses:**
1. **No validation that refresh succeeded** before returning credentials (line 67 assumes `creds.refresh()` succeeds silently)
2. **Refresh can fail silently if invalid token JSON in env var** (caught at creation, not refresh)
3. Refresh failure could be masked by exception handling

**Example vulnerability:**
```python
if creds and creds.expired and creds.refresh_token:
    creds.refresh(Request())  # ← If this raises, exception isn't caught
    # Caller gets expired creds
```

**Recommendation:** Add explicit refresh validation:
```python
def _get_credentials() -> Credentials:
    """Load or refresh Google OAuth2 credentials with validation."""
    creds = None
    token_path = _get_token_path()

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Google credentials refreshed successfully")
                if not IS_RAILWAY:
                    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
                    TOKEN_PATH.chmod(0o600)
            except Exception as e:
                logger.error("Google credential refresh FAILED: %s", e)
                raise RuntimeError(
                    f"Google OAuth refresh failed: {e}. "
                    f"Re-authorize locally and update GOOGLE_TOKEN_JSON_B64 in Railway."
                ) from e
        else:
            # ... existing error handling ...
            pass

    return creds
```

---

### Railway Credential Materialization

**Status:** ✅ **GOOD** — Secure temp file handling

**Implementation (tools/core/config.py, lines 58–95):**
```python
def _materialize_credential_file(
    b64_env_key: str,
    fallback_path: Path,
    file_permissions: int = 0o600,
) -> Path:
    decoded = _decode_b64_env(b64_env_key)
    if decoded:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix=f"{b64_env_key.lower()}_",
            delete=False,
        )
        tmp.write(decoded)
        tmp.close()
        os.chmod(tmp.name, file_permissions)  # ← 0o600 (user RW only)
        result = Path(tmp.name)
        _credential_file_cache[b64_env_key] = result
        return result
```

**Strengths:**
1. Uses `tempfile.NamedTemporaryFile` with auto-cleanup (`delete=False` is intentional for caching)
2. Permissions set to `0o600` (user read/write only)
3. Results cached to avoid re-materialization
4. Errors logged, not silenced

**Weaknesses:**
1. **Temp files NOT explicitly deleted on process shutdown** — ephemeral in Railway, but on VMs they persist until temp cleanup
2. **Cached files not cleared on credential rotation** — old credential files remain on disk

**Recommendation:** Add explicit cleanup:
```python
import atexit

def _cleanup_temp_credentials() -> None:
    """Clean up materialized credential files on process exit."""
    for path in _credential_file_cache.values():
        if path.exists():
            try:
                path.unlink()
                logger.debug("Cleaned up temp credential file: %s", path)
            except Exception as e:
                logger.warning("Failed to clean up %s: %s", path, e)

atexit.register(_cleanup_temp_credentials)
```

---

### Scope Creep

**Status:** ✅ **GOOD** — Minimal scopes

**Implementation (line 28–31):**
```python
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/docs",
]
```

**Analysis:**
- `drive` scope is overly broad (grants all Drive operations) but necessary for:
  - Creating/managing lecture folders
  - Uploading recordings and summaries
  - Creating Google Docs
- `docs` scope is necessary for document creation and updates

**Recommendation:** Document why minimal scopes can't be used:
```python
SCOPES = [
    # Note: Google Drive doesn't offer granular scopes
    # (no "drive.file" equivalent for serviceAccount operations).
    # This scope grants full Drive access; minimize usage to service accounts only.
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/docs",
]
```

---

## 3. Webhook Validation (tools/app/server.py)

### HMAC-SHA256 Implementation

**Status:** ✅ **EXCELLENT** — Timing-safe comparison used

**Implementation (lines 216–234):**
```python
def verify_webhook_secret(authorization: str | None = Header(None)) -> None:
    """Validate webhook secret with timing-safe comparison."""
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET not configured — rejecting request")
        raise HTTPException(status_code=503, detail="Server misconfigured")

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    expected = f"Bearer {WEBHOOK_SECRET}"
    if not hmac.compare_digest(authorization, expected):  # ← Timing-safe!
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
```

**Strengths:**
1. Uses `hmac.compare_digest()` (constant-time comparison)
2. Prevents timing-based secret enumeration
3. Fails closed: if WEBHOOK_SECRET unset, rejects all requests
4. Clear error messages for debugging (non-sensitive)

**Weaknesses:**
None identified. This is a textbook-correct implementation.

---

### Zoom Webhook CRC Validation

**Status:** ✅ **CORRECT** — Proper HMAC-SHA256 implementation

**Implementation (lines 535–547):**
```python
def _handle_zoom_crc(body: dict) -> dict:
    """Handle Zoom endpoint.url_validation (CRC challenge-response)."""
    plain_token = body.get("payload", {}).get("plainToken", "")
    if not plain_token or len(plain_token) > 256:
        raise HTTPException(status_code=400, detail="Invalid plainToken")

    encrypted_token = hmac.new(
        ZOOM_WEBHOOK_SECRET_TOKEN.encode(),
        plain_token.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted_token}
```

**Strengths:**
1. Validates plainToken length (prevents DOS via huge strings)
2. Correct HMAC-SHA256 implementation (per Zoom spec)
3. Validation happens before HMAC computation

**Weaknesses:**
- **plainToken length limit of 256 chars is arbitrary** — should match Zoom's spec (if documented)

---

### Zoom Webhook Signature Verification

**Status:** ✅ **GOOD** — Correct implementation with timestamp validation

**Implementation (lines 550–577):**
```python
def _verify_zoom_signature(raw_body: bytes, request: Request) -> None:
    """Verify Zoom HMAC-SHA256 signature and reject stale timestamps."""
    if not ZOOM_WEBHOOK_SECRET_TOKEN:
        raise HTTPException(status_code=503, detail="ZOOM_WEBHOOK_SECRET_TOKEN not configured")

    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Zoom signature headers")

    # Timestamp validation: reject requests older than 5 minutes
    try:
        ts_age = abs(time.time() - int(timestamp))
        if ts_age > 300:
            logger.warning("Zoom webhook timestamp too old: %s seconds", ts_age)
            raise HTTPException(status_code=401, detail="Zoom webhook timestamp expired")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp header")

    # HMAC verification
    message = f"v0:{timestamp}:{raw_body.decode()}"
    expected_sig = "v0=" + hmac.new(
        ZOOM_WEBHOOK_SECRET_TOKEN.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):  # ← Timing-safe!
        logger.warning("Zoom webhook signature mismatch — rejecting request")
        raise HTTPException(status_code=401, detail="Invalid Zoom webhook signature")
```

**Strengths:**
1. Constant-time HMAC comparison (`hmac.compare_digest()`)
2. Timestamp validation (prevents replay attacks)
3. 5-minute window is reasonable for clock skew
4. Proper error handling for malformed headers

**Weaknesses:**
1. **Timestamp validation uses `abs()` — accepts future timestamps** (could be exploited if server clock is behind attacker's)
   - Example: Attacker sends request with `timestamp = now + 200s`, signs it, waits 300s to deliver
   - Solution: Only allow timestamps ≤ 5 min in the **past**, not future

**Recommendation:** Tighten timestamp validation:
```python
try:
    request_timestamp = int(timestamp)
    current_time = int(time.time())
    ts_age = current_time - request_timestamp  # Only past timestamps

    if ts_age < -10:  # Allow 10s clock skew forward
        logger.warning("Zoom webhook timestamp in future (skew: %ds)", ts_age)
        raise HTTPException(status_code=401, detail="Zoom webhook timestamp invalid (future)")

    if ts_age > 300:
        logger.warning("Zoom webhook timestamp too old: %s seconds", ts_age)
        raise HTTPException(status_code=401, detail="Zoom webhook timestamp expired")
except ValueError:
    raise HTTPException(status_code=401, detail="Invalid timestamp header")
```

---

### WEBHOOK_SECRET Handling

**Status:** ✅ **GOOD** — Fail-closed behavior enforced

**Implementation (lines 222–227):**
```python
if not WEBHOOK_SECRET:
    logger.error("WEBHOOK_SECRET not configured — rejecting request (fail closed)")
    raise HTTPException(
        status_code=503,
        detail="Server misconfigured: WEBHOOK_SECRET not set",
    )
```

**Strengths:**
1. Fails closed: missing secret = reject all requests
2. Prevents accidental open access
3. Returns 503 (service unavailable) which signals operational issue, not auth failure

**Weaknesses:**
- None identified. This is correct.

---

## 4. WhatsApp Green API (tools/integrations/whatsapp_sender.py)

### Instance ID + Token Authentication

**Status:** ⚠️ **ACCEPTABLE** — Works but authentication is weak

**Implementation (lines 51–80):**
```python
def _base_url() -> str:
    return f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE_ID}"

def _send_request(method: str, payload: dict[str, Any], purpose: str) -> dict[str, Any]:
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        raise ValueError("Green API not configured...")

    url = f"{_base_url()}/{method}/{GREEN_API_TOKEN}"

    with httpx.Client(timeout=30) as client:
        response = client.post(url, json=payload)
```

**Strengths:**
1. Authentication enforced before sending
2. Errors on missing credentials
3. Uses HTTPS for all requests

**Weaknesses:**
1. **Token in URL** (line 80) — violates best practices:
   - URLs may be logged (web server logs, proxy logs, browser history)
   - URLs may appear in referer headers, error messages
   - Green API doesn't support Authorization headers (protocol limitation)
2. **Instance ID is essentially public** — used to construct the URL
3. **No request signing** — relies entirely on HTTPS + token
4. **Retry logic retries on ANY RuntimeError** (line 101) — could retry non-idempotent operations (message sending)

**Recommendation:** Add request logging controls:
```python
# Patch httpx logger to redact token in URLs
import logging
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)  # Reduce verbosity

# Or: Document that logs must be treated as secrets
logger.info("Green API request: %s (token redacted)", method)
```

Also, tighten retry logic:
```python
IDEMPOTENT_METHODS = {"sendMessage"}  # May have built-in dedup, but be conservative

def _send_request(...):
    def _do_request() -> dict[str, Any]:
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json=payload)

        if response.status_code == 200:
            return response.json()

        # 409 (conflict) = message already sent — not retryable
        if response.status_code == 409:
            logger.warning("Message already sent (idempotent conflict)")
            return response.json()

        # Don't retry client errors (except 429 rate limit)
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise _NonRetryableError(...)

        raise RuntimeError(...)

    # Only retry on server errors and rate limits
    return retry_with_backoff(
        _do_request,
        max_retries=MAX_RETRIES,
        backoff_base=float(RETRY_BASE_DELAY),
        retryable_exceptions=(RuntimeError,),  # Not TransportError (could be app-level)
    )
```

---

### Incoming Message Validation

**Status:** ⚠️ **GOOD ENOUGH** — No spoofing risk, but could be tighter

**Implementation (lines 453–514):**
```python
@app.post("/whatsapp-incoming")
async def whatsapp_incoming(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
):
    """Receive incoming WhatsApp messages from Green API webhook."""
    verify_webhook_secret(authorization)  # ← Validates WEBHOOK_SECRET

    body = await request.json()

    type_webhook = body.get("typeWebhook")
    if type_webhook != "incomingMessageReceived":
        return {"status": "ignored", "reason": f"type: {type_webhook}"}

    # Extract message data
    message_data = body.get("messageData", {})
    type_message = message_data.get("typeMessage")
    sender_data = body.get("senderData", {})

    # Skip self-messages
    if message_data.get("fromMe", False):
        return {"status": "ignored", "reason": "own message"}

    # Process in background
    incoming = IncomingMessage(...)
    background_tasks.add_task(_handle_assistant_message, incoming)
    return {"status": "accepted"}
```

**Strengths:**
1. Authorization header validated (Bearer token)
2. Skips self-messages (prevents infinite loops)
3. Type validation (only processes `incomingMessageReceived` events)
4. No sensitive data in responses

**Weaknesses:**
1. **No rate limiting per sender** — a single attacker can spam `/whatsapp-incoming` at the per-IP limit
   - Fixed rate of 30/minute applies to all senders combined
   - Should add sender-based rate limiting

**Recommendation:** Add sender-based deduplication:
```python
import hashlib
from datetime import datetime, timedelta

_message_cache: dict[str, datetime] = {}

@app.post("/whatsapp-incoming")
@limiter.limit("30/minute")  # Per-IP
async def whatsapp_incoming(...):
    verify_webhook_secret(authorization)
    body = await request.json()

    # Deduplicate by sender + message ID
    sender_id = body.get("senderData", {}).get("sender", "")
    message_id = body.get("idMessage", "")

    if not sender_id or not message_id:
        logger.warning("Missing sender or message ID")
        return {"status": "ignored", "reason": "incomplete message"}

    cache_key = f"{sender_id}_{message_id}"
    now = datetime.now()

    if cache_key in _message_cache:
        age = (now - _message_cache[cache_key]).total_seconds()
        if age < 300:  # 5-minute window
            logger.debug("Duplicate message from %s (cached %ds ago)", sender_id, age)
            return {"status": "ignored", "reason": "duplicate"}
        else:
            del _message_cache[cache_key]  # Expired, allow retry

    _message_cache[cache_key] = now

    # ... rest of processing ...
```

---

## 5. Rate Limiting (slowapi in server.py)

### Per-IP Limits

**Status:** ⚠️ **POTENTIALLY BYPASSABLE** — IP-based in proxy scenario

**Implementation (lines 145–148):**
```python
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

And endpoint limits:
```python
@app.get("/health")
@limiter.limit("60/minute")
async def health_check(request: Request):
    ...

@app.post("/whatsapp-incoming")
@limiter.limit("30/minute")
async def whatsapp_incoming(...):
    ...

@app.post("/process-recording")
@limiter.limit("5/minute")
async def process_recording(...):
    ...
```

**Strengths:**
1. Endpoints have reasonable limits
2. Uses slowapi (proven rate limiter)
3. Catches RateLimitExceeded exceptions

**Weaknesses:**
1. **`get_remote_address()` reads X-Forwarded-For header** (default slowapi behavior)
   - On Railway behind Cloudflare, this is safe (Cloudflare validates the header)
   - On localhost or untrusted reverse proxy, attackers can spoof their IP
2. **TrustedHostMiddleware (lines 134–137) uses wildcard on Railway** (line 132)
   - This is correct (Railway's proxy validates hosts internally)
   - But combined with IP-based rate limiting, could be spoofable in custom deployments

**Recommendation:** Document rate limiting assumptions:
```python
# Rate limiting is IP-based. On Railway, this is safe because:
# 1. TrustedHostMiddleware validates Host headers
# 2. Cloudflare proxy sets X-Forwarded-For which slowapi reads
# 3. Attackers cannot forge client IPs through the Railway proxy
#
# If deploying behind an untrusted proxy, disable rate limiting or
# implement request signing instead.
```

Also, consider adding per-token rate limiting for authenticated endpoints:
```python
@app.post("/process-recording")
@limiter.limit("5/minute")  # Per IP
async def process_recording(
    request: Request,
    authorization: str | None = Header(None),
    ...
):
    # Additional rate limit per token
    token_hash = hashlib.sha256(authorization.encode()).hexdigest()[:8]
    await limiter.hit(key=f"token:{token_hash}")
    ...
```

---

## 6. Pinecone (tools/integrations/knowledge_indexer.py)

### API Key Handling

**Status:** ⚠️ **ACCEPTABLE** — In-memory only, no explicit validation

**Implementation (lines 60–102):**
```python
def get_pinecone_index() -> object:
    """Get or create the Pinecone index (cached after first call)."""
    global _pinecone_index_cache
    if _pinecone_index_cache is not None:
        return _pinecone_index_cache

    if not PINECONE_API_KEY:
        raise RuntimeError("Pinecone API key not configured")

    pc = Pinecone(api_key=PINECONE_API_KEY)

    # ... create index if needed ...

    index = pc.Index(PINECONE_INDEX_NAME)
    _pinecone_index_cache = index
    return index
```

And embedding client (lines 138–149):
```python
_embed_client_cache: genai.Client | None = None

def _get_embed_client() -> genai.Client:
    """Return a cached Gemini client for embedding calls."""
    global _embed_client_cache
    if _embed_client_cache is not None:
        return _embed_client_cache

    api_key = GEMINI_API_KEY_PAID or GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("Gemini API key not configured")

    _embed_client_cache = genai.Client(api_key=api_key)
    return _embed_client_cache
```

**Strengths:**
1. API key checked at initialization (fails early)
2. Client object cached (avoids repeated auth)
3. Falls back to free tier if paid key unavailable

**Weaknesses:**
1. **API key stored in memory as plaintext**
   - Acceptable for in-process use, but no explicit cleanup
   - Memory dumps could leak the key
2. **No validation that the key is actually valid** — only checked if present
3. **Token refresh not handled** — Gemini and Pinecone use API keys (stateless), but long-lived keys could be revoked
4. **Caching doesn't detect key invalidation** — if key is revoked, will keep using cached client until next restart

**Recommendation:** Add periodic key validation:
```python
import threading
import time

_last_api_validation = 0.0
_api_validation_lock = threading.Lock()
API_VALIDATION_INTERVAL = 3600  # 1 hour

def _validate_pinecone_api_key() -> bool:
    """Test Pinecone API key by making a safe read-only call."""
    global _last_api_validation

    with _api_validation_lock:
        now = time.time()
        if now - _last_api_validation < API_VALIDATION_INTERVAL:
            return True  # Skip if recently validated

        try:
            index = get_pinecone_index()
            stats = index.describe_index_stats()
            _last_api_validation = now
            logger.debug("Pinecone API key validated successfully")
            return True
        except Exception as e:
            logger.error("Pinecone API key validation FAILED: %s", e)
            # Invalidate cache and fail subsequent calls
            global _pinecone_index_cache
            _pinecone_index_cache = None
            return False

# Call before operations
if not _validate_pinecone_api_key():
    raise RuntimeError("Pinecone API key invalid or service unavailable")
```

---

## 7. Cross-Cutting Security Issues

### SSRF Protection

**Status:** ✅ **GOOD** — Download URL validation implemented

**Implementation (lines 263–269, 353–360 in server.py):**
```python
# Step 0: Validate download URL (SSRF prevention)
parsed = urlparse(payload.download_url)
if parsed.scheme != "https":
    raise ValueError(f"Only HTTPS download URLs allowed, got: {parsed.scheme}")
hostname = (parsed.hostname or "").lower()
if hostname != "zoom.us" and not hostname.endswith(".zoom.us"):
    raise ValueError(f"Download URL must be from zoom.us, got: {parsed.hostname}")

# ... later, after redirects ...
final_host = (response.url.host or "").lower()
is_zoom = final_host == "zoom.us" or final_host.endswith(".zoom.us")
is_zoomgov = final_host == "zoomgov.com" or final_host.endswith(".zoomgov.com")
if not is_zoom and not is_zoomgov:
    raise ValueError(f"Download redirected to untrusted host: {final_host}")
```

**Strengths:**
1. Initial URL validation (scheme + hostname)
2. Post-redirect validation (prevents open redirect exploitation)
3. Allows both zoom.us and zoomgov.com (US government Zoom)

**Weaknesses:**
- None identified. This is correct SSRF protection.

---

### Sensitive Data in Logs

**Status:** ⚠️ **NEEDS IMPROVEMENT** — Some secrets may appear in error messages

**Current Practice:**
- Zoom API errors logged with response.text (line 224): could include auth errors
- Webhook error responses logged (line 576): signature mismatches logged (acceptable)
- Exception tracebacks logged (line 313): could expose stack traces with secrets

**Example vulnerability:**
```python
logger.error(
    "Zoom API %s %s returned HTTP %d: %s",  # ← response.text could contain sensitive data
    method, endpoint, response.status_code, error_body
)
```

**Recommendation:** Redact sensitive data in error logs:
```python
def _redact_error_response(error_text: str, max_length: int = 200) -> str:
    """Redact potentially sensitive data from error responses."""
    if len(error_text) > max_length:
        return error_text[:max_length] + "... (truncated)"
    # Redact tokens, keys, emails
    import re
    redacted = re.sub(r'"access_token":"[^"]*"', '"access_token":"***"', error_text)
    redacted = re.sub(r'"token":"[^"]*"', '"token":"***"', redacted)
    redacted = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '***@***.***', redacted)
    return redacted

# Use in logging:
logger.error(
    "Zoom API %s %s returned HTTP %d: %s",
    method, endpoint, response.status_code, _redact_error_response(error_body)
)
```

---

### Missing Security Headers

**Status:** ⚠️ **MOSTLY GOOD** — HSTS header missing

**Implementation (lines 152–172):**
```python
@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> JSONResponse:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "..."
    )
    return response
```

**Strengths:**
1. ✅ X-Content-Type-Options: nosniff (prevents MIME sniffing)
2. ✅ X-Frame-Options: DENY (prevents clickjacking)
3. ✅ Cache-Control: no-store (prevents caching of auth responses)
4. ✅ CSP configured (though 'unsafe-inline' is present)

**Weaknesses:**
1. **Missing Strict-Transport-Security (HSTS)** — no enforcement of HTTPS
2. **CSP has 'unsafe-inline'** (line 165) — allows inline scripts, reducing CSP effectiveness

**Recommendation:** Add HSTS and tighten CSP:
```python
response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
response.headers["Content-Security-Policy"] = (
    "default-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net; "  # Remove 'unsafe-inline'
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)
```

---

### Input Validation

**Status:** ✅ **GOOD** — Most inputs validated

**Examples:**
- Group/lecture numbers (lines 798–801): `if payload.group_number not in (1, 2):`
- Drive folder ID regex (lines 182, 196–197): `_DRIVE_FOLDER_ID_RE`
- Plaintext token length in Zoom CRC (line 538): `len(plain_token) > 256`

**Weaknesses:**
- WhatsApp chat IDs not validated (should match format `^\d+@c\.us$` or `\d+@g\.us$`)

**Recommendation:** Add chat ID validation:
```python
def _validate_chat_id(chat_id: str) -> bool:
    """Validate WhatsApp chat ID format."""
    import re
    # Individual: 995XXXXXXXXX@c.us (country code + number)
    # Group: XXXXXXXXXX-XXXXXXXXXX@g.us
    pattern = r'^(\d+@c\.us|[0-9\-]+@g\.us)$'
    return bool(re.match(pattern, chat_id))

def send_message_to_chat(chat_id: str, message: str) -> dict[str, Any]:
    if not _validate_chat_id(chat_id):
        raise ValueError(f"Invalid chat_id format: {chat_id}")
    ...
```

---

## Summary Table

| Component | Risk | Issue | Recommendation |
|-----------|------|-------|-----------------|
| Zoom Token Cache | LOW | No explicit cleanup on shutdown | Add atexit handler |
| Zoom Refresh | LOW | Silent failures possible | Add validation before use |
| Google OAuth | MEDIUM | Railway no-disk refresh untested | Add explicit success validation |
| Drive Credentials | LOW | Temp files not cleaned up | Add atexit handler |
| Webhook Secret | LOW | — | ✅ No issues |
| Zoom CRC | LOW | — | ✅ No issues |
| Zoom Signature | MEDIUM | Accepts future timestamps | Tighten timestamp validation to past-only |
| Green API | MEDIUM | Token in URL, retries non-idempotent ops | Document logging risk, limit retries |
| WhatsApp Incoming | MEDIUM | No sender-based deduplication | Add message dedup cache |
| Rate Limiting | LOW | Could be spoofable in untrusted proxy | Document assumptions |
| Pinecone API Key | LOW | No validation after initial check | Add periodic validation |
| SSRF | LOW | — | ✅ No issues |
| Logs | MEDIUM | Sensitive data may appear in errors | Redact secrets from error messages |
| Security Headers | LOW | Missing HSTS | Add HSTS header |
| Input Validation | LOW | Missing WhatsApp chat ID validation | Add format validation |

---

## Deployment Checklist

### Before Going to Production

- [ ] All API keys rotated and stored in Railway env vars (base64-encoded)
- [ ] WEBHOOK_SECRET and ZOOM_WEBHOOK_SECRET_TOKEN configured (non-empty)
- [ ] GOOGLE_TOKEN_JSON_B64 contains valid refresh token (test refresh flow)
- [ ] Green API webhook URL configured to include WEBHOOK_SECRET as token
- [ ] Rate limiter tested under load
- [ ] Logs reviewed for sensitive data leakage
- [ ] HSTS header enabled
- [ ] Zoom timestamp validation tightened (optional, good-to-have)

### Runtime Monitoring

- [ ] Alert on WEBHOOK_SECRET validation failures (401/403 on protected endpoints)
- [ ] Alert on token refresh failures (Google, Zoom)
- [ ] Alert on Pinecone API errors (key may be revoked)
- [ ] Periodic audit of credential files on filesystem
- [ ] Monitor for rate limit (429) spike anomalies

---

## References

- [OWASP: Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [OWASP: Webhook Security](https://owasp.org/www-community/attacks/Webhook_Injection)
- [OWASP: Server-Side Request Forgery (SSRF)](https://owasp.org/www-community/attacks/Server-Side_Request_Forgery)
- [Zoom Webhook Security Docs](https://marketplace.zoom.us/docs/api-reference/webhook-reference#event-types)
- [slowapi Rate Limiting Docs](https://slowapi.readthedocs.io/)
- [Python hmac.compare_digest() — Timing-Safe Comparison](https://docs.python.org/3/library/hmac.html#hmac.compare_digest)
