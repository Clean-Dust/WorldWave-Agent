"""
ww/core/subconscious/federation.py — cross-node federation aggregation v8 (Neural Network)

Replaces legacy tree-based federation (v7) with neural-network-compatible
aggregation for Gossip Learning.

Key changes (v8):
  - `aggregate_into_model()` trains a DeepRiskNet instead of DecisionTree
  - `export_model_update()` exports NN weight dict instead of tree dict
  - `import_model_update()` imports NN model weights with robust aggregation
  - CrashReport format unchanged; all v7 chain compatibility preserved

Core protocol:
  1. Local crash → submit_local_report() → write Chain
  2. Train model on accumulated crash data
  3. Export model weights → PoW → broadcast to peers
  4. Peer imports → PoW verify → sandbox validate → robust aggregation
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.predictor import DeepRiskNet

# 32-dimensional fixed vector
from core.features import PADDED_FEATURES

# ── Sybil Defense ──
from p2p.pow import solve as pow_solve, verify as pow_verify, DifficultyAdjuster
from core.subconscious.sandbox import SandboxValidator, ValidationSetManager
from p2p.aggregation import (
    median_aggregation, multi_krum_aggregation,
    aggregate_forest, balancer_protection, local_validation_check,
)
from p2p.reputation import ReputationTracker

logger = logging.getLogger("ww.subconscious.federation")

FEDERATION_DIR = os.path.expanduser("~/worldwave/data/subconscious/federation")
BLOCK_SIZE = 10  # every 10 crash reports produce a block

# ── Event Priority ──

class EventPriority:
    LOW = 0
    HIGH = 1
    CRITICAL = 2
    NAMES = {0: "LOW", 1: "HIGH", 2: "CRITICAL"}


# ════════════════════════════════════════════════════════════════
#  Data format (unchanged, v7 compatible)
# ════════════════════════════════════════════════════════════════


@dataclass
class CrashReport:
    node_id: str = ""
    trigger_event: str = ""
    state_vector_before_crash: List[float] = field(default_factory=list)
    failed_tool_sequence: List[str] = field(default_factory=list)
    successful_correction: str = ""
    timestamp: float = field(default_factory=time.time)
    reward: float = 0.0
    signature: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "trigger_event": self.trigger_event,
            "state_vector_before_crash": [round(v, 3) for v in self.state_vector_before_crash],
            "failed_tool_sequence": self.failed_tool_sequence,
            "successful_correction": self.successful_correction,
            "timestamp": self.timestamp,
            "reward": self.reward,
            "signature": self.signature,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "CrashReport":
        return cls(
            node_id=d.get("node_id", ""),
            trigger_event=d.get("trigger_event", ""),
            state_vector_before_crash=d.get("state_vector_before_crash", []),
            failed_tool_sequence=d.get("failed_tool_sequence", []),
            successful_correction=d.get("successful_correction", ""),
            timestamp=d.get("timestamp", time.time()),
            reward=d.get("reward", 0.0),
            signature=d.get("signature", ""),
        )

    def compute_signature(self, secret: str = "") -> str:
        raw = f"{self.node_id}:{self.timestamp}:{json.dumps(self.state_vector_before_crash)}:{secret}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def verify(self, secret: str = "") -> bool:
        return self.signature == self.compute_signature(secret)


# ════════════════════════════════════════════════════════════════
#  Federation Aggregator (v8 — NN compatible)
# ════════════════════════════════════════════════════════════════


class FederationAggregator:
    """
    Federation aggregator v8 — neural network compatible.

    Integrates:
    - Chain (Merkle chain stores crash reports)
    - P2P Network (HTTP gossip sync)
    - DeepRiskNet (neural network aggregation via FedAvg)
    - Four-layer Sybil defense (PoW → Sandbox → Reputation → Robust Agg)
    - Inevitable Differential Privacy (gradient-level DP-SGD)
    """

    ARCH_FINGERPRINT = "DeepRiskNet-32-64-v1"  # Protocol: same arch required

    def __init__(
        self,
        chain=None,
        network=None,
        data_dir: str = FEDERATION_DIR,
        block_size: int = BLOCK_SIZE,
        privacy_epsilon: float = 3.0,
    ):
        self.data_dir = data_dir
        self.node_id = uuid.uuid4().hex[:12]
        self.block_size = block_size
        self.chain = chain
        self.network = network

        # Nostr Payload separation
        self.MAX_INLINE_SIZE = 48_000
        self._payload_store: Dict[str, dict] = {}
        self._on_payload_received = None

        # Differential privacy
        from .privacy import DifferentialPrivacy
        self._privacy = DifferentialPrivacy(epsilon=privacy_epsilon)

        # Pending block staging area
        self._pending_crashes: List[CrashReport] = []
        self._crash_store: Dict[str, CrashReport] = {}
        self._peer_reports: Dict[str, List[CrashReport]] = {}

        # Event-driven broadcast
        self._broadcast_queue: List[dict] = []
        self._last_broadcast_time: float = time.time()
        self._loss_history: List[float] = []
        self._broadcast_interval: int = 86400

        # Peer model cache (for robust aggregation)
        self._peer_models: Dict[str, list] = {}

        os.makedirs(data_dir, exist_ok=True)
        self._load()

    # ── Submit local report ──

    def submit_local_report(self, report: CrashReport) -> str:
        report.node_id = self.node_id
        report.signature = report.compute_signature()
        self._pending_crashes.append(report)
        self._crash_store[report.signature] = report

        if len(self._pending_crashes) >= self.block_size:
            self._flush_block()
        self._save()
        return report.signature

    def _flush_block(self):
        if not self._pending_crashes or self.chain is None:
            return
        crash_hashes = [r.signature for r in self._pending_crashes]
        try:
            block = self.chain.append_block(crash_hashes, node_id=self.node_id)
            logger.info(f"🧱 New block #{block.index}: {len(crash_hashes)} crashes, hash={block.hash[:16]}")
            self._pending_crashes = []
        except Exception as e:
            logger.error(f"Block append failed: {e}")

    # ── Receive remote report ──

    def receive_peer_crash(self, report: CrashReport, peer_id: str):
        if not report.signature:
            report.signature = report.compute_signature()
        if report.signature in self._crash_store:
            return
        self._crash_store[report.signature] = report
        if peer_id not in self._peer_reports:
            self._peer_reports[peer_id] = []
        self._peer_reports[peer_id].append(report)
        if self.chain:
            self._pending_crashes.append(report)
            if len(self._pending_crashes) >= self.block_size:
                self._flush_block()
        self._save()

    def recent_local_reports(self, n: int = 10) -> List[CrashReport]:
        return [r for sig, r in list(self._crash_store.items())[-n:]]

    def all_peer_reports(self, limit: int = 100) -> List[CrashReport]:
        reports = []
        for prs in self._peer_reports.values():
            reports.extend(prs)
        reports.sort(key=lambda r: r.timestamp, reverse=True)
        return reports[:limit]

    # ── Model aggregation ──

    def aggregate_into_model(
        self,
        model: DeepRiskNet,
        min_samples: int = 5,
        max_samples: int = 200,
    ) -> Dict[str, Any]:
        """
        Train model on all crash reports (local + remote + chain).

        Uses DeepRiskNet.fit() to train on crash data.
        Unlike v7 (which appended trees), v8 re-trains on the entire dataset
        for better generalization.

        Args:
            model: current DeepRiskNet instance
            min_samples: minimum samples required to train
            max_samples: max samples used

        Returns:
            training summary dict
        """
        # Collect training data
        training_X: List[List[float]] = []
        training_y: List[float] = []

        all_reports = list(self._crash_store.values()) + [
            r for pr in self._peer_reports.values() for r in pr
        ]
        # Deduplication
        seen = set()
        deduped = []
        for r in all_reports:
            if r.signature and r.signature not in seen:
                seen.add(r.signature)
                deduped.append(r)

        deduped.sort(key=lambda r: r.timestamp, reverse=True)
        deduped = deduped[:max_samples]

        for report in deduped:
            vec = report.state_vector_before_crash
            if len(vec) >= PADDED_FEATURES:
                training_X.append(vec[:PADDED_FEATURES])
            elif len(vec) >= 15:
                padded = list(vec[:15]) + [0.0] * (PADDED_FEATURES - 15)
                training_X.append(padded)
            elif len(vec) >= 12:
                padded = list(vec[:12]) + [0.0, 1.0, 0.0] + [0.0] * (PADDED_FEATURES - 15)
                training_X.append(padded)
            else:
                continue
            label = 1.0 if report.reward < 0.5 else 0.0
            training_y.append(label)

        if len(training_X) < min_samples:
            return {
                "trained": False,
                "reason": f"Insufficient samples ({len(training_X)} < {min_samples})",
                "samples": len(training_X),
            }

        # Train the model on accumulated data
        result = model.fit(training_X, training_y, epochs=10, verbose=False)

        result["samples"] = len(training_X)
        result["model_size_bytes"] = model.size_bytes()
        return result

    # ── Chain integration ──

    def sync_from_chain(self) -> int:
        if self.chain is None:
            return 0
        count = 0
        for block in self.chain.blocks:
            for ch in block.crash_hashes:
                if ch not in self._crash_store:
                    report = self._load_report(ch)
                    if report:
                        self._crash_store[ch] = report
                        count += 1
        return count

    # ── Sybil Defense ──

    def enable_defense(
        self,
        sandbox_validator: Optional[SandboxValidator] = None,
        validation_data: Optional[ValidationSetManager] = None,
        reputation_tracker: Optional[ReputationTracker] = None,
        difficulty_adjuster: Optional[DifficultyAdjuster] = None,
        aggregation_method: str = "multi_krum",
    ):
        self.defense_enabled = True
        self.sandbox_validator = sandbox_validator or SandboxValidator()
        self.validation_set = validation_data or ValidationSetManager()
        self.reputation_tracker = reputation_tracker or ReputationTracker()
        self.pow_difficulty = difficulty_adjuster or DifficultyAdjuster()
        self.aggregation_method = aggregation_method

    def disable_defense(self):
        self.defense_enabled = False
        self.sandbox_validator = None
        self.validation_set = None
        self.reputation_tracker = None
        self.pow_difficulty = None
        self.aggregation_method = "median"

    @property
    def defense_active(self) -> bool:
        return getattr(self, "defense_enabled", False)

    @property
    def privacy_active(self) -> bool:
        return True

    # ── Export (with PoW + DP) ──

    def store_payload(self, cid: str, payload: dict):
        self._payload_store[cid] = payload

    def get_payload(self, cid: str):
        return self._payload_store.get(cid)

    def set_payload_callback(self, fn):
        self._on_payload_received = fn

    def export_model_update(self, model: DeepRiskNet) -> dict:
        """
        Export model as broadcast payload (NN weights + DP + PoW).

        v8: exports full weight dict instead of individual tree dict.
        """
        # Deep copy with DP noise
        dp = getattr(self, "_privacy", None)
        if dp is not None:
            noisy_model = dp.get_noisy_copy(model)
            params = noisy_model.to_dict()["params"]
        else:
            params = model.to_dict()["params"]

        payload = {
            "node_id": self.node_id,
            "timestamp": time.time(),
            "model_version": "subconscious-v8",
            "arch_fingerprint": self.ARCH_FINGERPRINT,
            "params": params,
            "size_bytes": len(json.dumps(params)),
        }

        # Layer 1: PoW
        if self.defense_active:
            pow_result = self._solve_pow_for_payload(payload)
            if pow_result:
                payload["pow_nonce"] = pow_result["nonce"]
                payload["pow_hash"] = pow_result["hash"]
                payload["pow_bits"] = pow_result["bits"]

        return payload

    def _solve_pow_for_payload(self, payload: dict) -> Optional[dict]:
        if not self.pow_difficulty:
            return None
        try:
            data = json.dumps(payload, sort_keys=True).encode()
            bits = self.pow_difficulty.current_bits()
            result = pow_solve(data, bits, timeout_s=30.0)
            if result:
                nonce, h, attempts, elapsed = result
                self.pow_difficulty.record_solve(elapsed)
                logger.info(f"⛏️ PoW solved: {bits} bits, {attempts} attempts, {elapsed:.1f}s, hash={h[:16]}")
                return {"nonce": nonce, "hash": h, "bits": bits}
            else:
                logger.warning("⛏️ PoW timeout (30s)")
                return None
        except Exception as e:
            logger.error(f"PoW solve error: {e}")
            return None

    # ── Event-driven broadcast ──

    def record_loss(self, loss_value: float):
        self._loss_history.append(loss_value)
        if len(self._loss_history) > 20:
            self._loss_history.pop(0)

    def classify_priority(self, current_loss: float) -> int:
        if len(self._loss_history) < 2:
            return EventPriority.LOW
        prev_loss = self._loss_history[-2]
        drop = prev_loss - current_loss
        if drop > 0.3:
            return EventPriority.CRITICAL
        elif drop > 0.1:
            return EventPriority.HIGH
        else:
            return EventPriority.LOW

    def broadcast_update(
        self,
        model: DeepRiskNet,
        current_loss: Optional[float] = None,
    ) -> dict:
        if current_loss is not None:
            self.record_loss(current_loss)
            priority = self.classify_priority(current_loss)
        else:
            priority = EventPriority.LOW

        payload = self.export_model_update(model)
        now = time.time()
        age = (now - self._last_broadcast_time) / 3600.0

        payload_bytes = len(json.dumps(payload))
        payload_type = "inline"
        broadcast_content = payload

        if payload_bytes > self.MAX_INLINE_SIZE:
            cid = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
            self.store_payload(cid, payload)
            broadcast_content = {
                "node_id": self.node_id,
                "type": "model_pointer",
                "cid": cid,
                "size": payload_bytes,
                "arch_fingerprint": self.ARCH_FINGERPRINT,
                "priority": EventPriority.NAMES[priority],
                "timestamp": now,
            }
            payload_type = "pointer"
            logger.info(f"📦 Payload too large ({payload_bytes}B > {self.MAX_INLINE_SIZE}B), "
                        f"broadcasting pointer cid={cid}")

        if priority >= EventPriority.HIGH:
            broadcast_content["priority"] = EventPriority.NAMES[priority]
            if self.network:
                try:
                    self.network.broadcast(json.dumps(broadcast_content))
                    logger.info(f"📡 Immediate broadcast [{EventPriority.NAMES[priority]}]: "
                                f"type={payload_type} {payload_bytes}B, queue={len(self._broadcast_queue)}")
                except Exception as e:
                    logger.error(f"Broadcast failed: {e}")
            self._flush_broadcast_queue(model)
            self._last_broadcast_time = now
            return {
                "queued": False,
                "priority": EventPriority.NAMES[priority],
                "priority_code": priority,
                "queue_size": len(self._broadcast_queue),
                "age_hours": round(age, 1),
                "payload_type": payload_type,
            }

        # LOW → queue
        payload["priority"] = "LOW"
        self._broadcast_queue.append(payload)

        if age >= (self._broadcast_interval / 3600.0):
            flushed = self._flush_broadcast_queue(model)
            return {
                "queued": False,
                "priority": "LOW_BATCH",
                "priority_code": EventPriority.LOW,
                "flushed": flushed,
                "queue_size": 0,
                "age_hours": round(age, 1),
            }

        return {
            "queued": True,
            "priority": "LOW",
            "priority_code": EventPriority.LOW,
            "queue_size": len(self._broadcast_queue),
            "age_hours": round(age, 1),
        }

    def _flush_broadcast_queue(self, model: DeepRiskNet) -> int:
        if not self._broadcast_queue:
            return 0

        batch = {
            "node_id": self.node_id,
            "timestamp": time.time(),
            "model_version": "subconscious-v8",
            "batch": True,
            "count": len(self._broadcast_queue),
            "params": [p.get("params") for p in self._broadcast_queue if "params" in p],
        }

        if self.defense_active:
            pow_result = self._solve_pow_for_payload(batch)
            if pow_result:
                batch["pow_nonce"] = pow_result["nonce"]
                batch["pow_hash"] = pow_result["hash"]
                batch["pow_bits"] = pow_result["bits"]

        if self.network:
            try:
                self.network.broadcast(json.dumps(batch))
            except Exception as e:
                logger.error(f"Batch broadcast failed: {e}")

        count = len(self._broadcast_queue)
        self._broadcast_queue = []
        self._last_broadcast_time = time.time()
        logger.info(f"📦 Batch broadcast: {count} updates")
        return count

    @property
    def queue_size(self) -> int:
        return len(self._broadcast_queue)

    # ── Import (PoW → sandbox → reputation → robust aggregation) ──

    def import_model_update(
        self, update: dict, local_model: DeepRiskNet,
        peer_id: str = "",
    ) -> bool:
        """
        Import a model update from peer.

        If defense enabled, full pipeline:
          Layer 1: PoW validate
          Layer 2: Sandbox validate
          Layer 3: Reputation update
          Layer 4: Robust aggregation via BALANCE + LPC

        Args:
            update: model update dict (from export_model_update)
            local_model: current local DeepRiskNet
            peer_id: peer identifier

        Returns:
            True if update was accepted and applied
        """
        params_data = update.get("params")
        if not params_data:
            return False

        peer_id = peer_id or update.get("node_id", "unknown")

        # Verify architecture fingerprint
        peer_fp = update.get("arch_fingerprint", "")
        if peer_fp and peer_fp != self.ARCH_FINGERPRINT:
            logger.warning(f"🚫 Architecture mismatch: {peer_fp} != {self.ARCH_FINGERPRINT}")
            return False

        # Build peer model
        template = local_model.to_dict()
        template["params"] = params_data
        peer_model = DeepRiskNet.from_dict(template)

        # Layer 1: PoW validate
        if self.defense_active and self.pow_difficulty:
            if not self._verify_pow(update):
                logger.warning(f"🚫 PoW verification failed for peer={peer_id[:12]}")
                if self.reputation_tracker:
                    self.reputation_tracker.record_validation(
                        peer_id, passed=False,
                        details={"reason": "pow_failed", "verdict": "rejected"},
                    )
                return False

        # Layer 2: Sandbox validate (BALANCE + LPC)
        if self.defense_active and self.sandbox_validator and self.validation_set:
            val_data = self.validation_set.get_data()
            if val_data:
                # BALANCE: compare peer vs local on local data
                bal_pass, local_acc = balancer_protection(
                    peer_model, local_model, val_data, threshold=0.02,
                )

                # LPC: baseline sanity check
                lpc_pass = local_validation_check(peer_model, val_data, min_accuracy=0.4)

                passed = bal_pass and lpc_pass
                result = {
                    "passed": passed,
                    "accuracy": local_acc,
                    "verdict": "accepted" if passed else "rejected",
                    "balancer": bal_pass,
                    "lpc": lpc_pass,
                }

                if self.reputation_tracker:
                    self.reputation_tracker.record_validation(
                        peer_id, passed=passed, details=result,
                    )

                if not passed:
                    logger.info(f"🚫 Sandbox rejected peer={peer_id[:12]} "
                                f"accuracy={local_acc:.3f}")
                    return False

                logger.info(f"✅ Sandbox passed peer={peer_id[:12]} "
                            f"accuracy={local_acc:.3f}")
            else:
                logger.warning("No validation data, skipping sandbox check")

        # Layer 4: Robust aggregation
        if self.defense_active:
            return self._robust_import(peer_model, local_model, peer_id)
        else:
            # Direct replacement (no defense)
            # FedAvg: in-place model averaging
            avg = aggregate_forest([local_model, peer_model], method="weighted_average")
            # Copy averaged weights into local model
            avg_d = avg.to_dict()
            local_d = local_model.to_dict()
            for key in avg_d["params"]:
                local_d["params"][key] = avg_d["params"][key]
            # Reload
            new_local = DeepRiskNet.from_dict(local_d)
            # Copy state back (hacky but works for in-place)
            local_model.__dict__.update(new_local.__dict__)
            return True

    def _verify_pow(self, update: dict) -> bool:
        pow_nonce = update.get("pow_nonce")
        pow_hash = update.get("pow_hash")
        pow_bits = update.get("pow_bits")

        if pow_nonce is None or pow_bits is None:
            logger.debug("No PoW in update (allowing for compatibility)")
            return True

        payload = {k: v for k, v in update.items()
                   if k not in ("pow_nonce", "pow_hash", "pow_bits")}
        try:
            data = json.dumps(payload, sort_keys=True).encode()
            return pow_verify(data, pow_nonce, pow_bits, expected_hash=pow_hash or "")
        except Exception as e:
            logger.error(f"PoW verify error: {e}")
            return False

    def _robust_import(
        self, peer_model: DeepRiskNet, local_model: DeepRiskNet,
        peer_id: str,
    ) -> bool:
        """
        Robust peer model aggregation.

        Strategy:
        1. Accumulate each peer's models (up to 3 latest)
        2. Apply Multi-Krum to select honest models
        3. FedAvg the selected models into local

        Args:
            peer_model: incoming peer's model
            local_model: current local model
            peer_id: peer identifier

        Returns:
            True if local model was updated
        """
        if peer_id not in self._peer_models:
            self._peer_models[peer_id] = []
        self._peer_models[peer_id].append(peer_model)

        # Keep only latest 3 per peer
        while len(self._peer_models[peer_id]) > 3:
            self._peer_models[peer_id].pop(0)

        # Collect all peer models (excluding blacklisted)
        candidate_models = []
        for pid, models in self._peer_models.items():
            if self.reputation_tracker and self.reputation_tracker.is_blacklisted(pid):
                continue
            candidate_models.extend(models)

        if not candidate_models:
            return False

        # Validate with local validation set
        validation_set = self._get_validation_data()
        if validation_set and len(validation_set) >= 3:
            # Apply Multi-Krum if enough models
            if len(candidate_models) >= 5:
                agg = multi_krum_aggregation(candidate_models, f=1)
            elif len(candidate_models) >= 3:
                agg = median_aggregation(candidate_models)
            else:
                agg = candidate_models[0]

            if agg is None:
                return False

            # Compare with local model (BALANCE check)
            bal_pass, local_acc = balancer_protection(
                agg, local_model, validation_set, threshold=0.02,
            )
            if not bal_pass:
                logger.debug(f"Peer model rejected by BALANCE (local_acc={local_acc:.3f})")
                return False

            # Replace local model with FedAvg of (agg, local)
            merged = aggregate_forest(
                [local_model, agg], method="weighted_average",
            )
        else:
            # No validation data → weighted average
            merged = aggregate_forest(
                [local_model, candidate_models[0]], method="weighted_average",
            )

        # Copy merged weights into local model
        merged_d = merged.to_dict()
        local_d = local_model.to_dict()
        for key in merged_d["params"]:
            local_d["params"][key] = merged_d["params"][key]
        new_local = DeepRiskNet.from_dict(local_d)
        local_model.__dict__.update(new_local.__dict__)

        logger.info(f"🧬 Model aggregated from {len(candidate_models)} candidate(s)")
        return True

    def _get_validation_data(self) -> list:
        vs = getattr(self, "validation_set", None)
        if vs is not None:
            return vs.get_data(balanced=True)
        return []

    # ── Statistics ──

    def stats(self) -> Dict[str, Any]:
        total_local = sum(1 for r in self._crash_store.values() if r.node_id == self.node_id)
        total_remote = len(self._crash_store) - total_local
        pending = len(self._pending_crashes)
        chain_blocks = len(self.chain.blocks) if self.chain else 0

        stats = {
            "node_id": self.node_id,
            "local_reports": total_local,
            "peers": len(self._peer_reports),
            "remote_reports": total_remote,
            "pending_flush": pending,
            "chain_blocks": chain_blocks,
            "data_dir": self.data_dir,
        }

        if self.defense_active:
            stats["defense"] = {
                "enabled": True,
                "aggregation_method": self.aggregation_method,
                "pow_difficulty_bits": self.pow_difficulty.current_bits()
                if self.pow_difficulty else 0,
                "pow_estimated_time_s": round(
                    self.pow_difficulty.estimated_time(), 2
                ) if self.pow_difficulty else 0,
                "validation_samples": len(self.validation_set.get_data())
                if self.validation_set else 0,
            }
            if self.reputation_tracker:
                rep_stats = self.reputation_tracker.get_stats()
                stats["defense"]["reputation"] = rep_stats

        return stats

    # ── Persistence ──

    def _save(self):
        path = os.path.join(self.data_dir, "reports.json")
        data = {
            "node_id": self.node_id,
            "pending": [r.to_dict() for r in self._pending_crashes[-20:]],
            "store": {sig: r.to_dict() for sig, r in list(self._crash_store.items())[-200:]},
            "peers": {
                pid: [r.to_dict() for r in prs[-20:]]
                for pid, prs in self._peer_reports.items()
            },
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _load(self):
        path = os.path.join(self.data_dir, "reports.json")
        if not os.path.isfile(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self.node_id = data.get("node_id", self.node_id)
            self._pending_crashes = [CrashReport.from_dict(d)
                                     for d in data.get("pending", [])]
            self._crash_store = {
                sig: CrashReport.from_dict(d)
                for sig, d in data.get("store", {}).items()
            }
            self._peer_reports = {
                pid: [CrashReport.from_dict(d) for d in pl]
                for pid, pl in data.get("peers", {}).items()
            }
        except Exception as e:
            logger.warning(f"Federation load failed: {e}")

    def _save_report(self, report: CrashReport):
        path = os.path.join(self.data_dir, "crashes", f"{report.signature}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(report.to_json())

    def _load_report(self, signature: str) -> Optional[CrashReport]:
        path = os.path.join(self.data_dir, "crashes", f"{signature}.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return CrashReport.from_dict(json.load(f))
            except Exception:
                pass
        return None
