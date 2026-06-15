#!/bin/bash
# Worldwave startup script v0.5 — with crash recovery
set -e

# Switch to project root
cd "$(dirname "$0")/.."
WW_ROOT="$(pwd)"

# Load secrets (no hardcoded keys in scripts)
ENV_FILE="$WW_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
else
    echo "WARNING: $ENV_FILE not found — copy .env.example to .env"
fi

export WW_PORT="${WW_PORT:-9300}"
export WW_MEMORY_URL="${WW_MEMORY_URL:-http://localhost:9200}"

# Crash recovery check (non-blocking)
cd "$WW_ROOT"
PYTHONPATH="$WW_ROOT" python3 -c "
from core.persistence import SessionPersistence
sp = SessionPersistence()
rec = sp.recovery_check()
if rec['needs_recovery']:
    print(chr(0x1f504) + ' Recovery: ' + rec.get('message',''))
" 2>/dev/null || true

exec /usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port $WW_PORT --log-level info --timeout-keep-alive 300
