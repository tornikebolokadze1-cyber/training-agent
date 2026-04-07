#!/usr/bin/env bash
# Deploy current HEAD to Railway from a clean worktree.
#
# Why this exists:
#   GitHub Actions auto-deploy is broken because Railway's CI deploy requires
#   a token created via the Railway web UI (https://railway.com/account/tokens)
#   which cannot be created programmatically. Until that token is set as the
#   RAILWAY_TOKEN GitHub secret, this script provides an alternative path:
#   it uses the local Railway CLI session (already authenticated via
#   `railway login`) to deploy directly from the developer machine.
#
# Why it uses a worktree:
#   The working tree often contains uncommitted phantom edits that should NOT
#   be deployed. A clean worktree at HEAD ensures only committed code goes to
#   production.
#
# Usage:
#   ./scripts/deploy.sh                    # deploy current HEAD to Railway
#   ./scripts/deploy.sh --push-first       # git push, then deploy
#
# Requires:
#   - railway CLI installed and logged in (`railway login`)
#   - railway link already done for this project

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKTREE_DIR="$(mktemp -d "/tmp/training-agent-deploy.XXXXXX")"
SERVICE_NAME="training-agent"
PROJECT_NAME="training-agent"

cleanup() {
    cd "$REPO_ROOT" 2>/dev/null || true
    if [ -d "$WORKTREE_DIR" ]; then
        git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    fi
}
trap cleanup EXIT

# Optional: push first
if [ "${1:-}" = "--push-first" ]; then
    echo "==> Pushing current branch to origin..."
    cd "$REPO_ROOT"
    git push origin "$(git symbolic-ref --short HEAD)"
    echo ""
fi

# Verify Railway CLI is installed and authenticated
if ! command -v railway >/dev/null 2>&1; then
    echo "ERROR: railway CLI not found. Install: npm install -g @railway/cli"
    exit 1
fi

if ! railway whoami >/dev/null 2>&1; then
    echo "ERROR: railway CLI not authenticated. Run: railway login"
    exit 1
fi

# Create clean worktree at HEAD (committed code only)
cd "$REPO_ROOT"
HEAD_SHA="$(git rev-parse --short HEAD)"
echo "==> Creating clean worktree at HEAD ($HEAD_SHA)..."
git worktree add --detach "$WORKTREE_DIR" HEAD >/dev/null

# Copy .env so Railway CLI can read project config (only if .env exists)
if [ -f "$REPO_ROOT/.env" ]; then
    cp "$REPO_ROOT/.env" "$WORKTREE_DIR/.env"
fi

# Link worktree to Railway project (idempotent)
cd "$WORKTREE_DIR"
echo "==> Linking worktree to Railway project '$PROJECT_NAME'..."
railway link --project "$PROJECT_NAME" >/dev/null 2>&1 || {
    echo "WARN: railway link failed — assuming already linked"
}

# Upload and deploy
echo "==> Uploading to Railway and triggering deploy..."
railway up --service "$SERVICE_NAME" --detach

echo ""
echo "==> Deploy initiated. Check status:"
echo "    railway logs --build"
echo "    curl https://training-agent-production.up.railway.app/health"
