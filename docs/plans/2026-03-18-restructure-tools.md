# Restructure tools/ into Subpackages

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reorganize the flat `tools/` directory into logical subpackages (`core/`, `integrations/`, `services/`, `app/`) for better separation of concerns.

**Architecture:** Move files into 4 subpackages by responsibility layer. Update all `tools.X` imports to `tools.<layer>.X`. No backward-compatibility shims — clean cut.

**Tech Stack:** Python 3.12, pytest, GitHub Actions CI, Docker

---

## File Mapping

| Old Path | New Path | Layer |
|----------|----------|-------|
| `tools/config.py` | `tools/core/config.py` | core |
| `tools/prompts.py` | `tools/core/prompts.py` | core |
| `tools/logging_config.py` | `tools/core/logging_config.py` | core |
| `tools/retry.py` | `tools/core/retry.py` | core |
| `tools/railway_setup.py` | `tools/core/railway_setup.py` | core |
| `tools/gdrive_manager.py` | `tools/integrations/gdrive_manager.py` | integrations |
| `tools/zoom_manager.py` | `tools/integrations/zoom_manager.py` | integrations |
| `tools/whatsapp_sender.py` | `tools/integrations/whatsapp_sender.py` | integrations |
| `tools/gemini_analyzer.py` | `tools/integrations/gemini_analyzer.py` | integrations |
| `tools/knowledge_indexer.py` | `tools/integrations/knowledge_indexer.py` | integrations |
| `tools/transcribe_lecture.py` | `tools/services/transcribe_lecture.py` | services |
| `tools/whatsapp_assistant.py` | `tools/services/whatsapp_assistant.py` | services |
| `tools/analytics.py` | `tools/services/analytics.py` | services |
| `tools/server.py` | `tools/app/server.py` | app |
| `tools/orchestrator.py` | `tools/app/orchestrator.py` | app |
| `tools/scheduler.py` | `tools/app/scheduler.py` | app |
| `tools/process_recording.py` | `tools/app/process_recording.py` | app |

## Import Mapping

All `from tools.X import Y` and `import tools.X` must change:

```
tools.config            → tools.core.config
tools.prompts           → tools.core.prompts
tools.logging_config    → tools.core.logging_config
tools.retry             → tools.core.retry
tools.railway_setup     → tools.core.railway_setup
tools.gdrive_manager    → tools.integrations.gdrive_manager
tools.zoom_manager      → tools.integrations.zoom_manager
tools.whatsapp_sender   → tools.integrations.whatsapp_sender
tools.gemini_analyzer   → tools.integrations.gemini_analyzer
tools.knowledge_indexer → tools.integrations.knowledge_indexer
tools.transcribe_lecture → tools.services.transcribe_lecture
tools.whatsapp_assistant → tools.services.whatsapp_assistant
tools.analytics         → tools.services.analytics
tools.server            → tools.app.server
tools.orchestrator      → tools.app.orchestrator
tools.scheduler         → tools.app.scheduler
tools.process_recording → tools.app.process_recording
```

## Infrastructure Updates

### Dockerfile
- `CMD ["python", "-m", "tools.app.orchestrator"]`

### start.sh
- `"$VENV_PYTHON" -m tools.app.orchestrator`

### CI (.github/workflows/ci.yml)
- Syntax check: iterate `tools/**/*.py` recursively
- Lint: `ruff check tools/`
- Coverage: `--cov=tools`
- Docker verify: `import tools.app.server`

### conftest.py (tools/tests/)
- Stub `tools.services.whatsapp_assistant` instead of `tools.whatsapp_assistant`

### CLAUDE.md
- Update all tool path references
