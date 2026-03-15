# Recording Analysis Workflow

## Overview
Automatically processes Zoom recordings: downloads → uploads to Google Drive → analyzes with Gemini AI → delivers results.

## Trigger
- **Event**: Zoom `recording.completed` webhook
- **When**: ~30 minutes after each meeting ends (~22:30)
- **n8n Workflow ID**: 9K6kBOFPgG8xSuff (Zoom Recording → Python — **active**)
- **n8n Workflow ID**: 1mw2v47eliAk2l1s (Post-Processing Delivery)
- **Legacy workflow**: zoz7yWVzTMqhfVyd (superseded, inactive)

## Full Pipeline

### Phase 1: n8n (Workflow #2)
1. Zoom sends `recording.completed` webhook
2. Code node validates event, extracts download URL
3. Determines group number and lecture number
4. Sends recording details to Python server (POST /process-recording)
5. Returns 200 OK to Zoom

### Phase 2: Python Server (`transcribe_and_index()`)
1. Downloads Zoom recording to `.tmp/` (streaming, handles large files)
2. Creates lecture subfolder in Google Drive if needed
3. Uploads recording to Google Drive (resumable upload)
4. Splits video into 45-min chunks (ffmpeg stream copy)
5. Transcribes each chunk via Gemini 2.5 Pro (multimodal)
6. Runs **summary** (Claude Opus → Gemini 3.1 Pro) → Google Doc in Drive
7. Runs **gap analysis** (Claude Opus → Gemini 3.1 Pro) → private Drive folder
8. Runs **deep analysis** (Claude Opus → Gemini 3.1 Pro) → private Drive folder
9. Sends combined analysis link privately to Tornike via WhatsApp
10. Indexes transcript, summary, gap & deep analysis into Pinecone (RAG)
11. Sends group notification with recording + summary links via WhatsApp
12. Calls back to n8n Workflow #3 with results
13. Cleans up temporary files

### Phase 3: n8n (Workflow #3)
1. Receives callback from Python server
2. Checks success/error status
3. Sends email notification with processing results

## Output Locations

| Output | Where | Who sees it |
|--------|-------|-------------|
| Recording video | Google Drive → ლექცია #N folder | Shared |
| Lecture summary | Google Drive → ლექცია #N folder (Google Doc) | Shared |
| Gap analysis | Google Drive → კურსი #N ანალიზი folder (Google Doc) | Tornike only |
| Deep analysis | Google Drive → კურსი #N ანალიზი folder (Google Doc) | Tornike only |
| Analysis link | WhatsApp (private message) | Tornike only |
| Group notification | WhatsApp (group chat) | All participants |
| Knowledge vectors | Pinecone (training-course index) | WhatsApp assistant |
| Status notification | Email (via n8n) | Tornike only |

## Manual Processing

If the automated pipeline fails, use the CLI tool:
```bash
python -m tools.process_recording /path/to/video.mp4 --group 1 --lecture 3
```

Options:
- `--skip-drive` — skip Google Drive recording upload (analysis pipeline still runs fully)

## Troubleshooting

### Recording not triggering
- Check Zoom webhook subscription is active in Marketplace app
- Verify webhook URL points to n8n Recording Processor workflow
- Check n8n workflow is activated

### Python server not responding
- Verify server is running: `curl http://localhost:5001/health`
- Check server logs: `tail -f logs/training_agent.log`
- Check launchd service: `launchctl list | grep training-agent`
- Restart: `launchctl stop com.aipulsegeorgia.training-agent` (auto-restarts)
- Manual start: `python -m tools.orchestrator` or `./start.sh`

### Gemini analysis failing
- Check GEMINI_API_KEY is valid
- Verify video file isn't corrupted
- Check Gemini API quotas (video processing limits)
- For very long videos (>2hrs), the file may need chunking

### Google Drive upload failing
- Check OAuth token isn't expired (re-run auth flow if needed)
- Verify folder IDs in .env are correct
- Check Google Drive storage quota

## Expected Timeline
| Step | Duration |
|------|----------|
| Zoom processes recording | 5-30 min |
| Download to server | 5-15 min |
| Upload to Google Drive | 5-10 min |
| Video chunking (ffmpeg) | 1-2 min |
| Gemini transcription (per chunk) | 5-10 min |
| AI analysis (3 stages × 3 models) | 10-20 min |
| Pinecone indexing | 2-5 min |
| WhatsApp notifications | <1 min |
| **Total** | **~30-90 min after meeting ends** |
