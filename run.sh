#!/bin/bash
# Worldwave — One-click blockchain node starter
# Clone → bash run.sh → connected to the WW P2P network
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$DIR/data/subconscious/blockchain"
mkdir -p "$DATA_DIR"

echo "🌍 Worldwave Blockchain Node"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Tracker: set BOOTSTRAP_TRACKER_URL env var to display tracker info"
echo ""

# Check Python
python3 --version 2>/dev/null || { echo "❌ Python 3 required"; exit 1; }

# Start node
cd "$DIR"
exec python3 scripts/blockchain_node.py 2>&1
