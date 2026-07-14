"""
WW Bootstrap Tracker — lightweight P2P seednode registration service

deploy to free platforms such as Render / Fly.io / Railway.
Pure Python stdlib, zero external dependencies.

Usage:
  python3 p2p/bootstrap_tracker.py

Environment:
  PORT                 — listening port (default 8080)
  WW_TRACKER_TOKEN     — if set, required for POST /p2p/register and DELETE
  WW_TRACKER_REQUIRE_AUTH — if true/1, refuse open register even when token unset
"""

import hmac
import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8080))
PEER_TIMEOUT = 3600 * 2  # 2 hours
CLEANUP_INTERVAL = 300    # cleanup every 5 minutes

# Shared secret for mutating endpoints. When set, register/unregister require it.
TRACKER_TOKEN = (os.environ.get("WW_TRACKER_TOKEN") or os.environ.get("TRACKER_TOKEN") or "").strip()
_REQUIRE_AUTH_RAW = str(os.environ.get("WW_TRACKER_REQUIRE_AUTH", "")).strip().lower()
REQUIRE_AUTH = _REQUIRE_AUTH_RAW in ("1", "true", "yes", "on", "y") or bool(TRACKER_TOKEN)

# ── in-memory store ──

peers: dict = {}
_server_start: float = time.time()


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n", ""):
        return False
    return True


def _token_ok(headers) -> bool:
    """Validate Authorization Bearer / X-Tracker-Token against TRACKER_TOKEN."""
    if not TRACKER_TOKEN:
        # Open mode only when require-auth is off
        return not REQUIRE_AUTH
    auth = headers.get("Authorization", "") or ""
    provided = ""
    if auth.startswith("Bearer ") and auth[7:].strip():
        provided = auth[7:].strip()
    if not provided:
        provided = (headers.get("X-Tracker-Token") or headers.get("X-Api-Key") or "").strip()
    if not provided:
        return False
    try:
        return hmac.compare_digest(provided.encode("utf-8"), TRACKER_TOKEN.encode("utf-8"))
    except Exception:
        return False


def register_peer(data: dict, client_ip: str = "") -> dict:
    node_id = data.get("node_id", "")
    address = data.get("address", "") or client_ip
    port = int(data.get("port", 9833))
    if not node_id:
        return {"error": "node_id required"}
    if not address:
        return {"error": "address required"}

    # Reject obviously bogus loopback registrations from remote clients
    if address in ("127.0.0.1", "localhost", "::1") and client_ip not in ("127.0.0.1", "::1", ""):
        return {"error": "loopback address not allowed from remote client"}

    # Deduplicate by (address, port): reinstalls from same machine
    # reuse existing node_id to avoid ghost entries
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
        "first_seen": peers[node_id].get("first_seen", time.time()) if node_id in peers else time.time(),
    }
    return {"status": "ok", "peers_count": len(peers), "detected_ip": client_ip or address}


def unregister_peer(node_id: str) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    if node_id in peers:
        del peers[node_id]
        return {"status": "ok", "removed": node_id, "peers_count": len(peers)}
    return {"status": "ok", "removed": None, "peers_count": len(peers)}


def get_active_peers(include_private: bool = False) -> list:
    now = time.time()
    active = [p for p in peers.values() if now - p.get("last_seen", 0) < PEER_TIMEOUT]

    def score(p):
        s = 0
        if p.get("public"):
            s += 10
        s += min(p.get("height", 0) / 1000, 5)
        return s

    active.sort(key=score, reverse=True)

    out = []
    for p in active[:100]:
        # Public listings only expose dialable public peers by default
        if not include_private and not p.get("public"):
            continue
        entry = {
            "node_id": p["node_id"],
            "address": p["address"],
            "port": p["port"],
            "version": p.get("version", ""),
            "public": bool(p.get("public", False)),
            "height": int(p.get("height", 0)),
        }
        out.append(entry)
    return out


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

    def _client_ip(self) -> str:
        # Prefer reverse-proxy headers only when they look like single IPs
        xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff and len(xff) < 64:
            return xff
        return self.client_address[0] if self.client_address else ""

    def _json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Tracker-Token, X-Api-Key")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Tracker-Token, X-Api-Key")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")

        if path in ("/p2p/peers", "/p2p/peers/all"):
            # /all with auth can include non-public; public list only public peers
            include_private = path.endswith("/all") and _token_ok(self.headers)
            active = get_active_peers(include_private=include_private)
            return self._json({
                "peers": active,
                "count": len(active),
                "tracker_version": "ww-tracker-v1.1",
                "auth_required_for_register": REQUIRE_AUTH,
            })

        if path == "/p2p/bootstrap-urls":
            return self._json({
                "urls": [],
                "dht_seeds": [],
                "node_id": "tracker",
            })

        if path.startswith("/p2p/whois/"):
            nid = path[len("/p2p/whois/"):]
            peer = peers.get(nid)
            if peer and peer.get("public"):
                now = time.time()
                ago = int(now - peer.get("last_seen", now))
                return self._json({
                    "found": True,
                    "node_id": peer["node_id"],
                    "address": peer["address"],
                    "port": peer["port"],
                    "version": peer.get("version", ""),
                    "last_seen_seconds_ago": ago,
                    "status": "online" if ago < PEER_TIMEOUT else "offline",
                })
            if peer and not peer.get("public"):
                # Do not leak private peer endpoints
                return self._json({"found": True, "node_id": nid, "public": False})
            return self._json({"found": False, "node_id": nid}, 404)

        if path == "/p2p/stats":
            now = time.time()
            active = sum(1 for p in peers.values() if now - p.get("last_seen", 0) < PEER_TIMEOUT)
            public_n = sum(
                1 for p in peers.values()
                if now - p.get("last_seen", 0) < PEER_TIMEOUT and p.get("public")
            )
            return self._json({
                "registered": len(peers),
                "active": active,
                "public_active": public_n,
                "uptime_seconds": int(time.time() - _server_start),
                "auth_required_for_register": REQUIRE_AUTH,
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
                "version": "1.1",
                "endpoints": {
                    "GET /p2p/peers": "Active public peer list",
                    "POST /p2p/register": "Register your node (auth if token set)",
                    "DELETE /p2p/register": "Unregister node (auth if token set)",
                    "GET /health": "Health check",
                },
                "auth_required_for_register": REQUIRE_AUTH,
            })

        return self._json({"error": "not_found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/p2p/register":
            if not _token_ok(self.headers):
                return self._json({
                    "error": "unauthorized",
                    "message": "Valid tracker token required (Authorization: Bearer <WW_TRACKER_TOKEN>)",
                }, 401)
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return self._json({"error": "invalid_json"}, 400)
            result = register_peer(data, client_ip=self._client_ip())
            status = 400 if result.get("error") else 200
            return self._json(result, status)

        return self._json({"error": "not_found"}, 404)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/p2p/register" or path.startswith("/p2p/unregister"):
            if not _token_ok(self.headers):
                return self._json({"error": "unauthorized"}, 401)
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            node_id = ""
            try:
                data = json.loads(body) if body else {}
                node_id = data.get("node_id", "")
            except json.JSONDecodeError:
                pass
            if not node_id and path.startswith("/p2p/unregister/"):
                node_id = path[len("/p2p/unregister/"):]
            return self._json(unregister_peer(node_id))

        return self._json({"error": "not_found"}, 404)


def main():
    import threading

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print("🌍 WW Bootstrap Tracker")
    print(f"   Listen: http://0.0.0.0:{PORT}")
    print(f"   Register auth required: {REQUIRE_AUTH}")
    if TRACKER_TOKEN:
        print(f"   Token configured: yes (len={len(TRACKER_TOKEN)})")
    else:
        print("   Token configured: no (open register — set WW_TRACKER_TOKEN in production)")

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
