#!/bin/bash
# Quick-start: sets up env, starts WW, runs tests, cleans up
# Usage: bash tests/quick_test.sh [api|telegram|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WW_DIR="$(dirname "$SCRIPT_DIR")"

# Kill any existing WW on test port
kill $(lsof -ti :9302) 2>/dev/null || true
sleep 1

# Clean persistent state
rm -f ~/.ww/scheduler.json ~/.ww/scheduler.db ~/.ww/pairing.json 2>/dev/null || true

# Start WW
echo "🚀 Starting WW on port 9302..."
cd "$WW_DIR"

# Use .env if exists, else set manually
export WW_PORT=9302
export WW_API_KEY="${WW_API_KEY}"
export WW_SKIP_AUTO_EVOLUTION=true
export WW_PAIRING_AUTO_APPROVE=true

# Load Telegram token from .env if available
if [ -f .env ]; then
    set -a; source .env; set +a
fi

python3 server.py &
WW_PID=$!
echo "   PID: $WW_PID"

# Wait for server
for i in $(seq 1 30); do
    if curl -s -o /dev/null "http://localhost:9302/health" 2>/dev/null; then
        echo "   ✅ Server ready"
        break
    fi
    sleep 1
done

# Run tests
bash "$SCRIPT_DIR/e2e_harness.sh" "${1:-all}"
TEST_EXIT=$?

# Cleanup
kill $WW_PID 2>/dev/null || true
wait $WW_PID 2>/dev/null || true

exit $TEST_EXIT
