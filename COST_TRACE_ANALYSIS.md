# SIMULATION: Monday, April 13, Group 2, Lecture #10 Processing

## SCENARIO SETUP
- **Current date**: April 10, 2026 (but we're simulating April 13)
- **Lecture**: Group 2, Lecture #10
- **Pipeline key**: "g2_l10"
- **Status**: Next lecture after Group 1 Lecture #5 was just processed

## COST TRACKER STATE BEFORE LECTURE
- **Daily limit**: $50.00 USD
- **Lecture limit**: $5.00 USD (PER LECTURE)
- **Previous spending today**: $19.74 (from Group 1 Lecture #5)
- **Remaining daily budget**: $30.26
- **Previous lecture cost (g1_l5)**: $2.15 (duration adjustment only)

---

## STEP-BY-STEP SIMULATION

### STEP 1: Zoom Webhook Arrives → server.py Processes
**Expected behavior:**
- Webhook contains `recording_id`, `recording_url`, lecture metadata
- server.py calls `transcribe_and_index(group=2, lecture=10, video_path=...)`
- Pipeline state is created with pipeline_key = "g2_l10"

**Cost tracker at this point:**
- Accumulated cost for g2_l10: $0.00
- Daily total: $19.74
- Remaining daily: $30.26

---

### STEP 2: transcribe_and_index() Runs → Pre-flight Budget Check
**Location:** `transcribe_lecture.py:294-316`

```python
budget_ok, remaining = check_daily_budget()
# Checks: remaining > 0 && total < $50
```

**Current state:**
- Daily total: $19.74
- Daily limit: $50.00
- Remaining: $30.26

**Result:** ✅ **PASSES** — $30.26 remaining > $0

**Decision:** Pipeline proceeds to analyze_lecture()

---

### STEP 3: analyze_lecture() Runs → transcribe_chunked_video()
**Location:** `gemini_analyzer.py:692-900`

The lecture video is 90 minutes, split into chunks for processing.
For simplicity, assume **2 chunks of 45 minutes each** (typical for a lecture).

#### **CHUNK 1 PROCESSING**

**Pre-chunk budget check:**
- At i=0 (first chunk), the pre-chunk check is SKIPPED (line 751: `if i > 0`)
- Cost so far for g2_l10: $0.00
- Decision: proceed with transcription

**Transcription step:**
1. Upload 45-minute video to Gemini
2. Call Gemini 2.5 Flash to transcribe multimodally
3. Receive transcript + usage_metadata with token counts

**Token-based cost recorded:**
Assume typical transcription response:
- Input tokens: 800,000 (video frames + prompt)
- Output tokens: 12,000 (transcript)
- Gemini 2.5 Flash rates: $0.075/M input, $0.30/M output
- Input cost: (800,000 / 1,000,000) * $0.075 = **$0.06**
- Output cost: (12,000 / 1,000,000) * $0.30 = **$0.0036**
- **Initial recorded cost: $0.0636**

`record_cost()` called at `gemini_analyzer.py:436-447`

**Duration-based adjustment (lines 878-905):**
- Chunk duration: 45 minutes = 2700 seconds
- Duration cost: 2700 * $0.002/sec = **$5.40**
- Subtract token-based estimate: $5.40 - $0.25 = **$5.15 adjustment**

**Accumulated cost for g2_l10 after chunk 1:**
- Token-based: $0.0636
- Duration adjustment: $5.15
- **Subtotal: $5.2136**

**Daily accumulated cost:**
- Previous (before this lecture): $19.74
- Chunk 1: $5.2136
- **New total: $24.9536**
- **Remaining daily: $25.0464**

---

#### **CHUNK 2 PROCESSING**

**Pre-chunk budget check (line 751-763 in gemini_analyzer.py):**
```python
if i > 0:  # Check before chunk 2, 3, etc.
    _ok, _rem = check_lecture_budget("g2_l10")
    estimated_cost = LECTURE_COST_LIMIT_USD - _rem
    if estimated_cost > MAX_COST_PER_LECTURE:  # if > $5.00
        raise RuntimeError(...)
```

**Current state:**
- Lecture limit: $5.00
- Lecture spending so far for g2_l10: $5.2136
- **estimated_cost ($5.2136) > LECTURE_COST_LIMIT_USD ($5.00)**: TRUE

**RESULT: 🚫 HARD FAIL — LECTURE BUDGET EXCEEDED**

**Exception raised:**
```
RuntimeError("Gemini cost budget exceeded: $5.21 > $5.00")
```

**Partial transcript saved to:** `.tmp/{video_stem}_partial_transcript.txt`

**Error logged:**
```
ERROR: Cost budget EXCEEDED before chunk 2/2: $5.21 > $5.00 limit. 
Stopping to prevent further spend. Partial transcript saved.
```

**Pipeline marked as FAILED**

---

### STEP 4: Exception Propagates → WhatsApp Alert
**Location:** `transcribe_lecture.py:481-484`

Pipeline fails, exception caught, operator alerted via WhatsApp.

---

## COST SUMMARY FOR g2_l10

| Stage | Purpose | Cost | Cumulative |
|-------|---------|------|-----------|
| Chunk 1 — Token-based | transcription chunk 1/2 | $0.0636 | $0.0636 |
| Chunk 1 — Duration adjust | video_duration_adjustment 1/2 | $5.15 | $5.2136 |
| Chunk 2 — Budget check | (blocked, never executed) | $0 | $5.2136 |
| **TOTAL** | | | **$5.2136** |

---

## DAILY COST IMPACT

| Time | Event | Amount | Daily Total | Remaining |
|------|-------|--------|-------------|-----------|
| Pre-lecture | Previous lectures | $19.74 | $19.74 | $30.26 |
| 1. Pre-flight check | ✅ Passes | — | $19.74 | $30.26 |
| 2. Chunk 1 token | Recorded | $0.0636 | $19.8036 | $30.1964 |
| 3. Chunk 1 duration | Recorded | $5.15 | $24.9536 | $25.0464 |
| 4. Chunk 2 attempt | 🚫 BLOCKED | — | **$24.9536** | **$25.0464** |

---

## THE CRITICAL PROBLEM: $5.00 PER-LECTURE LIMIT IS IMPOSSIBLE

### Root Cause Analysis

The `$5.00` per-lecture limit was set when cost tracking only used **token-based calculations**:
- Token-based cost per chunk: ~$0.25
- Typical 4 chunks per 90-min lecture: 4 × $0.25 = $1.00 total
- Buffer for Claude reasoning & Gemini writing: +$2-3
- Reasonable limit: $5.00 total per lecture ✅

### What Changed: Duration-Based Cost Adjustment

A **duration-based adjustment** was added (line 878-905 of gemini_analyzer.py):
```python
duration_cost = chunk_duration * 0.002  # ~$0.002/sec for Flash video
adjustment = max(0.0, duration_cost - 0.25)
record_cost(..., cost_usd=adjustment)
```

This reveals the **true cost of Gemini video transcription**: ~$0.002/second

**Per-chunk cost for 45-minute segments:**
- Duration: 45 min × 60 sec/min = 2,700 seconds
- Cost: 2,700 sec × $0.002/sec = **$5.40 actual**
- Token-based estimate: ~$0.25 (massively underestimates)
- Duration adjustment recorded: $5.40 - $0.25 = **$5.15**

### The Conflict

**Per-lecture budget vs. per-chunk cost:**
- Configured limit: $5.00 per lecture
- Actual cost: $5.15+ just for the duration adjustment of chunk 1
- **Result: EVERY LECTURE FAILS AFTER CHUNK 1**

---

## VERIFICATION: Real Cost Data

From actual cost file (.tmp/daily_costs_2026-04-10.json) for Group 1 Lecture #5:
```json
{
  "timestamp": "2026-04-10T21:06:51.084583+04:00",
  "service": "gemini",
  "model": "gemini-2.5-flash",
  "purpose": "video_duration_adjustment chunk 1/1",
  "pipeline_key": "g1_l5",
  "cost_usd": 2.15
}
```

**Calculation to verify:**
- Logged adjustment: $2.15
- Formula: adjustment = (duration_sec × $0.002) - $0.25
- Therefore: duration_sec = ($2.15 + $0.25) / $0.002 = 1,200 seconds = **20 minutes**
- Conclusion: Group 1 Lecture #5 had a 20-minute video (shorter than typical 45-min chunk)

**For a typical 45-minute chunk:**
- Duration adjustment: (2,700 × $0.002) - $0.25 = $5.40 - $0.25 = **$5.15**
- Per-lecture limit: **$5.00**
- Budget exceeded: **YES** (after chunk 1 of 2)

---

## WHAT HAPPENS IF DAILY BUDGET ALSO BLOCKS?

**Hypothetical: Start g2_l10 when daily budget is $45 (already spent $5):**
1. Pre-flight check: $45 spent, $50 limit, $5 remaining ✅ PASSES (barely)
2. Chunk 1 processing: $5.15 added → daily total = $50.15
3. But wait — the check is `remaining > 0`, so $5 > 0 is TRUE at check time
4. Then we spend $5.15 and EXCEED the $50 limit during processing

**Issue:** Daily budget check happens BEFORE expensive operations, but individual API calls can cause overspend

**This is actually correct design** — pre-flight checks prevent NEW pipelines from starting, but in-progress pipelines are protected by the PER-LECTURE check.

---

## KEY FINDINGS

✅ **Cost tracker is recording costs correctly**
✅ **Budget checks are firing at the right times (pre-chunk and pre-pipeline)**
✅ **Daily budget protection is working** (pre-flight check blocks if daily budget exhausted)
🚫 **Per-lecture budget ($5.00) is too low**

**The problem:**
- One chunk of a typical 45-minute segment costs $5.15 just for duration adjustment
- Multi-chunk lectures need $15-20 total budget to complete
- Current $5.00 limit kills every lecture after chunk 1

---

## RECOMMENDATIONS

### Immediate Fix (Required)

**Update per-lecture cost limit to $20.00 USD:**

In `.env`:
```bash
LECTURE_COST_LIMIT_USD=20.0
```

Or in `tools/core/cost_tracker.py`:
```python
LECTURE_COST_LIMIT_USD = float(os.environ.get("LECTURE_COST_LIMIT_USD", "20.0"))
```

### Justification

**Cost breakdown per typical 90-minute lecture:**
- Chunk 1 (45 min): $5.15 (duration) + $0.06 (tokens) = $5.21
- Chunk 2 (45 min): $5.15 (duration) + $0.06 (tokens) = $5.21
- **Subtotal transcription:** ~$10.42
- Claude reasoning (extended thinking): ~$3-5
- Gemini writing (3 calls): ~$1-2
- **Total per lecture: ~$15-17**
- **Recommended limit: $20** (includes buffer)

### Daily Budget Impact

- 15 lectures per group × 2 groups = 30 lectures/month
- Current daily limit: $50
- If all lectures complete: 30 × $20 / 30 days = **$20/day average**
- Current limit of $50/day is more than sufficient

---

## NEXT STEPS

1. **Update LECTURE_COST_LIMIT_USD to 20.0**
2. **Test with next scheduled lecture** (Monday April 13, Group 2 Lecture #10)
3. **Verify lecture completes without budget blocking**
4. **Monitor daily costs** (should be ~$10-20/day during active periods)
5. **Consider alerts:** Warn at 70% of daily budget ($35/day)

