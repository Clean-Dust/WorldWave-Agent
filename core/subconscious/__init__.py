"""
ww/core/subconscious — Worldwave subconscious v7: Sybil defense + four-layer validation gateway

v7 adds a complete Sybil attack defense system on v6 basis:

  features.py     — 12-dimensional feature extraction (does not read dialogue, only looks at numerical values)
  predictor.py    — Pure Python Random Forest (zero external dependencies)
  rewind.py       — Rewind resurrection engine + intuition injection
  federation.py   — Cross-node federation aggregation + Chain integration
  chain.py        — Merkle Chain ledger (pure Python blockchain)
  network.py      — Global P2P network v6 (bootstrap tracker + HTTP gossip)
  blockchain.py   — Real PoW blockchain (SHA256 double hash, compact bits, mempool)

  ── Sybil Defense (v7 new) ──
  pow.py          — Lightweight PoW anti-Sybil (self-adaptive difficulty, goal 5-10 seconds to solve)
  sandbox.py      — Local sandbox validation (external models must pass validation before being allowed to merge)
  aggregation.py  — Robust aggregation algorithm (Trimmed Mean / Median / Krum)
  reputation.py   — Web of Trust (reputation trace + blacklist + demotion)
  nostr.py        — Nostr Relay communication layer (BIP-340 Schnorr signature, relay pool)
  api.py          — FastAPI route（20+ endpoint）

  ── Contrastive Learning (v8 new) ──
  signal_pipeline.py   — Four-dimensional signal collection pipeline (environment/user/efficiency/reflection)
  contrastive.py       — DPO contrastive learning engine (leaf node value push-pull adjustment)
  runtime_collector.py — Spiral loop hook collection (auto-collect training data)

  ── Translation Layer (v12 new) ──
  rule_dict.py         — Optimized rule dictionary (static System Prompt snippets/API parameters)
  wrapper.py           — Translation layer: signal → Rule ID → system instructions/parameters/action code

  ── Resource Scheduling (v8 new) ──
  scheduler.py         — TRIAGE-inspired resource scheduler: token budget, task priority, abandon dead paths
  ppo.py               — PPO policy gradient: learns steering policy (which intervention to apply)
"""

from __future__ import annotations
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..consent import ConsentManager

from core.features import FeatureExtractor, FEATURE_NAMES, PADDED_FEATURES, pad_vector
from core.predictor import DeepRiskNet, TriageVector  # v8: replaced RandomForest
from .rewind import RewindEngine

# ── Sybil Defense (v7) ──
from p2p.pow import solve as pow_solve, verify as pow_verify, DifficultyAdjuster
from .sandbox import SandboxValidator, ValidationSetManager
from p2p.aggregation import (
    trimmed_mean, median_aggregation, krum_aggregation, multi_krum_aggregation,
    aggregate_forest,
)
from p2p.reputation import ReputationTracker, ReputationEntry

# ── Translation Layer (v12) ──
from .rule_dict import RuleDictionary
from .wrapper import SubconsciousWrapper, SignalMatcher, Intervention

# ── Nostr Relay Communication (v7) ──
from p2p.nostr import (
    NostrEvent, NostrRelayClient, RelayPool,
    pack_model_update, unpack_model_update,
    generate_keypair,
    schnorr_sign, schnorr_verify,
)

# ── contrastive learning（v8） ──
from .signal_pipeline import SignalCollector, TrainingTriple, SignalSource
from .contrastive import ContrastiveEngine, CFREngine
from .runtime_collector import RuntimeCollector
from .snapshot import SnapshotManager
from p2p.privacy import DifferentialPrivacy

# ── DHT (Kademlia peer discovery) ──
from p2p.dht import DHTNode

# ── Resource Scheduling + PPO (v8) ──
from .scheduler import ResourceScheduler, TaskBudgetTracker
from .ppo import PPOAgent, PolicyValueNet

# ── SUPO Context Compression + Nighttime (dead code revival) ──
from .compress import ContextCompressor, Segment
from .night import NighttimeEngine

# ── Self-hosted LLM plugins (disabled by default) ──
from .plugins import SelfHostedPluginManager

logger = logging.getLogger("ww.subconscious")


# Model data directory — configurable via WW_DATA_DIR env, default to project-root/data/subconscious
_DEFAULT_DATA_DIR = os.environ.get(
    "WW_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "subconscious")
)
SUBCONSCIOUS_DIR = os.path.abspath(_DEFAULT_DATA_DIR)
MODEL_FILE = os.path.join(SUBCONSCIOUS_DIR, "model.json")
CONFIG_FILE = os.path.join(SUBCONSCIOUS_DIR, "config.json")


class Subconscious:
    """
    WW subconscious v4: meta learning observation.

    Subconscious does not talk to user, does not produce task results.
    It only does one thing: observe main consciousness behavior patterns, predict failures, optimize decisions.

    This is a pure Python decision tree ensemble, not a language model.
    Size: ~20KB. Does not occupy RAM. Does not need GPU.
    """

    def __init__(
        self,
        enabled: bool = True,
        model_path: str = MODEL_FILE,
        hidden_dim: int = 64,
        learning_rate: float = 0.001,
        rewind_threshold: float = 0.7,
        auto_train_interval: int = 5,
        blockchain_enabled: bool = True,
        p2p_enabled: bool = True,
        p2p_public_mode: bool = False,
        p2p_public_address: str = "",
        bootstrap_urls: Optional[List[str]] = None,
        miner_id: str = "",
        provider_id: str = "",
        self_hosted_config: Optional[Dict[str, Any]] = None,
    ):
        self.enabled = enabled
        self.model_path = model_path
        self.auto_train_interval = auto_train_interval
        self._spiral_count = 0
        self._training_count = 0
        self._provider_id = provider_id

        # Submodules
        self.feature_extractor = FeatureExtractor()
        if provider_id:
            self.feature_extractor.set_provider(provider_id)
        self.predictor = DeepRiskNet(
            n_features=PADDED_FEATURES,
            hidden_dim=hidden_dim,
            dropout=0.1,
            lr=learning_rate,
            use_temporal=True,
            temporal_buffer_size=8,
        )
        self.rewind_engine = RewindEngine(
            rewind_threshold=rewind_threshold,
        )

        # ── Lazy P2P imports (deferred to break circular import) ──
        from p2p.chain import Chain
        from p2p.blockchain import Blockchain
        from p2p.network import GlobalP2PNetwork
        from p2p.gossip import GossipModule
        from p2p.federation import FederationAggregator

        self.chain = Chain()

        # ── CFR regret minimization ──
        self.cfr: Optional[CFREngine] = CFREngine(n_bins=64, regret_weight=0.3)

        # ── SUPO Context Compressor ──
        self.compressor = ContextCompressor(
            token_budget=4096,
            auto_persist=True,
            data_dir=os.path.join(SUBCONSCIOUS_DIR, "compression"),
        )

        # ── Nighttime Engine ──
        self.night = NighttimeEngine(
            data_dir=os.path.join(SUBCONSCIOUS_DIR, "nighttime"),
        )
        self._night_last_run = 0.0
        self._night_interval = 300  # seconds between runs
        self._night_vectors: List[List[float]] = []

        # ── PPO Steering Agent ──
        self.ppo = PPOAgent(
            policy_net=PolicyValueNet(input_dim=32),  # operate on 32-dim features
            min_steps_before_update=50,
            auto_persist=True,
            data_dir=os.path.join(SUBCONSCIOUS_DIR, "ppo"),
        )
        self._ppo_episode_active = False
        self._last_ppo_action = 0
        self._last_ppo_logprob = 0.0
        self._last_ppo_value = 0.0
        self._last_ppo_features: List[float] = []

        # ── Resource Scheduler ──
        self.resource_scheduler = ResourceScheduler(
            data_dir=os.path.join(SUBCONSCIOUS_DIR, "scheduling"),
        )

        # ── User consent check ──
        self._consent = ConsentManager()
        if not self._consent.check("p2p_network"):
            if p2p_enabled or blockchain_enabled:
                logger.warning(
                    "P2P/blockchain feature user consent setting disabled.\n"
                    "  To enable, please execute: python -m ww setup\n"
                    "  This will connect to public Nostr relay stations, belonging to P2P behavior.\n"
                    "  If not in firewall allowlist, antivirus software may issue a warning."
                )
            blockchain_enabled = False
            p2p_enabled = False

        # ── WW Blockchain (Global PoW blockchain) ──
        self.blockchain: Optional[Blockchain] = None
        if blockchain_enabled:
            self.blockchain = Blockchain(mining_enabled=False)

        # ── Global P2P network ──
        self.p2p: Optional[GlobalP2PNetwork] = None
        if p2p_enabled:
            node_id = miner_id or uuid.uuid4().hex[:12]
            self.p2p = GlobalP2PNetwork(
                node_id=node_id,
                public_mode=p2p_public_mode,
                public_address=p2p_public_address,
                bootstrap_urls=bootstrap_urls,
            )
            # Connect blockchain callbacks
            if self.blockchain:
                self.p2p.set_blockchain_callbacks(
                    get_blocks=lambda f, c: [b.to_dict() for b in self.blockchain.chain[f:f+c]],
                    get_height=lambda: self.blockchain.height if self.blockchain else -1,
                    get_latest_hash=lambda: self.blockchain.latest_hash if self.blockchain else "",
                    get_mempool=lambda: [tx.to_dict() for tx in self.blockchain.mempool],
                    mempool_count=lambda: len(self.blockchain.mempool) if self.blockchain else 0,
                    receive_block=lambda bd: self._receive_block(bd),
                    receive_tx=lambda td: self._receive_tx(td),
                )
                # Blockchain mines new block → P2P broadcast
                self.blockchain._on_block_callback = lambda b: self.p2p.broadcast_block(b.to_dict())

            # Start DHT (Kademlia peer discovery) + P2P network
            self.p2p.start()

            # Warn if isolated after grace period
            import time as _time
            _time.sleep(3)
            if self.p2p.peers_discovered == 0:
                if not bootstrap_urls and not os.environ.get("WW_BOOTSTRAP_URLS"):
                    logger.warning(
                        "No peers discovered: no bootstrap URLs configured and no LAN peers "
                        "found via mDNS. This node is isolated in WAN scenarios. Set "
                        "WW_BOOTSTRAP_URLS or deploy a bootstrap tracker."
                    )
                else:
                    logger.warning(
                        "No peers discovered after %d seconds. The bootstrap tracker at %s "
                        "may be unreachable or no other peers are registered.",
                        3, bootstrap_urls or os.environ.get("WW_BOOTSTRAP_URLS", ""),
                    )

            # Start Nostr relay pool (decentralized pub/sub)
            try:
                self._nostr_pool = RelayPool(
                    on_model_update=self._handle_nostr_model_update,
                )
                self._nostr_pool.start(subscription_id=f"ww-{node_id[:8]}")
                logger.info("🗞️ Nostr relay pool started (%d relays)",
                            len(self._nostr_pool.relay_urls))
            except Exception as e:
                logger.warning("Nostr relay pool init failed: %s", e)
                self._nostr_pool = None

        # ── DHT convenience reference (backed by P2P network's DHT node) ──
        self.dht = self.p2p.dht if self.p2p else None

        # ── training buffer (must init before gossip, which calls _get_validation_set) ──
        self._training_buffer_x: List[List[float]] = []
        self._training_buffer_y: List[float] = []

        # ── Gossip Learning (v8 + Phase 5/6) ──
        self._validation_cache: List[Tuple[List[float], float]] = []
        self._validation_cache_ts = 0.0
        self._validation_cache_ttl = 60.0  # refresh every 60s
        self.gossip: Optional[GossipModule] = None
        if p2p_enabled and self.p2p:
            self.gossip = GossipModule(
                local_model=self.predictor,
                get_peers=self._get_peers_for_gossip,
                http_port=self.p2p.listen_port,
                node_id=self.p2p.node_id,
                gossip_interval=300,
                mix_ratio=0.5,
                validation_set=self._get_validation_set(),
            )
            # Wire gossip handler into P2P server
            self.p2p.gossip_handler = self.gossip.handle_gossip_request
            self.gossip.start()

        # ── federationaggregation ──
        self.federation = FederationAggregator(
            chain=self.chain,
        )

        # ── contrastive learning（v8） ──
        self.runtime = RuntimeCollector(
            feature_extractor=self.feature_extractor,
            predictor=self.predictor,
        )

        # ── Snapshot + differential privacy (v8) ──
        self.snapshot_manager = SnapshotManager()
        self.privacy = DifferentialPrivacy(epsilon=3.0)

        # ── Self-hosted LLM plugins (disabled by default) ──
        self.plugins = SelfHostedPluginManager(
            config=self_hosted_config or {}
        )
        if self.plugins.enabled:
            logger.info("🧩 Self-hosted LLM plugins enabled")
            self.plugins.initialize()
            # Register interrupt callback with backend (try-catch for BackendUnsupportedError)
            if (self.plugins.enable_interrupts
                    and self.plugins.backend_ready
                    and self.plugins.backend):
                try:
                    self.plugins.backend.register_interrupt_callback(
                        lambda: self._check_plugin_interrupt()
                    )
                except Exception:
                    logger.debug("Interrupt callback registration not supported by backend")

        # UX event log (subconscious intervention record, for UI display ⚡)
        self._event_log: List[Dict[str, Any]] = []
        self._event_id = 0

        # training databuffer
        self._training_buffer_x: List[List[float]] = []
        self._training_buffer_y: List[float] = []

        # Validation set for BALANCE+LPC gossip defence (held-out 20%)
        self._validation_cache: List[Tuple[List[float], float]] = []
        self._validation_cache_ts = 0.0
        self._validation_cache_ttl = 60.0  # refresh every 60s

        # Phase repetition count (detect stuck)
        self._phase_history: List[int] = []
        self._phase_repeat_count = 0
        self._last_phase_id = -1

        # toolsequencetrace
        self._tool_sequence: List[str] = []

        # Load save model
        self._load_model()
        if self.enabled:
            logger.info("🧠 subconscious v8 start: DeepRiskNet hidden_dim=%d, lr=%s",
                        hidden_dim, learning_rate)

    def _handle_nostr_model_update(self, model_data: dict) -> None:
        """Handle incoming model update from Nostr relay."""
        logger.debug("Nostr model update received: %d keys", len(model_data))
        if self.gossip and hasattr(self.gossip, '_handle_external_update'):
            try:
                self.gossip._handle_external_update(model_data)
            except Exception as e:
                logger.debug("Nostr gossip forward failed: %s", e)

    def _get_validation_set(self) -> List[Tuple[List[float], float]]:
        """Return held-out validation set from recent training buffer (cached)."""
        now = time.time()
        if (self._validation_cache and
                now - self._validation_cache_ts < self._validation_cache_ttl):
            return self._validation_cache

        n = len(self._training_buffer_x)
        if n < 10:
            self._validation_cache = []
            return []

        # Held-out 20% as validation
        split = max(1, n // 5)
        self._validation_cache = [
            (self._training_buffer_x[i], self._training_buffer_y[i])
            for i in range(-split, 0)
        ]
        self._validation_cache_ts = now
        return self._validation_cache

    def _get_peers_for_gossip(self) -> List[tuple[str, str]]:
        """Return list of (peer_id, http_url) for gossip peer selection.
        
        Combines HTTP-discovered peers with DHT-discovered peers (via Kademlia).
        """
        result: List[tuple[str, str]] = []
        seen_ids: set = set()

        # 1. HTTP-discovered peers (from P2P peer exchange)
        if self.p2p:
            for pid, peer in self.p2p.peers.items():
                if pid == self.p2p.node_id or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                url = peer.gossip_endpoint()
                result.append((pid, url))

            # 2. DHT-discovered peers (from Kademlia routing table)
            dht_peers = self.p2p.dht.get_all_peers()
            for pid, addr in dht_peers:
                if pid == self.p2p.node_id or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                url = f"http://{addr}"
                result.append((pid, url))

        return result

    def _check_plugin_interrupt(self) -> bool:
        """Called by backend before each generation step.
        Returns True to interrupt, False to continue."""
        if not self.enabled or not self.plugins.enable_interrupts:
            return False
        if not self.plugins.interrupt:
            return False

        # Get latest risk + probe values
        triage = self.predict()
        risk = triage.crash_risk
        probe_vals = self.plugins.probes if self.plugins.probes else None
        should_stop, _ = self.plugins.check_interrupt(
            risk_score=risk,
            token_entropy=(probe_vals.smoothed("token_entropy")
                          if probe_vals else 0.5),
            attention_sparsity=(probe_vals.smoothed("attention_sparsity")
                               if probe_vals else 0.5),
            logit_magnitude=(probe_vals.smoothed("logit_magnitude")
                            if probe_vals else 0.5),
            hidden_state_norm=(probe_vals.smoothed("hidden_state_norm")
                              if probe_vals else 0.5),
            gate_confidence=self.plugins._last_gate_decision.get(
                "confidence", 0.5),
        )
        return should_stop

    # ── Observation (called per spiral) ──

    def observe_spiral(self, phase_id: int, spirals_completed: int):
        """Observe a spiral phase."""
        if not self.enabled:
            return
        self._spiral_count += 1

        # Phase repetition detection
        if phase_id == self._last_phase_id:
            self._phase_repeat_count += 1
        else:
            self._phase_repeat_count = 0
        self._last_phase_id = phase_id
        self._phase_history.append(phase_id)
        if len(self._phase_history) > 20:
            self._phase_history.pop(0)

        # Daily snapshot check
        self.snapshot_manager.daily_check(self.predictor)

    def set_provider(self, provider_id: str):
        """Setting LLM provider (affects subconscious feature vector one-hot encode)."""
        self._provider_id = provider_id
        self.feature_extractor.set_provider(provider_id)

    def set_ram_level(self, level: int):
        """
        RAM level is fixed for neural network model (~270KB).

        Args:
            level: ignored (no concept of RAM presets in NN models)
        """
        logger.info(f"RAM level {level} ignored: DeepRiskNet model size is fixed (~270KB)")
        return {"level": level, "name": "DeepRiskNet", "target": None}

    def save_sparse(self, path: Optional[str] = None) -> str:
        """Save model in COO sparse format (for network transmission).
        defaultpath：~/worldwave/data/subconscious/model_sparse.json
        """
        if path is None:
            path = os.path.expanduser(
                "~/worldwave/data/subconscious/model_sparse.json"
            )
        s = self.predictor.to_json_sparse()
        with open(path, "w") as f:
            f.write(s)
        return path

    def observe_action(self, tool_name: str, success: bool,
                       latency: float = 0.0, token_count: int = 0):
        """Observe a tool call."""
        if not self.enabled:
            return
        self.feature_extractor.observe_action(
            tool_name, success, latency, token_count
        )
        self._tool_sequence.append(tool_name)
        if len(self._tool_sequence) > 20:
            self._tool_sequence.pop(0)

        # Contrastive learning: record environment feedback
        state_vec = self.feature_extractor.extract(
            spirals_completed=self._spiral_count,
        )
        self.runtime.after_action(
            tool_name=tool_name,
            success=success,
            exit_code=0 if success else 1,
            latency=latency,
            tokens=token_count,
            state_before=state_vec,
            spirals_completed=self._spiral_count,
        )

        # CFR observation: feed actual outcome back
        if self.cfr is not None and len(state_vec) >= 12:
            padded = self.feature_extractor.normalize(
                pad_vector(state_vec)
            )
            outcome = 0.0 if success else 1.0
            model_pred = (
                self.predictor.predict(padded).crash_risk
                if not self.predictor.empty() else 0.5
            )
            self.cfr.observe(padded, model_pred, outcome)

        # SUPO Compressor: add action as context segment
        if self.enabled:
            seg = Segment(
                seg_id=f"act_{self._spiral_count}_{int(time.time() * 1000)}",
                action_type="tool_result" if tool_name != "llm" else "assistant",
                content_length=0,
                token_estimate=token_count,
                action_name=tool_name,
                exit_code=0 if success else 1,
                succeeded=success,
            )
            self.compressor.add_segment(seg)

        # Nighttime: collect state vector for background clustering
        if self.enabled and len(state_vec) >= 12:
            self._night_vectors.append(state_vec[:12])
            if len(self._night_vectors) > 5000:
                self._night_vectors = self._night_vectors[-5000:]

        # PPO: record reward for the previous intervention action
        if self.enabled and self._ppo_episode_active and len(self._last_ppo_features) >= 12:
            reward = 1.0 if success else -0.5
            self.ppo.record(
                features=self._last_ppo_features,
                action=self._last_ppo_action,
                log_prob=self._last_ppo_logprob,
                value=self._last_ppo_value,
                reward=reward,
                done=False,
            )

        # Periodic nighttime engine tick
        self._tick_night()

    # ── UX event system ──

    def _emit_event(self, event_type: str, message: str,
                    data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Record a UX event (for UI display ⚡).

         Terminal UI can periodically pull get_recent_events() to display subconscious activity.
        """
        self._event_id += 1
        event = {
            "id": self._event_id,
            "type": event_type,
            "message": message,
            "data": data or {},
            "timestamp": time.time(),
        }
        self._event_log.append(event)
        # Keep at most 100 entries
        if len(self._event_log) > 100:
            self._event_log.pop(0)
        return event

    def get_recent_events(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent UX events."""
        return list(self._event_log[-limit:])

    # ── Snapshot ──

    def snapshot(self, tag: str = "manual") -> Dict[str, Any]:
        """Take a snapshot."""
        return self.snapshot_manager.snapshot(self.predictor, tag=tag)

    def rollback(self, snapshot_name: str) -> bool:
        """Rollback to specified snapshot."""
        model = self.snapshot_manager.rollback(snapshot_name)
        if model is None:
            return False
        self.predictor = model
        self._save_model()
        self._emit_event("rollback", f"⏪ Rollback to snapshot: {snapshot_name}")
        return True

    def list_snapshots(self) -> List[Dict[str, Any]]:
        return self.snapshot_manager.list_snapshots()

    def on_user_interrupt(self, ctrl_c: bool = False, edit_ratio: float = 0.0,
                          follow_up_count: int = 0):
        """Record user intervention."""
        if not self.enabled:
            return
        self.runtime.on_user_interrupt(
            ctrl_c=ctrl_c,
            edit_ratio=edit_ratio,
            follow_up_count=follow_up_count,
            spirals_completed=self._spiral_count,
        )

    def on_task_start(self, task_id: str):
        """Task start (for trajectory learning)."""
        if not self.enabled:
            return
        state_vec = self.feature_extractor.extract(
            spirals_completed=self._spiral_count,
        )
        self.runtime.on_task_start(task_id, state_vec)

        # Resource Scheduler: register new task
        self.resource_scheduler.register_task(
            task_id=task_id,
            name=f"task_{self._spiral_count}",
            estimated_cost=1000,
            budget=5000,
            priority=5,
        )

        # Self-hosted: start interrupt monitoring
        if self.plugins.enabled and self.plugins.enable_interrupts and self.plugins.interrupt:
            self.plugins.interrupt.start_monitoring()

    def on_task_end(self, task_id: str, success: bool,
                    task_type: str = "", tokens_used: int = 0,
                    latency_seconds: float = 0.0):
        """Task end (for trajectory + efficiency learning)."""
        if not self.enabled:
            return
        state_vec = self.feature_extractor.extract(
            spirals_completed=self._spiral_count,
        )
        self.runtime.on_task_end(
            task_id=task_id,
            final_state=state_vec,
            success=success,
            task_type=task_type,
            tokens_used=tokens_used,
            latency_seconds=latency_seconds,
            spirals=self._spiral_count,
        )

        # Resource Scheduler: complete task + update success prob
        self.resource_scheduler.complete(task_id)
        self.resource_scheduler.update_success_probability(task_id, success)
        if tokens_used > 0:
            self.resource_scheduler.consume(task_id, tokens_used)

        # SUPO Compressor: reward based on outcome
        if self.enabled:
            token_savings = self.compressor.estimate_token_count() / max(1, self.compressor.token_budget)
            self.compressor.reward(task_success=1.0 if success else 0.0,
                                   token_savings=1.0 - token_savings)

        # PPO: end episode with reward
        if self.enabled and self._ppo_episode_active:
            final_reward = 1.0 if success else -1.0
            self.ppo.end_episode(final_features=state_vec, final_reward=final_reward)
            self._ppo_episode_active = False
            # Trigger PPO update if enough steps
            if self.ppo.total_steps >= self.ppo.min_steps:
                try:
                    ppo_result = self.ppo.update()
                    if ppo_result.get("updated"):
                        self._emit_event("ppo_learn",
                                         f"🤖 PPO update: policy_loss={ppo_result.get('avg_policy_loss', 0):.6f}",
                                         {"steps": ppo_result.get("steps", 0),
                                          "avg_return": ppo_result.get("avg_return", 0)})
                except Exception as e:
                    logger.warning(f"PPO update failed: {e}")

        # Self-hosted: stop interrupt monitoring
        if self.plugins.enabled and self.plugins.interrupt:
            self.plugins.interrupt.stop_monitoring()
            # Provide feedback if task outcome is known
            reward = 1.0 if success else -0.5
            self.plugins.feedback(reward)

    def observe_memory_recall(self):
        if self.enabled:
            self.feature_extractor.observe_memory_recall()

    def notify_checkpoint(self):
        if self.enabled:
            self.feature_extractor.notify_checkpoint()

    def _tick_night(self):
        """Periodic nighttime engine tick — run clustering when enough data."""
        if not self.enabled:
            return
        now = time.time()
        if now - self._night_last_run < self._night_interval:
            return
        if len(self._night_vectors) < 10:
            return
        try:
            self._night_last_run = now
            triples = []
            for v in self._night_vectors:
                triples.append(TrainingTriple(
                    state_vector=v,
                    outcome=0.0,
                    timestamp=now,
                    source="night",
                ))
            self.night.feed(triples)
            result = self.night.run()
            if result.get("clustering", {}).get("n_clusters", 0) > 0:
                self._emit_event("night",
                                 f"🌙 Nighttime: {result['clustering']['n_clusters']} clusters, "
                                 f"{len(result.get('schema', {}).get('schemas', []))} schemas",
                                 {"clusters": result['clustering']['n_clusters'],
                                  "duration": result.get('duration_s', 0)})
        except Exception as e:
            logger.warning(f"Nighttime tick failed: {e}")

    # ── Prediction (evaluate failure risk) ──

    def predict(self, state_vector: Optional[List[float]] = None,
                step_data: Optional[Dict] = None) -> "TriageVector":
        """
        Predict triage vector (4 signals: crash risk, compression urgency, tool downgrade, mode switch).
        
        When self-hosted plugins are enabled, this also:
          - Extracts metacognitive probes from the LLM backend
          - Fills probe dimensions (19-23) in the feature vector
          - Checks the control gate for mode decisions

        Returns:
            TriageVector with crash_risk (0.0-1.0), compress_urgency, tool_downgrade, mode_switch
        """
        if not self.enabled:
            return TriageVector(crash_risk=0.0, compress_urgency=0.0,
                                tool_downgrade=0.0, mode_switch=0.0)
        if state_vector is None:
            state_vector = self.feature_extractor.extract(
                spirals_completed=self._spiral_count,
            )

        # Self-hosted plugin integration (if enabled)
        if self.plugins.enabled:
            # Extract probes from backend
            self.plugins.extract_probes(step_data=step_data or {})

            # Fill probe dimensions into the padded vector
            padded = pad_vector(state_vector)
            self.plugins.fill_probe_features(padded)

            # Fill interrupt features
            self.plugins.fill_interrupt_features(padded)

            normalized = self.feature_extractor.normalize(padded)
        else:
            normalized = self.feature_extractor.normalize(
                pad_vector(state_vector)
            )

        if self.predictor.empty():
            risk = self._heuristic_risk(state_vector)
            return TriageVector(crash_risk=risk, compress_urgency=min(1.0, risk * 0.5),
                                tool_downgrade=min(1.0, max(0.0, risk - 0.3) * 2),
                                mode_switch=min(1.0, max(0.0, risk - 0.5) * 2))
        triage = self.predictor.predict(normalized)
        # Feed temporal buffer for time-series path
        if hasattr(self.predictor, 'push_temporal'):
            self.predictor.push_temporal(normalized)
        # CFR regret-matching adjustment on crash_risk
        if self.cfr is not None:
            triage.crash_risk = self.cfr.adjust(normalized, triage.crash_risk)
        return triage

    def _heuristic_risk(self, vector: List[float]) -> float:
        """
        Model-free heuristic risk estimation.

        Only uses a few dimensions of the 12-dimensional vector, no language understanding.
        """
        if len(vector) < 12:
            return 0.0
        risk = 0.0
        # consecutive errors
        risk += min(1.0, vector[0] / 10) * 0.3
        # tool loop
        risk += min(1.0, vector[1] / 5) * 0.25
        # latency
        risk += min(1.0, vector[2] / 60) * 0.15
        # LLM empty response
        risk += vector[10] * 0.2
        # Long without checkpoint
        risk += min(1.0, vector[11] / 3600) * 0.1
        return min(1.0, risk)

    # ── Decide intervention ──

    def should_intervene(self) -> Dict[str, Any]:
        """
        Determine whether to intervene in main consciousness using Triage Vector.

        4-tier intervention based on 4-dimensional subconscious output:
          Tier 1 — Context injection: compress_urgency > 0.6 → suggest context compression
          Tier 2 — Tool sieving: tool_downgrade > 0.5 → restrict tool privilege
          Tier 3 — Cognitive mode switch: mode_switch > 0.3 → change thinking mode
          Tier 4 — Emergency rewind: crash_risk > 0.7 OR is_critical → rewind

        When self-hosted plugins are enabled, also evaluates the
        control gate for α weighting and mode decisions.

        Returns:
            {"intervene": bool, "reason": str, "action": str, 
             "risk": float (crash_risk), "triage": dict, ...}
        """
        if not self.enabled:
            return {"intervene": False, "reason": "disabled"}

        vec = self.feature_extractor.extract(
            spirals_completed=self._spiral_count,
        )

        # Resource Scheduler: evaluate task priorities
        scheduler_result = self.resource_scheduler.evaluate(vec)
        abandon_ids = scheduler_result.get("abandon_ids", [])

        # Predict Triage Vector (4 signals)
        triage = self.predict(vec)
        crash_risk = triage.crash_risk

        # PPO: get action suggestion for non-critical decisions
        ppo_action = -1
        ppo_guideline = ""
        if not triage.is_critical and self.ppo.total_steps > 0:
            try:
                padded = pad_vector(vec)
                norm_vec = self.feature_extractor.normalize(padded)
                ppo_action, ppo_logprob, ppo_val = self.ppo.get_action(norm_vec)
                self._last_ppo_action = ppo_action
                self._last_ppo_logprob = ppo_logprob
                self._last_ppo_value = ppo_val
                self._last_ppo_features = norm_vec
                if not self._ppo_episode_active:
                    self.ppo.start_episode()
                    self._ppo_episode_active = True
            except Exception:
                pass

        # Self-hosted plugin control gate evaluation (unchanged)
        if self.plugins.enabled and self.plugins.enable_control_gate:
            padded = pad_vector(vec)
            self.plugins.fill_probe_features(padded)
            gate_dec = self.plugins.evaluate_gate(padded, crash_risk)

            # If gate says interrupt, treat as high-priority intervention
            if gate_dec.get("should_interrupt", False):
                self._emit_event("interrupt",
                    f"⛔ Self-hosted interrupt triggered: {gate_dec.get('reason', '')}",
                    {"risk": round(crash_risk, 3), "alpha": round(gate_dec['alpha'], 3)})
                return {
                    "intervene": True,
                    "reason": f"Plugin interrupt: {gate_dec.get('reason', '')}",
                    "action": "interrupt",
                    "risk": round(crash_risk, 3),
                    "triage": triage.to_dict(),
                    "state_vector": vec,
                    "gate_decision": gate_dec,
                }

            # If in latent_thinking mode with high alpha, suggest prefix
            if (gate_dec.get("mode") == "latent_thinking"
                    and gate_dec.get("alpha", 0) > 0.6):
                # Don't mark as full intervention — just attach metadata
                pass

        # ── Tier 4: Emergency rewind (highest priority) ──
        if triage.is_critical:
            should_rewind, reason = self.rewind_engine.should_rewind(
                vec, crash_risk, self._last_phase_id, self._phase_repeat_count,
            )
            if should_rewind:
                self._emit_event("rewind",
                                 f"⚠️ Subconscious intervention: {reason}",
                                 {"risk": round(crash_risk, 3), "action": "rewind",
                                  "triage": triage.to_dict()})
                return {
                    "intervene": True,
                    "reason": reason,
                    "action": "rewind",
                    "risk": round(crash_risk, 3),
                    "triage": triage.to_dict(),
                    "state_vector": vec,
                }

        # ── High risk → warn + Tier 2/3: tool downgrade or mode switch ──
        if crash_risk >= 0.5:
            action = "warn"
            reason = f"Risk {crash_risk:.2f}, suggest observation"
            guideline = ""

            # Propensity signals
            if triage.mode_switch >= 0.3:
                action = "mode_switch"
                reason = f"Mode switch to {triage.mode_name} (mode_switch={triage.mode_switch:.2f})"
                guideline = "The subconscious recommends switching to a more focused thinking mode. Current mode may be causing repetitive patterns. Try to re-evaluate the approach and be more decisive."

            if triage.should_downgrade:
                action = "tool_downgrade"
                reason = f"Tool downgrade needed (tool_downgrade={triage.tool_downgrade:.2f})"
                guideline = "The subconscious detected potential tool over-use or anomalous patterns. Stay focused on the core goal and avoid unnecessary tool calls. Prefer reading/observation over destructive actions."

            self._emit_event(action,
                             f"⚡ Subconscious intervention: {reason}",
                             {"risk": round(crash_risk, 3), "action": action,
                              "triage": triage.to_dict()})
            return {
                "intervene": True,
                "reason": reason,
                "action": action,
                "guideline": guideline,
                "risk": round(crash_risk, 3),
                "triage": triage.to_dict(),
                "state_vector": vec,
            }

        # ── Tier 1: Context compression ──
        if triage.needs_compression:
            # Run actual SUPO context evaluation
            compress_decisions = self.compressor.evaluate()
            kept = sum(1 for k in compress_decisions.values() if k)
            pruned = sum(1 for k in compress_decisions.values() if not k)
            return {
                "intervene": True,
                "reason": f"Context compression suggested (compress_urgency={triage.compress_urgency:.2f})",
                "action": "compress",
                "guideline": "The subconscious suggests that your current context may be getting too large or unfocused. Prioritize concise outputs and avoid expanding the conversation with verbose actions or exploratory detours.",
                "risk": round(crash_risk, 3),
                "triage": triage.to_dict(),
                "state_vector": vec,
                "compressor": {
                    "segments": len(compress_decisions),
                    "kept": kept,
                    "pruned": pruned,
                },
                "ppo_action": ppo_action,
                "scheduler": scheduler_result if abandon_ids else None,
            }

        result = {
            "intervene": False,
            "reason": f"Risk {crash_risk:.2f}, normal",
            "action": "noop",
            "risk": round(crash_risk, 3),
            "triage": triage.to_dict(),
            "scheduler": scheduler_result if abandon_ids else None,
        }
        # If PPO suggests a non-none action and risk is moderate, hint it
        if ppo_action > 0 and 0.3 <= crash_risk < 0.5:
            action_names = {1: "system_prompt", 2: "param_tune", 3: "action_code"}
            result["ppo_hint"] = action_names.get(ppo_action, "none")
        return result

    # ── training ──

    def contrastive_train(self) -> Dict[str, Any]:
        """
        executecontrastive learningtraining。

        Take data from runtime collector signal pipeline, execute DPO-style contrastive learning.
        Do not change tree structure, only adjust leaf node values.
        """
        if not self.enabled:
            return {"trained": False, "reason": "disabled"}
        result = self.runtime.train()
        if result.get("updated"):
            self._emit_event("learn",
                             f"🧠 Subconscious learning: {result.get('leaves_pushed', 0)} leaf node adjustments",
                             {"leaves_pushed": result.get("leaves_pushed"),
                              "margin_resolved": result.get("margin_resolved")})
            # Contrastive learning auto snapshot
            self.snapshot(tag="post_learn")
        return result

    def record_training_sample(self, state_vector: List[float],
                                outcome: float):
        """
        recorda trainingsample。

        Args:
            state_vector: line dynamic 12-dimensional state vector
            outcome: 0.0 = success, 1.0 = failure/crash
        """
        if len(state_vector) < 12:
            return
        self._training_buffer_x.append(pad_vector(state_vector))
        self._training_buffer_y.append(outcome)

        # Autotraining: only trigger after first spiral completes
        # (guard against 0 % N == 0 bug that fires on every call)
        if self._spiral_count > 0 and self._spiral_count % self.auto_train_interval == 0:
            self.train()

    def train(self, epochs: int = 10) -> Dict[str, Any]:
        """
        Training random forest model.

        Take samples from training buffer, oversampling failure cases (class imbalance process).
        """
        X = self._training_buffer_x
        y = self._training_buffer_y

        if len(X) < 5:
            return {"trained": False, "reason": f"Insufficient samples ({len(X)} < 5)"}

        # Oversampling failure cases
        failure_indices = [i for i, v in enumerate(y) if v >= 0.5]
        success_indices = [i for i, v in enumerate(y) if v < 0.5]

        # Sampling: keep at least 30% failure cases
        import random as rnd
        if failure_indices and len(failure_indices) < len(success_indices) * 0.3:
            extra = int(len(success_indices) * 0.3) - len(failure_indices)
            sampled_extra = rnd.choices(failure_indices, k=extra)
            indices = success_indices + failure_indices + sampled_extra
        else:
            indices = list(range(len(X)))

        X_train = [X[i] for i in indices]
        y_train = [y[i] for i in indices]

        # normalize
        X_norm = [self.feature_extractor.normalize(x) for x in X_train]

        # training
        self.predictor.fit(X_norm, y_train, dp=self.privacy, epochs=epochs)
        self._training_count += 1

        # Save model
        self._save_model()

        pos = sum(y_train)
        neg = len(y_train) - pos
        return {
            "trained": True,
            "samples": len(X_train),
            "model_size": self.predictor.model_size(),
            "positive": pos,
            "negative": neg,
        }

    # ── Execute rewind ──

    def execute_rewind(self, reason: str, state_vector: List[float],
                       risk: float) -> Dict[str, Any]:
        """Execute rewind recovery + record training sample + inject guardrails."""
        event = self.rewind_engine.execute_rewind(
            reason=reason,
            state_vector=state_vector,
            failure_risk=risk,
            failed_tool_sequence=self._tool_sequence[-10:],
        )

        # Record training sample (failure = 1.0)
        self.record_training_sample(state_vector, outcome=1.0)

        # ── Offline trajectory analysis + Guardrail injection ──
        guardrail = self._generate_guardrail(reason, self._tool_sequence[-10:])
        if guardrail:
            self._latest_guardrail = guardrail
            self._emit_event("guardrail",
                             f"🛡️ Guardrail injected: {guardrail[:80]}",
                             {"guardrail": guardrail, "risk": round(risk, 3)})

        # ── CFR learning from failure trajectory ──
        try:
            cfr_result = self.contrastive_train()
            if cfr_result.get("updated"):
                self._emit_event("cfr_learn",
                                 f"🧪 CFR updated: {cfr_result.get('leaves_pushed', 0)} adjustments",
                                 cfr_result)
        except Exception:
            self._emit_event("cfr_error", "CFR training failed after rewind", {})

        # Commit federation report
        from p2p.federation import CrashReport
        report = CrashReport(
            node_id=self.federation.node_id,
            trigger_event=reason,
            state_vector_before_crash=state_vector,
            failed_tool_sequence=self._tool_sequence[-10:],
            successful_correction="rewind_with_intuition",
            reward=1.0 if event.recovered else 0.0,
        )
        self.federation.submit_local_report(report)

        return event.to_dict()

    def _generate_guardrail(self, reason: str,
                            tool_sequence: List[str]) -> str:
        """Analyze failure pattern and generate a preventive guardrail.

        Extracts the dominant failure pattern from tool sequence + reason,
        and produces a human-readable guardrail for the spiral loop to
        inject into the system prompt.
        """
        # Pattern: repeated tool failures
        from collections import Counter
        tool_counter = Counter(t for t in tool_sequence if t)
        most_common = tool_counter.most_common(3)

        guardrails = []
        if "rewind" in reason.lower() or "loop" in reason.lower():
            guardrails.append(
                "Avoid getting stuck in a rewind loop. "
                "If the same approach fails twice, switch strategies entirely."
            )
        if most_common:
            repeated = [f"'{t}' ({c}x)" for t, c in most_common if c >= 2]
            if repeated:
                guardrails.append(
                    f"Tools over-used in the failed segment: "
                    f"{', '.join(repeated[:2])}. "
                    f"Prefer alternative approaches."
                )
        if "error" in reason.lower() or "exception" in reason.lower():
            guardrails.append(
                "Previous session crashed with an error. "
                "Verify preconditions before retrying."
            )

        if not guardrails:
            guardrails.append(
                "Avoid repeating the previous failure pattern. "
                "Try a fundamentally different approach."
            )
        return " | ".join(guardrails)

    # ── blockchainnetworkreceive ──

    def _receive_block(self, block_data: dict) -> bool:
        """Receive new block from P2P network."""
        if not self.blockchain:
            return False
        try:
            from p2p.blockchain import Block
            block = Block.from_dict(block_data)
            return self.blockchain.add_block(block, broadcast=False)
        except Exception:
            return False

    def _receive_tx(self, tx_data: dict) -> bool:
        """Receive new transaction from P2P network."""
        if not self.blockchain:
            return False
        try:
            from p2p.blockchain import Transaction
            tx = Transaction.from_dict(tx_data)
            return self.blockchain.add_transaction(tx)
        except Exception:
            return False

    # ── state ──

    def get_status(self) -> Dict[str, Any]:
        v = self.feature_extractor.extract(spirals_completed=self._spiral_count)
        triage = self.predict(v)
        return {
            "enabled": self.enabled,
            "spirals_observed": self._spiral_count,
            "training_count": self._training_count,
            "model_trained": not self.predictor.empty(),
            "model_size": self.predictor.model_size(),
            "current_failure_risk": round(triage.crash_risk, 3),
            "triage": triage.to_dict(),
            "state_vector": [round(x, 3) for x in v],
            "privacy": self.privacy.stats(),
            "snapshots": self.snapshot_manager.stats(),
            "blockchain": {
                "height": self.blockchain.height if self.blockchain else -1,
                "blocks": len(self.blockchain.chain) if self.blockchain else 0,
                "peers": len(self.blockchain.mempool) if self.blockchain else 0,
            } if self.blockchain else None,
            "p2p": {
                "running": self.p2p.running if self.p2p else False,
                "peers": len(self.p2p.peers) if self.p2p else 0,
                "mode": "public" if (self.p2p and self.p2p.public_mode) else "private",
                "dht": self.p2p.dht.stats() if self.p2p else None,
            } if self.p2p else None,
            "gossip": {
                "enabled": self.gossip.running if self.gossip else False,
                "count": self.gossip._gossip_count if self.gossip else 0,
            } if self.gossip else None,
            "plugins": self.plugins.status() if self.plugins.enabled else {
                "enabled": False,
            },
        }

    def get_stats(self) -> Dict[str, Any]:
        return {
            "feature_extractor": self.feature_extractor.stats(),
            "rewind_engine": self.rewind_engine.stats(),
            "federation": self.federation.stats(),
            "gossip": self.gossip.stats() if self.gossip else None,
            "runtime_collector": self.runtime.stats(),
            "training_buffer": len(self._training_buffer_x),
            "cfr": self.cfr.stats() if self.cfr is not None else None,
            "compressor": self.compressor.stats() if self.enabled else None,
            "night": {"run_count": self.night._run_count,
                       "vectors": len(self._night_vectors),
                       "last_run_ago": round(time.time() - self._night_last_run, 1)
                       if self._night_last_run else 0},
            "ppo": {
                "total_steps": self.ppo.total_steps,
                "update_count": self.ppo.update_count,
                "avg_return": round(self.ppo.avg_return, 4),
                "temperature": round(self.ppo.temperature, 3),
            } if self.enabled else None,
            "resource_scheduler": self.resource_scheduler.stats() if self.enabled else None,
            "predictor": {
                "model": "DeepRiskNet",
                "hidden_dim": self.predictor.hidden_dim,
                "size": self.predictor.model_size(),
                "params": self.predictor._param_count,
                "trained": self.predictor._has_trained,
            },
        }

    # ── Persistence ──

    def _save_model(self):
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        state = json.loads(self.predictor.to_json())
        # Append CFR state
        if self.cfr is not None:
            state["cfr"] = {
                "regret": {k: v for k, v in self.cfr._regret.items()},
                "action_count": {k: v for k, v in self.cfr._action_count.items()},
                "obs_count": {k: v for k, v in self.cfr._obs_count.items()},
                "total_updates": self.cfr._total_updates,
                "total_adjustments": self.cfr._total_adjustments,
            }
        # Append training buffer (persist across restarts)
        state["training_buffer_x"] = self._training_buffer_x[-200:]
        state["training_buffer_y"] = self._training_buffer_y[-200:]
        # Append additional module states
        state["compressor"] = self.compressor.stats()
        state["night_interval"] = self._night_interval
        state["night_vectors"] = [[round(v, 4) for v in vec] for vec in self._night_vectors[-200:]]
        state["resource_scheduler"] = {
            "decision_count": self.resource_scheduler._decision_count,
        }
        # PPO: save via its own checkpoint mechanism
        try:
            self.ppo.save_checkpoint(os.path.join(SUBCONSCIOUS_DIR, "ppo", "checkpoint.json"))
        except Exception:
            pass
        with open(self.model_path, "w") as f:
            json.dump(state, f, indent=2)

    def _load_model(self):
        if os.path.isfile(self.model_path):
            try:
                with open(self.model_path) as f:
                    data = json.load(f)
                # Check if it's a new-format model
                if data.get("arch") == "DeepRiskNet":
                    self.predictor = DeepRiskNet.from_dict(data)
                    self.predictor._has_trained = True
                    # Force enable temporal mode on loaded models
                    if self.predictor.temporal_buffer is None:
                        from core.predictor import TemporalBuffer, TemporalConvNet
                        self.predictor.use_temporal = True
                        self.predictor.temporal_buffer = TemporalBuffer(
                            capacity=8, input_dim=self.predictor.n_features)
                        self.predictor.temporal_conv = TemporalConvNet(
                            input_dim=self.predictor.n_features, hidden_dim=32,
                            output_dim=32, buffer_size=8)
                        self.predictor._param_count += (
                            self.predictor.temporal_conv.param_count())
                else:
                    # Old RandomForest format — ignore and start fresh
                    logger.info("Old model format detected, starting fresh neural network")
                    self.predictor = DeepRiskNet(
                        n_features=PADDED_FEATURES,
                        hidden_dim=64,
                        dropout=0.1,
                        lr=0.001,
                    )
                logger.info(f"🧠 Loaded model: {self.predictor.model_size()}")
                # Load CFR state
                if self.cfr is not None and "cfr" in data:
                    cfr_state = data["cfr"]
                    self.cfr._regret = {
                        k: v for k, v in cfr_state.get("regret", {}).items()
                    }
                    self.cfr._action_count = {
                        k: v for k, v in cfr_state.get("action_count", {}).items()
                    }
                    self.cfr._obs_count = {
                        k: v for k, v in cfr_state.get("obs_count", {}).items()
                    }
                    self.cfr._total_updates = cfr_state.get("total_updates", 0)
                    self.cfr._total_adjustments = cfr_state.get("total_adjustments", 0.0)
                    logger.info(f"🧪 CFR restored: {len(self.cfr._regret)} buckets, "
                                f"{self.cfr._total_updates} total updates")
                # Load PPO checkpoint
                try:
                    ppo_ckpt = os.path.join(SUBCONSCIOUS_DIR, "ppo", "checkpoint.json")
                    if os.path.isfile(ppo_ckpt):
                        self.ppo.load_checkpoint(ppo_ckpt)
                        logger.info(f"🤖 PPO checkpoint available: {self.ppo.total_steps} steps")
                except Exception:
                    pass
                # Restore nighttime vectors from saved state
                if "night_vectors" in data:
                    self._night_vectors = data["night_vectors"]
                # Restore training buffer from saved state (survive restarts)
                if "training_buffer_x" in data and "training_buffer_y" in data:
                    self._training_buffer_x = data["training_buffer_x"]
                    self._training_buffer_y = data["training_buffer_y"]
                    logger.info(f"🧠 Training buffer restored: {len(self._training_buffer_x)} samples")
            except Exception as e:
                logger.warning(f"Load model failed: {e}")
