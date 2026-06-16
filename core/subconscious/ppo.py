"""
ww/core/subconscious/ppo.py — PPO-based Steering Policy Optimiser

Learns a policy for subconscious steering actions (which intervention to
apply given the current feature state) using the PPO clipped surrogate
objective.  The policy network sits on top of the DeepRiskNet shared
encoder's L4 hidden layer (16-dim).

Key components:
  - TrajectoryBuffer: ring-buffer for (state, action, reward, ...)
  - PolicyValueNet: 2-layer MLP for action logits + state value
  - PPOAgent: orchestrates collection, advantage estimation, PPO updates

Pure Python, zero external dependencies.  All gradients computed manually.
"""

from __future__ import annotations
import json
import logging
import math
import os
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Import neural primitives from predictor (monorepo) ──
import sys as _sys
_basedir = os.path.dirname(os.path.abspath(__file__))
if _basedir not in _sys.path:
    _sys.path.insert(0, _basedir)

from core.predictor import Linear, ReLU, Adam, ParameterGroup


# ── Small helpers not in predictor ──

class Softmax:
    """ Numerically stable softmax. """
    @staticmethod
    def forward(logits: List[float]) -> List[float]:
        max_l = max(logits)
        exps = [math.exp(l - max_l) for l in logits]
        total = sum(exps)
        return [e / total for e in exps]


class Tanh:
    """ Tanh activation. """
    def __init__(self):
        self._out: List[float] = []

    def forward(self, x: List[float]) -> List[float]:
        self._out = [math.tanh(v) for v in x]
        return self._out

    def backward(self, grad: List[float]) -> List[float]:
        return [g * (1 - o * o) for g, o in zip(grad, self._out)]


def init_xavier(W: List[List[float]], b: Optional[List[float]] = None,
                scale: float = 1.0):
    """Xavier/Glorot uniform initialisation for weights."""
    import random as _r
    fan_in = len(W[0]) if W else 1
    fan_out = len(W)
    limit = scale * math.sqrt(6.0 / (fan_in + fan_out))
    for i in range(len(W)):
        for j in range(len(W[i])):
            W[i][j] = _r.uniform(-limit, limit)
    if b is not None:
        for i in range(len(b)):
            b[i] = 0.0

logger = logging.getLogger("ww.subconscious.ppo")

PPO_DIR = os.path.expanduser("~/worldwave/data/subconscious/ppo")


# ── Small helpers ──

def _tensor_flatten(t: list) -> List[float]:
    """Flatten arbitrarily nested list into 1D float list."""
    result = []
    stack = [t]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        else:
            result.append(float(item))
    return result


def _scale_inplace(t: list, s: float):
    """Scale all leaf elements in-place."""
    if isinstance(t, list):
        if t and not isinstance(t[0], list):
            for i in range(len(t)):
                t[i] *= s
        else:
            for sub in t:
                _scale_inplace(sub, s)


def _add_inplace(dst: list, src: list):
    """Element-wise add src into dst, in-place (mutual nesting assumed)."""
    if isinstance(dst, list):
        if dst and not isinstance(dst[0], list):
            for i in range(len(dst)):
                dst[i] += src[i]
        else:
            for i in range(len(dst)):
                _add_inplace(dst[i], src[i])


# ── Action space ──

ACTION_NONE = 0          # no intervention
ACTION_SYSTEM_PROMPT = 1 # inject system prompt guidance
ACTION_PARAM_TUNE = 2    # adjust temperature/top-p
ACTION_CODE = 3          # inject code-level steering

ACTION_NAMES = {
    ACTION_NONE: "none",
    ACTION_SYSTEM_PROMPT: "system_prompt",
    ACTION_PARAM_TUNE: "param_tune",
    ACTION_CODE: "action_code",
}

N_ACTIONS = 4


@dataclass
class TrajectoryStep:
    """One step in a trajectory."""
    features: List[float]      # feature vector (32-dim)
    action: int                # chosen action index
    log_prob: float            # log probability of chosen action
    value: float               # state value estimate
    reward: float = 0.0        # observed reward
    next_features: Optional[List[float]] = None
    done: bool = False


class TrajectoryBuffer:
    """Ring buffer for trajectory storage.

    Stores full episodes for GAE computation.
    Each episode is a list of TrajectoryStep.
    """

    def __init__(self, max_steps: int = 10000):
        self.max_steps = max_steps
        self._episodes: List[List[TrajectoryStep]] = []
        self._current: List[TrajectoryStep] = []
        self._total_steps = 0

    def start_episode(self):
        """Begin a new episode."""
        self._current = []

    def add_step(self, step: TrajectoryStep):
        """Add a step to the current episode."""
        self._current.append(step)
        self._total_steps += 1

        # Evict oldest if over capacity
        if self._total_steps > self.max_steps:
            if self._episodes:
                oldest = self._episodes.pop(0)
                self._total_steps -= len(oldest)

    def end_episode(self, final_features: Optional[List[float]] = None):
        """Finalise the current episode.

        Sets the next_features of the last step to the final observation
        (if any) and marks it done.
        """
        if not self._current:
            return
        last = self._current[-1]
        if final_features is not None:
            last.next_features = final_features
        last.done = True
        self._episodes.append(self._current)
        self._current = []

    def all_steps(self) -> List[TrajectoryStep]:
        """All steps across all episodes (flat)."""
        steps = []
        for ep in self._episodes:
            steps.extend(ep)
        steps.extend(self._current)
        return steps

    def clear(self):
        self._episodes.clear()
        self._current.clear()
        self._total_steps = 0

    def __len__(self) -> int:
        return self._total_steps

    @property
    def trajectory_count(self) -> int:
        return len(self._episodes) + (1 if self._current else 0)


class PolicyValueNet:
    """Small policy+value network on top of shared encoder features.

    Takes the 16-dim L4 hidden representation from DeepRiskNet and
    produces:
      - policy_logits: N_ACTIONS-dim (intervention type probabilities)
      - value: scalar state value estimate

    Architecture:
        Input(16) → Linear(16→32) → Tanh → Linear(32→16) → Tanh
          → policy_head: Linear(16→N_ACTIONS) → logits
          → value_head: Linear(16→1) → scalar
    """

    def __init__(self, n_actions: int = N_ACTIONS, hidden_dim: int = 32,
                 input_dim: int = 16, lr: float = 0.0003):
        self.n_actions = n_actions
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.lr = lr

        # Shared trunk
        self.fc1 = Linear(input_dim, hidden_dim)
        self.tanh1 = Tanh()
        self.fc2 = Linear(hidden_dim, hidden_dim // 2)
        self.tanh2 = Tanh()

        # Heads
        self.policy_head = Linear(hidden_dim // 2, n_actions)
        self.value_head = Linear(hidden_dim // 2, 1)

        self._params = ParameterGroup()
        self._param_count = (
            self.fc1.param_count() + self.fc2.param_count() +
            self.policy_head.param_count() + self.value_head.param_count()
        )

        # Xavier init
        init_xavier(self.fc1.W, self.fc1.b if self.fc1.use_bias else None)
        init_xavier(self.fc2.W, self.fc2.b if self.fc2.use_bias else None)
        init_xavier(self.policy_head.W, self.policy_head.b if self.policy_head.use_bias else None)
        init_xavier(self.value_head.W, self.value_head.b if self.value_head.use_bias else None)

        # Optimiser (created on first forward)
        self._optim: Optional[Adam] = None

        # Cache for backward pass
        self._cache: Dict[str, Any] = {}

    def param_count(self) -> int:
        return self._param_count

    def forward(self, x: List[float]) -> Tuple[List[float], float]:
        """Forward pass.

        Args:
            x: 16-dim hidden features from encoder

        Returns:
            (logits list length N_ACTIONS, value scalar)
        """
        self._cache = {}

        h = self.fc1.forward(x)
        self._cache["fc1_out"] = h
        h = self.tanh1.forward(h)
        self._cache["tanh1_out"] = h

        h = self.fc2.forward(h)
        self._cache["fc2_out"] = h
        h = self.tanh2.forward(h)
        self._cache["tanh2_out"] = h

        # Policy head
        logits = self.policy_head.forward(h)
        self._cache["logits"] = logits

        # Value head
        v = self.value_head.forward(h)
        self._cache["value"] = v[0]

        return logits, v[0]

    def sample_action(self, logits: List[float],
                      temperature: float = 1.0) -> Tuple[int, float]:
        """Sample an action from the policy.

        Args:
            logits: raw action logits
            temperature: higher = more exploration

        Returns:
            (action_index, log_prob)
        """
        if temperature != 1.0:
            logits = [l / temperature for l in logits]

        # Softmax to get probabilities
        sm = Softmax()
        probs = sm.forward(logits)
        self._cache["probs"] = probs

        # Sample
        r = random.random()
        cumulative = 0.0
        for i, p in enumerate(probs):
            cumulative += p
            if r < cumulative:
                self._cache["sampled_action"] = i
                self._cache["sampled_log_prob"] = math.log(max(p, 1e-10))
                return i, math.log(max(p, 1e-10))

        # Default to highest probability
        best = max(range(len(probs)), key=lambda i: probs[i])
        lp = math.log(max(probs[best], 1e-10))
        self._cache["sampled_action"] = best
        self._cache["sampled_log_prob"] = lp
        return best, lp

    def evaluate_actions(self, x: List[float],
                         actions: List[int]) -> Tuple[List[float], float, List[float]]:
        """Evaluate a batch of actions (used during PPO update).

        Args:
            x: feature vectors (list of 16-dim inputs) — flattened
            actions: list of action indices

        Returns:
            (log_probs, entropy_estimate, values)
            Where log_probs[i] = log π(action_i | state_i)
        """
        # Simplified single-sample eval
        logits, value = self.forward(x)

        sm = Softmax()
        probs = sm.forward(logits)

        log_probs = []
        for a in actions:
            p = max(probs[a], 1e-10)
            log_probs.append(math.log(p))

        # Entropy: -Σ p_i * log(p_i)
        entropy = -sum(p * math.log(max(p, 1e-10)) for p in probs)

        return log_probs, entropy, [value]

    def backward(self, grad_logits: Optional[List[float]] = None,
                 grad_value: float = 0.0) -> ParameterGroup:
        """Backprop through the network.

        Args:
            grad_logits: gradient of loss w.r.t. action logits (N_ACTIONS-dim)
            grad_value: gradient of loss w.r.t. value output (scalar)

        Returns:
            ParameterGroup with accumulated gradients
        """
        pg = ParameterGroup()

        # Value head backward
        dv_in = self.value_head.backward([grad_value], pg, prefix="value_")

        # Policy head backward
        if grad_logits:
            d_policy_in = self.policy_head.backward(grad_logits, pg, prefix="policy_")
        else:
            d_policy_in = [0.0] * self.hidden_dim // 2

        # Combined gradient into tanh2 output
        d_combined = [d_policy_in[i] + dv_in[i] for i in range(len(d_policy_in))]

        # Tanh2 backward
        tanh2_out = self._cache.get("tanh2_out", d_combined)
        fc2_out = self._cache.get("fc2_out", d_combined)
        d_tanh2 = [d * (1 - o * o) for d, o in zip(d_combined, tanh2_out)]

        # FC2 backward
        d_fc2 = self.fc2.backward(d_tanh2, pg, prefix="fc2_")

        # Tanh1 backward
        tanh1_out = self._cache.get("tanh1_out", d_fc2)
        d_tanh1 = [d * (1 - o * o) for d, o in zip(d_fc2, tanh1_out)]

        # FC1 backward
        _ = self.fc1.backward(d_tanh1, pg, prefix="fc1_")

        return pg

    def apply_gradients(self, pg: ParameterGroup, lr_override: Optional[float] = None):
        """Apply accumulated gradients via Adam."""
        lr = lr_override if lr_override is not None else self.lr
        if self._optim is None:
            self._optim = Adam(self._param_count, lr=lr)
        self._optim.lr = lr
        self._optim.step(pg)

    def save(self, path: str):
        """Export parameters to JSON."""
        data = {
            "fc1_W": self.fc1.W,
            "fc1_b": self.fc1.b,
            "fc2_W": self.fc2.W,
            "fc2_b": self.fc2.b,
            "policy_W": self.policy_head.W,
            "policy_b": self.policy_head.b,
            "value_W": self.value_head.W,
            "value_b": self.value_head.b,
        }
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False)

    @classmethod
    def load(cls, path: str, **kwargs) -> "PolicyValueNet":
        """Load parameters from JSON."""
        net = cls(**kwargs)
        with open(path) as f:
            data = json.load(f)
        net.fc1.W = data["fc1_W"]
        net.fc1.b = data["fc1_b"]
        net.fc2.W = data["fc2_W"]
        net.fc2.b = data["fc2_b"]
        net.policy_head.W = data["policy_W"]
        net.policy_head.b = data["policy_b"]
        net.value_head.W = data["value_W"]
        net.value_head.b = data["value_b"]
        return net


class PPOAgent:
    """PPO agent that learns steering policy from experience.

    Integrates with the spiral loop:
      1. ppo.get_action(l4_features) → intervention type
      2. Execute intervention, observe outcome
      3. ppo.record(state, action, log_prob, value, reward, next_state)
      4. After enough steps, ppo.update() applies PPO

    PPO hyperparams:
      - clip_epsilon: 0.2 (clipping range)
      - gamma: 0.99 (discount)
      - gae_lambda: 0.95 (GAE trace decay)
      - value_coef: 0.5 (value loss weight)
      - entropy_coef: 0.01 (entropy bonus weight)
      - max_grad_norm: 0.5 (gradient clipping)
      - update_epochs: 4 (K epochs per update)
      - batch_size: 64 (mini-batch)
    """

    def __init__(
        self,
        policy_net: Optional[PolicyValueNet] = None,
        clip_epsilon: float = 0.2,
        gamma: float = 0.99,
        gae_lambda: float = 0.95,
        value_coef: float = 0.5,
        entropy_coef: float = 0.01,
        max_grad_norm: float = 0.5,
        update_epochs: int = 4,
        batch_size: int = 64,
        min_steps_before_update: int = 200,
        temperature: float = 1.0,
        temperature_decay: float = 0.995,
        min_temperature: float = 0.2,
        lr_decay: float = 0.9995,
        lr_min: float = 1e-5,
        auto_persist: bool = True,
        data_dir: str = PPO_DIR,
    ):
        self.policy_net = policy_net or PolicyValueNet()
        self.buffer = TrajectoryBuffer()
        self.clip_epsilon = clip_epsilon
        self.gamma = gamma
        self.gae_lambda = gae_lambda
        self.value_coef = value_coef
        self.entropy_coef = entropy_coef
        self.max_grad_norm = max_grad_norm
        self.update_epochs = update_epochs
        self.batch_size = batch_size
        self.min_steps = min_steps_before_update
        self.temperature = temperature
        self.temperature_decay = temperature_decay
        self.min_temperature = min_temperature
        self.lr_decay = lr_decay
        self.lr_min = lr_min
        self.auto_persist = auto_persist
        self.data_dir = data_dir

        os.makedirs(data_dir, exist_ok=True)

        self.total_steps = 0
        self.update_count = 0
        self._current_episode_returns: List[float] = []

    # ── Interaction loop ──

    def get_action(self, l4_features: List[float]) -> Tuple[int, float, float]:
        """Select an action given state features.

        Args:
            l4_features: 16-dim hidden representation from encoder

        Returns:
            (action_id, log_prob, value)
        """
        logits, value = self.policy_net.forward(l4_features)
        action, log_prob = self.policy_net.sample_action(logits, self.temperature)
        self.total_steps += 1
        return action, log_prob, value

    def get_best_action(self, l4_features: List[float]) -> int:
        """Greedy action (no exploration)."""
        logits, _ = self.policy_net.forward(l4_features)
        sm = Softmax()
        probs = sm.forward(logits)
        return max(range(len(probs)), key=lambda i: probs[i])

    def record(self, features: List[float], action: int,
               log_prob: float, value: float,
               reward: float, next_features: Optional[List[float]] = None,
               done: bool = False):
        """Record one step of experience."""
        step = TrajectoryStep(
            features=features,
            action=action,
            log_prob=log_prob,
            value=value,
            reward=reward,
            next_features=next_features,
            done=done,
        )
        self.buffer.add_step(step)

    # ── Episode management ──

    def start_episode(self):
        self.buffer.start_episode()

    def end_episode(self, final_features: Optional[List[float]] = None,
                    final_reward: float = 0.0):
        """End current episode and compute return.

        Appends final reward if provided, then finalises.
        """
        if final_reward != 0.0:
            last = self.buffer._current[-1] if self.buffer._current else None
            if last:
                last.reward = final_reward
                last.next_features = final_features
                last.done = True

        if self.buffer._current:
            ep_return = sum(s.reward for s in self.buffer._current)
            self._current_episode_returns.append(ep_return)
            if len(self._current_episode_returns) > 100:
                self._current_episode_returns.pop(0)

        self.buffer.end_episode(final_features)

    @property
    def avg_return(self) -> float:
        if not self._current_episode_returns:
            return 0.0
        return sum(self._current_episode_returns) / len(self._current_episode_returns)

    # ── PPO update ──

    def compute_gae(self, steps: List[TrajectoryStep]) -> Tuple[List[float], List[float]]:
        """Compute Generalised Advantage Estimation.

        Returns:
            (advantages, returns) lists aligned with steps
        """
        n = len(steps)
        advantages = [0.0] * n
        returns = [0.0] * n
        gae = 0.0

        for t in reversed(range(n)):
            step = steps[t]
            if t == n - 1:
                next_value = step.value if step.done else 0.0
            else:
                next_value = steps[t + 1].value if not steps[t].done else 0.0

            delta = step.reward + self.gamma * next_value - step.value
            gae = delta + self.gamma * self.gae_lambda * gae
            advantages[t] = gae
            returns[t] = advantages[t] + step.value

        return advantages, returns

    def update(self) -> Dict[str, Any]:
        """Run a PPO update on collected experience.

        Returns:
            summary dict with loss metrics
        """
        steps = self.buffer.all_steps()
        if len(steps) < 2:
            return {"updated": False, "reason": f"only {len(steps)} steps", "steps": len(steps)}

        # Decay temperature
        self.temperature = max(self.min_temperature, self.temperature * self.temperature_decay)

        # Decay learning rate
        new_lr = max(self.lr_min, self.policy_net.lr * self.lr_decay)
        self.policy_net.lr = new_lr

        # Compute GAE
        advantages, returns = self.compute_gae(steps)

        # Normalise advantages
        adv_mean = sum(advantages) / len(advantages)
        adv_std = math.sqrt(sum((a - adv_mean) ** 2 for a in advantages) / len(advantages)) or 1.0
        advantages_norm = [(a - adv_mean) / adv_std for a in advantages]

        total_policy_loss = 0.0
        total_value_loss = 0.0
        total_entropy = 0.0
        total_loss = 0.0
        batches = 0

        for _ in range(self.update_epochs):
            # Create mini-batch indices
            indices = list(range(len(steps)))
            random.shuffle(indices)

            for start in range(0, len(indices), self.batch_size):
                batch_idx = indices[start:start + self.batch_size]
                batches += 1

                batch_policy_loss = 0.0
                batch_value_loss = 0.0
                batch_entropy_sum = 0.0

                for idx in batch_idx:
                    step = steps[idx]
                    # Recompute log prob under current policy
                    logits, _ = self.policy_net.forward(step.features)
                    sm = Softmax()
                    probs = sm.forward(logits)
                    new_log_prob = math.log(max(probs[step.action], 1e-10))

                    # Entropy: -Σ p log p
                    ent = -sum(p * math.log(max(p, 1e-10)) for p in probs)
                    batch_entropy_sum += ent

                    # Ratio: π_new(a|s) / π_old(a|s)
                    ratio = math.exp(new_log_prob - step.log_prob)

                    # Clipped surrogate
                    adv = advantages_norm[idx]
                    surr1 = ratio * adv
                    surr2 = max(1.0 - self.clip_epsilon, 0.0) * adv
                    surr3 = min(1.0 + self.clip_epsilon, 2.0) * adv
                    policy_loss = min(surr1, surr2, surr3)
                    # Policy loss = -min(...) since we maximise
                    batch_policy_loss -= policy_loss

                    # Value loss (MSE)
                    value_pred = step.value
                    value_target = returns[idx]
                    value_loss = (value_pred - value_target) ** 2
                    batch_value_loss += value_loss

                # Average over batch
                n = len(batch_idx)
                batch_policy_loss /= n
                batch_value_loss = self.value_coef * batch_value_loss / n
                batch_entropy_val = self.entropy_coef * batch_entropy_sum / n

                batch_total = batch_policy_loss + batch_value_loss - batch_entropy_val

                total_policy_loss += batch_policy_loss
                total_value_loss += batch_value_loss
                total_entropy += batch_entropy_val
                total_loss += batch_total

                # ── Backward pass (per-sample gradients, summed) ──
                batch_n = len(batch_idx)
                summed_pg = ParameterGroup()
                for idx in batch_idx:
                    step = steps[idx]
                    # Forward to populate cache for THIS sample
                    logits, _ = self.policy_net.forward(step.features)
                    sm = Softmax()
                    probs = sm.forward(logits)
                    new_log_prob = math.log(max(probs[step.action], 1e-10))
                    ratio = math.exp(new_log_prob - step.log_prob)
                    adv = advantages_norm[idx]

                    # Policy gradient: d(-surr1)/dlogit where surr1 = ratio * adv
                    clip_ok = (ratio >= 1.0 - self.clip_epsilon and
                               ratio <= 1.0 + self.clip_epsilon)
                    grad_logits = [0.0] * N_ACTIONS
                    if clip_ok:
                        coeff = -adv / batch_n
                        for a in range(N_ACTIONS):
                            kronecker = 1.0 if a == step.action else 0.0
                            grad_logits[a] = coeff * ratio * (kronecker - probs[a])

                    # Entropy gradient: d(entropy_coef * H)/dlogit_i
                    s = sum(p * math.log(max(p, 1e-10)) for p in probs)
                    for a in range(N_ACTIONS):
                        lp = math.log(max(probs[a], 1e-10))
                        dH = probs[a] * (1.0 - lp + s)
                        grad_logits[a] += self.entropy_coef * dH / batch_n

                    # Value gradient: d(0.5 * (v - target)^2) / dv = v - target
                    grad_value = (step.value - returns[idx]) / batch_n

                    # Backward for THIS sample with its OWN gradients
                    step_pg = self.policy_net.backward(grad_logits, grad_value)
                    for name, val, dw in step_pg.params:
                        summed_pg.add(name, val, dw)

                # Deduplicate: sum gradients for the same parameter name
                grad_accum: Dict[str, list] = {}
                for name, _, dw in summed_pg.params:
                    if name not in grad_accum:
                        grad_accum[name] = dw
                    else:
                        _add_inplace(grad_accum[name], dw)

                # Rebuild clean ParameterGroup
                clean_pg = ParameterGroup()
                for name, val, dw in summed_pg.params:
                    if name not in [n for n, _, _ in clean_pg.params]:
                        clean_pg.add(name, val, grad_accum[name])

                # Gradient clipping
                total_norm = 0.0
                for _, _, dw in clean_pg.params:
                    flat = _tensor_flatten(dw)
                    total_norm += sum(f * f for f in flat)
                total_norm = math.sqrt(total_norm) or 1.0
                clip_scale = min(1.0, self.max_grad_norm / total_norm)
                for _, _, dw in clean_pg.params:
                    _scale_inplace(dw, clip_scale)

                # Single Adam step
                self.policy_net.apply_gradients(clean_pg)

        n_batches = batches
        metrics = {
            "updated": True,
            "steps": len(steps),
            "epochs": self.update_epochs,
            "batches": n_batches,
            "avg_policy_loss": round(total_policy_loss / n_batches, 6) if n_batches else 0,
            "avg_value_loss": round(total_value_loss / n_batches, 6) if n_batches else 0,
            "avg_entropy": round(total_entropy / n_batches, 6) if n_batches else 0,
            "avg_total_loss": round(total_loss / n_batches, 6) if n_batches else 0,
            "temperature": round(self.temperature, 3),
            "lr": self.policy_net.lr,
            "avg_return": round(self.avg_return, 4),
        }

        self.update_count += 1
        logger.info(f"PPO update #{self.update_count}: policy_loss={metrics['avg_policy_loss']:.6f} "
                    f"value_loss={metrics['avg_value_loss']:.6f} "
                    f"entropy={metrics['avg_entropy']:.6f}")

        # Clear buffer after update
        self.buffer.clear()

        # Persist
        if self.auto_persist:
            self._save_checkpoint()

        return metrics

    # ── Persistence ──

    def _save_checkpoint(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            policy_path = os.path.join(self.data_dir, f"policy_update_{self.update_count}.json")
            self.policy_net.save(policy_path)

            meta_path = os.path.join(self.data_dir, "ppo_meta.json")
            meta = {
                "update_count": self.update_count,
                "total_steps": self.total_steps,
                "temperature": self.temperature,
                "lr": self.policy_net.lr,
                "avg_return": self.avg_return,
            }
            with open(meta_path, "w") as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"PPO checkpoint failed: {e}")

    def save_policy(self, path: str):
        """Save the current policy network."""
        self.policy_net.save(path)

    def load_policy(self, path: str):
        """Load policy network weights."""
        self.policy_net = PolicyValueNet.load(path)

    def save_checkpoint(self, path: str):
        """Full agent checkpoint."""
        data = {
            "total_steps": self.total_steps,
            "update_count": self.update_count,
            "temperature": self.temperature,
            "avg_return": self.avg_return,
        }
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        # Save policy separately
        policy_path = path.replace(".json", "_policy.json")
        self.save_policy(policy_path)

    def load_checkpoint(self, path: str):
        """Load full agent checkpoint."""
        with open(path) as f:
            data = json.load(f)
        self.total_steps = data.get("total_steps", 0)
        self.update_count = data.get("update_count", 0)
        self.temperature = data.get("temperature", 1.0)
        # Load policy separately
        policy_path = path.replace(".json", "_policy.json")
        if os.path.isfile(policy_path):
            self.load_policy(policy_path)
