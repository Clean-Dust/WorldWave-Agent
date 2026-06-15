"""Semantic codebase search tools — embeddings + vector search.

Registers tools: index_codebase, semantic_search, search_similar_files
"""

from __future__ import annotations
import os
from typing import Optional

from tools.registry import ToolRegistry, ToolDef
from core.codebase_index import get_codebase_index


def register_tools(registry: ToolRegistry):
    """Register semantic search tools."""

    _index = get_codebase_index()

    def handle_index_codebase(path: str = ".", **kwargs) -> dict:
        """Index a codebase for semantic search."""
        try:
            path = os.path.expanduser(path)
            if not os.path.isdir(path):
                return {"error": f"Not a directory: {path}"}
            stats = _index.index(path)
            return {
                "indexed": True,
                "path": path,
                "files": stats["files"],
                "chunks": stats["chunks"],
                "lines": stats["lines"],
                "errors": stats.get("errors", 0),
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_semantic_search(
        query: str,
        path: str = ".",
        top_k: int = 10,
        file_filter: str = "",
        language_filter: str = "",
        **kwargs,
    ) -> dict:
        """Semantic search across indexed codebase."""
        try:
            # Auto-index if not indexed
            stats = _index.get_stats()
            if stats.get("chunk_count", 0) == 0:
                index_stats = _index.index(os.path.expanduser(path))
                if index_stats["files"] == 0:
                    return {"error": f"No code files found in {path}"}

            results = _index.search(
                query,
                top_k=top_k,
                file_filter=file_filter or None,
                language_filter=language_filter or None,
            )
            return {
                "query": query,
                "results": [
                    {
                        "file": r["file_path"],
                        "lines": f"{r['start_line']}-{r['end_line']}",
                        "content": r["content"][:500],
                        "score": r["score"],
                        "language": r.get("language", ""),
                    }
                    for r in results
                ],
                "total": len(results),
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_search_similar_files(query: str, path: str = ".", top_k: int = 5, **kwargs) -> dict:
        """Find files most similar to a query."""
        try:
            stats = _index.get_stats()
            if stats.get("chunk_count", 0) == 0:
                _index.index(os.path.expanduser(path))

            results = _index.search_files(query, top_k=top_k)
            return {
                "query": query,
                "files": [
                    {
                        "file": r["file_path"],
                        "score": r["score"],
                        "language": r["language"],
                        "best_match_lines": f"L{r['best_chunk']['start_line']}-L{r['best_chunk']['end_line']}",
                        "best_match": r["best_chunk"]["content"][:300],
                    }
                    for r in results
                ],
                "total": len(results),
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_codebase_stats(**kwargs) -> dict:
        """Get codebase index statistics."""
        stats = _index.get_stats()
        return dict(stats)

    registry.register(ToolDef(
        name="index_codebase",
        description="Index a codebase directory for semantic search. Run once before using semantic_search.",
        handler=handle_index_codebase,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root directory to index", "default": "."},
            },
            "required": [],
        },
        category="code_search",
    ))

    registry.register(ToolDef(
        name="semantic_search",
        description="Semantic code search — find code by meaning, not just text. Example: 'auth logic', 'file upload handler'.",
        handler=handle_semantic_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query describing what you're looking for."},
                "path": {"type": "string", "description": "Codebase root (auto-indexes if needed).", "default": "."},
                "top_k": {"type": "integer", "description": "Max results.", "default": 10},
                "file_filter": {"type": "string", "description": "Optional file path substring filter.", "default": ""},
                "language_filter": {"type": "string", "description": "Optional language filter (python, javascript, etc).", "default": ""},
            },
            "required": ["query"],
        },
        category="code_search",
    ))

    registry.register(ToolDef(
        name="search_similar_files",
        description="Find files most semantically similar to a query. Returns file-level results with best match excerpt.",
        handler=handle_search_similar_files,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query."},
                "path": {"type": "string", "description": "Codebase root.", "default": "."},
                "top_k": {"type": "integer", "description": "Max files.", "default": 5},
            },
            "required": ["query"],
        },
        category="code_search",
    ))

    registry.register(ToolDef(
        name="codebase_stats",
        description="Get codebase index statistics (files, chunks, lines indexed).",
        handler=handle_codebase_stats,
        parameters={"type": "object", "properties": {}, "required": []},
        category="code_search",
    ))
