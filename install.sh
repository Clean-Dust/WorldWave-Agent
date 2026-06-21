#!/usr/bin/env bash
#
# worldwave — One-click install script v0.2
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Clean-Dust/worldwave/main/install.sh)
#   # or:
#   bash install.sh
#
# Supports: Linux (Ubuntu/Debian/CentOS), macOS, WSL2
# For native Windows: use install.ps1 (PowerShell)
# Minimum: Python 3.10+, pip

set -euo pipefail

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info()  { echo -e "${BLUE}⟳${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1" >&2; }
header(){ echo -e "\n${BOLD}${CYAN}══ $1 ══${NC}\n"; }

# ── Default Paths ──
INSTALL_DIR="${WW_HOME:-$HOME/worldwave}"
WW_CONFIG="${WW_CONFIG:-$HOME/.ww}"
PYTHON="${WW_PYTHON:-python3}"
MIN_PYTHON="3.10"
BRANCH="${WW_BRANCH:-main}"
REPO="${WW_REPO:-https://github.com/Clean-Dust/worldwave.git}"

# ── Pre-flight Checks ──
header "Environment Check"

# Python
if ! command -v $PYTHON &>/dev/null; then
    err "Python ($PYTHON) not found. Please install Python $MIN_PYTHON+"
    exit 1
fi
pyver=$($PYTHON --version 2>&1 | grep -oP '\d+\.\d+\.\d+' | head -1 || echo "0.0.0")
major=$(echo "$pyver" | cut -d. -f1)
minor=$(echo "$pyver" | cut -d. -f2)
if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    err "Need Python $MIN_PYTHON+ (current: $pyver)"
    exit 1
fi
ok "Python $pyver"

# Git
if command -v git &>/dev/null; then
    GIT_AVAILABLE=true
    ok "Git $(git --version 2>&1 | grep -oP '\d+\.\d+\.\d+' || echo '?')"
else
    GIT_AVAILABLE=false
    warn "Git not installed, skipping version management"
fi

# pip
if $PYTHON -m pip --version &>/dev/null; then
    PIP_AVAILABLE=true
    ok "pip $($PYTHON -m pip --version 2>&1 | grep -oP '\d+\.\d+\.\d+' | head -1)"
else
    PIP_AVAILABLE=false
    warn "pip not installed, skipping dependency installation"
fi

# OS
case "$(uname -s)" in
    Linux)  OS="linux" ;;
    Darwin) OS="darwin" ;;
    *)      OS="unknown" ;;
esac
ok "System: $(uname -s) / $(uname -m)"

# ── Download WW ──
header "Installing Worldwave"

if [ -d "$INSTALL_DIR" ]; then
    info "Directory exists: $INSTALL_DIR"
    if $GIT_AVAILABLE && [ -d "$INSTALL_DIR/.git" ]; then
        info "Updating code..."
        cd "$INSTALL_DIR" && git pull origin $BRANCH 2>/dev/null || true
    fi
else
    if $GIT_AVAILABLE; then
        info "Cloning from repo..."
        git clone --depth 1 --branch $BRANCH "$REPO" "$INSTALL_DIR" 2>/dev/null || {
            warn "Git clone failed, creating empty directory"
            mkdir -p "$INSTALL_DIR"
        }
    else
        mkdir -p "$INSTALL_DIR"
    fi
fi
ok "Install directory: $INSTALL_DIR"

# ── Virtual Environment ──
header "Python Virtual Environment"

VENV_DIR="$INSTALL_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    info "Virtual environment exists, upgrading pip..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip 2>/dev/null || true
else
    info "Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
    ok "Virtual environment created"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
ok "Python: $($VENV_PYTHON --version 2>&1)"

# ── Install WW Package ──
header "Installing Worldwave"

if $PIP_AVAILABLE; then
    # If VENV_PYTHON is not pipx, use pip install -e .
    cd "$INSTALL_DIR"
    if [ -f "pyproject.toml" ]; then
        info "Installing WW package (editable mode)..."
        "$VENV_DIR/bin/pip" install --quiet -e . 2>&1 | tail -1 || {
            warn "pip install failed, trying core dependencies..."
            "$VENV_DIR/bin/pip" install --quiet -e . --no-build-isolation 2>&1 | tail -1
        }
    else
        info "Installing core dependencies..."
        "$VENV_DIR/bin/pip" install --quiet fastapi uvicorn pydantic httpx requests 2>&1 | tail -1
    fi
    ok "Dependencies installed"

    # Optional dependencies
    if [ -f "$INSTALL_DIR/core/subconscious/nostr.py" ]; then
        "$VENV_DIR/bin/pip" install --quiet websockets 2>/dev/null || true
    fi
    if [ -f "$INSTALL_DIR/tools/browser.py" ]; then
        "$VENV_DIR/bin/pip" install --quiet playwright 2>/dev/null || true
    fi

    # Show install status
    "$VENV_DIR/bin/pip" list --format=columns 2>&1 | grep -i "worldwave\|fastapi\|uvicorn" || true
fi

# ── Setup ──
header "Initial Setup"

mkdir -p "$WW_CONFIG"

# .env template (don't overwrite)
ENV_FILE="$INSTALL_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'EOF'
# Worldwave Environment Configuration
# Fill in at least one LLM API key to get started

# LLM Provider (at least one required) 
DEEPSEEK_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=

# Custom / Self-hosted Provider (Ollama, vLLM, etc.)
CUSTOM_API_KEY=
CUSTOM_BASE_URL=http://localhost:11434/v1

# Computer Use Vision (optional) 
WW_VISION_API_KEY=
WW_VISION_MODEL=qwen/qwen2.5-vl-72b-instruct

# MQTT Communication (optional) 
WW_MQTT_HOST=localhost
WW_MQTT_PORT=1883

# SSH Remote Hosts (optional) 
WW_SSH_HOSTS=

# Telegram Gateway (optional) 
TELEGRAM_WW_TOKEN=
TELEGRAM_WW_WORKSPACE=

# Discord Gateway (optional) 
DISCORD_BOT_TOKEN=

# Webhook Gateway (optional) 
WW_WEBHOOK_SECRET=

# API Authentication (auto-generated if not set)
# WW_API_KEY=

# Service Settings
WW_PORT=9300
WW_HOME=${HOME}/worldwave
WW_CONFIG=${HOME}/.ww
WW_MEMORY_SLEEP_HOUR=3
WW_HIPPOCAMPUS_CAP=100
EOF
    ok ".env template created → fill in your API keys"
else
    warn ".env already exists, skipping"
fi

# Default config
CONFIG_FILE="$WW_CONFIG/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    mkdir -p "$WW_CONFIG/profiles"
    cat > "$CONFIG_FILE" << 'EOF'
{
    "model": "deepseek/deepseek-v4-flash",
    "provider": "deepseek",
    "memory_enabled": true,
    "subconscious_enabled": true,
    "tools_enabled": true
}
EOF
    # Default profile
    cat > "$WW_CONFIG/profiles/default.json" << 'EOF'
{
    "provider": "deepseek",
    "model": "deepseek/deepseek-v4-flash",
    "profile_name": "default"
}
EOF
    ok "Default configuration created (profile: default)"
fi

# ── CLI ──
header "CLI Setup"

mkdir -p "$HOME/.local/bin"
CLI_TARGET="$HOME/.local/bin/ww"

# Prefer pip-installed ww command from the venv
if [ -f "$VENV_DIR/bin/ww" ]; then
    # Create symlink to pip-installed entry point
    ln -sf "$VENV_DIR/bin/ww" "$CLI_TARGET"
    ok "ww command installed → $CLI_TARGET (→ venv entry point)"

    # PATH reminder
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        warn "\$HOME/.local/bin is not in PATH"
        echo "  Run:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    fi
    ok "ww command installed → $CLI_TARGET"
fi

# ── Systemd Service (Linux, optional) ──
if [ "$OS" = "linux" ] && command -v systemctl &>/dev/null; then
    header "Systemd Service (optional)"

    SERVICE_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$SERVICE_DIR/ww.service"

    if [ ! -f "$SERVICE_FILE" ]; then
        mkdir -p "$SERVICE_DIR"
        cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Worldwave AI Agent Server
After=network.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/server.py
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload 2>/dev/null || true
        info "Systemd service created: $SERVICE_FILE"
        info "  Enable: systemctl --user enable --now ww.service"
    else
        warn "Service file already exists, skipping"
    fi
fi

# ── Complete ──
header "✅ Worldwave Installation Complete!"

echo -e "  ${BOLD}Install:${NC}  $INSTALL_DIR"
echo -e "  ${BOLD}Venv:${NC}     $VENV_DIR"
echo -e "  ${BOLD}Config:${NC}   $WW_CONFIG"
echo -e "  ${BOLD}CLI:${NC}      $CLI_TARGET"
echo ""
echo -e "  ${BOLD}Next Steps:${NC}"
echo "    1. Edit .env and fill in API keys"
echo "       ${DIM}nano $INSTALL_DIR/.env${NC}"
echo ""
echo "    2. Start the server"
echo "       ${DIM}ww server start${NC}"
echo ""
echo "    3. Run your first task"
echo "       ${DIM}ww run 'What can you do?'${NC}"
echo ""
echo -e "  ${DIM}Docs: github.com/Clean-Dust/worldwave${NC}"
echo ""
