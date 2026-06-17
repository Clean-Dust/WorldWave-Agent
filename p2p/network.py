"""
ww/core/subconscious/network.py — Global P2P network v6

Three-layer architecture achieves global connectivity:

1. Bootstrap Tracker（HTTP seedserver）
   - New node registers with tracker (POST /register)
   - New node gets active peer list from tracker (GET /peers)
   - Tracker periodically cleans up unresponsive nodes

2. HTTP P2P between nodes (TCP :9833)
   - Public node (port open) receives connections, relays block/tx
   - NAT node can only connect out, periodically polls public nodes
   - Supports outbound-only mode (99% home network available)

3. Block/Tx Propagation
   - New block → broadcast to all known peers
   - New tx → broadcast to all known peers
   - Periodic blockchain sync (getblocks → getdata)
"""

from __future__ import annotations
import json
import logging
import os
import socket
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("ww.subconscious.network")

# ── Constants ──

P2P_PORT = 9833
BOOTSTRAP_INTERVAL = 300           # Connect to bootstrap every 5 minutes
BLOCK_SYNC_INTERVAL = 30           # Pull new block every 30 seconds
TX_PUSH_INTERVAL = 5               # Push new tx every 5 seconds
PEER_CLEANUP_INTERVAL = 3600      # Clean up zombie peers every 1 hour
PEER_TIMEOUT = 7200                # Kick out if no response for 2 hours
MAX_PEERS = 100
MAX_BLOCKS_PER_SYNC = 50

# Bootstrap tracker — resolve from env var, fall back to empty list
# Users can set WW_BOOTSTRAP_URLS (comma-separated) at deploy time
_BOOTSTRAP_ENV = os.environ.get("WW_BOOTSTRAP_URLS", "")
BOOTSTRAP_URLS = (
    [u.strip() for u in _BOOTSTRAP_ENV.split(",") if u.strip()]
    if _BOOTSTRAP_ENV
    else []
)

PEERS_FILE = os.environ.get("WW_PEERS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "subconscious", "peers.json"))


# ════════════════════════════════════════════════════════════════
#  Global Peer Info
# ════════════════════════════════════════════════════════════════


class PeerInfo:
    """Global information of a remote node."""
    def __init__(self, node_id: str, address: str, port: int,
                 version: str = "", last_seen: float = 0,
                 public: bool = False, gossip_url: str = ""):
        self.node_id = node_id
        self.address = address  # Public IP or hostname
        self.port = port
        self.version = version
        self.last_seen = last_seen or time.time()
        self.public = public    # Whether it can receive connections
        self.failures = 0
        self.gossip_url = gossip_url  # URL for gossip model exchange

    def url(self) -> str:
        return f"http://{self.address}:{self.port}"

    def gossip_endpoint(self) -> str:
        """Return the full gossip model URL."""
        if self.gossip_url:
            return f"{self.gossip_url.rstrip('/')}/gossip/model"
        return f"http://{self.address}:{self.port}/gossip/model"

    def is_alive(self) -> bool:
        return time.time() - self.last_seen < PEER_TIMEOUT

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "address": self.address,
            "port": self.port,
            "version": self.version,
            "last_seen": self.last_seen,
            "public": self.public,
            "gossip_url": self.gossip_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PeerInfo":
        return cls(
            node_id=d.get("node_id", ""),
            address=d.get("address", ""),
            port=d.get("port", P2P_PORT),
            version=d.get("version", ""),
            last_seen=d.get("last_seen", 0),
            public=d.get("public", False),
            gossip_url=d.get("gossip_url", ""),
        )


# ════════════════════════════════════════════════════════════════
#  HTTP P2P Handler
# ════════════════════════════════════════════════════════════════


def _make_handler(network: "GlobalP2PNetwork", gossip_handler=None) -> type:
    """Factory: returns a P2P HTTP request handler class bound to the given network.

    Args:
        network: GlobalP2PNetwork instance
        gossip_handler: optional callable(peer_params_dict) → our_params_dict
            Used by GossipModule for peer-to-peer model exchange.
    """

    class P2PHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, data: dict, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

        def do_OPTIONS(self):
            """Handle CORS preflight."""
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if self.path == "/p2p/ping":
                self._json({
                    "status": "pong",
                    "node_id": network.node_id,
                    "version": network.version,
                    "height": network.blockchain_height(),
                    "peers": network.peer_count(),
                    "public": network.public_mode,
                })

            elif self.path == "/p2p/peers":
                self._json({
                    "peers": [p.to_dict() for p in network.peers.values()],
                    "count": len(network.peers),
                })

            elif self.path == "/p2p/blockchain/height":
                self._json({
                    "height": network.blockchain_height(),
                    "hash": network.blockchain_latest_hash()[:16] if network.blockchain_latest_hash() else "none",
                })

            elif self.path.startswith("/p2p/blocks"):
                # returnblock（parameters: ?from=height&count=count）
                parts = self.path.split("?")
                params = {}
                if len(parts) > 1:
                    for pair in parts[1].split("&"):
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            params[k] = v
                from_h = int(params.get("from", 0))
                count = int(params.get("count", MAX_BLOCKS_PER_SYNC))

                blocks_data = network.get_blocks(from_h, count)
                self._json({
                    "blocks": blocks_data,
                    "from": from_h,
                    "count": len(blocks_data),
                })

            elif self.path == "/p2p/mempool":
                self._json({
                    "transactions": network.get_mempool_txs(),
                    "count": network.mempool_count(),
                })

            elif self.path.startswith("/p2p/payload/"):
                cid = self.path[len("/p2p/payload/"):]
                # Try local payload_store first, then callback
                payload = network.payload_store.get(cid)
                if payload is None and network._on_payload_pull:
                    pulled = network._on_payload_pull(cid)
                    if pulled:
                        payload = json.dumps(pulled).encode()
                if payload:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(payload if isinstance(payload, bytes) else payload.encode())
                else:
                    self._json({"error": "payload_not_found"}, 404)

            else:
                self._json({"error": "not_found"}, 404)

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json({"error": "invalid_json"}, 400)
                return

            if self.path == "/p2p/register":
                """Bootstrap tracker: accept peer registration."""
                node_id = data.get("node_id", "")
                if node_id:
                    # Detect client's real public IP from TCP connection
                    client_ip = self.client_address[0]
                    provided_addr = data.get("address", "")
                    # If the peer didn't provide a good address, use the TCP source
                    if not provided_addr or provided_addr.startswith("192.") or provided_addr.startswith("10.") or provided_addr.startswith("172."):
                        real_addr = client_ip
                    else:
                        real_addr = provided_addr
                    peer = PeerInfo(
                        node_id=node_id,
                        address=real_addr,
                        port=int(data.get("port", 9833)),
                        version=data.get("version", ""),
                        last_seen=time.time(),
                        gossip_url=data.get("gossip_url", ""),
                    )
                    network.add_peer(peer)
                    logger.info(f"📝 Peer registered: {node_id} @ {peer.address}:{peer.port}")
                self._json({
                    "status": "ok",
                    "peers_count": len(network.peers),
                    "detected_ip": self.client_address[0],
                })

            elif self.path == "/p2p/block":
                """Receive a new block."""
                ok = network.receive_block(data)
                self._json({"accepted": ok})

            elif self.path == "/p2p/transaction":
                """Receive a new transaction."""
                ok = network.receive_transaction(data)
                self._json({"accepted": ok})

            elif self.path == "/p2p/peers/exchange":
                """Exchange peer list."""
                their_peers = data.get("peers", [])
                network.exchange_peers(their_peers)
                self._json({
                    "exchanged": True,
                    "peers": [p.to_dict() for p in list(network.peers.values())[:20]],
                })

            elif self.path == "/p2p/payload":
                cid = data.get("cid", "")
                payload_raw = data.get("payload", None)
                if cid and payload_raw:
                    payload_str = json.dumps(payload_raw)
                    network.payload_store[cid] = payload_str.encode()
                    self._json({"stored": True, "cid": cid, "size": len(payload_str)})
                else:
                    self._json({"error": "missing_cid_or_payload"}, 400)

            elif self.path == "/gossip/model" and gossip_handler is not None:
                """Gossip learning: peer model exchange."""
                try:
                    response = gossip_handler(data)
                    self._json(response)
                except Exception as e:
                    self._json({"error": str(e)}, 400)

            else:
                self._json({"error": "not_found"}, 404)

    return P2PHandler


# ════════════════════════════════════════════════════════════════
#  Global P2P Network
# ════════════════════════════════════════════════════════════════


class GlobalP2PNetwork:
    """
    Global P2P network layer with Kademlia DHT peer discovery.

    Two-layer architecture:
      Layer 1: DHT (UDP :9834) — peer discovery via XOR routing
      Layer 2: HTTP (TCP :9833) — data transfer (blocks, txs, gossip models)

    No matter where you are in the world, as long as WW has network you can join.

    Mode:
      public  = port 9833 open, can accept incoming connections
      private = outbound only, cannot accept connections
    """

    def __init__(
        self,
        node_id: str = "",
        listen_port: int = P2P_PORT,
        dht_port: int = 9834,
        version: str = "ww-subconscious-v0.5",
        public_mode: bool = False,
        public_address: str = "",
        bootstrap_urls: Optional[List[str]] = None,
    ):
        self.node_id = node_id or uuid.uuid4().hex[:12]
        self.listen_port = listen_port
        self.version = version
        self.public_mode = public_mode or self._can_open_port(listen_port)
        self.public_address = public_address  # Publicly reachable IP/hostname
        self.bootstrap_urls = bootstrap_urls or BOOTSTRAP_URLS

        # state
        self.peers: Dict[str, PeerInfo] = {}
        self.running = False
        self._my_address = public_address or self._detect_public_ip()

        # HTTP server (only start in public mode)
        self._server: Optional[HTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None

        # DHT (peer discovery)
        from .dht import DHTNode
        self.dht = DHTNode(
            node_id=self.node_id,
            listen_port=dht_port,
            k=8,
        )
        self.dht.on_peer_found = self._on_dht_peer_found

        # backgroundthreads
        self._threads: List[threading.Thread] = []

        # Blockchain / Mempool callback
        self._get_blocks_fn: Optional[Callable] = None
        self._get_height_fn: Optional[Callable] = None
        self._get_latest_hash_fn: Optional[Callable] = None
        self._get_mempool_fn: Optional[Callable] = None
        self._mempool_count_fn: Optional[Callable] = None
        self._receive_block_fn: Optional[Callable] = None
        self._receive_tx_fn: Optional[Callable] = None

        # Payload store for Nostr payload separation
        self.payload_store: Dict[str, bytes] = {}
        self._on_payload_pull = None  # callback(cid) -> dict or None

        # Gossip learning handler (set by Subconscious)
        self.gossip_handler = None

        # Queue of tx/block to be propagated
        self._pending_broadcast: List[str] = []
        self._broadcasted: Set[str] = set()

        # Load saved peer list
        self._load_peers()

    def set_blockchain_callbacks(
        self,
        get_blocks: Callable,
        get_height: Callable,
        get_latest_hash: Callable,
        get_mempool: Callable,
        mempool_count: Callable,
        receive_block: Callable,
        receive_tx: Callable,
    ):
        self._get_blocks_fn = get_blocks
        self._get_height_fn = get_height
        self._get_latest_hash_fn = get_latest_hash
        self._get_mempool_fn = get_mempool
        self._mempool_count_fn = mempool_count
        self._receive_block_fn = receive_block
        self._receive_tx_fn = receive_tx

    # ── DHT Integration ──

    def _on_dht_peer_found(self, peer_id_str: str, address: str):
        """Called when DHT discovers a new peer."""
        if peer_id_str in self.peers:
            return
        try:
            parts = address.rsplit(":", 1)
            ip = parts[0]
            port = int(parts[1])
        except (ValueError, IndexError):
            return
        peer = PeerInfo(
            node_id=peer_id_str,
            address=ip,
            port=port,
            version=self.version,
            last_seen=time.time(),
        )
        self.add_peer(peer)

    def _dht_get_address(self) -> str:
        """Get this node's address in format expected by DHT."""
        if self._my_address:
            return f"{self._my_address}:{self.listen_port}"
        return f"127.0.0.1:{self.listen_port}"

    def _dht_bootstrap_from_http(self):
        """Bootstrap DHT from existing HTTP bootstrap URLs."""
        for url in self.bootstrap_urls or []:
            try:
                url = url.rstrip("/")
                # Try to discover peers from HTTP bootstrap
                resp = self._http_get(url, "/p2p/peers/all")
                if resp and isinstance(resp, dict):
                    for p in resp.get("peers", []):
                        pid = p.get("node_id", "")
                        addr = f"{p.get('address', '')}:{p.get('port', 9833)}"
                        if pid and addr:
                            self.dht.add_peer(pid, addr)
            except Exception as e:
                logger.debug(f"DHT HTTP bootstrap {url} failed: {e}")

    # ── start／stop ──

    def start(self):
        if self.running:
            return
        self.running = True

        # Start DHT (Kademlia UDP peer discovery)
        self.dht.start()

        # HTTP server（receive incoming connection）
        if self.public_mode:
            try:
                handler = _make_handler(self, gossip_handler=self.gossip_handler)
                self._server = HTTPServer(("0.0.0.0", self.listen_port), handler)
                self._server_thread = threading.Thread(
                    target=self._server.serve_forever, daemon=True
                )
                self._server_thread.start()
                logger.info(f"🌐 P2P server: http://0.0.0.0:{self.listen_port} (PUBLIC)")
            except OSError as e:
                logger.warning(f"P2P server failed (likely port in use): {e}")
                self.public_mode = False
        else:
            logger.info("🌐 P2P mode: PRIVATE (outbound only)")

        # backgroundsyncthread
        threads_spec = [
            ("bootstrap", self._bootstrap_loop, BOOTSTRAP_INTERVAL),
            ("sync_blocks", self._block_sync_loop, BLOCK_SYNC_INTERVAL),
            ("push_tx", self._tx_push_loop, TX_PUSH_INTERVAL),
            ("cleanup", self._cleanup_loop, PEER_CLEANUP_INTERVAL),
        ]
        for name, fn, interval in threads_spec:
            t = threading.Thread(target=self._loop_wrapper, args=(fn, interval), daemon=True)
            t.start()
            self._threads.append(t)

        logger.info(f"🌐 P2P started: node_id={self.node_id[:12]} mode={'public' if self.public_mode else 'private'}")

    def stop(self):
        self.running = False
        self.dht.stop()
        if self._server:
            self._server.shutdown()
        for t in self._threads:
            t.join(timeout=2)
        self._save_peers()
        logger.info("🌐 P2P stopped")

    # ── Peer management ──

    def add_peer(self, peer: PeerInfo):
        if len(self.peers) >= MAX_PEERS:
            return
        if peer.node_id and peer.node_id != self.node_id:
            existing = self.peers.get(peer.node_id)
            if existing:
                existing.last_seen = time.time()
                existing.failures = 0
                if peer.public:
                    existing.public = True
            else:
                self.peers[peer.node_id] = peer
                logger.debug(f"➕ Peer: {peer.node_id[:12]} @ {peer.address}:{peer.port}")

    def remove_peer(self, node_id: str):
        self.peers.pop(node_id, None)

    def peer_count(self) -> int:
        return len(self.peers)

    def exchange_peers(self, peer_dicts: List[dict]):
        for pd in peer_dicts:
            if pd.get("node_id") and pd["node_id"] != self.node_id:
                self.add_peer(PeerInfo.from_dict(pd))

    # ── Bootstrap Tracker ──

    def add_bootstrap_url(self, url: str):
        """Add a bootstrap tracker URL at runtime (not just env var)."""
        url = url.rstrip("/")
        if url not in self.bootstrap_urls:
            self.bootstrap_urls.append(url)
            logger.info(f"➕ Bootstrap URL added: {url}")
            # Immediately try to bootstrap
            self._bootstrap_with_tracker(url)

    def get_bootstrap_urls(self) -> List[str]:
        """Return current bootstrap tracker URLs."""
        return list(self.bootstrap_urls)

    def _bootstrap_with_tracker(self, tracker_url: str):
        """Register with bootstrap tracker and get peer list."""
        try:
            import urllib.request

            # Register self
            gossip_url = ""
            if self.public_mode and self._my_address:
                gossip_url = f"http://{self._my_address}:{self.listen_port}"
            register_data = json.dumps({
                "node_id": self.node_id,
                "address": self._my_address,
                "port": self.listen_port,
                "version": self.version,
                "public": self.public_mode,
                "height": self.blockchain_height(),
                "gossip_url": gossip_url,
            }).encode()
            req = urllib.request.Request(
                tracker_url.rstrip("/") + "/p2p/register",
                data=register_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Worldwave-P2P/0.7",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp_body = json.loads(resp.read().decode())
                detected_ip = resp_body.get("detected_ip", "")
                if detected_ip and not self._my_address:
                    self._my_address = detected_ip
                    logger.info(f"📍 Public IP detected from tracker: {detected_ip}")

            # get peer list
            req2 = urllib.request.Request(
                tracker_url.rstrip("/") + "/p2p/peers",
                headers={"User-Agent": "Worldwave-P2P/0.7"},
            )
            with urllib.request.urlopen(req2, timeout=5) as resp:
                data = json.loads(resp.read())
            for pd in data.get("peers", []):
                if pd.get("node_id") and pd["node_id"] != self.node_id:
                    self.add_peer(PeerInfo.from_dict(pd))

            logger.info(f"🌍 Bootstrap registered at {tracker_url}: height={self.blockchain_height()}")
        except Exception as e:
            logger.info(f"Bootstrap {tracker_url} failed: {e}")

    def _bootstrap_loop(self):
        """Connect to bootstrap tracker and DHT peers."""
        if not self.bootstrap_urls:
            return
        while self.running:
            # DHT bootstrap: discover peers via Kademlia
            self._dht_bootstrap_from_http()
            # HTTP bootstrap: register with tracker
            for url in self.bootstrap_urls:
                self._bootstrap_with_tracker(url)
            time.sleep(BOOTSTRAP_INTERVAL)

    # ── blocksync ──

    def _block_sync_loop(self):
        """Sync latest block from peer."""
        time.sleep(3)  # Wait for server to start
        while self.running:
            if not self.peers:
                time.sleep(BLOCK_SYNC_INTERVAL)
                continue
            try:
                self._sync_blocks_from_peers()
            except Exception as e:
                logger.debug(f"Block sync error: {e}")
            time.sleep(BLOCK_SYNC_INTERVAL)

    def _sync_blocks_from_peers(self):
        """Traverse peers, find blocks more updated than local."""
        local_height = self.blockchain_height()

        # First ask peer height
        candidates = []
        for peer in list(self.peers.values())[:10]:
            result = self._http_get(peer, "/p2p/blockchain/height")
            if result and result.get("height", -1) > local_height:
                candidates.append((peer, result["height"]))

        if not candidates:
            return

        # Select highest peer
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_peer, best_height = candidates[0]
        logger.info(f"📥 Peer {best_peer.node_id[:12]} has {best_height} blocks (local: {local_height})")

        # Request missing blocks from it
        result = self._http_get(best_peer, f"/p2p/blocks?from={local_height}&count={MAX_BLOCKS_PER_SYNC}")
        if result and "blocks" in result:
            for block_data in result["blocks"]:
                self.receive_block(block_data)

    # ── Transaction propagation ──

    def _tx_push_loop(self):
        """Push local new transaction to peer."""
        while self.running:
            if self.peers:
                # Get new transaction from local mempool
                local_txs = self.get_mempool_txs()
                for tx in local_txs:
                    tx_hash = tx.get("hash", "")
                    if tx_hash and tx_hash not in self._broadcasted:
                        self._broadcast_transaction(tx)
                        self._broadcasted.add(tx_hash)
                # Limit broadcasted set size
                if len(self._broadcasted) > 1000:
                    self._broadcasted = set(list(self._broadcasted)[-500:])
            time.sleep(TX_PUSH_INTERVAL)

    def _broadcast_transaction(self, tx_data: dict):
        """Broadcast a transaction to all known peers."""
        for peer in list(self.peers.values())[:10]:
            self._http_post(peer, "/p2p/transaction", tx_data)

    def broadcast_block(self, block_data: dict):
        """Broadcast a new block to all peers."""
        count = 0
        for peer in list(self.peers.values())[:20]:
            result = self._http_post(peer, "/p2p/block", block_data)
            if result:
                count += 1
        logger.info(f"📡 Block broadcast to {count} peers")

    def set_payload_pull_callback(self, fn):
        """Set callback for requesting payload. fn(cid) -> dict or None"""
        self._on_payload_pull = fn

    def pull_payload_from_peers(self, cid: str, timeout_s: float = 10.0) -> Optional[dict]:
        """Pull a payload from known peer."""
        for peer in list(self.peers.values())[:10]:
            try:
                import urllib.request
                url = f"http://{peer.address}:{peer.port}/p2p/payload/{cid}"
                req = urllib.request.Request(url, headers={"User-Agent": "Worldwave-P2P/0.7"})
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode())
            except Exception:
                continue
        logger.warning(f"Could not pull payload {cid} from any peer")
        return None

    def push_payload_to_peers(self, cid: str, payload: dict):
        """push payload to all  peer。"""
        count = 0
        for peer in list(self.peers.values())[:10]:
            result = self._http_post(peer, "/p2p/payload", {"cid": cid, "payload": payload})
            if result:
                count += 1
        if count > 0:
            logger.info(f"📤 Pushed payload {cid} to {count} peers")

    # ── Data access ──

    def receive_block(self, block_data: dict) -> bool:
        if self._receive_block_fn:
            try:
                return self._receive_block_fn(block_data)
            except Exception:
                return False
        return False

    def receive_transaction(self, tx_data: dict) -> bool:
        if self._receive_tx_fn:
            try:
                return self._receive_tx_fn(tx_data)
            except Exception:
                return False
        return False

    # ── Blockchain proxy ──

    def blockchain_height(self) -> int:
        if self._get_height_fn:
            try:
                return self._get_height_fn()
            except Exception:
                return -1
        return -1

    def blockchain_latest_hash(self) -> str:
        if self._get_latest_hash_fn:
            try:
                return self._get_latest_hash_fn()
            except Exception:
                return ""
        return ""

    def get_blocks(self, from_height: int, count: int) -> List[dict]:
        if self._get_blocks_fn:
            try:
                return self._get_blocks_fn(from_height, count)
            except Exception:
                return []
        return []

    def get_mempool_txs(self) -> List[dict]:
        if self._get_mempool_fn:
            try:
                return self._get_mempool_fn()
            except Exception:
                return []
        return []

    def mempool_count(self) -> int:
        if self._mempool_count_fn:
            try:
                return self._mempool_count_fn()
            except Exception:
                return 0
        return 0

    # ── networktool ──

    def _http_get(self, peer: PeerInfo, path: str) -> Optional[dict]:
        try:
            import urllib.request
            url = peer.url() + path
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            peer.failures += 1
            if peer.failures >= 3:
                self.remove_peer(peer.node_id)
            return None

    def _http_post(self, peer: PeerInfo, path: str, data: dict) -> Optional[dict]:
        try:
            import urllib.request
            url = peer.url() + path
            payload = json.dumps(data).encode()
            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            peer.failures += 1
            if peer.failures >= 3:
                self.remove_peer(peer.node_id)
            return None

    def _can_open_port(self, port: int) -> bool:
        """Check if port can be listened on."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("0.0.0.0", port))
            s.close()
            return True
        except OSError:
            return False

    def _detect_public_ip(self) -> str:
        """Try to detect public IP (use external service)."""
        try:
            import urllib.request
            req = urllib.request.Request("https://api.ipify.org?format=json")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                return data.get("ip", "")
        except Exception:
            return ""

    def _loop_wrapper(self, fn, interval: float):
        time.sleep(1)
        while self.running:
            try:
                fn()
            except Exception as e:
                logger.debug(f"P2P loop error: {e}")
            if self.running:
                time.sleep(interval)

    # ── Peer cleanup ──

    def _cleanup_loop(self):
        while self.running:
            now = time.time()
            dead = [nid for nid, p in self.peers.items()
                    if now - p.last_seen > PEER_TIMEOUT]
            for nid in dead:
                self.peers.pop(nid, None)
            if dead:
                logger.debug(f"🧹 Cleaned {len(dead)} dead peers")
            time.sleep(PEER_CLEANUP_INTERVAL)

    # ── statistics ──

    def stats(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "mode": "public" if self.public_mode else "private",
            "port": self.listen_port,
            "address": self._my_address or "unknown",
            "peers": len(self.peers),
            "public_peers": sum(1 for p in self.peers.values() if p.public),
            "running": self.running,
            "bootstrap_urls": self.bootstrap_urls,
        }

    # ── Persistence ──

    def _save_peers(self):
        try:
            os.makedirs(os.path.dirname(PEERS_FILE), exist_ok=True)
            with open(PEERS_FILE, "w") as f:
                json.dump({
                    "peers": [p.to_dict() for p in self.peers.values()],
                    "node_id": self.node_id,
                }, f, indent=2)
        except Exception:
            pass

    def _load_peers(self):
        if os.path.isfile(PEERS_FILE):
            try:
                with open(PEERS_FILE) as f:
                    data = json.load(f)
                for pd in data.get("peers", []):
                    peer = PeerInfo.from_dict(pd)
                    if peer.is_alive() and peer.node_id != self.node_id:
                        self.peers[peer.node_id] = peer
                logger.info(f"📂 Loaded {len(self.peers)} saved peers")
            except Exception:
                pass
