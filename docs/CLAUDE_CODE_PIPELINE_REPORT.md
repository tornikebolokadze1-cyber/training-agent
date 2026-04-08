# Claude Code Report: Training Agent Pipeline Failures

## Purpose

This report is for Claude Code. Its goal is to explain, with evidence, why the lecture-processing pipeline keeps failing, why costs increase unexpectedly, and why fixes do not hold across lectures.

This is not a speculative write-up. It is based on:

- repository inspection
- runtime logs
- persisted retry/job artifacts
- targeted test runs
- adversarial review of earlier conclusions

The report separates:

- **proven facts**
- **strong hypotheses**
- **historical evidence that may not exactly match the current branch**

That distinction matters. Some failures are clearly still encoded in the current code. Others are proven to have happened in production, even if the exact code path has since changed.

---

## Executive Summary

The recurring problem is **systemic**, not a single bug.

The pipeline is unstable because five critical subsystems do not agree on what "in progress", "failed", and "complete" mean:

1. orchestration and trigger paths
2. pipeline state persistence
3. retry behavior
4. AI failure handling
5. health/deploy gating

As a result:

- the same lecture can enter the system through multiple paths
- failures are sometimes swallowed and detected only after side effects
- retries are inconsistent across paths
- completed delivery steps are not tracked reliably enough to support real idempotency
- health checks can restart or fail deploys for warning-level conditions
- cost attribution can be corrupted when pipelines overlap

This explains the pattern:

- "I fix it after one lecture"
- "it breaks again later"
- "the same issue seems to come back"
- "costs double"

That pattern is expected in the current architecture.

---

## What Is Proven

### 1. `transcribe_and_index()` does not implement the resume/idempotency behavior that tests expect

This is the strongest current-branch finding.

Current production flow:

- always calls `analyze_lecture()` after entering the analysis step
- always proceeds into upload/notify/index flow
- does not branch on persisted `summary_doc_id`, `report_doc_id`, `group_notified`, `private_notified`, or `pinecone_indexed`

Relevant code:

- `tools/services/transcribe_lecture.py`

Key locations:

- Step 1 analysis call
- Step 2 summary upload
- Step 3 private report upload
- Step 4 group notification
- Step 5 private notification
- Step 6 Pinecone indexing

Why this is important:

The test suite clearly encodes a different intended behavior:

- if state is already past transcription and cached results exist, analysis should be skipped
- if notifications are already marked done, notification steps should be skipped

Evidence in tests:

- `tools/tests/test_transcribe.py`
- `test_skip_analysis_when_state_past_transcribing`
- `test_resume_skips_completed_delivery_stages`

Observed verification:

- `pytest tools/tests/test_transcribe.py -q`
- result: **4 failed, 20 passed**

The failed tests include:

- quality gate expected before delivery, but code fails later
- resume/idempotency tests that assume already-completed delivery stages are skipped

Conclusion:

The current implementation does **not** fully honor the state-machine/resume design that the repo itself claims to support.

---

### 2. Quality-gate failure happens too late

The pipeline checks for empty `summary`, `gap_analysis`, or `deep_analysis` only after:

- Google Drive uploads
- WhatsApp notification attempts
- Pinecone indexing attempts

Relevant code:

- `tools/services/transcribe_lecture.py`

This means the system can:

- perform side effects
- then fail
- then retry later
- then repeat side effects

This is exactly the wrong failure boundary.

The tests also expect a different design:

- `tools/tests/test_transcribe.py`
- `test_empty_summary_triggers_alert`
- `test_all_analyses_empty_triggers_alert`

These tests expect the quality gate to fire **before** delivery.

Verification:

- `pytest tools/tests/test_transcribe.py -q`
- both quality-gate tests failed

Conclusion:

There is a concrete mismatch between intended behavior and implemented behavior.

---

### 3. AI failures are intentionally swallowed in key places

Safe wrappers return fallback values instead of stopping the pipeline:

- Claude combined reasoning wrapper returns `None`
- Gemini Georgian writing wrapper returns `""`

Relevant code:

- `tools/core/retry.py`
- `tools/integrations/gemini_analyzer.py`

Effect:

- the real failure is obscured
- later stages continue
- the pipeline fails only after damage is already done

This is not accidental behavior. It is coded into the retry/safe-operation model.

Conclusion:

The system is structurally biased toward **fail-late**, not fail-fast.

---

### 4. Cost attribution is concurrency-unsafe

`tools/integrations/gemini_analyzer.py` uses a module-global `_current_pipeline_key`.

That value is reassigned by more than one pipeline entry point.

Effect:

- overlapping pipelines can write costs under the wrong lecture key
- lecture budget checks can read the wrong accumulated totals
- later diagnosis of "which lecture spent the money" becomes unreliable

This is a proven code-level risk, even without reproducing concurrency locally.

Conclusion:

Any overlap between lecture pipelines can corrupt cost tracking.

---

### 5. Retry behavior differs depending on how a lecture entered the system

This is another major source of instability.

The same lecture can arrive via:

- meeting-ended flow
- recording-completed direct path
- scheduler fallback
- startup recovery
- retry executor
- admin/manual endpoints

These paths do not all fail and retry the same way.

In particular:

- scheduler/polling flow can schedule retries on exceptions
- direct processing path marks failed and alerts, but does not clearly share the same retry orchestration contract

Effect:

- one lecture self-recovers
- another lecture gets stuck
- another lecture duplicates work

Conclusion:

There is no single source of truth for retry semantics.

---

### 6. Early aborts in the post-meeting path can leave bad state

In the scheduler post-meeting pipeline, some early failure paths:

- clean dedup state
- alert
- return

but do not clearly convert the lecture into a durable failed state that the retry system can reason about consistently.

Examples include:

- insufficient disk space
- no recording found
- segment download failure
- ffmpeg concat failure

Effect:

- the lecture can fall between orchestration layers
- retry logic may skip it because it still appears active or otherwise inconsistent

Conclusion:

Some failure exits are operationally incomplete.

---

### 7. The `/health` endpoint is too strict and too expensive to be used as the deploy/liveness gate

Current behavior:

- `/health` runs a full dependency audit
- warning-level issues can push overall status away from `healthy`
- non-healthy status returns HTTP `503`

That endpoint is used by:

- Railway health checks
- Docker `HEALTHCHECK`
- GitHub deploy verification

This is the wrong contract for liveness.

Effect:

- deploy can fail even when the service is basically up
- dependency noise can look like service death
- health checks themselves consume external API calls

Conclusion:

Liveness and deep dependency health must be separated.

---

### 8. CI is too permissive to protect deployment

The test job in CI is non-blocking.

That means:

- failing tests do not necessarily stop the workflow
- deploy can proceed even when correctness regressions are already visible

This is especially harmful in this repo because the failing tests are not cosmetic. They target:

- quality-gate timing
- resume behavior
- idempotency behavior

Conclusion:

The repo already contains signals of the real problems, but CI is not enforcing them.

---

## Strong Hypotheses

These are highly plausible and well-supported, but less direct than the findings above.

### 1. Startup work and recovery scans increase the chance of deploy instability

Startup performs heavy work before the app reaches a stable ready state, including:

- analytics DB init
- backfill from `.tmp`
- Pinecone sync
- startup recovery scan

This likely increases:

- slow startups
- deploy flakiness
- health-check timing problems

This is strongly supported by code and deployment structure.

---

### 2. Some of the observed duplicate launches come from historical production behavior that may only partially exist in the current branch

Production logs clearly show repeated startup recovery launches for the same lecture and repeated failure loops during shutdown/restart windows.

However, the exact current source path is not guaranteed to be identical line-for-line to the historical deploy that produced those logs.

So the safe claim is:

- this behavior definitely happened in production
- the current architecture still makes similar failure classes plausible
- but the exact repeated-launch mechanism should be re-verified after fixes

---

## Historical Evidence

These artifacts prove the system has already exhibited the problematic behavior.

### 1. Repeated startup launches for the same lecture

In `logs/training_agent.log`, `G1/L5` is started repeatedly via startup recovery.

This is direct evidence of duplicate processing in production history.

### 2. Shutdown-window failures

The same log shows repeated:

- `cannot schedule new futures after interpreter shutdown`

That means pipelines were still trying to submit work while the process was shutting down.

This is consistent with:

- restart loops
- in-flight background work
- unstable lifecycle boundaries

### 3. Claude credit exhaustion

`logs/g1_l5_pipeline.log` shows repeated Claude failures because billing/credits were unavailable.

This matters because the system retried expensive reasoning work rather than cutting the lecture over to a clean failed state early.

### 4. Missing chunk file during retry flow

`logs/g2_l6_retry.log` shows a missing chunk file during a retry run.

This supports the broader conclusion that retry/resume/resource cleanup boundaries are not robust enough.

---

## Why Claude Alone Cannot Solve This

The root issue is not "Claude is weak".

Claude is being asked to operate inside a pipeline with flawed boundaries:

- it is invoked in a fail-late architecture
- downstream steps continue after upstream AI failure
- retries are not unified
- persisted state is not used consistently enough
- cost attribution is not isolated per pipeline

There is also a prompt-design problem:

- the analysis prompt asks for market comparisons, current AI trends, competitor coverage, and 2025-2026 blind spots
- but the model is only grounded in the lecture transcript

That means some outputs are inherently under-grounded.

Claude can reason over the transcript. It cannot reliably infer current external market reality without retrieval or curated benchmark context.

So there are two different issues:

1. **system architecture problems**
2. **prompt grounding problems**

The first is far more important.

---

## Root Cause Model

The recurring failure loop can be described as follows:

1. A lecture enters the system through one of several trigger paths.
2. State is created or partially resumed.
3. Expensive AI work starts.
4. Some failures are swallowed instead of stopping the run immediately.
5. Delivery side effects happen too early.
6. A late quality check fails or the process restarts mid-flight.
7. Retry/recovery paths re-enter the lecture inconsistently.
8. Cost tracking may attach spend to the wrong lecture if overlap exists.
9. Health/deploy logic adds more instability at process boundaries.

This is why the same problem keeps "coming back". The system keeps recreating the failure conditions.

---

## What Claude Code Should Fix First

The fix order matters.

### Phase 1: Stabilize failure boundaries

1. Move critical quality gates to **before** any delivery side effects.
2. Remove fail-late swallowing for essential AI outputs.
3. Make `transcribe_and_index()` honor persisted state for resume and skip already-completed steps.

Desired result:

- no Drive/WhatsApp/Pinecone work happens if required analysis outputs are invalid
- retries restart from the correct stage instead of replaying everything

---

### Phase 2: Unify orchestration and retries

1. Define one canonical owner for lecture lifecycle state.
2. Standardize all trigger paths to the same retry contract.
3. Ensure every abort path ends in a consistent durable state.
4. Reconcile startup recovery, scheduler fallback, and retry executor around one source of truth.

Desired result:

- a lecture cannot be both active and retry-eligible in contradictory ways
- no duplicate launches for the same lecture without an explicit reset

---

### Phase 3: Make cost tracking trustworthy

1. Replace global `_current_pipeline_key` with per-call context.
2. Ensure cost and budget checks are lecture-scoped and concurrency-safe.
3. Review whether lecture budget should reset daily or remain lecture-lifetime scoped across days.

Desired result:

- cost attribution remains correct even if two lectures overlap
- "why did this lecture cost twice?" becomes answerable

---

### Phase 4: Separate liveness from deep health

1. Create a cheap `/live` or `/ready` endpoint for deploy/liveness.
2. Keep deep dependency checks in a separate observability endpoint.
3. Stop using billable external API calls as the main restart gate.

Desired result:

- deploy stability improves
- the service is not restarted for warning-only conditions
- health checks stop creating extra noise and cost

---

### Phase 5: Make CI enforce reality

1. Make the test job blocking.
2. Re-run and repair the failing `test_transcribe` expectations or adjust code intentionally.
3. Keep docs aligned with the actual pipeline behavior.

Desired result:

- regressions are caught before deploy
- docs, tests, and implementation describe the same system

---

## Acceptance Criteria For the Fix

Claude Code should not consider this solved until all of the following are true:

### Behavior

- a lecture with empty required analyses fails before any delivery side effect
- a resumed lecture skips already-completed delivery steps
- a lecture cannot be launched twice by competing orchestration paths
- retry behavior is consistent regardless of entry path

### Cost

- overlapping pipelines cannot overwrite each other's cost key
- cost records are attributable to the correct lecture
- lecture budget behavior is explicitly defined and tested

### Deploy/Operations

- liveness endpoint does not call external billable dependencies
- warning-only health states do not fail deploy/liveness
- startup/recovery behavior is bounded and predictable

### Verification

- `tools/tests/test_transcribe.py` passes
- relevant state-machine and retry tests pass
- docs are updated to match the actual implementation

---

## Recommended Working Style For Claude Code

Use this order:

1. fix quality gate timing and fail-fast behavior
2. implement true state-driven resume/idempotency
3. unify retry semantics across trigger paths
4. remove global cost-tracking state
5. split liveness from deep health
6. turn CI back into a real gate

Do not start with cosmetic refactors. Do not start with prompt tuning. Do not start with more retries.

The dominant problem is lifecycle design.

---

## Suggested Prompt For Claude Code

Use this report as the source of truth. Your job is to stabilize the lecture-processing pipeline. Do not begin with superficial cleanup. First fix fail-late behavior, late quality gates, and missing state-driven resume logic in `tools/services/transcribe_lecture.py`. Then unify retry semantics across scheduler/server/retry entry paths. Then remove global `_current_pipeline_key` from `tools/integrations/gemini_analyzer.py`. Then split deploy liveness from deep dependency health. Run targeted tests after each step, especially `tools/tests/test_transcribe.py`, and do not claim success until the behavior matches the acceptance criteria in this report.

---

## Files That Matter Most

- `tools/services/transcribe_lecture.py`
- `tools/integrations/gemini_analyzer.py`
- `tools/core/retry.py`
- `tools/core/pipeline_state.py`
- `tools/core/pipeline_retry.py`
- `tools/app/server.py`
- `tools/app/scheduler.py`
- `tools/core/health_monitor.py`
- `.github/workflows/ci.yml`
- `.github/workflows/deploy.yml`
- `tools/tests/test_transcribe.py`

---

## Appendix: High-Value Code References

Use these locations first.

### Delivery flow and late failure boundary

- `tools/services/transcribe_lecture.py:317-326`
- `tools/services/transcribe_lecture.py:352-387`
- `tools/services/transcribe_lecture.py:400-413`
- `tools/services/transcribe_lecture.py:434-446`

### Safe-operation swallowing and AI wrappers

- `tools/core/retry.py:88-156`
- `tools/integrations/gemini_analyzer.py:1217-1233`

### Cost-tracking global state

- `tools/integrations/gemini_analyzer.py:63-64`
- `tools/integrations/gemini_analyzer.py:401-438`
- `tools/integrations/gemini_analyzer.py:681-682`
- `tools/integrations/gemini_analyzer.py:1324-1325`

### Expensive retry and billing amplification paths

- `tools/integrations/gemini_analyzer.py:469-553`
- `tools/integrations/gemini_analyzer.py:775-800`

### Trigger-path inconsistency and early abort exits

- `tools/app/scheduler.py:422-429`
- `tools/app/scheduler.py:446-459`
- `tools/app/scheduler.py:476-484`
- `tools/app/scheduler.py:498-506`
- `tools/app/scheduler.py:570-598`
- `tools/app/server.py:556-567`
- `tools/core/pipeline_retry.py:450-458`

### Startup recovery and deploy/liveness behavior

- `tools/app/server.py:150-280`
- `tools/app/server.py:288-313`
- `tools/app/server.py:679-713`
- `tools/app/orchestrator.py:454-466`
- `tools/core/health_monitor.py:267-312`
- `tools/core/health_monitor.py:315-339`
- `tools/core/health_monitor.py:594-637`

### CI/deploy gates

- `.github/workflows/ci.yml:108-113`
- `.github/workflows/deploy.yml:41-62`
- `Dockerfile:52-57`
- `railway.toml:5-10`

### Tests that should become green again

- `tools/tests/test_transcribe.py:302-325`
- `tools/tests/test_transcribe.py:327-349`
- `tools/tests/test_transcribe.py:470-513`
- `tools/tests/test_transcribe.py:555-603`

### Historical operational evidence

- `logs/training_agent.log`
- `logs/g1_l5_pipeline.log`
- `logs/g2_l6_retry.log`
- `.tmp/retry_tracker.json`
- `.tmp/pending_post_meeting_jobs.json`

---

## Final Assessment

This repo does not mainly suffer from "random pipeline failures".

It suffers from:

- incomplete idempotency
- inconsistent orchestration
- late failure boundaries
- unsafe cost context
- overly strict operational gating

That is why the same class of problem keeps returning.

Fix the lifecycle, and the recurring symptoms should drop sharply.
