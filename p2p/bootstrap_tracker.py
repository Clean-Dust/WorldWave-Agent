"""
WW Bootstrap Tracker — lightweight P2P seednode registrationservice

deploy to free platforms such as Render / Fly.io / Railway.
Pure Python stdlib, zero external dependencies.

Usage:
  python3 scripts/bootstrap_tracker.py

environment variable: 
  PORT — listening port (default 8080)
"""

import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8080))
PEER_TIMEOUT = 3600 * 2  # 2 hours 
CLEANUP_INTERVAL = 300    # cleanup every 5 minutes

# ── memorysave ──

peers: dict = {}
_server_start: float = time.time()


def register_peer(data: dict) -> dict:
    node_id = data.get("node_id", "")
    if not node_id:
        return {"error": "node_id required"}
    peers[node_id] = {
        "node_id": node_id,
        "address": data.get("address", ""),
        "port": int(data.get("port", 9833)),
        "version": data.get("version", ""),
        "public": bool(data.get("public", False)),
        "height": int(data.get("height", 0)),
        "last_seen": time.time(),
        "first_seen": peers[node_id].get("first_seen", time.time()) if node_id in peers else time.time(),
    }
    return {"status": "ok", "peers_count": len(peers)}


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


def cleanup_dead_peers():
    now = time.time()
    dead = [nid for nid, p in peers.items() if now - p.get("last_seen", 0) > PEER_TIMEOUT]
    for nid in dead:
        del peers[nid]
    if dead:
        print(f"[tracker] Cleaned {len(dead)} stale peers (active: {len(peers)})")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.rstrip("/")

        if path in ("/p2p/peers", "/p2p/peers/all"):
            return self._json({
                "peers": get_active_peers(),
                "count": len(peers),
                "tracker_version": "ww-tracker-v1",
            })

        if path == "/p2p/bootstrap-urls":
            return self._json({
                "urls": [],
                "dht_seeds": [],
                "node_id": "tracker",
            })

        if path == "/p2p/stats":
            now = time.time()
            active = sum(1 for p in peers.values() if now - p.get("last_seen", 0) < PEER_TIMEOUT)
            return self._json({
                "registered": len(peers),
                "active": active,
                "uptime_seconds": int(time.time() - _server_start),
            })

        if path == "/health":
            return self._json({
                "status": "healthy",
                "peers": len(peers),
                "uptime": int(time.time() - _server_start),
            })

        if path in ("", "/"):
            return self._json({
                "name": "WW P2P Bootstrap Tracker",
                "version": "1.0",
                "endpoints": {
                    "GET /p2p/peers": "Active peer list",
                    "POST /p2p/register": "Register your node",
                    "GET /health": "Health check",
                },
            })

        return self._json({"error": "not_found"}, 404)

    def do_POST(self):
        path = self.path.rstrip("/")

        if path == "/p2p/register":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return self._json({"error": "invalid_json"}, 400)
            return self._json(register_peer(data))

        return self._json({"error": "not_found"}, 404)


def main():
    import threading

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print("🌍 WW Bootstrap Tracker")
    print(f"   Listen: http://0.0.0.0:{PORT}")

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
    main()
