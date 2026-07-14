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

# ── Helpers (shared by subcommands + install) ──
# True if env or .env has a non-empty LLM API key.
ww_has_llm_key() {
    local env_file="${1:-}"
    local var key val
    for var in DEEPSEEK_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENROUTER_API_KEY CUSTOM_API_KEY; do
        # bash indirect expansion
        if [ -n "${!var:-}" ]; then
            return 0
        fi
    done
    if [ -n "$env_file" ] && [ -f "$env_file" ]; then
        for key in DEEPSEEK_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENROUTER_API_KEY CUSTOM_API_KEY; do
            val=$(grep "^${key}=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')
            # Treat empty / placeholder values as missing
            if [ -n "$val" ] && [ "$val" != "sk-your-deepseek-key-here" ] && [ "$val" != "your-key-here" ]; then
                return 0
            fi
        done
    fi
    return 1
}

# ── Subcommands ──
CMD="${1:-start}"
if [ "$CMD" = "update" ]; then
    INSTALL_DIR="${WW_HOME:-$HOME/worldwave}"
    cd "$INSTALL_DIR"
    echo "🌊 Worldwave — Updating..."
    git fetch origin main 2>/dev/null
    git reset --hard origin/main 2>/dev/null
    echo "   ✓ Updated to $(git log -1 --format='%h %s')"
    VENV_DIR="$INSTALL_DIR/.venv"
    if [ ! -f "$VENV_DIR/bin/pip" ]; then
        python3 -m venv "$VENV_DIR"
    fi
    "$VENV_DIR/bin/pip" install --quiet -r requirements.txt 2>/dev/null || true
    echo "   ✓ Dependencies ready"
    # Re-install ww binary (in case bin/ww changed)
    LOCAL_BIN="$HOME/.local/bin"
    mkdir -p "$LOCAL_BIN"
    cp "$INSTALL_DIR/bin/ww" "$LOCAL_BIN/ww" 2>/dev/null || true
    chmod +x "$LOCAL_BIN/ww" 2>/dev/null || true
    # Ensure ~/.local/bin is on PATH hint
    if ! echo "$PATH" | grep -q "$LOCAL_BIN"; then
        echo "   ℹ Add to PATH: export PATH=\"$LOCAL_BIN:\$PATH\""
    fi

    ENV_FILE="$INSTALL_DIR/.env"
    ENV="WW_PORT=${WW_PORT:-9300}"
    if [ -f "$ENV_FILE" ]; then
        # Load KEY=VAL lines only (skip comments/blank); do not print secrets
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                \#*|"") continue ;;
                *=*) ENV="$ENV $line" ;;
            esac
        done < "$ENV_FILE"
    fi

    # Restart server only if already running — always background, never foreground logs
    SERVER_WAS_RUNNING=false
    if systemctl --user is-active --quiet ww.service 2>/dev/null; then
        SERVER_WAS_RUNNING=true
        systemctl --user restart ww.service 2>/dev/null || true
        echo "   ✓ Server restarted (systemd --user)"
    elif pgrep -f "python.*server\.py" >/dev/null 2>&1; then
        SERVER_WAS_RUNNING=true
        pkill -f "python.*server\.py" 2>/dev/null || true
        sleep 1
        # shellcheck disable=SC2086
        nohup env $ENV "$VENV_DIR/bin/python" server.py \
            >>"$INSTALL_DIR/server.log" 2>&1 &
        disown 2>/dev/null || true
        echo "   ✓ Server restarted in background (log: $INSTALL_DIR/server.log)"
    fi

    if [ "$SERVER_WAS_RUNNING" = false ]; then
        echo "   ✓ Code updated (server was not running)"
    fi
    echo ""
    echo "Updated. Run: ww"
    exit 0
fi

if [ "$CMD" = "key" ]; then
    INSTALL_DIR="${WW_HOME:-$HOME/worldwave}"
    ENV_FILE="$INSTALL_DIR/.env"
    KEY_ACTION="${2:-show}"
    NEW_KEY="${3:-}"

    case "$KEY_ACTION" in
        set)
            if [ -z "$NEW_KEY" ]; then
                echo "⚠️  Usage: ww key set sk-xxx"
                echo "   Get a free key: https://platform.deepseek.com"
                exit 1
            fi
            # Validate key format
            if ! echo "$NEW_KEY" | grep -q "^sk-"; then
                echo "⚠️  Invalid key format. DeepSeek keys start with 'sk-'"
                exit 1
            fi
            mkdir -p "$(dirname "$ENV_FILE")"
            # Write/update DEEPSEEK_API_KEY in .env
            if [ -f "$ENV_FILE" ] && grep -q "^DEEPSEEK_API_KEY=" "$ENV_FILE" 2>/dev/null; then
                # Update existing key
                if [ "$(uname -s)" = "Darwin" ]; then
                    sed -i '' "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$NEW_KEY|" "$ENV_FILE"
                else
                    sed -i "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$NEW_KEY|" "$ENV_FILE"
                fi
                echo "✓ Key updated in $ENV_FILE"
            else
                echo "DEEPSEEK_API_KEY=$NEW_KEY" >> "$ENV_FILE"
                echo "✓ Key saved to $ENV_FILE"
            fi
            # Chat loads .env via dotenv — no server restart / ww update needed
            echo "  Ready. Type: ww"
            exit 0
            ;;
        show)
            if [ -f "$ENV_FILE" ] && grep -q "^DEEPSEEK_API_KEY=" "$ENV_FILE" 2>/dev/null; then
                KEY_LINE=$(grep "^DEEPSEEK_API_KEY=" "$ENV_FILE" | head -1)
                KEY_VAL=$(echo "$KEY_LINE" | cut -d= -f2- | tr -d '\r')
                if [ -z "$KEY_VAL" ]; then
                    echo "⚠️  No key configured (empty DEEPSEEK_API_KEY in .env)."
                    echo "   Set one: ww key set sk-xxx"
                    echo "   Get a free key: https://platform.deepseek.com"
                else
                    MASKED="$(echo "$KEY_VAL" | head -c 8)...$(echo "$KEY_VAL" | tail -c 5)"
                    echo "🔑 Current key: $MASKED"
                fi
            else
                echo "⚠️  No key configured."
                echo "   Set one: ww key set sk-xxx"
                echo "   Get a free key: https://platform.deepseek.com"
            fi
            exit 0
            ;;
        test)
            if [ ! -f "$ENV_FILE" ] || ! grep -q "^DEEPSEEK_API_KEY=" "$ENV_FILE" 2>/dev/null; then
                echo "⚠️  No key configured. Set one first: ww key set sk-xxx"
                exit 1
            fi
            # Read raw key; do NOT redacted — Authorization must be the real token
            KEY_VAL=$(grep "^DEEPSEEK_API_KEY=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')
            if [ -z "$KEY_VAL" ]; then
                echo "⚠️  DEEPSEEK_API_KEY is empty. Set one: ww key set sk-xxx"
                exit 1
            fi
            echo "🔍 Testing DeepSeek API..."
            # Pass real key in Authorization (do not redact to *** — that breaks the request)
            RESP=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${KEY_VAL}" \
                "https://api.deepseek.com/v1/models" 2>&1 || echo "NETWORK_ERROR")
            if echo "$RESP" | grep -q '"id"'; then
                echo "✅ Key is valid — API reachable"
            elif echo "$RESP" | grep -q "NETWORK_ERROR"; then
                echo "❌ Network error — check internet connection"
            else
                echo "❌ Key invalid or API error:"
                echo "$RESP" | head -3
            fi
            exit 0
            ;;
        *)
            echo "🌊 ww key — manage DeepSeek API key"
            echo ""
            echo "  ww key set sk-xxx   Save/update API key"
            echo "  ww key show         Show current key (masked)"
            echo "  ww key test         Test key against DeepSeek API"
            echo ""
            echo "  Get a free key: https://platform.deepseek.com"
            exit 0
            ;;
    esac
fi

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
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
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
#  6. Finish install → chat (not foreground server logs)
# ═══════════════════════════════════════════════════════════
step "6/6  Ready for chat"

echo -e "  ${BOLD}Install:${NC}  $INSTALL_DIR"
echo -e "  ${BOLD}Venv:${NC}     $VENV_DIR"
echo -e "  ${BOLD}Port:${NC}     $WW_PORT (API auto-starts with ww)"
echo ""

ENV_FILE="$INSTALL_DIR/.env"

# Auto-detect LLM API key from environment and persist to .env
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    if [ ! -f "$ENV_FILE" ]; then
        echo "DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY" > "$ENV_FILE"
    elif ! grep -q "^DEEPSEEK_API_KEY=" "$ENV_FILE" 2>/dev/null; then
        echo "DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY" >> "$ENV_FILE"
    else
        # Replace empty/placeholder key line with env value
        if [ "$(uname -s)" = "Darwin" ]; then
            sed -i '' "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY|" "$ENV_FILE"
        else
            sed -i "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY|" "$ENV_FILE"
        fi
    fi
    ok "DEEPSEEK_API_KEY detected from environment"
elif [ -f "$ENV_FILE" ] && grep -qE "^DEEPSEEK_API_KEY=.+" "$ENV_FILE" 2>/dev/null; then
    ok "DEEPSEEK_API_KEY loaded from $ENV_FILE"
fi

# Node ID (quiet — one line)
NODE_ID_FILE="$HOME/.ww_data/node_id.txt"
if [ -f "$NODE_ID_FILE" ]; then
    NID=$(cat "$NODE_ID_FILE")
else
    NID=$("$VENV_DIR/bin/python" -c "import uuid; print(uuid.uuid4().hex[:12])")
    mkdir -p "$(dirname "$NODE_ID_FILE")"
    echo "$NID" > "$NODE_ID_FILE"
fi
ok "Node ID: $NID"

# Install ww CLI to PATH
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
cp "$INSTALL_DIR/bin/ww" "$LOCAL_BIN/ww"
chmod +x "$LOCAL_BIN/ww"
# Ensure ~/.local/bin is in PATH
if ! echo "$PATH" | grep -q "$LOCAL_BIN"; then
    if ! grep -q "$LOCAL_BIN" "$HOME/.bashrc" 2>/dev/null; then
        echo "export PATH=\"$LOCAL_BIN:\$PATH\"" >> "$HOME/.bashrc"
    fi
    export PATH="$LOCAL_BIN:$PATH"
    warn "Added $LOCAL_BIN to PATH (new shells pick it up from ~/.bashrc)"
fi
ok "ww command ready → $LOCAL_BIN/ww"

# LLM key: prompt when missing/empty/partial .env (not only when .env is absent)
if ! ww_has_llm_key "$ENV_FILE"; then
    if [ -t 0 ]; then
        echo ""
        echo -e "  ${BOLD}🔑  Paste your DeepSeek API key to chat:${NC}"
        echo -e "  ${DIM}     Free key: https://platform.deepseek.com${NC}"
        echo ""
        printf "  ${CYAN}→ ${NC}"
        read -r USER_KEY || USER_KEY=""
        USER_KEY=$(echo "$USER_KEY" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        if [ -n "$USER_KEY" ]; then
            mkdir -p "$(dirname "$ENV_FILE")"
            if [ -f "$ENV_FILE" ] && grep -q "^DEEPSEEK_API_KEY=" "$ENV_FILE" 2>/dev/null; then
                if [ "$(uname -s)" = "Darwin" ]; then
                    sed -i '' "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$USER_KEY|" "$ENV_FILE"
                else
                    sed -i "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$USER_KEY|" "$ENV_FILE"
                fi
            else
                echo "DEEPSEEK_API_KEY=$USER_KEY" >> "$ENV_FILE"
            fi
            export DEEPSEEK_API_KEY="$USER_KEY"
            echo ""
            ok "Key saved — change anytime: ww key set sk-xxx"
        else
            echo ""
            warn "No key entered — chat needs a key first"
            echo -e "  ${DIM}  Later: ww key set sk-xxx${NC}"
            echo -e "  ${DIM}  Get one: https://platform.deepseek.com${NC}"
        fi
    else
        # Non-TTY (curl | bash, CI): install done, do not hang on prompt or server
        echo ""
        warn "No LLM API key configured (non-interactive install)"
        echo "  Set a key, then chat:"
        echo "    ww key set sk-xxx"
        echo "    ww"
        echo "  Free key: https://platform.deepseek.com"
        echo ""
        ok "Install complete"
        exit 0
    fi
fi

echo ""
echo -e "  ${BOLD}${GREEN}═══ Worldwave ready ═══${NC}"
echo -e "  ${DIM}Server starts automatically when you chat (no log spam).${NC}"
echo ""

cd "$INSTALL_DIR"
# Interactive default: enter chat. Server is auto-started by ww_cli (background).
# Never attach foreground server.py as the end state of first install.
if [ -t 0 ] && [ -t 1 ] && ww_has_llm_key "$ENV_FILE"; then
    echo -e "  Starting interactive chat…  (${DIM}Ctrl+C or /exit to leave${NC})"
    echo ""
    exec "$LOCAL_BIN/ww"
fi

echo "  Ready. Type: ww"
echo ""
exit 0
