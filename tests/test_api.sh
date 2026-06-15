#!/bin/bash
# Worldwave API Integration Test
set -e

cd ~/worldwave
source .venv/bin/activate

# Kill any previous server
pkill -f "python3 server.py" 2>/dev/null || true
sleep 1

# Start server
python3 server.py > /tmp/ww_server.log 2>&1 &
SERVER_PID=$!
sleep 3

echo "========================================="
echo "  Worldwave API Integration Test"
echo "========================================="

# 1. Health Check
echo ""
echo "[1/5] Health Check"
curl -s http://localhost:9300/ww/health | python3 -m json.tool

# 2. Code-as-action
echo ""
echo "[2/5] Code-as-action"
curl -s -X POST http://localhost:9300/ww/code \
  -H "Content-Type: application/json" \
  -d '{"code":"import math; print(f\"Pi: {math.pi:.6f}\")"}' | python3 -m json.tool

# 3. Memory Snapshot
echo ""
echo "[3/5] Memory Snapshot"
curl -s -X POST http://localhost:9300/ww/memory \
  -H "Content-Type: application/json" \
  -d '{"action":"snapshot","limit":2}' | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for m in d:
        print(f'  {m.get(\"content\",\"\")[:60]}')
except:
    print(json.dumps(json.load(sys.stdin), indent=2))
"

# 4. Status Query
echo ""
echo "[4/5] Status Query"
curl -s http://localhost:9300/ww/status | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'Session: {d[\"session\"][\"session_id\"][:12]}...')
print(f'Spiral: {d[\"session\"][\"current_spiral\"]}')
print(f'Memory v2: {\"✅\" if d[\"memory\"][\"available\"] else \"❌\"}')"

# 5. Task Execution
echo ""
echo "[5/5] Task Execution"
curl -s -X POST http://localhost:9300/ww/run \
  -H "Content-Type: application/json" \
  -d '{"goal":"quick test","max_spirals":1}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'Status: {d[\"status\"]}')
print(f'Spirals completed: {d[\"spirals_completed\"]}')
"

# Cleanup
kill $SERVER_PID 2>/dev/null
echo ""
echo "========================================="
echo "  ✅ WW API Test Complete"
echo "========================================="
