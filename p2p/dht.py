"""
ww/core/subconscious/dht.py — Simplified Kademlia DHT for Peer Discovery

A minimal Kademlia Distributed Hash Table for peer discovery in the WW
subconscious network. Not a full DHT (no value storage) — only peer discovery.

Key simplifications vs full Kademlia:
  - 160-bit node IDs (SHA-1 of node_id string)
  - k = 8 (kbucket size)
  - No iterative lookup (direct RPC to closest known peers)
  - No value storage (only peer address lookup)
  - UDP transport for liveness checks, TCP HTTP for actual data transfer

Protocol:
  PING   — check if peer is alive
  FIND_NODE — return k closest peers to a target ID
  PONG   — response to PING with node info
"""

from __future__ import annotations
import hashlib
import json
import logging
import random
import socket
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("ww.subconscious.dht")

# Constants
K = 8  # bucket size
ALPHA = 3  # parallel lookup factor
B = 160  # bits in node ID
PING_TIMEOUT = 2.0  # seconds
REFRESH_INTERVAL = 600  # refresh buckets every 10 min
STALE_TIMEOUT = 3600  # evict after 1 hour no contact


def _node_id(key: str) -> int:
    """Generate 160-bit node ID from a string key."""
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16)


def _xor_dist(a: int, b: int) -> int:
    return a ^ b


def _closest_to(target: int, candidates: List[Tuple[int, str, str]],
                n: int = K) -> List[Tuple[int, str, str]]:
    """Return the n closest (distance, peer_id, address) to target."""
    scored = [(_xor_dist(target, pid), pid, addr)
              for pid, addr in candidates]
    scored.sort(key=lambda x: x[0])
    return scored[:n]


def _int_to_ip_port(addr: str) -> Tuple[str, int]:
    """Parse 'ip:port' string to tuple."""
    parts = addr.rsplit(":", 1)
    return parts[0], int(parts[1])


def _udp_ping(address: Tuple[str, int], timeout: float = PING_TIMEOUT) -> Optional[bytes]:
    """Send UDP ping to address, return response data or None."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        ping_data = json.dumps({"type": "PING"}).encode("utf-8")
        sock.sendto(ping_data, address)
        data, _ = sock.recvfrom(4096)
        sock.close()
        return data
    except (socket.timeout, OSError):
        return None


def _udp_query(address: Tuple[str, int], query: dict,
               timeout: float = PING_TIMEOUT) -> Optional[dict]:
    """Send UDP query, return response dict or None."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        data = json.dumps(query).encode("utf-8")
        sock.sendto(data, address)
        resp, _ = sock.recvfrom(4096)
        sock.close()
        return json.loads(resp.decode("utf-8"))
    except (socket.timeout, OSError, json.JSONDecodeError):
        return None


class KBucket:
    """A single k-bucket covering a specific XOR distance range."""

    def __init__(self, min_dist: int, max_dist: int, k: int = K):
        self.min_dist = min_dist
        self.max_dist = max_dist
        self.k = k
        self.contacts: Dict[int, dict] = {}  # node_id -> {address, last_seen, version}
        self._lock = threading.Lock()

    def add_contact(self, node_id: int, address: str, version: str = ""):
        """Add or refresh a contact."""
        with self._lock:
            if node_id in self.contacts:
                self.contacts[node_id]["last_seen"] = time.time()
                self.contacts[node_id]["version"] = version
                return True
            if len(self.contacts) < self.k:
                self.contacts[node_id] = {
                    "address": address,
                    "last_seen": time.time(),
                    "version": version,
                }
                return True
            return False  # bucket full

    def remove_contact(self, node_id: int) -> bool:
        with self._lock:
            return self.contacts.pop(node_id, None) is not None

    def get_contacts(self) -> List[Tuple[int, str]]:
        """Return list of (node_id, address)."""
        with self._lock:
            now = time.time()
            # Clean stale
            stale = [nid for nid, c in self.contacts.items()
                     if now - c["last_seen"] > STALE_TIMEOUT]
            for nid in stale:
                del self.contacts[nid]
            return [(nid, c["address"]) for nid, c in self.contacts.items()]

    def is_full(self) -> bool:
        with self._lock:
            return len(self.contacts) >= self.k

    def __len__(self):
        with self._lock:
            return len(self.contacts)


class DHTNode:
    """Simplified Kademlia node.

    Provides peer discovery via XOR-distance routing.
    Does NOT store arbitrary key-value pairs — only node addresses.
    """

    def __init__(
        self,
        node_id: str,
        listen_port: int = 9834,
        k: int = K,
    ):
        self.node_id_str = node_id
        self.node_id_int = _node_id(node_id)
        self.listen_port = listen_port
        self.k = k

        # k-buckets: split the 160-bit space
        self._buckets: List[KBucket] = []
        for i in range(B):
            self._buckets.append(KBucket(
                2 ** (B - i - 1), 2 ** (B - i) - 1, k
            ))

        # UDP server
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Callbacks
        self.on_peer_found: Optional[Callable[[str, str], None]] = None

        # Total contacts tracked
        self._contact_count = 0

    # ── Lifecycle ──

    def start(self):
        """Start UDP listener for Kademlia RPCs."""
        if self._running:
            return
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(1.0)
        try:
            self._sock.bind(("0.0.0.0", self.listen_port))
        except OSError:
            self._sock.bind(("0.0.0.0", 0))
            self.listen_port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        logger.info(f"📡 DHT node listening on UDP :{self.listen_port}")

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # ── Bucket management ──

    def _bucket_for(self, target: int) -> KBucket:
        dist = _xor_dist(self.node_id_int, target)
        for b in self._buckets:
            if b.min_dist <= dist <= b.max_dist:
                return b
        return self._buckets[-1]

    def add_peer(self, peer_id: str, address: str, version: str = ""):
        """Add a peer to the routing table."""
        pid = _node_id(peer_id)
        if pid == self.node_id_int:
            return
        bucket = self._bucket_for(pid)
        added = bucket.add_contact(pid, address, version)
        if added:
            self._contact_count += 1
            if self.on_peer_found:
                self.on_peer_found(peer_id, address)

    def get_closest_peers(self, target: str, n: int = K) -> List[Tuple[str, str]]:
        """Return the n closest peers (by XOR distance) to a target node ID."""
        target_int = _node_id(target) if len(target) < 40 else int(target, 16)
        all_contacts: List[Tuple[int, str]] = []
        for b in self._buckets:
            all_contacts.extend(b.get_contacts())
        closest = _closest_to(target_int,
                              [(cid, addr) for cid, addr in all_contacts],
                              n)
        result = []
        for dist, cid, addr in closest:
            for b in self._buckets:
                for nid, caddr in b.get_contacts():
                    if nid == cid:
                        # Find the original peer_id string
                        peer_id = self._find_peer_id(nid)
                        if peer_id:
                            result.append((peer_id, addr))
                        break
        return result

    def _find_peer_id(self, node_id_int: int) -> Optional[str]:
        """Reverse-lookup: find peer_id string from int ID."""
        # Not stored — return hex string
        return hex(node_id_int)[2:]

    def get_all_peers(self) -> List[Tuple[str, str]]:
        """Return ALL known peers as (peer_id, address) pairs."""
        peers = []
        seen_addrs = set()
        for b in self._buckets:
            for nid, addr in b.get_contacts():
                if addr not in seen_addrs:
                    seen_addrs.add(addr)
                    peers.append((hex(nid)[2:], addr))
        return peers

    # ── Kademlia RPCs ──

    def _handle_ping(self, addr: Tuple[str, int]) -> bytes:
        address = f"{addr[0]}:{addr[1]}"
        return json.dumps({
            "type": "PONG",
            "node_id": self.node_id_str,
            "address": f"{addr[0]}:{self.listen_port}",
        }).encode("utf-8")

    def _handle_find_node(self, data: dict, addr: Tuple[str, int]) -> bytes:
        target = data.get("target", "")
        if not target:
            return json.dumps({"type": "ERROR", "message": "missing target"}).encode("utf-8")
        closest = self.get_closest_peers(target, self.k)
        return json.dumps({
            "type": "NODES",
            "target": target,
            "nodes": [{"node_id": pid, "address": a} for pid, a in closest],
        }).encode("utf-8")

    def _listen_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
                self._process_message(data, addr)
            except socket.timeout:
                continue
            except OSError:
                break

    def _process_message(self, data: bytes, addr: Tuple[str, int]):
        try:
            msg = json.loads(data.decode("utf-8"))
            msg_type = msg.get("type", "")
            peer_addr = f"{addr[0]}:{addr[1]}"

            if msg_type == "PING":
                resp = self._handle_ping(addr)
                try:
                    self._sock.sendto(resp, addr)
                except OSError:
                    pass

            elif msg_type == "FIND_NODE":
                resp = self._handle_find_node(msg, addr)
                try:
                    self._sock.sendto(resp, addr)
                except OSError:
                    pass

            elif msg_type == "PONG":
                peer_id = msg.get("node_id", "")
                paddr = msg.get("address", peer_addr)
                if peer_id:
                    self.add_peer(peer_id, paddr)

            elif msg_type == "NODES":
                target = msg.get("target", "")
                for node in msg.get("nodes", []):
                    pid = node.get("node_id", "")
                    a = node.get("address", "")
                    if pid and a:
                        self.add_peer(pid, a)

        except (json.JSONDecodeError, Exception) as e:
            logger.debug(f"DHT message error: {e}")

    # ── Outbound queries ──

    def ping(self, address: str) -> Optional[dict]:
        """Ping a peer, return their info or None."""
        addr = _int_to_ip_port(address)
        resp = _udp_query(addr, {"type": "PING"})
        if resp and resp.get("type") == "PONG":
            peer_id = resp.get("node_id", "")
            paddr = resp.get("address", address)
            if peer_id:
                self.add_peer(peer_id, paddr)
            return resp
        return None

    def find_node(self, address: str, target: str) -> Optional[List[tuple]]:
        """Ask a peer for nodes close to a target."""
        addr = _int_to_ip_port(address)
        resp = _udp_query(addr, {"type": "FIND_NODE", "target": target})
        if resp and resp.get("type") == "NODES":
            nodes = resp.get("nodes", [])
            result = []
            for n in nodes:
                pid = n.get("node_id", "")
                a = n.get("address", "")
                if pid and a:
                    self.add_peer(pid, a)
                    result.append((pid, a))
            return result
        return None

    def bootstrap(self, bootstrap_addresses: List[str]):
        """Join the DHT via bootstrap nodes.

        Sends FIND_NODE for our own ID to discover the closest peers.
        """
        if not bootstrap_addresses:
            return
        own_id = hex(self.node_id_int)[2:]
        for addr in bootstrap_addresses:
            try:
                nodes = self.find_node(addr, own_id)
                if nodes:
                    logger.info(f"DHT bootstrap via {addr}: {len(nodes)} peers found")
                    # Iteratively discover more
                    for pid, a in nodes[:3]:
                        more = self.find_node(a, own_id)
                        if more:
                            for mp, ma in more:
                                self.add_peer(mp, ma)
            except Exception as e:
                logger.debug(f"DHT bootstrap {addr} failed: {e}")

    # ── Stats ──

    def stats(self) -> Dict[str, Any]:
        total = sum(len(b) for b in self._buckets)
        return {
            "node_id": self.node_id_str[:12],
            "port": self.listen_port,
            "contacts": total,
            "running": self._running,
        }
