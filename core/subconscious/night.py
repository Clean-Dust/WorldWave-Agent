"""
ww/core/subconscious/night.py — Nighttime Engine (DCPM-inspired)

Idle-time batch processor that runs when the system is not busy.
Performs 3 cognitive operations on collected state vectors, ALL with NO LLM calls:

1. Feature Clustering — K-means over historical state vectors
2. Schema Induction — Transition matrix between cluster states
3. Bidirectional Supersedes Pointers — Belief revision tracking

Pure Python, zero external dependencies. No text processing.
"""

from __future__ import annotations
import json
import logging
import math
import os
import random
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .signal_pipeline import TrainingTriple

logger = logging.getLogger("ww.subconscious.night")

NIGHT_DIR = os.path.expanduser("~/worldwave/data/subconscious/nighttime")

# ════════════════════════════════════════════════════════════════
#  Math helpers (pure python, no numpy)
# ════════════════════════════════════════════════════════════════


def _l2_dist(a: List[float], b: List[float]) -> float:
    """Euclidean distance."""
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(min(len(a), len(b)))))


def _cos_sim(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors. Returns 0 if one is zero."""
    dot = sum(a[i] * b[i] for i in range(min(len(a), len(b))))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na < 1e-10 or nb < 1e-10:
        return 0.0
    return dot / (na * nb)


def _mean_vec(vecs: List[List[float]]) -> List[float]:
    """Component-wise mean of a list of vectors."""
    if not vecs:
        return []
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(vecs[i][j] for i in range(n)) / n for j in range(dim)]


# ════════════════════════════════════════════════════════════════
#  K-means
# ════════════════════════════════════════════════════════════════


class KMeans:
    """Pure Python K-means clustering.

    Args:
        n_clusters: number of clusters
        max_iter: max iterations
        tol: convergence tolerance (mean centroid shift)
        seed: random seed for reproducibility
    """

    def __init__(
        self,
        n_clusters: int = 8,
        max_iter: int = 50,
        tol: float = 1e-4,
        seed: int = 42,
    ):
        self.n_clusters = n_clusters
        self.max_iter = max_iter
        self.tol = tol
        self.seed = seed
        self.centroids: List[List[float]] = []
        self.labels: List[int] = []
        self.inertia: float = 0.0
        self._trained = False

    def fit(self, X: List[List[float]]) -> "KMeans":
        """Fit clusters to data X (list of vectors)."""
        if len(X) < self.n_clusters:
            self.n_clusters = max(1, len(X))

        rng = random.Random(self.seed)

        # K-means++ initialisation
        self.centroids = [list(X[rng.randint(0, len(X) - 1)])]
        for _ in range(1, self.n_clusters):
            dists = [min(_l2_dist(x, c) ** 2 for c in self.centroids) for x in X]
            total = sum(dists)
            if total < 1e-12:
                break
            r = rng.random() * total
            cumulative = 0.0
            for i, d in enumerate(dists):
                cumulative += d
                if cumulative >= r:
                    self.centroids.append(list(X[i]))
                    break
            else:
                self.centroids.append(list(X[-1]))

        # Iterate
        for iteration in range(self.max_iter):
            # Assign
            self.labels = [self._nearest(x) for x in X]

            # Recompute centroids
            clusters: Dict[int, List[List[float]]] = defaultdict(list)
            for i, label in enumerate(self.labels):
                clusters[label].append(X[i])

            new_centroids: List[List[float]] = []
            shift_sum = 0.0
            for cid in range(self.n_clusters):
                if cid in clusters:
                    new_c = _mean_vec(clusters[cid])
                else:
                    # Empty cluster — keep old centroid
                    new_c = list(self.centroids[cid]) if cid < len(self.centroids) else [0.0] * len(X[0])
                new_centroids.append(new_c)
                if cid < len(self.centroids):
                    shift_sum += _l2_dist(self.centroids[cid], new_c)

            self.centroids = new_centroids
            avg_shift = shift_sum / max(1, self.n_clusters)
            if avg_shift < self.tol:
                break

        # Final inertia (within-cluster sum of squares)
        self.inertia = 0.0
        for i, label in enumerate(self.labels):
            if label < len(self.centroids):
                self.inertia += _l2_dist(X[i], self.centroids[label]) ** 2

        self._trained = True
        return self

    def predict(self, x: List[float]) -> int:
        """Return nearest cluster index for a single vector."""
        return self._nearest(x)

    def _nearest(self, x: List[float]) -> int:
        best_d = float("inf")
        best_c = 0
        for i, c in enumerate(self.centroids):
            d = _l2_dist(x, c)
            if d < best_d:
                best_d = d
                best_c = i
        return best_c

    def to_dict(self) -> dict:
        return {
            "n_clusters": self.n_clusters,
            "centroids": self.centroids,
            "inertia": self.inertia,
            "trained": self._trained,
        }


# ════════════════════════════════════════════════════════════════
#  Schema Induction
# ════════════════════════════════════════════════════════════════


class SchemaInducer:
    """Analyze transitions between cluster states to discover schemas.

    A "schema" is a statistically significant transition pattern:
      cluster_A → cluster_B with probability > threshold.
    """

    def __init__(self, min_transition_prob: float = 0.3):
        self.min_transition_prob = min_transition_prob
        self.transition_matrix: Dict[Tuple[int, int], int] = defaultdict(int)
        self.cluster_counts: Dict[int, int] = defaultdict(int)
        self.schemas: List[Dict[str, Any]] = []
        self.loops: List[Dict[str, Any]] = []

    def feed_sequence(self, labels: List[int]):
        """Feed a time-ordered sequence of cluster labels."""
        for i in range(len(labels) - 1):
            self.transition_matrix[(labels[i], labels[i + 1])] += 1
            self.cluster_counts[labels[i]] += 1
        if labels:
            self.cluster_counts[labels[-1]] += 1

    def induce(self) -> Dict[str, Any]:
        """Run schema induction and return results."""
        # Normalise transitions to probabilities
        transition_probs: Dict[Tuple[int, int], float] = {}
        for (src, dst), count in self.transition_matrix.items():
            total = self.cluster_counts.get(src, 1)
            transition_probs[(src, dst)] = count / max(1, total)

        # Extract schemas (transitions above threshold)
        self.schemas = []
        for (src, dst), prob in transition_probs.items():
            if prob >= self.min_transition_prob:
                self.schemas.append({
                    "from_cluster": src,
                    "to_cluster": dst,
                    "probability": round(prob, 3),
                    "count": self.transition_matrix[(src, dst)],
                })
        self.schemas.sort(key=lambda s: -s["probability"])

        # Detect 2-step loops: A→B→A pattern
        loop_candidates: Dict[Tuple[int, int], int] = defaultdict(int)
        for (src, mid), prob_ab in transition_probs.items():
            if prob_ab < self.min_transition_prob:
                continue
            for (mid2, dst), prob_ba in transition_probs.items():
                if mid2 != mid or dst != src:
                    continue
                if prob_ba < self.min_transition_prob:
                    continue
                pair = tuple(sorted([src, mid]))
                loop_candidates[pair] += 1

        self.loops = []
        for (a, b), count in loop_candidates.items():
            if a != b:
                self.loops.append({
                    "cluster_a": a,
                    "cluster_b": b,
                    "oscillation_count": count,
                    "pattern": f"{a}↔{b}",
                })
        self.loops.sort(key=lambda l: -l["oscillation_count"])

        return {
            "schemas": self.schemas,
            "loops": self.loops,
            "total_transitions": len(self.transition_matrix),
        }


# ════════════════════════════════════════════════════════════════
#  Bidirectional Supersedes Pointers
# ════════════════════════════════════════════════════════════════


class SupersedesIndex:
    """Track belief revision: when new evidence contradicts older observations.

    If two state vectors have cosine similarity > threshold but different
    outcomes, the newer observation supersedes the older one.
    """

    def __init__(self, sim_threshold: float = 0.95, max_entries: int = 1000):
        self.sim_threshold = sim_threshold
        self.max_entries = max_entries

        # (state_vector_hash → entry) entries
        # entry: {"vector": [...], "outcome": float, "supersedes": [hash], "superseded_by": [hash], "timestamp": float}
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._revision_count = 0
        self._last_cleanup = time.time()

    def observe(self, state_vector: List[float], outcome: float, ts: Optional[float] = None):
        """Register a new observation. Automatically detect and record supersedes links."""
        key = self._vector_key(state_vector)
        timestamp = ts or time.time()

        existing = self._entries.get(key)
        if existing:
            # Exact same vector seen before — keep whichever has more recent consensus
            if existing["outcome"] != outcome:
                # Contradiction — newer wins
                if timestamp > existing["timestamp"]:
                    self._link_supersedes(key, existing)
                    existing["outcome"] = outcome
                    existing["timestamp"] = timestamp
                    existing["count"] = existing.get("count", 1) + 1
                    self._revision_count += 1
                else:
                    # Existing is newer, it supersedes this observation
                    existing["count"] = existing.get("count", 1) + 1
            else:
                # Consistent with existing — reinforce
                existing["count"] = existing.get("count", 1) + 1
                existing["timestamp"] = max(existing["timestamp"], timestamp)
            return

        # New entry — check similarity against existing
        self._entries[key] = {
            "vector": state_vector[:],
            "outcome": outcome,
            "supersedes": [],
            "superseded_by": [],
            "timestamp": timestamp,
            "count": 1,
        }

        # Compare with existing entries to find similar ones
        for other_key, other in list(self._entries.items()):
            if other_key == key:
                continue
            sim = _cos_sim(state_vector, other["vector"])
            if sim >= self.sim_threshold and other["outcome"] != outcome:
                if timestamp > other["timestamp"]:
                    # This observation supersedes the other
                    self._link_supersedes(key, other)
                else:
                    # Other supersedes this one
                    # Re-add with the link
                    self._entries[key]["superseded_by"].append(other_key)

        # Cleanup if too large
        if len(self._entries) > self.max_entries:
            self._prune()

    def get_supersedes_chain(self, state_vector: List[float]) -> List[Dict[str, Any]]:
        """Get the revision chain for the closest matching vector."""
        key = self._vector_key(state_vector)
        if key in self._entries:
            return self._build_chain(key, set())
        # Try fuzzy match
        best_sim = 0.0
        best_key = None
        for k, e in self._entries.items():
            sim = _cos_sim(state_vector, e["vector"])
            if sim > best_sim:
                best_sim = sim
                best_key = k
        if best_key and best_sim > 0.8:
            return self._build_chain(best_key, set())
        return []

    def stats(self) -> dict:
        return {
            "entries": len(self._entries),
            "revisions": self._revision_count,
            "chains": sum(1 for e in self._entries.values() if e["supersedes"]),
        }

    def to_dict(self) -> dict:
        return {
            "entries": {
                k: {
                    "outcome": e["outcome"],
                    "supersedes_count": len(e["supersedes"]),
                    "superseded_by_count": len(e["superseded_by"]),
                    "count": e["count"],
                }
                for k, e in self._entries.items()
            },
            "revisions": self._revision_count,
        }

    def _vector_key(self, vec: List[float]) -> str:
        """Hash a state vector to a string key (first 8 dims rounded)."""
        key_parts = [str(round(v, 3)) for v in vec[:8]]
        return ":".join(key_parts)

    def _link_supersedes(self, newer_key: str, older: dict):
        """Create a supersedes link from newer to older."""
        self._entries[newer_key]["supersedes"].append(
            self._entries.get(newer_key, {}).get("supersedes", [])
        )

    def _build_chain(self, key: str, visited: set) -> List[Dict[str, Any]]:
        if key in visited or key not in self._entries:
            return []
        visited.add(key)
        e = self._entries[key]
        chain = [{"outcome": e["outcome"], "timestamp": e["timestamp"], "count": e["count"]}]
        for sup_key in e.get("supersedes", []):
            chain.extend(self._build_chain(sup_key, visited))
        for sup_key in e.get("superseded_by", []):
            chain.extend(self._build_chain(sup_key, visited))
        return chain

    def _prune(self):
        """Remove oldest entries that have no supersedes links."""
        keys = sorted(self._entries.keys(),
                      key=lambda k: self._entries[k]["timestamp"])
        for k in keys:
            if len(self._entries) <= self.max_entries * 0.75:
                break
            e = self._entries[k]
            if not e["supersedes"] and not e["superseded_by"]:
                del self._entries[k]


# ════════════════════════════════════════════════════════════════
#  Nighttime Engine (orchestrator)
# ════════════════════════════════════════════════════════════════


class NighttimeEngine:
    """Orchestrator for all three nighttime cognitive operations.

    Usage:
        engine = NighttimeEngine()
        engine.feed(triples)        # Feed TrainingTriple list
        engine.feed_labels(labels)  # Feed cluster labels sequence
        result = engine.run()       # Run all 3 operations
    """

    def __init__(
        self,
        n_clusters: int = 8,
        min_transition_prob: float = 0.3,
        supersedes_threshold: float = 0.95,
        data_dir: str = NIGHT_DIR,
        auto_persist: bool = True,
    ):
        self.n_clusters = n_clusters
        self.auto_persist = auto_persist
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

        # Sub-modules
        self.kmeans = KMeans(n_clusters=n_clusters)
        self.schema = SchemaInducer(min_transition_prob=min_transition_prob)
        self.supersedes = SupersedesIndex(sim_threshold=supersedes_threshold)

        # Data buffers
        self._vectors: List[List[float]] = []
        self._triples: List[TrainingTriple] = []
        self._labels: List[int] = []
        self._run_count = 0
        self._last_result: Optional[Dict[str, Any]] = None

    def feed(self, triples: List[TrainingTriple]):
        """Feed TrainingTriple objects into the engine."""
        self._triples.extend(triples)
        for t in triples:
            self._vectors.append(list(t.state_vector))
            self.supersedes.observe(t.state_vector, t.outcome, t.timestamp)
        # Keep a bound
        if len(self._triples) > 5000:
            self._triples = self._triples[-5000:]
            self._vectors = self._vectors[-5000:]

    def feed_vectors(self, vectors: List[List[float]]):
        """Feed raw state vectors (without training triples)."""
        self._vectors.extend(vectors)
        if len(self._vectors) > 5000:
            self._vectors = self._vectors[-5000:]

    def feed_labels(self, labels: List[int]):
        """Feed cluster label sequence for schema induction."""
        self._labels = labels

    def run(self) -> Dict[str, Any]:
        """Execute all 3 nighttime operations. Returns combined result dict."""
        start = time.time()
        result: Dict[str, Any] = {
            "timestamp": start,
            "run_id": self._run_count,
            "vectors_processed": len(self._vectors),
        }

        # ── 1. Clustering ──
        if len(self._vectors) >= self.n_clusters:
            self.kmeans.fit(self._vectors)
            self._labels = self.kmeans.labels
            result["clustering"] = self.kmeans.to_dict()
        else:
            result["clustering"] = {"n_clusters": 0, "reason": "insufficient vectors"}
            logger.debug(f"Night: skipping clustering, only {len(self._vectors)} vectors")

        # ── 2. Schema induction ──
        if len(self._labels) > 1:
            self.schema.feed_sequence(self._labels)
            result["schema"] = self.schema.induce()
        else:
            result["schema"] = {"schemas": [], "loops": [], "reason": "insufficient labels"}

        # ── 3. Supersedes pointers ──
        result["supersedes"] = self.supersedes.stats()

        result["duration_s"] = round(time.time() - start, 3)
        self._last_result = result
        self._run_count += 1

        # Persist
        if self.auto_persist:
            self._persist(result)

        logger.info(
            f"🌙 Nighttime engine run #{self._run_count}: "
            f"{result.get('clustering', {}).get('n_clusters', 0)} clusters, "
            f"{len(result.get('schema', {}).get('schemas', []))} schemas, "
            f"{result.get('supersedes', {}).get('revisions', 0)} revisions "
            f"in {result['duration_s']}s"
        )
        return result

    def get_last_result(self) -> Optional[Dict[str, Any]]:
        return self._last_result

    def get_cluster_info(self, state_vector: List[float]) -> Dict[str, Any]:
        """Get cluster and schema info for a given state vector."""
        if not self.kmeans._trained:
            return {"cluster_id": -1, "info": "not trained"}
        cid = self.kmeans.predict(state_vector)
        # Find schemas originating from this cluster
        schemas_from = [s for s in self.schema.schemas if s["from_cluster"] == cid]
        return {
            "cluster_id": cid,
            "centroid": self.kmeans.centroids[cid] if cid < len(self.kmeans.centroids) else [],
            "schemas_from": schemas_from[:5],
            "loops": [l for l in self.schema.loops if cid in (l["cluster_a"], l["cluster_b"])],
        }

    def get_supersedes(self, state_vector: List[float]) -> List[Dict[str, Any]]:
        return self.supersedes.get_supersedes_chain(state_vector)

    def _persist(self, result: dict):
        """Save latest result to disk."""
        try:
            path = os.path.join(self.data_dir, f"night_{self._run_count}.json")
            with open(path, "w") as f:
                json.dump(result, f, ensure_ascii=False, default=str)
            # Keep symlink to latest
            latest = os.path.join(self.data_dir, "latest.json")
            try:
                os.remove(latest)
            except OSError:
                pass
            os.symlink(os.path.basename(path), latest)
        except Exception as e:
            logger.warning(f"Night: persist failed: {e}")

    def load_latest(self) -> Optional[Dict[str, Any]]:
        """Load the most recent persisted result."""
        latest = os.path.join(self.data_dir, "latest.json")
        try:
            with open(latest, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
