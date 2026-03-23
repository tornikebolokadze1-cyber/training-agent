# Security Rules — Training Agent

## Secrets Management

### Absolute Prohibitions
- NEVER hardcode API keys, passwords, tokens, or secrets in source code.
- NEVER commit `.env`, `credentials.json`, `token.json`, or `*.pem` to git.
- NEVER log secrets (passwords, tokens, PII, credit card numbers) in any form.
- NEVER put secrets in URL query parameters.
- NEVER store secrets in frontend/client-side code or HTML templates.

### Required Practices
- All secrets loaded from `.env` via `python-dotenv` — use `os.environ["KEY"]` with startup validation.
- Every secret must be validated at application startup (`tools/core/config.py`). Fail fast if missing.
- Use different secrets per environment (development, staging, production).
- `.gitignore` must exclude: `.env`, `.env.*`, `*.pem`, `*.key`, `credentials.json`, `token.json`, `serviceAccountKey.json`, `secrets/`.
- `.env.example` must exist with placeholder values (never real secrets).

---

## OWASP Top 10 — Auto-Enforcement

### A01: Broken Access Control
- Every FastAPI endpoint must check authentication and authorization.
- WEBHOOK_SECRET validation on ALL incoming requests.
- Exception: `/zoom-webhook` uses Zoom's own HMAC-SHA256 signature verification (not custom header).
- `/health` and `/status` endpoints may be public but must not expose sensitive data.
- Never expose internal IDs without ownership checks.

### A02: Cryptographic Failures
- All traffic over HTTPS/TLS 1.2+ in production (Railway enforces this).
- Passwords hashed with bcrypt (cost 12+) or argon2id — never plaintext.
- Use `secrets` module for random token generation, never `random`.
- Sensitive data at rest encrypted (AES-256-GCM) when stored locally.

### A03: Injection
- ALWAYS use parameterized queries if raw SQL is used. Prefer ORM methods.
- Sanitize all user input rendered in HTML responses.
- Never pass user input to shell commands. If unavoidable, use strict allowlists.
- Never use `eval()`, `exec()`, or `compile()` with user input.

### A04: Insecure Design
- Rate limit all endpoints (see Rate Limiting section below).
- Validate all webhook signatures before processing payloads.
- Never trust client-side validation alone — always validate server-side with Pydantic.

### A05: Security Misconfiguration
- Disable debug mode in production (`DEBUG=false`).
- Remove `/docs` endpoint in production (already configured in server.py).
- Set security headers on every response (see Security Headers below).
- Disable verbose error messages — never expose stack traces to clients.

### A06: Vulnerable Components
- Run `pip audit` after every dependency install.
- Pin dependency versions in `requirements.txt` with exact versions.
- Never use deprecated or unmaintained packages.
- Review new packages: check PyPI downloads, last update, known CVEs.

### A07: Authentication Failures
- Session tokens must be cryptographically random (128+ bits via `secrets.token_urlsafe()`).
- Zoom S2S OAuth tokens: refresh before expiry, never cache indefinitely.
- Google OAuth2 refresh tokens: store securely, validate on each use.
- Implement brute-force protection on any login-like endpoints.

### A09: Logging and Monitoring Failures
- Log all authentication events and webhook signature failures.
- Log all access control failures (unauthorized access attempts).
- Include: timestamp, request_id, operation, source IP.
- NEVER log: passwords, API keys, tokens, PII, recording content.
- Use structured logging (JSON format) in production.
- `alert_operator()` for critical security events via WhatsApp.

### A10: SSRF
- Validate all URLs provided in webhook payloads.
- Block requests to internal IP ranges (10.x, 172.16-31.x, 192.168.x, 127.x).
- Zoom recording download URLs: validate they match `*.zoom.us` domains.

---

## Input Validation

### Pydantic at All Boundaries
- Every FastAPI endpoint uses Pydantic models for request validation.
- Webhook payloads validated with strict Pydantic schemas.
- WhatsApp incoming messages: sanitize before processing (strip HTML, limit length).
- File paths: validate against path traversal (`../` attacks).

### Webhook Signature Validation
```
# Standard endpoints: validate Authorization header
Authorization: Bearer <WEBHOOK_SECRET>

# Zoom webhook: validate Zoom HMAC-SHA256 signature
x-zm-signature header verified against ZOOM_WEBHOOK_SECRET_TOKEN
```
- Reject requests with missing or invalid signatures immediately (401/403).
- Log signature failures for monitoring.

---

## Security Headers (FastAPI Middleware)

Every response must include:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()`

---

## Rate Limiting

| Endpoint Type | Limit |
|---|---|
| Webhook endpoints | 30 requests/minute per IP |
| Health/status | 60 requests/minute per IP |
| WhatsApp assistant | 10 requests/minute per user |
| Recording processing trigger | 5 requests/minute per IP |
| All other endpoints | 100 requests/minute per IP |

---

## CORS Configuration

- Production: explicit allowlist of permitted origins (Railway domain, n8n instance).
- NEVER use wildcard `Access-Control-Allow-Origin: *` in production.
- Development: localhost origins only.
- Restrict methods to those actually used (POST for webhooks, GET for health).

---

## File Upload / Download Security

- Recording downloads from Zoom: validate URL domain (`*.zoom.us`).
- Use resumable uploads for files over 10MB to Google Drive.
- Temporary files in `.tmp/`: auto-clean after processing.
- Never serve uploaded files directly — always through authenticated endpoints.
- Validate file types by magic bytes, not just extension.
- Maximum file size: 5GB for recordings (Zoom limit), 50MB for other uploads.

---

## Error Handling

- Production: return generic error messages with unique error ID.
- Never expose: stack traces, file paths, SQL queries, internal IPs, config values.
- Log full error details server-side with the same error ID.
- Use custom exception classes per domain (`WebhookValidationError`, `RecordingDownloadError`, etc.).

---

## Network Security

- HTTPS mandatory for all external communication.
- Secure cookies: `Secure`, `HttpOnly`, `SameSite=Strict`.
- WebSocket connections (if any): authenticate during handshake, use `wss://`.
- Internal service communication: validate source even between n8n and Python server.

---

## Incident Response

If a potential security issue is detected during development:
1. STOP the current task immediately.
2. ALERT the user clearly in Georgian: "უსაფრთხოების პრობლემა აღმოვაჩინე."
3. EXPLAIN the risk in simple terms.
4. PROVIDE specific fix steps.
5. DO NOT proceed until the user acknowledges.
6. For critical issues: `alert_operator()` immediately.
