# Security Policy for Training Agent

## Overview

Training Agent processes sensitive educational content including lecture recordings,
student communications, and API credentials for multiple services (Zoom, Google Drive,
Gemini, Claude, WhatsApp). Security is a top priority.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Current `main` branch | Yes |
| Previous releases | No — always use the latest version |

Only the current code on the `main` branch receives security updates.
There are no versioned releases at this time.

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

### How to Report

**Option 1 (Preferred):** Create a private security advisory on GitHub.
Go to the repository's "Security" tab and select "Report a vulnerability."

**Option 2:** Email the maintainer directly at the address listed in the
repository's GitHub profile. Include "[SECURITY]" in the subject line.

**Do NOT** open a public GitHub issue for security vulnerabilities.

### What to Include

When reporting, please provide:

- A clear description of the vulnerability
- Steps to reproduce the issue
- The potential impact (what could an attacker do?)
- Any suggested fix, if you have one
- Your contact information for follow-up

### Response Timeline

| Action | Timeline |
|--------|----------|
| Acknowledgement of your report | Within 48 hours |
| Initial assessment and severity rating | Within 72 hours |
| Fix for critical vulnerabilities | Within 7 days |
| Fix for high-severity vulnerabilities | Within 14 days |
| Fix for medium/low vulnerabilities | Within 30 days |

## What Qualifies as a Vulnerability

The following are considered security vulnerabilities:

- **Authentication bypass**: accessing protected endpoints without valid credentials
- **Data exposure**: leaking API keys, tokens, user data, or lecture content
- **Injection attacks**: SQL injection, command injection, or code injection
- **Secrets leakage**: credentials appearing in logs, error messages, or responses
- **Unauthorized access**: accessing recordings, reports, or Drive folders without permission
- **Webhook tampering**: bypassing HMAC-SHA256 signature verification on Zoom webhooks
- **SSRF**: server-side request forgery through user-controlled URLs
- **Path traversal**: accessing files outside intended directories

## What Does NOT Qualify

The following are not considered security vulnerabilities:

- Cosmetic issues (typos, styling, formatting)
- Feature requests or enhancement suggestions
- Denial of service through excessive API calls (covered by rate limiting)
- Issues in third-party services (Zoom, Google, WhatsApp) — report those to the respective vendors
- Bugs that require physical access to the server
- Social engineering attacks against users

## Responsible Disclosure

We ask that you:

1. **Give us time to fix it** before disclosing publicly. Follow the timelines above.
2. **Do not access or modify** other users' data during your research.
3. **Do not disrupt** the service (no DoS attacks, no data destruction).
4. **Provide enough detail** for us to reproduce and fix the issue.
5. **Act in good faith** — research conducted within these guidelines will not result in legal action.

## Credit

We appreciate security researchers who help keep this project safe.
With your permission, we will credit you in the fix commit message and
in this file's acknowledgements section.

If you prefer to remain anonymous, let us know in your report.

## Security Measures in Place

This project implements the following security controls:

- **Webhook authentication**: Zoom webhooks verified via HMAC-SHA256 signature;
  all other webhooks require `WEBHOOK_SECRET` in the Authorization header
- **Secrets management**: all credentials stored in environment variables, never in code
- **Input validation**: request payloads validated at API boundaries
- **Logging**: structured logging without sensitive data (no tokens, passwords, or PII)
- **Rate limiting**: API endpoints are rate-limited to prevent abuse
- **File handling**: uploaded recordings processed in temporary directories with cleanup
- **Dependency scanning**: regular audits of Python dependencies

## Acknowledgements

No vulnerabilities have been reported yet. Thank you for helping keep Training Agent secure.
