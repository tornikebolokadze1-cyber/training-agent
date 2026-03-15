# Railway Deployment Guide - Training Agent

## Table of Contents

1. [Architecture Decision](#1-architecture-decision)
2. [Code Changes Status](#2-code-changes-status)
3. [Google OAuth Token Strategy](#3-google-oauth-token-strategy)
4. [APScheduler Persistence](#4-apscheduler-persistence)
5. [Video Processing Viability](#5-video-processing-viability)
6. [Single vs Multi-Service](#6-single-vs-multi-service)
7. [Networking and Webhook URLs](#7-networking-and-webhook-urls)
8. [Environment Variables (Complete List)](#8-environment-variables-complete-list)
9. [Scaling and Instance Count](#9-scaling-and-instance-count)
10. [Monitoring and Health Checks](#10-monitoring-and-health-checks)
11. [Cost Estimation](#11-cost-estimation)
12. [Step-by-Step Migration Plan](#12-step-by-step-migration-plan)
13. [Rollback Plan](#13-rollback-plan)

---

## 1. Architecture Decision

**Verdict: Single Railway service, single instance.**

The Training Agent is a stateful singleton by design. APScheduler holds in-memory
cron jobs, the FastAPI server maintains an in-flight task registry for deduplication
(`_processing_tasks` dict in `server.py`), and Google OAuth tokens need a single
writer. Running multiple instances would cause duplicate scheduled jobs, race
conditions on token refresh, and broken deduplication.

Railway runs the single container with the existing entry point:
`python -m tools.orchestrator`

This starts both APScheduler and uvicorn on the same asyncio event loop, exactly
as it works locally today.

---

## 2. Code Changes Status

Most Railway-readiness changes have already been applied to the codebase.
Here is a summary of what exists and what was just applied:

### Already Applied (in codebase)

| File | Change | Status |
|------|--------|--------|
| `tools/server.py` | TrustedHostMiddleware accepts `RAILWAY_PUBLIC_DOMAIN` and `SERVER_PUBLIC_URL` hostnames | Done |
| `tools/config.py` | `IS_RAILWAY` detection flag, `_decode_b64_env()` helper, `_materialize_credential_file()` for base64 credential loading | Done |
| `tools/config.py` | `_load_attendees()` reads `ATTENDEES_JSON_B64` env var with file fallback | Done |
| `tools/config.py` | `get_google_credentials_path()` reads `GOOGLE_CREDENTIALS_JSON_B64` | Done |
| `tools/gdrive_manager.py` | `_get_token_path()` reads `GOOGLE_TOKEN_JSON_B64`, `_get_credentials()` handles Railway mode (in-memory refresh, no disk writes, clear error when re-auth needed) | Done |

### Applied in This Session

| File | Change | Status |
|------|--------|--------|
| `tools/config.py` | `SERVER_PORT` falls back to Railway's `PORT` env var: `int(_env("SERVER_PORT", _env("PORT", "5001")))` | Done |
| `tools/orchestrator.py` | File-based logging skipped when `RAILWAY_ENVIRONMENT` is set (Railway captures stdout) | Done |
| `Dockerfile` | Created with Python 3.12, ffmpeg, health check | Done |
| `railway.toml` | Created with Dockerfile builder, health check config, single replica | Done |

### Still Required: Set `SERVER_HOST=0.0.0.0`

The code defaults `SERVER_HOST` to `127.0.0.1`. Railway routes traffic into
the container but the process must listen on all interfaces. Set this as
a Railway environment variable (no code change needed):

```
SERVER_HOST=0.0.0.0
```

---

## 3. Google OAuth Token Strategy

The code already handles this via base64-encoded env vars. Here is the exact
workflow for getting credentials into Railway.

### How It Works

`config.py` provides `_materialize_credential_file()` which:
1. Reads a base64-encoded env var (e.g., `GOOGLE_TOKEN_JSON_B64`)
2. Decodes it to JSON text
3. Writes it to a secure temp file with `0o600` permissions
4. Returns the path to that temp file

`gdrive_manager.py` then loads credentials from that temp file and refreshes
the access token in memory. On Railway, it never writes back to disk since the
filesystem is ephemeral.

### Setup Steps

**Step 1:** Encode your local credential files:
```bash
cd "/Users/tornikebolokadze/Desktop/Training Agent"

# Google OAuth token (access + refresh tokens)
base64 -i token.json | tr -d '\n' | pbcopy
# Paste into Railway as GOOGLE_TOKEN_JSON_B64

# Google OAuth client secrets
base64 -i credentials.json | tr -d '\n' | pbcopy
# Paste into Railway as GOOGLE_CREDENTIALS_JSON_B64

# Attendee list
base64 -i attendees.json | tr -d '\n' | pbcopy
# Paste into Railway as ATTENDEES_JSON_B64
```

**Step 2:** Set these as Railway env vars (dashboard or CLI).

### Why This Works

- The `refresh_token` in `token.json` is permanent (until revoked).
- On container start, the base64 env var is decoded to a temp file.
- The code loads credentials from the temp file, calls `creds.refresh()` to
  get a fresh access token, and proceeds.
- The access token (1-hour TTL) lives in memory. The Google client library
  auto-refreshes using the refresh token for the lifetime of the process.

### When It Breaks

- If you revoke the OAuth consent in Google Cloud Console.
- If you change the OAuth scopes (requires re-consent).
- Fix: run `python -m tools.gdrive_manager` locally with a browser, then
  re-encode `token.json` and update the Railway env var.

### Better Long-Term: Google Service Account

For a production system, consider switching from three-legged OAuth to a
service account:
1. Create a service account in Google Cloud Console.
2. Share your Drive folders with the service account email.
3. Download the JSON key, base64-encode it, store as env var.
4. Use `google.oauth2.service_account.Credentials` -- no browser consent,
   no token expiration, no refresh complexity.

---

## 4. APScheduler Persistence

**Current behavior:** All cron jobs are defined in code in `start_scheduler()`.
They are recreated every time the scheduler starts. This is fine.

**On Railway restart:** The container stops, a new container starts,
`orchestrator.py` runs, `start_scheduler()` re-registers all 4 cron jobs.
They fire at the correct times because they are cron-based (not interval-based
from a past reference point).

**What IS lost on restart:**
- One-shot `date`-triggered post-meeting jobs (created dynamically by
  `pre_meeting_job()`). If Railway restarts between 19:00 (pre-meeting
  fires, creates Zoom meeting, schedules post-meeting at 22:00) and 22:00
  (post-meeting fires), the recording pipeline will NOT run automatically.

**Mitigation options (pick one):**

A. **Accept the risk.** Restarts during the 3-hour window (19:00-22:00 on
   lecture days, 4 times per week) are rare on Railway. If it happens,
   process the recording manually via the `/process-recording` endpoint.

B. **Convert post-meeting to a cron job.** Instead of dynamically scheduling,
   add fixed cron jobs at 22:15 for each group's meeting days. The job
   checks if a lecture happened today and processes the recording:
   ```python
   # Group 1: Tue/Fri at 22:15
   scheduler.add_job(
       post_meeting_check,
       trigger=CronTrigger(day_of_week="tue,fri", hour=22, minute=15, ...),
       args=[1], id="post_group1", ...
   )
   ```
   This survives restarts because the cron trigger is declarative.

C. **Use APScheduler's SQLAlchemy job store** with Railway's Postgres add-on
   ($7/month). Jobs persist across restarts. Overkill for 4 cron jobs, but
   guarantees zero job loss.

**Recommendation:** Option B. It is simple, stateless, and survives restarts.

---

## 5. Video Processing Viability

### Disk Usage Analysis

Railway provides ~10 GB of ephemeral disk per service. Typical lecture recording:

| Component | Size | Duration |
|-----------|------|----------|
| Original MP4 download | 1-2 GB | Exists until cleanup |
| ffmpeg chunks (2-3 chunks of ~45 min) | 0.5-0.7 GB each | Cleaned up per-chunk |
| Peak disk usage | ~3 GB | During chunk splitting |

**Verdict: Viable.** Peak usage of ~3 GB is well within 10 GB. The code already
cleans up chunk files after each is transcribed (`gemini_analyzer.py` line 406)
and cleans up the original MP4 after processing (`server.py` line 216).

### ffmpeg

The Dockerfile installs ffmpeg from Debian packages. It is used by
`split_video_chunks()` for stream-copy splitting (no re-encoding, near-instant)
and `_get_video_duration_seconds()` via ffprobe.

### Memory

Video file I/O is streamed (1 MB chunks in `_download_recording`), so memory
stays low. The Gemini and Claude API calls are HTTP-based. Typical RSS during
processing: 300-500 MB. The Developer plan ($5/month) provides up to 8 GB RAM
billed by usage -- you will pay for ~400 MB average.

### Processing Time

A full pipeline run (download + chunk + transcribe + 3x analysis) takes
15-45 minutes. Railway has no hard timeout on long-running background tasks.
The 600-second httpx timeout for downloads is sufficient for 1-2 GB files.

---

## 6. Single vs Multi-Service

**Decision: Single service.**

Reasons:
- APScheduler and FastAPI share an asyncio event loop by design.
- The scheduler creates dynamic jobs that reference the same process state.
- Deduplication relies on in-memory `_processing_tasks` dict.
- Splitting would require Redis/Postgres for coordination -- complexity with
  no benefit at this traffic volume (a few webhook calls per week).
- Video processing runs in background tasks / thread pool, so the FastAPI
  server stays responsive even during heavy processing.

---

## 7. Networking and Webhook URLs

### Railway Public URL

Railway assigns a URL like `training-agent-production.up.railway.app`. You can
also configure a custom domain (e.g., `agent.aipulsegeorgia.com`) for free.

### What to Update After Deploy

| System | Setting | New Value |
|--------|---------|-----------|
| Green API | Instance > Webhooks URL | `https://<railway-domain>/whatsapp-incoming` |
| Green API | Webhook token | Same as `WEBHOOK_SECRET` |
| n8n workflow | HTTP Request node URL | `https://<railway-domain>/process-recording` |
| Railway env var | `SERVER_PUBLIC_URL` | `https://<railway-domain>` |
| Railway env var | `N8N_CALLBACK_URL` | Keep as-is (points to n8n cloud) |

### TrustedHostMiddleware

Already handled. `server.py` reads `RAILWAY_PUBLIC_DOMAIN` (auto-injected by
Railway) and `SERVER_PUBLIC_URL` (your env var), adding both hostnames to the
allowed hosts list. No manual `ALLOWED_HOST` config needed.

---

## 8. Environment Variables (Complete List)

Set ALL of these in Railway dashboard (Settings > Variables) or via CLI.

### Required (server will not start without these)

| Variable | Source |
|----------|--------|
| `ZOOM_ACCOUNT_ID` | Zoom marketplace |
| `ZOOM_CLIENT_ID` | Zoom marketplace |
| `ZOOM_CLIENT_SECRET` | Zoom marketplace |
| `GEMINI_API_KEY` | Google AI Studio (free tier) |
| `GREEN_API_INSTANCE_ID` | Green API dashboard |
| `GREEN_API_TOKEN` | Green API dashboard |
| `WEBHOOK_SECRET` | Your secret (shared with n8n + Green API) |

### Required for Full Functionality

| Variable | Source |
|----------|--------|
| `GEMINI_API_KEY_PAID` | Google AI Studio (billing-enabled key) |
| `ANTHROPIC_API_KEY` | Anthropic Console |
| `PINECONE_API_KEY` | Pinecone Console |

### Base64-Encoded Files (Railway-specific)

Generate with: `base64 -i <file> | tr -d '\n'`

| Variable | Source File |
|----------|------------|
| `GOOGLE_TOKEN_JSON_B64` | `token.json` (OAuth access + refresh tokens) |
| `GOOGLE_CREDENTIALS_JSON_B64` | `credentials.json` (OAuth client secrets) |
| `ATTENDEES_JSON_B64` | `attendees.json` (student email lists) |

### Google Drive Folder IDs

| Variable | Source |
|----------|--------|
| `DRIVE_GROUP1_FOLDER_ID` | Google Drive folder URL for group 1 |
| `DRIVE_GROUP2_FOLDER_ID` | Google Drive folder URL for group 2 |
| `DRIVE_GROUP1_ANALYSIS_FOLDER_ID` | Private analysis folder for group 1 |
| `DRIVE_GROUP2_ANALYSIS_FOLDER_ID` | Private analysis folder for group 2 |

### Zoom Meeting IDs

| Variable | Source |
|----------|--------|
| `ZOOM_GROUP1_MEETING_ID` | Zoom meeting settings |
| `ZOOM_GROUP2_MEETING_ID` | Zoom meeting settings |

### WhatsApp / Messaging

| Variable | Source |
|----------|--------|
| `WHATSAPP_TORNIKE_PHONE` | Your phone (e.g., `995599123456`) |
| `WHATSAPP_GROUP1_ID` | Green API group info (e.g., `120363XXX@g.us`) |
| `WHATSAPP_GROUP2_ID` | Green API group info |
| `MANYCHAT_API_KEY` | ManyChat (if still used) |
| `MANYCHAT_TORNIKE_SUBSCRIBER_ID` | ManyChat |
| `MANYCHAT_GROUP1_FLOW_ID` | ManyChat |
| `MANYCHAT_GROUP2_FLOW_ID` | ManyChat |

### Server Configuration

| Variable | Value | Notes |
|----------|-------|-------|
| `SERVER_HOST` | `0.0.0.0` | CRITICAL -- must be 0.0.0.0 for Railway |
| `SERVER_PORT` | `5001` | Or omit; code falls back to Railway's `PORT` |
| `SERVER_PUBLIC_URL` | `https://<your>.railway.app` | Used for self-reference and TrustedHost |
| `N8N_CALLBACK_URL` | `https://aipulsegeorgia2025.app.n8n.cloud/webhook/...` | n8n cloud URL |

### Railway Auto-Injected (do not set manually)

| Variable | Description |
|----------|-------------|
| `PORT` | Railway's assigned port (code uses as fallback) |
| `RAILWAY_ENVIRONMENT` | `production` or `staging` |
| `RAILWAY_PUBLIC_DOMAIN` | Your `*.railway.app` domain |

---

## 9. Scaling and Instance Count

**Single instance only. Do not scale horizontally.**

Reasons:
1. **APScheduler duplication:** 2 instances = 2 cron fires = 2 Zoom meetings
   created, 2 WhatsApp reminders sent, 2 recording pipelines competing.
2. **Deduplication broken:** `_processing_tasks` is in-memory per instance.
3. **OAuth token contention:** Two instances refreshing the same token can
   cause race conditions.

Railway scaling settings: **Min Instances = 1, Max Instances = 1**.

---

## 10. Monitoring and Health Checks

### Railway Health Check

Configured in `railway.toml` and `Dockerfile`:
- **Endpoint:** `/health`
- **Interval:** 30 seconds
- **Timeout:** 10 seconds
- **Start period:** 30 seconds

The `/health` endpoint checks tmp directory writability and env var presence.

### Extended Status

`curl https://<domain>/status` returns:
- Uptime seconds and start time
- Scheduler state and all upcoming job fire times
- Last execution results
- Server host/port/version

### Logging

Railway captures all stdout/stderr. File logging is automatically disabled
when `RAILWAY_ENVIRONMENT` is set. The structured format
(`2026-03-14 20:00:00 INFO [tools.scheduler] message`) is fully searchable
in Railway's log viewer.

### Application-Level Alerting

Already built in. `alert_operator()` sends WhatsApp messages to Tornike when:
- Recording download fails
- Processing pipeline fails
- Recording not found after 3-hour polling timeout

### External Monitoring (Recommended)

Set up UptimeRobot (free tier):
- Monitor: `https://<domain>/health`
- Interval: 5 minutes
- Alert: Email or Telegram on downtime

---

## 11. Cost Estimation

### Railway Developer Plan ($5/month base)

| Resource | Usage Estimate | Cost |
|----------|---------------|------|
| Compute (idle, 24/7) | ~100 MB RAM, near-zero CPU | ~$1-2/month |
| Compute (processing, ~8 lectures/month) | ~500 MB RAM, 30-45 min each | ~$0.50/lecture |
| Network egress | <1 GB/month | Included |
| Disk | Ephemeral, <3 GB peak | Included |
| **Monthly total** | | **~$9-12/month** |

### vs Current Setup

| | Local + ngrok | Railway |
|--|--------------|---------|
| Compute | Mac must be on | Always on |
| Network | ngrok free (8h sessions) or $8/month | Included |
| Reliability | Depends on Mac being awake | 99.9%+ |
| Maintenance | Manual restarts, ngrok reconnects | Zero-touch |
| **Total** | **$0-8/month + inconvenience** | **~$10/month** |

---

## 12. Step-by-Step Migration Plan

### Phase 1: Prepare Credentials (30 minutes)

```bash
cd "/Users/tornikebolokadze/Desktop/Training Agent"

# Encode credential files for Railway
echo "=== GOOGLE_TOKEN_JSON_B64 ==="
base64 -i token.json | tr -d '\n'
echo ""

echo "=== GOOGLE_CREDENTIALS_JSON_B64 ==="
base64 -i credentials.json | tr -d '\n'
echo ""

echo "=== ATTENDEES_JSON_B64 ==="
base64 -i attendees.json | tr -d '\n'
echo ""
```

Save each output -- you will paste these into Railway.

Also have your `.env` file open -- you will copy each value.

### Phase 2: Railway Project Setup (15 minutes)

1. Install Railway CLI:
   ```bash
   npm install -g @railway/cli
   railway login
   ```

2. Initialize and link:
   ```bash
   cd "/Users/tornikebolokadze/Desktop/Training Agent"
   railway init    # Choose "Empty Project", name: training-agent
   railway link    # Link to the project you just created
   ```

3. Generate a public domain:
   Go to Railway dashboard > your service > Settings > Networking > Generate Domain.
   Note the assigned domain.

### Phase 3: Set Environment Variables (15 minutes)

Use the Railway dashboard (easier for long base64 strings):

1. Go to your service > Variables tab.
2. Add every variable from Section 8.
3. Double-check: `SERVER_HOST` is `0.0.0.0`.
4. Double-check: `SERVER_PUBLIC_URL` matches your Railway domain.

### Phase 4: Deploy (10 minutes)

```bash
railway up
```

Watch logs:
```bash
railway logs
```

Verify these lines appear:
- `All required credentials validated successfully.`
- `Scheduler started with 4 recurring jobs`
- `Starting uvicorn on 0.0.0.0:...`

Test endpoints:
```bash
curl https://<domain>/health
curl https://<domain>/status
```

### Phase 5: Update Webhook URLs (10 minutes)

1. **Green API:** Dashboard > Instance > Webhooks
   - URL: `https://<railway-domain>/whatsapp-incoming`
   - Token: same as `WEBHOOK_SECRET`

2. **n8n:** Recording processor workflow
   - HTTP Request node URL: `https://<railway-domain>/process-recording`

3. **Test:** Send a WhatsApp message, verify it appears in Railway logs.

### Phase 6: Parallel Run (2-3 days)

Keep local server as cold standby. Run Railway as primary for 1-2 lecture
cycles. Monitor during live lectures (19:00-22:00 Tbilisi).

Verify the full pipeline:
- [ ] Pre-meeting reminder fires at 19:00
- [ ] Zoom meeting is created with correct link
- [ ] WhatsApp group reminder is sent
- [ ] Post-meeting recording pipeline starts after lecture
- [ ] Video downloads to Railway container
- [ ] ffmpeg chunking works
- [ ] Gemini transcription completes
- [ ] Claude + Gemini analysis completes
- [ ] Google Drive uploads succeed (summary Doc created)
- [ ] Gap/deep analysis arrives on private WhatsApp
- [ ] Pinecone indexing completes
- [ ] Temp files cleaned up (check via `/health`)

### Phase 7: Cutover (Day 4+)

1. Stop local server.
2. Stop ngrok.
3. Railway is now sole production.

### Phase 8: Harden (Week 2)

1. Set up UptimeRobot on `/health`.
2. Optionally add a custom domain.
3. Consider implementing Option B from Section 4 (cron-based post-meeting
   jobs) for restart resilience.
4. Consider switching to Google Service Account for long-term stability.

---

## 13. Rollback Plan

If Railway fails during a live lecture:

**Immediate (< 2 minutes):**
```bash
# On your Mac:
cd "/Users/tornikebolokadze/Desktop/Training Agent"
python -m tools.orchestrator &
ngrok http 5001
```
Then update Green API webhook URL and n8n HTTP Request node back to the
ngrok URL.

**Post-incident:**
```bash
railway logs              # Diagnose
# Fix the issue
railway up                # Redeploy
# Re-point webhooks to Railway
```

---

## Files Created/Modified

| File | Action |
|------|--------|
| `/Users/tornikebolokadze/Desktop/Training Agent/Dockerfile` | Created |
| `/Users/tornikebolokadze/Desktop/Training Agent/railway.toml` | Created |
| `/Users/tornikebolokadze/Desktop/Training Agent/tools/config.py` | Modified (SERVER_PORT fallback) |
| `/Users/tornikebolokadze/Desktop/Training Agent/tools/orchestrator.py` | Modified (conditional file logging) |
| `/Users/tornikebolokadze/Desktop/Training Agent/RAILWAY_DEPLOYMENT.md` | Created (this guide) |
