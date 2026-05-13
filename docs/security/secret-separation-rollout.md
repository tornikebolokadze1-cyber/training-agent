# Secret Separation Rollout Guide

**Status**: Required before next Railway deploy  
**Branch**: `fix/utf8-stdout-encoding` (Wave 2 security audit)  
**Owned by**: Wave 2B (config.py + tests)

---

## What changed

Before this PR the three webhook secrets collapsed to one:

```
PAPERCLIP_WEBHOOK_SECRET = _env("PAPERCLIP_WEBHOOK_SECRET") or WEBHOOK_SECRET
PAPERCLIP_OPENCLAW_SECRET = _env("PAPERCLIP_OPENCLAW_SECRET", PAPERCLIP_WEBHOOK_SECRET)
# server.py line ~686:
OPERATOR_WEBHOOK_SECRET or WEBHOOK_SECRET   # inline fallback
```

**Effect**: In the default Railway deployment all three "different" secrets were the same
value. Compromising n8n's webhook secret granted Paperclip + Operator access too.

After this PR:
- `PAPERCLIP_WEBHOOK_SECRET` and `PAPERCLIP_OPENCLAW_SECRET` are pure `_env(...)` calls — no fallback.
- `validate_critical_config()` enforces strength, distinctness, and presence in production.
- `OPERATOR_WEBHOOK_SECRET` is required in `IS_RAILWAY=True`; the inline `or WEBHOOK_SECRET`
  fallback in `server.py` line ~686 is flagged for Wave 2A to remove.

---

## Required actions before deploying to Railway

### Step 1 — Generate three independent secrets

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Record three different outputs.

### Step 2 — Set them on Railway

In the Railway dashboard → training-agent → Variables:

| Variable | Value | Notes |
|---|---|---|
| `WEBHOOK_SECRET` | `<new-value-1>` | Rotate this; update n8n auth header too |
| `OPERATOR_WEBHOOK_SECRET` | `<new-value-2>` | Must be different from WEBHOOK_SECRET |
| `PAPERCLIP_WEBHOOK_SECRET` | `<new-value-3>` | Must be different from the other two |
| `PAPERCLIP_OPENCLAW_SECRET` | `<new-value-4>` (optional) | If OpenClaw gateway is active |

### Step 3 — Update n8n

Any n8n workflow that sends `Authorization: Bearer <WEBHOOK_SECRET>` to this service must
be updated to the new `WEBHOOK_SECRET` value.

### Step 4 — Update Paperclip registration

In the Paperclip company registration (AI Pulse Georgia), update the agent's auth secret
to the new `PAPERCLIP_WEBHOOK_SECRET` value.

### Step 5 — Deploy

Push to `main` or trigger Railway deploy. The new `validate_critical_config()` will raise
`RuntimeError` at startup if any of the three secrets is missing or shares a value with
another — the deploy will fail safely before accepting traffic.

---

## CI/CD considerations

The GitHub Actions workflow sets `WEBHOOK_SECRET: test` but does NOT set
`OPERATOR_WEBHOOK_SECRET` or `PAPERCLIP_WEBHOOK_SECRET`. This is intentional:

- `IS_RAILWAY` is `False` in CI (no `RAILWAY_ENVIRONMENT` env var).
- In non-Railway mode, `validate_critical_config()` degrades missing/weak/identical
  secrets to **warnings** instead of errors, so existing tests continue to pass.
- This means the CI environment remains a single-secret setup. That is acceptable
  for local/CI testing. It is NOT acceptable for production.

If you want to test production-mode validation in CI, add all three secrets to the
GitHub Actions secrets store and pass them as env vars in the CI workflow.

---

## Known test regression in test_config.py (requires manual update)

`tools/tests/test_config.py::TestValidateCriticalConfig::test_all_vars_present_returns_empty_warnings`
fails after this PR because it sets `WEBHOOK_SECRET="s3cr3t"` (6 chars) and
`OPERATOR_WEBHOOK_SECRET="op-secret"` (9 chars) — both now fail the 32-char minimum.

This test is **not owned by Wave 2B** (it is in test_config.py, shared ownership). The fix
is to update those two values to 32+ char strings, for example:

```python
patch.object(cfg, "WEBHOOK_SECRET", "s" * 40),
patch.object(cfg, "OPERATOR_WEBHOOK_SECRET", "o" * 40),
patch.object(cfg, "PAPERCLIP_WEBHOOK_SECRET", "p" * 40),
```

The test also needs `PAPERCLIP_WEBHOOK_SECRET` patched to suppress the "not set" warning
(since the new validation always warns when Paperclip secret is absent).

Until this test is updated it will produce 1 failure in CI. The failure is a **test
data problem**, not a regression in production behavior.

---

## Residual issue: server.py inline fallback (Wave 2A task)

`tools/app/server.py` around line 686 still contains:

```python
_verify_bearer_secret(
    authorization,
    OPERATOR_WEBHOOK_SECRET or WEBHOOK_SECRET,   # ← this fallback must be removed
    "OPERATOR_WEBHOOK_SECRET",
)
```

This file is owned by Wave 2A. After Wave 2A removes the `or WEBHOOK_SECRET` fallback,
the operator endpoints will start rejecting requests when `OPERATOR_WEBHOOK_SECRET` is
empty. In production this is the correct behavior (we require it to be set). In dev/CI,
the fallback at the call site currently compensates for the missing env var.

Once Wave 2A removes the fallback:
- CI tests that hit operator endpoints with only `WEBHOOK_SECRET` set will start failing
  unless those tests are updated to provide `OPERATOR_WEBHOOK_SECRET`.
- The test matrix in `test_server.py` and `test_admin_routes.py` will need updating.

**Do not merge Wave 2A's changes before setting `OPERATOR_WEBHOOK_SECRET` on Railway.**

---

## Security model summary

Each secret has its own threat model and rotation schedule:

| Secret | Callers | Rotation trigger |
|---|---|---|
| `WEBHOOK_SECRET` | n8n, Zoom (via CRC), direct `/process-recording` | n8n credential rotation or breach |
| `OPERATOR_WEBHOOK_SECRET` | Operator dashboard, manual-trigger, retry-latest | Operator access change |
| `PAPERCLIP_WEBHOOK_SECRET` | Paperclip company platform | Paperclip re-registration |
| `PAPERCLIP_OPENCLAW_SECRET` | OpenClaw / CRO gateway | Gateway re-key |

Compromising one secret must not grant access to surfaces protected by another.
The validation in `validate_critical_config()` enforces this at startup.
