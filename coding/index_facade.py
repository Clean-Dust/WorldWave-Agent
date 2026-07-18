"""coding/index_facade.py — Unified indexing facade (PM 0.12).

Single entrypoint for perception over a project:

  build(project_root)   — graph + BM25/code_rag lifecycle under .ww/
  update(paths)         — incremental refresh for changed files
  query(kind, ...)      — map | grep | graph | outline/symbol | rag

Counters (map/grep/graph/rag/symbol) are exported for CodingMetrics / arena.
Same .ww lifecycle as code_graph + code_rag (no third-party vendoring).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class IndexCounters:
    map_calls: int = 0
    grep_calls: int = 0
    graph_calls: int = 0
    rag_calls: int = 0
    symbol_calls: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "map_calls": self.map_calls,
            "grep_calls": self.grep_calls,
            "graph_calls": self.graph_calls,
            "rag_calls": self.rag_calls,
            "symbol_calls": self.symbol_calls,
        }


@dataclass
class IndexFacade:
    """Unified index over repo_map, code_graph, outline/symbols, BM25 code_rag."""

    project_root: str = "."
    counters: IndexCounters = field(default_factory=IndexCounters)
    _built: bool = False
    _rag: Any = None
    _graph: Any = None

    def __post_init__(self) -> None:
        self.project_root = os.path.abspath(self.project_root or ".")

    # ── lifecycle ─────────────────────────────────────────────────────

    def build(self, project_root: str = None, force: bool = False) -> Dict[str, Any]:
        """Build / refresh graph + RAG indexes under <root>/.ww/."""
        if project_root:
            self.project_root = os.path.abspath(project_root)
        root = self.project_root
        out: Dict[str, Any] = {"project_root": root, "success": True}

        # Code graph
        try:
            from coding.code_graph import CodeGraphStore

            store = CodeGraphStore(project_root=root)
            build_r = store.build(root, force=force)
            self._graph = store
            self.counters.graph_calls += 1
            out["graph"] = {
                "success": True,
                "result": build_r if isinstance(build_r, dict) else {"raw": str(build_r)[:200]},
                "stats": store.stats() if hasattr(store, "stats") else {},
            }
        except Exception as e:
            out["graph"] = {"success": False, "error": str(e)}
            out["success"] = False

        # BM25 / code_rag
        try:
            from coding.code_rag import CodeRAGEngine

            rag = CodeRAGEngine(root_dir=root)
            rag_r = rag.build_index(["*.py"])
            self._rag = rag
            self.counters.rag_calls += 1
            out["rag"] = {"success": True, "result": rag_r}
        except Exception as e:
            out["rag"] = {"success": False, "error": str(e)}

        # Ensure .ww exists (lifecycle doc surface)
        ww = os.path.join(root, ".ww")
        try:
            os.makedirs(ww, exist_ok=True)
            marker = os.path.join(ww, "index_facade.json")
            import json

            with open(marker, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "project_root": root,
                        "counters": self.counters.to_dict(),
                        "graph": out.get("graph", {}).get("success"),
                        "rag": out.get("rag", {}).get("success"),
                    },
                    f,
                    indent=2,
                )
            out["ww_dir"] = ww
            out["marker"] = marker
        except OSError as e:
            out["ww_error"] = str(e)

        self._built = True
        out["counters"] = self.counters.to_dict()
        return out

    def update(self, paths: Optional[List[str]] = None) -> Dict[str, Any]:
        """Incremental refresh for *paths* (or full rebuild if empty)."""
        paths = list(paths or [])
        if not paths:
            return self.build(force=False)

        out: Dict[str, Any] = {"updated": [], "errors": []}
        root = self.project_root

        # Graph: rebuild is cheapest correct path for medium trees
        try:
            from coding.code_graph import CodeGraphStore

            store = self._graph or CodeGraphStore(project_root=root)
            store.build(root, force=False)
            self._graph = store
            self.counters.graph_calls += 1
            out["graph"] = {"success": True, "stats": store.stats()}
        except Exception as e:
            out["errors"].append(f"graph: {e}")

        # RAG: re-index changed files via full build_index (merkle-aware)
        try:
            from coding.code_rag import CodeRAGEngine

            rag = self._rag or CodeRAGEngine(root_dir=root)
            rag_r = rag.build_index(["*.py"])
            self._rag = rag
            self.counters.rag_calls += 1
            out["rag"] = rag_r
            out["updated"] = paths
        except Exception as e:
            out["errors"].append(f"rag: {e}")

        out["success"] = not out["errors"]
        out["counters"] = self.counters.to_dict()
        return out

    # ── queries ───────────────────────────────────────────────────────

    def query(self, kind: str, **kwargs: Any) -> Dict[str, Any]:
        """Query unified index.

        kind:
          map | repo_map     — signature map (token budget)
          grep              — text search
          graph             — graph ops: action=build|who_calls|blast|hubs|stats
          outline | symbol  — file symbol outline
          rag | search      — BM25/code_rag search
        """
        k = (kind or "").strip().lower()
        if k in ("map", "repo_map"):
            return self._q_map(**kwargs)
        if k == "grep":
            return self._q_grep(**kwargs)
        if k == "graph":
            return self._q_graph(**kwargs)
        if k in ("outline", "symbol", "symbols"):
            return self._q_outline(**kwargs)
        if k in ("rag", "search", "bm25"):
            return self._q_rag(**kwargs)
        return {"success": False, "error": f"unknown query kind: {kind}"}

    def _q_map(self, token_budget: int = 4000, force_graph: bool = True, **_: Any) -> Dict[str, Any]:
        from coding.perception import repo_map

        self.counters.map_calls += 1
        r = repo_map(self.project_root, token_budget=token_budget, force_graph=force_graph)
        # repo_map may touch graph; count that as graph activity when force_graph
        if force_graph:
            self.counters.graph_calls += 1
        return {"success": True, "kind": "map", "result": r, "counters": self.counters.to_dict()}

    def _q_grep(
        self,
        pattern: str = "",
        path: str = None,
        glob: str = "*.py",
        max_matches: int = 50,
        **_: Any,
    ) -> Dict[str, Any]:
        from coding.perception import grep

        self.counters.grep_calls += 1
        r = grep(
            pattern or "",
            path=path or self.project_root,
            glob=glob,
            max_matches=max_matches,
        )
        return {"success": True, "kind": "grep", "result": r, "counters": self.counters.to_dict()}

    def _q_graph(
        self,
        action: str = "stats",
        target: str = None,
        max_depth: int = 3,
        force: bool = False,
        **_: Any,
    ) -> Dict[str, Any]:
        from coding.code_graph import CodeGraphStore

        self.counters.graph_calls += 1
        store = self._graph or CodeGraphStore(project_root=self.project_root)
        action = (action or "stats").lower()
        if action == "build" or (store.stats().get("nodes", 0) == 0 and action != "stats"):
            store.build(self.project_root, force=force)
            self._graph = store
        if action == "build":
            return {
                "success": True,
                "kind": "graph",
                "action": "build",
                "stats": store.stats(),
                "counters": self.counters.to_dict(),
            }
        if action in ("who_calls", "callers"):
            r = store.who_calls(target or "")
            return {"success": True, "kind": "graph", "action": "who_calls", "result": r,
                    "counters": self.counters.to_dict()}
        if action in ("blast", "blast_radius"):
            r = store.blast_radius(target or "", max_depth=max_depth)
            return {"success": True, "kind": "graph", "action": "blast_radius", "result": r,
                    "counters": self.counters.to_dict()}
        if action == "hubs":
            r = store.hubs() if hasattr(store, "hubs") else store.stats()
            return {"success": True, "kind": "graph", "action": "hubs", "result": r,
                    "counters": self.counters.to_dict()}
        # default stats
        return {
            "success": True,
            "kind": "graph",
            "action": "stats",
            "stats": store.stats(),
            "counters": self.counters.to_dict(),
        }

    def _q_outline(self, path: str = "", **_: Any) -> Dict[str, Any]:
        from coding.perception import outline

        self.counters.symbol_calls += 1
        if not path:
            return {"success": False, "error": "path required for outline", "kind": "outline"}
        # Resolve relative to project root
        p = path
        if not os.path.isabs(p):
            p = os.path.join(self.project_root, p)
        r = outline(p)
        return {"success": "error" not in r, "kind": "outline", "result": r,
                "counters": self.counters.to_dict()}

    def _q_rag(self, query: str = "", top_k: int = 10, hybrid: bool = False, **_: Any) -> Dict[str, Any]:
        from coding.code_rag import CodeRAGEngine

        self.counters.rag_calls += 1
        rag = self._rag or CodeRAGEngine(root_dir=self.project_root)
        if not getattr(rag, "_chunks", None):
            try:
                rag.build_index(["*.py"])
            except Exception:
                pass
        self._rag = rag
        r = rag.search(query or "", top_k=top_k, hybrid=hybrid)
        return {"success": True, "kind": "rag", "result": r, "counters": self.counters.to_dict()}

    def metrics(self) -> Dict[str, int]:
        return self.counters.to_dict()

    def close(self) -> None:
        store = self._graph
        if store is not None and hasattr(store, "close"):
            try:
                store.close()
            except Exception:
                pass
        self._graph = None


def build(project_root: str = ".", force: bool = False) -> Dict[str, Any]:
    """Module-level convenience: build indexes and return facade metrics."""
    fac = IndexFacade(project_root=project_root)
    result = fac.build(force=force)
    result["facade"] = fac
    return result


def get_facade(project_root: str = ".") -> IndexFacade:
    return IndexFacade(project_root=project_root)
