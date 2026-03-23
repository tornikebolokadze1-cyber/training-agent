# Testing Rules — Training Agent

## Framework & Setup

- **Framework**: pytest with pytest-asyncio for async tests.
- **Run command**: `python -m pytest tools/tests/ -v`
- **Coverage**: `python -m pytest tools/tests/ --cov=tools --cov-report=term-missing`
- **Test location**: `tools/tests/` directory, following existing patterns.
- **Naming**: `test_*.py` files, `test_*` functions, `Test*` classes.

---

## Automatic Test Triggers (8 Mandatory Triggers)

Claude must write/run tests automatically when any of these happen:

| # | Trigger | Action |
|---|---------|--------|
| 1 | New function or class created | Write 1+ tests: happy path + 1 error case |
| 2 | Bug fix requested | Write regression test FIRST (RED), then fix (GREEN) |
| 3 | API endpoint created or modified | Test status codes, response shapes, auth |
| 4 | Webhook handler modified | Test signature validation, payload parsing, error handling |
| 5 | Dependency installed or updated | Run full test suite to verify nothing broke |
| 6 | External integration changed (Zoom, Drive, Gemini, WhatsApp) | Verify mocks still match expected API |
| 7 | Config or environment variable added | Test startup validation (missing var = fail fast) |
| 8 | Before every git commit | Run full suite; block commit if tests fail |

---

## Coverage Targets

| Code Category | Target |
|---|---|
| New files | 80%+ line coverage |
| Critical paths (webhooks, auth, recording pipeline) | 90%+ coverage |
| Utility functions (config, helpers) | 100% coverage |
| Integration clients (Zoom, Drive, Gemini) | 80%+ with mocked APIs |

---

## What to Test

### Happy Path (Every Function)
- Function returns expected output for valid input.
- Async functions resolve correctly.
- Pipeline stages produce expected intermediate results.

### Error Handling (Every Function)
- Invalid input: None, empty string, wrong type.
- External API failures: timeouts, 401s, 500s, rate limits.
- Missing environment variables at startup.
- Malformed webhook payloads.

### Edge Cases (Utilities & Config)
- Empty inputs, boundary values, Unicode/Georgian text.
- Very large files (recording download edge cases).
- Concurrent requests (deduplication tracker).
- Stale task recovery (>4 hour tasks).

### Specific to Training Agent
- Lecture numbering: correct group (1 or 2), correct lecture number (1-15).
- Schedule detection: correct day mapping (Tue/Fri for Group 1, Mon/Thu for Group 2).
- Georgian text handling: UTF-8 everywhere, folder names in Georgian script.
- Gemini prompt integrity: prompts remain in Georgian, not accidentally translated.
- Recording deduplication: same recording ID processed only once.

---

## What to Mock

All external services MUST be mocked in unit tests:

| Service | What to Mock | How |
|---|---|---|
| Zoom API | `zoom_manager.py` methods | `unittest.mock.patch` or `pytest-mock` |
| Google Drive | `gdrive_manager.py` methods | Mock file creation, upload, doc creation |
| Gemini API | `gemini_analyzer.py` calls | Mock transcription and analysis responses |
| Claude/Anthropic | API calls in analysis pipeline | Mock reasoning responses |
| WhatsApp (Green API) | `whatsapp_sender.py` methods | Mock send_message, send_group_message |
| Pinecone | `knowledge_indexer.py` methods | Mock upsert, query operations |
| Email (Gmail) | SMTP/OAuth calls | Mock send operations |
| File system | Large file downloads | Use small test fixtures or tmpdir |

### Mock Fixtures (Shared via conftest.py)
```python
# tools/tests/conftest.py
@pytest.fixture
def mock_zoom_client():
    """Mock Zoom API client with standard responses."""

@pytest.fixture
def mock_gdrive_client():
    """Mock Google Drive client."""

@pytest.fixture
def sample_webhook_payload():
    """Valid Zoom recording webhook payload."""

@pytest.fixture
def sample_whatsapp_message():
    """Valid WhatsApp incoming message."""
```

---

## Integration Tests

### API Endpoint Tests (FastAPI TestClient)
For every endpoint in `app/server.py`:
- `GET /health` — returns 200 with status info
- `GET /status` — returns 200 with system status
- `POST /zoom-webhook` — validates Zoom HMAC signature, returns 200
- `POST /zoom-webhook` — rejects invalid signature with 401
- `POST /zoom-webhook` — CRC challenge returns correct response
- `POST /process-recording` — validates WEBHOOK_SECRET, processes recording
- `POST /whatsapp-webhook` — validates signature, processes message

### Test Patterns
```python
from fastapi.testclient import TestClient

def test_health_endpoint(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert "status" in response.json()

def test_webhook_rejects_invalid_signature(client: TestClient):
    response = client.post("/zoom-webhook", json={"event": "test"})
    assert response.status_code in (401, 403)

def test_webhook_handles_crc_challenge(client: TestClient):
    payload = {"event": "endpoint.url_validation", "payload": {"plainToken": "test"}}
    response = client.post("/zoom-webhook", json=payload)
    assert response.status_code == 200
    assert "plainToken" in response.json()
```

---

## Webhook-Specific Tests

### Zoom Webhook
- Valid HMAC-SHA256 signature accepted.
- Invalid/missing signature rejected (401/403).
- CRC challenge: correct `hashToken` returned for `plainToken`.
- `recording.completed` event: triggers processing pipeline.
- Duplicate recording ID: rejected by deduplication tracker.
- Malformed payload: returns 400 with generic error (no internals exposed).

### WhatsApp Webhook
- Valid message triggers assistant response.
- "მრჩეველო" trigger word activates AI assistant.
- Malformed message body: handled gracefully.
- Rate limiting: excessive messages from same user throttled.

---

## Pre-Commit Testing

Before every commit, this sequence runs automatically:
1. `ruff check tools/` — lint must pass
2. `ruff format --check tools/` — format must be clean
3. `python -m pytest tools/tests/ -x -q` — all tests must pass
4. Check for `print()` statements in non-test code
5. Check for hardcoded secrets patterns

Rules:
- NEVER use `--no-verify` to skip pre-commit hooks.
- NEVER commit with failing tests.
- NEVER delete test files to make tests "pass."
- If a test is wrong, fix the test AND document why.
- If fixing takes 3+ attempts, inform the user before proceeding.

---

## Communication with Non-Technical Users

### Never Show
- pytest output, tracebacks, assertion errors
- Coverage percentages or reports
- Test file names or function names

### Always Say Instead
- "შევამოწმე და ყველაფერი სწორად მუშაობს."
- "Zoom webhook-ის დამუშავება შევამოწმე — კარგად მუშაობს."
- "პრობლემა ვიპოვე: [მარტივი ახსნა]. ახლა ვასწორებ."
- "ჩანაწერის ანალიზის პროცესი ვერიფიცირებულია — ყველა ეტაპი მუშაობს."

### Georgian Approval Prompt
After verification: "ნახე შედეგი — კარგად გამოიყურება?"

---

## Test Maintenance

- When modifying existing code: run existing tests FIRST to establish baseline.
- Update tests when behavior intentionally changes.
- Remove tests for deleted features.
- Keep test files organized: mirror source structure.
- Never leave commented-out tests.
- Shared fixtures go in `tools/tests/conftest.py`.
