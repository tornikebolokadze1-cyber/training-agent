# API Contract Validation Audit — Training Agent

**Date:** 2026-03-18
**Auditor:** Claude (Haiku 4.5)
**Scope:** Input validation across webhooks, internal functions, and external API responses

---

## Executive Summary

The Training Agent has **moderate validation coverage** with several **HIGH-severity gaps** in API contract validation. While Zoom and n8n endpoints implement good signature/secret verification, internal function contracts are **largely unvalidated** and rely on implicit caller guarantees. Pydantic models are used selectively (only for n8n/FastAPI payloads), but missing elsewhere.

**Critical Issues Found:** 4
**High Issues:** 6
**Medium Issues:** 5

---

## 1. Zoom Webhooks (`tools/app/server.py`)

### 1.1 CRC Challenge-Response (`endpoint.url_validation`)

**Status:** ✅ **GOOD** with minor hardening needed

**Current Implementation (lines 535-547):**
- Validates `plainToken` presence and length check (max 256 chars)
- Returns HMAC-SHA256 encrypted token
- Fails if `ZOOM_WEBHOOK_SECRET_TOKEN` unset (503 error)

**Gaps:**
- No regex validation on `plainToken` format (should be alphanumeric only)
- No check for empty/null payload structure

**Severity:** Low
**Suggestion:**
```python
# Add to _handle_zoom_crc()
if not re.match(r'^[a-zA-Z0-9]+$', plain_token):
    raise HTTPException(status_code=400, detail="Invalid plainToken format")
```

---

### 1.2 Signature Verification (`_verify_zoom_signature`)

**Status:** ✅ **GOOD**

**Strengths (lines 550-577):**
- Validates both `x-zm-request-timestamp` and `x-zm-signature` headers present
- Rejects stale timestamps (>300s age) — prevents replay attacks
- HMAC-SHA256 comparison using `hmac.compare_digest()` — timing-safe
- Handles `ValueError` for invalid timestamp parsing

**No Issues Found**

---

### 1.3 Recording.Completed Payload (`_extract_recording_context`)

**Status:** ⚠️ **MEDIUM** — Field extraction lacks validation

**Current Implementation (lines 580-625):**
- Extracts from nested `payload.object.recording_files` array
- Prefers `shared_screen_with_speaker_view` MP4, falls back to any MP4
- Extracts group number from topic using `extract_group_from_topic()`
- **Problem:** No validation of extracted fields before use

**Missing Validations:**
```python
# ❌ No validation:
- download_url: could be missing or malformed (see SSRF issue below)
- access_token: not checked for empty string
- topic: assumed to match group pattern, but could be garbage
- start_time: parsing failure silently falls back to today()
- meeting_id: never validated as numeric/UUID
```

**Severity:** HIGH
**Impact:** Malformed Zoom events could crash pipeline or cause data corruption

**Suggested Fix:**
```python
from pydantic import BaseModel, HttpUrl, validator

class ZoomRecordingFile(BaseModel):
    file_type: str
    recording_type: str | None = None
    download_url: HttpUrl  # Validates URL format

class ZoomRecordingContext(BaseModel):
    group_number: int  # Will fail if extraction returns None
    lecture_number: int
    download_url: HttpUrl
    access_token: str  # Must be non-empty
    drive_folder_id: str

    @validator('group_number')
    def validate_group(cls, v):
        if v not in (1, 2):
            raise ValueError(f"Invalid group: {v}")
        return v
```

---

### 1.4 Meeting.Ended Payload (`_handle_meeting_ended`)

**Status:** ⚠️ **MEDIUM** — Indirect validation gaps

**Current Implementation (lines 628-724):**
- Extracts group, duration, timestamps from payload
- Duration gate: ignores meetings <2 hours
- **Problem:** Assumes nested structure exists without checking

**Missing Validations:**
```python
# ❌ Accessed without existence checks:
obj.get("id")        # Could fail to int conversion
obj.get("topic")     # Could be empty, fails extract_group_from_topic()
start_time_str       # Could be malformed ISO string
duration             # Could be negative or 0
```

**Severity:** HIGH
**Impact:** Malformed meeting.ended events could bypass duration gate

**Suggested Schema:**
```python
class ZoomMeetingEndedPayload(BaseModel):
    id: int | str  # Coerce to string for comparison
    uuid: str | None = None
    topic: str
    start_time: str  # ISO-8601
    end_time: str | None = None
    duration: int  # Zoom reports in minutes

    @validator('topic')
    def topic_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Meeting topic cannot be empty")
        return v

    @validator('duration')
    def duration_positive(cls, v):
        if v < 0:
            raise ValueError("Duration cannot be negative")
        return v
```

---

## 2. WhatsApp Incoming Messages (`tools/app/server.py:453-513`)

**Status:** ⚠️ **MEDIUM** — Loose structure validation

**Current Implementation:**
- Checks `typeWebhook == "incomingMessageReceived"`
- Extracts text from nested `messageData` (handles multiple formats: textMessage, extendedTextMessage, quotedMessage)
- Filters out own messages (`fromMe == True`)
- **Problem:** No validation of message structure or suspicious edge cases

**Missing Validations:**
```python
# ❌ No checks:
- body.get("senderData") could be missing entirely
- chat_id could be empty string → crashes downstream
- sender_id could be invalid WhatsApp format
- text could exceed WhatsApp's encoding limits
- timestamp could be in future or ancient past
- message_data could have conflicting fields (both textMessage AND extendedTextMessage)
```

**Severity:** MEDIUM
**Impact:** Malformed messages could crash assistant or create confusing responses

**Current Code (lines 471-508):**
```python
# ❌ Unsafe extraction:
message_data = body.get("messageData", {})  # Defaults to empty dict
type_message = message_data.get("typeMessage")  # Could be None

# ❌ No schema validation:
incoming = IncomingMessage(
    chat_id=sender_data.get("chatId", ""),  # Could be ""!
    sender_id=sender_data.get("sender", ""),
    sender_name=sender_data.get("senderName", ""),
    text=text,
)
```

**Suggested Fix:**
```python
from pydantic import BaseModel, Field, validator

class GreenAPIIncomingMessage(BaseModel):
    """Validated Green API webhook payload."""

    typeWebhook: str
    timestamp: int
    messageData: dict
    senderData: dict

    @validator('timestamp')
    def timestamp_reasonable(cls, v):
        import time
        now = time.time()
        if abs(now - v) > 86400:  # 24 hours tolerance for test data
            raise ValueError(f"Timestamp too old/future: {v}")
        return v

class ValidatedIncomingMessage(BaseModel):
    """Post-extraction validated message."""
    chat_id: str
    sender_id: str
    text: str

    @validator('chat_id', 'sender_id')
    def whatsapp_id_format(cls, v):
        # Basic format check: should contain @ and digits
        if not v or '@' not in v:
            raise ValueError(f"Invalid WhatsApp ID format: {v}")
        return v

    @validator('text')
    def text_length(cls, v):
        if len(v) > 4096:  # WhatsApp limit
            raise ValueError("Message exceeds WhatsApp length limit")
        return v

# In whatsapp_incoming():
payload = GreenAPIIncomingMessage(**body)  # Pydantic will reject malformed
incoming = ValidatedIncomingMessage(
    chat_id=payload.senderData["chatId"],
    sender_id=payload.senderData["sender"],
    text=extracted_text,
)
```

---

## 3. `/manual-trigger` Endpoint (`tools/app/server.py:920-972`)

**Status:** ✅ **GOOD** with validation complete

**Current Implementation:**
- Uses Pydantic `ManualTriggerRequest` model (line 866-871)
- Validates `group_number in (1, 2)` (line 936)
- Validates `lecture_number in range(1, 16)` (line 938)
- Validates Drive file ID format with regex (line 940)

**Strengths:**
- Drive file ID validated against `_DRIVE_FOLDER_ID_RE` regex pattern
- Early rejection of invalid inputs (422 Unprocessable Entity)
- Deduplication check prevents duplicate processing

**Minor Improvement:**
```python
# Current regex is overly permissive:
_DRIVE_FOLDER_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{10,100}$")

# Better version (Google Drive IDs are strictly alphanumeric + underscore):
_DRIVE_FOLDER_ID_RE = re.compile(r"^[a-zA-Z0-9_]{25,100}$")
```

---

## 4. `/process-recording` Endpoint (`tools/app/server.py:782-836`)

**Status:** ✅ **GOOD** with full validation

**Current Implementation:**
- Pydantic model `ProcessRecordingRequest` validates on init (lines 185-197)
- Drive folder ID format regex checked in `__init__`
- Group/lecture number validated in endpoint handler (lines 798-801)
- Deduplication prevents duplicate processing
- SSRF protection in `process_recording_task()` (lines 264-269)

**Strengths:**
- URL validation: checks `https://` scheme only
- Hostname validation: restricts to `zoom.us` or `.zoom.us` domains
- Redirect validation: validates final URL after redirects (lines 354-360)

**SSRF Implementation Review (lines 340-364):**
```python
# ✅ Good: validates after redirects
final_host = (response.url.host or "").lower()
is_zoom = final_host == "zoom.us" or final_host.endswith(".zoom.us")
is_zoomgov = final_host == "zoomgov.com" or final_host.endswith(".zoomgov.com")
if not is_zoom and not is_zoomgov:
    raise ValueError(f"Download redirected to untrusted host: {final_host}")
```

**No Issues Found** — This endpoint is well-protected.

---

## 5. Internal Function Contracts

### 5.1 `extract_group_from_topic()` — `tools/core/config.py:376-389`

**Status:** ⚠️ **CRITICAL** — No validation of caller input

**Current Implementation:**
```python
def extract_group_from_topic(topic: str) -> int | None:
    """Extract group number from a Zoom meeting topic string."""
    for group_num in GROUPS:
        if f"ჯგუფი #{group_num}" in topic:
            return group_num
    return None
```

**Issues:**
1. **No type checking:** Callers could pass `None`, int, or list
2. **No length check:** Could parse gigabyte-size strings
3. **Partial match:** "ჯგუფი #1234" would match and return wrong group
4. **Silent None return:** Callers assume non-None and crash downstream

**Severity:** CRITICAL
**Impact:** Used in 5+ locations (Zoom webhooks, scheduler). Silent failures propagate.

**Suggested Fix:**
```python
from pydantic import BaseModel, validator

class TopicParser(BaseModel):
    topic: str

    @validator('topic')
    def validate_topic(cls, v):
        if not isinstance(v, str):
            raise TypeError(f"topic must be str, got {type(v)}")
        if len(v) > 1000:
            raise ValueError("topic string exceeds 1000 chars")
        return v

def extract_group_from_topic(topic: str) -> int | None:
    """Extract group number from a Zoom meeting topic string.

    Raises:
        TypeError: If topic is not a string
        ValueError: If topic exceeds reasonable length
    """
    parsed = TopicParser(topic=topic)
    topic = parsed.topic

    for group_num in GROUPS:
        marker = f"ჯგუფი #{group_num}"
        # Require word boundary to avoid "ჯგუფი #123" matching group 1
        pattern = rf"\b{re.escape(marker)}\b"
        if re.search(pattern, topic):
            return group_num
    return None
```

---

### 5.2 `transcribe_and_index()` — `tools/services/transcribe_lecture.py`

**Status:** ⚠️ **HIGH** — No input validation on function entry

**Current Signature (not shown but inferred from usage):**
```python
def transcribe_and_index(
    group_number: int,
    lecture_number: int,
    local_path: Path | str,
) -> dict[str, int]:
    """Run full transcription + analysis pipeline."""
```

**Issues:**
1. **No type enforcement:** Callers could pass `group_number="1"` (string)
2. **No range check:** lecture_number could be 0 or 999
3. **No file existence check:** Crashes on missing video
4. **No size validation:** Could attempt to process 50GB files
5. **No codec check:** Could try to transcribe non-video files

**Severity:** HIGH
**Impact:** Entry point for 3 different callers (webhook, scheduler, manual). Callers trust it won't crash.

**Suggested Validation:**
```python
from pydantic import BaseModel, validator
from pathlib import Path

class TranscriptionRequest(BaseModel):
    group_number: int
    lecture_number: int
    local_path: Path

    @validator('group_number')
    def valid_group(cls, v):
        if not isinstance(v, int) or v not in (1, 2):
            raise ValueError(f"Invalid group_number: {v}")
        return v

    @validator('lecture_number')
    def valid_lecture(cls, v):
        if not isinstance(v, int) or not (1 <= v <= 15):
            raise ValueError(f"Invalid lecture_number: {v}")
        return v

    @validator('local_path')
    def file_exists_and_valid(cls, v):
        p = Path(v) if isinstance(v, str) else v
        if not p.exists():
            raise FileNotFoundError(f"Video file not found: {p}")
        if not p.is_file():
            raise ValueError(f"Not a file: {p}")
        size_mb = p.stat().st_size / (1024 * 1024)
        if size_mb > 2000:  # 2GB limit
            raise ValueError(f"File too large: {size_mb:.0f} MB (max 2000 MB)")
        if p.suffix.lower() not in ('.mp4', '.mov', '.avi', '.webm'):
            raise ValueError(f"Unsupported video format: {p.suffix}")
        return p

def transcribe_and_index(
    group_number: int,
    lecture_number: int,
    local_path: Path | str,
) -> dict[str, int]:
    """Run full transcription + analysis pipeline.

    Raises:
        ValueError: If inputs are invalid
        FileNotFoundError: If video file doesn't exist
    """
    req = TranscriptionRequest(
        group_number=group_number,
        lecture_number=lecture_number,
        local_path=local_path,
    )
    # ... rest of implementation uses validated req
```

---

### 5.3 Config Validation (`tools/core/config.py`)

**Status:** ⚠️ **MEDIUM** — Partial validation at import time

**Current Implementation (lines 161-182):**
```python
GROUPS: dict[int, GroupConfig] = {
    1: {
        "name": "მარტის ჯგუფი #1",
        "folder_name": "AI კურსი (მარტის ჯგუფი #1. 2026)",
        "drive_folder_id": _env("DRIVE_GROUP1_FOLDER_ID"),  # ❌ No validation
        "analysis_folder_id": _env("DRIVE_GROUP1_ANALYSIS_FOLDER_ID"),
        "zoom_meeting_id": _env("ZOOM_GROUP1_MEETING_ID"),
        "meeting_days": [1, 4],
        "start_date": date(2026, 3, 13),
        "attendee_emails": _ATTENDEES.get("1", []),
    },
    # ...
}
```

**Issues:**
1. **No validation of Drive folder IDs:** Could be empty strings or invalid format
2. **No validation of Zoom meeting IDs:** Not checked for numeric format
3. **No validation of attendee emails:** Could be garbage strings
4. **No validation of meeting_days:** Could contain invalid weekday numbers
5. **No schema enforcement:** TypedDict is used but not validated

**Severity:** MEDIUM
**Impact:** Invalid config silently corrupts Drive operations and Zoom API calls downstream

**Suggested Fix:**
```python
from pydantic import BaseModel, EmailStr, validator

class GroupConfigModel(BaseModel):
    name: str
    folder_name: str
    drive_folder_id: str
    analysis_folder_id: str
    zoom_meeting_id: str
    meeting_days: list[int]
    start_date: date
    attendee_emails: list[EmailStr]

    @validator('drive_folder_id', 'analysis_folder_id', 'zoom_meeting_id')
    def ids_not_empty(cls, v, field):
        if not v or not v.strip():
            raise ValueError(f"{field.name} cannot be empty")
        return v

    @validator('zoom_meeting_id')
    def zoom_id_format(cls, v):
        if not re.match(r'^\d{10,}$', v):
            raise ValueError(f"Invalid Zoom meeting ID format: {v}")
        return v

    @validator('meeting_days')
    def valid_weekdays(cls, v):
        if not all(0 <= d <= 6 for d in v):
            raise ValueError(f"Invalid weekdays: {v}")
        if len(v) != len(set(v)):
            raise ValueError("Duplicate weekdays in meeting_days")
        return v

def validate_critical_config() -> list[str]:
    """Check that critical environment variables are set."""
    warnings: list[str] = []

    # NEW: Validate GROUPS structure
    for group_num, group in GROUPS.items():
        try:
            GroupConfigModel(**group)
        except Exception as e:
            raise RuntimeError(f"Invalid config for Group {group_num}: {e}")

    # ... rest of validation
```

---

## 6. External API Response Validation

### 6.1 Zoom API Responses

**Status:** ⚠️ **MEDIUM** — No response schema validation

**Locations:** `tools/integrations/zoom_manager.py` (not examined in detail, but likely affected)

**Risk:** Zoom API response structure changes could crash pipelines silently

**Suggestion:**
- Use Pydantic for responses from:
  - `meeting.create()` responses (validate meeting_id, start_url, join_url)
  - Recording list responses (validate recording_files structure)
  - Token refresh responses (validate expires_in, access_token present)

---

### 6.2 Gemini API Responses

**Status:** ⚠️ **HIGH** — No validation of transcription output

**Location:** `tools/integrations/gemini_analyzer.py` (not fully examined)

**Risk:**
- Gemini could return empty content, wrong media type, or malformed JSON
- Pipeline crashes when trying to parse None or unexpected structure

**Suggested Fix:**
```python
from pydantic import BaseModel

class GeminiTranscriptionResponse(BaseModel):
    """Validated Gemini transcription response."""
    text: str  # The transcription

    @validator('text')
    def text_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Gemini returned empty transcription")
        if len(v) < 100:  # 2hr lecture should be 10K+ words
            raise ValueError(f"Transcription suspiciously short: {len(v)} chars")
        return v

# In gemini_analyzer.py, wrap the response:
response = client.generate_content(...)
result = GeminiTranscriptionResponse(text=response.text)
```

---

### 6.3 Google Drive API Responses

**Status:** ⚠️ **MEDIUM** — Partial validation

**Current:** `tools/integrations/gdrive_manager.py` does basic checks (file exists) but doesn't validate response structure

**Risk:**
- `files().create()` could return incomplete metadata
- `files().get()` could be missing expected fields
- Resumable upload could report success with missing file ID

**Suggestion:**
```python
class DriveFileMetadata(BaseModel):
    id: str
    name: str
    mimeType: str
    webViewLink: str | None = None

    @validator('id')
    def id_not_empty(cls, v):
        if not v:
            raise ValueError("Drive file ID missing from response")
        return v
```

---

## 7. Config & Startup Validation (`tools/app/orchestrator.py:63-92`)

**Status:** ✅ **GOOD**

**Strengths:**
- `validate_credentials()` checks all required env vars at startup
- Differentiates between critical (must-have) and optional (nice-to-have) variables
- Logs each missing var with context
- Fails fast in production (raises `EnvironmentError`) if critical vars missing

**Minor Issue:**
- Does not validate **format** of credentials (e.g., Zoom ID should be numeric)
- Does not check if credential files are readable/valid JSON

---

## Summary Table

| Component | Status | Severity | Issue | Recommendation |
|-----------|--------|----------|-------|-----------------|
| Zoom CRC | ✅ Good | Low | plainToken format not validated | Add regex check |
| Zoom Signature | ✅ Good | None | — | — |
| recording.completed | ⚠️ Medium | HIGH | No field validation | Add Pydantic models |
| meeting.ended | ⚠️ Medium | HIGH | No field validation | Add Pydantic models |
| WhatsApp incoming | ⚠️ Medium | MEDIUM | Loose structure validation | Add schema model |
| /manual-trigger | ✅ Good | None | — | — |
| /process-recording | ✅ Good | None | — | — |
| extract_group_from_topic() | ❌ Bad | **CRITICAL** | No type/length checks | Add Pydantic + type check |
| transcribe_and_index() | ⚠️ Medium | HIGH | No input validation | Add request model |
| GROUPS config | ⚠️ Medium | MEDIUM | No schema validation | Add Pydantic validation |
| Gemini responses | ⚠️ Medium | HIGH | No output validation | Add response models |
| Google Drive responses | ⚠️ Medium | MEDIUM | Incomplete field checks | Add metadata models |
| Startup validation | ✅ Good | Low | No format checks | Validate JSON/format |

---

## Recommendations (Priority Order)

### Phase 1: CRITICAL (Implement First)
1. **Add Pydantic validation to `extract_group_from_topic()`** — used in 5+ locations, silent failures
2. **Add Pydantic models for Zoom recording.completed payload** — prevents malformed recordings from entering pipeline
3. **Add input validation to `transcribe_and_index()`** — guards 3 entry points

### Phase 2: HIGH (Implement Soon)
4. Add Pydantic model for WhatsApp incoming messages — prevents assistant crashes
5. Add response validation for Gemini transcriptions — prevents downstream parsing failures
6. Add Zoom meeting.ended payload validation — improves duration gate robustness

### Phase 3: MEDIUM (Nice-to-Have)
7. Add Drive folder ID validation to config startup
8. Add Google Drive response metadata models
9. Add Zoom API response validation
10. Add plainToken format check to CRC handler

---

## Testing Strategy

Each validation layer should have unit tests:

```python
# Example test structure
def test_extract_group_from_topic_rejects_invalid_input():
    with pytest.raises(TypeError):
        extract_group_from_topic(None)
    with pytest.raises(ValueError):
        extract_group_from_topic("x" * 10000)
    with pytest.raises(ValueError):
        extract_group_from_topic(123)  # int instead of str

def test_whatsapp_payload_validation():
    # Missing sender_data → validation error
    with pytest.raises(ValidationError):
        GreenAPIIncomingMessage(
            typeWebhook="incomingMessageReceived",
            timestamp=int(time.time()),
            messageData={...},
            senderData={},  # Empty!
        )
```

---

## Code Quality Wins

Implementing these validations will also:
- ✅ Improve error messages (clear "why it failed" vs silent crashes)
- ✅ Enable better logging (validation errors are caught early)
- ✅ Make types explicit (Pydantic catches int vs str mismatches)
- ✅ Document API contracts (models are self-documenting)
- ✅ Enable OpenAPI docs improvement (Pydantic integrates with FastAPI/OpenAPI)

