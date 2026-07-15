#!/usr/bin/env bash
# ============================================================
#  Worldwave partner smoke checklist
# ============================================================
#  Failure = deploy/product bug, not "user error".
#
#  Default partner path (only path we document):
#    bash <(curl -fsSL https://raw.githubusercontent.com/Clean-Dust/worldwave/main/deploy.sh)
#    # if prompted / missing key:  ww key set sk-xxx
#    ww
#
#  Optional owner Telegram continuity (single-user install):
#    WW_OWNER_TELEGRAM_ID=<your_telegram_user_id>
#    WW_SINGLE_USER=1   # default on
#
#  Run on a live node (after install):
#    bash scripts/partner_smoke.sh
# ============================================================
set -euo pipefail

INSTALL_DIR="${WW_HOME:-$HOME/worldwave}"
cd "$INSTALL_DIR" 2>/dev/null || {
  echo "FAIL: install dir not found: $INSTALL_DIR"
  exit 1
}

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PORT="${WW_PORT:-9300}"
KEY="${WW_API_KEY:-}"
BASE="http://127.0.0.1:${PORT}"
FAIL=0

pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAIL=1; }

echo "Worldwave partner smoke — $INSTALL_DIR (port $PORT)"
echo

# 1) Health
if curl -sS -m 5 "$BASE/ww/health" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
  pass "health ok"
else
  fail "health not ok — is the server running? (ww / deploy.sh)"
fi

# 2) Auth present for API checks
if [ -z "$KEY" ]; then
  fail "WW_API_KEY missing in env/.env — install should create one"
else
  pass "WW_API_KEY present"
fi

# 3) Clean pong via /ww/run (content + no internal leak)
if [ -n "$KEY" ]; then
  RESP=$(.venv/bin/python - <<'PY2' || true
import json, os, urllib.request
port = os.environ.get("WW_PORT", "9300")
key = os.environ.get("WW_API_KEY", "")
body = {"goal": "Reply with exactly the single word pong and nothing else.", "max_spirals": 2}
req = urllib.request.Request(
    "http://127.0.0.1:%s/ww/run" % port,
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "X-API-Key": key},
    method="POST",
)
try:
    print(urllib.request.urlopen(req, timeout=90).read().decode())
except Exception as e:
    print(json.dumps({"response": "", "error": str(e)}))
PY2
)
  BODY=$(printf '%s' "$RESP" | python3 -c 'import sys,json
try:
 d=json.load(sys.stdin); print(d.get("response") or "")
except Exception:
 print("")' 2>/dev/null || true)
  LOW=$(printf '%s' "$BODY" | tr '[:upper:]' '[:lower:]')
  if printf '%s' "$LOW" | grep -q 'reflex arc'; then
    fail "user response leaked Reflex arc: ${BODY:0:80}"
  elif printf '%s' "$LOW" | grep -q 'pong'; then
    pass "pong response clean (content)"
  else
    fail "expected pong in response, got: ${BODY:0:120}"
  fi
fi

# 4) Identity: owner surfaces share; stranger does not (single-user)
export WW_SINGLE_USER="${WW_SINGLE_USER:-1}"
if [ -n "${WW_OWNER_TELEGRAM_ID:-}" ]; then
  export WW_OWNER_TELEGRAM_ID
fi
ID_OUT=$(.venv/bin/python - <<'PY3' 2>/dev/null || true
import os
from wavegate.identity import IdentityResolver
r = IdentityResolver()
http = r.resolve("http", "default")
term = r.resolve("terminal", "default")
owner_tg = os.environ.get("WW_OWNER_TELEGRAM_ID", "").strip()
if owner_tg:
    tg = r.resolve("telegram", owner_tg, owner_tg)
else:
    tg = http
guest = r.resolve("telegram", "999888777", "999888777", display_name="SmokeGuest")
print("http", http)
print("term", term)
print("tg", tg)
print("guest", guest)
print("owner_same", http == term == tg)
print("guest_diff", guest != http)
PY3
)
if [ -z "$ID_OUT" ]; then
  fail "identity resolve failed (venv/python?)"
else
  echo "$ID_OUT" | sed 's/^/         /'
  echo "$ID_OUT" | grep -q 'owner_same True' && pass "owner surfaces same entity" || fail "owner surfaces not same entity"
  echo "$ID_OUT" | grep -q 'guest_diff True' && pass "stranger telegram != owner entity" || fail "stranger merged into owner"
fi

# 5) Optional systemd shape
if systemctl --user is-active ww.service >/dev/null 2>&1; then
  pass "ww.service active (optional production shape)"
else
  echo "  INFO  ww.service not active (ok for bare deploy.sh nodes)"
fi

echo
if [ "$FAIL" -ne 0 ]; then
  echo "RESULT: FAIL — treat as product/deploy bug"
  exit 1
fi
echo "RESULT: PASS — default partner path healthy"
exit 0
