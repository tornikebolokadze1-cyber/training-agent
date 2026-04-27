# 006: Railway PaaS for Deployment

## Date
2026-03-20

## Status
accepted

## Context
The Python server (FastAPI + APScheduler) needs a hosting platform that supports:
- Long-running processes (scheduler must run continuously)
- Webhook endpoints (Zoom, WhatsApp incoming)
- Large file processing (2+ hour video downloads and analysis)
- Docker support
- Affordable for a small training project

Options considered: Railway, Render, Fly.io, AWS EC2, Google Cloud Run.

## Decision
Use **Railway PaaS** with Docker multi-stage build.

Configuration:
- `Dockerfile` — Multi-stage Python build
- `railway.toml` — Railway-specific config
- Entry point: `python -m tools.app.orchestrator`
- Port: Railway injects `$PORT`
- Deploy: `git push` to main → GitHub Actions → Railway CLI

## Reasoning
1. **Railway chosen**: Simple Docker deployments, persistent processes (unlike Cloud Run), reasonable pricing, good DX with `railway up` command. Supports long-running processes needed for APScheduler.
2. **Cloud Run rejected**: Scales to zero — kills the scheduler. Zoom webhooks need always-on endpoint.
3. **EC2 rejected**: Too much infrastructure management for a solo developer project.
4. **Fly.io considered**: Good alternative but Railway's GitHub integration was simpler.
5. **Credential handling**: Railway has no persistent filesystem, so JSON credentials (Google OAuth) are base64-encoded into env vars and decoded at runtime via `_materialize_credential_file()` in config.py.

## Consequences
- **Positive**: Simple deployments, Docker support, persistent processes, reasonable cost
- **Negative**: No persistent filesystem — requires base64 credential pattern. Single instance (no horizontal scaling).
- **Trade-off**: Acceptable for current scale (2 training groups, ~30 lectures total)
