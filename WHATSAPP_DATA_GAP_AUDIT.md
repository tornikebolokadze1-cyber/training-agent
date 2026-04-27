# WhatsApp & Knowledge Data Gap Audit

**Date:** 2026-04-24
**Scope:** Identify and quantify every gap between what the Training Agent is capturing today vs. what is required for full retrospective analysis of the 30-lecture course (23 completed, 7 remaining).
**Status:** Mode A deliverable — diagnostic only, no production changes.

---

## Executive Summary

| Layer | Coverage | Severity |
|---|---|---|
| Lecture transcripts + Pinecone RAG | ~95% | OK |
| Lecture summaries | ~95% | OK |
| **WhatsApp group messages (raw archive)** | **< 10%** | **CRITICAL** |
| WhatsApp semantic fragments (Mem0) | ~60-80% estimated | Degraded |
| Scoring data (5-dim) | 100% rows, 78% fully populated | Fixable today |
| Obsidian vault sync | Partial, manual-only | Degraded |
| Cross-reference layer (lecture ↔ questions ↔ students) | 0% | Missing by design |

**One-sentence diagnosis:** The agent is losing its most valuable retrospective-analysis data source (WhatsApp conversations) on every restart. The capability to recover it exists and has a narrow time window.

---

## 1. WhatsApp Archive — CRITICAL GAP

### 1.1 Current state (verified in code)
`tools/services/whatsapp_assistant.py:141`:
```python
self._chat_history: dict[str, list[dict]] = {}
```
`:327`:
```python
self._chat_history[message.chat_id] = history[-15:]
```

- In-memory only. Lost on every restart.
- Trimmed to last 15 messages per chat.
- No SQLite / Postgres / file archive.

### 1.2 What partial archives exist
1. **Mem0** (`whatsapp_assistant.py:163-246`): semantic fragments only. LLM extracts "facts" — raw conversational flow, timing, reply threading are lost.
2. **Obsidian dumps** (`obsidian-vault/WhatsApp დისკუსიები/ჯგუფი N -- ჩატი.md`):
   - G1 chat file: 11,893 bytes, **last modified 2026-04-01** — 23 days stale.
   - G2 chat file: 11,025 bytes, last modified 2026-04-01 — 23 days stale.
   - Only "last 100 messages" as of that date.

### 1.3 Green API recoverable depth (probe 2026-04-24)

Probe script: `scripts/probe_green_api_history.py`. Report: `data/green_api_probe_report.json`.

| Metric | Group 1 | Group 2 |
|---|---|---|
| Messages returned at count=100 | 100 | 100 |
| Oldest timestamp | 2026-04-04 15:27 UTC | 2026-04-08 19:33 UTC |
| Newest timestamp | 2026-04-23 22:53 UTC | **2026-04-24 08:00 UTC** (~1h ago) |
| Days of history returned | **19.31** | **15.52** |
| Unique senders | 9 | 10 |
| Message types | text (35), extendedText (34), reaction (17), image (10), quoted (4) | text (56), extendedText (26), reaction (13), quoted (4), image (1) |

**Interpretation:** Green API still holds ~19 days of history. A single `count=1000` request likely yields **30-60 days**. The Obsidian-dump → today gap (2026-04-01 → 2026-04-24) is fully recoverable via Green API. Anything earlier would come from the existing dumps (partial) or be unrecoverable.

### 1.4 Recovery plan

**Immediate window (next 24-72h):**
- Run `count=1000` backfill probe to verify max depth.
- Export Green API history to JSON before it rotates out.

**Medium-term (after Railway unfreeze):**
- Implement the Postgres message archive per `.claude/plans/postgres_message_archive.md`.
- Webhook path: INSERT before assistant logic.
- Nightly backfill cron for gap-filling.

### 1.5 Data still permanently lost
Messages sent between last Obsidian dump (2026-04-01) and `oldest_ts_greenapi` (2026-04-04 for G1, 2026-04-08 for G2):
- **G1:** ~3 days window lost (Apr 1 → Apr 4)
- **G2:** ~7 days window lost (Apr 1 → Apr 8)

Partial reconstruction possible only from Mem0 semantic fragments (lossy). **Do not expect to recover these conversationally.**

---

## 2. Obsidian Vault — DEGRADED

### 2.1 Folder inventory
- `ანალიზი/ჯგუფი 1/`: 11 files (L1-L11) ✅ matches scores.db
- `ანალიზი/ჯგუფი 2/`: 12 files (L1-L12) ✅ matches scores.db
- `კონცეფციები/`: 129 entity files
- `WhatsApp დისკუსიები/`: 2 chat dumps + 1 index file
- `ლექციები/`, `ინსტრუმენტები/`, `პრაქტიკული მაგალითები/`: populated

### 2.2 Sync schedule
`scheduler.py` registers: pre-meeting reminders, nightly catch-all (02:00 Tbilisi). **No cron for `obsidian_sync`** — it runs manually via `python -m tools.integrations.obsidian_sync`.

### 2.3 Freshness audit (timestamps)
| File | Last modified | Stale |
|---|---|---|
| G1 L2-L5 analyses | 2026-03-28 / 29 | 26-27 days |
| G1 L6-L11 analyses | 2026-04-24 00:03-02:44 | **Fresh (manual re-run today)** |
| G2 L10-L12 analyses | 2026-04-24 (partial) | Fresh |
| WhatsApp dumps G1/G2 | 2026-04-01 | 23 days |
| Concept entities | Unknown (bulk timestamp check deferred) | — |

### 2.4 Action needed
1. Add `obsidian_sync --whatsapp` to `scheduler.py` as daily 04:00 Tbilisi cron.
2. Hook `obsidian_sync --group N --lecture X` into `orchestrator.py` lecture post-processing.
3. Full re-sync once after Anthropic credits top-up (to catch L2-L5 stale files if analysis prompts have evolved).

---

## 3. Scoring Data — FIXABLE TODAY

### 3.1 Schema check
```sql
CREATE TABLE lecture_scores (
    id, group_number, lecture_number,
    content_depth, practical_value, engagement,
    technical_accuracy, market_relevance, overall_score,
    composite, raw_score_text, processed_at
);
```

No `created_at`. Timestamp is `processed_at TEXT`.

### 3.2 Inventory
23 rows total. Matches user's reported 23 lectures (G1=11, G2=12).

### 3.3 NULL analysis

| Row | composite | dimensions | overall_score | Status |
|---|---|---|---|---|
| G1 L1 | 5.2 | all populated | 5.2 | OK |
| G1 L2 | 6.4 | all populated | **NULL** | Recoverable |
| G1 L3-7 | 5.4-6.5 | all populated | populated | OK |
| G1 L8 | 7.2 | all populated | **NULL** | Recoverable |
| G1 L9-11 | 6.2-7.0 | all populated | populated | OK |
| G2 L1-5 | 5.0-6.0 | all populated | populated | OK |
| G2 L6 | 6.2 | all populated | **NULL** | Recoverable |
| G2 L7-8 | 5.2-6.6 | all populated | populated | OK |
| G2 L9 | 6.6 | all populated | **NULL** | Recoverable |
| G2 L10 | 7.0 | all populated | populated | OK |
| G2 L11 | 6.0 | all populated | **NULL** | Recoverable |
| G2 L12 | 5.2 | all populated | populated | OK |

**All NULL rows have full dimensional scores.** Only the LLM-written "საერთო შეფასება" row from the analysis markdown is missing. Root cause: regex in `extract_scores()` didn't match the overall-score line format used in those 5 markdowns.

### 3.4 Recovery (dry-run verified 2026-04-24)

Script: `scripts/recover_null_scores.py`. Dry-run output:

```
G1 L2:  proposed overall_score=6.40  (composite fallback)
G1 L8:  proposed overall_score=7.20  (composite fallback)
G2 L6:  proposed overall_score=6.20  (composite fallback)
G2 L9:  proposed overall_score=6.60  (composite fallback)
G2 L11: proposed overall_score=6.00  (composite fallback)
```

All 5 markdown files exist. None contain the expected "საერთო შეფასება" pattern. Composite (mean of 5 dimensions) is used as the overall score — a defensible interpretation because composite *is* the numerical overall score; "საერთო შეფასება" in the prose is just the LLM's textual restatement.

**Caveat:** If re-running deep analysis with updated prompt produces an LLM-written overall that differs from composite by > 0.5, a decision is needed on which is authoritative. For now: composite is the safest value.

**To apply:** `python -m scripts.recover_null_scores --apply`

### 3.5 Correlation with known bugs
- `bug_railway_deploy_frozen.md` (2026-04-16): Railway frozen. G2 L11 processed_at `2026-04-23` but fell within post-freeze window where deep analysis may have been partial.
- `bug_anthropic_credits_exhausted.md` (2026-04-24): Credits exhausted. Re-analysis unavailable until top-up.
- G1 L2 NULL dates back to 2026-03-20 (first lecture) — likely early prompt version lacking the overall-score row.

---

## 4. Cross-Reference Layer — MISSING BY DESIGN

No join table exists linking:
- Lecture ↔ follow-up questions in WhatsApp
- Student ↔ per-lecture engagement
- Concept (Obsidian) ↔ confusion signals in chat

**This is the layer that would answer "which concepts never landed with students?"** It requires:
1. The Postgres archive (§1).
2. A sender_hash ↔ student_id mapping (roster).
3. A `lecture_windows` table with lecture start/end times.
4. Scheduled enrichment queries.

**Deferred to post-archive phase.** Design already in `.claude/plans/postgres_message_archive.md` §2.

---

## 5. Prioritized action list

| # | Action | Time | Risk | Depends on |
|---|---|---|---|---|
| 1 | Run `count=1000` Green API probe | 30 sec | None | — |
| 2 | Export Green API G1+G2 history to JSON before rotation | 5 min | None | 1 |
| 3 | Apply `recover_null_scores --apply` | 1 min | Reversible | User approval |
| 4 | Anthropic credits top-up | User action | — | — |
| 5 | Add `obsidian_sync` daily cron to scheduler | 20 min | Low | 4 |
| 6 | Railway token regeneration | User action | — | — |
| 7 | Implement Postgres message archive (Supabase) | 5-7 days | Medium (8 files) | 4, 6, user approval per scope-control rule |
| 8 | Webhook-path INSERT integration | Included in #7 | Medium | 7 |
| 9 | Backfill job from Green API + Obsidian dumps | Included in #7 | Low | 7, 8 |
| 10 | Lecture-window enrichment query | 2 hours | Low | 7 |
| 11 | Contextual Retrieval + Cohere Rerank (separate plan) | 1-2 days | Low | 4 |

**Today, with current blockers (credits, Railway freeze):** Actions 1, 2, 3 are doable. Action 5 is doable but deploy-blocked.

---

## 6. Unresolved questions for user

1. **Supabase vs DigitalOcean** for Postgres hosting? (Recommendation: Supabase free tier.)
2. **Apply scores recovery?** Dry-run complete, one command away.
3. **Does `count=1000` Green API probe risk hitting daily quota?** (Prior incident `bug_advisor_green_api_quota.md` — need quota headroom verified.)
4. **Consent / GDPR:** Were students notified that WhatsApp conversations would be analyzed? This affects retention policy design.
5. **Retention window:** Post-course, archive cold (Drive JSON) or keep hot for alumni analytics? 12 months default in design doc.

---

## 7. Artifacts produced by this audit

- `scripts/probe_green_api_history.py` — read-only API probe
- `data/green_api_probe_report.json` — probe output 2026-04-24 09:26 UTC
- `scripts/recover_null_scores.py` — dry-run-default scores recovery
- `.claude/plans/postgres_message_archive.md` — full design for message archive
- `WHATSAPP_DATA_GAP_AUDIT.md` — this document

Zero production modifications. No DB writes. No deployed code changes.
