# Labeling Audit — Group 3/4 → "მაისის ჯგუფი #1/#2"

**Date:** 2026-05-21
**Scope:** Verify that user-facing surfaces (WhatsApp messages, Drive folders, Google Docs, Obsidian notes, admin reports) use the cohort label "მაისის ჯგუფი #1/#2" instead of the raw internal ID "Group 3/4" / "ჯგუფი #3/#4".
**Method:** Read-only. Source grep + production `/admin/*` endpoint sampling + Drive API name lookups + Obsidian vault inspection.

---

## Verdict: **PARTIAL**

Production runtime is mostly correct — WhatsApp messages, Drive folders, admin reports, and lecture summaries all resolve to the cohort label via `GROUPS[n].name`. **However, the Obsidian sync generator still emits raw `ჯგუფი #{internal_id}` in two note-body template sites, producing files like `ლექცია 3 -- ანალიზი.md` whose heading reads "ჯგუფი #4" instead of "მაისის ჯგუფი #2". Two operator alert messages (token-revoked, permanent-failure) sent to Tornike's private WhatsApp also still use raw "Group {N}".**

---

## 1. Student-facing leakage — none

WhatsApp `/admin/recent-outgoing?minutes=4320` (last 72h, 144 messages across all four group chats):
- Raw `Group 3`, `Group 4`, `ჯგუფი #3`, `ჯგუფი #4`, `ჯგუფი 3`, `ჯგუფი 4`, `G3`, `G4`: **0 hits each**
- Cohort labels `მაისის ჯგუფი #1`: 16 hits; `მაისის ჯგუფი #2`: 23 hits

Reminder template (`tools/integrations/whatsapp_sender.py:478`) and post-meeting notification (`:520`) both resolve `group['name']` correctly. Lecture summary docs (`tools/services/transcribe_lecture.py:152, 214`) also use `group['name']` with safe fallback that does not trigger in production (GROUPS[3,4].name is set).

## 2. Drive folder names — clean

Live Drive API lookup of `DRIVE_GROUP3_FOLDER_ID`, `DRIVE_GROUP4_FOLDER_ID`, and their `_ANALYSIS_` siblings returns:
- `AI კურსი (მაისის ჯგუფი #1) - 2026`
- `AI კურსი (მაისის ჯგუფი #2) - 2026`
- `AI კურსი ანალიზი (მაისის ჯგუფი #1) - 2026`
- `AI კურსი ანალიზი (მაისის ჯგუფი #2) - 2026`

`.env` values for `GROUP3_NAME`, `GROUP4_NAME`, `GROUP3_FOLDER_NAME`, `GROUP4_FOLDER_NAME` all use the cohort label.

## 3. Admin endpoints — clean

- `GET /admin/groups-debug` — every `name` field returns the cohort label.
- `GET /admin/system-report` — uses `მაისის ჯგუფი #1/#2` in the header, per-group sections, and active-pipeline list.
- `GET /admin/lecture-status` — top-level `groups` dict keyed by cohort label (`მაისის ჯგუფი #1/#2`); only the per-row `"group": 3/4` numeric field carries the internal ID, which is intentional and machine-readable.

## 4. Obsidian vault — **CRITICAL (operator-visible)**

Generator code in `tools/integrations/obsidian_sync.py` emits raw `ჯგუფი #{g}` (internal id) in two human-readable template sites:

| File:line | Template string | Effect |
|-----------|-----------------|--------|
| `tools/integrations/obsidian_sync.py:807` | `> ჯგუფი #{g} -- ლექცია #{lec}` | Lecture-note quote line shows "ჯგუფი #4" |
| `tools/integrations/obsidian_sync.py:895` | `# ანალიზი -- ლექცია #{lec} (ჯგუფი #{g})` | Analysis-note H1 heading shows "(ჯგუფი #4)" |

Confirmed in produced files:
- `obsidian-vault/ლექციები/მაისის ჯგუფი #2/ლექცია 3.md:11` → `> ჯგუფი #4 -- ლექცია #3`
- `obsidian-vault/ანალიზი/მაისის ჯგუფი #2/ლექცია 3 -- ანალიზი.md:9` → `# ანალიზი -- ლექცია #3 (ჯგუფი #4)`

So far only one May-cohort lecture note is generated (G4 L3); the bug will reappear in every new May-cohort note as the pipeline writes them. The Obsidian directory name itself (`მაისის ჯგუფი #2/`) is correct — only the note body text is wrong.

Note: YAML frontmatter `group: {g}` (line 801, 891) and `tags: [ლექცია, ჯგუფი-{g}]` (line 799, 889) carry the internal ID. Frontmatter `group: 4` is defensible as machine-readable metadata, but the tag `ჯგუფი-4` is ambiguous (cohort #4 doesn't exist) — recommend either dropping it or switching to `ჯგუფი-მაისის-2`.

Legacy directories `obsidian-vault/ლექციები/ჯგუფი 1/`, `.../ჯგუფი 2/`, `obsidian-vault/ანალიზი/ჯგუფი 1/`, `.../ჯგუფი 2/` still exist on disk (15 files each, mirrors March cohort). Git status shows them flagged for deletion; obsidian_sync `_migrate_legacy_group_dirs()` (line 702) handles renames on startup. Confirm the deletion is committed before next sync run to avoid re-pollination.

## 5. Operator-facing WhatsApp alerts — **HIGH (Tornike sees these)**

Two `alert_operator()` call sites emit raw "Group {N}" even though a `_label()` helper at `tools/core/pipeline_retry.py:28` already resolves the cohort name:

| File:line | Current string | Should be |
|-----------|----------------|-----------|
| `tools/core/pipeline_retry.py:474` | `f"PERMANENT FAILURE: Group {record.group}, Lecture #{record.lecture}..."` | `f"PERMANENT FAILURE: {_label(record.group)}, ლექცია #{record.lecture}..."` |
| `tools/core/pipeline_retry.py:500` | `f"Pipeline HALTED for Group {record.group}, Lecture #{record.lecture}."` | `f"Pipeline HALTED for {_label(record.group)}, ლექცია #{record.lecture}."` |
| `tools/core/pipeline_retry.py:479, 505` (instructions inside the alert body) | `POST /retry-lecture with group={record.group}` | acceptable as-is (this is an API param value, not a display label) |

These fire to Tornike's WhatsApp number on permanent failure or OAuth-revocation events. They are operator-only but visible.

## 6. Operator-facing health-monitor strings — MEDIUM

`tools/core/health_monitor.py` defines `_group_label()` at line 49 that resolves the cohort name, but three CheckResult message builders bypass it and use raw `G{N} L{M}`:

| File:line | Current | Notes |
|-----------|---------|-------|
| `tools/core/health_monitor.py:613` | `f"G{pipeline.group} L{pipeline.lecture} in '{pipeline.state}' for {elapsed_hours:.1f}h"` | feeds `check_stuck_pipelines` → flows into health summaries and operator alerts |
| `tools/core/health_monitor.py:726` | `f"G{pipeline.group} L{pipeline.lecture} ({pipeline.state}) stale for {age_hours:.1f}h"` | `check_pipeline_state_drift` |
| `tools/core/health_monitor.py:795, 797` | `f"G{group_num} L{lecture_num}"` | `check_qdrant_scores_consistency` (terse compressed list) |

`G3 L2` style is arguably acceptable for compact diagnostic strings, but is inconsistent with the cohort-label convention used everywhere else in the same file (lines 577, 1027, 1038, 1049 all go through `_group_label`).

## 7. Other surfaces — acceptable

- **Comments / docstrings:** mentions of "G3 L2 RECITATION incident", "Group 3 with meeting_days=[2,5]", etc. in `gemini_analyzer.py`, `obsidian_sync.py`, `scheduler.py`, `whatsapp_sender.py`, `test_*.py` — these are internal developer-facing post-mortems. **Keep as-is.**
- **Logger calls:** `logger.info("...Group %d...", group_number)` throughout — log lines, not user-facing. **Keep as-is.**
- **CLI prints:** `tools/app/process_recording.py:101`, `tools/services/transcribe_lecture.py:678`, `tools/integrations/gdrive_manager.py:824`, `tools/integrations/whatsapp_sender.py:848` — operator terminal output. **Keep as-is.**
- **Pinecone/Qdrant embedding text:** `tools/services/analytics.py:1544` embeds the literal string `"Lecture scores backup Group {g} Lecture {lec}"` into a vector. Not user-visible; only seen if someone inspects a vector payload. **Keep as-is.**
- **Runbook `docs/operations/runbooks/add-new-course.md`:** uses "Group 3/4" as generic placeholders for "the next new cohort being added". **Keep as-is.**
- **Quality-gate pattern list** (`tools/core/quality_gates.py:265-285`) explicitly tolerates `ჯგუფი {N}` and `Group {N}` patterns in summaries — defensive design, not user-facing leakage.

---

## Recommended Fix PRs

### PR 1 — `fix(obsidian): use cohort label in note bodies` (HIGH, student/operator-visible)
Scope: `tools/integrations/obsidian_sync.py`
- Line 807: change `> ჯგუფი #{g} -- ლექცია #{lec}` → use `_group_label(g)` so the quote reads `> მაისის ჯგუფი #2 -- ლექცია #3`.
- Line 895: change `# ანალიზი -- ლექცია #{lec} (ჯგუფი #{g})` → use `_group_label(g)` so the heading reads `# ანალიზი -- ლექცია #3 (მაისის ჯგუფი #2)`.
- Optional: drop the `tags: [ლექცია, ჯგუფი-{g}]` tag or rename it to a cohort-stable form. The `group: {g}` frontmatter can stay as machine-readable metadata.
- Regenerate the affected May-cohort notes after merging so existing files get rewritten on next `sync_full()`.

### PR 2 — `fix(retry-alerts): use cohort label in permanent-failure and token-block alerts` (HIGH, Tornike-visible)
Scope: `tools/core/pipeline_retry.py`
- Lines 474, 500: replace `Group {record.group}` with `{_label(record.group)}`. Helper `_label()` already exists at line 28 — just use it.
- Keep the `group={record.group}` token inside the instructional body that tells Tornike which POST parameters to use; those are API params, not display labels.

### PR 3 — `chore(health-monitor): use _group_label() in stuck/drift/consistency checks` (MEDIUM, consistency)
Scope: `tools/core/health_monitor.py`
- Lines 613, 726, 795, 797: replace `G{group} L{lec}` with `_group_label(group)` + `ლექცია #{lec}`. Other check messages in the same file already use this helper — these three were missed.

### Optional PR 4 — `chore(scheduler/admin/server): unify fallback string` (LOW, polish)
Scope: `tools/app/scheduler.py:77`, `tools/app/admin_routes.py:89`, `tools/app/server.py:209`, `tools/core/pipeline_retry.py:31`, `tools/services/data_reconciliation.py:30`, `tools/services/drive_audit.py:26`, `tools/services/transcribe_lecture.py:36`
- All have a fallback `f"Group {group_number}"` that only triggers when GROUPS[n].name is missing. In practice the env-var bootstrap always sets `name`, so the fallback never fires. If it ever does, normalize to `f"ჯგუფი #{group_number}"` to match the Georgian house style. No behavior change today.

---

**Summary:** Two source-code sites (obsidian_sync.py) actively produce user-visible files containing raw "ჯგუფი #4" inside a directory correctly named "მაისის ჯგუფი #2" — this is the residual case Tornike noticed. Two operator-alert sites (pipeline_retry.py) and three health-check sites (health_monitor.py) also use raw labels but are seen only by Tornike. All runtime channels to students (WhatsApp, Drive shared folders, Google Doc summaries) are clean.
