#!/bin/sh
# Fix permissions on Railway volume mount (mounted as root, app runs as agent)
if [ -d /app/.tmp ]; then
    chown -R agent:agent /app/.tmp 2>/dev/null || true
fi
mkdir -p /app/.tmp/dlq /app/logs /app/data 2>/dev/null || true
chown -R agent:agent /app/.tmp /app/logs /app/data 2>/dev/null || true

# Switch to agent user and run the app
exec gosu agent python -m tools.app.orchestrator
