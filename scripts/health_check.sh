#!/bin/sh
# health_check.sh — Training Agent server health monitor
# Polls /health endpoint; sends WhatsApp alert via Green API on failure.
# Designed to run every 5 minutes via launchd.

set -eu

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"
LOG_FILE="$PROJECT_DIR/logs/health_check.log"
HEALTH_URL="http://localhost:5001/health"
CURL_TIMEOUT=10

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# Load required variables from .env
# Only parse lines that match KEY=VALUE; skip comments and blanks.
# ---------------------------------------------------------------------------
load_env() {
    if [ ! -f "$ENV_FILE" ]; then
        log "ERROR: .env file not found at $ENV_FILE"
        exit 1
    fi

    while IFS='=' read -r key value; do
        # Skip comments and blank lines
        case "$key" in
            ''|\#*) continue ;;
        esac
        # Strip inline comments and leading/trailing whitespace from value
        value="$(printf '%s' "$value" | sed 's/#.*//' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')"
        case "$key" in
            GREEN_API_INSTANCE_ID) GREEN_API_INSTANCE_ID="$value" ;;
            GREEN_API_TOKEN)       GREEN_API_TOKEN="$value" ;;
            WHATSAPP_TORNIKE_PHONE) WHATSAPP_TORNIKE_PHONE="$value" ;;
        esac
    done < "$ENV_FILE"

    if [ -z "${GREEN_API_INSTANCE_ID:-}" ] || \
       [ -z "${GREEN_API_TOKEN:-}" ] || \
       [ -z "${WHATSAPP_TORNIKE_PHONE:-}" ]; then
        log "ERROR: One or more Green API variables missing from .env"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Send WhatsApp alert via Green API
# ---------------------------------------------------------------------------
send_alert() {
    message="$1"
    chat_id="${WHATSAPP_TORNIKE_PHONE}@c.us"
    api_url="https://api.green-api.com/waInstance${GREEN_API_INSTANCE_ID}/sendMessage/${GREEN_API_TOKEN}"

    # Build JSON payload without external tools (pure shell + printf)
    payload="$(printf '{"chatId":"%s","message":"%s"}' "$chat_id" "$message")"

    http_code="$(curl -s -o /dev/null -w '%{http_code}' \
        --max-time "$CURL_TIMEOUT" \
        -X POST \
        -H 'Content-Type: application/json' \
        -d "$payload" \
        "$api_url" 2>/dev/null)" || http_code="000"

    if [ "$http_code" = "200" ]; then
        log "ALERT sent via WhatsApp (HTTP $http_code)"
    else
        log "WARNING: WhatsApp alert delivery uncertain (HTTP $http_code)"
    fi
}

# ---------------------------------------------------------------------------
# Main health check logic
# ---------------------------------------------------------------------------
main() {
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"

    # Fetch health endpoint; capture body and HTTP status separately
    response_body="$(curl -s \
        --max-time "$CURL_TIMEOUT" \
        --write-out '\n__HTTP_STATUS__%{http_code}' \
        "$HEALTH_URL" 2>/dev/null)" || response_body=""

    http_status="$(printf '%s' "$response_body" | grep '__HTTP_STATUS__' | sed 's/__HTTP_STATUS__//')"
    body="$(printf '%s' "$response_body" | grep -v '__HTTP_STATUS__')"

    # Determine health state
    # Success requires: curl succeeded, HTTP 200, and body contains "healthy"
    if [ -z "$http_status" ]; then
        # curl failed entirely (connection refused, timeout, etc.)
        failure_reason="curl failed — server unreachable or timed out"
    elif [ "$http_status" != "200" ]; then
        failure_reason="unexpected HTTP status $http_status"
    else
        # Check response body for "healthy" status
        case "$body" in
            *'"status":"healthy"'*|*'"status": "healthy"'*)
                # All good — exit silently
                return 0
                ;;
            *)
                failure_reason="response does not indicate healthy status (body: $body)"
                ;;
        esac
    fi

    # Something is wrong — log and alert
    log "FAILURE: $failure_reason"

    load_env

    alert_message="Training Agent health check FAILED at ${timestamp}. Server may be down."
    send_alert "$alert_message"
}

main "$@"
