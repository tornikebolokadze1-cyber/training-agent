# ---- Stage 1: Build dependencies ----
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build-only system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: Runtime ----
FROM python:3.12-slim AS runtime

# Install runtime system deps (ffmpeg for video chunking, curl for healthcheck)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Copy Python venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root user for security
RUN groupadd --gid 1000 agent && \
    useradd --uid 1000 --gid agent --create-home agent

WORKDIR /app

# Copy application code
COPY tools/ ./tools/
COPY workflows/ ./workflows/

# Create writable directories and set ownership
RUN mkdir -p .tmp logs data && chown -R agent:agent /app

USER agent

# Railway injects PORT; default to 5001
ENV SERVER_HOST="0.0.0.0" \
    SERVER_PORT=5001 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 5001

# Health check — generous timeout to survive long Gemini API calls.
# Pipeline runs on a separate thread, but under heavy CPU/memory load
# the event loop can be slow to respond. 30s timeout + 60s start period
# prevents false-positive restarts during video transcription.
HEALTHCHECK --interval=60s --timeout=30s --start-period=60s --retries=3 \
    CMD curl -sf --max-time 25 http://localhost:5001/health || exit 1

# Run the unified orchestrator (APScheduler + FastAPI)
CMD ["python", "-m", "tools.app.orchestrator"]
