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

# Install runtime system deps (ffmpeg for video chunking, curl for healthcheck, gosu for entrypoint)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl gosu && \
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
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create writable directories (volume mount overrides .tmp at runtime)
RUN mkdir -p .tmp logs data && chown -R agent:agent /app

# Railway injects PORT; default to 5001
ENV SERVER_HOST="0.0.0.0" \
    SERVER_PORT=5001 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 5001

# Liveness check — uses /live (no external API calls, < 50ms).
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf --max-time 8 http://localhost:5001/live || exit 1

# Entrypoint fixes volume permissions, then runs as agent user
ENTRYPOINT ["/entrypoint.sh"]
