#!/usr/bin/env bash
# Run all automated WW memory verifies on a live node.
# Usage (Banana):
#   set -a; source ~/worldwave/.env; set +a
#   export WW_PROVE_URL=http://127.0.0.1:${WW_PORT:-9302}
#   export WW_PROVE_SKIP_L0=1
#   export WW_PROVE_ALLOW_RESTART=1   # optional; restarts ww.service
#   export WW_OWNER_TELEGRAM_ID=...  # for --telegram
#   bash scripts/memory_auto_all.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
if [ ! -x "$PY" ]; then PY=python3; fi

export WW_PROVE_URL="${WW_PROVE_URL:-http://127.0.0.1:${WW_PORT:-9300}}"
if [ -z "${WW_API_KEY:-}" ] && [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

echo "== mechanism =="
"$PY" scripts/memory_prove.py --mechanism

echo "== product =="
"$PY" scripts/memory_prove.py --product

echo "== narrative =="
"$PY" scripts/memory_prove.py --narrative

if [ -n "${WW_OWNER_TELEGRAM_ID:-}" ]; then
  echo "== telegram =="
  "$PY" scripts/memory_prove.py --telegram
else
  echo "== telegram SKIP (no WW_OWNER_TELEGRAM_ID) =="
fi

if [ "${WW_PROVE_ALLOW_RESTART:-0}" = "1" ]; then
  echo "== restart =="
  "$PY" scripts/memory_prove.py --restart
else
  echo "== restart SKIP (set WW_PROVE_ALLOW_RESTART=1) =="
fi

echo "== soak tick =="
"$PY" scripts/memory_soak.py || true

echo "ALL AUTOMATED SUITES FINISHED"
