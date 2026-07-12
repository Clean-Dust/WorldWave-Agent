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
# ============================================================
#  Supports: Linux (Ubuntu/Debian), macOS, WSL2
#  Zero manual setup — auto-installs Python + Git if missing
# ============================================================
set -euo pipefail

# ── Config (override via env vars) ──
REPO="${WW_REPO:-https://github.com/Clean-Dust/worldwave.git}"
BRANCH="${WW_BRANCH:-main}"
INSTALL_DIR="${WW_HOME:-$HOME/worldwave}"
PYTHON="${WW_PYTHON:-python3}"
# ── Bootstrap — every node is a tracker. Comma-separated, tried in order.
#     Public:    http://tracker.dse-5-star-star.org      (Apple, anyone)
#     Tailscale: http://100.80.143.105:19833              (Banana, team only)
#     Custom:    set WW_BOOTSTRAP_URLS env var to override
BOOTSTRAP_URLS="${WW_BOOTSTRAP_URLS:-http://tracker.dse-5-star-star.org,http://100.80.143.105:19833}"
# P2P port for HTTP server (every node serves as tracker on this port)
P2P_PORT="${WW_P2P_PORT:-19833}"
# DHT bootstrap seeds — UDP-based, fully decentralized (no central server needed)
# Format: "IP:port,IP:port"  (port is usually P2P_PORT+1 = 19834)
DHT_SEEDS="${WW_DHT_BOOTSTRAP_NODES:-100.80.143.105:19834}"
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

# ── OS detection ──
case "$(uname -s)" in
    Linux)  OS="linux" ;;
    Darwin) OS="darwin" ;;
    MINGW*|MSYS*|CYGWIN*) OS="windows" ;;
    *)      OS="unknown" ;;
esac

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
#  0. Auto-install missing system deps
# ═══════════════════════════════════════════════════════════
NEED_SUDO=""
MISSING_PKGS=""

# Git
if ! command -v git &>/dev/null; then
    case "$OS" in
        linux)  MISSING_PKGS="$MISSING_PKGS git" ;;
        darwin) MISSING_PKGS="$MISSING_PKGS git" ;;  # brew handles below
        *)      warn "Git not found. Install it manually: https://git-scm.com" ;;
    esac
fi

# Python 3
HAVE_PY=false
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' | head -1 || echo "0.0")
    PY_MJ=$(echo "$PY_VER" | cut -d. -f1)
    PY_MN=$(echo "$PY_VER" | cut -d. -f2)
    if [ "$PY_MJ" -ge 3 ] && [ "$PY_MN" -ge 10 ]; then
        HAVE_PY=true
    fi
fi
if ! $HAVE_PY; then
    case "$OS" in
        linux)  MISSING_PKGS="$MISSING_PKGS python3 python3-pip" ;;
        darwin) MISSING_PKGS="$MISSING_PKGS python3" ;;
        *)      warn "Python 3.10+ not found. Install it manually: https://python.org" ;;
    esac
fi

# Compute version-specific venv package name (e.g. python3.10-venv)
VENV_PKG="python${PY_VER}-venv"

# Check venv module separately — python3-venv package may be missing even with Python 3 installed
if $HAVE_PY && ! python3 -m venv --help >/dev/null 2>&1; then
    case "$OS" in
        linux) MISSING_PKGS="$MISSING_PKGS $VENV_PKG" ;;
    esac
fi

# Install missing packages
if [ -n "$MISSING_PKGS" ]; then
    step "0/6  Installing System Dependencies"
    echo -e "  Missing:${BOLD}$MISSING_PKGS${NC}"
    echo ""
    case "$OS" in
        linux)
            if command -v sudo &>/dev/null && [ "$(id -u)" != "0" ]; then
                NEED_SUDO="sudo"
            fi
            info "Running: $NEED_SUDO apt update && $NEED_SUDO apt install -y$MISSING_PKGS"
            echo ""
            $NEED_SUDO apt-get update -qq 2>/dev/null || true
            $NEED_SUDO apt-get install -y -qq $MISSING_PKGS 2>&1 | tail -3
            ;;
        darwin)
            if ! command -v brew &>/dev/null; then
                err "Homebrew not found. Install it first:"
                echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
                exit 1
            fi
            # Install missing individually
            for pkg in $MISSING_PKGS; do
                info "brew install $pkg..."
                brew install "$pkg" 2>&1 | tail -1
            done
            ;;
    esac
    ok "Dependencies installed"
else
    step "0/6  System Dependencies"
    ok "Python 3 + Git already installed"
fi

# ═══════════════════════════════════════════════════════════
#  1. Environment Summary
# ═══════════════════════════════════════════════════════════
step "1/6  Environment Check"

ok "Python $("$PYTHON" --version 2>&1)"
ok "Git $(git --version 2>&1 | grep -oP '\d+\.\d+\.\d+' || echo 'ok')"
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
# Recreate venv if missing OR broken (leftover from failed install)
if [ ! -f "$VENV_DIR/bin/pip" ]; then
    if [ -d "$VENV_DIR" ]; then
        warn "Found broken venv — recreating..."
        rm -rf "$VENV_DIR"
    fi
    info "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment ready"
else
    ok "Virtual environment exists"
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
        "p2p_network": true,
        "model_broadcast": true,
        "auto_update": true
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
echo ""

# Show node ID (pre-generate if needed)
NODE_ID_FILE="$HOME/.ww_data/node_id.txt"
if [ -f "$NODE_ID_FILE" ]; then
    NID=$(cat "$NODE_ID_FILE")
else
    NID=$("$VENV_DIR/bin/python" -c "import uuid; print(uuid.uuid4().hex[:12])")
    mkdir -p "$(dirname "$NODE_ID_FILE")"
    echo "$NID" > "$NODE_ID_FILE"
fi
echo -e "  ${BOLD}${GREEN}🆔 Your Node ID: ${CYAN}$NID${NC}"
echo -e "  ${DIM}  Share this with the network admin so they can find you.${NC}"
echo -e "  ${DIM}  Verify: curl http://tracker.dse-5-star-star.org/p2p/whois/$NID${NC}"
echo -e "  ${DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

cd "$INSTALL_DIR"
exec env $ENV "$VENV_DIR/bin/python" server.py
