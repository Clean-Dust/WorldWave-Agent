"""
ww/core/subconscious/sandbox.py — localsandboxvalidate（Local Sandbox Validation）

First line of defense: never blindly trust weights from external sources.

When Agent receives a new subconscious model from a neighbor node, first place it in a virtual sandbox.
Use locally accumulated historical data (Validation Set) to test run the new model.
Only when the new model's performance >= local model, allow merging.

Decision metrics:
  - accuracy（Accuracy）
  - Inference speed (Inference Latency)
  - Token consumption reduction (roughly estimated by model size)
"""

from __future__ import annotations
import json
import logging
import math
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple


# ── TriageVector compat helper ──
# DeepRiskNet.predict() returns TriageVector; old models return float.
# This helper extracts the crash_risk scalar from either.


def _safe_risk(pred: Any) -> float:
    """Extract crash_risk from TriageVector or return float directly."""
    return getattr(pred, 'crash_risk', pred)

from .features import PADDED_FEATURES

from .predictor import DecisionTree, DecisionNode, RandomForest  # noqa: F401 — legacy aliases
# v8: sandbox now uses DeepRiskNet for validation
# DecisionTree/DecisionNode/RandomForest are aliases for DeepRiskNet

logger = logging.getLogger("ww.subconscious.sandbox")

SANDBOX_DIR = os.path.expanduser("~/worldwave/data/subconscious/sandbox")


# ════════════════════════════════════════════════════════════════
#  Validate metric calculation
# ════════════════════════════════════════════════════════════════


class SandboxValidator:
    """
    sandboxvalidate 。

    usage：
      1. Receive a new tree from peer → validate_tree(new_tree, validation_data)
      2. Receive complete RF from peer → validate_model(new_model, validation_data)
      3. compare new model vs localmodel performance delta

    Validation Data format：
      [(state_vector_12d, ground_truth), ...]
      where  ground_truth = 0.0（success）or 1.0（fault）
    """

    def __init__(
        self,
        accuracy_weight: float = 0.6,
        latency_weight: float = 0.2,
        complexity_weight: float = 0.2,
    ):
        """
        Args:
            accuracy_weight: accuracyweight（mainmetric）
            latency_weight: inferencespeedweight
            complexity_weight: model complexityweight（smaller is better → inferencefaster、save token）
        """
        self.accuracy_weight = accuracy_weight
        self.latency_weight = latency_weight
        self.complexity_weight = complexity_weight

        os.makedirs(SANDBOX_DIR, exist_ok=True)

    # ── single treetreevalidate ──

    def validate_tree(
        self,
        new_tree: DecisionTree,
        validation_data: List[Tuple[List[float], float]],
        local_tree: Optional[DecisionTree] = None,
    ) -> Dict[str, Any]:
        """
        validateone from peer  newdecision tree。

        Args:
            new_tree: from peer  tree
            validation_data: [(state_vector, ground_truth), ...]
            local_tree: current has localtree（optional，for comparison delta）

        Returns:
            {
                "passed": bool,        #  is whether passedvalidate
                "accuracy": float,      # newtreeaccuracy
                "accuracy_delta": float,# compared tolocaltree improvement
                "latency_us": float,    # averageinferencelatency（microsecond）
                "complexity_score": int, # nodenumber（smaller is better）
                "scores": {...},        # detailscore
                "verdict": str,         # passed / rejected / marginal
            }
        """
        if not validation_data:
            return {
                "passed": False,
                "verdict": "rejected",
                "reason": "validation data empty",
            }

        # accuracy
        correct = 0
        for vec, truth in validation_data:
            pred = new_tree.predict(vec)
            if abs(_safe_risk(pred) - truth) <= 0.5:
                correct += 1
        accuracy = correct / max(1, len(validation_data))

        # inferencelatency（microsecond）
        latencies = []
        for vec, _ in validation_data[:100]:  # at most 100 samplessamplemeasurelatency
            start = time.perf_counter_ns()
            new_tree.predict(vec)
            elapsed_ns = time.perf_counter_ns() - start
            latencies.append(elapsed_ns / 1000.0)  # ns → µs
        avg_latency = sum(latencies) / max(1, len(latencies))

        # model complexity（nodenumber）
        complexity = self._count_nodes(new_tree.root)

        # compared tolocalmodel  delta
        accuracy_delta = 0.0
        latency_delta = 0.0
        complexity_delta = 0
        if local_tree is not None and local_tree.root is not None:
            local_correct = 0
            for vec, truth in validation_data:
                pred = local_tree.predict(vec)
                if abs(_safe_risk(pred) - truth) <= 0.5:
                    local_correct += 1
            local_accuracy = local_correct / max(1, len(validation_data))
            accuracy_delta = accuracy - local_accuracy

            local_complexity = self._count_nodes(local_tree.root)
            complexity_delta = complexity - local_complexity

        # comprehensive score
        scores = self._compute_score(
            accuracy=accuracy,
            accuracy_delta=accuracy_delta,
            latency_us=avg_latency,
            complexity=complexity,
            complexity_delta=complexity_delta,
        )

        verdict = "passed" if scores["total"] >= 0.5 else "rejected"
        if 0.3 <= scores["total"] < 0.5:
            verdict = "marginal"

        result = {
            "passed": verdict == "passed",
            "accuracy": round(accuracy, 4),
            "accuracy_delta": round(accuracy_delta, 4),
            "latency_us": round(avg_latency, 2),
            "complexity_score": complexity,
            "scores": scores,
            "verdict": verdict,
            "samples_tested": len(validation_data),
        }

        # record
        self._save_result(result)
        return result

    # ── completemodelvalidate ──

    def validate_model(
        self,
        new_model: RandomForest,
        validation_data: List[Tuple[List[float], float]],
        local_model: Optional[RandomForest] = None,
    ) -> Dict[str, Any]:
        """
        validatea from peer  complete Random Forest。

         and  validate_tree similar to ，but for the entireensembleevaluateevaluate。
        """
        if not validation_data:
            return {
                "passed": False,
                "verdict": "rejected",
                "reason": "validation data empty",
            }

        correct = 0
        for vec, truth in validation_data:
            pred = new_model.predict(vec)
            if abs(_safe_risk(pred) - truth) <= 0.5:
                correct += 1
        accuracy = correct / max(1, len(validation_data))

        latencies = []
        for vec, _ in validation_data[:50]:
            start = time.perf_counter_ns()
            new_model.predict(vec)
            elapsed_ns = time.perf_counter_ns() - start
            latencies.append(elapsed_ns / 1000.0)
        avg_latency = sum(latencies) / max(1, len(latencies))

        total_nodes = sum(
            self._count_nodes(t.root) for t in new_model.trees if t.root
        )

        accuracy_delta = 0.0
        if local_model is not None and not local_model.empty():
            local_correct = 0
            for vec, truth in validation_data:
                pred = local_model.predict(vec)
                if abs(_safe_risk(pred) - truth) <= 0.5:
                    local_correct += 1
            accuracy_delta = accuracy - (local_correct / max(1, len(validation_data)))

        scores = self._compute_score(
            accuracy=accuracy,
            accuracy_delta=accuracy_delta,
            latency_us=avg_latency,
            complexity=total_nodes,
        )

        verdict = "passed" if scores["total"] >= 0.5 else "rejected"
        if 0.3 <= scores["total"] < 0.5:
            verdict = "marginal"

        return {
            "passed": verdict == "passed",
            "accuracy": round(accuracy, 4),
            "accuracy_delta": round(accuracy_delta, 4),
            "latency_us": round(avg_latency, 2),
            "total_nodes": total_nodes,
            "trees": len(new_model.trees),
            "scores": scores,
            "verdict": verdict,
            "samples_tested": len(validation_data),
        }

    # ── internalmethod ──

    def _compute_score(
        self,
        accuracy: float,
        accuracy_delta: float = 0.0,
        latency_us: float = 0.0,
        complexity: int = 0,
        complexity_delta: int = 0,
    ) -> Dict[str, float]:
        """
        calculate composite score（0.0-1.0）。

        Score = w1 * accuracy_score + w2 * latency_score + w3 * complexity_score

        - accuracy_score = accuracy（0.5 = baseline, 1.0 = perfect）
          but if  has  delta then use delta，encouragement ratiolocalmodel is better 
        - latency_score = exp(-latency / 100)（slower is worse）
        - complexity_score = exp(-nodes / 200)（nodemore is worse）
        """
        if accuracy_delta != 0:
            #  has localmodel comparison → see delta
            # delta = +0.1 → score 0.6, delta = -0.1 → score 0.4
            acc_score = 0.5 + (accuracy_delta * 2.0)
            acc_score = max(0.0, min(1.0, acc_score))
        else:
            # no comparison → directly useaccuracy
            acc_score = accuracy

        lat_score = math.exp(-latency_us / 100.0)
        comp_score = math.exp(-complexity / 200.0)

        total = (
            self.accuracy_weight * acc_score
            + self.latency_weight * lat_score
            + self.complexity_weight * comp_score
        )

        return {
            "accuracy_score": round(acc_score, 4),
            "latency_score": round(lat_score, 4),
            "complexity_score": round(comp_score, 4),
            "total": round(total, 4),
        }

    @staticmethod
    def _count_nodes(node: Optional[DecisionNode]) -> int:
        """recursive calculationdecision tree nodetotal number。"""
        if node is None:
            return 0
        if node.is_leaf:
            return 1
        return 1 + SandboxValidator._count_nodes(node.left) + SandboxValidator._count_nodes(node.right)

    # ── A/B test（End-to-End Performance Comparison） ──

    def ab_test(
        self,
        old_model: RandomForest,
        new_model: RandomForest,
        validation_data: List[Tuple[List[float], float]],
    ) -> Dict[str, Any]:
        """
        A/B test：compare old and new models endto endperformance。

        measurementmetric：
          - Accuracy Delta（accuracychange）
          - Latency Delta（inferencelatencychange）
          - Complexity Delta（model complexity change）
          - Weighted Score（comprehensive score）
          - Verdict（commit / hold / reject）

        this layer is 「ultimate defense line」：even if Multi-Krum judge a batchweightsecure，
        A/B testensure new model truly  has actual improvement only Commit。

        Args:
            old_model: when  runningline model
            new_model: from peer  new model
            validation_data: [(state_vector, ground_truth), ...]

        Returns:
            {
                "old_accuracy": float,
                "new_accuracy": float,
                "accuracy_delta": float,
                "old_latency_us": float,
                "new_latency_us": float,
                "latency_delta_pct": float,
                "old_complexity": int,
                "new_complexity": int,
                "complexity_delta": int,
                "weighted_score": float,  # 0.0-1.0
                "verdict": "commit" | "hold" | "reject",
                "improvements": [str],  # improvement oriented towards
                "regressions": [str],   # regression oriented towards
            }
        """
        if not validation_data:
            return {"verdict": "hold", "reason": "no validation data"}

        # ── Accuracy ──
        old_correct = 0
        new_correct = 0
        for vec, truth in validation_data:
            if abs(_safe_risk(old_model.predict(vec)) - truth) <= 0.5:
                old_correct += 1
            if abs(_safe_risk(new_model.predict(vec)) - truth) <= 0.5:
                new_correct += 1

        n = len(validation_data)
        old_acc = old_correct / n
        new_acc = new_correct / n
        acc_delta = new_acc - old_acc

        # ── Latency（take  50 samplessamplemeasure） ──
        old_latencies, new_latencies = [], []
        for vec, _ in validation_data[:50]:
            start = time.perf_counter_ns()
            old_model.predict(vec)
            old_latencies.append((time.perf_counter_ns() - start) / 1000.0)

            start = time.perf_counter_ns()
            new_model.predict(vec)
            new_latencies.append((time.perf_counter_ns() - start) / 1000.0)

        old_lat = sum(old_latencies) / len(old_latencies)
        new_lat = sum(new_latencies) / len(new_latencies)
        lat_delta_pct = ((new_lat - old_lat) / max(old_lat, 0.001)) * 100

        # ── Complexity（nodetotal number） ──
        old_nodes = sum(self._count_nodes(t.root) for t in old_model.trees if t.root)
        new_nodes = sum(self._count_nodes(t.root) for t in new_model.trees if t.root)
        comp_delta = new_nodes - old_nodes

        # ── comprehensive score ──
        improvements = []
        regressions = []

        # Accuracy score (0-1): positive delta → bonus points
        acc_score = 0.5 + acc_delta * 5.0  # +0.1 acc → +0.5 score
        acc_score = max(0.0, min(1.0, acc_score))

        # Latency score (0-1): negative delta（faster）→ bonus points
        lat_score = 0.5 - (lat_delta_pct / 100.0)
        lat_score = max(0.0, min(1.0, lat_score))

        # Complexity score (0-1): negative delta（smaller）→ bonus points
        comp_score = 0.5 - (comp_delta / max(old_nodes, 1)) * 0.5
        comp_score = max(0.0, min(1.0, comp_score))

        # weighted total score（accuracyweighthighest）
        weighted = acc_score * 0.6 + lat_score * 0.2 + comp_score * 0.2

        # judgment
        if acc_delta > 0.02:
            improvements.append(f"accuracy +{acc_delta:.1%}")
        elif acc_delta < -0.02:
            regressions.append(f"accuracy {acc_delta:.1%}")

        if lat_delta_pct < -5:
            improvements.append(f"latency {lat_delta_pct:.0f}%")
        elif lat_delta_pct > 5:
            regressions.append(f"latency +{lat_delta_pct:.0f}%")

        if comp_delta < 0:
            improvements.append(f"model -{abs(comp_delta)} nodes")
        elif comp_delta > 0:
            regressions.append(f"model +{comp_delta} nodes")

        if weighted >= 0.55 and acc_delta >= 0:
            verdict = "commit"
        elif weighted >= 0.4:
            verdict = "hold"  # marginal, wait for more data
        else:
            verdict = "reject"

        return {
            "old_accuracy": round(old_acc, 4),
            "new_accuracy": round(new_acc, 4),
            "accuracy_delta": round(acc_delta, 4),
            "old_latency_us": round(old_lat, 2),
            "new_latency_us": round(new_lat, 2),
            "latency_delta_pct": round(lat_delta_pct, 1),
            "old_complexity": old_nodes,
            "new_complexity": new_nodes,
            "complexity_delta": comp_delta,
            "weighted_score": round(weighted, 4),
            "verdict": verdict,
            "improvements": improvements,
            "regressions": regressions,
        }

    def _save_result(self, result: dict):
        """Record validation results (for audit)."""
        path = os.path.join(SANDBOX_DIR, f"validation_{int(time.time())}.json")
        with open(path, "w") as f:
            json.dump(result, f, indent=2)

        # Keep only the latest 100 records
        all_files = sorted(
            [f for f in os.listdir(SANDBOX_DIR) if f.endswith(".json")]
        )
        while len(all_files) > 100:
            os.remove(os.path.join(SANDBOX_DIR, all_files.pop(0)))


# ════════════════════════════════════════════════════════════════
#  Validation Set management 
# ════════════════════════════════════════════════════════════════


class ValidationSetManager:
    """
    Management local validation dataset.

    From crash report store retrieve sample, keep the latest N records as validation set.
    Auto balance positive/negative samples.
    """

    def __init__(self, max_size: int = 50):
        self.max_size = max_size
        self._data: List[Tuple[List[float], float]] = []

    def add_sample(self, state_vector: List[float], outcome: float):
        """Add a sample."""
        vec = state_vector[:PADDED_FEATURES]
        if len(vec) >= 12:
            # Padding to PADDED_FEATURES dimension
            padded = list(vec)
            while len(padded) < PADDED_FEATURES:
                padded.append(0.0)
            self._data.append((padded, outcome))
            self._prune()

    def add_samples(self, samples: List[Tuple[List[float], float]]):
        for vec, outcome in samples:
            self.add_sample(vec, outcome)

    def get_data(self, balanced: bool = True) -> List[Tuple[List[float], float]]:
        """
        Get validation dataset.

        Args:
            balanced: whether to balance positive/negative

        Returns:
            [(state_vector, ground_truth), ...]
        """
        if not balanced or len(self._data) < 4:
            return self._data[:]

        pos = [(v, o) for v, o in self._data if o >= 0.5]
        neg = [(v, o) for v, o in self._data if o < 0.5]

        if not pos or not neg:
            return self._data[:]

        # Balance: take min(len(pos), len(neg)) each half
        import random as rnd
        min_count = min(len(pos), len(neg))
        sampled = rnd.sample(pos, min_count) + rnd.sample(neg, min_count)
        rnd.shuffle(sampled)
        return sampled

    def stats(self) -> Dict[str, Any]:
        pos = sum(1 for _, o in self._data if o >= 0.5)
        neg = len(self._data) - pos
        return {
            "total": len(self._data),
            "positive": pos,
            "negative": neg,
            "ratio": round(pos / max(1, neg), 2),
        }

    def _prune(self):
        while len(self._data) > self.max_size:
            self._data.pop(0)
