# Recording Analysis Workflow

## Overview
Automatically processes Zoom recordings: downloads → uploads to Google Drive → analyzes with Gemini AI → delivers results.

## Trigger
- **Event**: Zoom `recording.completed` webhook
- **When**: ~30 minutes after each meeting ends (~22:30)
- **n8n Workflow ID**: zoz7yWVzTMqhfVyd (Recording Processor)
- **n8n Workflow ID**: 1mw2v47eliAk2l1s (Post-Processing Delivery)

## Full Pipeline

### Phase 1: n8n (Workflow #2)
1. Zoom sends `recording.completed` webhook
2. Code node validates event, extracts download URL
3. Determines group number and lecture number
4. Sends recording details to Python server (POST /process-recording)
5. Returns 200 OK to Zoom

### Phase 2: Python Server
1. Downloads Zoom recording to `.tmp/` (streaming, handles large files)
2. Creates lecture subfolder in Google Drive if needed
3. Uploads recording to Google Drive (resumable upload)
4. Uploads video to Gemini File API
5. Waits for Gemini processing (~5-10 minutes)
6. Runs **summarization prompt** → creates Google Doc in Drive
7. Runs **gap analysis prompt** → sends privately to Tornike via WhatsApp
8. Calls back to n8n Workflow #3 with results
9. Cleans up temporary files

### Phase 3: n8n (Workflow #3)
1. Receives callback from Python server
2. Checks success/error status
3. Sends email notification with processing results

## Output Locations

| Output | Where | Who sees it |
|--------|-------|-------------|
| Recording video | Google Drive → ლექცია #N folder | Shared |
| Lecture summary | Google Drive → ლექცია #N folder (Google Doc) | Shared |
| Gap analysis | WhatsApp (private message) | Tornike only |
| Status notification | Email | Tornike only |

## Manual Processing

If the automated pipeline fails, use the CLI tool:
```bash
python -m tools.process_recording /path/to/video.mp4 --group 1 --lecture 3
```

Options:
- `--skip-drive` — skip Google Drive upload (for testing)
- `--skip-whatsapp` — skip WhatsApp message (for testing)

## Troubleshooting

### Recording not triggering
- Check Zoom webhook subscription is active in Marketplace app
- Verify webhook URL points to n8n Recording Processor workflow
- Check n8n workflow is activated

### Python server not responding
- Verify server is running: `curl http://localhost:5000/health`
- Check server logs for errors
- Restart: `python -m tools.server`

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
| Upload to Gemini | 5-15 min |
| Gemini processing | 5-10 min |
| AI analysis (2 prompts) | 3-5 min |
| **Total** | **~30-85 min after meeting ends** |
