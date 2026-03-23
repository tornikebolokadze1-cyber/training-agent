# Code Quality Rules — Training Agent

## Python Version & Type Safety

- Python 3.12+ required.
- Type hints on ALL function signatures: parameters AND return types.
- Use `typing.Protocol` for duck-typed interfaces.
- Use `@dataclass(frozen=True)` for immutable data objects.
- Use Pydantic `BaseModel` for API request/response validation.
- Never use `Any` — use `Unknown` patterns and narrow with type guards.
- Validate external data (webhook payloads, API responses) with Pydantic at boundaries.

---

## Linting & Formatting

### Ruff (replaces pylint, flake8, isort, black)
- `ruff check` — zero errors required before commit.
- `ruff format` — 88 character line length (black-compatible).
- Import ordering: stdlib, third-party, local (isort-compatible rules).
- Run automatically before every commit.

### Pre-Commit Checks (Mandatory)
1. `ruff check --fix` — auto-fix safe lint issues
2. `ruff format` — apply formatting
3. Secret scan (gitleaks/detect-secrets) — block if secrets found
4. `python -m pytest tools/tests/ -x` — all tests must pass
5. Type check if mypy is configured — no errors

---

## Git Workflow

### Branch Strategy
- `main` — production-ready code only (deployed to Railway)
- `develop` — integration branch for testing
- `feature/description` — new features (e.g., `feature/add-attendance-tracking`)
- `fix/description` — bug fixes (e.g., `fix/zoom-webhook-timeout`)
- `hotfix/description` — emergency production fixes

### Conventional Commits
Format: `type: description`

Types:
- `feat:` — new feature (e.g., `feat: add lecture gap analysis report`)
- `fix:` — bug fix (e.g., `fix: prevent duplicate recording processing`)
- `refactor:` — restructure without behavior change
- `docs:` — documentation updates
- `test:` — test additions or fixes
- `chore:` — maintenance (dependency updates, config changes)
- `perf:` — performance improvement
- `ci:` — CI/CD pipeline changes

Never commit directly to main. Always use feature branches and PRs.

---

## File & Function Organization

### File Size Limits
- Target: 200-400 lines per file.
- Maximum: 800 lines. Extract into separate modules when larger.
- One primary concept per file (one service, one integration, one model).

### Function Size Limits
- Functions under 50 lines — extract when larger.
- One responsibility per function.
- Maximum 4 levels of nesting — extract to helper functions.
- Name functions by what they DO: `download_recording()`, `validate_webhook_signature()`, `send_group_notification()`.

### Project Structure (Existing — Follow This Pattern)
```
tools/
  core/           # Shared config, constants
  integrations/   # External service clients (Zoom, Drive, Gemini, etc.)
  services/       # Business logic (transcription, analytics, assistant)
  app/            # Entry points (server, scheduler, orchestrator)
  tests/          # Test files
```

---

## Immutability

- Use `@dataclass(frozen=True)` for data objects that should not change.
- Never use mutable default arguments: `def f(items: list = None)` — use `def f(items: list | None = None)`.
- Prefer tuple over list for fixed collections.
- Create new objects instead of mutating existing ones.

---

## Logging

- Use `logging.getLogger(__name__)` — never `print()` in production.
- Log levels:
  - `ERROR` — something broke, needs attention
  - `WARNING` — degraded state, may need attention
  - `INFO` — lifecycle events (server start, recording processed, lecture analyzed)
  - `DEBUG` — development details (API call params, intermediate results)
- Include: timestamp, module name, operation, duration for timed operations.
- NEVER log: passwords, API keys, tokens, recording content, student PII.
- Structured format for production: JSON via logging config.
- Rotating file handler: 10 MB x 5 files (already configured in project).

---

## Error Handling

### Custom Exceptions
- Define domain-specific exception classes:
  - `WebhookValidationError` — invalid signatures or payloads
  - `RecordingDownloadError` — Zoom download failures
  - `AnalysisPipelineError` — Gemini/Claude analysis failures
  - `DriveUploadError` — Google Drive upload failures
  - `NotificationError` — WhatsApp/email delivery failures
- Never bare `except:` — always catch specific exceptions.
- Wrap errors with context: `raise AnalysisPipelineError(f"Failed to analyze lecture {lecture_id}: {e}") from e`.

### Retry Strategy
- External API calls: retry with exponential backoff (3 attempts, 1s/2s/4s delays).
- Use `tenacity` library for retry logic.
- Zoom recording downloads: retry up to 5 times (large files, network issues).
- Never retry on authentication errors (401/403) — fail immediately.

---

## Secrets & Configuration

- All secrets from `.env` via `python-dotenv`.
- Centralized config in `tools/core/config.py`.
- Validate all required env vars at startup — fail fast if missing.
- Zero hardcoded values for: URLs, ports, API keys, folder IDs, group chat IDs.
- Georgian text (prompts, templates) can be hardcoded in config — they are not secrets.

---

## Docstrings

- Google style docstrings for public APIs only.
- Not needed for: private methods, obvious one-liners, test functions.
- Format:
```python
def analyze_lecture(video_path: Path, group: int) -> AnalysisResult:
    """Analyze a lecture recording using the Gemini + Claude pipeline.

    Args:
        video_path: Path to the downloaded recording file.
        group: Training group number (1 or 2).

    Returns:
        AnalysisResult with transcription, summary, and gap analysis.

    Raises:
        AnalysisPipelineError: If any stage of the pipeline fails.
    """
```

---

## Code Quality Checklist (Before Every Commit)

- [ ] All functions < 50 lines
- [ ] All files < 800 lines
- [ ] Type hints on all function signatures
- [ ] No bare except clauses
- [ ] No print() statements (use logging)
- [ ] No hardcoded secrets or config values
- [ ] ruff check passes with zero errors
- [ ] ruff format applied
- [ ] All tests pass
- [ ] Commit message follows conventional format
