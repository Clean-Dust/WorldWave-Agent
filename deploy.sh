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
# LLM env vars supported by core (transports + ww_has_llm_key).
# GOOGLE_API_KEY is an alias for Gemini (has-key checks); primary write target is GEMINI_API_KEY.
WW_LLM_KEY_VARS="DEEPSEEK_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENROUTER_API_KEY GEMINI_API_KEY GOOGLE_API_KEY XAI_API_KEY GROQ_API_KEY FIREWORKS_API_KEY TOGETHER_API_KEY MISTRAL_API_KEY MOONSHOT_API_KEY DEEPINFRA_API_KEY OLLAMA_API_KEY CUSTOM_API_KEY"
WW_PROVIDER_IDS="deepseek openai anthropic openrouter gemini xai groq fireworks together mistral moonshot deepinfra ollama custom"

# True if value looks like a real key (not empty / placeholder).
ww_key_value_ok() {
    local val="${1:-}"
    [ -z "$val" ] && return 1
    case "$val" in
        sk-your-deepseek-key-here|your-key-here|sk-xxx|xxx|changeme|placeholder|none|null)
            return 1 ;;
    esac
    return 0
}

# True if Ollama local mode is opted in (env or .env).
ww_has_ollama_opt_in() {
    local env_file="${1:-}"
    local val
    val="${WW_USE_OLLAMA:-}"
    case "$(echo "${val}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
    esac
    if [ -n "${OLLAMA_BASE_URL:-}" ] || [ -n "${OLLAMA_HOST:-}" ]; then
        return 0
    fi
    if [ -n "$env_file" ] && [ -f "$env_file" ]; then
        val=$(grep "^WW_USE_OLLAMA=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')
        case "$(echo "${val:-}" | tr '[:upper:]' '[:lower:]')" in
            1|true|yes|on) return 0 ;;
        esac
        if grep -qE "^OLLAMA_BASE_URL=.+" "$env_file" 2>/dev/null; then
            return 0
        fi
        if grep -qE "^OLLAMA_HOST=.+" "$env_file" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# True if env or .env has a non-empty LLM API key (or Ollama opt-in).
ww_has_llm_key() {
    local env_file="${1:-}"
    local var key val
    for var in $WW_LLM_KEY_VARS; do
        # bash indirect expansion
        if [ -n "${!var:-}" ] && ww_key_value_ok "${!var}"; then
            return 0
        fi
    done
    if [ -n "$env_file" ] && [ -f "$env_file" ]; then
        for key in $WW_LLM_KEY_VARS; do
            val=$(grep "^${key}=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')
            if ww_key_value_ok "$val"; then
                return 0
            fi
        done
    fi
    if ww_has_ollama_opt_in "$env_file"; then
        return 0
    fi
    return 1
}

# True if Telegram gateway token is already configured (env or .env).
ww_has_telegram_token() {
    local env_file="${1:-}"
    local val
    val="${TELEGRAM_WW_TOKEN:-}"
    val=$(echo "$val" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -n "$val" ]; then
        return 0
    fi
    if [ -n "$env_file" ] && [ -f "$env_file" ]; then
        val=$(grep "^TELEGRAM_WW_TOKEN=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//;s/^[[:space:]]*//;s/[[:space:]]*$//')
        if [ -n "$val" ]; then
            return 0
        fi
    fi
    return 1
}

# Upsert KEY=VALUE in .env (Darwin/Linux sed). Creates file if missing.
ww_upsert_env() {
    local key="$1"
    local value="$2"
    local env_file="${3:-}"
    if [ -z "$key" ] || [ -z "$env_file" ]; then
        return 1
    fi
    mkdir -p "$(dirname "$env_file")"
    if [ ! -f "$env_file" ]; then
        printf '%s=%s\n' "$key" "$value" > "$env_file"
        return 0
    fi
    if grep -q "^${key}=" "$env_file" 2>/dev/null; then
        if [ "$(uname -s)" = "Darwin" ]; then
            sed -i '' "s|^${key}=.*|${key}=${value}|" "$env_file"
        else
            sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
        fi
    else
        printf '%s=%s\n' "$key" "$value" >> "$env_file"
    fi
}

# Map provider name → env var for API key.
ww_env_var_for_provider() {
    case "${1:-}" in
        deepseek)   echo "DEEPSEEK_API_KEY" ;;
        openai)     echo "OPENAI_API_KEY" ;;
        anthropic)  echo "ANTHROPIC_API_KEY" ;;
        openrouter) echo "OPENROUTER_API_KEY" ;;
        gemini|google) echo "GEMINI_API_KEY" ;;
        xai|grok)   echo "XAI_API_KEY" ;;
        groq)       echo "GROQ_API_KEY" ;;
        fireworks)  echo "FIREWORKS_API_KEY" ;;
        together)   echo "TOGETHER_API_KEY" ;;
        mistral)    echo "MISTRAL_API_KEY" ;;
        moonshot|kimi) echo "MOONSHOT_API_KEY" ;;
        deepinfra)  echo "DEEPINFRA_API_KEY" ;;
        ollama)     echo "OLLAMA_API_KEY" ;;
        custom)     echo "CUSTOM_API_KEY" ;;
        *)          echo "" ;;
    esac
}

# Map env var → provider name.
ww_provider_for_env_var() {
    case "${1:-}" in
        DEEPSEEK_API_KEY)   echo "deepseek" ;;
        OPENAI_API_KEY)     echo "openai" ;;
        ANTHROPIC_API_KEY)  echo "anthropic" ;;
        OPENROUTER_API_KEY) echo "openrouter" ;;
        GEMINI_API_KEY|GOOGLE_API_KEY) echo "gemini" ;;
        XAI_API_KEY)        echo "xai" ;;
        GROQ_API_KEY)       echo "groq" ;;
        FIREWORKS_API_KEY)  echo "fireworks" ;;
        TOGETHER_API_KEY)   echo "together" ;;
        MISTRAL_API_KEY)    echo "mistral" ;;
        MOONSHOT_API_KEY)   echo "moonshot" ;;
        DEEPINFRA_API_KEY)  echo "deepinfra" ;;
        OLLAMA_API_KEY)     echo "ollama" ;;
        CUSTOM_API_KEY)     echo "custom" ;;
        *)                  echo "" ;;
    esac
}

# Default model per provider (matches core/transports infer_provider + registry).
ww_default_model_for_provider() {
    case "${1:-}" in
        deepseek)   echo "deepseek/deepseek-v4-flash" ;;
        openai)     echo "gpt-4o-mini" ;;
        anthropic)  echo "claude-sonnet-4" ;;
        openrouter) echo "google/gemini-2.0-flash" ;;
        gemini)     echo "gemini-2.0-flash" ;;
        xai)        echo "grok-3-mini" ;;
        groq)       echo "llama-3.3-70b-versatile" ;;
        fireworks)  echo "accounts/fireworks/models/llama-v3p1-70b-instruct" ;;
        together)   echo "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo" ;;
        mistral)    echo "mistral-small-latest" ;;
        moonshot)   echo "moonshot-v1-8k" ;;
        deepinfra)  echo "meta-llama/Meta-Llama-3.1-8B-Instruct" ;;
        ollama)     echo "ollama/llama3.2" ;;
        custom)     echo "custom/default" ;;
        *)          echo "deepseek/deepseek-v4-flash" ;;
    esac
}

# Infer provider from key shape. Empty string = ambiguous sk-* (need ask).
ww_infer_provider_from_key() {
    local key="${1:-}"
    case "$key" in
        sk-ant-*|sk-ant*) echo "anthropic" ;;
        sk-or-*|sk-or*)   echo "openrouter" ;;
        sk-proj-*)        echo "openai" ;;
        gsk_*)            echo "groq" ;;
        xai-*)            echo "xai" ;;
        AIza*)            echo "gemini" ;;
        sk-*)             echo "" ;;  # DeepSeek / OpenAI / OpenRouter classic — ambiguous
        none|null|"")     echo "" ;;
        *)                echo "custom" ;;
    esac
}

# Normalize provider label (accept aliases). Empty if unknown.
# Number map (TTY menu): 1–8 majors, 9 More… submenu
ww_normalize_provider() {
    local p
    p=$(echo "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    case "$p" in
        deepseek|ds)           echo "deepseek" ;;
        openai|oai)            echo "openai" ;;
        anthropic|claude)      echo "anthropic" ;;
        openrouter|or)         echo "openrouter" ;;
        gemini|google)         echo "gemini" ;;
        xai|grok)              echo "xai" ;;
        groq)                  echo "groq" ;;
        fireworks|fw)          echo "fireworks" ;;
        together)              echo "together" ;;
        mistral)               echo "mistral" ;;
        moonshot|kimi)         echo "moonshot" ;;
        deepinfra|di)          echo "deepinfra" ;;
        ollama)                echo "ollama" ;;
        custom|local)          echo "custom" ;;
        1) echo "deepseek" ;;
        2) echo "openai" ;;
        3) echo "anthropic" ;;
        4) echo "openrouter" ;;
        5) echo "gemini" ;;
        6) echo "xai" ;;
        7) echo "groq" ;;
        8) echo "ollama" ;;
        *) echo "" ;;
    esac
}

# Read first non-empty LLM key from env/.env. Sets globals:
#   WW_PRIMARY_KEY_VAR, WW_PRIMARY_KEY_VAL, WW_PRIMARY_PROVIDER
ww_load_primary_key() {
    local env_file="${1:-}"
    local var val
    WW_PRIMARY_KEY_VAR=""
    WW_PRIMARY_KEY_VAL=""
    WW_PRIMARY_PROVIDER=""
    for var in $WW_LLM_KEY_VARS; do
        val="${!var:-}"
        if ww_key_value_ok "$val"; then
            WW_PRIMARY_KEY_VAR="$var"
            WW_PRIMARY_KEY_VAL="$val"
            WW_PRIMARY_PROVIDER="$(ww_provider_for_env_var "$var")"
            return 0
        fi
    done
    if [ -n "$env_file" ] && [ -f "$env_file" ]; then
        for var in $WW_LLM_KEY_VARS; do
            val=$(grep "^${var}=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')
            if ww_key_value_ok "$val"; then
                WW_PRIMARY_KEY_VAR="$var"
                WW_PRIMARY_KEY_VAL="$val"
                WW_PRIMARY_PROVIDER="$(ww_provider_for_env_var "$var")"
                return 0
            fi
        done
    fi
    return 1
}

# Ask TTY which provider (first-install, or ambiguous sk-* on CLI).
# Prints provider (stdout) or empty. Prompts go to stderr so $(...) stays clean.
# Provider first, then key (callers paste key after this returns).
ww_ask_provider_tty() {
    echo "  Which provider?" >&2
    echo "    1) DeepSeek" >&2
    echo "    2) OpenAI" >&2
    echo "    3) Anthropic" >&2
    echo "    4) OpenRouter" >&2
    echo "    5) Google Gemini" >&2
    echo "    6) xAI (Grok)" >&2
    echo "    7) Groq" >&2
    echo "    8) Ollama (local)" >&2
    echo "    9) More…" >&2
    printf "  → " >&2
    local choice="" more=""
    read -r choice || choice=""
    choice=$(echo "$choice" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    case "$choice" in
        1|deepseek|DeepSeek|ds)   echo "deepseek" ;;
        2|openai|OpenAI)          echo "openai" ;;
        3|anthropic|Anthropic|claude|Claude) echo "anthropic" ;;
        4|openrouter|OpenRouter)  echo "openrouter" ;;
        5|gemini|Gemini|google|Google) echo "gemini" ;;
        6|xai|xAI|XAI|grok|Grok)  echo "xai" ;;
        7|groq|Groq)              echo "groq" ;;
        8|ollama|Ollama)          echo "ollama" ;;
        9|more|More|m|M)
            echo "  More providers:" >&2
            echo "    a) Fireworks" >&2
            echo "    b) Together" >&2
            echo "    c) Mistral" >&2
            echo "    d) Moonshot (Kimi)" >&2
            echo "    e) DeepInfra" >&2
            echo "    f) Custom (OpenAI-compatible)" >&2
            printf "  → " >&2
            read -r more || more=""
            more=$(echo "$more" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            case "$more" in
                a|A|fireworks|Fireworks) echo "fireworks" ;;
                b|B|together|Together)   echo "together" ;;
                c|C|mistral|Mistral)     echo "mistral" ;;
                d|D|moonshot|Moonshot|kimi|Kimi) echo "moonshot" ;;
                e|E|deepinfra|DeepInfra) echo "deepinfra" ;;
                f|F|custom|Custom|local|Local) echo "custom" ;;
                *)
                    local n
                    n=$(ww_normalize_provider "$more")
                    echo "${n}"
                    ;;
            esac
            ;;
        *)
            local n
            n=$(ww_normalize_provider "$choice")
            echo "${n}"
            ;;
    esac
}

# Save LLM key + optional default model. Args: key [provider] [env_file]
# Returns 0 on success, 1 on validation failure, 2 if provider needed but unavailable (non-TTY).
# When force_provider is set and key shape clearly infers a different provider, warn and use
# the inferred one (safer). Ambiguous sk-* (empty inference) keeps the chosen provider.
# Ollama: empty key / "none" enables local mode (WW_USE_OLLAMA=1) without a real API key.
ww_save_llm_key() {
    local key="${1:-}"
    local force_provider="${2:-}"
    local env_file="${3:-}"
    local provider env_var model inferred

    key=$(echo "$key" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -z "$env_file" ]; then
        echo "⚠️  No .env path for key save."
        return 1
    fi

    provider=$(ww_normalize_provider "${force_provider:-${WW_PROVIDER:-}}")

    # Ollama local: allow empty / none / null as "no key needed"
    if [ "$provider" = "ollama" ]; then
        case "$key" in
            ""|none|null|placeholder|ollama|no-key|nokey)
                ww_upsert_env "WW_USE_OLLAMA" "1" "$env_file"
                export WW_USE_OLLAMA=1
                model=$(ww_default_model_for_provider "ollama")
                ww_upsert_env "WW_MODEL" "$model" "$env_file"
                export WW_MODEL="$model"
                echo "✓ Ollama local enabled (WW_USE_OLLAMA=1) in $env_file"
                echo "  Default model: $model  (override: WW_MODEL=...)"
                echo "  Optional: set OLLAMA_BASE_URL if not on 127.0.0.1:11434"
                return 0
                ;;
        esac
    fi

    if ! ww_key_value_ok "$key"; then
        echo "⚠️  Empty or placeholder key — not saved."
        echo "   Tip: for local Ollama use: ww key set none ollama"
        return 1
    fi

    inferred=$(ww_infer_provider_from_key "$key")
    if [ -n "$provider" ]; then
        # Chosen/forced provider: prefer clear key-shape inference when it conflicts
        if [ -n "$inferred" ] && [ "$inferred" != "$provider" ]; then
            echo "⚠️  Key shape looks like $inferred (not $provider) — saving as $inferred"
            provider="$inferred"
        fi
    else
        provider="$inferred"
    fi
    if [ -z "$provider" ]; then
        # Ambiguous sk-* (CLI one-liner without provider)
        if [ -t 0 ]; then
            provider=$(ww_ask_provider_tty)
        fi
    fi
    if [ -z "$provider" ]; then
        echo "⚠️  Ambiguous key prefix (sk-*). Specify a provider:"
        echo "   ww key set <key> deepseek|openai|anthropic|openrouter|gemini|xai|groq|ollama|…"
        echo "   Or set WW_PROVIDER=<id>  (see: ww key)"
        return 2
    fi

    env_var=$(ww_env_var_for_provider "$provider")
    if [ -z "$env_var" ]; then
        echo "⚠️  Unknown provider: $provider"
        echo "   Known: $WW_PROVIDER_IDS"
        return 1
    fi

    ww_upsert_env "$env_var" "$key" "$env_file"
    # Export for current process (safe for special chars in key)
    printf -v "$env_var" '%s' "$key"
    export "$env_var"

    if [ "$provider" = "ollama" ]; then
        ww_upsert_env "WW_USE_OLLAMA" "1" "$env_file"
        export WW_USE_OLLAMA=1
    fi

    # Set WW_MODEL so chat matches provider (ConfigManager ENV_PREFIX=WW_)
    model=$(ww_default_model_for_provider "$provider")
    if [ "$provider" != "deepseek" ]; then
        ww_upsert_env "WW_MODEL" "$model" "$env_file"
        export WW_MODEL="$model"
        echo "✓ Saved $env_var ($provider) in $env_file"
        echo "  Default model: $model  (override: WW_MODEL=...)"
    else
        # Leave existing WW_MODEL unless unset — default config is deepseek-v4-flash
        echo "✓ Saved $env_var ($provider) in $env_file"
    fi
    return 0
}

# Test primary configured key against its provider API.
ww_test_llm_key() {
    local env_file="${1:-}"
    local key_val provider url resp headers
    if ! ww_load_primary_key "$env_file"; then
        echo "⚠️  No key configured. Set one: ww key set <key> [provider]"
        return 1
    fi
    key_val="$WW_PRIMARY_KEY_VAL"
    provider="$WW_PRIMARY_PROVIDER"
    echo "🔍 Testing $provider API ($WW_PRIMARY_KEY_VAR)..."
    case "$provider" in
        anthropic)
            resp=$(curl -sS --connect-timeout 10 \
                -H "x-api-key: ${key_val}" \
                -H "anthropic-version: 2023-06-01" \
                "https://api.anthropic.com/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        openai)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.openai.com/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        openrouter)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://openrouter.ai/api/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        gemini)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://generativelanguage.googleapis.com/v1beta/openai/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        xai)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.x.ai/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        groq)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.groq.com/openai/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        mistral)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.mistral.ai/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        together)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.together.xyz/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        fireworks)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.fireworks.ai/inference/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        moonshot)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.moonshot.cn/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        deepinfra)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.deepinfra.com/v1/openai/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
        ollama|custom)
            echo "⚠️  $provider — local/custom endpoint; key/config is present (no fixed public test)."
            return 0
            ;;
        deepseek|*)
            resp=$(curl -sS --connect-timeout 10 \
                -H "Authorization: Bearer ${key_val}" \
                "https://api.deepseek.com/v1/models" 2>&1 || echo "NETWORK_ERROR")
            ;;
    esac
    if echo "$resp" | grep -qE '"id"|"data"'; then
        echo "✅ Key is valid — API reachable"
        return 0
    elif echo "$resp" | grep -q "NETWORK_ERROR"; then
        echo "❌ Network error — check internet connection"
        return 1
    else
        echo "❌ Key invalid or API error:"
        echo "$resp" | head -3
        return 1
    fi
}

# ── Subcommands ──
CMD="${1:-start}"
# upgrade is an alias for update
[ "$CMD" = "upgrade" ] && CMD=update
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
    "$VENV_DIR/bin/pip" install --quiet "python-dotenv>=1.0.0" 2>/dev/null || true
    if ! "$VENV_DIR/bin/python" -c "import dotenv, fastapi, uvicorn, pydantic; from core.config import ConfigManager" 2>/dev/null; then
        echo "   ✗ Core deps missing after update (dotenv/fastapi/…)"
        echo "     Fix: $VENV_DIR/bin/pip install -r requirements.txt"
        exit 1
    fi
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

    # WW_API_KEY (local HTTP auth) is distinct from LLM keys in .env.
    # If .env has no WW_API_KEY, pass the stable key from ~/.ww/api_key so
    # restart does not desync from the CLI (HTTP 401 after update).
    # Server also reads this file; export here for older code / env inheritance.
    _WW_KEY_FILE="${WW_CONFIG:-$HOME/.ww}/api_key"
    if ! echo " $ENV " | grep -qE ' WW_API_KEY=[^ ]'; then
        if [ -f "$_WW_KEY_FILE" ]; then
            _WW_KEY_VAL=$(tr -d '\r\n' < "$_WW_KEY_FILE" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            if [ -n "$_WW_KEY_VAL" ]; then
                ENV="$ENV WW_API_KEY=$_WW_KEY_VAL"
            fi
            unset _WW_KEY_VAL
        fi
    fi
    unset _WW_KEY_FILE

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
    KEY_PROVIDER="${4:-${WW_PROVIDER:-}}"

    case "$KEY_ACTION" in
        set)
            # No key arg: interactive on TTY (provider then key); usage on non-TTY
            if [ -z "$NEW_KEY" ]; then
                if [ -t 0 ]; then
                    if [ -z "$KEY_PROVIDER" ]; then
                        KEY_PROVIDER=$(ww_ask_provider_tty)
                    fi
                    KEY_PROVIDER=$(ww_normalize_provider "${KEY_PROVIDER:-}")
                    if [ -z "$KEY_PROVIDER" ]; then
                        echo "⚠️  No provider selected."
                        exit 1
                    fi
                    if [ "$KEY_PROVIDER" = "ollama" ]; then
                        echo "Paste API key (Enter = none for local Ollama):"
                        printf "  → "
                        read -r NEW_KEY || NEW_KEY=""
                        NEW_KEY=$(echo "$NEW_KEY" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
                        case "$(echo "$NEW_KEY" | tr '[:upper:]' '[:lower:]')" in
                            ""|none|null) NEW_KEY="none" ;;
                        esac
                    else
                        echo "Paste your API key:"
                        printf "  → "
                        read -r NEW_KEY || NEW_KEY=""
                        NEW_KEY=$(echo "$NEW_KEY" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
                        if [ -z "$NEW_KEY" ]; then
                            echo "⚠️  Empty key — not saved."
                            exit 1
                        fi
                    fi
                else
                    echo "⚠️  Usage: ww key set <key> [provider]"
                    echo "   Or run interactively (TTY): ww key set"
                    echo "   Providers: $WW_PROVIDER_IDS"
                    echo "   Local Ollama (no key): ww key set none ollama"
                    exit 1
                fi
            fi
            # Allow empty key when provider is ollama (handled in ww_save_llm_key)
            _norm_p=$(ww_normalize_provider "${KEY_PROVIDER:-}")
            if [ -z "$NEW_KEY" ] && [ "$_norm_p" != "ollama" ]; then
                echo "⚠️  Usage: ww key set <key> [provider]"
                echo "   Or run interactively (TTY): ww key set"
                echo "   Providers: $WW_PROVIDER_IDS"
                exit 1
            fi
            if [ -z "$NEW_KEY" ] && [ "$_norm_p" = "ollama" ]; then
                NEW_KEY="none"
            fi
            # Non-empty validation only for non-ollama (Anthropic/custom may not use sk-*)
            if [ "$_norm_p" != "ollama" ] && ! ww_key_value_ok "$NEW_KEY"; then
                # still pass through — ww_save_llm_key may map ollama via force provider later
                if [ -z "$KEY_PROVIDER" ]; then
                    echo "⚠️  Empty or placeholder key — not saved."
                    exit 1
                fi
            fi
            set +e
            ww_save_llm_key "$NEW_KEY" "$KEY_PROVIDER" "$ENV_FILE"
            _save_rc=$?
            set -e
            if [ "$_save_rc" -eq 0 ]; then
                echo "  Ready. Type: ww"
                exit 0
            fi
            exit "$_save_rc"
            ;;
        show)
            FOUND=0
            for _kv in $WW_LLM_KEY_VARS; do
                _val=""
                if [ -n "${!_kv:-}" ] && ww_key_value_ok "${!_kv}"; then
                    _val="${!_kv}"
                elif [ -f "$ENV_FILE" ]; then
                    _val=$(grep "^${_kv}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')
                fi
                if ww_key_value_ok "${_val:-}"; then
                    FOUND=1
                    _prov=$(ww_provider_for_env_var "$_kv")
                    MASKED="$(echo "$_val" | head -c 8)...$(echo "$_val" | tail -c 5)"
                    echo "🔑 ${_kv} (${_prov}): $MASKED"
                fi
            done
            # Ollama may be opted-in without a key
            _use_ollama="${WW_USE_OLLAMA:-}"
            if [ -z "$_use_ollama" ] && [ -f "$ENV_FILE" ]; then
                _use_ollama=$(grep "^WW_USE_OLLAMA=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')
            fi
            case "$(echo "${_use_ollama:-}" | tr '[:upper:]' '[:lower:]')" in
                1|true|yes|on)
                    FOUND=1
                    echo "🔑 Ollama local: WW_USE_OLLAMA=1 (no API key required)"
                    ;;
            esac
            if [ "$FOUND" -eq 0 ]; then
                echo "⚠️  No key configured."
                echo "   Set one: ww key set <key> [provider]"
                echo "   Or interactively (TTY): ww key set"
                echo "   Providers: $WW_PROVIDER_IDS"
            fi
            exit 0
            ;;
        test)
            ww_test_llm_key "$ENV_FILE"
            exit $?
            ;;
        *)
            echo "🌊 ww key — manage LLM API keys (multi-provider)"
            echo ""
            echo "  ww key set                    Interactive setup (TTY: provider, then key)"
            echo "  ww key set <key> [provider]   Save/update API key"
            echo "  ww key show                   Show configured keys (masked)"
            echo "  ww key test                   Test primary key against its provider API"
            echo ""
            echo "  Providers: $WW_PROVIDER_IDS"
            echo "  Key shape is auto-detected when possible (sk-ant-*, sk-or-*, sk-proj-*, gsk_*, AIza*)."
            echo "  Ambiguous sk-* keys need a provider: ww key set <key> openai"
            echo "  Local Ollama without key: ww key set none ollama"
            echo "  Bare ww key set on a terminal starts interactive setup."
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
# Brand cyan/blue #31D1F7 (public hero) — truecolor with basic CYAN fallback
if [ -n "${NO_COLOR:-}" ]; then
    BRAND=""
elif [ "${TERM:-}" = "dumb" ]; then
    BRAND="${BOLD}${CYAN}"
else
    BRAND=$'\033[1;38;2;49;209;247m'  # bold + #31D1F7
    # Some old terminals ignore truecolor; CYAN is still fine as soft fallback for other UI
fi
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

# ── Banner (WORLDWAVE art in brand cyan/blue; subtitle dim) ──
echo ""
printf '%b' "${BRAND:-${BOLD}${CYAN}}"
cat << 'BANNER'
   ██╗    ██╗ ██████╗ ██████╗ ██╗     ██████╗ ██╗    ██╗ █████╗ ██╗   ██╗███████╗
   ██║    ██║██╔═══██╗██╔══██╗██║     ██╔══██╗██║    ██║██╔══██╗██║   ██║██╔════╝
   ██║ █╗ ██║██║   ██║██████╔╝██║     ██║  ██║██║ █╗ ██║███████║██║   ██║█████╗  
   ██║███╗██║██║   ██║██╔══██╗██║     ██║  ██║██║███╗██║██╔══██║╚██╗ ██╔╝██╔══╝  
   ╚███╔███╔╝╚██████╔╝██║  ██║███████╗██████╔╝╚███╔███╔╝██║  ██║ ╚████╔╝ ███████╗
    ╚══╝╚══╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═╝  ╚═══╝  ╚══════╝
BANNER
printf '%b\n' "${NC}"
printf '%b\n' "${DIM}                                                                                   "
printf '%b\n' "                      Decentralized P2P Node — Every Node is a Tracker${NC}"

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
# Ensure critical packages are present (requirements.txt may lag pyproject)
"$VENV_DIR/bin/pip" install --quiet "python-dotenv>=1.0.0" requests 2>/dev/null || true
# Hard fail if core chat path cannot import (clean install regression guard)
if ! "$VENV_DIR/bin/python" -c "import dotenv, fastapi, uvicorn, pydantic; from core.config import ConfigManager" 2>/dev/null; then
    echo ""
    echo "ERROR: Core Python dependencies missing after install."
    echo "       Required: dotenv, fastapi, uvicorn, pydantic, core.config"
    echo "       Try: $VENV_DIR/bin/pip install -r requirements.txt"
    echo "            $VENV_DIR/bin/pip install 'python-dotenv>=1.0.0'"
    exit 1
fi
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

# Auto-detect any LLM API keys from environment and persist to .env
_WW_ENV_KEY_FOUND=0
for _kv in $WW_LLM_KEY_VARS; do
    _val="${!_kv:-}"
    if ww_key_value_ok "$_val"; then
        ww_upsert_env "$_kv" "$_val" "$ENV_FILE"
        ok "${_kv} detected from environment"
        _WW_ENV_KEY_FOUND=1
        # Non-deepseek shell keys: ensure a matching default model for chat
        _prov=$(ww_provider_for_env_var "$_kv")
        if [ "$_prov" != "deepseek" ] && [ -z "${WW_MODEL:-}" ]; then
            _model=$(ww_default_model_for_provider "$_prov")
            ww_upsert_env "WW_MODEL" "$_model" "$ENV_FILE"
            export WW_MODEL="$_model"
        fi
    fi
done
if [ "$_WW_ENV_KEY_FOUND" -eq 0 ] && [ -f "$ENV_FILE" ]; then
    for _kv in $WW_LLM_KEY_VARS; do
        if grep -qE "^${_kv}=.+" "$ENV_FILE" 2>/dev/null; then
            _val=$(grep "^${_kv}=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')
            if ww_key_value_ok "$_val"; then
                ok "${_kv} loaded from $ENV_FILE"
                _WW_ENV_KEY_FOUND=1
            fi
        fi
    done
fi
unset _kv _val _prov _model _WW_ENV_KEY_FOUND

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
# First-install TTY: ask provider FIRST, then paste key (avoids post-paste sk-* re-ask).
if ! ww_has_llm_key "$ENV_FILE"; then
    if [ -t 0 ]; then
        echo ""
        echo -e "  ${BOLD}🔑  LLM API key needed to chat${NC}"
        echo -e "  ${DIM}     DeepSeek · OpenAI · Anthropic · OpenRouter · Gemini · xAI · Groq · Ollama · …${NC}"
        echo ""
        # Prefer WW_PROVIDER if already set; otherwise always ask provider before paste
        USER_PROVIDER=$(ww_normalize_provider "${WW_PROVIDER:-}")
        if [ -z "$USER_PROVIDER" ]; then
            USER_PROVIDER=$(ww_ask_provider_tty)
        fi
        if [ -z "$USER_PROVIDER" ]; then
            echo ""
            warn "No provider selected — chat needs a key first"
            echo -e "  ${DIM}  Later: ww key set <key> [provider]  (see ww key)${NC}"
        elif [ "$USER_PROVIDER" = "ollama" ]; then
            echo ""
            echo -e "  ${BOLD}Ollama (local) — API key optional${NC}"
            echo -e "  ${DIM}  Press Enter for no key, or paste OLLAMA_API_KEY if set${NC}"
            printf "  ${CYAN}→ ${NC}"
            read -r USER_KEY || USER_KEY=""
            USER_KEY=$(echo "$USER_KEY" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            if [ -z "$USER_KEY" ]; then
                USER_KEY="none"
            fi
            echo ""
            if ww_save_llm_key "$USER_KEY" "ollama" "$ENV_FILE"; then
                ok "Ollama ready — change anytime: ww key set <key> [provider]"
            else
                warn "Ollama not configured — later: ww key set none ollama"
            fi
        else
            echo ""
            echo -e "  ${BOLD}Paste your API key:${NC}"
            printf "  ${CYAN}→ ${NC}"
            read -r USER_KEY || USER_KEY=""
            USER_KEY=$(echo "$USER_KEY" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            if [ -n "$USER_KEY" ]; then
                echo ""
                if ww_save_llm_key "$USER_KEY" "$USER_PROVIDER" "$ENV_FILE"; then
                    ok "Key saved — change anytime: ww key set <key> [provider]"
                else
                    # Invalid key: install still continues; chat may need key later
                    warn "Key not saved — set later: ww key set <key> [provider]"
                fi
            else
                echo ""
                warn "No key entered — chat needs a key first"
                echo -e "  ${DIM}  Later: ww key set <key> [provider]  (see ww key)${NC}"
            fi
        fi
    else
        # Non-TTY (curl | bash, CI): install done, do not hang on prompt or server
        echo ""
        warn "No LLM API key configured (non-interactive install)"
        echo "  Set a key, then chat:"
        echo "    ww key set <key> [provider]"
        echo "    ww"
        echo "  Providers: $WW_PROVIDER_IDS"
        echo "  Local Ollama: ww key set none ollama"
        echo ""
        ok "Install complete"
        exit 0
    fi
fi

# Offer messaging gateway (opt-in, after LLM key is present; TTY only for prompt)
if ww_has_llm_key "$ENV_FILE"; then
    if [ -t 0 ]; then
        if ww_has_telegram_token "$ENV_FILE"; then
            echo ""
            ok "Telegram gateway token already configured"
        else
            echo ""
            echo -e "  ${BOLD}🌐  Set up a messaging gateway now? (Telegram)${NC}"
            echo -e "  ${DIM}    Chat from your phone after install.${NC}"
            printf "  ${CYAN}[y/N] ${NC}"
            read -r GW_SETUP || GW_SETUP=""
            GW_SETUP=$(echo "$GW_SETUP" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            case "$GW_SETUP" in
                y|Y|yes|YES|Yes)
                    export PATH="$LOCAL_BIN:$PATH"
                    # Align CLI ↔ server on shared WW_API_KEY (~/.ww/api_key)
                    _WW_KEY_FILE="${WW_CONFIG:-$HOME/.ww}/api_key"
                    if [ -z "${WW_API_KEY:-}" ] && [ -f "$_WW_KEY_FILE" ]; then
                        WW_API_KEY=$(tr -d '\r\n' < "$_WW_KEY_FILE")
                        export WW_API_KEY
                    fi
                    unset _WW_KEY_FILE
                    cd "$INSTALL_DIR"
                    if [ -x "$LOCAL_BIN/ww" ]; then
                        "$LOCAL_BIN/ww" gateway setup || \
                            warn "Gateway setup exited — later: ww gateway setup"
                    elif [ -x "$VENV_DIR/bin/python" ] && [ -f "$INSTALL_DIR/ww_cli.py" ]; then
                        "$VENV_DIR/bin/python" "$INSTALL_DIR/ww_cli.py" gateway setup || \
                            warn "Gateway setup exited — later: ww gateway setup"
                    else
                        warn "ww CLI not found — later: ww gateway setup"
                    fi
                    ;;
                *)
                    echo -e "  ${DIM}Later: ww gateway setup${NC}"
                    ;;
            esac
        fi
    else
        # Non-TTY (curl|bash): do not prompt
        if ! ww_has_telegram_token "$ENV_FILE"; then
            echo -e "  ${DIM}Optional later: ww gateway setup${NC}"
        fi
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
