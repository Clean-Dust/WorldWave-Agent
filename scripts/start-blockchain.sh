#!/bin/bash
# Blockchain Node & Gateway — Auto-start script
# Add to crontab: @reboot $HOME/worldwave/scripts/start-blockchain.sh

set -e

LOG_DIR="$HOME/worldwave/data/subconscious/blockchain"
mkdir -p "$LOG_DIR"

# Start blockchain node (P2P + mining)
if ! pgrep -f 'blockchain_node.py' > /dev/null 2>&1; then
    cd "$HOME/worldwave"
    nohup python3 scripts/blockchain_node.py >> "$LOG_DIR/node.log" 2>&1 &
    echo "blockchain_node started (PID $!)"
fi

sleep 3

# Start HTTP Gateway (wallet + API)
if ! lsof -ti:8080 > /dev/null 2>&1; then
    cd "$HOME/worldwave"
    nohup python3 scripts/bootstrap_server.py \
        --blockchain "$LOG_DIR/blockchain.json" \
        --mempool "$LOG_DIR/mempool.json" \
        >> "$LOG_DIR/gateway.log" 2>&1 &
    echo "gateway started (PID $!)"
fi

echo "Blockchain services running."
echo "Bootstrap tracker URL from BOOTSTRAP_URLS env var, or default to embedded tracker."
