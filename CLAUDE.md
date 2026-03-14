# Training Agent — Project Instructions

## Project Overview
Automated AI training session management system for Zoom-based Georgian language lectures. Handles the full lifecycle: pre-meeting reminders → recording → AI analysis → reporting.

**Architecture**: Hybrid (n8n orchestration + Python execution)
- n8n handles: scheduling, triggers, email/WhatsApp notifications
- Python handles: large file downloads, Gemini multimodal analysis, Google Drive uploads

## WAT Framework
This project follows the **WAT framework** (Workflows, Agents, Tools):
- `workflows/` — Markdown SOPs defining what to do and how
- `tools/` — Python scripts for deterministic execution
- `.env` — API keys and secrets (NEVER commit)
- `.tmp/` — Temporary processing files (auto-cleaned)

## Two Training Groups
| Group | Days | Time (GMT+4) | Drive Folder |
|-------|------|-------------|--------------|
| #1 | Tuesday, Friday | 20:00-22:00 | AI კურსი (მარტის ჯგუფი #1. 2026) |
| #2 | Monday, Thursday | 20:00-22:00 | AI კურსი (მარტის ჯგუფი #2. 2026) |

15 lectures per group. Lecture #1 completed for both.

## System Components

### Python Tools (`tools/`)
| Tool | Purpose |
|------|---------|
| `config.py` | Shared config: groups, schedules, folder IDs, Gemini prompts |
| `gdrive_manager.py` | Google Drive: folder creation, resumable upload, Doc creation |
| `gemini_analyzer.py` | Gemini multimodal: video upload, transcription, analysis |
| `whatsapp_sender.py` | WhatsApp messaging via Green API (group + private) |
| `manychat_sender.py` | Legacy ManyChat API (deprecated — replaced by Green API) |
| `process_recording.py` | CLI tool: manual recording processing and testing |
| `server.py` | FastAPI webhook server (receives n8n calls) |
| `scheduler.py` | APScheduler: cron jobs for pre/post meeting automation |
| `zoom_manager.py` | Zoom S2S OAuth: meeting creation, recording download |
| `orchestrator.py` | Unified entry point: APScheduler + FastAPI on single loop |
| `email_sender.py` | Gmail OAuth2 (backup — Zoom handles invitations directly) |

### n8n Workflows (aipulsegeorgia2025.app.n8n.cloud)
1. **Pre-Meeting Reminders** — 18:00 trigger → email + WhatsApp Zoom link
2. **Recording Processor** — Zoom webhook → Python handoff
3. **Post-Processing Delivery** — Python callback → private WhatsApp report

## Critical Rules
- NEVER commit `.env`, `credentials.json`, or `token.json`
- NEVER edit production n8n workflows directly — copy first
- Always validate n8n workflows before activating
- Use resumable uploads for files over 10MB
- Gemini prompts are in Georgian — don't translate them
- Gap analysis reports go ONLY to Tornike (private WhatsApp), never to Drive
- Lecture summaries go to Google Drive in the correct ლექცია folder
- Python server must validate WEBHOOK_SECRET on all incoming requests

## API Integrations
- **Zoom**: Server-to-Server OAuth (meeting:write, recording:read)
- **Google Drive/Docs**: OAuth2 with refresh tokens
- **Gemini**: API key, hybrid models (2.5 Flash for video, 3.1 Pro for text analysis)
- **WhatsApp (Green API)**: REST API via WhatsApp Web QR code connection
- **Email**: Handled by Zoom directly (meeting_invitees in settings)

## Code Style
- Python 3.12+, type hints, async where beneficial
- Logging via `logging` module (no print in production)
- All secrets from `.env` via `python-dotenv`
- Error handling: retry with exponential backoff for API calls
- Georgian text: UTF-8 everywhere, folder names in Georgian script

## Running Locally
```bash
# Install dependencies
pip install -r requirements.txt

# One-time: create lecture folders
python -m tools.gdrive_manager

# Start the full system (scheduler + webhook server)
python -m tools.orchestrator
```
