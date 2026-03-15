#!/usr/bin/env zsh
# =============================================================================
# Training Agent — startup script
#
# Usage:
#   ./start.sh            Start the orchestrator in the foreground
#   ./start.sh --check    Validate environment only, do not start
#   ./start.sh --help     Show this help message
#
# The script:
#   1. Resolves its own directory so it works from any cwd
#   2. Activates the .venv virtual environment
#   3. Validates all required .env variables
#   4. Tails logs to a rotating file (max 10 MB, keeps 3 archives)
#   5. Starts python -m tools.orchestrator with proper signal forwarding
#   6. On SIGTERM/SIGINT: gracefully shuts the process down
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve project root (the directory containing this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
ENV_FILE="$PROJECT_ROOT/.env"
LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/training-agent.log"
LOG_MAX_BYTES=10485760   # 10 MB
LOG_KEEP=3               # number of rotated archives to keep

# ---------------------------------------------------------------------------
# Colour helpers (disabled if not a TTY)
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    RED='\033[0;31m'; YELLOW='\033[0;33m'; GREEN='\033[0;32m'; NC='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; NC=''
fi

info()  { printf "%b[INFO]%b  %s\n"  "$GREEN"  "$NC" "$*"; }
warn()  { printf "%b[WARN]%b  %s\n"  "$YELLOW" "$NC" "$*"; }
error() { printf "%b[ERROR]%b %s\n"  "$RED"    "$NC" "$*" >&2; }
die()   { error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--help" ]]; then
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

# ---------------------------------------------------------------------------
# Virtual environment check
# ---------------------------------------------------------------------------
[[ -d "$VENV_DIR" ]] || die "Virtual environment not found at $VENV_DIR — run: python -m venv .venv && .venv/bin/pip install -r requirements.txt"
[[ -x "$VENV_PYTHON" ]] || die "Python binary not executable: $VENV_PYTHON"

info "Activating virtual environment: $VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ---------------------------------------------------------------------------
# .env presence check
# ---------------------------------------------------------------------------
[[ -f "$ENV_FILE" ]] || die ".env file not found at $ENV_FILE"

# ---------------------------------------------------------------------------
# Load .env into the shell for validation purposes
# ---------------------------------------------------------------------------
# We export only non-blank, non-comment lines without overwriting existing vars
while IFS='=' read -r key value; do
    # Skip blank lines and comments
    [[ -z "$key" || "$key" == \#* ]] && continue
    # Strip surrounding quotes from value if present
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    # Only export if not already set in the environment
    if [[ -z "${(P)key:-}" ]]; then
        export "$key=$value"
    fi
done < <(grep -v '^\s*#' "$ENV_FILE" | grep '=')

# ---------------------------------------------------------------------------
# Required variable validation
# ---------------------------------------------------------------------------
REQUIRED_VARS=(
    ZOOM_ACCOUNT_ID
    ZOOM_CLIENT_ID
    ZOOM_CLIENT_SECRET
    GEMINI_API_KEY
    GREEN_API_INSTANCE_ID
    GREEN_API_TOKEN
    WEBHOOK_SECRET
)

OPTIONAL_VARS=(
    N8N_CALLBACK_URL
    GOOGLE_CREDENTIALS_PATH
    SERVER_PUBLIC_URL
)

missing_required=()
for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${(P)var:-}" ]]; then
        missing_required+=("$var")
    fi
done

for var in "${OPTIONAL_VARS[@]}"; do
    if [[ -z "${(P)var:-}" ]]; then
        warn "Optional variable not set: $var — some features may be disabled"
    fi
done

if (( ${#missing_required[@]} > 0 )); then
    error "Cannot start — the following required variables are missing from .env:"
    for var in "${missing_required[@]}"; do
        error "  - $var"
    done
    exit 1
fi

info "All required environment variables are present."

# Stop here if --check was passed
if [[ "${1:-}" == "--check" ]]; then
    info "Environment check passed. Not starting server (--check mode)."
    exit 0
fi

# ---------------------------------------------------------------------------
# Log directory and rotation
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"

rotate_log() {
    local log="$1"
    [[ -f "$log" ]] || return 0

    local size
    size=$(stat -f%z "$log" 2>/dev/null || echo 0)
    if (( size < LOG_MAX_BYTES )); then
        return 0
    fi

    info "Rotating log file ($((size / 1024 / 1024)) MB)..."
    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    mv "$log" "${log}.${ts}"

    # Prune old archives beyond LOG_KEEP
    local count=0
    for archive in "${log}."*(N); do
        (( count++ ))
    done
    if (( count > LOG_KEEP )); then
        local excess=$(( count - LOG_KEEP ))
        for archive in "${log}."*(N[1,$excess]); do
            rm -f "$archive"
            info "Pruned old log archive: $archive"
        done
    fi
}

rotate_log "$LOG_FILE"

# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------
PID_FILE="$PROJECT_ROOT/.training-agent.pid"

check_already_running() {
    if [[ -f "$PID_FILE" ]]; then
        local old_pid
        old_pid=$(< "$PID_FILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            die "Training Agent is already running (PID $old_pid). Stop it first or remove $PID_FILE."
        else
            warn "Stale PID file found (PID $old_pid no longer running). Removing."
            rm -f "$PID_FILE"
        fi
    fi
}

check_already_running

# ---------------------------------------------------------------------------
# Graceful shutdown trap
# ---------------------------------------------------------------------------
SERVER_PID=""

cleanup() {
    local sig="${1:-TERM}"
    info "Received signal — initiating graceful shutdown..."
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        info "Sending SIGTERM to orchestrator (PID $SERVER_PID)..."
        kill -TERM "$SERVER_PID" 2>/dev/null || true
        # Wait up to 15 seconds for clean exit
        local waited=0
        while kill -0 "$SERVER_PID" 2>/dev/null && (( waited < 15 )); do
            sleep 1
            (( waited++ ))
        done
        if kill -0 "$SERVER_PID" 2>/dev/null; then
            warn "Orchestrator did not exit cleanly — sending SIGKILL."
            kill -KILL "$SERVER_PID" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
    info "Training Agent stopped."
    exit 0
}

trap 'cleanup TERM' TERM
trap 'cleanup INT'  INT

# ---------------------------------------------------------------------------
# Launch orchestrator
# ---------------------------------------------------------------------------
info "Starting Training Agent orchestrator..."
info "Logs: $LOG_FILE"
info "PID file: $PID_FILE"

cd "$PROJECT_ROOT"

# Tee stdout+stderr to the log file while keeping console output
"$VENV_PYTHON" -m tools.orchestrator 2>&1 | tee -a "$LOG_FILE" &
SERVER_PID=$!

echo "$SERVER_PID" > "$PID_FILE"
info "Orchestrator started (PID $SERVER_PID)."

# Wait for the background process; 'wait' is interruptible by our traps
wait "$SERVER_PID"
EXIT_CODE=$?
rm -f "$PID_FILE"

if (( EXIT_CODE != 0 )); then
    error "Orchestrator exited with code $EXIT_CODE. Check $LOG_FILE for details."
    exit "$EXIT_CODE"
fi

info "Orchestrator exited cleanly."
