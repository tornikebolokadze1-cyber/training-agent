# Security Fixes — Implementation Guide

This document provides copy-paste-ready code fixes for the issues identified in SECURITY_REVIEW.md.

---

## Fix 1: Add Token Cache Cleanup (zoom_manager.py)

**Issue:** Zoom token cache not explicitly cleared on shutdown

**File:** `tools/integrations/zoom_manager.py`

**Add at end of file (before or after the docstring at the top):**

```python
import atexit

def clear_token_cache() -> None:
    """Explicitly clear cached Zoom tokens on process shutdown."""
    with _token_lock:
        if _token_cache:
            _token_cache.clear()
            logger.info("Zoom token cache cleared on shutdown")

# Register cleanup on process exit
atexit.register(clear_token_cache)
```

---

## Fix 2: Add Google OAuth Refresh Validation (gdrive_manager.py)

**Issue:** OAuth refresh can fail silently on Railway without raising an error

**File:** `tools/integrations/gdrive_manager.py`

**Replace lines 65–76 with:**

```python
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info("Google OAuth credentials refreshed successfully")
            if not IS_RAILWAY:
                TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
                TOKEN_PATH.chmod(0o600)
        except Exception as e:
            logger.error("Google OAuth refresh FAILED: %s — user re-auth required", e)
            raise RuntimeError(
                "Google OAuth refresh failed. This requires manual re-authorization:\n"
                "1. Run locally: python -m tools.integrations.gdrive_manager\n"
                "2. Complete browser authorization\n"
                "3. On Railway, update GOOGLE_TOKEN_JSON_B64 with:\n"
                "   base64 -i token.json | tr -d '\\n'\n"
                "4. Restart the service"
            ) from e
    else:
        # ... existing error handling continues ...
```

---

## Fix 3: Add Railway Credential Cleanup (config.py)

**Issue:** Temp credential files not cleaned up on Railway

**File:** `tools/core/config.py`

**Add at the end of the file (after all function definitions):**

```python
import atexit as _atexit

def _cleanup_temp_credentials() -> None:
    """Clean up materialized credential files on process exit."""
    for key, path in list(_credential_file_cache.items()):
        if path and path.exists():
            try:
                path.unlink()
                logger.info("Cleaned up temp credential file: %s", path)
            except Exception as e:
                logger.warning("Failed to clean up %s: %s", path, e)

_atexit.register(_cleanup_temp_credentials)
```

---

## Fix 4: Tighten Zoom Timestamp Validation (server.py)

**Issue:** Current implementation accepts future timestamps (replay attack window)

**File:** `tools/app/server.py`

**Replace lines 560–566 with:**

```python
try:
    request_timestamp = int(timestamp)
    current_time = int(time.time())
    ts_age = current_time - request_timestamp

    # Allow up to 10 seconds in the future (clock skew tolerance)
    if ts_age < -10:
        logger.warning("Zoom webhook timestamp in future: %d seconds", -ts_age)
        raise HTTPException(status_code=401, detail="Zoom webhook timestamp invalid (future)")

    # Reject timestamps older than 5 minutes (replay attack prevention)
    if ts_age > 300:
        logger.warning("Zoom webhook timestamp too old: %d seconds", ts_age)
        raise HTTPException(status_code=401, detail="Zoom webhook timestamp expired")
except ValueError:
    raise HTTPException(status_code=401, detail="Invalid timestamp header")
```

---

## Fix 5: Add HSTS Header (server.py)

**Issue:** Missing Strict-Transport-Security header for HTTPS enforcement

**File:** `tools/app/server.py`

**Replace lines 159–162 with:**

```python
response = await call_next(request)
response.headers["Strict-Transport-Security"] = (
    "max-age=31536000; includeSubDomains; preload"
)
response.headers["X-Content-Type-Options"] = "nosniff"
```

---

## Fix 6: Add Sensitive Data Redaction for Logs (server.py)

**Issue:** API error responses may contain sensitive data in logs

**File:** `tools/app/server.py`

**Add near the top of the file (after imports):**

```python
import re as _re

def _redact_sensitive_data(text: str, max_length: int = 300) -> str:
    """Redact potentially sensitive data from error responses for logging."""
    if not text:
        return text

    # Truncate very long responses
    if len(text) > max_length:
        text = text[:max_length] + "... (truncated)"

    # Redact common secret patterns
    text = _re.sub(r'"access_token":"[^"]*"', '"access_token":"***"', text)
    text = _re.sub(r'"token":"[^"]*"', '"token":"***"', text)
    text = _re.sub(r'"apikey":"[^"]*"', '"apikey":"***"', text)
    text = _re.sub(r'Bearer [A-Za-z0-9\._\-]+', 'Bearer ***', text)
    # Redact email addresses
    text = _re.sub(
        r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
        '***@***.***',
        text
    )

    return text
```

**Then replace the error log on line 224 with:**

```python
error_body = response.text
logger.error(
    "Zoom API %s %s returned HTTP %d (attempt %d/%d): %s",
    method.upper(),
    endpoint,
    response.status_code,
    attempt,
    MAX_RETRIES,
    _redact_sensitive_data(error_body),
)
```

---

## Fix 7: Add Sender-Based Message Deduplication (server.py)

**Issue:** No sender-based deduplication for WhatsApp incoming messages

**File:** `tools/app/server.py`

**Add near the top (after the _processing_tasks definition, around line 62):**

```python
# WhatsApp message deduplication cache
_whatsapp_message_cache: dict[str, datetime] = {}
WHATSAPP_MESSAGE_TTL_SECONDS = 300  # 5-minute cache window
```

**Replace the whatsapp_incoming function (lines 453–514) with:**

```python
@app.post("/whatsapp-incoming")
@limiter.limit("30/minute")
async def whatsapp_incoming(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
):
    """Receive incoming WhatsApp messages from Green API webhook.

    Green API sends notifications for all incoming messages.
    We process them in the background to return 200 immediately.

    Authentication: same Bearer token as /process-recording.
    Configure Green API webhookUrlToken to send this header.
    """
    verify_webhook_secret(authorization)

    # Parse the raw JSON (Green API format varies)
    body = await request.json()

    # Only process incoming text messages
    type_webhook = body.get("typeWebhook")
    if type_webhook != "incomingMessageReceived":
        return {"status": "ignored", "reason": f"type: {type_webhook}"}

    message_data = body.get("messageData", {})
    type_message = message_data.get("typeMessage")

    # Extract text from different message types
    sender_data = body.get("senderData", {})
    text = ""
    if type_message == "textMessage":
        text = message_data.get("textMessageData", {}).get("textMessage", "")
    elif type_message in ("extendedTextMessage", "quotedMessage"):
        text = message_data.get("extendedTextMessageData", {}).get("text", "")
    else:
        return {"status": "ignored", "reason": f"message type: {type_message}"}

    if not text.strip():
        return {"status": "ignored", "reason": "empty text"}

    # Skip messages sent by the bot itself (prevents infinite loops)
    if message_data.get("fromMe", False):
        return {"status": "ignored", "reason": "own message"}

    # Deduplicate by sender + message ID (prevent processing duplicates)
    sender_id = sender_data.get("sender", "")
    message_id = body.get("idMessage", "")

    if not sender_id or not message_id:
        logger.warning("Incoming WhatsApp message missing sender or ID")
        return {"status": "ignored", "reason": "incomplete message"}

    dedup_key = f"{sender_id}_{message_id}"
    now = datetime.now()

    # Check if we've seen this message recently
    if dedup_key in _whatsapp_message_cache:
        cached_time = _whatsapp_message_cache[dedup_key]
        age_seconds = (now - cached_time).total_seconds()

        if age_seconds < WHATSAPP_MESSAGE_TTL_SECONDS:
            logger.debug(
                "Ignoring duplicate WhatsApp message from %s (cached %d seconds ago)",
                sender_id[:20], int(age_seconds),
            )
            return {"status": "ignored", "reason": "duplicate"}
        else:
            # Expired, allow reprocessing
            del _whatsapp_message_cache[dedup_key]

    # Cache this message ID
    _whatsapp_message_cache[dedup_key] = now

    # Clean up old cache entries (prune every 100 messages)
    if len(_whatsapp_message_cache) % 100 == 0:
        stale_keys = [
            k for k, v in _whatsapp_message_cache.items()
            if (now - v).total_seconds() > WHATSAPP_MESSAGE_TTL_SECONDS
        ]
        for k in stale_keys:
            del _whatsapp_message_cache[k]
        if stale_keys:
            logger.debug("Pruned %d stale WhatsApp message cache entries", len(stale_keys))

    if not _assistant_available or assistant is None:
        logger.warning("WhatsApp assistant not available — ignoring incoming message")
        return {"status": "ignored", "reason": "assistant not available"}

    incoming = IncomingMessage(
        chat_id=sender_data.get("chatId", ""),
        sender_id=sender_id,
        sender_name=sender_data.get("senderName", ""),
        text=text,
        timestamp=body.get("timestamp", 0),
    )

    # Process in background
    background_tasks.add_task(_handle_assistant_message, incoming)

    return {"status": "accepted"}
```

---

## Fix 8: Add WhatsApp Chat ID Validation (whatsapp_sender.py)

**Issue:** No validation of chat ID format

**File:** `tools/integrations/whatsapp_sender.py`

**Add near the top (after imports, around line 33):**

```python
import re as _re

def _validate_chat_id(chat_id: str) -> bool:
    """Validate WhatsApp chat ID format.

    Valid formats:
    - Individual: '995XXXXXXXXX@c.us' (country code + 9+ digits)
    - Group: '120363XXXXXXXXX-XXXXXXXXX@g.us'
    """
    if not chat_id or not isinstance(chat_id, str):
        return False

    # Individual chat: digits + @c.us
    if chat_id.endswith("@c.us"):
        number_part = chat_id[:-5]
        return bool(_re.match(r'^\d{10,}$', number_part))

    # Group chat: digits/dashes + @g.us
    if chat_id.endswith("@g.us"):
        id_part = chat_id[:-5]
        return bool(_re.match(r'^[\d\-]{15,}$', id_part))

    return False
```

**Update send_message_to_chat function (line 115) to:**

```python
def send_message_to_chat(chat_id: str, message: str) -> dict[str, Any]:
    """Send a text message to a WhatsApp chat (individual or group).

    Args:
        chat_id: WhatsApp chat ID.
            - Individual: '995XXXXXXXXX@c.us' (country code + number)
            - Group: 'XXXXXXXXXX-XXXXXXXXXX@g.us'
        message: The text message to send.

    Returns:
        Green API response dict.

    Raises:
        ValueError: If chat_id format is invalid.
    """
    if not _validate_chat_id(chat_id):
        raise ValueError(f"Invalid WhatsApp chat ID format: {chat_id}")

    chunks = _split_message(message)
    # ... rest of function continues unchanged ...
```

---

## Fix 9: Document Rate Limiting Assumptions (server.py)

**Issue:** Rate limiting assumptions not documented for future maintainers

**File:** `tools/app/server.py`

**Add comment block near the TrustedHostMiddleware setup (around line 107):**

```python
# ============================================================================
# Rate Limiting Strategy
# ============================================================================
# This application uses IP-based rate limiting via slowapi.
#
# IMPORTANT: This is safe only when deployed behind a TRUSTED reverse proxy
# (e.g., Railway, Cloudflare) that:
#   1. Validates the Host header (prevents host header injection)
#   2. Sets X-Forwarded-For correctly (client IP is authenticated)
#   3. Prevents header spoofing by clients
#
# On Railway:
#   - Cloudflare proxy validates X-Forwarded-For before forwarding
#   - TrustedHostMiddleware with wildcard is acceptable (Railway's proxy
#     validates internally)
#   - Attacker cannot forge client IP through the proxy
#
# If deploying on untrusted infrastructure:
#   - Disable IP-based rate limiting or use request signing
#   - Consider API key-based rate limiting (per-token buckets)
#   - Implement HMAC request signing to prevent spoofing
# ============================================================================
```

---

## Fix 10: Add Pinecone API Key Periodic Validation (knowledge_indexer.py)

**Issue:** Pinecone API key validity not checked after initialization

**File:** `tools/integrations/knowledge_indexer.py`

**Add near the top (after constants, around line 45):**

```python
import threading as _threading

# Periodic API key validation
_last_pinecone_validation = 0.0
_pinecone_validation_lock = _threading.Lock()
PINECONE_VALIDATION_INTERVAL = 3600  # Validate every 1 hour

_last_gemini_validation = 0.0
_gemini_validation_lock = _threading.Lock()
GEMINI_VALIDATION_INTERVAL = 3600  # Validate every 1 hour
```

**Add these functions (before get_pinecone_index):**

```python
def _validate_pinecone_key() -> bool:
    """Periodically verify Pinecone API key is still valid.

    Returns:
        True if key is valid or was recently validated.
        False if validation fails.
    """
    global _last_pinecone_validation

    with _pinecone_validation_lock:
        now = time.time()
        if now - _last_pinecone_validation < PINECONE_VALIDATION_INTERVAL:
            return True  # Recently validated, skip

        try:
            index = get_pinecone_index()
            # Safe read-only operation
            stats = index.describe_index_stats()
            _last_pinecone_validation = now
            logger.debug("Pinecone API key validated successfully")
            return True
        except Exception as e:
            logger.error("Pinecone API key validation FAILED: %s", e)
            # Invalidate cache so next call attempts fresh initialization
            global _pinecone_index_cache
            _pinecone_index_cache = None
            return False


def _validate_gemini_key() -> bool:
    """Periodically verify Gemini API key is still valid.

    Returns:
        True if key is valid or was recently validated.
        False if validation fails.
    """
    global _last_gemini_validation

    with _gemini_validation_lock:
        now = time.time()
        if now - _last_gemini_validation < GEMINI_VALIDATION_INTERVAL:
            return True  # Recently validated, skip

        try:
            client = _get_embed_client()
            # Make a tiny test embedding to verify key works
            response = client.models.embed_content(
                model=GEMINI_EMBEDDING_MODEL,
                contents="test",
            )
            _last_gemini_validation = now
            logger.debug("Gemini API key validated successfully")
            return True
        except Exception as e:
            logger.error("Gemini API key validation FAILED: %s", e)
            # Invalidate cache
            global _embed_client_cache
            _embed_client_cache = None
            return False
```

**Update embed_text to validate before use:**

```python
def embed_text(text: str) -> list[float]:
    """Generate an embedding vector using gemini-embedding-001.

    Args:
        text: Input text to embed (any length; truncated server-side if needed).

    Returns:
        A list of 3072 floats representing the embedding vector.

    Raises:
        RuntimeError: If GEMINI_API_KEY is not configured or all retries fail.
    """
    # Validate key is still working
    if not _validate_gemini_key():
        raise RuntimeError(
            "Gemini API key is invalid or service is unavailable. "
            "Check GEMINI_API_KEY environment variable."
        )

    client = _get_embed_client()

    def _do_embed() -> list[float]:
        logger.debug(
            "Embedding text (%d chars) with %s...",
            len(text), GEMINI_EMBEDDING_MODEL,
        )
        response = client.models.embed_content(
            model=GEMINI_EMBEDDING_MODEL,
            contents=text,
        )
        vector = response.embeddings[0].values
        logger.debug("Embedding generated (%d dims).", len(vector))
        return list(vector)

    return retry_with_backoff(
        _do_embed,
        max_retries=MAX_RETRIES,
        backoff_base=RETRY_BASE_DELAY,
        operation_name="embedding",
    )
```

**Update index_lecture_content to validate before use:**

```python
def index_lecture_content(
    group_number: int,
    lecture_number: int,
    content: str,
    content_type: str,
) -> int:
    """Chunk, embed, and upsert lecture content into Pinecone.

    Args:
        group_number: Training group identifier (1 or 2).
        lecture_number: Lecture sequence number (1–15).
        content: Raw text content to index.
        content_type: One of "transcript", "summary", "gap_analysis", "deep_analysis".

    Returns:
        Number of vectors successfully upserted.

    Raises:
        ValueError: If content_type is not a recognised type.
        RuntimeError: If Pinecone or Gemini API calls fail after retries.
    """
    # Validate both API keys are working
    if not _validate_pinecone_key():
        raise RuntimeError(
            "Pinecone API key is invalid or service is unavailable. "
            "Check PINECONE_API_KEY environment variable."
        )
    if not _validate_gemini_key():
        raise RuntimeError(
            "Gemini API key is invalid or service is unavailable. "
            "Check GEMINI_API_KEY environment variable."
        )

    if content_type not in CONTENT_TYPES:
        raise ValueError(
            f"Unknown content_type '{content_type}'. "
            f"Must be one of: {sorted(CONTENT_TYPES)}"
        )

    if not content.strip():
        logger.warning(
            "Empty content for g%d l%d %s — skipping.", group_number, lecture_number, content_type
        )
        return 0

    # ... rest of function continues unchanged ...
```

---

## Testing the Fixes

**Run all tests after applying fixes:**

```bash
cd /Users/tornikebolokadze/Desktop/Training\ Agent
pytest tools/tests/ -v --tb=short
```

**Test specific security areas:**

```bash
# Test server security
pytest tools/tests/test_server.py -v -k "webhook or rate_limit or cors"

# Test auth flows
pytest tools/tests/test_zoom_manager.py -v
pytest tools/tests/test_gdrive.py -v

# Test WhatsApp deduplication
pytest tools/tests/test_server.py -v -k "whatsapp"
```

---

## Deployment Checklist After Fixes

- [ ] Applied all 10 fixes above
- [ ] Tests pass: `pytest tools/tests/ -v`
- [ ] Zoom token cleanup tested locally
- [ ] Google OAuth refresh tested on Railway env var (GOOGLE_TOKEN_JSON_B64)
- [ ] HSTS header enabled in Firefox DevTools (Settings → HTTPS section)
- [ ] Sensitive data redaction tested by triggering an API error and checking logs
- [ ] WhatsApp dedup cache validated with duplicate incoming messages
- [ ] Pinecone key validation tested by temporarily invalidating the key
- [ ] Code reviewed for any missed sensitive logs
- [ ] Deployed to Railway and monitored for errors in first 24 hours

---

## References

- Python `hmac.compare_digest()`: https://docs.python.org/3/library/hmac.html#hmac.compare_digest
- Python `atexit` module: https://docs.python.org/3/library/atexit.html
- Zoom Webhook Signature: https://marketplace.zoom.us/docs/api-reference/webhook-reference#event-types
- OWASP: Sensitive Data Exposure: https://owasp.org/www-community/attacks/Log_Injection
- HSTS Header: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Strict-Transport-Security
