# Training Agent

Automated AI training session management for Zoom-based lectures. Handles the full lifecycle: **pre-meeting reminders** → **cloud recording** → **AI transcription & analysis** → **Google Drive delivery** → **WhatsApp notifications** → **RAG knowledge base**.

Built for Georgian-language AI training courses, but adaptable to any recurring Zoom-based teaching program.

## What It Does

```
19:00  Scheduler creates Zoom meeting + sends WhatsApp reminder with join link
20:00  Lecture begins (Zoom records to cloud automatically)
22:00  Lecture ends → system polls for recording
22:15  Downloads recording → uploads to Google Drive
22:20  Gemini 2.5 Pro transcribes 2-hour video (multimodal, 45-min chunks)
22:35  Claude Opus analyzes transcript (extended thinking for deep reasoning)
22:40  Gemini 3.1 Pro writes Georgian summary + gap analysis + deep analysis
22:42  Summary uploaded to shared Google Drive folder
22:43  Private analysis uploaded to owner-only Drive folder
22:44  WhatsApp: group gets video + summary links; instructor gets analysis link
22:45  All content indexed into Pinecone for RAG-powered Q&A assistant
```

## Architecture

**Hybrid design**: n8n orchestration + Python heavy lifting.

```
┌─────────────────────────────────────────────────────────┐
│                    APScheduler (cron)                     │
│  Pre-meeting: create Zoom + WhatsApp + email reminders   │
│  Post-meeting: poll recording + trigger analysis pipeline │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                  FastAPI Server (:5001)                   │
│  /zoom-webhook  — Zoom recording.completed events        │
│  /whatsapp      — Green API incoming messages            │
│  /health        — Health check + system status            │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│              Analysis Pipeline                           │
│                                                          │
│  Video ──→ Gemini 2.5 Pro (transcription, 45-min chunks) │
│        ──→ Claude Opus 4 (reasoning + analysis)          │
│        ──→ Gemini 3.1 Pro (Georgian text generation)     │
│                                                          │
│  Output ──→ Google Drive (shared summary + private report)│
│         ──→ WhatsApp (group notification + private link)  │
│         ──→ Pinecone (RAG vectors for Q&A assistant)      │
└─────────────────────────────────────────────────────────┘
```

### Multi-Model AI Pipeline

| Stage | Model | Purpose |
|-------|-------|---------|
| Transcription | Gemini 2.5 Pro | Multimodal video transcription (1M token context) |
| Reasoning | Claude Opus 4 | Extended thinking for gap analysis + deep analysis |
| Writing | Gemini 3.1 Pro Preview | Georgian language text generation (summary, reports) |
| Embeddings | Gemini Embedding 001 | 3072-dim vectors for Pinecone RAG index |
| Q&A Assistant | Claude + Gemini | Claude reasons, Gemini writes Georgian responses |

### WhatsApp AI Assistant

A built-in AI assistant ("მრჩეველი") that answers course-related questions in WhatsApp groups:
- Triggered by name mention or direct message
- Retrieves relevant lecture content from Pinecone (RAG)
- Claude reasons about the answer, Gemini writes in Georgian
- Maintains per-chat conversation history and cooldown periods

## Project Structure

```
Training Agent/
├── tools/
│   ├── config.py              # Groups, schedules, folder IDs, model config
│   ├── orchestrator.py        # Entry point: APScheduler + FastAPI on single loop
│   ├── scheduler.py           # Cron jobs: pre/post meeting automation
│   ├── server.py              # FastAPI webhooks (Zoom, WhatsApp, health)
│   ├── gemini_analyzer.py     # Multi-model analysis pipeline
│   ├── transcribe_lecture.py  # Full pipeline: transcribe → analyze → deliver
│   ├── gdrive_manager.py      # Google Drive: upload, folders, Docs
│   ├── zoom_manager.py        # Zoom S2S OAuth: meetings, recordings
│   ├── whatsapp_sender.py     # WhatsApp via Green API
│   ├── whatsapp_assistant.py  # AI Q&A assistant for WhatsApp groups
│   ├── knowledge_indexer.py   # Pinecone: chunk, embed, upsert, query
│   ├── prompts.py             # Georgian-language prompt templates
│   ├── logging_config.py      # Structured JSON logging (production)
│   ├── retry.py               # Shared retry utility
│   ├── process_recording.py   # CLI: manual recording processing
│   └── tests/                 # 523 tests, 96% coverage
├── workflows/                 # Markdown SOPs
├── .env.example               # Environment variable template
├── attendees.json.example     # Attendee list template
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Multi-stage production build
├── railway.toml               # Railway PaaS config
└── CLAUDE.md                  # AI agent instructions
```

## Setup

### Prerequisites

- Python 3.12+
- ffmpeg (for recording segment concatenation)
- API accounts: Zoom, Google Cloud, Gemini, Anthropic, Pinecone, Green API

### 1. Clone and install

```bash
git clone https://github.com/tornikebolokadze/training-agent.git
cd training-agent
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys and configuration
```

See [.env.example](.env.example) for all required variables with descriptions.

### 3. Set up Google OAuth

```bash
# Place your Google Cloud OAuth credentials file
# Enable Drive API + Docs API in Google Cloud Console
cp /path/to/your/credentials.json ./credentials.json

# Run initial authorization (opens browser)
python -m tools.gdrive_manager
```

### 4. Configure attendees

```bash
cp attendees.json.example attendees.json
# Edit with your group member email addresses
# Keys are group numbers ("1", "2"), values are email arrays
```

### 5. Create lecture folders

```bash
# Creates ლექცია #1 through #15 in each group's Drive folder
python -m tools.gdrive_manager
```

### 6. Run

```bash
# Start the full system (scheduler + webhook server)
python -m tools.orchestrator

# Or manually process a recording
python -m tools.transcribe_lecture <group> <lecture> <video_path>
```

### Health check

```bash
curl http://localhost:5001/health
curl http://localhost:5001/status  # Detailed system status
```

## Deployment (Railway)

The system is designed to run on [Railway](https://railway.app):

```bash
# Base64-encode credential files for Railway env vars
base64 -i credentials.json | tr -d '\n'  # → GOOGLE_CREDENTIALS_JSON_B64
base64 -i token.json | tr -d '\n'        # → GOOGLE_TOKEN_JSON_B64
base64 -i attendees.json | tr -d '\n'    # → ATTENDEES_JSON_B64
```

Set all `.env` variables in Railway dashboard. The Dockerfile handles the rest.

See [RAILWAY_DEPLOYMENT.md](RAILWAY_DEPLOYMENT.md) for detailed instructions.

## Configuration

### Training Groups

Edit `tools/config.py` to define your groups:

```python
GROUPS = {
    1: {
        "name": "Group Name",
        "meeting_days": [1, 4],      # Tuesday=1, Friday=4 (Monday=0)
        "start_date": date(2026, 3, 13),
        "drive_folder_id": "...",     # From .env
        "analysis_folder_id": "...",  # Private analysis folder
        "attendee_emails": [...],     # From attendees.json
    },
}
```

### Customizing Prompts

All AI prompts are in `tools/prompts.py`. They're written in Georgian but can be adapted to any language. The prompts define:
- **Transcription**: How to transcribe lecture video with timestamps
- **Summarization**: Structure for lecture summaries
- **Gap Analysis**: Framework for identifying teaching gaps
- **Deep Analysis**: Detailed pedagogical analysis template

## Testing

```bash
pip install -r requirements-dev.txt

# Run all tests
pytest tools/tests/ -v

# With coverage
pytest tools/tests/ --cov=tools --cov-report=term-missing
```

## API Integrations

| Service | Auth Method | Purpose |
|---------|------------|---------|
| Zoom | Server-to-Server OAuth | Meeting creation, cloud recording download |
| Google Drive/Docs | OAuth2 (refresh token) | File upload, document creation |
| Gemini | API key | Video transcription, text generation, embeddings |
| Claude/Anthropic | API key | Reasoning, analysis (extended thinking) |
| Pinecone | API key | Vector storage for RAG knowledge base |
| Green API | Instance ID + token | WhatsApp messaging (group + private) |

## Resilience Features

- **Recording segment handling**: If the host disconnects and rejoins, multiple recording segments are automatically downloaded and concatenated with ffmpeg
- **Transcript caching**: Transcripts are saved to `.tmp/` — if the pipeline crashes mid-analysis, it resumes from the cached transcript instead of re-transcribing
- **Retry with backoff**: All API calls use exponential backoff with configurable retries
- **Operator alerts**: Critical failures trigger WhatsApp alerts to the instructor
- **Stale task recovery**: Tasks running >4 hours are auto-evicted from the deduplication tracker
- **Dual Gemini tiers**: If paid API quota is exhausted, automatically falls back to free tier

## License

[MIT](LICENSE)
