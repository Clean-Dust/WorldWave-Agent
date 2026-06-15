#!/usr/bin/env python3
"""Blockchain Node — dedicated hardware blockchain node.

Start sequence:
  1. Load/initialize blockchain (PoW + subconscious payload)
  2. Start global P2P network (public mode)
  3. Start background mining
  4. Bootstrap gateway to collect pending txs from mempool

Environment variables:
  MINER_ID       — Miner node ID (default: node-<hostname>)
  BLOCKCHAIN_DIR — Blockchain data directory
  P2P_PORT       — P2P listening port (default 9833)
  BOOTSTRAP_URL  — Bootstrap tracker URL
"""

import json
import logging
import os
import sys
import time
import glob

# settinglog
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ww.node")

# Add project root directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)  # ~/worldwave/
sys.path.insert(0, PROJECT_DIR)

# ── setting ──

MINER_ID = os.environ.get("MINER_ID", f"node-{os.uname().nodename}")
BLOCKCHAIN_DIR = os.environ.get("BLOCKCHAIN_DIR",
    os.path.expanduser("~/worldwave/data/subconscious/blockchain"))
P2P_PORT = int(os.environ.get("P2P_PORT", 9833))
BOOTSTRAP_URLS = os.environ.get("BOOTSTRAP_URLS", "").split(",") if os.environ.get("BOOTSTRAP_URLS") else []

# Try from cloudflared tunnel URL fileread
_tracker_url_file = os.path.expanduser("~/.ww/tracker_url")
if not BOOTSTRAP_URLS and os.path.isfile(_tracker_url_file):
    try:
        with open(_tracker_url_file) as f:
            url = f.read().strip()
            if url:
                BOOTSTRAP_URLS = [url]
    except Exception:
        pass
PENDING_TX_DIR = os.path.join(BLOCKCHAIN_DIR, "pending_txs")


def main():
    logger.info("=" * 50)
    logger.info(f"🌍 WW Blockchain Node")
    logger.info(f"   Miner: {MINER_ID}")
    logger.info(f"   Data:  {BLOCKCHAIN_DIR}")
    logger.info(f"   P2P:   :{P2P_PORT}")
    logger.info(f"   Bootstrap: {BOOTSTRAP_URLS or '(self-hosted tracker)'}")
    logger.info("=" * 50)

    # ── Import blockchain ──
    from core.subconscious.blockchain import Blockchain, Transaction
    from core.subconscious.network import GlobalP2PNetwork

    # Ensure directory exists at 
    os.makedirs(BLOCKCHAIN_DIR, exist_ok=True)
    os.makedirs(PENDING_TX_DIR, exist_ok=True)

    # initializeblockchain
    bc = Blockchain(data_dir=BLOCKCHAIN_DIR, mining_enabled=False)
    logger.info(f"📚 Blockchain loaded: height={bc.height}, blocks={len(bc.chain)}")

    # Give miner some start balance
    if bc.get_balance(MINER_ID) == 0 and bc.height < 1:
        bc.balances[MINER_ID] = 100000
        bc._save()
        logger.info(f"💰 Miner {MINER_ID}: initial balance 100000 WW Credits")

    # ── start P2P network ──
    # Check if port is available
    import socket
    can_public = True
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("0.0.0.0", P2P_PORT))
        s.close()
    except OSError:
        can_public = False

    p2p = GlobalP2PNetwork(
        node_id=MINER_ID,
        listen_port=P2P_PORT,
        version="ww-blockchain-node-v0.7",
        public_mode=can_public,
        bootstrap_urls=BOOTSTRAP_URLS if BOOTSTRAP_URLS else None,
    )

    # Connect blockchain callbacks
    p2p.set_blockchain_callbacks(
        get_blocks=lambda f, c: [b.to_dict() for b in bc.chain[f:f+c]],
        get_height=lambda: bc.height,
        get_latest_hash=lambda: bc.latest_hash,
        get_mempool=lambda: [tx.to_dict() for tx in bc.mempool],
        mempool_count=lambda: len(bc.mempool),
        receive_block=lambda bd: bc_receive_block(bc, bd),
        receive_tx=lambda td: bc_receive_tx(bc, td),
    )

    # Mine to new block → P2P broadcast
    bc._on_block_callback = lambda b: p2p.broadcast_block(b.to_dict())

    p2p.start()
    logger.info(f"🌐 P2P started: mode={'public' if can_public else 'private'} port={P2P_PORT}")

    # ── startbackgroundmining ──
    bc.start_mining(MINER_ID)
    logger.info(f"⛏️  Mining started")

    # ── Main loop: monitor + import pending txs ──
    logger.info("🔄 Monitoring... (Ctrl+C to stop)")
    last_stats_time = time.time()

    try:
        while True:
            time.sleep(5)

            # Scan pending_txs directory
            for tx_file in glob.glob(os.path.join(PENDING_TX_DIR, "tx_*.json")):
                try:
                    with open(tx_file) as f:
                        tx_data = json.load(f)
                    # from JSON create Transaction (gateway commit original JSON)
                    td = tx_data.get("data", {})
                    fv = td.get("feature_vector", [0.0]*12)
                    tools = td.get("tools_sequence", [])
                    reward = float(td.get("reward", 0.5))
                    sender = tx_data.get("sender", "anon")

                    tx = Transaction.experience(sender, fv, tools, reward)
                    # Ensure sender has initial balance
                    if bc.get_balance(sender) < tx.fee:
                        bc.balances[sender] = bc.balances.get(sender, 0) + 10000
                    if bc.add_transaction(tx):
                        logger.info(f"📥 Imported pending tx: {tx.type} {tx.hash()[:16]}")
                    os.remove(tx_file)
                except Exception as e:
                    logger.debug(f"Pending tx error: {e}")

            # Print state every 30 seconds
            if time.time() - last_stats_time >= 30:
                stats = bc.stats()
                logger.info(
                    f"📊 height={stats['height']} "
                    f"mempool={stats['mempool']['count']} "
                    f"exps={stats['total_experiences']} "
                    f"updates={stats['total_model_updates']} "
                    f"hashrate={stats['hashrate']} "
                    f"peers={p2p.peer_count()}"
                )
                last_stats_time = time.time()

    except KeyboardInterrupt:
        logger.info("Shutting down...")
        bc.stop_mining()
        p2p.stop()
        logger.info("Node stopped.")


def bc_receive_block(bc, block_data: dict) -> bool:
    """from P2P receive to new block."""
    from core.subconscious.blockchain import Block
    try:
        block = Block.from_dict(block_data)
        return bc.add_block(block, broadcast=False)
    except Exception as e:
        logger.debug(f"Receive block error: {e}")
        return False


def bc_receive_tx(bc, tx_data: dict) -> bool:
    """from P2P receive to new transaction."""
    from core.subconscious.blockchain import Transaction
    try:
        tx = Transaction.from_dict(tx_data)
        return bc.add_transaction(tx)
    except Exception as e:
        logger.debug(f"Receive tx error: {e}")
        return False


if __name__ == "__main__":
    main()
