#!/usr/bin/env bash
# deploy_tracker.sh — one-command deploy for the WW P2P bootstrap tracker to Fly.io
set -euo pipefail

# --- colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
bold()  { echo -e "${BOLD}$*${NC}"; }

APP_NAME="${WW_TRACKER_APP_NAME:-ww-bootstrap-tracker}"
REGION="${WW_TRACKER_REGION:-iad}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
P2P_DIR="$PROJECT_DIR/p2p"

echo ""
bold "  WW Bootstrap Tracker — Deploy to Fly.io"
echo ""

# --- detect flyctl ---
AUTO_DEPLOY=false
if command -v flyctl &>/dev/null; then
    if flyctl auth whoami &>/dev/null 2>&1; then
        AUTO_DEPLOY=true
        info "flyctl detected and authenticated — auto-deploy mode"
    else
        warn "flyctl found but not logged in (run: flyctl auth login)"
    fi
else
    info "flyctl not found — showing manual instructions"
fi

# ═══════════════════════════════════════════════════════════
# Auto-deploy path
# ═══════════════════════════════════════════════════════════
if $AUTO_DEPLOY; then
    info "Generating fly.toml for app: ${APP_NAME}"

    cat > "$P2P_DIR/fly.toml" << TOML
app = "${APP_NAME}"
primary_region = "${REGION}"

[build]
  image = "python:3.11-slim"

[env]
  PORT = "8080"

[[services]]
  internal_port = 8080
  protocol = "tcp"
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

  [[services.ports]]
    handlers = ["http"]
    port = 80

  [[services.ports]]
    handlers = ["tls", "http"]
    port = 443
TOML
    ok "Wrote $P2P_DIR/fly.toml"

    cat > "$P2P_DIR/Procfile" << 'PROCFILE'
web: python3 bootstrap_tracker.py
PROCFILE
    ok "Wrote $P2P_DIR/Procfile"

    info "Deploying to Fly.io (region: ${REGION})..."
    cd "$P2P_DIR"
    flyctl deploy --ha=false --region "$REGION"

    echo ""
    ok "Deploy complete!"
    echo ""
    bold "  Public URL:"
    echo "    https://${APP_NAME}.fly.dev"
    echo ""
    bold "  Bootstrap URL export:"
    echo "    export WW_BOOTSTRAP_URLS=https://${APP_NAME}.fly.dev"
    echo ""
    info "Verify with: curl https://${APP_NAME}.fly.dev/health"
    echo ""
    exit 0
fi

# ═══════════════════════════════════════════════════════════
# Manual instructions
# ═══════════════════════════════════════════════════════════
echo ""
bold "  Manual Deploy Instructions — Fly.io Free Tier"
echo ""
echo "  Step 1 — Install flyctl"
echo "    curl -L https://fly.io/install.sh | sh"
echo "    # or: brew install flyctl (macOS)"
echo ""
echo "  Step 2 — Sign up / login"
echo "    flyctl auth signup"
echo "    # or: flyctl auth login"
echo ""
echo "  Step 3 — Create these two files in the p2p/ directory:"
echo ""
bold "    p2p/Procfile:"
echo "      web: python3 bootstrap_tracker.py"
echo ""
bold "    p2p/fly.toml:"
echo "      app = \"${APP_NAME}\""
echo "      primary_region = \"${REGION}\""
echo ""
echo "      [build]"
echo "        image = \"python:3.11-slim\""
echo ""
echo "      [env]"
echo "        PORT = \"8080\""
echo ""
echo "      [[services]]"
echo "        internal_port = 8080"
echo "        protocol = \"tcp\""
echo "        auto_stop_machines = \"stop\""
echo "        auto_start_machines = true"
echo "        min_machines_running = 0"
echo ""
echo "        [[services.ports]]"
echo "          handlers = [\"http\"]"
echo "          port = 80"
echo ""
echo "        [[services.ports]]"
echo "          handlers = [\"tls\", \"http\"]"
echo "          port = 443"
echo ""
echo "  Step 4 — Deploy"
echo "    cd p2p"
echo "    flyctl deploy --ha=false --region ${REGION}"
echo ""
echo "  Step 5 — After deploy completes, export the bootstrap URL:"
echo ""
bold "    export WW_BOOTSTRAP_URLS=https://${APP_NAME}.fly.dev"
echo ""
echo "  Verify: curl https://${APP_NAME}.fly.dev/health"
echo ""
info "Free tier includes up to 3 shared-cpu-1x VMs (256 MB each)."
info "App auto-stops when idle; wakes on first request."
echo ""

