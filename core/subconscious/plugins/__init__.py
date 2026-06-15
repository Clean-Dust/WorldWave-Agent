"""Self-hosted LLM plugin system for the Worldwave subconscious.

Provides metacognitive probes, latent decoding, control gates,
thinking token injection, and interrupt capabilities for users
who run their own LLM (transformers, llama.cpp, etc.).

All plugins are DISABLED by default. Users with self-hosted models
enable them in their config:

    config = {
        "self_hosted": {
            "enabled": True,
            "backend": "transformers",
            "backend_config": {
                "model_name_or_path": "Qwen/Qwen2.5-7B-Instruct",
                "device": "cuda",
            },
            "enable_prefix_injection": True,
            "enable_interrupts": True,
            "enable_probes": True,
            "enable_control_gate": True,
            "enable_thinking_token": True,
        }
    }
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import (
    BackendPlugin, PluginHost,
    HiddenStateSlice, AttentionInfo, LogitInfo,
    PrefixPayload,
    BackendNotReadyError, BackendUnsupportedError,
)
from .backends import get_backend, list_backends
from .latent_decoder import LatentDecoder
from .probes import (
    ProbeAggregator, ProbeSignal,
    token_level_entropy,
    attention_sparsity as compute_attention_sparsity,
    logit_magnitude as compute_logit_magnitude,
    hidden_state_norm as compute_hidden_state_norm,
    thinking_tokens_ratio as compute_thinking_tokens_ratio,
)
from .control_gate import ControlGate, GateMode
from .thinking_token import TokenExtensionManager
from .interrupt import InterruptController

# Constants
DEFAULT_ENABLED = False
DEFAULT_BACKEND = "simulated"
PROBE_INDEX_MAP = {
    "token_entropy": 19,
    "attention_sparsity": 20,
    "logit_magnitude": 21,
    "hidden_state_norm": 22,
    "thinking_tokens_ratio": 23,
}

logger = logging.getLogger("ww.subconscious.plugins")


class SelfHostedPluginManager:
    """Ties together all self-hosted LLM plugins.

    One manager per Subconscious instance. Handles lifecycle:
      - Backend connection
      - Probe extraction → feature vector filling
      - Control gate decisions → α output
      - Latent decoding → prefix generation
      - Interrupt monitoring → generation halting

    All features are opt-in via config flags.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg: Dict[str, Any] = config or {}
        self.enabled = cfg.get("enabled", DEFAULT_ENABLED)
        self.backend_name = cfg.get("backend", DEFAULT_BACKEND)

        # Individual feature flags
        self.enable_prefix_injection = cfg.get(
            "enable_prefix_injection", False)
        self.enable_interrupts = cfg.get("enable_interrupts", False)
        self.enable_probes = cfg.get("enable_probes", True)
        self.enable_control_gate = cfg.get("enable_control_gate", False)
        self.enable_thinking_token = cfg.get(
            "enable_thinking_token", False)

        # Backend
        self.backend: Optional[BackendPlugin] = None
        self.backend_ready = False
        self.backend_config = cfg.get("backend_config", {})

        # Plugin instances (lazy init)
        self.decoder: Optional[LatentDecoder] = None
        self.probes: Optional[ProbeAggregator] = None
        self.gate: Optional[ControlGate] = None
        self.token_mgr: Optional[TokenExtensionManager] = None
        self.interrupt: Optional[InterruptController] = None

        # Data paths
        self._data_dir = os.path.expanduser(
            cfg.get("data_dir", "~/.ww/plugins")
        )

        # Feature vector modification callback
        self._feature_callback: Optional[
            Callable[[List[float]], None]
        ] = None

        # Latest gate decision (for logging/inspection)
        self._last_gate_decision: Dict[str, Any] = {
            "alpha": 0.0, "mode": "normal",
            "confidence": 0.0, "reason": "not_started",
        }

    # ── Lifecycle ──

    def initialize(self) -> None:
        """Initialize all enabled plugins. Called once at startup."""
        if not self.enabled:
            logger.info("Self-hosted plugins disabled (default)")
            return

        # 1. Connect backend
        try:
            self.backend = get_backend(
                self.backend_name, self.backend_config
            )
            self.backend_ready = self.backend.validate()
            logger.info(f"Backend '{self.backend_name}' "
                        f"{'ready' if self.backend_ready else 'not ready'}")
        except Exception as e:
            logger.warning(f"Failed to initialize backend "
                           f"'{self.backend_name}': {e}")
            self.backend_ready = False

        # 2. Latent decoder
        if self.enable_prefix_injection:
            self.decoder = LatentDecoder(
                latent_noise_dim=16,
                embedding_dim=256,
                learning_rate=1e-3,
            )
            dec_path = self._get_data_path("decoder.json")
            if os.path.isfile(dec_path):
                try:
                    self.decoder = LatentDecoder.load(dec_path)
                    logger.info(f"Loaded decoder ({dec_path})")
                except Exception as e:
                    logger.warning(f"Failed to load decoder: {e}")
            logger.info(f"Latent decoder initialized "
                        f"(~{self.decoder.param_count()} params)")

        # 3. Probes
        if self.enable_probes:
            self.probes = ProbeAggregator(max_history=10)
            logger.info("Probe aggregator initialized")

        # 4. Control gate
        if self.enable_control_gate:
            gate_path = self._get_data_path("control_gate.json")
            try:
                self.gate = ControlGate.load(gate_path) if os.path.isfile(
                    gate_path) else ControlGate()
            except Exception:
                self.gate = ControlGate()
            logger.info("Control gate initialized")

        # 5. Thinking token manager
        if self.enable_thinking_token:
            token_path = self._get_data_path("thinking_tokens.json")
            try:
                self.token_mgr = (
                    TokenExtensionManager.load(token_path)
                    if os.path.isfile(token_path)
                    else TokenExtensionManager()
                )
            except Exception:
                self.token_mgr = TokenExtensionManager()
            self.token_mgr.add_builtins()
            logger.info(f"Token manager initialized "
                        f"({self.token_mgr.total_tokens()} tokens)")

        # 6. Interrupt controller
        if self.enable_interrupts:
            self.interrupt = InterruptController()
            if self.backend_ready and self.backend:
                self.interrupt.start_monitoring()
            logger.info("Interrupt controller initialized")

        logger.info("Self-hosted plugin system initialized")

    def shutdown(self) -> None:
        """Save state and release resources."""
        self._save_all()
        if self.backend:
            try:
                self.backend.remove_interrupt_callback()
            except Exception:
                pass
        self.backend_ready = False
        logger.info("Self-hosted plugins shut down")

    def _save_all(self) -> None:
        """Save all plugin state to disk."""
        if self.decoder:
            try:
                self.decoder.save(self._get_data_path("decoder.json"))
            except Exception as e:
                logger.warning(f"Failed to save decoder: {e}")
        if self.gate:
            try:
                self.gate.save(self._get_data_path("control_gate.json"))
            except Exception as e:
                logger.warning(f"Failed to save gate: {e}")
        if self.token_mgr:
            try:
                self.token_mgr.save(
                    self._get_data_path("thinking_tokens.json"))
            except Exception as e:
                logger.warning(f"Failed to save tokens: {e}")
        if self.interrupt:
            try:
                self.interrupt.save(
                    self._get_data_path("interrupt.json"))
            except Exception as e:
                logger.warning(f"Failed to save interrupt: {e}")

    def _get_data_path(self, name: str) -> str:
        return os.path.join(self._data_dir, name)

    # ── Probe Extraction ──

    def extract_probes(self, step_data: Optional[Dict] = None) -> None:
        """Extract metacognitive probes from the backend and update
        the probe aggregator."""
        if not self.enabled or not self.enable_probes:
            return
        if not self.backend_ready or not self.backend:
            return

        try:
            # Logit-based probes
            try:
                logit_info = self.backend.get_logits()
                if logit_info.token_ids:
                    ent_signal = token_level_entropy(logit_info.logprobs)
                    mag_signal = compute_logit_magnitude(
                        logit_info.logprobs)
                else:
                    ent_signal = ProbeSignal(
                        "token_entropy", 0.5, 0.0)
                    mag_signal = ProbeSignal(
                        "logit_magnitude", 0.5, 0.0)
            except BackendUnsupportedError:
                ent_signal = ProbeSignal("token_entropy", 0.5, 0.0)
                mag_signal = ProbeSignal("logit_magnitude", 0.5, 0.0)

            # Attention-based probes
            try:
                attn_info = self.backend.get_attention_info()
                sparsity_signal = compute_attention_sparsity(attn_info)
            except BackendUnsupportedError:
                sparsity_signal = ProbeSignal(
                    "attention_sparsity", 0.5, 0.0)

            # Hidden state probes
            try:
                hs = self.backend.get_hidden_states(
                    pool="last")
                if hs:
                    # Use last layer
                    last = max(hs.keys())
                    norm_signal = compute_hidden_state_norm(
                        hs[last].values)
                else:
                    norm_signal = ProbeSignal(
                        "hidden_state_norm", 0.5, 0.0)
            except (BackendUnsupportedError, BackendNotReadyError):
                norm_signal = ProbeSignal(
                    "hidden_state_norm", 0.5, 0.0)

            # Thinking tokens ratio
            if self.token_mgr and step_data:
                token_ids = step_data.get("token_ids", [])
                think_ids = self.token_mgr.get_all_token_ids()
                think_signal = compute_thinking_tokens_ratio(
                    token_ids, think_ids)
            else:
                think_signal = ProbeSignal(
                    "thinking_tokens_ratio", 0.0, 0.0)

            # Record all
            if self.probes:
                for sig in [ent_signal, sparsity_signal,
                            mag_signal, norm_signal, think_signal]:
                    self.probes.record(sig)

        except Exception as e:
            logger.debug(f"Probe extraction error: {e}")

    def fill_probe_features(self, features: List[float]) -> None:
        """Fill metacognitive probe dimensions (19-23) in the feature
        vector."""
        if not self.enabled or not self.enable_probes or not self.probes:
            return
        self.probes.fill_features(features, PROBE_INDEX_MAP)

    # ── Control Gate ──

    def evaluate_gate(
        self, features: List[float], risk_score: float
    ) -> Dict[str, Any]:
        """Evaluate the control gate and return decision dict."""
        if not self.enabled or not self.enable_control_gate or not self.gate:
            return {
                "alpha": 0.0, "mode": "normal",
                "confidence": 1.0, "should_interrupt": False,
                "alpha_split": 0.0, "alpha_diffuse": 0.0,
                "reason": "gate_disabled",
            }

        decision = self.gate.evaluate(features, risk_score)
        self._last_gate_decision = decision
        return decision

    # ── Prefix Generation ──

    def generate_prefix(
        self, n_prefixes: int = 4
    ) -> Optional[PrefixPayload]:
        """Generate a prefix embedding from the latent decoder."""
        if not self.enabled or not self.enable_prefix_injection:
            return None
        if not self.decoder:
            return None

        vector = self.decoder.decode_multiple(n_prefixes=n_prefixes)
        return PrefixPayload(
            embedding_vector=vector,
            length_tokens=n_prefixes,
            intent="latent_context",
        )

    def inject_prefix(self, payload: PrefixPayload) -> bool:
        """Inject a prefix embedding into the backend."""
        if not self.enabled or not self.backend_ready or not self.backend:
            return False
        try:
            self.backend.inject_prefix_embeddings(payload)
            return True
        except (BackendUnsupportedError, Exception) as e:
            logger.debug(f"Prefix injection failed: {e}")
            return False

    def clear_prefix(self) -> None:
        """Clear injected prefix embeddings."""
        if self.backend:
            try:
                self.backend.clear_prefix_embeddings()
            except Exception:
                pass

    # ── Thinking Tokens ──

    def get_control_tokens(
        self, features: List[float], risk_score: float,
        gen_stage: str = "start",
    ) -> List[str]:
        """Get control tokens to inject at the current generation stage."""
        if not self.enabled or not self.enable_thinking_token:
            return []
        if not self.token_mgr:
            return []

        mode = (self._last_gate_decision.get("mode", "normal")
                if self.gate else "normal")
        return self.token_mgr.decide_injection(
            features, risk_score, mode, gen_stage
        )

    # ── Interrupt ──

    def check_interrupt(
        self,
        risk_score: float,
        token_entropy: float = 0.5,
        attention_sparsity: float = 0.5,
        logit_magnitude: float = 0.5,
        hidden_state_norm: float = 0.5,
        gate_confidence: float = 0.5,
    ) -> Tuple[bool, str]:
        """Check if generation should be interrupted."""
        if not self.enabled or not self.enable_interrupts:
            return (False, "interrupts_disabled")
        if not self.interrupt:
            return (False, "no_interrupt_controller")

        return self.interrupt.should_interrupt(
            risk_score=risk_score,
            token_entropy=token_entropy,
            attention_sparsity=attention_sparsity,
            logit_magnitude=logit_magnitude,
            hidden_state_norm=hidden_state_norm,
            gate_confidence=gate_confidence,
        )

    def resolve_interrupt(self, params: Optional[Dict] = None) -> None:
        """Resolve an active interrupt."""
        if self.interrupt:
            self.interrupt.resolve(params)

    def fill_interrupt_features(
        self, features: List[float]
    ) -> None:
        """Fill interrupt-related feature slots."""
        if self.enabled and self.interrupt:
            self.interrupt.fill_interrupt_features(features)

    # ── Feedback / Learning ──

    def feedback(self, reward: float) -> None:
        """Provide outcome reward to trainable components."""
        if self.decoder:
            # Simple training step
            self.decoder.train_step_heuristic(
                self.decoder.decode(), reward
            )
        if self.gate:
            self.gate.feedback(reward)

    # ── Status ──

    def status(self) -> Dict[str, Any]:
        """Get a status dict for inspection/debugging."""
        backends = list_backends()
        return {
            "enabled": self.enabled,
            "backend": self.backend_name,
            "backend_ready": self.backend_ready,
            "available_backends": backends,
            "features": {
                "prefix_injection": self.enable_prefix_injection,
                "probes": self.enable_probes,
                "control_gate": self.enable_control_gate,
                "thinking_token": self.enable_thinking_token,
                "interrupts": self.enable_interrupts,
            },
            "last_gate_decision": self._last_gate_decision,
            "has_decoder": self.decoder is not None,
            "has_probes": self.probes is not None,
            "has_gate": self.gate is not None,
            "has_token_mgr": self.token_mgr is not None,
            "has_interrupt": self.interrupt is not None,
            "interrupt_state": (
                self.interrupt.state if self.interrupt else "n/a"
            ),
            "gate_mode": (
                self.gate.current_mode if self.gate else "n/a"
            ),
        }

    def __repr__(self) -> str:
        return (f"SelfHostedPluginManager(enabled={self.enabled}, "
                f"backend={self.backend_name}, "
                f"ready={self.backend_ready})")


# ── Convenience exports ──

__all__ = [
    "SelfHostedPluginManager",
    "PluginHost",
    "BackendPlugin",
    "LatentDecoder",
    "ProbeAggregator",
    "ControlGate",
    "TokenExtensionManager",
    "InterruptController",
    "ProbeSignal",
    "PrefixPayload",
    "get_backend",
    "list_backends",
    "DEFAULT_ENABLED",
    "PROBE_INDEX_MAP",
]
