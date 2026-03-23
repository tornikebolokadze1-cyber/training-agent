# Training Agent Architecture

## Overview

Training Agent is a hybrid system that automates AI training session management
for Zoom-based Georgian language lectures. It combines n8n workflow orchestration
with Python execution to handle the full lifecycle: scheduling, recording capture,
AI-powered analysis, and multi-channel delivery.

**Architecture style**: n8n orchestration + Python execution
- **n8n** handles scheduling, triggers, and notifications (email + WhatsApp)
- **Python** handles heavy processing: file downloads, AI analysis, uploads

## System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        EXTERNAL SERVICES                        │
├──────────┬──────────┬───────────┬──────────┬───────────────────┤
│   Zoom   │  Google  │  Gemini   │  Claude  │    WhatsApp       │
│  S2S API │Drive API │   API     │   API    │  (Green API)      │
│          │          │           │          │                   │
│ meetings │ folders  │ transcr.  │ analysis │ group messages    │
│recording │ uploads  │ Georgian  │ reasoning│ private reports   │
│ download │ Docs API │ writing   │ thinking │ assistant bot     │
└────┬─────┴────┬─────┴─────┬─────┴────┬─────┴─────┬─────────────┘
     │          │           │          │           │
┌────▼──────────▼───────────▼──────────▼───────────▼─────────────┐
│                     PYTHON APPLICATION                          │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  tools/app/orchestrator.py  (single entry point)        │   │
│  │  ├── FastAPI webhook server (app/server.py)             │   │
│  │  └── APScheduler cron jobs (app/scheduler.py)           │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │   Services    │  │ Integrations │  │       Core           │ │
│  │              │  │              │  │                      │ │
│  │ transcribe   │  │ gdrive       │  │ config.py            │ │
│  │ _lecture.py  │  │ _manager.py  │  │  - group schedules   │ │
│  │              │  │              │  │  - folder IDs         │ │
│  │ whatsapp     │  │ gemini       │  │  - Gemini prompts    │ │
│  │ _assistant.py│  │ _analyzer.py │  │  - Claude prompts    │ │
│  │              │  │              │  │                      │ │
│  │ analytics.py │  │ zoom         │  │                      │ │
│  │              │  │ _manager.py  │  │                      │ │
│  │              │  │              │  │                      │ │
│  │              │  │ whatsapp     │  │                      │ │
│  │              │  │ _sender.py   │  │                      │ │
│  │              │  │              │  │                      │ │
│  │              │  │ knowledge    │  │                      │ │
│  │              │  │ _indexer.py  │  │                      │ │
│  └──────────────┘  └──────────────┘  └──────────────────────┘ │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Pinecone Vector DB                                     │   │
│  │  - gemini-embedding-001 (3072 dimensions)               │   │
│  │  - Course knowledge RAG for WhatsApp assistant          │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
     │
     │  webhooks + callbacks
     ▼
┌─────────────────────────────────────────────────────────────────┐
│                    n8n CLOUD WORKFLOWS                           │
│  aipulsegeorgia2025.app.n8n.cloud                               │
│                                                                 │
│  1. Pre-Meeting Reminders                                       │
│     18:00 trigger → email + WhatsApp with Zoom link             │
│                                                                 │
│  2. Zoom Recording → Python Handoff                             │
│     Zoom webhook + CRC validation → POST to Python server       │
│                                                                 │
│  3. Post-Processing Delivery                                    │
│     Python callback → email notification with Drive links       │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow: Lecture Processing Pipeline

```
Zoom Meeting Ends
       │
       ▼
Zoom sends recording.completed webhook
       │
       ▼
Python server receives webhook (HMAC-SHA256 verified)
       │
       ▼
Zoom Manager downloads recording (resumable for large files)
       │
       ▼
Gemini 2.5 Pro transcribes 2-hour video (1M token context)
       │
       ▼
Claude Opus 4.6 analyzes content (extended thinking)
  ├── Gap analysis: what students struggle with
  ├── Deep analysis: pedagogical insights
  └── Lecture summary: key takeaways
       │
       ▼
Gemini 3.1 Pro Preview writes Georgian text
  ├── Summary document (shared with group)
  └── Analysis report (private to instructor)
       │
       ▼
Google Drive Manager uploads everything
  ├── Video → ლექცია N folder (group shared)
  ├── Summary → ლექცია N folder (group shared)
  └── Analysis → კურსი #N ანალიზი (private)
       │
       ▼
Knowledge Indexer chunks + embeds into Pinecone
       │
       ▼
WhatsApp Sender delivers notifications
  ├── Group chat: "ლექცია N მზადაა" + Drive link
  └── Private: analysis report link to instructor
```

## Two Training Groups

| Property | Group #1 | Group #2 |
|----------|----------|----------|
| Schedule | Tuesday, Friday | Monday, Thursday |
| Time | 20:00–22:00 GMT+4 | 20:00–22:00 GMT+4 |
| Drive folder | AI კურსი (მარტის ჯგუფი #1. 2026) | AI კურსი (მარტის ჯგუფი #2. 2026) |
| Total lectures | 15 | 15 |

Each group gets its own set of lecture folders, analysis folders,
and WhatsApp group chat.

## WhatsApp Assistant ("მრჩეველი")

An AI-powered assistant that students can message for course-related help.

```
Student sends question via WhatsApp
       │
       ▼
Green API delivers message to Python server
       │
       ▼
Pinecone RAG retrieves relevant lecture content
       │
       ▼
Claude Opus reasons about the answer (extended thinking)
       │
       ▼
Gemini writes the response in Georgian
       │
       ▼
WhatsApp sends response back to student
```

The assistant always web-searches for current information, responds without
requiring a trigger word, and understands reply context from conversations.

## Key Directories

```
Training Agent/
├── tools/
│   ├── core/
│   │   └── config.py          # Groups, schedules, folder IDs, prompts
│   ├── integrations/
│   │   ├── gdrive_manager.py  # Google Drive operations
│   │   ├── gemini_analyzer.py # Multi-model AI pipeline
│   │   ├── zoom_manager.py    # Zoom S2S OAuth + recordings
│   │   ├── whatsapp_sender.py # WhatsApp messaging
│   │   └── knowledge_indexer.py # Pinecone RAG
│   ├── services/
│   │   ├── transcribe_lecture.py  # Main analysis pipeline
│   │   ├── whatsapp_assistant.py  # AI assistant
│   │   └── analytics.py          # Course reporting
│   ├── app/
│   │   ├── orchestrator.py    # Entry point (scheduler + server)
│   │   ├── server.py          # FastAPI webhook server
│   │   ├── scheduler.py       # APScheduler cron jobs
│   │   └── process_recording.py # CLI for manual processing
│   └── tests/                 # Test suite
├── workflows/                 # Markdown SOPs (WAT framework)
├── docs/                      # Architecture docs and decisions
├── logs/                      # Rotating log files (local only)
└── .tmp/                      # Temporary processing files
```

## API Integrations

| Service | Auth Method | Purpose |
|---------|-------------|---------|
| Zoom | Server-to-Server OAuth | Meeting creation, recording download |
| Google Drive | OAuth2 + refresh tokens | Folder management, file upload |
| Google Docs | OAuth2 + refresh tokens | Document creation for summaries |
| Gemini | API key | Video transcription (2.5 Pro), Georgian writing (3.1 Pro Preview) |
| Claude/Anthropic | API key | Analysis reasoning (Opus 4.6, extended thinking) |
| Pinecone | API key | Vector DB for course knowledge RAG |
| WhatsApp | Green API REST | Group + private messaging |
| Gmail | OAuth2 | Backup notifications |

## Deployment

### Production: Railway

- **Platform**: Railway PaaS with Docker (multi-stage build)
- **Entry point**: `python -m tools.app.orchestrator`
- **Credentials**: base64-encoded JSON files in environment variables
- **Deploy trigger**: `git push` to `main` → GitHub Actions → Railway CLI

### Local Development

- **Service**: `com.aipulsegeorgia.training-agent` (launchd, auto-restarts)
- **Port**: 5001 (Railway injects `$PORT`)
- **Health check**: `GET /health`
- **Full status**: `GET /status`
- **API docs**: `/docs` (local only, disabled on Railway)
- **Logs**: `logs/training_agent.log` (rotating, 10 MB x 5 files)

## Reliability

- **Stale task recovery**: tasks running longer than 4 hours are auto-evicted
  from the deduplication tracker
- **Operator alerts**: `alert_operator()` sends last-resort WhatsApp notifications
  when critical failures occur
- **Resumable uploads**: files over 10MB use resumable upload protocol to Google Drive
- **Retry with backoff**: all API calls use exponential backoff for transient failures
