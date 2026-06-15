"""
ww/core/subconscious/contrastive.py — contrastive learningengine（Contrastive Learning Engine）

DPO-style training, directly at leaf node value empty operation.

core idea:
  unchangedtreestructure（splitfeature/threshold），adjust onlyleafnodevalue。
  for each (state, Y_win, Y_lose) comparison pair：
    findto  state fall into leafnode，push-pull theleafnodevalue：
      - if leafnodevaluefrom Y_lose ratio distance Y_win close → toward Y_win pull
      - use margin γ ensure win/lose  at least γ   separation

loss function (implicit):
  L = max(0, predict(state, lose_params) - predict(state, win_params) + γ)

this is a piecewise constant function (decision tree) advantage: no need for backpropagation,
directly operate on discrete leaf nodes, CPU overhead is basically zero.

mathematical form:
  Δ(value) = η · (Y_win - value)   if state's leaf has win_count > lose_count
  Δ(value) = η · (Y_lose - value)  if state's leaf has lose_count > win_count

where η = lr · confidence · (|win_count - lose_count| / max(win_count, lose_count, 1))
"""

from __future__ import annotations
import copy
import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from core.predictor import DecisionTree, RandomForest

logger = logging.getLogger("ww.subconscious.contrastive")


class ContrastiveEngine:
    """
    contrastive learningengine。

    usage：
      engine = ContrastiveEngine(learning_rate=0.1, margin=0.3)
      engine.contrastive_update(forest, contrast_pairs)
    """

    def __init__(
        self,
        learning_rate: float = 0.15,
        margin: float = 0.3,
        min_pairs_per_leaf: int = 2,
        max_adjustment_per_step: float = 0.3,
    ):
        """
        Args:
            learning_rate: each timeupdatestride (0.0-1.0)
            margin: win/lose   minimum separation (0.0-1.0)
            min_pairs_per_leaf: eachleafnodeminimum contrastive pairs neededupdate
            max_adjustment_per_step: max adjustment per step
        """
        self.lr = learning_rate
        self.margin = margin
        self.min_pairs = min_pairs_per_leaf
        self.max_adj = max_adjustment_per_step
        self._update_count = 0
        self._total_adjustment = 0.0

    def contrastive_update(
        self,
        model: RandomForest,
        contrast_pairs: List[Tuple[List[float], float, float, float]],
    ) -> Dict[str, Any]:
        """
    execute DPO contrastive learningupdate。

    Args:
        model: toupdate  RandomForest
        contrast_pairs: [(state_vector, Y_win, Y_lose, weight), ...]
            Y_win and Y_lose both is  0.0-1.0   outcome value。
            usually Y_win = 0.0 (success), Y_lose = 1.0 (failed)。
            weight = credibility (0.0-1.0)

    Returns:
        {
            "updated": bool,
            "trees": int,        # update treenumber
            "leaves_pushed": int, #  adjust leafnodenumber
            "avg_adjustment": float,
            "margin_violations": int,  # update violate margin  logarithm
            "margin_resolved": int,    # update solve logarithm
        }
        """
        if not model.trees or not contrast_pairs:
            return {"updated": False, "reason": "no trees or no pairs"}

        original = copy.deepcopy(model.trees)
        total_adjustment = 0.0
        total_leaves = 0

        # update statistics margin violations
        margin_violations = self._count_margin_violations(model, contrast_pairs)

        for tree_idx, tree in enumerate(model.trees):
            if tree.root is None:
                continue

            leaves_updated, adj = self._update_tree(
                tree, contrast_pairs, tree_idx
            )
            total_leaves += leaves_updated
            total_adjustment += adj

        # update statistics
        margin_resolved = max(0, margin_violations -
            self._count_margin_violations(model, contrast_pairs))

        self._update_count += 1
        if total_leaves > 0:
            self._total_adjustment += total_adjustment

        return {
            "updated": total_leaves > 0,
            "trees": len(model.trees),
            "leaves_pushed": total_leaves,
            "avg_adjustment": round(total_adjustment / max(1, total_leaves), 4),
            "margin_violations_before": margin_violations,
            "margin_violations_after": max(0, margin_violations - margin_resolved),
            "margin_resolved": margin_resolved,
            "total_pairs": len(contrast_pairs),
        }

    def _update_tree(
        self,
        tree: DecisionTree,
        pairs: List[Tuple[List[float], float, float, float]],
        tree_idx: int,
    ) -> Tuple[int, float]:
        """
        updatesingle treedecision tree leafnodevalue。

        for each group (state, win, lose, weight)：
          1. findto  state fall into leafnode
          2. record：this leafnodeshould toward win push back is toward lose push

        aggregationall comparison pair ，foreachleafnodecalculate final adjustment amount。
        """
        # leafnode id → {"delta": float, "count": float}
        leaf_updates: Dict[int, Dict[str, float]] = defaultdict(
            lambda: {"delta": 0.0, "wins": 0.0, "loses": 0.0, "count": 0}
        )

        # Phase 1: traverseall for，collecteachleafnode push-pull direction
        for state, win_val, lose_val, weight in pairs:
            leaf = self._find_leaf(tree.root, state)
            if leaf is None:
                continue

            leaf_id = id(leaf)  # target object id as key（samememoryposition）
            upd = leaf_updates[leaf_id]

            # when  leafnodevalue
            current = leaf.value
            # goaldirection：toward win approach（immediately decreasevalue）or is toward lose approach（immediately increasevalue）？
            # win_val < lose_val  is normal state（success=0.0, failure=1.0）

            dist_to_win = abs(current - win_val)
            dist_to_lose = abs(current - lose_val)

            # if from lose ratio distance win close → toward win push（reduce failure risk prediction）
            if dist_to_lose < dist_to_win:
                # too close lose   → pull toward win
                delta = (win_val - current) * self.lr * weight
                upd["delta"] += delta
                upd["wins"] += weight
                upd["count"] += weight
            elif dist_to_lose < self.margin and current > 0.5:
                # although far from win also close，but valueinherently high → pull down
                delta = (win_val - current) * self.lr * weight * 0.5
                upd["delta"] += delta
                upd["wins"] += weight * 0.5
                upd["count"] += weight * 0.5

        # Phase 2: applicationaggregationadjust
        leaves_updated = 0
        total_adj = 0.0

        for leaf_id, upd in leaf_updates.items():
            if upd["count"] < self.min_pairs:
                continue

            avg_delta = upd["delta"] / max(1, upd["count"])

            # limit max adjustment
            avg_delta = max(-self.max_adj, min(self.max_adj, avg_delta))

            # findto corresponding  DecisionNode object
            for node in self._iter_leaves(tree.root):
                if id(node) == leaf_id:
                    old_val = node.value
                    node.value = max(0.0, min(1.0, node.value + avg_delta))
                    leaves_updated += 1
                    total_adj += abs(node.value - old_val)
                    break

        return leaves_updated, total_adj

    def _find_leaf(self, node, state: List[float]):
        """traversedecision tree，findto  state fall into leafnode。"""
        if node is None:
            return None
        if node.is_leaf:
            return node
        if state[node.feature_idx] <= node.threshold:
            return self._find_leaf(node.left, state)
        return self._find_leaf(node.right, state)

    def _iter_leaves(self, node):
        """iterationall leafnode。"""
        if node is None:
            return
        if node.is_leaf:
            yield node
        else:
            yield from self._iter_leaves(node.left)
            yield from self._iter_leaves(node.right)

    def _count_margin_violations(
        self,
        model: RandomForest,
        pairs: List[Tuple[List[float], float, float, float]],
    ) -> int:
        """
        calculate has how many contrastive pairs violated margin condition：
          predict(lose_state) - predict(win_state) < margin

        note：same contrastive pairat forest differenttree may different performance。
        hereuseforest ensembleprediction（all tree average）。
        """
        violations = 0
        for state, win_val, lose_val, _ in pairs:
            pred = model.predict(state)
            risk = getattr(pred, 'crash_risk', pred)
            # expected：pred should close to win_val（0.0），far from lose_val（1.0）
            # violate：pred too close lose_val or win/lose  insufficient spacing
            dist_to_lose = abs(risk - lose_val)
            margin_achieved = lose_val - win_val
            if dist_to_lose < self.margin or margin_achieved < self.margin * 0.5:
                violations += 1
        return violations

    def estimate(self) -> Dict[str, Any]:
        """estimateengineusestatistics。"""
        return {
            "update_count": self._update_count,
            "total_adjustment": round(self._total_adjustment, 4),
            "avg_adjustment_per_update": round(
                self._total_adjustment / max(1, self._update_count), 4
            ),
            "learning_rate": self.lr,
            "margin": self.margin,
        }


# ════════════════════════════════════════════════════════════════
#  CFR — Counterfactual Regret Minimization
# ════════════════════════════════════════════════════════════════


class CFREngine:
    """
    Counterfactual Regret Minimization for subconscious decision optimization.

    Tracks regret for each feature-region bucket across two actions:
      - action 0: predict "success" (low failure risk, outcome near 0.0)
      - action 1: predict "failure" (high failure risk, outcome near 1.0)

    Uses regret matching to compute a strategy that minimises cumulative regret.
    Blends CFR-adjusted predictions with the original model output.

    Pure Python, zero external dependencies.
    """

    def __init__(
        self,
        n_bins: int = 64,
        regret_weight: float = 0.3,
        action_values: Optional[Tuple[float, float]] = None,
    ):
        """
        Args:
            n_bins: max number of feature-region buckets
            regret_weight: blending weight (0.0 = pure model, 1.0 = pure CFR)
            action_values: (value_for_success_prediction, value_for_failure_prediction)
                           Default: (0.0, 1.0) — same as outcome encoding
        """
        self.n_bins = n_bins
        self.regret_weight = regret_weight
        self.av = action_values or (0.0, 1.0)

        # Per-bucket regret: bucket_key → [regret_accumulator_for_action_0, ..._for_action_1]
        self._regret: Dict[str, List[float]] = {}
        # Per-bucket action counts
        self._action_count: Dict[str, List[int]] = {}
        # Per-bucket total observations
        self._obs_count: Dict[str, int] = {}

        self._total_updates = 0
        self._total_adjustments = 0.0

    # ── Public API ──

    def observe(
        self,
        state_vector: List[float],
        model_prediction: float,
        actual_outcome: float,
    ) -> float:
        """Feed one observation and get the CFR-adjusted prediction.

        Args:
            state_vector: 32-dim padded feature vector
            model_prediction: original model output (0.0-1.0)
            actual_outcome: what actually happened (0.0=success, 1.0=failure)

        Returns:
            CFR-adjusted prediction (model_prediction blended with CFR strategy)
        """
        bucket = self._bucket(state_vector)

        # Initialise bucket
        if bucket not in self._regret:
            self._regret[bucket] = [0.0, 0.0]
            self._action_count[bucket] = [0, 0]
            self._obs_count[bucket] = 0

        # Compute regret for each action
        # regret[action] = actual_outcome - action_value[action]
        for a, val in enumerate(self.av):
            self._regret[bucket][a] += actual_outcome - val

        # Increment observation count
        self._obs_count[bucket] += 1

        # Get adjusted prediction
        adjusted = self.adjust(state_vector, model_prediction)
        self._total_updates += 1

        return adjusted

    def adjust(
        self,
        state_vector: List[float],
        model_prediction: float,
    ) -> float:
        """Compute CFR-adjusted prediction without recording a new observation.

        Args:
            state_vector: 32-dim padded feature vector
            model_prediction: original model output (0.0-1.0)

        Returns:
            Blended prediction: (1 - w) * model + w * cfr_strategy
        """
        bucket = self._bucket(state_vector)

        if bucket not in self._regret:
            return model_prediction

        # Regret matching: strategy = positive_regret / sum(positive_regret)
        positive = [max(0.0, r) for r in self._regret[bucket]]
        total_pos = sum(positive)

        if total_pos > 1e-10:
            strategy = [p / total_pos for p in positive]
        else:
            strategy = [0.5, 0.5]  # uniform

        # Expected value under CFR strategy
        cfr_value = sum(strategy[a] * self.av[a] for a in range(len(self.av)))

        # Blend with model prediction
        w = self.regret_weight
        blended = (1.0 - w) * model_prediction + w * cfr_value

        self._total_adjustments += abs(blended - model_prediction)
        return max(0.0, min(1.0, blended))  # clamp

    def adjust_many(
        self,
        state_vectors: List[List[float]],
        model_predictions: List[float],
    ) -> List[float]:
        """Batch adjust multiple predictions."""
        return [
            self.adjust(sv, mp) for sv, mp in zip(state_vectors, model_predictions)
        ]

    def reset(self):
        """Clear all regret tables."""
        self._regret.clear()
        self._action_count.clear()
        self._obs_count.clear()
        self._total_updates = 0
        self._total_adjustments = 0.0

    def stats(self) -> Dict[str, Any]:
        """Return debugging info."""
        total_obs = sum(self._obs_count.values())
        buckets = len(self._regret)
        avg_adjust = (
            self._total_adjustments / max(1, self._total_updates)
            if self._total_updates > 0
            else 0.0
        )
        # Average regret per bucket
        regret_magnitudes = []
        for r in self._regret.values():
            regret_magnitudes.append(abs(r[0]) + abs(r[1]))
        avg_regret = sum(regret_magnitudes) / max(1, len(regret_magnitudes))
        return {
            "buckets": buckets,
            "total_observations": total_obs,
            "total_updates": self._total_updates,
            "avg_adjustment": round(avg_adjust, 6),
            "avg_regret_magnitude": round(avg_regret, 6),
            "regret_weight": self.regret_weight,
            "action_values": list(self.av),
        }

    # ── Internal ──

    def _bucket(self, state_vector: List[float]) -> str:
        """Hash first 6 dynamic feature dimensions into a bucket key.

        Rounds to 2 decimal places to create stable bins.
        """
        key_parts = []
        for i in range(min(6, len(state_vector))):
            key_parts.append(str(round(state_vector[i], 2)))
        return ":".join(key_parts)
