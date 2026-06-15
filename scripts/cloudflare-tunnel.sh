#!/bin/bash
# WW Blockchain Public Tunnel Wrapper
# 
# Uses cloudflared quick tunnel to expose P2P/tracker to the public internet
# Auto-detects trycloudflare URL and writes to ~/.ww/tracker_url
#
# Usage: run directly (stays running; add to crontab @reboot)
#   @reboot nohup $HOME/worldwave/scripts/cloudflare-tunnel.sh &

set -e
PIDFILE="$HOME/.ww/cloudflared.pid"
URLFILE="$HOME/.ww/tracker_url"
LOGFILE="$HOME/.ww/cloudflared.log"
INTERNAL_PORT="${1:-9833}"

mkdir -p "$HOME/.ww"

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOGFILE"; }

log "=== Starting cloudflared tunnel ==="
log "Internal: localhost:$INTERNAL_PORT"

# Cleanup old tunnel
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
fi

# Start tunnel, capture URL
CLOUDFLARED_LOG=$(mktemp)
cloudflared tunnel --url "http://localhost:$INTERNAL_PORT" > "$CLOUDFLARED_LOG" 2>&1 &
CF_PID=$!
echo "$CF_PID" > "$PIDFILE"
log "cloudflared PID: $CF_PID"

# Wait for URL (max 30 seconds)
URL=""
for i in $(seq 1 30); do
    URL=$(grep -oP 'https://[a-z-]+\.trycloudflare\.com' "$CLOUDFLARED_LOG" | head -1)
    if [ -n "$URL" ]; then
        break
    fi
    sleep 1
done

if [ -n "$URL" ]; then
    echo "$URL" > "$URLFILE"
    log "✅ Public URL: $URL"
else
    log "❌ Failed to detect tunnel URL"
fi

# Monitor loop: auto-restart if tunnel dies
while true; do
    if ! kill -0 "$CF_PID" 2>/dev/null; then
        log "⚠️  Tunnel died, restarting..."

        # Restart and re-detect URL
        CLOUDFLARED_LOG=$(mktemp)
        cloudflared tunnel --url "http://localhost:$INTERNAL_PORT" > "$CLOUDFLARED_LOG" 2>&1 &
        CF_PID=$!
        echo "$CF_PID" > "$PIDFILE"
        log "New cloudflared PID: $CF_PID"

        for i in $(seq 1 30); do
            URL=$(grep -oP 'https://[a-z-]+\.trycloudflare\.com' "$CLOUDFLARED_LOG" | head -1)
            if [ -n "$URL" ]; then
                echo "$URL" > "$URLFILE"
                log "✅ New URL: $URL"
                break
            fi
            sleep 1
        done
    fi
    sleep 10
done
