"""
ww/core/subconscious/gossip.py — Gossip Learning Module v1

Replaces the centralized aggregation (FedAvg via aggregator) with an
asynchronous peer-to-peer gossip protocol:

  Every N seconds:
    1. Pick a random peer from the local peer list
    2. Send local model weights to that peer
    3. Receive peer's model weights in the HTTP response
    4. FedAvg local + peer weights → new local model
    5. Continue local training on new data

Key design decisions:
  - Symmetric exchange: both sides learn from each other
  - No global round counter: each node independently decides when to gossip
  - Async: failures don't block anything
  - Random peer selection: O(log n) information spread per round
  - Lightweight: each exchange is a single HTTP request, ~200KB payload

Architecture:
  HTTP POST /gossip/model — body: {"params": {weights}, "arch_fingerprint": "..."}
  Response: {"params": {weights}, "arch_fingerprint": "..."}  (peer's weights)

Usage:
  gossip = GossipModule(local_model, get_peers_fn, http_port)
  gossip.start()  # start background thread

Integration with existing network.py:
  gossip.inject_route(server) — adds POST /gossip/model to HTTP server
"""

from __future__ import annotations
import json
import logging
import math
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .predictor import DeepRiskNet
from .aggregation import (
    aggregate_forest,
    balancer_protection,
    balance_gradient_defense,
    evaluate_model,
    flatten_weights,
    local_validation_check,
    unflatten_weights,
)

# ════════════════════════════════════════════════════
#  INT8 quantization for weight compression (4x)
# ════════════════════════════════════════════════════

# Maximum number of reputation entries shared per gossip
MAX_REPUTATION_SHARE = 20

# Headers for quantized weight format
QKEY = "_int8"      # marker in param dict
SCALE_KEY = "_s"    # scale factor key


def _int8_quantize(val: float, scale: float) -> int:
    """Quantize a float to int8 with given scale."""
    return max(-128, min(127, round(val / scale)))


def _int8_dequantize(q: int, scale: float) -> float:
    """Dequantize int8 back to float."""
    return q * scale


def _quantize_tensor(t: list, scale: float) -> list:
    """Recursively quantize a nested list of floats to int8."""
    if isinstance(t, list):
        if t and isinstance(t[0], list):
            return [_quantize_tensor(row, scale) for row in t]
        else:
            return [_int8_quantize(v, scale) for v in t]
    return [_int8_quantize(t, scale)]


def _dequantize_tensor(t, scale: float):
    """Recursively dequantize a nested list of ints back to float."""
    if isinstance(t, list):
        if t and isinstance(t[0], list):
            return [_dequantize_tensor(row, scale) for row in t]
        else:
            return [_int8_dequantize(v, scale) for v in t]
    return _int8_dequantize(t, scale)


def _tensor_scale(t: list) -> float:
    """Compute optimal INT8 scale for a tensor (max abs value / 127)."""
    flat = _tensor_flatten_list(t)
    if not flat:
        return 1.0
    max_abs = max(abs(v) for v in flat)
    if max_abs < 1e-8:
        return 1e-8
    return max_abs / 127.0


def _tensor_flatten_list(t):
    """Flatten nested list to 1D list of floats."""
    if isinstance(t, list):
        if t and isinstance(t[0], list):
            result = []
            for row in t:
                result.extend(_tensor_flatten_list(row))
            return result
        return list(t)
    return [t]


def compress_weights(params: dict) -> dict:
    """
    Compress weight dict by INT8 quantizing each tensor.
    Returns: {key: {QKEY: [int8...], SCALE_KEY: s}, ...}
    ~4x smaller payload.
    """
    compressed = {}
    for key, tensor in params.items():
        scale = _tensor_scale(tensor)
        q = _quantize_tensor(tensor, scale)
        compressed[key] = {QKEY: q, SCALE_KEY: scale}
    return compressed


def decompress_weights(compressed: dict, template: dict) -> dict:
    """
    Decompress INT8 weight dict back to float, preserving original structure
    via the template (unquantized) params dict.
    """
    out = {}
    for key, val in compressed.items():
        if isinstance(val, dict) and QKEY in val:
            scale = val[SCALE_KEY]
            q_data = val[QKEY]
            out[key] = _dequantize_tensor(q_data, scale)
        else:
            out[key] = val
    return out


# ════════════════════════════════════════════════════════════════
#  Delta Sum — per-peer weight difference compression
# ════════════════════════════════════════════════════════════════

DELTA_MAGIC = "_delta"


def _nested_subtract(a: list, b: list) -> list:
    """
    Recursively subtract two nested lists: a - b.
    Used to compute weight delta for Delta Sum compression.
    """
    if isinstance(a, list):
        if a and isinstance(a[0], list):
            return [_nested_subtract(ra, rb) for ra, rb in zip(a, b)]
        return [x - y for x, y in zip(a, b)]
    return a - b


def _nested_add(a: list, b: list) -> list:
    """
    Recursively add two nested lists: a + b.
    Used to reconstruct weights from delta.
    """
    if isinstance(a, list):
        if a and isinstance(a[0], list):
            return [_nested_add(ra, rb) for ra, rb in zip(a, b)]
        return [x + y for x, y in zip(a, b)]
    return a + b


def compute_weight_delta(current: dict, baseline: dict) -> dict:
    """
    Compute weight difference (current - baseline) per layer.
    Delta will be sparse and close to zero → efficient INT8 compression.
    """
    delta = {}
    for key in current:
        if key in baseline:
            delta[key] = _nested_subtract(current[key], baseline[key])
        else:
            delta[key] = current[key]
    return delta


def apply_weight_delta(baseline: dict, delta: dict) -> dict:
    """
    Reconstruct weights from baseline + delta.
    """
    result = {}
    for key in baseline:
        if key in delta:
            result[key] = _nested_add(baseline[key], delta[key])
        else:
            result[key] = baseline[key]
    return result

logger = logging.getLogger("ww.subconscious.gossip")

# Default gossip interval (seconds) — every 5 minutes
DEFAULT_GOSSIP_INTERVAL = 300

# Minimum peers needed to gossip
MIN_PEERS = 2

# Timeout for HTTP requests to peers (seconds)
HTTP_TIMEOUT = 30

# Maximum history size for gradient tracking
HISTORY_SIZE = 5


def _fetch_model_from_peer(peer_url: str, payload: dict) -> Optional[dict]:
    """Fetch model weights from a peer via HTTP POST.

    Args:
        peer_url: http://ip:port/gossip/model
        payload: our model weights to send

    Returns:
        peer's response dict, or None on failure
    """
    import urllib.request
    import urllib.error

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            peer_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.debug(f"Gossip fetch from {peer_url} failed: {e}")
        return None


class GossipModule:
    """Gossip learning module for async peer-to-peer model exchange.

    Attributes:
        local_model: reference to local DeepRiskNet model
        get_peers: callable returning list of (peer_id, http_url) tuples
        gossip_interval: seconds between gossip cycles
        http_port: local HTTP server port (for building peer URL)
        enabled: controlled by start/stop
    """

    def __init__(
        self,
        local_model: DeepRiskNet,
        get_peers: Callable[[], List[tuple[str, str]]],
        http_port: int = 9833,
        node_id: str = "",
        gossip_interval: int = DEFAULT_GOSSIP_INTERVAL,
        mix_ratio: float = 0.5,
        validation_set: Optional[List[Tuple[List[float], float]]] = None,
        reputation: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.local_model = local_model
        self.get_peers = get_peers
        self.http_port = http_port
        self.node_id = node_id
        self.gossip_interval = gossip_interval
        self.mix_ratio = mix_ratio  # 0.5 = equal mix, >0.5 = favour local

        # Validation set for BALANCE + LPC defences
        self._validation_set: List[Tuple[List[float], float]] = validation_set or []
        self._validation_max = 200  # sliding window max
        self._lpc_min_accuracy = 0.4  # baseline for LPC

        # Reputation tracking: peer_id -> {"success": int, "fail": int, "last_seen": float, "tribe": str}
        self._reputation: Dict[str, Dict[str, Any]] = reputation or {}
        self._reputation_lock = threading.Lock()
        self._reputation_alpha = 0.99  # start trusting all equally
        self._reputation_min_exchanges = 3  # after 3 exchanges, start weighting

        # Delta Sum: per-peer weight baselines for delta compression
        self._peer_baselines: Dict[str, dict] = {}
        self._delta_enabled = True  # toggle delta sum on/off

        # Feature tribe: model signature hash for natural differentiation
        self._tribe: str = ""
        self._tribe_history: List[str] = []  # last 3 tribe values
        self._tribe_similarity_threshold = 0.85  # cosine sim threshold for same tribe

        # Runtime state
        self._enabled = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._gossip_count = 0
        self._last_gossip_time = 0.0
        self._peer_models_received = 0
        self._last_peer: str = ""
        self._history: List[Dict[str, Any]] = []
        self._rejected_by_balance = 0
        self._rejected_by_lpc = 0

        # Relay (optional — set up via enable_relay())
        self.relay: Optional["RelayHub"] = None

    # ── Lifecycle ──

    def enable_relay(self, public_endpoint: str = "",
                     gossip_url: str = "", punch_port: int = 0,
                     punch_interval: int = 120):
        """Enable relay mode: this node acts as a relay for private peers.

        Also starts the background DCUtR daemon for TCP hole-punching
        and connection migration.

        Args:
            public_endpoint: public "ip:port" for this node
            gossip_url: full "http://ip:port" URL
            punch_port: TCP listener port for direct connections (0 = auto)
            punch_interval: seconds between punch retry rounds
        """
        self.relay = RelayHub(
            node_id=self.node_id,
            public_endpoint=public_endpoint or f"0.0.0.0:{self.http_port}",
            gossip_url=gossip_url or f"http://0.0.0.0:{self.http_port}",
        )
        # Set punch callback for connection migration
        self.relay.set_punch_callback(self._on_direct_established)
        # Start DCUtR daemon
        self.relay.start_punch_daemon(port=punch_port, interval=punch_interval)

    def _on_direct_established(self, peer_id: str, state: str,
                                direct_url: str):
        """Callback: connection migrated to/from direct."""
        if state == "direct_established":
            logger.info(f"🔗 Connection migration: {peer_id[:12]} → DIRECT "
                         f"({direct_url})")
        elif state == "direct_lost":
            logger.info(f"🌐 Connection migration: {peer_id[:12]} → RELAY")

    def start(self) -> None:
        """Start gossip background thread."""
        if self._enabled:
            return
        self._enabled = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="gossip-loop",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"📡 Gossip module started (interval={self.gossip_interval}s, "
                     f"port={self.http_port}, mix_ratio={self.mix_ratio})")

    def stop(self):
        """Stop gossip background thread."""
        self._enabled = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("📡 Gossip module stopped")

    @property
    def running(self) -> bool:
        return self._enabled and (self._thread is not None)

    # ── Tribe / Reputation / Peer Selection ──

    def _compute_tribe(self) -> str:
        """
        Compute a tribe signature from the current model weights.
        Uses weight binarisation: each layer's first principal direction
        becomes a single bit.  With 8 parameter groups → 8-bit tribe id.
        """
        flat = flatten_weights(self.local_model)
        if not flat:
            return "unknown"

        # Divide weights into 8 regions, take sign of mean as a bit
        bits = []
        chunk = max(1, len(flat) // 8)
        for i in range(8):
            seg = flat[i * chunk: (i + 1) * chunk]
            bits.append("1" if sum(seg) / len(seg) > 0 else "0")
        return "".join(bits)

    def _update_tribe(self):
        """Recalc tribe signature and push to history."""
        self._tribe = self._compute_tribe()
        self._tribe_history.append(self._tribe)
        if len(self._tribe_history) > 3:
            self._tribe_history.pop(0)

    def _tribe_similarity(self, peer_flat: List[float]) -> float:
        """Cosine similarity between local weights and peer flat vector."""
        local_flat = flatten_weights(self.local_model)
        min_len = min(len(local_flat), len(peer_flat))
        if min_len < 2:
            return 0.0
        a = local_flat[:min_len]
        b = peer_flat[:min_len]
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na * nb == 0:
            return 0.0
        return dot / (na * nb)

    def _select_peer_reputation(self, peers: List[tuple[str, str]]) -> tuple[str, str]:
        """
        Select peer with reputation-weighted random sampling.
        New/unseen peers get a default score of 0.5.
        After min_exchanges exchanges, reputation dominates.
        """
        with self._reputation_lock:
            scores = []
            for pid, url in peers:
                rep = self._reputation.get(pid, {})
                s = rep.get("success", 0)
                f = rep.get("fail", 0)
                total = s + f
                if total < self._reputation_min_exchanges:
                    # New or low-info peer: score = 0.5 (neutral)
                    score = 0.5
                else:
                    # Reputation = success ratio
                    score = s / max(1, total)

                # Tribe bonus: prefer same-tribe peers
                peer_tribe = rep.get("tribe", "")
                if peer_tribe and peer_tribe == self._tribe:
                    score = min(1.0, score * 1.2)  # 20% bonus

                scores.append((pid, url, score))

        # Weighted random choice
        total_w = max(0.01, sum(s for _, _, s in scores))
        r = random.uniform(0, total_w)
        cumulative = 0.0
        for pid, url, score in scores:
            cumulative += score
            if r <= cumulative:
                return pid, url
        return random.choice(peers)  # fallback

    def _record_gossip_outcome(self, peer_id: str, success: bool):
        """Update reputation after a gossip exchange."""
        with self._reputation_lock:
            rep = self._reputation.setdefault(peer_id, {"success": 0, "fail": 0,
                                                         "last_seen": 0.0, "tribe": ""})
            if success:
                rep["success"] += 1
            else:
                rep["fail"] += 1
            rep["last_seen"] = time.time()
            rep["tribe"] = self._tribe  # record our tribe at time of exchange

    def _get_shareable_reputation(self) -> Dict[str, Dict[str, Any]]:
        """Return top-N reputation entries for gossip trust propagation."""
        with self._reputation_lock:
            items = sorted(
                self._reputation.items(),
                key=lambda x: x[1]["success"] + x[1]["fail"],
                reverse=True,
            )[:MAX_REPUTATION_SHARE]
            return dict(items)

    def _merge_reputation(self, incoming: Dict[str, Dict[str, Any]], source_peer: str):
        """
        Merge incoming reputation data from a gossip peer.
        Trust propagates: if peer A trusts B, and we gossip with A,
        we learn to also trust B (with lower weight).
        """
        if not incoming:
            return
        with self._reputation_lock:
            for pid, rep in incoming.items():
                if pid == self.node_id:
                    continue  # don't trust our own reputation from others
                local = self._reputation.setdefault(pid, {"success": 0, "fail": 0,
                                                           "last_seen": 0.0, "tribe": ""})
                # Propagated trust has half weight
                s = rep.get("success", 0)
                f = rep.get("fail", 0)
                local["success"] += s // 2
                local["fail"] += f // 2
                local["last_seen"] = max(local["last_seen"], rep.get("last_seen", 0))
                if rep.get("tribe"):
                    local["tribe"] = rep["tribe"]

    # ── Background loop ──

    def _run_loop(self):
        """Main gossip loop — runs in background thread."""
        while self._enabled:
            try:
                self.tick()
            except Exception as e:
                logger.error(f"Gossip tick error: {e}")
            time.sleep(self.gossip_interval)

    def tick(self) -> Dict[str, Any]:
        """Execute one gossip cycle with reputation selection + BALANCE+LPC gating.

        Returns:
            gossip result dict
        """
        result = {
            "tick": self._gossip_count,
            "timestamp": time.time(),
            "gossiped": False,
            "peer": "",
            "reason": "",
        }

        peers = self.get_peers()
        if len(peers) < MIN_PEERS:
            result["reason"] = f"insufficient peers ({len(peers)} < {MIN_PEERS})"
            return result

        # Filter self
        peers = [(pid, url) for pid, url in peers if pid != self.node_id]
        if not peers:
            result["reason"] = "no peers (excluding self)"
            return result

        # Reputation-weighted peer selection (Phase 6)
        peer_id, peer_url = self._select_peer_reputation(peers)
        gossip_url = f"{peer_url.rstrip('/')}/gossip/model"

        # Update tribe before sending
        self._update_tribe()

        # Build our payload with delta compression + reputation
        with self._lock:
            raw_params = self.local_model.to_dict()["params"]
            use_delta = self._delta_enabled and peer_id in self._peer_baselines

            if use_delta:
                delta = compute_weight_delta(raw_params, self._peer_baselines[peer_id])
                compressed = compress_weights(delta)
                payload = {
                    "node_id": self.node_id,
                    "arch_fingerprint": "DeepRiskNet-32-64-v1",
                    "params": compressed,
                    "gossip_version": "v3",
                    "delta": True,
                    "timestamp": time.time(),
                    "reputation": self._get_shareable_reputation(),
                }
            else:
                compressed = compress_weights(raw_params)
                payload = {
                    "node_id": self.node_id,
                    "arch_fingerprint": "DeepRiskNet-32-64-v1",
                    "params": compressed,
                    "gossip_version": "v3",
                    "delta": False,
                    "timestamp": time.time(),
                    "reputation": self._get_shareable_reputation(),
                }

            # Store baseline for future delta
            self._peer_baselines[peer_id] = raw_params

        # Send and receive
        resp = _fetch_model_from_peer(gossip_url, payload)

        if resp is None:
            self._record_gossip_outcome(peer_id, False)
            result["reason"] = f"peer {peer_id[:12]} unreachable"
            return result

        # Merge incoming reputation (trust propagation)
        self._merge_reputation(resp.get("reputation", {}), peer_id)

        # Extract peer's weights (decompress + delta if needed)
        peer_params_raw = resp.get("params")
        if not peer_params_raw:
            self._record_gossip_outcome(peer_id, False)
            result["reason"] = "empty response from peer"
            return result

        # Decode: v3 supports delta; v2 = INT8 full; v1 = raw JSON
        gv = resp.get("gossip_version", "v1")
        is_delta = resp.get("delta", False) and gv == "v3"
        template = self.local_model.to_dict()["params"]

        if is_delta:
            # Delta Sum: decompress INT8 delta, add to stored baseline
            delta_params = decompress_weights(peer_params_raw, template)
            if peer_id in self._peer_baselines:
                peer_params = apply_weight_delta(self._peer_baselines[peer_id], delta_params)
            else:
                peer_params = delta_params
        elif gv in ("v2", "v3"):
            peer_params = decompress_weights(peer_params_raw, template)
        else:
            peer_params = peer_params_raw

        # Store baseline for peer (also used for our future sends)
        self._peer_baselines[peer_id] = peer_params

        # Check architecture
        peer_fp = resp.get("arch_fingerprint", "")
        if peer_fp and peer_fp != "DeepRiskNet-32-64-v1":
            self._record_gossip_outcome(peer_id, False)
            result["reason"] = f"arch mismatch: {peer_fp}"
            return result

        # Build peer model
        try:
            template = self.local_model.to_dict()
            template["params"] = peer_params
            peer_model = DeepRiskNet.from_dict(template)
        except Exception as e:
            self._record_gossip_outcome(peer_id, False)
            result["reason"] = f"model parse failed: {e}"
            return result

        # ── Phase 5: BALANCE + LPC defence gating ──

        # BALANCE accuracy-gap: peer must not be worse than local
        if self._validation_set:
            bal_ok, local_acc = balancer_protection(
                peer_model, self.local_model,
                self._validation_set, threshold=0.05,
            )
            if not bal_ok:
                self._rejected_by_balance += 1
                self._record_gossip_outcome(peer_id, False)
                result["reason"] = f"BALANCE gap blocked (local_acc={local_acc:.3f})"
                return result

            # BALANCE gradient-direction: reject strongly opposing directions
            grad_ok, cos_sim = balance_gradient_defense(
                peer_model, self.local_model,
                self._validation_set, threshold=-0.1,
            )
            if not grad_ok:
                self._rejected_by_balance += 1
                self._record_gossip_outcome(peer_id, False)
                result["reason"] = f"BALANCE gradient blocked (cos_sim={cos_sim:.3f})"
                return result

            # LPC: peer must meet baseline accuracy on local data
            if not local_validation_check(peer_model, self._validation_set,
                                          min_accuracy=self._lpc_min_accuracy):
                self._rejected_by_lpc += 1
                self._record_gossip_outcome(peer_id, False)
                result["reason"] = "LPC blocked (peer accuracy below threshold)"
                return result

        # ── Phase 5: Multi-party robust aggregation (when 3+ peers available) ──
        # Even though we only exchange with one peer, we aggregate local + peer
        # with weighted average. For multi-peer scenarios, gossip iterates.

        # FedAvg: combine local + peer
        with self._lock:
            local_mix = self.mix_ratio
            peer_mix = 1.0 - local_mix
            merged = aggregate_forest(
                [self.local_model, peer_model],
                method="weighted_average",
                weights=[local_mix, peer_mix],
            )

            # Overwrite local model weights
            merged_d = merged.to_dict()
            local_d = self.local_model.to_dict()
            for key in merged_d["params"]:
                local_d["params"][key] = merged_d["params"][key]
            new_local = DeepRiskNet.from_dict(local_d)
            self.local_model.__dict__.update(new_local.__dict__)

            self._gossip_count += 1
            self._last_gossip_time = time.time()
            self._last_peer = peer_id
            self._peer_models_received += 1

        # Update tribe after merge
        self._update_tribe()

        # Record success
        self._record_gossip_outcome(peer_id, True)

        # Record history
        self._history.append({
            "tick": self._gossip_count,
            "peer": peer_id[:12],
            "timestamp": time.time(),
            "tribe": self._tribe,
        })
        while len(self._history) > HISTORY_SIZE:
            self._history.pop(0)

        logger.info(f"🔄 Gossip #{self._gossip_count} with {peer_id[:12]}: "
                     f"local={local_mix:.2f} peer={peer_mix:.2f} tribe={self._tribe}")

        result["gossiped"] = True
        result["peer"] = peer_id
        result["mix"] = self.mix_ratio
        result["tribe"] = self._tribe
        return result

    # ── HTTP handler (peer-to-peer /gossip/model endpoint) ──

    def handle_gossip_request(self, body: dict) -> dict:
        """Handle incoming gossip request from a peer.

        Called by HTTP server when receiving POST /gossip/model.

        Args:
            body: peer's model payload

        Returns:
            our local model weights (to send back)
        """
        # Decompress incoming params if compressed
        peer_params_raw = body.get("params")
        if not peer_params_raw:
            return {"error": "no params"}

        peer_id = body.get("node_id", "unknown")
        gv = body.get("gossip_version", "v1")
        is_delta = body.get("delta", False) and gv == "v3"
        template = self.local_model.to_dict()["params"]

        if is_delta:
            delta_params = decompress_weights(peer_params_raw, template)
            if peer_id in self._peer_baselines:
                peer_params = apply_weight_delta(self._peer_baselines[peer_id], delta_params)
            else:
                peer_params = delta_params
        elif gv in ("v2", "v3"):
            peer_params = decompress_weights(peer_params_raw, template)
        else:
            peer_params = peer_params_raw

        # Store peer's baseline
        self._peer_baselines[peer_id] = peer_params

        # Merge incoming reputation
        self._merge_reputation(body.get("reputation", {}), peer_id)

        peer_fp = body.get("arch_fingerprint", "")
        if peer_fp and peer_fp != "DeepRiskNet-32-64-v1":
            return {"error": f"arch mismatch: {peer_fp}"}

        peer_id = body.get("node_id", "unknown")

        # Build peer model
        try:
            template = self.local_model.to_dict()
            template["params"] = peer_params
            peer_model = DeepRiskNet.from_dict(template)
        except Exception as e:
            return {"error": f"model parse failed: {e}"}

        # BALANCE + LPC defence on incoming peer model
        rejected = False
        if self._validation_set:
            bal_ok, _ = balancer_protection(
                peer_model, self.local_model,
                self._validation_set, threshold=0.1,  # looser on incoming
            )
            if not bal_ok:
                self._rejected_by_balance += 1
                self._record_gossip_outcome(peer_id, False)
                logger.debug(f"BALANCE rejected incoming from {peer_id[:12]}")
                rejected = True

            if not rejected and not local_validation_check(
                    peer_model, self._validation_set,
                    min_accuracy=self._lpc_min_accuracy):
                self._rejected_by_lpc += 1
                self._record_gossip_outcome(peer_id, False)
                logger.debug(f"LPC rejected incoming from {peer_id[:12]}")
                rejected = True

        if not rejected:
            # FedAvg: combine local + peer (on incoming, mix local more)
            with self._lock:
                local_mix = 0.6
                peer_mix = 0.4
                merged = aggregate_forest(
                    [self.local_model, peer_model],
                    method="weighted_average",
                    weights=[local_mix, peer_mix],
                )

                merged_d = merged.to_dict()
                local_d = self.local_model.to_dict()
                for key in merged_d["params"]:
                    local_d["params"][key] = merged_d["params"][key]
                new_local = DeepRiskNet.from_dict(local_d)
                self.local_model.__dict__.update(new_local.__dict__)

                self._gossip_count += 1
                self._last_peer = peer_id
                self._peer_models_received += 1

            self._record_gossip_outcome(peer_id, True)
            self._update_tribe()
            logger.info(f"🔄 Gossip received from {peer_id[:12]}: "
                         f"local=0.60 peer=0.40 tribe={self._tribe}")

        # Return our (now merged) model weights with compression + reputation
        with self._lock:
            raw_return = self.local_model.to_dict()["params"]
            # Send delta if this peer has a baseline with us
            if self._delta_enabled and peer_id in self._peer_baselines:
                delta = compute_weight_delta(raw_return, self._peer_baselines[peer_id])
                return_data = {
                    "node_id": self.node_id,
                    "arch_fingerprint": "DeepRiskNet-32-64-v1",
                    "params": compress_weights(delta),
                    "gossip_version": "v3",
                    "delta": True,
                    "timestamp": time.time(),
                    "reputation": self._get_shareable_reputation(),
                }
            else:
                return_data = {
                    "node_id": self.node_id,
                    "arch_fingerprint": "DeepRiskNet-32-64-v1",
                    "params": compress_weights(raw_return),
                    "gossip_version": "v3",
                    "delta": False,
                    "timestamp": time.time(),
                    "reputation": self._get_shareable_reputation(),
                }
            # Update baseline
            self._peer_baselines[peer_id] = raw_return
        return return_data

    def inject_route(self, handler_class) -> None:
        """Inject /gossip/model route into an HTTP server handler class.

        Usage:
            gossip.inject_route(MyHTTPHandler)
            # Now POST /gossip/model calls gossip.handle_gossip_request()
        """
        orig_do_POST = getattr(handler_class, "do_POST", None)
        gossip_ref = self

        def do_POST_with_gossip(handler_self):
            path = handler_self.path
            if path == "/gossip/model":
                content_length = int(handler_self.headers.get("Content-Length", 0))
                body = handler_self.rfile.read(content_length)
                try:
                    data = json.loads(body.decode("utf-8"))
                    response = gossip_ref.handle_gossip_request(data)
                    resp_body = json.dumps(response).encode("utf-8")
                    handler_self.send_response(200)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(resp_body)))
                    handler_self.end_headers()
                    handler_self.wfile.write(resp_body)
                except Exception as e:
                    error = json.dumps({"error": str(e)}).encode("utf-8")
                    handler_self.send_response(400)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(error)))
                    handler_self.end_headers()
                    handler_self.wfile.write(error)
            elif orig_do_POST:
                orig_do_POST(handler_self)
            else:
                handler_self.send_response(405)

        handler_class.do_POST = do_POST_with_gossip

        # ── Relay routes (if relay mode is enabled) ──
        if self.relay:
            relay_ref = self.relay
            gossip_ref = self

            # This wraps both gossip AND relay into a single replacement
            # to avoid double-reading the HTTP body.  The order is:
            #   relay path  → handle_register/handle_introduce/…
            #   /gossip/model → handle_gossip_request
            #   fallback    → original do_POST (if any)
            orig_POST = getattr(handler_class, "do_POST", None)

            def do_POST_all(handler_self):
                path = handler_self.path
                content_length = int(handler_self.headers.get("Content-Length", 0))
                body = handler_self.rfile.read(content_length)
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception:
                    data = {}

                # Relay routes
                if path == "/relay/register":
                    resp = relay_ref.handle_register(data)
                elif path == "/relay/heartbeat":
                    resp = relay_ref.handle_heartbeat(data)
                elif path == "/relay/introduce":
                    resp = relay_ref.handle_introduce(data)
                elif path == "/relay/hole-punch":
                    resp = relay_ref.handle_hole_punch(data)
                elif path == "/relay/tcp-punch":
                    resp = relay_ref.handle_tcp_punch(data)
                elif path == "/relay/forward":
                    resp = relay_ref.handle_forward(data, gossip_ref,
                                                     gossip_ref.http_port)
                # Gossip route
                elif path == "/gossip/model":
                    resp = gossip_ref.handle_gossip_request(data)
                else:
                    resp = None

                if resp is not None:
                    resp_body = json.dumps(resp).encode("utf-8")
                    handler_self.send_response(200)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.send_header("Content-Length", str(len(resp_body)))
                    handler_self.end_headers()
                    handler_self.wfile.write(resp_body)
                elif orig_POST:
                    orig_POST(handler_self)
                else:
                    handler_self.send_response(405)

            handler_class.do_POST = do_POST_all

    # ── Integration ──

    def set_mix_ratio(self, ratio: float):
        """Set local model weight in gossip mixing.

        0.5 = equal mix (standard FedAvg)
        >0.5 = favour local model (conservative)
        <0.5 = favour peer model (aggressive)
        """
        self.mix_ratio = max(0.1, min(0.9, ratio))

    # ── Stats ──

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "gossip_count": self._gossip_count,
            "last_gossip_time": self._last_gossip_time,
            "last_peer": self._last_peer[:12] if self._last_peer else "",
            "peer_models_received": self._peer_models_received,
            "interval_s": self.gossip_interval,
            "mix_ratio": self.mix_ratio,
            "tribe": self._tribe,
            "tribe_history": self._tribe_history,
            "reputation_peers": len(self._reputation),
            "reputation_propagated": sum(
                1 for pid in self._reputation
                if pid != self.node_id and pid not in
                {p for p, _ in self.get_peers()}
            ),
            "rejected_by_balance": self._rejected_by_balance,
            "rejected_by_lpc": self._rejected_by_lpc,
            "validation_size": len(self._validation_set),
            "history": self._history[-3:],
            "relay": self.relay.stats() if self.relay else None,
        }


# ════════════════════════════════════════════════════════════════
#  RelayHub — Full DCUtR (TCP hole-punch + connection migration)
# ════════════════════════════════════════════════════════════════

RELAY_TIMEOUT = 300  # drop relay clients after 5 min silence


class RelayHub:
    """
    Full DCUtR relay with connection migration for private-to-private gossip.

    Two-layer state machine per peer:
      UNKNOWN → RELAY_ONLY → PUNCHING → DIRECT (or back to RELAY_ONLY)

    Flow:
      1. Node registers with relay → gets peer list → RELAY_ONLY
      2. Background daemon attempts TCP hole-punching
      3. On TCP connect → DIRECT → gossip uses direct URL
      4. On direct failure → fallback to RELAY_ONLY, retry later
      5. Established connections are periodically health-checked

    Thread-safe: all state mutations under self._lock.
    """

    def __init__(self, node_id: str, public_endpoint: str = "",
                 gossip_url: str = ""):
        self.node_id = node_id
        self.public_endpoint = public_endpoint  # "ip:port" or "" if unknown
        self.gossip_url = gossip_url  # "http://ip:port"
        self._clients: Dict[str, dict] = {}  # node_id → client info
        self._lock = threading.Lock()
        self._forwarded = 0
        self._introductions = 0

        # ── DCUtR state ──
        self._punched: Dict[str, dict] = {}  # peer_id → DCUtR state
        self._punch_listener_port = 0
        self._punch_listener: Any = None
        self._daemon_running = False
        self._daemon_thread: Optional[threading.Thread] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._punch_callback: Optional[Callable] = None

    def set_punch_callback(self, callback: Callable):
        """Set callback for connection migration events.

        Args:
            callback: fn(peer_id, state, direct_url) where state is
                      "direct_established" | "direct_lost"
        """
        self._punch_callback = callback

    def start_punch_daemon(self, port: int = 0,
                           interval: int = 120) -> bool:
        """Start background DCUtR daemon (TCP listener + punch loop).

        Args:
            port: TCP listener port for incoming punches (0 = auto)
            interval: seconds between punch retry rounds

        Returns: True if started, False if already running or no public endpoint
        """
        if self._daemon_running:
            return False

        self._punch_listener_port = port

        # Start TCP listener in background thread
        self._listener_thread = threading.Thread(
            target=self._tcp_listener_loop,
            name="dcutr-listener",
            daemon=True,
        )
        self._listener_thread.start()

        # Wait briefly for listener to bind
        time.sleep(0.1)
        if self._punch_listener_port == 0:
            logger.warning("DCUtR listener failed to bind — punch disabled")
            return False

        # Start punch loop daemon
        self._daemon_running = True
        self._daemon_thread = threading.Thread(
            target=self._punch_loop,
            name="dcutr-daemon",
            daemon=True,
            kwargs={"interval": interval},
        )
        self._daemon_thread.start()

        logger.info(f"🔌 DCUtR daemon started (listener port={self._punch_listener_port}, "
                     f"interval={interval}s)")
        return True

    def stop_punch_daemon(self):
        self._daemon_running = False
        if self._daemon_thread:
            self._daemon_thread.join(timeout=5)
            self._daemon_thread = None
        if self._listener_thread:
            self._listener_thread.join(timeout=2)
            self._listener_thread = None
        if self._punch_listener:
            try:
                self._punch_listener.close()
            except Exception:
                pass
            self._punch_listener = None

    # ── TCP listener (accepts incoming punch connections) ──

    def _tcp_listener_loop(self):
        """Background thread: listen for incoming direct TCP connections."""
        import socket as _sock
        try:
            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            sock.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self._punch_listener_port))
            sock.listen(5)
            sock.settimeout(5.0)  # allow periodic check for daemon stop
            self._punch_listener_port = sock.getsockname()[1]
            self._punch_listener = sock

            while self._daemon_running:
                try:
                    conn, addr = sock.accept()
                    conn.settimeout(10.0)
                    # Handle incoming direct connection in a temp thread
                    t = threading.Thread(
                        target=self._handle_direct_incoming,
                        args=(conn, addr),
                        daemon=True,
                    )
                    t.start()
                except _sock.timeout:
                    continue
                except OSError:
                    break
        except Exception as e:
            logger.warning(f"DCUtR listener failed: {e}")
        finally:
            if self._punch_listener:
                try:
                    self._punch_listener.close()
                except Exception:
                    pass
                self._punch_listener = None

    def _handle_direct_incoming(self, conn, addr):
        """Handle an incoming direct TCP connection.

        Expects a JSON handshake, responds with ack.
        """
        import socket as _sock
        try:
            data = conn.recv(4096)
            handshake = json.loads(data.decode("utf-8"))
            peer_id = handshake.get("node_id", "")
            peer_gossip_url = handshake.get("gossip_url", "")
            punch_id = handshake.get("punch_id", "")

            # Send ack
            ack = json.dumps({
                "type": "direct_ack",
                "node_id": self.node_id,
                "punch_id": punch_id,
            }).encode("utf-8")
            conn.sendall(ack)

            # Record as direct connection
            direct_url = f"http://{addr[0]}:{addr[1]}"
            # Extract actual gossip URL from handshake, default to IP-based
            if peer_gossip_url:
                direct_gossip_url = peer_gossip_url
            else:
                direct_gossip_url = f"http://{addr[0]}:{addr[1]}"

            with self._lock:
                self._punched[peer_id] = {
                    "state": "DIRECT",
                    "direct_url": direct_gossip_url,
                    "last_verified": time.time(),
                    "attempts": 0,
                    "punch_id": punch_id,
                }

            # Notify callback
            if self._punch_callback:
                try:
                    self._punch_callback(peer_id, "direct_established",
                                         direct_gossip_url)
                except Exception:
                    pass

            logger.info(f"✅ DCUtR direct connection established with "
                         f"{peer_id[:12]} via {addr[0]}")
        except Exception as e:
            logger.debug(f"DCUtR incoming handle failed from {addr}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── TCP hole-punch attempt (outgoing) ──

    def attempt_tcp_punch(self, peer_id: str,
                          relay_url: str = "") -> bool:
        """Attempt TCP hole-punch to a peer via relay coordination.

        Args:
            peer_id: target node id
            relay_url: relay's base URL for coordination

        Returns: True if direct connection established
        """
        import socket as _sock
        if not self._punch_listener_port:
            self._punch_listener_port = 9835
            logger.warning("Punch listener not started, using default port 9835")

        # Get peer's info via relay coordination
        peer_endpoint = ""
        with self._lock:
            info = self._clients.get(peer_id)
            if info:
                peer_endpoint = info.get("endpoint", "")

        if not peer_endpoint:
            # Try to get from relay
            if relay_url:
                try:
                    import urllib.request as _urllib
                    import urllib.error as _urlerr
                    req_body = json.dumps({
                        "node_id": self.node_id,
                        "target_id": peer_id,
                    }).encode("utf-8")
                    req = _urllib.Request(
                        relay_url.rstrip("/") + "/relay/tcp-punch",
                        data=req_body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with _urllib.urlopen(req, timeout=15) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                    if result.get("ok"):
                        # Both sides now have each other's info
                        peer_endpoint = result.get("target", {}).get("endpoint", "")
                    else:
                        logger.debug(f"DCUtR relay punch coord failed: "
                                     f"{result.get('error')}")
                        return False
                except Exception as e:
                    logger.debug(f"DCUtR relay punch coord error: {e}")
                    return False

        if not peer_endpoint:
            return False

        # Parse peer endpoint
        try:
            host, port_str = peer_endpoint.rsplit(":", 1)
            peer_host = host.strip("[]")
            peer_port = int(port_str)
        except (ValueError, AttributeError):
            return False

        # Generate a unique punch_id for correlation
        punch_id = f"{self.node_id}:{time.time():.6f}"

        # Attempt TCP simultaneous open
        sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect((peer_host, peer_port))
            # Send handshake
            handshake = json.dumps({
                "type": "direct_punch",
                "node_id": self.node_id,
                "gossip_url": self.gossip_url,
                "punch_id": punch_id,
            }).encode("utf-8")
            sock.sendall(handshake)

            # Wait for ack
            resp_data = sock.recv(4096)
            resp = json.loads(resp_data.decode("utf-8"))
            if resp.get("type") == "direct_ack":
                with self._lock:
                    self._punched[peer_id] = {
                        "state": "DIRECT",
                        "direct_url": f"http://{peer_host}:{peer_port}",
                        "last_verified": time.time(),
                        "attempts": 0,
                        "punch_id": punch_id,
                    }
                if self._punch_callback:
                    try:
                        self._punch_callback(
                            peer_id, "direct_established",
                            f"http://{peer_host}:{peer_port}")
                    except Exception:
                        pass
                logger.info(f"✅ DCUtR direct connection to {peer_id[:12]} "
                             f"({peer_host}:{peer_port})")
                sock.close()
                return True
        except (_sock.timeout, OSError, json.JSONDecodeError) as e:
            logger.debug(f"DCUtR punch to {peer_host}:{peer_port}: {e}")
        finally:
            try:
                sock.close()
            except Exception:
                pass
        return False

    # ── Background daemon ──

    def _punch_loop(self, interval: int = 120):
        """Background loop: retry hole-punch + verify direct connections."""
        while self._daemon_running:
            try:
                self._punch_tick()
            except Exception as e:
                logger.error(f"DCUtR punch tick error: {e}")
            time.sleep(interval)

    def _punch_tick(self):
        """Execute one DCUtR maintenance round."""
        now = time.time()

        # Collect peers in RELAY_ONLY state that need punching
        relay_only = []
        with self._lock:
            for peer_id, info in self._clients.items():
                if peer_id == self.node_id:
                    continue
                if now - info.get("last_seen", 0) >= RELAY_TIMEOUT:
                    continue

                pstate = self._punched.get(peer_id, {})
                state = pstate.get("state", "UNKNOWN")
                if state not in ("DIRECT", "PUNCHING", "FAILED"):
                    relay_only.append(peer_id)

        # Try to punch each relay-only peer
        for peer_id in relay_only:
            if not self._daemon_running:
                break

            # Mark as PUNCHING to avoid concurrent attempts
            with self._lock:
                self._punched[peer_id] = {
                    **self._punched.get(peer_id, {}),
                    "state": "PUNCHING",
                    "attempts": self._punched.get(peer_id, {}).get("attempts", 0) + 1,
                    "last_attempt": now,
                }

            # Get relay_url from client entry
            relay_url = ""
            with self._lock:
                info = self._clients.get(peer_id)
                if info:
                    relay_url = info.get("gossip_url", "")

            success = self.attempt_tcp_punch(peer_id, relay_url)

            with self._lock:
                if success:
                    self._punched[peer_id]["state"] = "DIRECT"
                    self._punched[peer_id]["attempts"] = 0
                else:
                    attempts = self._punched[peer_id].get("attempts", 0)
                    if attempts >= 5:
                        self._punched[peer_id]["state"] = "FAILED"
                    else:
                        self._punched[peer_id]["state"] = "RELAY_ONLY"

        # Verify existing direct connections
        to_verify = []
        with self._lock:
            for peer_id, pstate in list(self._punched.items()):
                if pstate.get("state") == "DIRECT":
                    if now - pstate.get("last_verified", 0) > interval:
                        to_verify.append(peer_id)

        for peer_id in to_verify:
            if not self._daemon_running:
                break
            pstate = self._punched.get(peer_id, {})
            direct_url = pstate.get("direct_url", "")
            if direct_url:
                alive = self._check_direct_alive(peer_id, direct_url)
                with self._lock:
                    if alive:
                        self._punched[peer_id]["last_verified"] = now
                    else:
                        self._punched[peer_id]["state"] = "RELAY_ONLY"
                        self._punched[peer_id]["direct_url"] = ""
                        self._punched[peer_id]["attempts"] = 0
                        if self._punch_callback:
                            try:
                                self._punch_callback(peer_id, "direct_lost", "")
                            except Exception:
                                pass
                        logger.info(f"❌ DCUtR direct lost with {peer_id[:12]}, "
                                     f"falling back to relay")

    def _check_direct_alive(self, peer_id: str, direct_url: str) -> bool:
        """Check if a direct connection is still alive via a lightweight ping."""
        import socket as _sock
        try:
            # Parse host:port from direct_url
            url = direct_url.replace("http://", "").replace("https://", "")
            host, port_str = url.rsplit(":", 1)
            port_str = port_str.split("/")[0]
            peer_port = int(port_str)

            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            sock.settimeout(3.0)
            sock.connect((host.strip("[]"), peer_port))
            # Quick handshake
            ping = json.dumps({
                "type": "direct_ping",
                "node_id": self.node_id,
            }).encode("utf-8")
            sock.sendall(ping)
            data = sock.recv(1024)
            sock.close()
            return b"direct_pong" in data
        except Exception:
            return False

    # ── HTTP handlers ──

    def handle_tcp_punch(self, body: dict) -> dict:
        """POST /relay/tcp-punch — coordinate TCP simultaneous open.

        Relay tells both peers about each other so they can
        simultaneously connect.
        """
        node_id = body.get("node_id", "")
        target_id = body.get("target_id", "")

        with self._lock:
            caller_info = self._clients.get(node_id)
            target_info = self._clients.get(target_id)

            if caller_info is None or target_info is None:
                return {"ok": False, "error": "one or both nodes not registered"}

            now = time.time()
            if (now - caller_info.get("last_seen", 0) >= RELAY_TIMEOUT or
                    now - target_info.get("last_seen", 0) >= RELAY_TIMEOUT):
                return {"ok": False, "error": "one or both nodes stale"}

            # Also push a notification to target so they start listening
            # (they may already have a listener, but this ensures it)
            target_punch_port = target_info.get("punch_port",
                                                 self._punch_listener_port)

            self._introductions += 1

            return {
                "ok": True,
                "caller": {
                    "node_id": node_id,
                    "endpoint": caller_info.get("endpoint", ""),
                    "gossip_url": caller_info.get("gossip_url", ""),
                },
                "target": {
                    "node_id": target_id,
                    "endpoint": target_info.get("endpoint", ""),
                    "gossip_url": target_info.get("gossip_url", ""),
                },
                "punch_port": target_punch_port,
            }

    # ── Existing relay handlers ──

    def handle_register(self, body: dict) -> dict:
        """POST /relay/register — private node registers with this relay."""
        node_id = body.get("node_id", "")
        if not node_id:
            return {"ok": False, "error": "missing node_id"}
        with self._lock:
            self._clients[node_id] = {
                "node_id": node_id,
                "endpoint": body.get("endpoint", ""),
                "gossip_url": body.get("gossip_url", ""),
                "last_seen": time.time(),
                "udp_port": body.get("udp_port", 0),
                "punch_port": body.get("punch_port", self._punch_listener_port),
            }
            # Return info we have about other clients (for hole-punching)
            other_clients = [
                {"node_id": cid, "endpoint": info["endpoint"],
                 "gossip_url": info["gossip_url"], "udp_port": info["udp_port"],
                 "punch_state": self._punched.get(cid, {}).get("state", "UNKNOWN")}
                for cid, info in self._clients.items()
                if cid != node_id and time.time() - info.get("last_seen", 0) < RELAY_TIMEOUT
            ]
        self._cleanup()
        return {"ok": True, "clients": other_clients, "count": len(other_clients)}

    def handle_heartbeat(self, body: dict) -> dict:
        """POST /relay/heartbeat — keep relay registration alive."""
        node_id = body.get("node_id", "")
        with self._lock:
            if node_id in self._clients:
                self._clients[node_id]["last_seen"] = time.time()
                # Return punch status for known peers
                punch_updates = {}
                for pid in self._clients:
                    if pid != node_id and pid in self._punched:
                        ps = self._punched[pid]
                        if ps.get("state") == "DIRECT":
                            punch_updates[pid] = {
                                "state": "DIRECT",
                                "direct_url": ps.get("direct_url", ""),
                            }
                return {"ok": True, "punch_updates": punch_updates}
        return {"ok": False, "error": "not registered"}

    def handle_introduce(self, body: dict) -> dict:
        """POST /relay/introduce — ask relay for another node's info."""
        target_id = body.get("target_id", "")
        with self._lock:
            info = self._clients.get(target_id)
            if info is None or time.time() - info.get("last_seen", 0) >= RELAY_TIMEOUT:
                return {"ok": False, "error": "target not found"}
            self._introductions += 1
            return {
                "ok": True,
                "target": info["endpoint"],
                "gossip_url": info.get("gossip_url", ""),
                "udp_port": info.get("udp_port", 0),
                "punch_state": self._punched.get(target_id, {}).get("state", "UNKNOWN"),
            }

    def handle_forward(self, body: dict, gossip_handler, http_port: int) -> dict:
        """POST /relay/forward — relay gossip data to a private peer."""
        target_id = body.get("to_id", "")
        caller_data = body.get("data", {})

        with self._lock:
            info = self._clients.get(target_id)
            if info is None:
                return {"ok": False, "error": "target not registered"}
            target_url = info.get("gossip_url", "")
            is_alive = time.time() - info.get("last_seen", 0) < RELAY_TIMEOUT
            if not is_alive:
                return {"ok": False, "error": "target unreachable"}

        # Check if there's a direct route
        direct_url = ""
        with self._lock:
            pstate = self._punched.get(target_id, {})
            if pstate.get("state") == "DIRECT":
                direct_url = pstate.get("direct_url", "")

        if direct_url:
            # Use direct connection instead of relay
            try:
                import urllib.request as _urllib
                peer_req = json.dumps({
                    "node_id": caller_data.get("node_id", ""),
                    "arch_fingerprint": caller_data.get("arch_fingerprint", ""),
                    "params": caller_data.get("params", {}),
                    "gossip_version": "v3",
                    "delta": caller_data.get("delta", False),
                    "timestamp": time.time(),
                    "direct": True,
                }).encode("utf-8")
                req = _urllib.Request(
                    direct_url.rstrip("/") + "/gossip/model",
                    data=peer_req,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _urllib.urlopen(req, timeout=15) as resp:
                    peer_response = json.loads(resp.read().decode("utf-8"))
                self._forwarded += 1
                return {"ok": True, "peer_response": peer_response, "direct": True}
            except Exception:
                # Direct failed, fallback to relay
                with self._lock:
                    self._punched[target_id] = {
                        **self._punched.get(target_id, {}),
                        "state": "RELAY_ONLY",
                        "direct_url": "",
                    }

        # Fallback to relay forwarding
        if target_url:
            import urllib.request as _urllib
            peer_req = json.dumps({
                "node_id": caller_data.get("node_id", ""),
                "arch_fingerprint": caller_data.get("arch_fingerprint", ""),
                "params": caller_data.get("params", {}),
                "gossip_version": "v3",
                "delta": caller_data.get("delta", False),
                "timestamp": time.time(),
                "relayed": True,
            }).encode("utf-8")

            try:
                req = _urllib.Request(
                    target_url.rstrip("/") + "/gossip/model",
                    data=peer_req,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _urllib.urlopen(req, timeout=15) as resp:
                    peer_response = json.loads(resp.read().decode("utf-8"))
                self._forwarded += 1
                return {"ok": True, "peer_response": peer_response}
            except Exception as e:
                logger.warning(f"Relay forward to {target_id[:12]} failed: {e}")
                return {"ok": False, "error": f"forward failed: {e}"}
        else:
            return {"ok": False, "error": "target has no gossip_url"}

    def handle_hole_punch(self, body: dict) -> dict:
        """POST /relay/hole-punch — trigger UDP hole-punching between two nodes."""
        caller_id = body.get("node_id", "")
        target_id = body.get("target_id", "")

        with self._lock:
            caller_info = self._clients.get(caller_id)
            target_info = self._clients.get(target_id)
            if caller_info is None or target_info is None:
                return {"ok": False, "error": "one or both nodes not registered"}

            now = time.time()
            if (now - caller_info["last_seen"] >= RELAY_TIMEOUT or
                    now - target_info["last_seen"] >= RELAY_TIMEOUT):
                return {"ok": False, "error": "one or both nodes stale"}

            self._introductions += 1
            return {
                "ok": True,
                "caller": {
                    "endpoint": caller_info["endpoint"],
                    "gossip_url": caller_info.get("gossip_url", ""),
                    "udp_port": caller_info.get("udp_port", 0),
                },
                "target": {
                    "endpoint": target_info["endpoint"],
                    "gossip_url": target_info.get("gossip_url", ""),
                    "udp_port": target_info.get("udp_port", 0),
                },
            }

    def _cleanup(self):
        """Remove stale clients."""
        now = time.time()
        stale = [cid for cid, info in self._clients.items()
                 if now - info.get("last_seen", 0) >= RELAY_TIMEOUT]
        for cid in stale:
            del self._clients[cid]
            self._punched.pop(cid, None)

    def stats(self) -> dict:
        with self._lock:
            direct_count = sum(1 for p in self._punched.values()
                               if p.get("state") == "DIRECT")
            relay_count = sum(1 for p in self._punched.values()
                              if p.get("state") in ("RELAY_ONLY", "UNKNOWN"))
            return {
                "relay_clients": len(self._clients),
                "forwarded": self._forwarded,
                "introductions": self._introductions,
                "punched_direct": direct_count,
                "punched_relay_only": relay_count,
                "punch_listener_port": self._punch_listener_port,
                "listener_running": self._punch_listener is not None,
            }


def start_udp_hole_punch(node_id: str, target_endpoint: str,
                         port: int = 0, timeout: float = 5.0) -> bool:
    """
    Attempt UDP hole-punching to a target endpoint.

    Sends packets from a local UDP socket — if both sides send
    simultaneously, their NATs create a temporary pinhole.

    Returns True if any response was received (hole opened).
    """
    import socket
    try:
        host, port_str = target_endpoint.rsplit(":", 1)
        target_host = host.strip("[]")  # strip IPv6 brackets
        target_port = int(port_str)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        if port > 0:
            sock.bind(("0.0.0.0", port))

        # Send several packets to pierce NAT
        handshake = json.dumps({
            "type": "hole_punch",
            "node_id": node_id,
            "timestamp": time.time(),
        }).encode("utf-8")

        for _ in range(5):
            sock.sendto(handshake, (target_host, target_port))
            time.sleep(0.1)

        # Listen for response
        try:
            data, addr = sock.recvfrom(1024)
            sock.close()
            return True
        except socket.timeout:
            sock.close()
            return False
    except Exception:
        return False
