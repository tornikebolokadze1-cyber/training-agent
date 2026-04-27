# Scheduler Jobs Reference

> **Source of truth**: `tools/app/scheduler.py`
> **Runtime**: APScheduler `AsyncIOScheduler` running inside `tools/app/orchestrator.py`
> **Timezone**: `Asia/Tbilisi` (GMT+4)

---

## Overview

The scheduler manages the full lecture lifecycle for two training groups:

1. **Pre-meeting** (T-120 min) -- create Zoom meeting, send email + WhatsApp reminders
2. **Post-meeting** (safety net at T+210 min) -- poll Zoom for recordings, download, analyze, deliver
3. **Periodic maintenance** -- WhatsApp health checks, Obsidian sync, Dead Letter Queue processing

---

## Constants

| Constant | Value | Description |
|---|---|---|
| `LECTURE_START_HOUR` | `20` | Lectures start at 20:00 GMT+4 |
| `REMINDER_OFFSET_MINUTES` | `120` | Pre-meeting job fires 120 min before start |
| `REMINDER_HOUR` | `18` | Derived: 20 - (120 / 60) = 18:00 |
| `REMINDER_MINUTE` | `0` | Pre-meeting fires at :00 |
| `RECORDING_INITIAL_DELAY` | `900` (15 min) | Wait before first Zoom API poll |
| `RECORDING_POLL_INTERVAL` | `300` (5 min) | Interval between recording polls |
| `RECORDING_POLL_TIMEOUT` | `10800` (3 hours) | Absolute deadline for polling |

---

## APScheduler Configuration

### Executors

| Executor | Type | Purpose |
|---|---|---|
| `default` | `AsyncIOExecutor` | Async jobs (pre-meeting, post-meeting bridge) |
| `threadpool` | `ThreadPoolExecutor(max_workers=6)` | Blocking I/O (recording download, Zoom API polling, ffmpeg, Drive upload) |

### Job Defaults

| Setting | Value | Purpose |
|---|---|---|
| `coalesce` | `True` | Merge multiple misfired instances into one execution |
| `max_instances` | `1` | Never run the same job concurrently |
| `misfire_grace_time` | `3300` (55 min) | Tolerate Railway restarts without silently dropping lectures |

---

## Recurring Cron Jobs

### Pre-Meeting Reminders

These fire at **18:00 GMT+4** on each group's meeting days.

| Job ID | Group | Day | Trigger |
|---|---|---|---|
| `pre_group1_tuesday` | 1 | Tuesday | `CronTrigger(day_of_week="tue", hour=18, minute=0)` |
| `pre_group1_friday` | 1 | Friday | `CronTrigger(day_of_week="fri", hour=18, minute=0)` |
| `pre_group2_monday` | 2 | Monday | `CronTrigger(day_of_week="mon", hour=18, minute=0)` |
| `pre_group2_thursday` | 2 | Thursday | `CronTrigger(day_of_week="thu", hour=18, minute=0)` |

#### What `pre_meeting_job()` Does

1. Derives today's lecture number from the group schedule (skips if no lecture today or all 15 are done).
2. Creates a Zoom meeting via `zoom_manager.create_meeting()` for 20:00 that day.
3. Sends a WhatsApp group reminder with the Zoom join link via `whatsapp_sender.send_group_reminder()`.
4. Schedules a **one-shot post-meeting fallback job** at **23:30** (safety net -- the webhook is the primary trigger).
5. On failure: sends operator alert via WhatsApp; continues with placeholder link if Zoom creation fails.

> **Note**: Email invitations are handled automatically by Zoom when a meeting is created with attendee emails.

### Periodic Maintenance Jobs

| Job ID | Schedule | Description |
|---|---|---|
| `whatsapp_obsidian_sync` | Every 6 hours (00:30, 06:30, 12:30, 18:30) | Sync WhatsApp chat history into Obsidian vault |
| `dlq_processor` | Every 10 minutes | Process pending Dead Letter Queue retries + weekly cleanup (7-day max age) |
| `whatsapp_health_check` | Every 30 minutes | Proactive WhatsApp connectivity check; alerts via **email** (not WhatsApp) if disconnected |

---

## Post-Meeting Pipeline

### Trigger Paths

The post-meeting pipeline has two trigger paths:

| Path | Trigger | Timing | Priority |
|---|---|---|---|
| **Primary** | `meeting.ended` webhook in `server.py` | Immediately after meeting ends | First |
| **Fallback** | Scheduler `post_meeting_job()` | 23:30 GMT+4 (T+210 min) | Safety net |

The fallback checks deduplication before running. If the webhook already handled it, the fallback skips silently.

### Deduplication

- Uses `_processing_tasks` dict (in `server.py`) with group+lecture composite key.
- Atomic check-and-set under `_processing_lock` prevents webhook+scheduler race conditions.
- Stale tasks (>4 hours) are auto-evicted before each check.
- For the scheduler fallback, stuck pipelines (>2 hours in ACTIVE state) are force-reset and retried.

### Pipeline Steps (`_run_post_meeting_pipeline`)

1. **Disk space check** -- aborts if <2 GB free; alerts operator.
2. **Poll for recording** -- calls `check_recording_ready()` (see below).
3. **Download segments** -- downloads all MP4 segments to `.tmp/`.
4. **Concatenate** -- if multiple segments, merges with ffmpeg concat demuxer (lossless, no re-encoding). Deletes individual segments immediately after merge.
5. **Upload to Drive** -- uploads merged recording to the correct lecture folder.
6. **Analysis pipeline** -- delegates to `transcribe_and_index()` (Gemini transcription, Claude reasoning, summary Doc, gap analysis, WhatsApp notifications, Pinecone indexing).
7. **Cleanup** -- removes temp files, dedup keys, and pending job entries in all exit paths (success, failure, early return).

A second disk space check occurs before step 6 with emergency cleanup of stale `.mp4` and `.chunk*.mp4` files if <1.5 GB free.

### Error Handling

- Auth errors (401/403) from Zoom abort immediately (no retry).
- Transient errors retry per the polling logic.
- Pipeline failures: mark state as FAILED, send n8n callback, alert operator via WhatsApp.
- 4-hour absolute timeout on the executor (`asyncio.wait_for`).

---

## Recording Polling (`check_recording_ready`)

This is a **blocking function** that runs in the thread pool executor. It polls the Zoom API until all recording segments are available.

### Flow

```
Start
  |
  v
[Skip initial delay?] --No--> Sleep 15 minutes
  |Yes                           |
  v                              v
Poll Zoom API <-----------------+
  |
  +-- Auth error (401/403) --> ABORT (alert operator)
  |
  +-- "Still processing" 404 --> Exponential backoff (see below)
  |
  +-- Transient error --> Retry in 5 min
  |
  +-- No MP4s yet --> Retry in 5 min
  |
  +-- Found MP4s --> Stabilization check (see below)
  |
  +-- Elapsed > 3 hours --> TIMEOUT (alert operator)
```

### Adaptive Exponential Backoff for 404s

When Zoom returns a "still processing" 404 (error code 3301), the interval increases exponentially:

| Consecutive 404s | Backoff |
|---|---|
| 1 | 5 min |
| 2 | 10 min |
| 3 | 20 min |
| 4+ | 30 min (cap) |

Formula: `min(RECORDING_POLL_INTERVAL * 2^(n-1), 30 min)`

The counter resets on any successful API response.

### Segment Stabilization Check

When a host disconnects and rejoins, Zoom creates multiple recording segments. To catch late-arriving segments:

1. First MP4 found -- record segment count, wait 5 min.
2. Re-poll -- if segment count increased, wait 5 more min and poll again (one extra stabilization round).
3. If the stability check poll itself fails, proceed with the previously found segments.

---

## Persistent Job Store

### Purpose

Post-meeting jobs are dynamically scheduled (not recurring cron). Without persistence, a Railway restart between meeting creation (18:00) and post-meeting fire time (23:30) would lose the job.

### Storage

- **File**: `.tmp/pending_post_meeting_jobs.json`
- **Format**: JSON array of job entries
- **Write strategy**: atomic write via temp file + rename (prevents corruption on crash)

### Entry Schema

```json
{
  "group": 1,
  "lecture": 3,
  "meeting_id": "abc123",
  "fire_time": "2026-03-29T23:30:00+04:00"
}
```

### Lifecycle

| Event | Action |
|---|---|
| Post-meeting job scheduled | Entry added/replaced (deduped by group+lecture) |
| Pipeline completes (success or failure) | Entry removed in `finally` block |
| Server starts | `_restore_pending_jobs()` called once |

### Startup Restoration (`_restore_pending_jobs`)

On startup, all persisted jobs are evaluated:

- **Jobs >2 hours in the past**: skipped as stale (logged and ignored).
- **Jobs in the past but within misfire grace (30 min)**: fired immediately.
- **Jobs in the future**: re-scheduled normally.
- If the fire time is already past when scheduling, it is rescheduled to `now + 15 min` (initial delay).

---

## Module-Level Scheduler Reference

The `pre_meeting_job()` function needs access to the scheduler to add post-meeting jobs, but APScheduler does not pass the scheduler instance to job functions. This is solved with a module-level variable `_scheduler_ref` set by `start_scheduler()` and accessed via `_get_running_scheduler()`.

---

## CLI Usage

```bash
# Start scheduler standalone (for development)
python -m tools.app.scheduler

# Production: scheduler runs inside the orchestrator
python -m tools.app.orchestrator
```
