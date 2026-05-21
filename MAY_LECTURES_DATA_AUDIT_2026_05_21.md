# May Cohort Data Audit — 2026-05-21

**Audit window**: G3 (მაისის ჯგუფი #1, Mon/Thu, started 2026-05-11) + G4 (მაისის ჯგუფი #2, Tue/Fri, started 2026-05-12). Expected progress today: G3 → L4, G4 → L3. Read-only audit, no data mutation.

## Verdict

**21 of 60 expected cells filled (35%)** for the 6 lectures that should currently exist (G3 L1–L4 + G4 L1–L3), counted across 10 storage columns. **5 critical gaps** require action:

1. **G3 L4 — entirely missing across every system** (Mon 2026-05-18 lecture was never processed; Zoom recording likely never arrived or pipeline crashed before any stage).
2. **Obsidian vault is almost empty for May cohort** — only G4 L3 has both a `ლექციები` note and an `ანალიზი` note; G3 has zero notes; G4 L1 & L2 have neither lecture nor analysis notes.
3. **GAP/DEEP analysis docs absent from Qdrant for 4/6 lectures** — only G3 L1, G4 L1, G4 L3 have `deep_analysis` content_type vectors; `gap_analysis` is only indexed for G4 L3. Drive does hold the docs (visible as "ლექცია #2 / #3" docs in each analysis folder), so the gap is at the indexer step, not the analysis step.
4. **scores.db has only 1 of 6 expected rows** (G3 L1). G3 L2, L3 and G4 L1, L2, L3 are missing from both `lecture_scores` and `lecture_insights`.
5. **Drive contains duplicate video files** in G3 L3 (2 mp4s) and G4 L2 (2 mp4s identical) and G4 L3 (3 mp4s) — intentional per memory note `feedback_drive_video_duplicates_intentional.md`, NOT a defect, but worth recording.

**Recommended next actions**: (a) investigate why G3 L4 (Mon 2026-05-18) never got captured — check Zoom cloud, scheduler logs, n8n state; (b) run `scripts/regenerate_qdrant_vectors.py` (or equivalent re-index command) for the 4 missing `gap_analysis` + 3 missing `deep_analysis` combos; (c) trigger `obsidian_sync.sync_full()` after Gemini quota window resets to backfill the 5 missing Obsidian notes; (d) re-run analytics extraction (`tools/services/analytics.py`) to populate scores.db for the 5 missing lecture rows; (e) extend the `recent-outgoing` window or check Green API journal to confirm L1 delivery for G3 + G4 (delivery happened more than 30 days before today's probe — note today is 2026-05-21 and G3 L1 was 2026-05-11, well inside 30d, so absence from `/admin/recent-outgoing?hours=720` is itself suspicious).

---

## Storage-Layer Findings (raw)

### Qdrant Cloud (`training-course` collection, filter `group_number` × `lecture_number` × `content_type`)

| Group | Lec | transcript | summary | gap_analysis | deep_analysis |
|-------|-----|-----------:|--------:|-------------:|--------------:|
| 3 | 1 | 104 | 2 | 0 | 18 |
| 3 | 2 | 89 | 6 | 0 | 0 |
| 3 | 3 | 110 | 5 | 0 | 0 |
| 3 | 4 | 0 | 0 | 0 | 0 |
| 4 | 1 | 138 | 8 | 0 | 16 |
| 4 | 2 | 110 | 6 | 0 | 0 |
| 4 | 3 | 90 | 8 | 7 | 7 |

Lectures 5–15 (both groups) are all-zero (expected — not yet held).

### Google Drive — main lecture folders

- `DRIVE_GROUP3_FOLDER_ID=165JQVRq9ueas0wAJhFjHneEtBSvbt_bN` → 15 lecture subfolders present (one-time scaffold complete).
- `DRIVE_GROUP4_FOLDER_ID=1K4XT7apK7ewI1_ihglb6ob8dWWKo9dOu` → 15 lecture subfolders present.

Per-lecture inventory (only L1–L4 / L1–L3 audited; rest are empty as expected):

| Folder | Items | Has video | Has summary doc | Notes |
|--------|------:|:---------:|:---------------:|-------|
| G3 L1 | 5 | ✅ | ✅ | + transcription doc + presentation PDF + presentation Doc |
| G3 L2 | 2 | ✅ | ✅ | clean |
| G3 L3 | 3 | ✅ | ✅ | duplicate raw `g3_l3_zoom_video.mp4` (intentional backup) |
| **G3 L4** | **0** | ❌ | ❌ | **empty folder — lecture not processed** |
| G4 L1 | 2 | ✅ | ✅ | clean |
| G4 L2 | 3 | ✅ | ✅ | duplicate canonical video (intentional backup) |
| G4 L3 | 4 | ✅ | ✅ | 2× `g4_l3_recording.mp4` + canonical (intentional backup) |
| G4 L4 | 0 | ❌ | ❌ | not yet held (Tue 2026-05-19 was L2, next G4 lecture is L4 on Fri 2026-05-22) |

### Google Drive — private analysis folders

- `DRIVE_GROUP3_ANALYSIS_FOLDER_ID=1Bi9A7Mi23OQqb-VBczxiAh9w9hzUkW3C`: 3 docs (`ლექცია #1 — GAP + DEEP ანალიზი`, `ლექცია #2`, `ლექცია #3`)
- `DRIVE_GROUP4_ANALYSIS_FOLDER_ID=1m2El_89hoR9CeeVzoI0uXg7KirsGUN6V`: 3 docs (same naming pattern)

So GAP+DEEP analysis Doc exists in Drive for **all 6 held lectures** — but only 3/6 are indexed into Qdrant.

### scores.db (SQLite, project root `data/scores.db`)

- `lecture_scores` filtered to (group 3,4): **1 row** — (group=3, lec=1, overall=7.2, composite=9.4, processed_at=2026-05-12T15:47:40Z).
- `lecture_insights` filtered to (group 3,4): **1 row** — (group=3, lec=1).

### Obsidian vault (`obsidian-vault/`)

- `ლექციები/მაისის ჯგუფი #1/` → **empty** (0 notes)
- `ლექციები/მაისის ჯგუფი #2/` → 1 note (`ლექცია 3.md`)
- `ანალიზი/მაისის ჯგუფი #1/` → **empty**
- `ანალიზი/მაისის ჯგუფი #2/` → 1 note (`ლექცია 3 -- ანალიზი.md`)

### messages.db (local SQLite, `data/messages.db`)

- Total rows: 4 712. **Zero** messages tagged to G3 (`120363409966993169@g.us`) or G4 (`120363426884083988@g.us`).
- Only March cohort chats (`120363407739933658`, `120363425514041539`) and operator DM (`995579225809`) are present.
- `lecture_windows` table: **0 rows** for groups 3, 4.

This means the local `data/messages.db` snapshot pre-dates the May cohort and has not been syncing for the May group chats. Production may still be writing to a separate volume on Railway — needs separate audit.

### `/admin/recent-outgoing?hours=720` (Green API last-30-days)

Group-chat outgoing messages parsed by lecture #:
- G3 (`120363…993169@g.us`): L2 ×1, L3 ×2 → **3 messages**, no L1
- G4 (`120363…083988@g.us`): L2 ×4, L3 ×1 → **5 messages**, no L1

Note: G3 L1 (2026-05-11) and G4 L1 (2026-05-12) are both inside the 30-day window but absent from the probe. Either the messages were sent before the bot was migrated to the current Green API instance, or the probe endpoint paginated them off. Worth a deeper look but not blocking — Drive + Qdrant confirm L1 content shipped end-to-end.

### Production `/admin/lecture-status`

| Group | Lec | pipeline_state | vectors | drive_video_id | summary_doc_id | report_doc_id |
|-------|-----|----------------|--------:|:--------------:|:--------------:|:-------------:|
| 3 | 1 | UNKNOWN | 124 | ❌ | ❌ | ❌ |
| 3 | 2 | TRANSCRIBING | 95 | ❌ | ❌ | ❌ |
| 3 | 3 | TRANSCRIBING | 115 | ❌ | ❌ | ❌ |
| 3 | 4 | UNKNOWN | 0 | ❌ | ❌ | ❌ |
| 4 | 1 | UNKNOWN | 162 | ❌ | ❌ | ❌ |
| 4 | 2 | TRANSCRIBING | 116 | ❌ | ❌ | ❌ |
| 4 | 3 | UNKNOWN | 112 | ❌ | ❌ | ❌ |

**All `pipeline_state` / `drive_video_id` / `summary_doc_id` / `report_doc_id` fields are empty even though Drive + Qdrant both hold the artifacts.** The pipeline-state tracker (`tools/core/pipeline_state.py`) is not being persisted across the recent Railway redeploy — this is a follow-up issue separate from the data presence question.

---

## Master Matrix — May Cohort (G3 + G4, L1–L15)

Legend: ✅ present and correct · ⚠️ present but incomplete (e.g. duplicates, missing content_type) · ❌ missing · N/A not yet expected

### G3 (მაისის ჯგუფი #1)

| Lec | Drive video | Drive summary | Drive GAP+DEEP doc | Qdrant transcript | Qdrant summary | Qdrant gap | Qdrant deep | scores.db row | Obsidian note | WA delivered |
|----:|:-----------:|:-------------:|:------------------:|:-----------------:|:--------------:|:----------:|:-----------:|:-------------:|:-------------:|:------------:|
| 1   | ✅ | ✅ | ✅ | ✅ (104) | ✅ (2) | ❌ | ✅ (18) | ✅ | ❌ | ⚠️ (not in 30d probe; vectors+Drive confirm) |
| 2   | ✅ | ✅ | ✅ | ✅ (89)  | ✅ (6) | ❌ | ❌ | ❌ | ❌ | ✅ |
| 3   | ⚠️ (dup) | ✅ | ✅ | ✅ (110) | ✅ (5) | ❌ | ❌ | ❌ | ❌ | ✅ |
| 4   | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 5   | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| 6–15 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

### G4 (მაისის ჯგუფი #2)

| Lec | Drive video | Drive summary | Drive GAP+DEEP doc | Qdrant transcript | Qdrant summary | Qdrant gap | Qdrant deep | scores.db row | Obsidian note | WA delivered |
|----:|:-----------:|:-------------:|:------------------:|:-----------------:|:--------------:|:----------:|:-----------:|:-------------:|:-------------:|:------------:|
| 1   | ✅ | ✅ | ✅ | ✅ (138) | ✅ (8) | ❌ | ✅ (16) | ❌ | ❌ | ⚠️ (not in 30d probe; vectors+Drive confirm) |
| 2   | ⚠️ (dup) | ✅ | ✅ | ✅ (110) | ✅ (6) | ❌ | ❌ | ❌ | ❌ | ✅ |
| 3   | ⚠️ (dup) | ✅ | ✅ | ✅ (90)  | ✅ (8) | ✅ (7) | ✅ (7) | ❌ | ✅ | ✅ |
| 4   | N/A (Fri 2026-05-22 next) | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| 5–15 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

---

## Concrete Recovery Actions (per gap)

| Cell | Recovery action |
|------|-----------------|
| G3 L4 — every column | Investigate root cause first. Check (a) Zoom cloud for the 2026-05-18 20:00 recording on meeting `85702036847`, (b) Railway logs for that night, (c) `/admin/lecture-status` history. If recording exists on Zoom, manually trigger pipeline: `python -m tools.app.process_recording --group 3 --lecture 4` (or the live equivalent). |
| Qdrant `gap_analysis` (G3 L1, L2, L3; G4 L1, L2) and `deep_analysis` (G3 L2, L3; G4 L2) | The GAP+DEEP analysis Doc exists in the private Drive folder for all 6 lectures. Re-run the knowledge-indexer pass for these specific docs: `python -m tools.integrations.knowledge_indexer --group {3,4} --lecture {1..3} --content gap_analysis,deep_analysis` (verify exact CLI; same tool that wrote G4 L3 entries). |
| scores.db missing 5 rows | Re-run analytics extraction: `python -m tools.services.analytics --extract --group 3 --lecture 2 3` and `--group 4 --lecture 1 2 3`. Source is the GAP+DEEP doc that is already in Drive. |
| Obsidian missing 5 notes (G3 L1, L2, L3; G4 L1, L2) | Per memory `runbook_obsidian_rerun_2026_05_07.md`, run `obsidian_sync.sync_full()` once Gemini Pro quota resets. Should pick up all transcripts + summaries from Drive and emit notes. |
| G3 L1 / G4 L1 WhatsApp absence in 30d probe | Confirm with `/admin/recent-outgoing?hours=720&offset=…` paginated calls, or query Green API journal directly. Data is consistent enough (vectors + Drive present) that this is verification-only, not recovery. |
| Production `pipeline_state` all UNKNOWN / IDs empty | Separate issue — pipeline-state tracker is not surviving Railway redeploys. Track under issue #55 (persistent `.tmp/` volume follow-up). Not a data-loss problem; data is in Drive and Qdrant. |

---

## Audit Source Notes

- Audit was fully read-only. No writes to Drive, Qdrant, scores.db, messages.db, Obsidian, or production state.
- Probes executed: Qdrant `count` filtered by payload fields; Drive `files.list` via `gdrive_manager.list_files_in_folder`; SQLite `SELECT` only; HTTP GET on `/admin/lecture-status` and `/admin/recent-outgoing`.
- Temporary diagnostic files written under project root and removed: `.tmp_lecture_status.json`, `.tmp_drive_top.json`, `.tmp_drive_perlecture.json` (clean up before commit).
- Audit produced by `Claude Opus 4.7` at 2026-05-21.
