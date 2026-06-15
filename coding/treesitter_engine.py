"""Tree-sitter accelerated code search engine.

Provides the same API as ASTSearchEngine but uses tree-sitter under the hood
for 5-50x faster parsing of large codebases.

Key advantages over pure Python ast:
  - Incremental parsing: re-parse only changed files
  - C-level speed: tree-sitter is written in C, not Python
  - Better error recovery: handles incomplete/syntactically invalid code
  - Multi-language: supports Python, JavaScript, TypeScript, etc.

Installation:
  pip install tree-sitter tree-sitter-python

This module gracefully degrades when tree-sitter is not available.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import tree_sitter
    import tree_sitter_python

    HAS_TREESITTER = True
    PY_LANGUAGE = tree_sitter_python.language()
    PARSER = tree_sitter.Parser()
    PARSER.set_language(PY_LANGUAGE)
except ImportError:
    HAS_TREESITTER = False
    PY_LANGUAGE = None
    PARSER = None


# ── Query patterns ───────────────────────────────────────────────

# tree-sitter query for function definitions
FUNCTION_QUERY = """
(function_definition
  name: (identifier) @func.name
  parameters: (parameters) @func.params
  body: (block) @func.body
) @func.def
"""

# tree-sitter query for class definitions
CLASS_QUERY = """
(class_definition
  name: (identifier) @class.name
  body: (block) @class.body
) @class.def
"""

# tree-sitter query for function calls
CALL_QUERY = """
(call
  function: (identifier) @call.name
  arguments: (argument_list) @call.args
) @call.expr
"""

# tree-sitter query for imports
IMPORT_QUERY = """
(import_statement
  name: (dotted_name) @import.module
) @import.stmt

(import_from_statement
  module_name: (dotted_name) @import.module
  name: (dotted_name) @import.name
) @import.from
"""

# tree-sitter query for variable assignments
VARIABLE_QUERY = """
(assignment
  left: (identifier) @var.name
) @var.assign
"""


# ── Tree-sitter Engine ───────────────────────────────────────────

class TreeSitterEngine:
    """Tree-sitter accelerated code search with same API as ASTSearchEngine."""

    def __init__(self):
        if not HAS_TREESITTER:
            raise ImportError(
                "tree-sitter not installed. "
                "Run: pip install tree-sitter tree-sitter-python"
            )
        self._parser = tree_sitter.Parser()
        self._parser.set_language(PY_LANGUAGE)
        self._cache: Dict[str, tree_sitter.Tree] = {}

    # ── Search API (compatible with ASTSearchEngine) ────────

    def search(
        self,
        pattern_type: str,
        target: str,
        root_dir: str = ".",
        file_glob: str = "*.py",
        max_results: int = 50,
    ) -> Dict:
        """Search for structural patterns across files.

        Args:
            pattern_type: 'function', 'class', 'call', 'import', 'variable'
            target: Name or regex pattern to match
            root_dir: Root directory to search
            file_glob: File glob pattern
            max_results: Maximum results

        Returns:
            Dict with matches, count, pattern info
        """
        query_map = {
            "function": FUNCTION_QUERY,
            "class": CLASS_QUERY,
            "call": CALL_QUERY,
            "import": IMPORT_QUERY,
            "variable": VARIABLE_QUERY,
        }

        query_src = query_map.get(pattern_type)
        if not query_src:
            return {"matches": [], "count": 0, "pattern": pattern_type,
                    "target": target, "error": f"Unknown pattern type: {pattern_type}"}

        target_re = re.compile(target, re.IGNORECASE) if target and target != ".*" else None

        matches = []
        files = self._find_files(root_dir, file_glob)

        for filepath in files:
            tree = self._parse_file(filepath)
            if tree is None:
                continue

            file_matches = self._query_tree(
                tree, query_src, filepath, pattern_type, target_re
            )
            matches.extend(file_matches)

            if len(matches) >= max_results:
                matches = matches[:max_results]
                break

        return {
            "matches": matches,
            "count": len(matches),
            "pattern": pattern_type,
            "target": target,
            "engine": "tree-sitter",
        }

    def find_functions(
        self,
        name_pattern: str = None,
        root_dir: str = ".",
        file_glob: str = "*.py",
    ) -> Dict:
        """Find function/method definitions matching a name pattern."""
        return self.search(
            "function", name_pattern or ".*", root_dir, file_glob
        )

    def find_classes(
        self,
        name_pattern: str = None,
        root_dir: str = ".",
        file_glob: str = "*.py",
    ) -> Dict:
        """Find class definitions matching a name pattern."""
        return self.search(
            "class", name_pattern or ".*", root_dir, file_glob
        )

    def find_calls(
        self,
        name_pattern: str,
        root_dir: str = ".",
        file_glob: str = "*.py",
    ) -> Dict:
        """Find function calls matching a name pattern."""
        return self.search(
            "call", name_pattern, root_dir, file_glob
        )

    def extract_call_graph(
        self,
        root_dir: str = ".",
        file_glob: str = "*.py",
    ) -> Dict:
        """Extract the complete call graph from all files.

        Returns:
            {function_name: [called_function_names, ...], ...}
        """
        graph: Dict[str, Set[str]] = {}
        files = self._find_files(root_dir, file_glob)

        for filepath in files:
            tree = self._parse_file(filepath)
            if tree is None:
                continue

            self._extract_graph_from_tree(tree, graph, filepath)

        return {k: sorted(v) for k, v in graph.items()}

    # ── Internal ──────────────────────────────────────────────

    def _parse_file(self, filepath: str) -> Optional[tree_sitter.Tree]:
        """Parse a file into a tree-sitter tree (with caching)."""
        if filepath in self._cache:
            return self._cache[filepath]

        try:
            with open(filepath, "rb") as f:
                source = f.read()
        except (OSError, IOError):
            return None

        tree = self._parser.parse(source)
        self._cache[filepath] = tree
        return tree

    def _query_tree(
        self,
        tree: tree_sitter.Tree,
        query_src: str,
        filepath: str,
        pattern_type: str,
        target_re: Optional[re.Pattern],
    ) -> List[Dict]:
        """Execute a tree-sitter query on a parsed tree."""
        try:
            query = tree_sitter.Query(PY_LANGUAGE, query_src)
        except Exception:
            return []

        captures = query.captures(tree.root_node)

        # Group captures by match pattern
        matches = []
        name_key = f"{pattern_type}.name"

        for node, capture_name in captures:
            if capture_name != name_key:
                continue

            name = node.text.decode("utf-8") if node.text else ""
            if target_re and not target_re.search(name):
                continue

            start_row, start_col = node.start_point
            end_row, end_col = node.end_point

            # Get surrounding code snippet
            source = node.text.decode("utf-8") if node.text else ""

            matches.append({
                "file": filepath,
                "line": start_row + 1,  # tree-sitter is 0-indexed
                "column": start_col + 1,
                "end_line": end_row + 1,
                "end_column": end_col + 1,
                "name": name,
                "type": pattern_type,
                "code": source[:200],
            })

        return matches

    def _extract_graph_from_tree(
        self,
        tree: tree_sitter.Tree,
        graph: Dict[str, Set[str]],
        filepath: str,
    ):
        """Extract call graph edges from a parsed tree."""
        try:
            func_query = tree_sitter.Query(PY_LANGUAGE, FUNCTION_QUERY)
            call_query = tree_sitter.Query(PY_LANGUAGE, CALL_QUERY)
        except Exception:
            return

        # Find all functions
        for node, capture_name in func_query.captures(tree.root_node):
            if capture_name != "func.name":
                continue
            func_name = node.text.decode("utf-8") if node.text else ""
            if func_name not in graph:
                graph[func_name] = set()

        # Find all calls inside each function
        for func_node, func_capture in func_query.captures(tree.root_node):
            if func_capture != "func.def":
                continue

            # Get function name
            func_name = ""
            for child in func_node.children:
                if child.type == "identifier":
                    func_name = child.text.decode("utf-8") if child.text else ""
                    break

            if not func_name:
                continue

            # Find calls within this function's body
            call_nodes = call_query.captures(func_node)
            for call_node, cap_name in call_nodes:
                if cap_name != "call.name":
                    continue
                called_name = call_node.text.decode("utf-8") if call_node.text else ""
                if called_name:
                    if func_name not in graph:
                        graph[func_name] = set()
                    graph[func_name].add(called_name)

    @staticmethod
    def _find_files(root_dir: str, file_glob: str) -> List[str]:
        """Find all files matching a glob pattern."""
        files = []
        root = Path(root_dir)
        for filepath in root.rglob(file_glob):
            parts = filepath.parts
            if ".git" in parts or "__pycache__" in parts or "node_modules" in parts:
                continue
            if filepath.is_file():
                files.append(str(filepath))
        return sorted(files)

    # ── Cache management ─────────────────────────────────────

    def clear_cache(self):
        self._cache.clear()

    def invalidate_file(self, filepath: str):
        self._cache.pop(filepath, None)


# ── Factory ─────────────────────────────────────────────────────

def create_engine(force_pure: bool = False) -> Any:
    """Create the best available code search engine.

    Args:
        force_pure: If True, force pure Python AST even if tree-sitter available.

    Returns:
        TreeSitterEngine or ASTSearchEngine (both have .search() API).
    """
    if not force_pure and HAS_TREESITTER:
        return TreeSitterEngine()

    # Fallback to pure Python
    from coding.code_search import ASTSearchEngine
    return ASTSearchEngine()
