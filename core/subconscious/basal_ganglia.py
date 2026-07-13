"""
ww/core/subconscious/basal_ganglia.py — Basal Ganglia v0.1

Biomimetic dual-pathway action selection with explicit G/N matrix separation.

Inspired by the basal ganglia's direct (D1/Go) and indirect (D2/NoGo) pathways:

G-matrix (D1-SPNs / Direct Pathway):
    Learns expected positive reward for executing an action in a given state.
    "What good things happen if I do this?"

N-matrix (D2-SPNs / Indirect Pathway):
    Learns expected risk/penalty for executing an action in a given state.
    "What bad things can happen if I do this?"

Pulley Model:
    P(action | state) = softmax(G(state, action) - lambda * N(state, action))
    where lambda is the stress/caution coefficient (modulated by amygdala).

This replaces single-output risk scoring with explicit dual-pathway competition,
enabling the safety inhibition described in the Gemini blueprint:
    "When a dangerous action is proposed, the N-matrix inhibition signal
     instantly overrides the G-matrix promotion signal."

Pure Python, zero external dependencies.
"""

from __future__ import annotations
import json
import math
import random
import time
from typing import Any, Dict, List, Tuple


# ── Math utilities (self-contained, no numpy) ──

def _randn() -> float:
    u1 = random.random()
    u2 = random.random()
    return math.sqrt(-2.0 * math.log(max(u1, 1e-10))) * math.cos(2.0 * math.pi * u2)

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, x))))

def _softmax(scores: List[float]) -> List[float]:
    max_s = max(scores)
    exps = [math.exp(s - max_s) for s in scores]
    total = sum(exps)
    return [e / max(total, 1e-10) for e in exps]

def _dot(a: List[float], b: List[float]) -> float:
    return sum(a[i] * b[i] for i in range(len(a)))

def _matvec(M: List[List[float]], v: List[float]) -> List[float]:
    return [sum(M[i][j] * v[j] for j in range(len(v))) for i in range(len(M))]


# ── Action definitions ──

# Tool categories that the basal ganglia evaluates
ACTION_CATEGORIES = {
    "safe_read": 0,      # read_file, search_files, ls — always low risk
    "safe_info": 1,      # web_search, web_extract — low risk
    "modify_local": 2,   # write_file, patch, terminal (local) — medium risk
    "modify_remote": 3,  # git push, deploy, scp — HIGH risk
    "delete": 4,         # rm, drop, delete — HIGH risk
    "system": 5,         # systemctl, kill, reboot — CRITICAL risk
    "unsafe": 6,         # force push, sudo, chmod 777 — MAX risk
}

# Base risk priors per category (before learning)
BASE_RISK = {
    "safe_read": 0.02,
    "safe_info": 0.05,
    "modify_local": 0.25,
    "modify_remote": 0.55,
    "delete": 0.75,
    "system": 0.85,
    "unsafe": 0.95,
}

# Base reward priors per category
BASE_REWARD = {
    "safe_read": 0.3,
    "safe_info": 0.4,
    "modify_local": 0.6,
    "modify_remote": 0.5,
    "delete": 0.3,
    "system": 0.3,
    "unsafe": 0.1,
}


class DualPathwayNetwork:
    """G-matrix and N-matrix as twin neural networks.

    Both take the same state vector and produce action-specific scores.
    - G-network: predicts reward (higher = promote action)
    - N-network: predicts risk (higher = inhibit action)

    Each is a simple 2-layer MLP for efficiency and interpretability.
    """

    def __init__(self, state_dim: int = 32, n_actions: int = 7,
                 hidden_dim: int = 24):
        self.state_dim = state_dim
        self.n_actions = n_actions
        self.hidden_dim = hidden_dim

        # ── G-network (Reward / Direct pathway) ──
        scale_g = math.sqrt(2.0 / state_dim)
        self.G_W1 = [[_randn() * scale_g for _ in range(state_dim)]
                      for _ in range(hidden_dim)]
        self.G_b1 = [0.0] * hidden_dim
        self.G_W2 = [[_randn() * scale_g for _ in range(hidden_dim)]
                      for _ in range(n_actions)]
        self.G_b2 = [BASE_REWARD.get(k, 0.3) for k in ACTION_CATEGORIES]

        # ── N-network (Risk / Indirect pathway) ──
        scale_n = math.sqrt(2.0 / state_dim)
        self.N_W1 = [[_randn() * scale_n for _ in range(state_dim)]
                      for _ in range(hidden_dim)]
        self.N_b1 = [0.0] * hidden_dim
        self.N_W2 = [[_randn() * scale_n for _ in range(hidden_dim)]
                      for _ in range(n_actions)]
        self.N_b2 = [BASE_RISK.get(k, 0.5) for k in ACTION_CATEGORIES]

        # ── Learning rates ──
        self.lr_G = 0.01
        self.lr_N = 0.01

        # ── Stats ──
        self.train_count = 0
        self.total_G_loss = 0.0
        self.total_N_loss = 0.0

    # ── Forward passes ──

    def forward_G(self, state: List[float]) -> List[float]:
        """G-network forward: state → reward predictions per action [0, 1]."""
        h = [_sigmoid(_dot(self.G_W1[i], state) + self.G_b1[i])
             for i in range(self.hidden_dim)]
        out = [_sigmoid(_dot(self.G_W2[i], h) + self.G_b2[i])
               for i in range(self.n_actions)]
        return out

    def forward_N(self, state: List[float]) -> List[float]:
        """N-network forward: state → risk predictions per action [0, 1]."""
        h = [_sigmoid(_dot(self.N_W1[i], state) + self.N_b1[i])
             for i in range(self.hidden_dim)]
        out = [_sigmoid(_dot(self.N_W2[i], h) + self.N_b2[i])
               for i in range(self.n_actions)]
        return out

    def forward(self, state: List[float]) -> Tuple[List[float], List[float]]:
        """Return both (G_scores, N_scores)."""
        return self.forward_G(state), self.forward_N(state)

    # ── Training ──

    def train_step(self, state: List[float], action_idx: int,
                   reward_observed: float, penalty_observed: float):
        """One SGD step updating both G and N networks.

        reward_observed: actual positive outcome (0-1)
        penalty_observed: actual negative outcome (0-1)
        """
        # Forward
        h_G = [_sigmoid(_dot(self.G_W1[i], state) + self.G_b1[i])
               for i in range(self.hidden_dim)]
        h_N = [_sigmoid(_dot(self.N_W1[i], state) + self.N_b1[i])
               for i in range(self.hidden_dim)]
        g_pred = [_sigmoid(_dot(self.G_W2[i], h_G) + self.G_b2[i])
                  for i in range(self.n_actions)]
        n_pred = [_sigmoid(_dot(self.N_W2[i], h_N) + self.N_b2[i])
                  for i in range(self.n_actions)]

        # ── G-network backprop (MSE loss on action_idx) ──
        g_error = reward_observed - g_pred[action_idx]
        g_delta2 = g_error * g_pred[action_idx] * (1 - g_pred[action_idx])

        # Grad for W2, b2
        for i in range(self.hidden_dim):
            self.G_W2[action_idx][i] += self.lr_G * g_delta2 * h_G[i]
        self.G_b2[action_idx] += self.lr_G * g_delta2

        # Grad for W1, b1
        g_delta1 = [0.0] * self.hidden_dim
        for i in range(self.hidden_dim):
            g_delta1[i] = g_delta2 * self.G_W2[action_idx][i] * h_G[i] * (1 - h_G[i])
            for j in range(self.state_dim):
                self.G_W1[i][j] += self.lr_G * g_delta1[i] * state[j]
            self.G_b1[i] += self.lr_G * g_delta1[i]

        # ── N-network backprop (MSE loss on action_idx) ──
        n_error = penalty_observed - n_pred[action_idx]
        n_delta2 = n_error * n_pred[action_idx] * (1 - n_pred[action_idx])

        for i in range(self.hidden_dim):
            self.N_W2[action_idx][i] += self.lr_N * n_delta2 * h_N[i]
        self.N_b2[action_idx] += self.lr_N * n_delta2

        n_delta1 = [0.0] * self.hidden_dim
        for i in range(self.hidden_dim):
            n_delta1[i] = n_delta2 * self.N_W2[action_idx][i] * h_N[i] * (1 - h_N[i])
            for j in range(self.state_dim):
                self.N_W1[i][j] += self.lr_N * n_delta1[i] * state[j]
            self.N_b1[i] += self.lr_N * n_delta1[i]

        self.train_count += 1
        self.total_G_loss += g_error ** 2
        self.total_N_loss += n_error ** 2

    # ── Serialization ──

    def to_dict(self) -> Dict:
        return {
            "state_dim": self.state_dim,
            "n_actions": self.n_actions,
            "hidden_dim": self.hidden_dim,
            "G_W1": self.G_W1, "G_b1": self.G_b1,
            "G_W2": self.G_W2, "G_b2": self.G_b2,
            "N_W1": self.N_W1, "N_b1": self.N_b1,
            "N_W2": self.N_W2, "N_b2": self.N_b2,
            "train_count": self.train_count,
            "total_G_loss": self.total_G_loss,
            "total_N_loss": self.total_N_loss,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "DualPathwayNetwork":
        net = cls(
            state_dim=d.get("state_dim", 32),
            n_actions=d.get("n_actions", 7),
            hidden_dim=d.get("hidden_dim", 24),
        )
        for key in ["G_W1", "G_b1", "G_W2", "G_b2",
                     "N_W1", "N_b1", "N_W2", "N_b2"]:
            if key in d:
                setattr(net, key, d[key])
        net.train_count = d.get("train_count", 0)
        net.total_G_loss = d.get("total_G_loss", 0.0)
        net.total_N_loss = d.get("total_N_loss", 0.0)
        return net

    def param_count(self) -> int:
        """Total scalar parameters in both networks."""
        n = 0
        for attr in ["G_W1", "G_b1", "G_W2", "G_b2",
                      "N_W1", "N_b1", "N_W2", "N_b2"]:
            v = getattr(self, attr)
            if isinstance(v, list):
                if v and isinstance(v[0], list):
                    n += sum(len(row) for row in v)
                else:
                    n += len(v)
        return n


class BasalGanglia:
    """Biomimetic Basal Ganglia — dual-pathway action selection.

    Evaluates proposed actions through the G/N pulley model:
        score(action) = G(action) - lambda * N(action)
        P(action) = softmax(scores)

    Where lambda (caution coefficient) is modulated by amygdala stress signals.
    High stress → high lambda → more inhibition of risky actions.
    """

    def __init__(
        self,
        state_dim: int = 32,
        caution_lambda: float = 1.0,      # Base caution coefficient
        danger_threshold: float = 0.7,     # N-score above this → auto-block
        softmax_temperature: float = 0.5,  # Lower = more decisive
        model_path: str = "",
    ):
        self.network = DualPathwayNetwork(state_dim=state_dim)
        self.caution_lambda = caution_lambda
        self.danger_threshold = danger_threshold
        self.softmax_temperature = softmax_temperature

        # Cross-module signals
        self._stress_level: float = 0.0
        self._last_action_blocked: str = ""
        self._blocked_count: int = 0
        self._passed_count: int = 0

        # History for learning
        self._action_history: List[Dict] = []  # (state, action, G, N, outcome)

        if model_path:
            self._load(model_path)

    # ── Action evaluation ──

    def evaluate_action(
        self,
        state: List[float],
        action_category: str,
        action_description: str = "",
    ) -> Dict[str, Any]:
        """Evaluate a proposed action through the dual pathway.

        Returns:
            {
                "allow": True/False,
                "g_score": float,        # G-network reward prediction
                "n_score": float,        # N-network risk prediction
                "net_score": float,      # G - lambda*N
                "confidence": float,     # softmax probability
                "caution_lambda": float, # current caution level
                "reason": str,           # human-readable explanation
            }
        """
        action_idx = self._action_idx(action_category)
        G, N = self.network.forward(state)

        g_score = G[action_idx]
        n_score = N[action_idx]

        # Pulley model: net = G - lambda * N
        # Amygdala stress increases effective lambda
        effective_lambda = self.caution_lambda * (1.0 + self._stress_level * 2.0)
        net_score = g_score - effective_lambda * n_score

        # Softmax over all actions
        all_nets = [G[i] - effective_lambda * N[i] for i in range(self.network.n_actions)]
        probs = _softmax([s / max(self.softmax_temperature, 0.01) for s in all_nets])
        confidence = probs[action_idx]

        # Safety checks
        allow = True
        reason = "action allowed"

        # Safe operations: always allow, skip network
        if action_category in ("safe_read", "safe_info"):
            self._passed_count += 1
            return {
                "allow": True,
                "g_score": 1.0,
                "n_score": 0.0,
                "net_score": 1.0,
                "confidence": 1.0,
                "caution_lambda": round(effective_lambda, 3),
                "action_category": action_category,
                "reason": "auto-allowed (safe category)",
            }

        # Absolute danger threshold: N-score alone can block
        if n_score >= self.danger_threshold:
            allow = False
            reason = f"BLOCKED: N-score {n_score:.3f} >= danger_threshold {self.danger_threshold}"
            self._blocked_count += 1
            self._last_action_blocked = action_category
        elif net_score < -0.3:
            allow = False
            reason = f"BLOCKED: net score {net_score:.3f} too negative (risk dominates reward)"
            self._blocked_count += 1
            self._last_action_blocked = action_category
        else:
            self._passed_count += 1

        result = {
            "allow": allow,
            "g_score": round(g_score, 4),
            "n_score": round(n_score, 4),
            "net_score": round(net_score, 4),
            "confidence": round(confidence, 4),
            "caution_lambda": round(effective_lambda, 3),
            "action_category": action_category,
            "reason": reason,
        }

        # Record for learning
        self._action_history.append({
            "state": state[:],
            "action_idx": action_idx,
            "action_category": action_category,
            "g_score": g_score,
            "n_score": n_score,
            "allowed": allow,
            "timestamp": time.time(),
        })
        if len(self._action_history) > 200:
            self._action_history = self._action_history[-100:]

        return result

    def should_allow(self, state: List[float], action_category: str) -> bool:
        """Quick boolean check."""
        return self.evaluate_action(state, action_category)["allow"]

    # ── Learning from outcomes ──

    def learn_from_outcome(
        self,
        state: List[float],
        action_category: str,
        success: bool,
        error_description: str = "",
        latency: float = 0.0,
    ):
        """Update G/N networks based on observed action outcome.

        success=True → high reward, low penalty
        success=False + error → low reward, high penalty (scaled by error severity)
        """
        action_idx = self._action_idx(action_category)

        # Compute reward_observed
        if success:
            reward_observed = 0.7 + (0.3 if latency < 1.0 else 0.0)  # Faster = more reward
        else:
            reward_observed = 0.05  # Failed action = minimal reward

        # Compute penalty_observed
        if success:
            penalty_observed = 0.05  # Successful = negligible penalty
        else:
            # Penalty scaled by error severity
            error_lower = error_description.lower() if error_description else ""
            if any(w in error_lower for w in ["permission denied", "access denied", "forbidden"]):
                penalty_observed = 0.4
            elif any(w in error_lower for w in ["not found", "missing", "does not exist"]):
                penalty_observed = 0.3
            elif any(w in error_lower for w in ["timeout", "timed out", "connection"]):
                penalty_observed = 0.6
            elif any(w in error_lower for w in ["fatal", "crash", "killed", "panic"]):
                penalty_observed = 1.0
            elif any(w in error_lower for w in ["syntax", "type error", "value error"]):
                penalty_observed = 0.5
            else:
                penalty_observed = 0.5  # Default failure penalty

        self.network.train_step(state, action_idx, reward_observed, penalty_observed)

    # ── Amygdala cascade interface ──

    def set_stress_level(self, level: float):
        """Receive stress signal from amygdala.

        Higher stress → higher effective lambda → more inhibition.
        """
        self._stress_level = max(0.0, min(1.0, level))

    def set_caution(self, lambda_val: float):
        """Directly set the base caution coefficient."""
        self.caution_lambda = max(0.1, min(10.0, lambda_val))

    # ── Utility ──

    def _action_idx(self, category: str) -> int:
        """Map category name to index."""
        return ACTION_CATEGORIES.get(category, 6)  # Default to "unsafe"

    def classify_action(self, tool_name: str) -> str:
        """Auto-classify a tool name into an action category."""
        tool = tool_name.lower()
        # Safe reads
        if any(w in tool for w in ["read", "search", "find", "list", "ls", "cat",
                                     "grep", "stat", "view", "show", "get",
                                     "analyze_image", "vision_analyze", "image",
                                     "screenshot", "photo", "picture", "ocr"]):
            return "safe_read"
        # Safe info (non-destructive operations)
        if any(w in tool for w in ["web_search", "web_extract", "fetch", "curl",
                                     "wget", "info", "help", "man", "switch_model",
                                     "config_get", "config_list", "config"]):
            return "safe_info"
        # Local modify
        if any(w in tool for w in ["write", "patch", "edit", "mkdir", "touch",
                                     "cp", "mv", "pip", "npm", "apt"]):
            return "modify_local"
        # Remote modify
        if any(w in tool for w in ["push", "deploy", "scp", "rsync", "upload",
                                     "publish", "release"]):
            return "modify_remote"
        # Delete
        if any(w in tool for w in ["rm", "delete", "remove", "drop", "truncate",
                                     "unlink", "purge"]):
            return "delete"
        # System
        if any(w in tool for w in ["systemctl", "service", "kill", "reboot",
                                     "shutdown", "mount", "umount"]):
            return "system"
        # Unsafe
        if any(w in tool for w in ["sudo", "chmod", "chown", "force",
                                     "fdisk", "dd", "mkfs"]):
            return "unsafe"
        # Default
        return "modify_local"

    # ── Serialization ──

    def to_dict(self) -> Dict:
        return {
            "network": self.network.to_dict(),
            "caution_lambda": self.caution_lambda,
            "danger_threshold": self.danger_threshold,
            "softmax_temperature": self.softmax_temperature,
            "stress_level": self._stress_level,
            "blocked_count": self._blocked_count,
            "passed_count": self._passed_count,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "BasalGanglia":
        bg = cls(
            caution_lambda=d.get("caution_lambda", 1.0),
            danger_threshold=d.get("danger_threshold", 0.7),
            softmax_temperature=d.get("softmax_temperature", 0.5),
        )
        if "network" in d:
            bg.network = DualPathwayNetwork.from_dict(d["network"])
        bg._stress_level = d.get("stress_level", 0.0)
        bg._blocked_count = d.get("blocked_count", 0)
        bg._passed_count = d.get("passed_count", 0)
        return bg

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def _load(self, path: str):
        try:
            with open(path) as f:
                d = json.load(f)
            loaded = BasalGanglia.from_dict(d)
            self.network = loaded.network
            self.caution_lambda = loaded.caution_lambda
            self.danger_threshold = loaded.danger_threshold
            self._stress_level = loaded._stress_level
            self._blocked_count = loaded._blocked_count
            self._passed_count = loaded._passed_count
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # Start fresh

    def stats(self) -> Dict:
        return {
            "params": self.network.param_count(),
            "caution_lambda": round(self.caution_lambda, 3),
            "stress_level": round(self._stress_level, 3),
            "danger_threshold": self.danger_threshold,
            "blocked": self._blocked_count,
            "passed": self._passed_count,
            "train_count": self.network.train_count,
            "avg_G_loss": round(self.network.total_G_loss / max(1, self.network.train_count), 4),
            "avg_N_loss": round(self.network.total_N_loss / max(1, self.network.train_count), 4),
        }


# ── Factory ──

def create_basal_ganglia(state_dim: int = 32, **kwargs) -> BasalGanglia:
    return BasalGanglia(state_dim=state_dim, **kwargs)
