"""
Global Bootstrap Tracker + Blockchain Gateway

Dual role:
  1. Bootstrap Tracker — P2P seednode registration/discoveryservice
  2. Blockchain Gateway — as browserwalletprovideblockchainquery API (attach  CORS) 

deploy to any publicly reachable HTTP server:
  nohup python3 bootstrap_server.py --blockchain /path/to/blockhain.json &

New user onboarding flow:
  1. POST /p2p/register  → registeroneself  peer info
  2. GET  /p2p/peers     → getactivenodelist
  3. GET  /api/status    → blockchainstate
  4. POST /api/transaction  → committransaction
"""

import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List

# ── setting ──

PORT = int(os.environ.get("PORT", 8080))
PEER_TIMEOUT = 3600 * 2
CLEANUP_INTERVAL = 300
MAX_PEERS = 1000

# Selective blockchain data file path
BLOCKCHAIN_FILE = os.environ.get("BLOCKCHAIN_FILE", "")
MEMPOOL_FILE = os.environ.get("MEMPOOL_FILE", "")

# ── memorysave ──

peers: Dict[str, Dict[str, Any]] = {}

# Cache blockchain data (periodically reload)
_cached_chain: Dict[str, Any] = {}
_cache_time = 0.0
_CACHE_TTL = 5  # seconds


def _load_blockchain() -> Dict[str, Any]:
    """Load blockchain data from file (no dependency on blockchain.py)."""
    global _cached_chain, _cache_time
    now = time.time()
    if not BLOCKCHAIN_FILE:
        return _cached_chain
    if now - _cache_time < _CACHE_TTL and _cached_chain:
        return _cached_chain

    path = os.path.expanduser(BLOCKCHAIN_FILE)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                _cached_chain = json.load(f)
            _cache_time = now
        except Exception:
            pass
    return _cached_chain


def _load_mempool() -> List[dict]:
    """from fileload mempool. """
    if not MEMPOOL_FILE:
        return []
    path = os.path.expanduser(MEMPOOL_FILE)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return []


# ── Bootstrap Tracker ──


def cleanup_dead_peers():
    now = time.time()
    dead = [nid for nid, p in peers.items() if now - p.get("last_seen", 0) > PEER_TIMEOUT]
    for nid in dead:
        del peers[nid]
    if dead:
        print(f"🧹 Cleaned {len(dead)} stale peers (active: {len(peers)})")


def get_active_peers() -> list:
    now = time.time()
    active = [p for p in peers.values() if now - p.get("last_seen", 0) < PEER_TIMEOUT]

    def score(p):
        s = 0
        if p.get("public"):
            s += 10
        s += min(p.get("height", 0) / 1000, 5)
        return s

    active.sort(key=score, reverse=True)
    return active[:100]


def register_peer(data: dict) -> dict:
    node_id = data.get("node_id", "")
    address = data.get("address", "")
    port = int(data.get("port", 9833))
    if not node_id:
        return {"error": "node_id required"}

    # Deduplicate by (address, port): reinstalls from same machine
    for existing_id, existing in list(peers.items()):
        if existing.get("address") == address and existing.get("port") == port:
            if existing_id != node_id:
                del peers[existing_id]
            break

    peers[node_id] = {
        "node_id": node_id,
        "address": address,
        "port": port,
        "version": data.get("version", ""),
        "public": bool(data.get("public", False)),
        "height": int(data.get("height", 0)),
        "last_seen": time.time(),
        "first_seen": time.time() if node_id not in peers else peers[node_id].get("first_seen", time.time()),
    }
    return {"status": "ok", "peers_count": len(peers)}


# ── HTTP Handler ──


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet mode

    def _json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-auth-token")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-auth-token")
        self.end_headers()

    def do_GET(self):
        path = self.path.rstrip("/")

        # ── Bootstrap Tracker endpoint ──
        if path == "/p2p/peers":
            return self._json({
                "peers": get_active_peers(),
                "count": len(peers),
                "tracker_version": "ww-bootstrap-v1",
            })

        if path == "/p2p/stats":
            now = time.time()
            active = sum(1 for p in peers.values() if now - p.get("last_seen", 0) < PEER_TIMEOUT)
            return self._json({
                "registered": len(peers), "active": active,
                "uptime_seconds": int(time.time() - self.server.start_time),
            })

        if path == "/health":
            return self._json({"status": "healthy", "peers": len(peers)})

        # ── Blockchain Gateway endpoint (read from file, zero dependencies) ──
        chain = _load_blockchain()

        if path == "/api/status":
            blocks = chain.get("blocks", [])
            latest = blocks[-1] if blocks else {}
            latest_header = latest.get("header", {}) if latest else {}
            experience_count = sum(
                1 for b in blocks
                for tx in b.get("transactions", [])
                if tx.get("type") == "subconscious_experience"
            )
            model_update_count = sum(
                1 for b in blocks
                for tx in b.get("transactions", [])
                if tx.get("type") == "model_update"
            )
            return self._json({
                "height": len(blocks) - 1 if blocks else -1,
                "blocks": len(blocks),
                "latest_hash": latest.get("hash", "none")[:16] if latest else "none",
                "latest_header": latest_header,
                "total_experiences": experience_count,
                "total_model_updates": model_update_count,
                "mempool": len(_load_mempool()),
            })

        if path.startswith("/api/blocks"):
            params = {}
            if "?" in self.path:
                for pair in self.path.split("?")[1].split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        params[k] = v
            from_h = int(params.get("from", 0))
            limit = int(params.get("limit", 20))
            blocks = chain.get("blocks", [])
            sliced = blocks[from_h:from_h + limit]
            return self._json({"blocks": sliced, "from": from_h, "count": len(sliced), "total": len(blocks)})

        if path == "/api/block" or path.startswith("/api/block/"):
            # GET /api/block or /api/block/<hash/height>
            blocks = chain.get("blocks", [])
            target = self.path.split("/api/block/")[-1] if "/api/block/" in self.path else ""
            if not target or target == "latest":
                b = blocks[-1] if blocks else None
            elif target.isdigit():
                idx = int(target)
                b = blocks[idx] if 0 <= idx < len(blocks) else None
            else:
                b = next((b2 for b2 in blocks if b2.get("hash", "").startswith(target)), None)
            return self._json({"block": b} if b else {"error": "not found"}, 200 if b else 404)

        if path == "/api/experiences":
            params = {}
            if "?" in self.path:
                for pair in self.path.split("?")[1].split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        params[k] = v
            limit = int(params.get("limit", 50))
            blocks = chain.get("blocks", [])
            exps = []
            for bi, b in enumerate(blocks):
                for tx in b.get("transactions", []):
                    if tx.get("type") == "subconscious_experience":
                        exp_data = tx.get("data", {})
                        exp_data["block_height"] = bi
                        exp_data["block_hash"] = b.get("hash", "")[:16]
                        exp_data["tx_hash"] = tx.get("hash", "")[:16] if "hash" in tx else ""
                        exps.append(exp_data)
                        if len(exps) >= limit:
                            break
                if len(exps) >= limit:
                    break
            return self._json({"experiences": exps, "count": len(exps)})

        # Homepage
        if path in ("", "/"):
            # Provide browser wallet
            wallet_path = os.path.expanduser("~/worldwave/scripts/blockchain-wallet.html")
            if os.path.isfile(wallet_path):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(wallet_path) as f:
                    self.wfile.write(f.read().encode())
                return
            return self._json({
                "name": "WW Blockchain Gateway",
                "version": "1.0",
                "endpoints": {
                    "GET /p2p/peers": "Active peer list",
                    "GET /p2p/stats": "Tracker stats",
                    "POST /p2p/register": "Register your node",
                    "GET /api/status": "Blockchain status",
                    "GET /api/blocks": "Block list (?from=0&limit=20)",
                    "GET /api/experiences": "Subconscious experiences (?limit=50)",
                    "POST /api/transaction": "Submit transaction (JSON body)",
                    "GET /api/block/<height>": "Single block by height",
                    "GET /health": "Health check",
                },
            })

        return self._json({"error": "not_found"}, 404)

    def do_POST(self):
        path = self.path.rstrip("/")

        # P2P Register
        if path == "/p2p/register":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return self._json({"error": "invalid_json"}, 400)
            result = register_peer(data)
            return self._json(result)

        # Submit Transaction (forward to local node or store in mempool)
        if path == "/api/transaction":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return self._json({"error": "invalid_json"}, 400)

            # writetransaction temporary file (periodically scanned and committed by real node)
            tx_dir = os.path.expanduser("~/worldwave/data/subconscious/blockchain/pending_txs")
            os.makedirs(tx_dir, exist_ok=True)
            tx_file = os.path.join(tx_dir, f"tx_{int(time.time() * 1000)}_{data.get('sender', 'anon')[:8]}.json")
            with open(tx_file, "w") as f:
                json.dump(data, f)
            tx_hash = data.get("signature", "")[:16] or "pending"
            return self._json({"accepted": True, "hash": tx_hash, "note": "tx queued for mining"})

        return self._json({"error": "not_found"}, 404)


# ── start ──


def run_server(host="0.0.0.0", port=PORT):
    server = HTTPServer((host, port), GatewayHandler)
    server.start_time = time.time()
    print("🌍 WW Bootstrap Tracker + Blockchain Gateway")
    print(f"   Listen: http://0.0.0.0:{port}")
    print(f"   Blockchain file: {BLOCKCHAIN_FILE or '(none — peer-only mode)'}")
    print("   CORS: enabled (all origins)")
    print()

    import threading

    def cleanup_loop():
        while True:
            time.sleep(CLEANUP_INTERVAL)
            cleanup_dead_peers()

    threading.Thread(target=cleanup_loop, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    # Parse --blockchain argument
    for i, arg in enumerate(sys.argv):
        if arg == "--blockchain" and i + 1 < len(sys.argv):
            BLOCKCHAIN_FILE = sys.argv[i + 1]
        if arg == "--mempool" and i + 1 < len(sys.argv):
            MEMPOOL_FILE = sys.argv[i + 1]
    run_server()
