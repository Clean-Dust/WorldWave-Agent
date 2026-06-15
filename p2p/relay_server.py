"""WW Agent Relay Server

Ultra-lightweight rendezvous server for Worldwave agents behind NAT.
Provides IP exchange only — no message relay, no encryption, no storage.

Endpoints:
  POST /register  — Register or heartbeat (body: did, ip, port, friend_code, label)
  GET  /peers     — List all currently registered peers
  GET  /peer/:did — Look up a specific peer by DID

Run:
  pip install fastapi uvicorn
  python scripts/relay_server.py

Environment:
  RELAY_HOST    — Bind address (default 0.0.0.0)
  RELAY_PORT    — Listen port (default 9700)
  RELAY_CLEANUP — Peer expiry in seconds (default 300 = 5min)
"""

import os
import time
import logging
from threading import Lock
from typing import Optional

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    raise SystemExit("Missing dependencies: pip install fastapi uvicorn")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ww-relay")

# ── Config ──

RELAY_HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
RELAY_PORT = int(os.environ.get("RELAY_PORT", "9700"))
PEER_TTL = int(os.environ.get("RELAY_CLEANUP", "300"))

# ── Data store (in-memory only) ──

_registry: dict[str, dict] = {}
_registry_lock = Lock()


def _stale_check():
    """Remove peers that haven't heartbeated within PEER_TTL."""
    now = time.time()
    stale = [did for did, p in _registry.items() if now - p["last_seen"] > PEER_TTL]
    for did in stale:
        del _registry[did]
    if stale:
        logger.info("Evicted %d stale peer(s)", len(stale))
    return stale


# ── Models ──

class RegisterBody(BaseModel):
    did: str
    ip: str
    port: int
    friend_code: Optional[str] = ""
    label: Optional[str] = ""
    capabilities: Optional[list[str]] = None


class PeerInfo(BaseModel):
    did: str
    ip: str
    port: int
    friend_code: str
    label: str
    capabilities: list[str] = []
    last_seen: float


# ── App ──

app = FastAPI(title="WW Relay Server", version="1.0.0")


@app.post("/register")
def register(body: RegisterBody):
    """Register or heartbeat. Returns peer count and whether new."""
    with _registry_lock:
        is_new = body.did not in _registry
        _registry[body.did] = {
            "did": body.did,
            "ip": body.ip,
            "port": body.port,
            "friend_code": body.friend_code or body.did[-8:],
            "label": body.label or body.did[-8:],
            "capabilities": body.capabilities or [],
            "last_seen": time.time(),
        }
    _stale_check()
    return {
        "success": True,
        "new": is_new,
        "total": len(_registry),
        "did": body.did,
    }


@app.get("/peers")
def list_peers():
    """List all active peers (stale filtered)."""
    _stale_check()
    with _registry_lock:
        return {
            "success": True,
            "count": len(_registry),
            "peers": list(_registry.values()),
        }


@app.get("/peer/{did}")
def get_peer(did: str):
    """Look up a specific peer by DID prefix or full DID."""
    _stale_check()
    with _registry_lock:
        # Exact match first
        if did in _registry:
            return {"success": True, "peer": _registry[did]}
        # Prefix match
        matches = {k: v for k, v in _registry.items() if k.startswith(did)}
        if len(matches) == 1:
            did = next(iter(matches))
            return {"success": True, "peer": _registry[did]}
        if len(matches) > 1:
            return {"success": False, "error": "Ambiguous DID prefix", "candidates": list(matches.keys())}
        raise HTTPException(status_code=404, detail=f"Peer not found: {did}")


@app.get("/health")
def health():
    return {"status": "ok", "peers": len(_registry)}


# ── Main ──

def main():
    logger.info("WW Relay Server starting on %s:%d (TTL=%ds)", RELAY_HOST, RELAY_PORT, PEER_TTL)
    uvicorn.run(app, host=RELAY_HOST, port=RELAY_PORT, log_level="info")


if __name__ == "__main__":
    main()
