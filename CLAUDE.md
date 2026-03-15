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
| `config.py` | Shared config: groups, schedules, folder IDs, Gemini/Claude prompts |
| `gdrive_manager.py` | Google Drive: folder creation, resumable upload, Doc creation |
| `gemini_analyzer.py` | Multi-model pipeline: Gemini transcription → Claude reasoning → Gemini writing |
| `transcribe_lecture.py` | Main analysis pipeline (single source of truth for all entry points) |
| `knowledge_indexer.py` | Pinecone RAG: chunk, embed, upsert lecture content for assistant |
| `whatsapp_sender.py` | WhatsApp messaging via Green API (group + private) |
| `whatsapp_assistant.py` | AI assistant "მრჩეველი": Claude reasoning + Gemini Georgian response |
| `manychat_sender.py` | Legacy ManyChat API (deprecated — replaced by Green API) |
| `process_recording.py` | CLI tool: manual recording processing and testing |
| `server.py` | FastAPI webhook server (receives n8n calls + WhatsApp messages) |
| `scheduler.py` | APScheduler: cron jobs for pre/post meeting automation |
| `zoom_manager.py` | Zoom S2S OAuth: meeting creation, recording download |
| `orchestrator.py` | Unified entry point: APScheduler + FastAPI on single loop |
| `email_sender.py` | Gmail OAuth2 (backup — Zoom handles invitations directly) |

### n8n Workflows (aipulsegeorgia2025.app.n8n.cloud)
1. **Pre-Meeting Reminders** (Hsa5YDWrOytFxAL5) — 18:00 trigger → email + WhatsApp Zoom link
2. **Zoom Recording → Python** (9K6kBOFPgG8xSuff, active) — Zoom webhook + CRC → Python handoff
3. **Post-Processing Delivery** (1mw2v47eliAk2l1s) — Python callback → email notification

## Critical Rules
- NEVER commit `.env`, `credentials.json`, or `token.json`
- NEVER edit production n8n workflows directly — copy first
- Always validate n8n workflows before activating
- Use resumable uploads for files over 10MB
- Gemini prompts are in Georgian — don't translate them
- Gap + deep analysis reports go to private Drive folder (კურსი #N ანალიზი) + link via private WhatsApp
- Lecture summaries go to Google Drive in the correct ლექცია folder (shared with group)
- Python server must validate WEBHOOK_SECRET on all incoming requests

## API Integrations
- **Zoom**: Server-to-Server OAuth (meeting:write, recording:read)
- **Google Drive/Docs**: OAuth2 with refresh tokens
- **Gemini**: API key, 2.5 Pro for video transcription, 3.1 Pro Preview for Georgian text
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
python -m tools.orchestrator       # or: ./start.sh

# Run tests
pip install -r requirements-dev.txt
pytest tools/tests/test_core.py -v
```

## Deployment (Railway)
- **Platform**: Railway PaaS with Docker (multi-stage build)
- **Entry point**: `python -m tools.orchestrator` (Dockerfile CMD)
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
