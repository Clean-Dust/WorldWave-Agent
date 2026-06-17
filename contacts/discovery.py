"""ww/contacts/discovery.py — Agent Discovery (LAN + Relay)

Two-layer discovery:
1. LAN: UDP broadcast on port 9420 — agents find each other on the same subnet
2. WAN (Relay): Optional HTTP relay server — agents behind NAT find each other via a rendezvous point

No external dependencies — pure Python sockets + httpx.
"""

from __future__ import annotations
import json
import logging
import os
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("ww.contacts.discovery")

# ── Constants ──

LAN_BROADCAST_PORT = int(os.environ.get("WW_DISCOVERY_PORT", "9420"))
LAN_BROADCAST_ADDR = "255.255.255.255"
LAN_LISTEN_ADDR = "0.0.0.0"
LAN_BROADCAST_INTERVAL = 30  # Announce presence every 30s
LAN_PEER_TIMEOUT = 120       # Remove peer if silent for 120s

RELAY_DEFAULT_URL = os.environ.get(
    "WW_RELAY_URL", ""
)

DISCOVERY_PROTOCOL_VERSION = "0.1"

# ── DiscoveryMessage ──


class DiscoveryMessage:
    """Message format for agent discovery broadcasts."""

    def __init__(
        self,
        did: str,
        friend_code: str,
        label: str,
        relay_url: str = "",
        port: int = 0,
        capabilities: Optional[Dict] = None,
    ):
        self.did = did
        self.friend_code = friend_code
        self.label = label
        self.relay_url = relay_url
        self.port = port
        self.capabilities = capabilities or {}

    def to_dict(self) -> dict:
        return {
            "protocol": DISCOVERY_PROTOCOL_VERSION,
            "type": "announce",
            "did": self.did,
            "friend_code": self.friend_code,
            "label": self.label,
            "relay_url": self.relay_url,
            "port": self.port,
            "capabilities": self.capabilities,
            "timestamp": time.time(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "DiscoveryMessage":
        return cls(
            did=data.get("did", ""),
            friend_code=data.get("friend_code", ""),
            label=data.get("label", ""),
            relay_url=data.get("relay_url", ""),
            port=data.get("port", 0),
            capabilities=data.get("capabilities", {}),
        )


# ── DiscoveredPeer ──


class DiscoveredPeer:
    """A peer discovered on the network."""

    def __init__(
        self,
        did: str,
        friend_code: str,
        label: str,
        ip: str,
        port: int = 0,
        relay_url: str = "",
        capabilities: Optional[Dict] = None,
    ):
        self.did = did
        self.friend_code = friend_code
        self.label = label
        self.ip = ip
        self.port = port
        self.relay_url = relay_url
        self.capabilities = capabilities or {}
        self.last_seen = time.time()

    def to_dict(self) -> dict:
        return {
            "did": self.did,
            "friend_code": self.friend_code,
            "label": self.label,
            "ip": self.ip,
            "port": self.port,
            "relay_url": self.relay_url,
            "capabilities": self.capabilities,
            "last_seen": self.last_seen,
        }

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_seen) > LAN_PEER_TIMEOUT


# ── NetworkDiscovery — LAN + Relay ──


class NetworkDiscovery:
    """Discovers other WW agents on LAN and via relay."""

    def __init__(
        self,
        identity,
        relay_url: str = RELAY_DEFAULT_URL,
        broadcast_port: int = LAN_BROADCAST_PORT,
        broadcast_interval: int = LAN_BROADCAST_INTERVAL,
    ):
        self._identity = identity
        self._relay_url = relay_url
        self._port = broadcast_port
        self._interval = broadcast_interval

        self._peers: Dict[str, DiscoveredPeer] = {}
        self._lock = threading.Lock()
        self._running = False
        self._threads: List[threading.Thread] = []

        # Callback for when a new peer is discovered
        self.on_discover: Optional[Callable[[DiscoveredPeer], None]] = None

    @property
    def discovered_peers(self) -> List[DiscoveredPeer]:
        """Get all recently seen peers (excluding self)."""
        with self._lock:
            now = time.time()
            active = []
            for peer in self._peers.values():
                if peer.did == self._identity.did:
                    continue  # Skip self
                if (now - peer.last_seen) < LAN_PEER_TIMEOUT:
                    active.append(peer)
            return active

    def start(self):
        """Start both LAN listener + periodic announcer."""
        if self._running:
            return
        self._running = True

        listener = threading.Thread(target=self._lan_listen, daemon=True)
        announcer = threading.Thread(target=self._lan_announce, daemon=True)
        cleaner = threading.Thread(target=self._clean_stale, daemon=True)
        relay_check = threading.Thread(target=self._relay_poll, daemon=True)

        self._threads = [listener, announcer, cleaner, relay_check]
        for t in self._threads:
            t.start()

        logger.info(
            "Discovery started (port=%s, relay=%s)",
            self._port, self._relay_url or "(none)",
        )

    def stop(self):
        self._running = False
        logger.info("Discovery stopped")

    def find_peer(self, did: str) -> Optional[DiscoveredPeer]:
        """Find a specific peer by DID."""
        with self._lock:
            peer = self._peers.get(did)
            if peer and not peer.is_stale:
                return peer
        return None

    def send_announce(self, target_ip: str = LAN_BROADCAST_ADDR):
        """Send a single UDP broadcast announcement."""
        msg = DiscoveryMessage(
            did=self._identity.did,
            friend_code=self._identity.friend_code,
            label=self._identity.label,
            port=self._port,
            capabilities={"max_permission": 3},
        )
        data = msg.to_json().encode("utf-8")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1)
            sock.sendto(data, (target_ip, self._port))
            sock.close()
        except Exception as e:
            logger.debug("Announce send failed: %s", e)

    # ── LAN Listener ──

    def _lan_listen(self):
        """Listen for UDP broadcast announcements from other agents."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((LAN_LISTEN_ADDR, self._port))
        except OSError:
            logger.warning("Discovery: port %s in use, trying alternate", self._port)
            sock.bind((LAN_LISTEN_ADDR, 0))
        sock.settimeout(2)

        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                msg_data = json.loads(data.decode("utf-8"))
                if msg_data.get("type") != "announce":
                    continue

                peer_did = msg_data.get("did", "")
                if not peer_did or peer_did == self._identity.did:
                    continue  # Skip self

                peer = DiscoveredPeer(
                    did=peer_did,
                    friend_code=msg_data.get("friend_code", ""),
                    label=msg_data.get("label", ""),
                    ip=addr[0],
                    port=msg_data.get("port", 0),
                    relay_url=msg_data.get("relay_url", ""),
                    capabilities=msg_data.get("capabilities", {}),
                )

                is_new = False
                with self._lock:
                    if peer_did not in self._peers:
                        is_new = True
                    self._peers[peer_did] = peer

                if is_new and self.on_discover:
                    try:
                        self.on_discover(peer)
                    except Exception as e:
                        logger.warning("on_discover callback error: %s", e)
            except json.JSONDecodeError:
                pass
            except socket.timeout:
                pass
            except OSError:
                break

    def _lan_announce(self):
        """Periodically broadcast presence on LAN."""
        while self._running:
            self.send_announce()
            time.sleep(self._interval)

    def _clean_stale(self):
        """Periodically remove stale peers."""
        while self._running:
            time.sleep(60)
            now = time.time()
            with self._lock:
                stale = [
                    did for did, peer in self._peers.items()
                    if (now - peer.last_seen) > LAN_PEER_TIMEOUT
                ]
                for did in stale:
                    del self._peers[did]
                if stale:
                    logger.debug("Removed %d stale peers", len(stale))

    def _relay_poll(self):
        """Periodically poll relay server for peer updates."""
        if not self._relay_url:
            return
        while self._running:
            time.sleep(60)
            try:
                import httpx
                resp = httpx.get(
                    f"{self._relay_url}/peers",
                    params={"did": self._identity.did},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for peer_data in data.get("peers", []):
                        peer = DiscoveredPeer(
                            did=peer_data["did"],
                            friend_code=peer_data.get("friend_code", ""),
                            label=peer_data.get("label", ""),
                            ip=peer_data.get("ip", ""),
                            port=peer_data.get("port", 0),
                            relay_url=peer_data.get("relay_url", ""),
                            capabilities=peer_data.get("capabilities", {}),
                        )
                        with self._lock:
                            self._peers[peer.did] = peer
                # Register self on relay
                self._register_on_relay()
            except Exception as e:
                logger.debug("Relay poll failed: %s", e)

    def _register_on_relay(self):
        """Register our presence on the relay server."""
        if not self._relay_url:
            return
        try:
            import httpx
            resp = httpx.post(
                f"{self._relay_url}/register",
                json={
                    "did": self._identity.did,
                    "friend_code": self._identity.friend_code,
                    "label": self._identity.label,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                logger.debug("Registered on relay")
        except Exception:
            pass
