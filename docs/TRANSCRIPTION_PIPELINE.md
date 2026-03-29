# Transcription Pipeline

Full lifecycle for processing a lecture recording: from raw video to delivered reports and indexed knowledge.

**Entry point**: `tools/services/transcribe_lecture.py` → `transcribe_and_index()`

**Caller**: `server.py` (webhook) or `scheduler.py` (cron fallback). The caller handles video download and upload to Drive — this pipeline handles everything after.

---

## Pipeline Flow Diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        transcribe_and_index()                           │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────────────┐                                                 │
│  │ Load pipeline state  │  ← .tmp/pipeline_state_g{N}_l{N}.json         │
│  │ Check cached results │  ← .tmp/g{N}_l{N}_{type}.txt                  │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐   Gemini 2.5 Flash (transcription)               │
│  │ Step 1: Analyze      │   Claude Opus 4.6 (reasoning)                  │
│  │   analyze_lecture()  │   Gemini 3.1 Pro Preview (Georgian writing)    │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐   Save .tmp/ files immediately                 │
│  │ Quality Gate         │   summary ≥ 500 chars                          │
│  │                      │   gap_analysis ≥ 300 chars                     │
│  │                      │   deep_analysis ≥ 300 chars                    │
│  └──────────┬──────────┘                                                 │
│             │ PASS                                                       │
│             ▼                                                            │
│  ┌─────────────────────┐                                                 │
│  │ Step 1.5: Scores     │   Extract scores → analytics DB (non-fatal)   │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐   Google Doc → shared ლექცია #N folder         │
│  │ Step 2: Drive        │   Title: "ლექცია #N — შეჯამება"               │
│  │   Summary Upload     │                                                │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐   Google Doc → private ანალიზი folder          │
│  │ Step 3: Drive        │   Gap + Deep analysis combined                 │
│  │   Private Report     │   Owner-only access                            │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐   Video link + summary link                    │
│  │ Step 4: WhatsApp     │   Only sent after Drive upload confirmed       │
│  │   Group Notification │                                                │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐   Link to private analysis doc                 │
│  │ Step 5: WhatsApp     │   Email fallback if WhatsApp fails             │
│  │   Private Report     │                                                │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐   4 content types: transcript, summary,        │
│  │ Step 6: Pinecone     │   gap_analysis, deep_analysis                  │
│  │   Index              │                                                │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐                                                 │
│  │ Step 7: Obsidian     │   Sync knowledge vault (non-fatal)             │
│  │   Sync               │   Concepts, relationships, files               │
│  └──────────┬──────────┘                                                 │
│             │                                                            │
│             ▼                                                            │
│  ┌─────────────────────┐                                                 │
│  │ Cleanup + Timing     │   Delete checkpoint files, log timing          │
│  │ mark_complete()      │                                                │
│  └──────────────────────┘                                                │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Pipeline State Machine

Defined in `tools/core/pipeline_state.py`. Each pipeline is backed by a JSON file at `.tmp/pipeline_state_g{group}_l{lecture}.json`.

### States

```
PENDING → DOWNLOADING → CONCATENATING → UPLOADING_VIDEO → TRANSCRIBING → ANALYZING → UPLOADING_DOCS → NOTIFYING → INDEXING → COMPLETE
                                                                                                                              ↑
Any state ────────────────────────────────────────────────────────────────────────────────────────────────────────────────→ FAILED
```

| State | Description |
|---|---|
| `PENDING` | Pipeline created, no work started |
| `DOWNLOADING` | Zoom recording download in progress (handled by caller) |
| `CONCATENATING` | Merging multi-part recordings (handled by caller) |
| `UPLOADING_VIDEO` | Uploading video to Google Drive (handled by caller) |
| `TRANSCRIBING` | Gemini transcription + Claude reasoning + Gemini writing |
| `ANALYZING` | Analysis stage (used by caller for finer granularity) |
| `UPLOADING_DOCS` | Summary and private report uploaded to Drive |
| `NOTIFYING` | WhatsApp group and private notifications |
| `INDEXING` | Pinecone vector indexing |
| `COMPLETE` | All steps finished successfully |
| `FAILED` | Pipeline failed — error message recorded |

### State Machine Properties

- **Immutable snapshots**: `PipelineState` is a frozen dataclass. Every transition produces a new instance.
- **Atomic writes**: State files are written via a temp file + `os.rename` (POSIX atomic).
- **File locking**: `try_claim_pipeline()` uses `fcntl.flock` to prevent race conditions between webhook, scheduler, and recovery.
- **Forward-only**: Backward transitions are blocked (logged as warning, state unchanged). Exception: `FAILED` can be reached from any state.
- **Terminal states**: `COMPLETE` and `FAILED` — no further transitions allowed.
- **Stale cleanup**: `cleanup_stale_failed()` auto-removes FAILED states older than 12 hours. `cleanup_completed()` removes COMPLETE states older than 24 hours.

### State Used by `transcribe_and_index()`

The pipeline function uses a subset of states. The earlier states (`DOWNLOADING`, `CONCATENATING`, `UPLOADING_VIDEO`) are managed by the caller (`server.py` / `scheduler.py`):

```
TRANSCRIBING → UPLOADING_DOCS → NOTIFYING → INDEXING → COMPLETE
```

---

## Step Details

### Step 1: Analysis Pipeline (`analyze_lecture()`)

**State**: `TRANSCRIBING`

Runs the multi-model AI pipeline from `tools/integrations/gemini_analyzer.py`:

1. **Gemini 2.5 Flash** — Multimodal transcription of the lecture video (processed in 45-minute chunks to stay within the 1M token context window).
2. **Claude Opus 4.6** — Extended thinking for deep reasoning and analysis (gap analysis, pedagogical evaluation).
3. **Gemini 3.1 Pro Preview** — Georgian language writing (summary, formatted reports).

**Resume**: If a valid transcript already exists in `.tmp/g{N}_l{N}_transcript.txt` (2000+ characters), transcription is skipped entirely.

**Crash resilience**: Immediately after analysis completes, all 4 output types are written to `.tmp/`:
- `g{N}_l{N}_transcript.txt`
- `g{N}_l{N}_summary.txt`
- `g{N}_l{N}_gap_analysis.txt`
- `g{N}_l{N}_deep_analysis.txt`

If the pipeline was already past `TRANSCRIBING` state on resume, these cached files are loaded directly and the entire analysis step is skipped.

### Quality Gate

Runs after Step 1, before any delivery. Blocks the pipeline if outputs are too short:

| Output | Minimum Length |
|---|---|
| `summary` | 500 characters |
| `gap_analysis` | 300 characters |
| `deep_analysis` | 300 characters |

On failure: logs an error, sends an operator alert via `alert_operator()`, raises `ValueError` which triggers `mark_failed()`.

### Step 1.5: Score Extraction

**Non-fatal**. Calls `save_scores_from_analysis()` from `tools/services/analytics.py` to extract numerical scores from the deep analysis text and persist them to the analytics database. If the score table is missing or malformed, a warning is logged and the pipeline continues.

### Step 2: Drive Summary Upload

**State**: `UPLOADING_DOCS`

- Creates a Google Doc titled `ლექცია #N — შეჯამება`.
- Uploads to the shared group folder: `AI კურსი (ჯგუფი #N)` → `ლექცია #N`.
- Students in the group can access this document.
- Wrapped in `@safe_operation("Drive summary upload", alert=True)`.
- **Resume**: If `pipeline.summary_doc_id` is already set, upload is skipped.

### Step 3: Drive Private Report Upload

**State**: `UPLOADING_DOCS` (same stage)

- Combines gap analysis and deep analysis into one Google Doc titled `ლექცია #N`.
- Uploads to the private analysis folder (`კურსი #N ანალიზი / ჯგუფი #N`).
- Only Tornike (the operator) has access.
- Wrapped in `@safe_operation("Drive private report upload", alert=True)`.
- **Resume**: If `pipeline.report_doc_id` is already set, upload is skipped.

### Step 4: WhatsApp Group Notification

**State**: `NOTIFYING`

- Sends a message to the training group's WhatsApp chat with links to the recording and summary.
- Only sent after Drive uploads are confirmed (requires `summary_doc_id`).
- Looks up the recording file ID by scanning the lecture's Drive folder for video files.
- If `summary_doc_id` is missing, notification is skipped with a warning.
- **Resume**: If `pipeline.group_notified` is `True`, notification is skipped.

### Step 5: Private Report to Tornike

**State**: `NOTIFYING` (same stage)

- Sends a WhatsApp message to Tornike with a link to the private analysis Google Doc.
- **Fallback chain**: If WhatsApp delivery fails, tries email via `send_email_fallback()`.
- If both WhatsApp and email fail, logs a `CRITICAL` error.
- **Resume**: If `pipeline.private_notified` is `True`, notification is skipped.

### Step 6: Pinecone Indexing

**State**: `INDEXING`

Indexes all 4 content types into Pinecone for RAG (used by the WhatsApp AI assistant):

| Content Type | Description |
|---|---|
| `transcript` | Full lecture transcription |
| `summary` | Lecture summary |
| `gap_analysis` | Knowledge gap analysis |
| `deep_analysis` | Deep pedagogical analysis |

Each content type is indexed separately via `index_lecture_content()` from `tools/integrations/knowledge_indexer.py`. Each call is wrapped in `@safe_operation("Pinecone indexing", alert=True)`.

- **Resume**: If `pipeline.pinecone_indexed` is `True`, indexing is skipped entirely.

### Step 7: Obsidian Sync

**Non-fatal**. Calls `sync_lecture()` from `tools/integrations/obsidian_sync.py` to update the Obsidian knowledge vault with extracted concepts, relationships, and lecture notes. Logs counts of concepts, relationships, and files updated. Failure is caught and logged without affecting pipeline status.

---

## Resume Support

The pipeline can resume after crashes at any point. Two mechanisms work together:

### 1. Pipeline State File

`.tmp/pipeline_state_g{N}_l{N}.json` tracks which steps have been completed via boolean flags:

| Flag | Skips |
|---|---|
| `summary_doc_id` set | Step 2 (Drive summary upload) |
| `report_doc_id` set | Step 3 (Drive private report upload) |
| `group_notified = True` | Step 4 (WhatsApp group notification) |
| `private_notified = True` | Step 5 (WhatsApp private report) |
| `pinecone_indexed = True` | Step 6 (Pinecone indexing) |

### 2. Cached Content Files

`.tmp/g{N}_l{N}_{type}.txt` files store raw analysis outputs. On resume, if the pipeline state is past `TRANSCRIBING` and cached transcript + summary exist, the entire analysis step (Step 1) is skipped.

---

## Pipeline Timing

Every stage is individually timed using `time.monotonic()`. At the end of the pipeline, a single log line summarizes all stage durations:

```
Pipeline timing: total=1847s | analysis=1680s | quality_gate=0s | score_extraction=1s | drive_summary=3s | drive_report=2s | whatsapp_group=1s | whatsapp_private=1s | pinecone=45s | obsidian=12s
```

The trace ID (`trace_id` parameter or auto-generated `g{N}_l{N}`) is included in every log message for end-to-end correlation.

---

## Cleanup

After the full pipeline succeeds:

1. **Checkpoint files**: `cleanup_checkpoints(group, lecture)` from `gemini_analyzer.py` removes intermediate checkpoint files created during the multi-chunk transcription process.
2. **State files**: `cleanup_completed()` (called separately by the scheduler) removes `COMPLETE` state files older than 24 hours.
3. **Failed state files**: `cleanup_stale_failed()` removes `FAILED` state files older than 12 hours, allowing retry.
4. **Cached content files** (`.tmp/g{N}_l{N}_*.txt`) are NOT automatically cleaned — they serve as a backup and resume source.

---

## Error Handling

- **Top-level try/except**: Any unhandled exception in the pipeline calls `mark_failed(pipeline, str(e))` before re-raising.
- **`@safe_operation` decorator**: Wraps individual steps (Drive uploads, WhatsApp notifications, Pinecone indexing). Catches exceptions, logs them, optionally sends operator alerts, and returns a default value instead of crashing the pipeline.
- **Non-fatal steps**: Score extraction (Step 1.5) and Obsidian sync (Step 7) are wrapped in their own try/except blocks. Failures are logged but do not affect pipeline completion.
- **Operator alerts**: Critical failures trigger `alert_operator()` which sends a WhatsApp message to Tornike as a last-resort notification.

---

## CLI Usage

```bash
python -m tools.services.transcribe_lecture <group_number> <lecture_number> <video_path>
```

Example:
```bash
python -m tools.services.transcribe_lecture 1 5 .tmp/recording.mp4
```

Validates that group is 1 or 2 and lecture is between 1 and 15.
