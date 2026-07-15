#!/usr/bin/env bash
# WW Memory prove wrapper — exit 0 only on full pass of enabled stages.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
exec "$PY" scripts/memory_prove.py "$@"
