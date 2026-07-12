#!/usr/bin/env bash
# ============================================================
#  Worldwave P2P Node — Universal Deploy Script
# ============================================================
#  Every node is a tracker. Join the decentralized network.
#
#  One-liner:
#    bash <(curl -fsSL https://raw.githubusercontent.com/Clean-Dust/worldwave/main/deploy.sh)
#
#  Or local:
#    bash deploy.sh
#
#  Supports: Linux (Ubuntu/Debian), macOS, WSL2
#  Requires: Python 3.10+, git
# ============================================================
set -euo pipefail

# ── Config (override via env vars) ──
REPO="${WW_REPO:-https://github.com/Clean-Dust/worldwave.git}"
BRANCH="${WW_BRANCH:-main}"
INSTALL_DIR="${WW_HOME:-$HOME/worldwave}"
PYTHON="${WW_PYTHON:-python3}"
# Bootstrap trackers — comma-separated. At least one must be reachable.
BOOTSTRAP_URLS="${WW_BOOTSTRAP_URLS:-http://tracker.dse-5-star-star.org}"
# P2P port — must match what server.py uses (default 9833)
P2P_PORT="${WW_P2P_PORT:-9833}"
# DHT bootstrap seeds (optional, for fully decentralized discovery)
DHT_SEEDS="${WW_DHT_BOOTSTRAP_NODES:-}"
# Server port for WW API
WW_PORT="${WW_PORT:-9300}"

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${BLUE}  ➤${NC} $1"; }
ok()    { echo -e "${GREEN}  ✓${NC} $1"; }
warn()  { echo -e "${YELLOW}  ⚠${NC} $1"; }
err()   { echo -e "${RED}  ✗${NC} $1"; }
step()  { echo -e "\n${BOLD}${CYAN}══ $1 ══${NC}\n"; }

# ── Banner ──
cat << 'BANNER'

   ██╗    ██╗ ██████╗ ██████╗ ██╗     ██████╗ ██╗    ██╗ █████╗ ██╗   ██╗███████╗
   ██║    ██║██╔═══██╗██╔══██╗██║     ██╔══██╗██║    ██║██╔══██╗██║   ██║██╔════╝
   ██║ █╗ ██║██║   ██║██████╔╝██║     ██║  ██║██║ █╗ ██║███████║██║   ██║█████╗  
   ██║███╗██║██║   ██║██╔══██╗██║     ██║  ██║██║███╗██║██╔══██║╚██╗ ██╔╝██╔══╝  
   ╚███╔███╔╝╚██████╔╝██║  ██║███████╗██████╔╝╚███╔███╔╝██║  ██║ ╚████╔╝ ███████╗
    ╚══╝╚══╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═╝  ╚═══╝  ╚══════╝
                                                                                   
                      Decentralized P2P Node — Every Node is a Tracker
BANNER

# ═══════════════════════════════════════════════════════════
#  1. Pre-flight Checks
# ═══════════════════════════════════════════════════════════
step "1/6  Pre-flight Checks"

# Python
if ! command -v "$PYTHON" &>/dev/null; then
    err "Python 3 not found. Install Python 3.10+ and retry."
    echo "    Ubuntu: sudo apt install python3 python3-pip python3-venv"
    echo "    macOS:  brew install python3"
    exit 1
fi
PY_VER=$("$PYTHON" --version 2>&1 | grep -oP '\d+\.\d+' | head -1 || echo "0.0")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    err "Need Python 3.10+ (have $PY_VER)"
    exit 1
fi
ok "Python $PY_VER"

# Git
if ! command -v git &>/dev/null; then
    err "Git not found. Install git and retry."
    exit 1
fi
ok "Git $(git --version 2>&1 | grep -oP '\d+\.\d+\.\d+' || echo 'ok')"

# OS
case "$(uname -s)" in
    Linux)  OS="linux" ;;
    Darwin) OS="darwin" ;;
    MINGW*|MSYS*|CYGWIN*) OS="windows" ;;
    *)      OS="unknown" ;;
esac
ok "System: $(uname -s) / $(uname -m)"

# ═══════════════════════════════════════════════════════════
#  2. Clone / Update Repo
# ═══════════════════════════════════════════════════════════
step "2/6  Code"

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating from GitHub..."
    cd "$INSTALL_DIR"
    git fetch origin "$BRANCH" 2>/dev/null
    git reset --hard "origin/$BRANCH" 2>/dev/null
    ok "Updated to latest $BRANCH"
else
    info "Cloning repository..."
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR" 2>/dev/null
    ok "Cloned → $INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ═══════════════════════════════════════════════════════════
#  3. Virtual Environment + Dependencies
# ═══════════════════════════════════════════════════════════
step "3/6  Python Environment"

VENV_DIR="$INSTALL_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi
# Upgrade pip
"$VENV_DIR/bin/pip" install --quiet --upgrade pip 2>/dev/null || true

# Install dependencies
if [ -f "requirements.txt" ]; then
    info "Installing dependencies..."
    "$VENV_DIR/bin/pip" install --quiet -r requirements.txt 2>&1 | tail -3
fi
# Ensure requests is installed (needed for P2P bootstrap with Cloudflare)
"$VENV_DIR/bin/pip" install --quiet requests 2>/dev/null || true
ok "Virtual environment ready"

# ═══════════════════════════════════════════════════════════
#  4. P2P Consent (required for decentralized features)
# ═══════════════════════════════════════════════════════════
step "4/6  P2P Configuration"

CONSENT_DIR="$HOME/.worldwave"
CONSENT_FILE="$CONSENT_DIR/consent.json"
mkdir -p "$CONSENT_DIR"

if [ ! -f "$CONSENT_FILE" ]; then
    cat > "$CONSENT_FILE" << 'CONSENT_EOF'
{
    "version": 1,
    "consent": {
        "p2p": true,
        "gossip": true,
        "dht": true,
        "nostr": true
    }
}
CONSENT_EOF
    ok "Consent file created → $CONSENT_FILE"
else
    ok "Consent file exists → $CONSENT_FILE"
fi

# Bootstrap config
info "Bootstrap URLs: $BOOTSTRAP_URLS"
if [ -n "$DHT_SEEDS" ]; then
    info "DHT seeds: $DHT_SEEDS"
fi

# ═══════════════════════════════════════════════════════════
#  5. Network Check
# ═══════════════════════════════════════════════════════════
step "5/6  Network Check"

# Test if bootstrap tracker is reachable
FIRST_URL=$(echo "$BOOTSTRAP_URLS" | cut -d, -f1)
if command -v curl &>/dev/null; then
    if curl -s --connect-timeout 5 "$FIRST_URL/health" >/dev/null 2>&1; then
        ok "Tracker reachable: $FIRST_URL"
    else
        warn "Tracker unreachable: $FIRST_URL"
        warn "Node will try cached peers + DHT on startup"
    fi
else
    warn "curl not available — skipping network check"
fi

# ═══════════════════════════════════════════════════════════
#  6. Start Node
# ═══════════════════════════════════════════════════════════
step "6/6  Starting Worldwave P2P Node"

echo -e "  ${BOLD}Install:${NC}  $INSTALL_DIR"
echo -e "  ${BOLD}Venv:${NC}     $VENV_DIR"
echo -e "  ${BOLD}Port:${NC}     $WW_PORT (API) / $P2P_PORT (P2P)"
echo -e "  ${BOLD}Tracker:${NC}  $BOOTSTRAP_URLS"
echo ""

# Build env
ENV="WW_BOOTSTRAP_URLS=$BOOTSTRAP_URLS"
ENV="$ENV WW_PORT=$WW_PORT"
[ -n "$DHT_SEEDS" ] && ENV="$ENV WW_DHT_BOOTSTRAP_NODES=$DHT_SEEDS"

info "Launching server..."
echo ""
echo -e "  ${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "  ${DIM}  Logs below. Press Ctrl+C to stop.${NC}"
echo -e "  ${DIM}  Health check: curl http://localhost:$WW_PORT/health${NC}"
echo -e "  ${DIM}  P2P peers:   curl http://localhost:$P2P_PORT/p2p/peers/all${NC}"
echo -e "  ${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

cd "$INSTALL_DIR"
exec env $ENV "$VENV_DIR/bin/python" server.py
