"""ww/core/memory/recall.py — recall engine (with token budget compression)

Recall engine is responsible for retrieving relevant information from memory.

Two retrieval modes:
1. Pattern Completion — given fragment, reconstruct complete memory
2. Diffusion Activation — BFS diffusion along link graph

Retrieval results are sorted: salience × relevance
Supports max_tokens budget: auto compress summary if exceeded
"""

from __future__ import annotations
import logging
import math
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Set, Tuple

from .atom import MemoryAtom, FactStore
from .amygdala import Amygdala
from .hippocampus import Hippocampus

logger = logging.getLogger("ww.memory.recall")

# Token estimation (heuristic, zero external dependencies):
#   English: ~1 token / 3-4 chars → len*4:over
#   Chinese: ~1 token / 1.5-2 chars → len*1.5:under
# Use 1.5 as safe composite multiplier to ensure Context Window is not over-compressed.
# Even if estimation is off, it's within LLM tolerance (truncation is better than giving up).
_CHARS_PER_TOKEN = 1.5


def _estimate_tokens(text: str) -> int:
    """Roughly estimate string token count (heuristic, not precise but fast enough, zero external dependencies)."""
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


class RecallEngine:
    def __init__(
        self,
        hippocampus: Hippocampus,
        amygdala: Amygdala,
        fact_store: Optional[FactStore] = None,
        diffusion_decay: float = 0.5,
        diffusion_max_hops: int = 3,
        diffuse_max_nodes: int = 200,
        top_k: int = 5,
        default_max_tokens: int = 2048,  # Default token budget
        summarize_fn: Optional[Callable[[List[dict], int], List[dict]]] = None,
    ):
        self.hippocampus = hippocampus
        self.amygdala = amygdala
        self.fact_store = fact_store
        self.diffusion_decay = diffusion_decay
        self.diffusion_max_hops = diffusion_max_hops
        self.diffuse_max_nodes = diffuse_max_nodes
        self.top_k = top_k
        self.default_max_tokens = default_max_tokens
        self.summarize_fn = summarize_fn

    def recall(
        self, query: str, top_k: int = 0, max_tokens: int = 0
    ) -> List[dict]:
        """Main recall interface.

        Args:
            query: query text / fragment
            top_k: returncount (defaultuse self.top_k) 
            max_tokens: token budget limit (0=use default_max_tokens, <0=no limit)

        Returns:
            [{"atom": ..., "salience": ..., "hops": ...,
              "compressed": bool, "original_count": int}, ...]
        """
        if top_k <= 0:
            top_k = self.top_k

        # resolve max_tokens
        budget = self.default_max_tokens if max_tokens == 0 else max_tokens

        # Step 1: Direct match
        direct = self._direct_match(query)
        if not direct:
            expanded = self._diffuse_from_seed(self._extract_seeds(query), top_k)
            return self._apply_budget(expanded, budget)

        # Step 2: diffusionactivation
        seed_ids = {a.atom_id for a, _ in direct}
        expanded = self._diffuse_activation(seed_ids)

        # Step 3: Deduplicate + filter archived
        seen: Set[str] = set()
        all_hits = list(direct)

        for atom, path_hops in expanded:
            if atom.atom_id not in seen and not atom.is_archived:
                seen.add(atom.atom_id)
                all_hits.append((atom, path_hops))

        # Step 4: Sort ⚠️ is_core should not crowd out working memory (unless query directly matches)
        scored = [
            {
                "atom": atom.to_dict(),
                "salience": round(self.amygdala.score(atom), 3),
                "hops": hops,
                "_is_core": atom.is_core,
            }
            for atom, hops in all_hits
            if not atom.is_archived
        ]
        # is_core in non-direct matches reduces weight to avoid ghost diffusion
        scored.sort(
            key=lambda x: x["salience"] * (
                0.3 if x["_is_core"]
                and not self._query_matches_core(query, x["atom"])
                else 1.0
            ),
            reverse=True,
        )
        scored = scored[:top_k]

        # Clear internal markers
        for r in scored:
            r.pop("_is_core", None)

        return self._apply_budget(scored, budget)

    def _apply_budget(
        self, results: List[dict], max_tokens: int
    ) -> List[dict]:
        """Apply token budget to recall results.

        If max_tokens < 0, skip compression (no limit).
        If within budget, return directly.
        if over budget: try user custom summarize_fn, otherwise fallback to taking N items.
        """
        if max_tokens < 0:
            return results

        total_tokens = sum(
            _estimate_tokens(r.get("atom", {}).get("content", ""))
            for r in results
        )

        if total_tokens <= max_tokens:
            return results

        original_count = len(results)
        logger.info(
            f"Token budget exceeded: {total_tokens} > {max_tokens}, "
            f"total {original_count} memories"
        )

        # try custom summary function
        if self.summarize_fn:
            try:
                compressed = self.summarize_fn(results, max_tokens)
                if compressed and isinstance(compressed, list):
                    for r in compressed:
                        r.setdefault("compressed", True)
                        r.setdefault("original_count", original_count)
                    return compressed
            except Exception as e:
                logger.error(f"summarize_fn failed: {e}")

        # native compress: take in descending order of salience to not exceed budget
        # keep at least 1 item (even if over budget)
        budget_results = []
        used = 0
        for r in results:
            content = r.get("atom", {}).get("content", "")
            tokens = _estimate_tokens(content)
            if used + tokens > max_tokens and len(budget_results) >= 1:
                break
            budget_results.append(r)
            used += tokens

        # mark compress
        for r in budget_results:
            r["compressed"] = True
            r["original_count"] = original_count
            r["total_budget"] = max_tokens
            r["used_tokens"] = used

        logger.info(
            f"compress done: {original_count} → {len(budget_results)} items, "
            f"used {used}/{max_tokens} tokens"
        )
        return budget_results

    def reconstruct(self, fragment: str, top_k: int = 0) -> List[dict]:
        """mode done: given fragments, reconstruct complete memory."""
        atoms = self.hippocampus.all()
        fragment_words = set(fragment.lower().split())
        if not fragment_words:
            return []

        scored = []
        for atom in atoms:
            content_words = set(atom.content.lower().split())
            overlap = len(fragment_words & content_words)
            if overlap > 0:
                ratio = overlap / max(1, len(fragment_words))
                salience = self.amygdala.score(atom)
                scored.append({
                    "atom": atom.to_dict(),
                    "salience": round(salience, 3),
                    "overlap_ratio": round(ratio, 3),
                    "score": round(ratio * salience, 3),
                })

        scored.sort(key=lambda x: -x["score"])
        if top_k <= 0:
            top_k = self.top_k
        return scored[:top_k]

    # ── internal: direct match ──

    def _direct_match(self, query: str) -> List[Tuple[MemoryAtom, int]]:
        query_lower = query.lower()
        query_words = set(query_lower.split())

        matches = []
        for atom in self.hippocampus.all():
            content_lower = atom.content.lower()
            if query_lower in content_lower:
                matches.append((atom, 0))
                continue
            content_words = set(content_lower.split())
            if query_words & content_words:
                matches.append((atom, 0))
                continue
            if any(e.lower() in query_lower for e in atom.entities):
                matches.append((atom, 0))

        return matches

    def _extract_seeds(self, query: str) -> Set[str]:
        seeds: Set[str] = set()
        query_lower = query.lower()
        for atom in self.hippocampus.all():
            for ent in atom.entities:
                if ent.lower() in query_lower:
                    seeds.add(atom.atom_id)
                    break
            if any(w in atom.content.lower() for w in query_lower.split()):
                seeds.add(atom.atom_id)
        return seeds

    def _query_matches_core(self, query: str, atom_dict: dict) -> bool:
        """check if query directly matches core memory content/entity.
        for determining whether to suppress is_core weight (no suppression if direct match)."""
        content = atom_dict.get("content", "").lower()
        if query.lower() in content:
            return True
        for ent in atom_dict.get("entities", []):
            if ent.lower() in query.lower():
                return True
        return False

    def _diffuse_from_seed(self, seed_ids: Set[str], top_k: int) -> List[dict]:
        if not seed_ids:
            return []
        expanded = self._diffuse_activation(seed_ids)
        scored = [
            {
                "atom": atom.to_dict(),
                "salience": round(self.amygdala.score(atom), 3),
                "hops": hops,
                "_is_core": atom.is_core,
            }
            for atom, hops in expanded
            if not atom.is_archived
        ]
        # is_core suppression same as recall(), prevent ghost diffusion
        scored.sort(
            key=lambda x: x["salience"] * (0.3 if x.pop("_is_core", False) else 1.0),
            reverse=True,
        )
        return scored[:top_k]

    # ── diffusionactivation ──

    def _diffuse_activation(
        self, seed_ids: Set[str]
    ) -> List[Tuple[MemoryAtom, int]]:
        atoms_by_id = {a.atom_id: a for a in self.hippocampus.all()}

        visited: Set[str] = set()
        queue: deque = deque()
        results: List[Tuple[MemoryAtom, int]] = []

        for sid in seed_ids:
            if sid in atoms_by_id:
                queue.append((sid, 0))
                visited.add(sid)

        while queue and len(visited) <= self.diffuse_max_nodes:
            current_id, hops = queue.popleft()
            current = atoms_by_id.get(current_id)
            if not current:
                continue
            if hops > 0:
                results.append((current, hops))
            if hops < self.diffusion_max_hops:
                for neighbor_id in current.links:
                    if neighbor_id not in visited:
                        visited.add(neighbor_id)
                        queue.append((neighbor_id, hops + 1))
                        if len(visited) >= self.diffuse_max_nodes:
                            break
        return results

    # ── Fact Store query ──

    def probe_entity(self, entity: str, limit: int = 20) -> List[MemoryAtom]:
        atoms = self.hippocampus.all()
        entity_lower = entity.lower()
        matched = []
        for atom in atoms:
            if any(entity_lower in e.lower() for e in atom.entities):
                matched.append(atom)
                continue
            if entity_lower in atom.content.lower():
                matched.append(atom)
        scored = sorted(matched, key=lambda a: -self.amygdala.score(a))
        return scored[:limit]

    def diffuse(self, seed_id: str, max_hops: int = 3) -> List[MemoryAtom]:
        atoms_by_id = {a.atom_id: a for a in self.hippocampus.all()}
        visited: Set[str] = set()
        queue: deque = deque()
        results: List[MemoryAtom] = []

        if seed_id not in atoms_by_id:
            return []
        queue.append((seed_id, 0))
        visited.add(seed_id)
        while queue:
            current_id, hops = queue.popleft()
            current = atoms_by_id.get(current_id)
            if not current:
                continue
            if hops > 0:
                results.append(current)
            if hops < max_hops:
                for neighbor_id in current.links:
                    if neighbor_id not in visited:
                        visited.add(neighbor_id)
                        queue.append((neighbor_id, hops + 1))
        return results

    def query_knowledge(self, entity: str) -> List[dict]:
        if not self.fact_store:
            return []
        return self.fact_store.probe(entity)

    def reason_knowledge(self, entities: List[str]) -> List[dict]:
        if not self.fact_store:
            return []
        return self.fact_store.reason(entities)
