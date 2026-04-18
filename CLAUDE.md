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

15 lectures per group.

## System Components

### Python Tools (`tools/`)
| Tool | Purpose |
|------|---------|
| `core/config.py` | Shared config: groups, schedules, folder IDs, Gemini/Claude prompts |
| `integrations/gdrive_manager.py` | Google Drive: folder creation, resumable upload, Doc creation |
| `integrations/gemini_analyzer.py` | Multi-model pipeline: Gemini transcription → Claude reasoning → Gemini writing |
| `services/transcribe_lecture.py` | Main analysis pipeline (single source of truth for all entry points) |
| `integrations/knowledge_indexer.py` | Pinecone RAG: chunk, embed, upsert lecture content for assistant |
| `integrations/whatsapp_sender.py` | WhatsApp messaging via Green API (group + private) |
| `services/whatsapp_assistant.py` | AI assistant "მრჩეველი": Claude reasoning + Gemini Georgian response |
| `services/analytics.py` | Course analytics and reporting |
| `app/process_recording.py` | CLI tool: manual recording processing and testing |
| `app/server.py` | FastAPI webhook server (receives n8n calls + WhatsApp messages) |
| `app/scheduler.py` | APScheduler: cron jobs for pre/post meeting automation |
| `integrations/zoom_manager.py` | Zoom S2S OAuth: meeting creation, recording download |
| `app/orchestrator.py` | Unified entry point: APScheduler + FastAPI on single loop |

### n8n Workflows
1. **Pre-Meeting Reminders** — 18:00 trigger → email + WhatsApp Zoom link
2. **Zoom Recording → Python** — Zoom webhook + CRC → Python handoff
3. **Post-Processing Delivery** — Python callback → email notification

## Critical Rules
- NEVER commit `.env`, `credentials.json`, or `token.json`
- NEVER edit production n8n workflows directly — copy first
- Always validate n8n workflows before activating
- Use resumable uploads for files over 10MB
- Gemini prompts are in Georgian — don't translate them
- Gap + deep analysis reports go to private Drive folder (კურსი #N ანალიზი) + link via private WhatsApp
- Lecture summaries go to Google Drive in the correct ლექცია folder (shared with group)
- Python server must validate WEBHOOK_SECRET on all incoming requests (exception: `/zoom-webhook` uses Zoom's own HMAC-SHA256 signature instead, since Zoom cannot attach custom Authorization headers)

## API Integrations
- **Zoom**: Server-to-Server OAuth (meeting:write, recording:read)
- **Google Drive/Docs**: OAuth2 with refresh tokens
- **Gemini**: API key, 2.5 Flash for video transcription, 3.1 Pro Preview for Georgian text
- **Claude/Anthropic**: API key, Opus 4.6 with extended thinking for analysis reasoning
- **Pinecone**: Vector DB (gemini-embedding-001, 3072 dims) for course knowledge RAG
- **WhatsApp (Green API)**: REST API via WhatsApp Web QR code connection
- **Email**: Gmail OAuth2 (backup — Zoom handles meeting invitations directly)

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
python -m tools.app.orchestrator    # or: ./start.sh

# Run tests
pip install -r requirements-dev.txt
pytest tools/tests/test_core.py -v
```

## Deployment (Railway)
- **Platform**: Railway PaaS with Docker (multi-stage build)
- **Entry point**: `python -m tools.app.orchestrator` (Dockerfile CMD)
- **Config files**: `Dockerfile`, `railway.toml`, `.dockerignore`
- **Credentials on Railway**: base64-encode JSON files into env vars (`GOOGLE_CREDENTIALS_JSON_B64`, etc.)
- **Deploy**: `git push` to `main` triggers GitHub Actions → Railway CLI deploy
- **Manual deploy**: `railway up` from project root
- See `.env.example` for all required environment variables

## Operations
- **Server port**: 5001 locally (Railway injects `$PORT`)
- **Health check**: `curl http://localhost:5001/health`
- **Full status**: `curl http://localhost:5001/status`
- **Local service**: `com.aipulsegeorgia.training-agent` (launchd, auto-restarts)
- **Logs**: `logs/training_agent.log` locally (rotating, 10 MB × 5); Railway captures stdout
- **OpenAPI docs**: `/docs` available locally only (disabled on Railway)
- **Stale task recovery**: tasks running >4 hours are auto-evicted from deduplication tracker
- **Operator alerts**: `alert_operator()` in `whatsapp_sender.py` — last-resort WhatsApp notification

## Paperclip Integration

Exposes a bridge endpoint for Paperclip (the company orchestration platform) to dispatch tasks to this agent.

- **Endpoint:** `POST /paperclip/task` (JSON in, JSON out)
- **Auth:** `Authorization: Bearer $PAPERCLIP_WEBHOOK_SECRET`
- **Health:** `GET /paperclip/health`
- **Status:** `GET /paperclip/status`

This agent is registered in the AI Pulse Georgia company at `http://localhost:3100` as "Training Operations Lead" (adapter: http, url: http://localhost:8000/paperclip/task).

The APScheduler-driven autonomous pipeline keeps running independently; the Paperclip bridge is additive — it exposes on-demand task execution without replacing the scheduler.

**Bridge file:** `tools/app/paperclip_bridge.py` — contains `PaperclipTask` / `PaperclipResponse` Pydantic models, task-type routing stubs, and health/status endpoints.
