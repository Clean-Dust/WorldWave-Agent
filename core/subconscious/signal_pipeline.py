"""
ww/core/subconscious/signal_pipeline.py — Signal collection pipeline (Signal Pipeline)

Will convert raw signals into training triples (state_vector, outcome, confidence).

Four signal dimensions:
1. Environment feedback (ENVIRONMENT) — exit_code, stderr → 0.0=success / 1.0=failed
2. User intervention (USER_INTERVENTION) — Ctrl+C, user edit, follow-up correction
3. Efficiency metric (EFFICIENCY) — token/latency comparison → 0.0~0.3=efficient / 0.7~1.0=inefficient
4. Self-correction (SELF_CORRECTION) — S₀→S_final shortcut learning

Each triple comes with confidence (credibility weight), for weighted federation aggregation.

Does not read any conversation content, only reads numerical state values.
"""

from __future__ import annotations
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ww.subconscious.signal")

SIGNAL_DIR = os.path.expanduser("~/worldwave/data/subconscious/signals")


class SignalSource(enum.Enum):
    """Signal source dimension."""
    ENVIRONMENT = "environment"           # compile/execute result
    USER_INTERVENTION = "user_intervention"  # Ctrl+C, edit, follow-up
    EFFICIENCY = "efficiency"             # token/latency
    SELF_CORRECTION = "self_correction"    # reflection shortcut


@dataclass
class TrainingTriple:
    """
    a training triple.

    core fields:
      state_vector: line dynamic 12-dimensional state vector
      outcome: 0.0 = success/efficient/win, 1.0 = failed/inefficient/lose
      confidence: confidence (0.0-1.0)
      source: sourcedimension
      timestamp: record  
      metadata: supplementary info (for audit)
    """
    state_vector: List[float]
    outcome: float
    confidence: float = 1.0
    source: str = "environment"
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    triple_id: str = ""

    def __post_init__(self):
        if not self.triple_id:
            raw = f"{self.timestamp}:{hash(json.dumps(self.state_vector, sort_keys=True))}:{self.outcome}:{self.source}"
            import hashlib
            self.triple_id = hashlib.md5(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "triple_id": self.triple_id,
            "state_vector": [round(v, 3) for v in self.state_vector],
            "outcome": round(self.outcome, 4),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class SignalCollector:
    """
    Signal collection.

    Collect triples from four dimensions, providing:
    - push(triple) — store in buffer
    - drain() — retrieve all triples pending processing
    - get_contrast_pairs() — construct (Y_win, Y_lose) contrast pairs
    - save() / load() — persist (auditable)
    """

    def __init__(self, data_dir: str = SIGNAL_DIR):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

        # buffer (to be fed to contrastive engine)
        self._buffer: List[TrainingTriple] = []

        # persistent storage (all processed triples)
        self._store: Dict[str, TrainingTriple] = {}

        # task-level trajectory (for S₀→S_final)
        self._task_trajectories: Dict[str, Dict[str, Any]] = {}

        # efficiency comparison history (similar task token/latency record)
        self._efficiency_history: List[Dict[str, Any]] = []

        self._load()

    # ── Signal Recording ──

    def push(self, triple: TrainingTriple):
        """Store a training triple into buffer."""
        self._buffer.append(triple)
        self._store[triple.triple_id] = triple
        logger.debug(f"📡 Signal: {triple.source} outcome={triple.outcome:.2f} "
                     f"conf={triple.confidence:.2f}")

    def drain(self) -> List[TrainingTriple]:
        """Retrieve all triples pending processing (clear buffer)."""
        batch = list(self._buffer)
        self._buffer.clear()
        self._save()
        return batch

    def buffer_size(self) -> int:
        return len(self._buffer)

    # ── Environment Feedback ──

    def record_environment(
        self,
        state_vector: List[float],
        exit_code: int,
        success: bool,
        latency: float = 0.0,
        token_count: int = 0,
        tool_name: str = "",
    ) -> TrainingTriple:
        """
        Record one environment execution result.

        Args:
            state_vector: execute 12-dimensional state
            exit_code: terminal exit code
            success: whether success (True=exit code 0 or passed)
            latency: API latency (seconds)
            token_count: consumed token count
            tool_name: tool name

        Returns:
            generate  TrainingTriple
        """
        outcome = 0.0 if success else 1.0
        confidence = 0.95  # environment feedback is most reliable 

        # exit_code non-zero → outcome is definitely 1.0
        if exit_code != 0:
            outcome = 1.0

        meta = {
            "exit_code": exit_code,
            "latency": round(latency, 3),
            "tokens": token_count,
            "tool": tool_name,
        }

        triple = TrainingTriple(
            state_vector=state_vector,
            outcome=outcome,
            confidence=confidence,
            source=SignalSource.ENVIRONMENT.value,
            metadata=meta,
        )
        self.push(triple)
        return triple

    # ── User Intervention ──

    def record_user_intervention(
        self,
        state_vector: List[float],
        intervention_type: str,  # "ctrl_c", "edit", "follow_up", "correction"
        severity: float = 0.5,  # 0.0=minor, 1.0=complete restart
    ) -> TrainingTriple:
        """
        Record one user intervention.

        Args:
            state_vector: intervention 12-dimensional state
            intervention_type: intervention type
            severity: severity (correction magnitude)

        Returns:
            generate  TrainingTriple
        """
        # user intervention = negative signal (main consciousness made a mistake)
        outcome = min(1.0, 0.5 + severity * 0.5)
        confidence = 0.9  # user line is very reliable

        meta = {
            "intervention_type": intervention_type,
            "severity": severity,
        }

        triple = TrainingTriple(
            state_vector=state_vector,
            outcome=outcome,
            confidence=confidence,
            source=SignalSource.USER_INTERVENTION.value,
            metadata=meta,
        )
        self.push(triple)
        return triple

    # ── Efficiency Metric ──

    def record_efficiency(
        self,
        state_vector: List[float],
        task_type: str,
        tokens_used: int,
        latency_seconds: float,
        baseline_tokens: Optional[int] = None,
        baseline_latency: Optional[float] = None,
    ) -> TrainingTriple:
        """
        Record one task efficiency performance.

        if baseline is provided (historical average performance of similar tasks),
        then outcome will be adjusted based on efficiency improvement/decrease.

        Args:
            state_vector: task start 12-dimensional state
            task_type: task type tag (e.g., "code", "search", "reasoning")
            tokens_used: tokens consumed this time
            latency_seconds: this API latency
            baseline_tokens: historical average token count (None = no comparison)
            baseline_latency: historical average latency (None = no comparison)

        Returns:
            generate  TrainingTriple
        """
        if baseline_tokens is not None and baseline_tokens > 0:
            token_ratio = tokens_used / baseline_tokens
        else:
            token_ratio = 1.0

        if baseline_latency is not None and baseline_latency > 0:
            latency_ratio = latency_seconds / baseline_latency
        else:
            latency_ratio = 1.0

        # comprehensive efficiency score: token and latency each account for half
        efficiency = (token_ratio + latency_ratio) / 2.0
        # efficiency < 1.0 = better than baseline (consumes fewer resources)
        # efficiency > 1.0 = worse than baseline

        # outcome: 0.0 = most efficient, 1.0 = least efficient
        outcome = min(1.0, max(0.0, (efficiency - 0.5) / 1.5))
        confidence = 0.6  # efficiency metric has relatively large noise

        meta = {
            "task_type": task_type,
            "tokens": tokens_used,
            "latency": round(latency_seconds, 3),
            "baseline_tokens": baseline_tokens,
            "baseline_latency": baseline_latency,
            "token_ratio": round(token_ratio, 3),
            "latency_ratio": round(latency_ratio, 3),
        }

        # record to efficiency history (for future baseline use)
        self._efficiency_history.append({
            "task_type": task_type,
            "tokens": tokens_used,
            "latency": latency_seconds,
            "time": time.time(),
        })
        if len(self._efficiency_history) > 1000:
            self._efficiency_history = self._efficiency_history[-500:]

        triple = TrainingTriple(
            state_vector=state_vector,
            outcome=outcome,
            confidence=confidence,
            source=SignalSource.EFFICIENCY.value,
            metadata=meta,
        )
        self.push(triple)
        return triple

    # ── self-reflection trajectory ──

    def start_task_trajectory(
        self,
        task_id: str,
        initial_state: List[float],
    ):
        """
        start recording a task trajectory.

        for S₀→S_final shortcut learning.
        called at task start.
        """
        self._task_trajectories[task_id] = {
            "initial_state": initial_state,
            "start_time": time.time(),
            "steps": 0,
            "failed_states": [],
            "final_state": None,
            "final_success": False,
            "total_tokens": 0,
            "total_latency": 0.0,
        }

    def record_trajectory_step(
        self,
        task_id: str,
        state_before: List[float],
        action: str,
        success: bool,
        tokens: int = 0,
        latency: float = 0.0,
    ):
        """record trajectory a step."""
        traj = self._task_trajectories.get(task_id)
        if traj is None:
            return

        traj["steps"] += 1
        if not success:
            traj["failed_states"].append(state_before)
        traj["total_tokens"] += tokens
        traj["total_latency"] += latency

    def finish_task_trajectory(
        self,
        task_id: str,
        final_state: List[float],
        success: bool,
    ) -> Optional[TrainingTriple]:
        """
        end task trajectory and generate S₀→S_final contrastive learning triplet.

        if the task goes through the process of "failed→reflection→retry→success",
        then generate a shortcut learning signal: S₀ should directly jump to S_final.

        Returns:
            shortcut learning triplet, or None (no shortcut to learn)
        """
        traj = self._task_trajectories.pop(task_id, None)
        if traj is None:
            return None

        traj["final_state"] = final_state
        traj["final_success"] = success

        # needs at least one failed + final success to have a shortcut to learn
        if not success or not traj["failed_states"]:
            return None

        # construct shortcut triplet:
        #   state = S₀ (initial state)
        #   outcome = low value (~0.1-0.3), representing "this state can actually succeed"
        #   because the subconscious once started from S₀, experienced failure, and then succeeded,
        #     next time encountering S₀ should directly skip trial and error
        shortcut_outcome = 0.2  # low failure risk = shortcut should succeed
        confidence = min(0.7, 0.3 + traj["steps"] * 0.05)

        meta = {
            "task_id": task_id,
            "steps": traj["steps"],
            "failures": len(traj["failed_states"]),
            "total_tokens": traj["total_tokens"],
            "total_latency": round(traj["total_latency"], 3),
            "trajectory_type": "self_correction_shortcut",
        }

        triple = TrainingTriple(
            state_vector=traj["initial_state"],
            outcome=shortcut_outcome,
            confidence=confidence,
            source=SignalSource.SELF_CORRECTION.value,
            metadata=meta,
        )
        self.push(triple)
        return triple

    # ── contrastive pair construction ──

    def get_contrast_pairs(
        self,
        batch: List[TrainingTriple],
    ) -> List[Tuple[List[float], float, float, float]]:
        """
        construct DPO contrastive pairs from triplet batch.

        return: [(state_vector, Y_win, Y_lose, weight), ...]

        rule：
          - if the same state has both success and failed samples → directly pair
          - if only has a single category → pair using global average
        """
        if not batch:
            return []

        # group states by "approximate bucketing" (use 4-dimensional feature vector for rough bucketing)
        buckets: Dict[str, List[TrainingTriple]] = {}
        for t in batch:
            # use 4-dimensional normalized values as key (consecutive errors/tool loop/latency/trend)
            key = tuple(
                min(3, int(v * 4)) if i < 4 else 0
                for i, v in enumerate(t.state_vector[:4])
            )
            buckets.setdefault(str(key), []).append(t)

        pairs: List[Tuple[List[float], float, float, float]] = []

        for key, triples in buckets.items():
            wins = [t for t in triples if t.outcome < 0.3]
            loses = [t for t in triples if t.outcome >= 0.7]

            if wins and loses:
                # has win and also has lose → pair
                for w in wins:
                    for l in loses:
                        # use average confidence of both
                        weight = (w.confidence + l.confidence) / 2.0
                        pairs.append((w.state_vector, w.outcome, l.outcome, weight))
            elif wins:
                # only has win → pair using global average lose value
                global_lose = self._global_lose_outcome()
                for w in wins:
                    pairs.append((w.state_vector, w.outcome, global_lose, w.confidence * 0.5))
            elif loses:
                # only has lose → pair using global average win value
                global_win = self._global_win_outcome()
                for l in loses:
                    pairs.append((l.state_vector, global_win, l.outcome, l.confidence * 0.5))

        # deduplicate (by approximate hash of state vector)
        seen = set()
        deduped = []
        for vec, win, lose, w in pairs:
            key = (tuple(round(v, 1) for v in vec[:6]), round(win, 2), round(lose, 2))
            if key not in seen:
                seen.add(key)
                deduped.append((vec, win, lose, w))

        return deduped

    def _global_win_outcome(self) -> float:
        """average outcome of all success samples."""
        wins = [t.outcome for t in self._store.values() if t.outcome < 0.3]
        return sum(wins) / max(1, len(wins)) if wins else 0.1

    def _global_lose_outcome(self) -> float:
        """average outcome of all failed samples."""
        loses = [t.outcome for t in self._store.values() if t.outcome >= 0.7]
        return sum(loses) / max(1, len(loses)) if loses else 0.9

    # ── statistics and persistence ──

    def get_efficiency_baseline(
        self,
        task_type: str,
        window: int = 20,
    ) -> Dict[str, float]:
        """get efficiency baseline for a certain type of task."""
        relevant = [
            e for e in self._efficiency_history
            if e["task_type"] == task_type
        ][-window:]

        if not relevant:
            return {"avg_tokens": 0, "avg_latency": 0.0}

        return {
            "avg_tokens": sum(r["tokens"] for r in relevant) / len(relevant),
            "avg_latency": sum(r["latency"] for r in relevant) / len(relevant),
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "buffer_size": len(self._buffer),
            "total_stored": len(self._store),
            "efficiency_history": len(self._efficiency_history),
            "active_trajectories": len(self._task_trajectories),
            "by_source": {
                source.value: sum(1 for t in self._store.values() if t.source == source.value)
                for source in SignalSource
            },
        }

    def _save(self):
        """persistent storage."""
        path = os.path.join(self.data_dir, "signal_store.json")
        try:
            data = {
                "triples": [t.to_dict() for t in self._store.values()],
                "efficiency_history": self._efficiency_history[-500:],
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Signal store save failed: {e}")

    def _load(self):
        """load persistent storage."""
        path = os.path.join(self.data_dir, "signal_store.json")
        if not os.path.isfile(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            for td in data.get("triples", []):
                t = TrainingTriple(
                    state_vector=td["state_vector"],
                    outcome=td["outcome"],
                    confidence=td.get("confidence", 1.0),
                    source=td.get("source", "environment"),
                    timestamp=td.get("timestamp", time.time()),
                    metadata=td.get("metadata", {}),
                    triple_id=td.get("triple_id", ""),
                )
                self._store[t.triple_id] = t
            self._efficiency_history = data.get("efficiency_history", [])
        except Exception as e:
            logger.warning(f"Signal store load failed: {e}")
