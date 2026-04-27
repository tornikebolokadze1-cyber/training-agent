# 005: Publish Google OAuth app to Production to stop 7-day refresh_token expiry

## Date
2026-04-08

## Status
accepted

## Context

On 2026-04-07 the pipeline for Group 1, Lecture #8 failed at 23:30 with:

```
invalid_grant: Token has been expired or revoked
```

Investigation showed the `refresh_token` in `token.json` had been revoked by
Google, not merely the short-lived `access_token`. All subsequent Drive/Docs
operations failed the same way, and the existing retry orchestrator kept
scheduling new attempts — each of which hit the same wall.

Root cause: the Google Cloud Console project backing this system has its
**OAuth consent screen in "Testing" mode**. In Testing mode, Google
automatically expires user refresh_tokens after **7 days** as a security
measure. The system had been running for roughly that long since the last
re-authorization.

## Decision

1. **Publish the OAuth consent screen to Production** in Google Cloud Console
   for the project whose `credentials.json` this repo uses. Once published,
   refresh_tokens become long-lived and only expire on explicit revocation,
   password change, or 6 months of inactivity.

2. **Do not add more OAuth scopes** than the strict minimum already in use
   (`drive`, `docs`). Adding scopes after publishing triggers a Google app
   verification flow that can take weeks for sensitive scopes.

3. **Keep the test user on the consent screen** so development re-auth works
   from a dev machine without affecting production.

4. **Document the exact project ID** used for this integration in the
   operator runbook so the next person to touch it knows which GCP project
   hosts the OAuth client.

## Reasoning

Alternatives considered:

- **Service Account instead of user OAuth**: would eliminate refresh_token
  rot entirely, but requires domain-wide delegation on a Google Workspace
  domain that the trainer's personal Google account does not have. Not
  viable without migrating the target Drive folders to a Workspace org.

- **Nightly preemptive refresh**: would hide the symptom but not the cause.
  Testing mode would still revoke the token every 7 days regardless of how
  actively it is used.

- **Increase retry count + longer backoff**: pure anti-pattern — retries
  cannot fix a revoked token, only make alerting noisier.

Publishing to Production is the only fix that eliminates the 7-day cliff
for the existing user-OAuth architecture. It is a one-time configuration
change with no ongoing maintenance cost.

## Consequences

**Positive**:
- `refresh_token` rotation drops from 7 days to effectively permanent
- No more monthly re-auth interruptions to the automated pipeline
- Preemptive health check (`google_token_health` job added in the same
  change) becomes a true safety net, not a workaround

**Negative**:
- Publishing the consent screen is a one-way door for the scope set —
  adding scopes later requires re-verification
- If the trainer's personal Google account ever gets compromised or loses
  access, re-authorization still needs a human in front of a browser

**Operational follow-ups** (tracked separately):
- Add the GCP project ID to `docs/runbook-google-oauth.md`
- Set a 5-month calendar reminder to verify the token still works (6-month
  inactivity rule is the only remaining expiry trigger in Production mode)
- If a second operator needs admin access, share the GCP project with them
  as Owner so they can run `--reauth` independently
