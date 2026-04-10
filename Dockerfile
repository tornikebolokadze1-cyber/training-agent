# ---- Stage 1: Build dependencies ----
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build-only system deps (git needed for commit hash extraction)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev git && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---- Stage 1.5: Capture git metadata (before stage 2) ----
# Copy git directory to extract commit hash
COPY .git .git/
RUN git rev-parse --short=8 HEAD > /build/GIT_COMMIT && \
    cat /build/GIT_COMMIT

# ---- Stage 2: Runtime ----
FROM python:3.12-slim AS runtime

# Install runtime system deps (ffmpeg for video chunking, curl for healthcheck)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Copy Python venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy git commit hash from builder
COPY --from=builder /build/GIT_COMMIT /etc/app/git_commit

# Non-root user for security
RUN groupadd --gid 1000 agent && \
    useradd --uid 1000 --gid agent --create-home agent

WORKDIR /app

# Copy application code
COPY tools/ ./tools/
COPY workflows/ ./workflows/

# Create writable directories and set ownership
RUN mkdir -p .tmp logs data && chown -R agent:agent /app && \
    chown -R agent:agent /etc/app 2>/dev/null || true

USER agent

# Railway injects PORT; default to 5001
ENV SERVER_HOST="0.0.0.0" \
    SERVER_PORT=5001 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 5001

# Liveness check — uses /live (no external API calls, < 50ms).
# /health runs a full dependency audit with billable Gemini/Claude calls;
# using it here caused false-positive restarts and unnecessary API spend.
# /live only checks that the process is alive and the event loop responds.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf --max-time 8 http://localhost:5001/live || exit 1

# Run the unified orchestrator (APScheduler + FastAPI)
CMD ["python", "-m", "tools.app.orchestrator"]
