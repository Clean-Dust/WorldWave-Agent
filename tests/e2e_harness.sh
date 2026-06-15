#!/bin/bash
# WW End-to-End Test Harness
# No user involvement needed — sends messages, checks responses, reports results.
# Usage: bash tests/e2e_harness.sh [telegram|api|all]

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WW_PORT="${WW_PORT:-9302}"
WW_API_KEY="${WW_API_KEY:-}"
WW_BASE="http://localhost:${WW_PORT}"
TG_BOT_TOKEN="${TELEGRAM_WW_TOKEN}"
TG_CHAT_ID="${TELEGRAM_WW_WORKSPACE}"
TG_API="https://api.telegram.org/bot${TG_BOT_TOKEN}"

pass() { echo -e "  ${GREEN}✅${NC} $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}❌${NC} $1 — $2"; FAIL=$((FAIL+1)); }

# ── sanity ──
check_server() {
    curl -s -o /dev/null -w "%{http_code}" "${WW_BASE}/health" 2>/dev/null || echo "000"
}

wait_for_server() {
    for i in $(seq 1 30); do
        code=$(check_server)
        if [ "$code" = "200" ]; then
            echo "  Server ready (port $WW_PORT)"
            return 0
        fi
        sleep 1
    done
    echo -e "  ${RED}Server did not start${NC}"
    return 1
}

# ── API tests ──
test_api() {
    echo ""
    echo "━━━ API Tests ━━━"

    # health
    code=$(check_server)
    if [ "$code" = "200" ]; then pass "GET /health ($code)"; else fail "GET /health" "got $code"; fi

    # status
    resp=$(curl -s "${WW_BASE}/status" -H "x-api-key: ${WW_API_KEY}")
    if echo "$resp" | grep -q '"running"'; then pass "GET /status"; else fail "GET /status" "$resp"; fi

    # tools list
    resp=$(curl -s "${WW_BASE}/tools" -H "x-api-key: ${WW_API_KEY}")
    count=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('tools',d)))" 2>/dev/null)
    if [ -n "$count" ] && [ "$count" -gt 10 ]; then pass "GET /tools ($count tools)"; else fail "GET /tools" "count=$count"; fi

    # simple chat
    resp=$(curl -s -X POST "${WW_BASE}/chat" \
        -H "Content-Type: application/json" \
        -H "x-api-key: ${WW_API_KEY}" \
        -d '{"message":"hello, say exactly: E2E_OK","max_spirals":1}' 2>/dev/null)
    if echo "$resp" | grep -q "E2E_OK"; then
        pass "POST /chat (E2E_OK response)"
    else
        fail "POST /chat" "response: $(echo $resp | head -c 200)"
    fi

    # run task
    resp=$(curl -s -X POST "${WW_BASE}/run" \
        -H "Content-Type: application/json" \
        -H "x-api-key: ${WW_API_KEY}" \
        -d '{"goal":"reply with one word: PASS","max_spirals":1}' 2>/dev/null)
    if echo "$resp" | grep -qi "PASS"; then
        pass "POST /run"
    else
        fail "POST /run" "response: $(echo $resp | head -c 200)"
    fi
}

# ── Telegram integration tests ──
test_telegram() {
    echo ""
    echo "━━━ Telegram Integration Tests ━━━"

    # Verify bot token works
    resp=$(curl -s "${TG_API}/getMe")
    if echo "$resp" | grep -q '"ok":true'; then
        bot_name=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])" 2>/dev/null)
        pass "Bot getMe ($bot_name)"
    else
        fail "Bot getMe" "$resp"
        return
    fi

    # Send test message to bot (simulating user DM)
    # Use a unique marker so we can track the response
    MARKER="E2E_$(date +%s)"
    send_resp=$(curl -s "${TG_API}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\": ${TG_CHAT_ID}, \"text\": \"${MARKER}: say exactly PONG\"}")
    
    msg_id=$(echo "$send_resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['message_id'])" 2>/dev/null)
    if [ -n "$msg_id" ] && [ "$msg_id" != "None" ]; then
        pass "Send test message (msg_id=$msg_id, marker=$MARKER)"
    else
        fail "Send test message" "$send_resp"
        return
    fi

    # Wait for WW to process and respond
    echo "  ⏳ Waiting for WW to respond (polling up to 30s)..."
    found=0
    for i in $(seq 1 30); do
        sleep 2
        # Check recent messages via getUpdates (offset to look for recent ones)
        updates=$(curl -s "${TG_API}/getUpdates?offset=-5&timeout=2")
        if echo "$updates" | grep -qi "PONG"; then
            found=1
            break
        fi
        if echo "$updates" | grep -qi "$MARKER"; then
            # Message was seen by bot, check if bot replied
            if echo "$updates" | grep -qi "PONG\|pong"; then
                found=1
                break
            fi
        fi
    done

    if [ "$found" = "1" ]; then
        pass "WW Telegram response (PONG received)"
    else
        # Check WW server logs for the marker
        ww_logs=$(curl -s "${WW_BASE}/telegram/status" -H "x-api-key: ${WW_API_KEY}" 2>/dev/null)
        fail "WW Telegram response" "no PONG in 30s. WW status: $ww_logs"
    fi
}

# ── main ──
MODE="${1:-all}"

echo "🌊 WW E2E Test Harness"
echo "   Target: ${WW_BASE}"
echo "   Mode: ${MODE}"
echo ""

case "$MODE" in
    api)
        test_api
        ;;
    telegram)
        wait_for_server || exit 1
        test_telegram
        ;;
    all)
        wait_for_server || exit 1
        test_api
        test_telegram
        ;;
    *)
        echo "Usage: $0 [api|telegram|all]"
        exit 1
        ;;
esac

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "Result: ${GREEN}${PASS} passed${NC} / ${RED}${FAIL} failed${NC} / total $((PASS+FAIL))"
echo ""

exit $FAIL
