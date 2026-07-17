"""coding/code_graph.py — Directed multigraph of code structure.

Nodes: file / class / function
Edges: calls, imports, inherits, defines

Python via stdlib ast always. SQLite store at <project>/.ww/code_graph.db
with mtime + content-hash incremental updates.
"""

from __future__ import annotations

import ast
import hashlib
import os
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ── Helpers ───────────────────────────────────────────────────────────

SKIP_DIRS = {
    ".git", "__pycache__", ".ww", "node_modules", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    "worldwave.egg-info", ".eggs",
}


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _node_id(kind: str, qualname: str, file: str = "") -> str:
    if kind == "file":
        return f"file:{qualname}"
    return f"{kind}:{qualname}@{file}"


def _find_project_root(start: str = None) -> str:
    path = os.path.abspath(start or os.getcwd())
    for _ in range(20):
        if os.path.isdir(os.path.join(path, ".git")) or os.path.isdir(os.path.join(path, ".ww")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return os.path.abspath(start or os.getcwd())


# ── AST extraction ────────────────────────────────────────────────────

class _FileExtractor(ast.NodeVisitor):
    """Extract symbols and relations from one Python module."""

    def __init__(self, filepath: str, module_name: str):
        self.filepath = filepath
        self.module_name = module_name
        self.nodes: List[Dict] = []
        self.edges: List[Dict] = []
        self._stack: List[str] = []  # qualified name stack
        self._class_stack: List[str] = []

    def visit_ClassDef(self, node: ast.ClassDef):
        qname = ".".join(self._stack + [node.name]) if self._stack else node.name
        nid = _node_id("class", qname, self.filepath)
        self.nodes.append({
            "id": nid,
            "kind": "class",
            "name": node.name,
            "qualname": qname,
            "file": self.filepath,
            "lineno": node.lineno,
        })
        self.edges.append({
            "src": _node_id("file", self.filepath),
            "dst": nid,
            "kind": "defines",
        })
        for base in node.bases:
            bname = self._name_of(base)
            if bname:
                self.edges.append({
                    "src": nid,
                    "dst": _node_id("class", bname, ""),  # may resolve later
                    "kind": "inherits",
                    "raw_target": bname,
                })
        self._stack.append(node.name)
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_func(node)

    def _visit_func(self, node):
        qname = ".".join(self._stack + [node.name]) if self._stack else node.name
        nid = _node_id("function", qname, self.filepath)
        self.nodes.append({
            "id": nid,
            "kind": "function",
            "name": node.name,
            "qualname": qname,
            "file": self.filepath,
            "lineno": node.lineno,
        })
        self.edges.append({
            "src": _node_id("file", self.filepath),
            "dst": nid,
            "kind": "defines",
        })
        # calls inside body
        caller = nid
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                cname = self._call_name(child)
                if cname:
                    self.edges.append({
                        "src": caller,
                        "dst": _node_id("function", cname, ""),
                        "kind": "calls",
                        "raw_target": cname,
                    })
        self._stack.append(node.name)
        # only recurse into nested defs via generic — but we already walked for calls
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(child)
        self._stack.pop()

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.edges.append({
                "src": _node_id("file", self.filepath),
                "dst": _node_id("file", alias.name.replace(".", "/") + ".py"),
                "kind": "imports",
                "raw_target": alias.name,
            })

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        self.edges.append({
            "src": _node_id("file", self.filepath),
            "dst": _node_id("file", mod.replace(".", "/") + ".py") if mod else _node_id("file", "?"),
            "kind": "imports",
            "raw_target": mod,
        })

    def _name_of(self, node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._name_of(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    def _call_name(self, node: ast.Call) -> Optional[str]:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            # Use attribute name for matching who_calls (leaf function name)
            return node.func.attr
        return None


def extract_file(filepath: str, project_root: str) -> Tuple[List[Dict], List[Dict]]:
    """Parse one Python file → (nodes, edges). Always includes a file node."""
    abs_path = os.path.abspath(filepath)
    try:
        rel = os.path.relpath(abs_path, project_root)
    except ValueError:
        rel = abs_path

    file_node = {
        "id": _node_id("file", rel),
        "kind": "file",
        "name": os.path.basename(rel),
        "qualname": rel,
        "file": rel,
        "lineno": 0,
    }
    nodes = [file_node]
    edges: List[Dict] = []

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=abs_path)
    except (SyntaxError, OSError):
        return nodes, edges

    mod_name = rel.replace(os.sep, ".").removesuffix(".py")
    ext = _FileExtractor(rel, mod_name)
    ext.visit(tree)
    # Fix file node ids in edges to use rel path
    for e in ext.edges:
        if e["src"].startswith("file:") and e["kind"] != "imports":
            e["src"] = file_node["id"]
        if e.get("src") == _node_id("file", abs_path):
            e["src"] = file_node["id"]
    # Rewrite defines edges
    fixed_edges = []
    for e in ext.edges:
        e = dict(e)
        if e["kind"] == "defines":
            e["src"] = file_node["id"]
        if e["kind"] == "imports":
            e["src"] = file_node["id"]
        # Normalize function node file field already set
        fixed_edges.append(e)
    nodes.extend(ext.nodes)
    edges.extend(fixed_edges)
    return nodes, edges


# ── SQLite store ──────────────────────────────────────────────────────

class CodeGraphStore:
    """SQLite-backed directed multigraph with incremental file updates."""

    def __init__(self, project_root: str = None, db_path: str = None):
        self.project_root = os.path.abspath(project_root or _find_project_root())
        ww_dir = os.path.join(self.project_root, ".ww")
        os.makedirs(ww_dir, exist_ok=True)
        self.db_path = db_path or os.path.join(ww_dir, "code_graph.db")
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        # In-memory resolution caches after load
        self._name_index: Dict[str, List[str]] = defaultdict(list)  # bare name -> node ids

    def _init_schema(self):
        c = self._conn.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            mtime REAL,
            content_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT,
            qualname TEXT,
            file TEXT,
            lineno INTEGER
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            kind TEXT NOT NULL,
            raw_target TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
        CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
        CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
        CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
        CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
        """)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def build(self, root_dir: str = None, force: bool = False) -> Dict:
        """Scan Python files and incrementally update the graph."""
        root = os.path.abspath(root_dir or self.project_root)
        py_files = self._iter_py_files(root)
        updated = 0
        skipped = 0
        errors = []

        for fpath in py_files:
            try:
                st = os.stat(fpath)
                mtime = st.st_mtime
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                chash = _content_hash(content)
                rel = os.path.relpath(fpath, self.project_root)

                row = self._conn.execute(
                    "SELECT mtime, content_hash FROM files WHERE path=?", (rel,)
                ).fetchone()
                if not force and row and row["content_hash"] == chash:
                    skipped += 1
                    continue

                # Remove old nodes/edges for this file
                self._purge_file(rel)
                nodes, edges = extract_file(fpath, self.project_root)
                for n in nodes:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO nodes(id,kind,name,qualname,file,lineno) VALUES(?,?,?,?,?,?)",
                        (n["id"], n["kind"], n["name"], n["qualname"], n["file"], n.get("lineno") or 0),
                    )
                for e in edges:
                    self._conn.execute(
                        "INSERT INTO edges(src,dst,kind,raw_target) VALUES(?,?,?,?)",
                        (e["src"], e["dst"], e["kind"], e.get("raw_target")),
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO files(path,mtime,content_hash) VALUES(?,?,?)",
                    (rel, mtime, chash),
                )
                updated += 1
            except OSError as exc:
                errors.append(f"{fpath}: {exc}")

        # Resolve raw_target edges to real node ids where possible
        self._resolve_edges()
        self._conn.commit()
        self._rebuild_name_index()
        stats = self.stats()
        return {
            "success": True,
            "project_root": self.project_root,
            "files_scanned": len(py_files),
            "files_updated": updated,
            "files_skipped": skipped,
            "errors": errors[:20],
            **stats,
        }

    def _purge_file(self, rel: str):
        # Delete nodes belonging to this file and edges involving them
        node_ids = [
            r["id"] for r in self._conn.execute(
                "SELECT id FROM nodes WHERE file=?", (rel,)
            )
        ]
        file_id = _node_id("file", rel)
        if file_id not in node_ids:
            node_ids.append(file_id)
        if node_ids:
            placeholders = ",".join("?" * len(node_ids))
            self._conn.execute(
                f"DELETE FROM edges WHERE src IN ({placeholders}) OR dst IN ({placeholders})",
                node_ids + node_ids,
            )
            self._conn.execute(
                f"DELETE FROM nodes WHERE id IN ({placeholders})",
                node_ids,
            )
        self._conn.execute("DELETE FROM files WHERE path=?", (rel,))

    def _resolve_edges(self):
        """Point edges with raw_target at concrete nodes by bare name when unique."""
        name_map: Dict[str, List[str]] = defaultdict(list)
        for row in self._conn.execute("SELECT id, name, kind FROM nodes"):
            if row["name"]:
                name_map[row["name"]].append(row["id"])

        rows = list(self._conn.execute(
            "SELECT id, src, dst, kind, raw_target FROM edges WHERE raw_target IS NOT NULL"
        ))
        for row in rows:
            target = row["raw_target"]
            if not target:
                continue
            bare = target.split(".")[-1]
            candidates = name_map.get(bare) or name_map.get(target) or []
            if len(candidates) == 1:
                new_dst = candidates[0]
                if new_dst != row["dst"]:
                    self._conn.execute(
                        "UPDATE edges SET dst=? WHERE id=?",
                        (new_dst, row["id"]),
                    )
            elif len(candidates) > 1:
                # Prefer same-project function/class over unresolved placeholder
                # Keep first function match if kinds match edge
                preferred = candidates[0]
                if row["kind"] == "calls":
                    for c in candidates:
                        if c.startswith("function:"):
                            preferred = c
                            break
                elif row["kind"] == "inherits":
                    for c in candidates:
                        if c.startswith("class:"):
                            preferred = c
                            break
                self._conn.execute(
                    "UPDATE edges SET dst=? WHERE id=?",
                    (preferred, row["id"]),
                )

    def _rebuild_name_index(self):
        self._name_index = defaultdict(list)
        for row in self._conn.execute("SELECT id, name FROM nodes"):
            if row["name"]:
                self._name_index[row["name"]].append(row["id"])

    def _iter_py_files(self, root: str) -> List[str]:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if fn.endswith(".py"):
                    files.append(os.path.join(dirpath, fn))
        return sorted(files)

    def _resolve_symbol(self, symbol: str) -> List[str]:
        """Resolve a symbol name (bare, qualname, or node id) to node ids."""
        if not symbol:
            return []
        # Exact id
        row = self._conn.execute("SELECT id FROM nodes WHERE id=?", (symbol,)).fetchone()
        if row:
            return [row["id"]]
        # qualname
        rows = list(self._conn.execute("SELECT id FROM nodes WHERE qualname=?", (symbol,)))
        if rows:
            return [r["id"] for r in rows]
        # bare name
        rows = list(self._conn.execute("SELECT id FROM nodes WHERE name=?", (symbol,)))
        if rows:
            return [r["id"] for r in rows]
        # suffix match on qualname
        rows = list(self._conn.execute(
            "SELECT id FROM nodes WHERE qualname LIKE ?", (f"%.{symbol}",)
        ))
        return [r["id"] for r in rows]

    def who_calls(self, symbol: str) -> Dict:
        """Find callers of *symbol* (function/class name)."""
        targets = self._resolve_symbol(symbol)
        if not targets:
            # Also search raw_target
            edges = list(self._conn.execute(
                "SELECT e.src, e.dst, e.kind, e.raw_target, n.name, n.qualname, n.file, n.kind as nkind "
                "FROM edges e LEFT JOIN nodes n ON n.id=e.src "
                "WHERE e.kind='calls' AND (e.raw_target=? OR e.raw_target LIKE ? OR e.dst LIKE ?)",
                (symbol, f"%.{symbol}", f"%{symbol}%"),
            ))
        else:
            edges = []
            for tid in targets:
                edges.extend(self._conn.execute(
                    "SELECT e.src, e.dst, e.kind, e.raw_target, n.name, n.qualname, n.file, n.kind as nkind "
                    "FROM edges e LEFT JOIN nodes n ON n.id=e.src "
                    "WHERE e.kind='calls' AND e.dst=?",
                    (tid,),
                ))
            # Also raw_target matches for unresolved
            edges.extend(self._conn.execute(
                "SELECT e.src, e.dst, e.kind, e.raw_target, n.name, n.qualname, n.file, n.kind as nkind "
                "FROM edges e LEFT JOIN nodes n ON n.id=e.src "
                "WHERE e.kind='calls' AND e.raw_target=?",
                (symbol,),
            ))

        callers = []
        seen = set()
        for e in edges:
            key = (e["src"], e["dst"])
            if key in seen:
                continue
            seen.add(key)
            callers.append({
                "caller_id": e["src"],
                "caller_name": e["name"],
                "caller_qualname": e["qualname"],
                "caller_file": e["file"],
                "caller_kind": e["nkind"],
                "edge_kind": e["kind"],
                "raw_target": e["raw_target"],
            })
        return {
            "symbol": symbol,
            "targets": targets,
            "callers": callers,
            "count": len(callers),
        }

    def blast_radius(self, symbol: str, max_depth: int = 5) -> Dict:
        """Downstream nodes reachable via calls/defines/imports from *symbol*."""
        seeds = self._resolve_symbol(symbol)
        if not seeds:
            return {"symbol": symbol, "error": f"Symbol not found: {symbol}", "downstream": [], "count": 0}

        # For hub files, also include defined functions as seeds
        expanded = list(seeds)
        for sid in seeds:
            for row in self._conn.execute(
                "SELECT dst FROM edges WHERE src=? AND kind IN ('defines','calls','imports')",
                (sid,),
            ):
                if row["dst"] not in expanded:
                    expanded.append(row["dst"])

        visited: Set[str] = set()
        downstream: List[Dict] = []
        q: deque = deque([(s, 0) for s in expanded])
        seed_set = set(seeds)

        while q:
            nid, depth = q.popleft()
            if nid in visited or depth > max_depth:
                continue
            visited.add(nid)
            if nid not in seed_set:
                row = self._conn.execute(
                    "SELECT id, kind, name, qualname, file, lineno FROM nodes WHERE id=?",
                    (nid,),
                ).fetchone()
                if row:
                    downstream.append({
                        "id": row["id"],
                        "kind": row["kind"],
                        "name": row["name"],
                        "qualname": row["qualname"],
                        "file": row["file"],
                        "depth": depth,
                    })
            if depth >= max_depth:
                continue
            for edge in self._conn.execute(
                "SELECT dst, kind FROM edges WHERE src=? AND kind IN ('calls','defines','imports','inherits')",
                (nid,),
            ):
                if edge["dst"] not in visited:
                    q.append((edge["dst"], depth + 1))

        return {
            "symbol": symbol,
            "seeds": seeds,
            "downstream": downstream,
            "count": len(downstream),
            "max_depth": max_depth,
        }

    def hubs(self, top_n: int = 15, kind: str = None) -> Dict:
        """Nodes with highest degree (in+out)."""
        degree: Dict[str, int] = defaultdict(int)
        for row in self._conn.execute("SELECT src, dst FROM edges"):
            degree[row["src"]] += 1
            degree[row["dst"]] += 1

        items = []
        for nid, deg in degree.items():
            row = self._conn.execute(
                "SELECT id, kind, name, qualname, file FROM nodes WHERE id=?", (nid,)
            ).fetchone()
            if not row:
                continue
            if kind and row["kind"] != kind:
                continue
            items.append({
                "id": row["id"],
                "kind": row["kind"],
                "name": row["name"],
                "qualname": row["qualname"],
                "file": row["file"],
                "degree": deg,
            })
        items.sort(key=lambda x: -x["degree"])
        return {"hubs": items[:top_n], "count": len(items[:top_n])}

    def path(self, source: str, target: str, max_depth: int = 8) -> Dict:
        """Shortest path between two symbols (BFS on undirected view of edges)."""
        srcs = self._resolve_symbol(source)
        tgts = self._resolve_symbol(target)
        if not srcs:
            return {"error": f"Source not found: {source}", "path": []}
        if not tgts:
            return {"error": f"Target not found: {target}", "path": []}

        tgt_set = set(tgts)
        # adjacency both directions
        adj: Dict[str, List[str]] = defaultdict(list)
        for row in self._conn.execute("SELECT src, dst FROM edges"):
            adj[row["src"]].append(row["dst"])
            adj[row["dst"]].append(row["src"])

        for start in srcs:
            prev = {start: None}
            q = deque([start])
            found = None
            while q:
                cur = q.popleft()
                if cur in tgt_set:
                    found = cur
                    break
                if len(prev) > 50000:
                    break
                for nb in adj.get(cur, []):
                    if nb not in prev:
                        # depth limit via path reconstruction length later
                        prev[nb] = cur
                        # rough depth
                        depth = 0
                        p = cur
                        while p is not None and depth <= max_depth:
                            p = prev.get(p)
                            depth += 1
                        if depth <= max_depth:
                            q.append(nb)
            if found:
                chain = []
                cur = found
                while cur is not None:
                    chain.append(cur)
                    cur = prev[cur]
                chain.reverse()
                detailed = []
                for nid in chain:
                    row = self._conn.execute(
                        "SELECT id, kind, name, qualname, file FROM nodes WHERE id=?", (nid,)
                    ).fetchone()
                    if row:
                        detailed.append(dict(row))
                    else:
                        detailed.append({"id": nid})
                return {
                    "source": source,
                    "target": target,
                    "path": detailed,
                    "length": len(detailed),
                }
        return {
            "source": source,
            "target": target,
            "path": [],
            "length": 0,
            "error": "No path found",
        }

    def stats(self) -> Dict:
        n_nodes = self._conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
        n_edges = self._conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
        n_files = self._conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
        by_kind = {
            r["kind"]: r["c"]
            for r in self._conn.execute(
                "SELECT kind, COUNT(*) AS c FROM nodes GROUP BY kind"
            )
        }
        edge_kinds = {
            r["kind"]: r["c"]
            for r in self._conn.execute(
                "SELECT kind, COUNT(*) AS c FROM edges GROUP BY kind"
            )
        }
        return {
            "nodes": n_nodes,
            "edges": n_edges,
            "files": n_files,
            "nodes_by_kind": by_kind,
            "edges_by_kind": edge_kinds,
            "db_path": self.db_path,
        }

    def degree_map(self) -> Dict[str, int]:
        degree: Dict[str, int] = defaultdict(int)
        for row in self._conn.execute("SELECT src, dst FROM edges"):
            degree[row["src"]] += 1
            degree[row["dst"]] += 1
        return dict(degree)

    def all_symbols(self) -> List[Dict]:
        return [
            dict(r) for r in self._conn.execute(
                "SELECT id, kind, name, qualname, file, lineno FROM nodes "
                "WHERE kind IN ('function','class') ORDER BY file, lineno"
            )
        ]


# ── Module singleton + tools ──────────────────────────────────────────

_store: Optional[CodeGraphStore] = None


def get_store(project_root: str = None) -> CodeGraphStore:
    global _store
    root = os.path.abspath(project_root) if project_root else None
    if _store is None or (root and _store.project_root != root):
        _store = CodeGraphStore(project_root=root)
    return _store


def _ensure_built(root_dir: str = None) -> CodeGraphStore:
    store = get_store(root_dir)
    stats = store.stats()
    if stats["nodes"] == 0:
        store.build(root_dir)
    return store


def get_code_graph_tools() -> List[Dict]:
    """Tool definitions for the code graph subsystem."""
    return [
        {
            "name": "coding_graph_build",
            "description": "Build or incrementally update the project code graph (files/classes/functions + calls/imports/inherits/defines). Stored in .ww/code_graph.db.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_dir": {"type": "string", "description": "Project root (default: cwd)"},
                    "force": {"type": "boolean", "description": "Force full rebuild", "default": False},
                },
            },
            "handler": lambda root_dir=None, force=False: get_store(root_dir).build(root_dir, force=force),
            "category": "code_graph",
            "permission": "safe",
        },
        {
            "name": "coding_graph_who_calls",
            "description": "Find all callers of a function/class/symbol in the code graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Function or class name"},
                    "root_dir": {"type": "string", "description": "Project root if graph not built"},
                },
                "required": ["symbol"],
            },
            "handler": lambda symbol, root_dir=None: _ensure_built(root_dir).who_calls(symbol),
            "category": "code_graph",
            "permission": "safe",
        },
        {
            "name": "coding_graph_blast_radius",
            "description": "List downstream symbols affected if the given hub/symbol changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Hub symbol or file"},
                    "max_depth": {"type": "integer", "default": 5},
                    "root_dir": {"type": "string"},
                },
                "required": ["symbol"],
            },
            "handler": lambda symbol, max_depth=5, root_dir=None: _ensure_built(root_dir).blast_radius(symbol, max_depth=max_depth),
            "category": "code_graph",
            "permission": "safe",
        },
        {
            "name": "coding_graph_hubs",
            "description": "Top high-degree nodes (important symbols/files) in the code graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "default": 15},
                    "kind": {"type": "string", "description": "Optional filter: file|class|function"},
                    "root_dir": {"type": "string"},
                },
            },
            "handler": lambda top_n=15, kind=None, root_dir=None: _ensure_built(root_dir).hubs(top_n=top_n, kind=kind),
            "category": "code_graph",
            "permission": "safe",
        },
        {
            "name": "coding_graph_path",
            "description": "Shortest path between two symbols in the code graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 8},
                    "root_dir": {"type": "string"},
                },
                "required": ["source", "target"],
            },
            "handler": lambda source, target, max_depth=8, root_dir=None: _ensure_built(root_dir).path(source, target, max_depth=max_depth),
            "category": "code_graph",
            "permission": "safe",
        },
        {
            "name": "coding_graph_stats",
            "description": "Code graph statistics: node/edge counts by kind.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_dir": {"type": "string"},
                },
            },
            "handler": lambda root_dir=None: get_store(root_dir).stats(),
            "category": "code_graph",
            "permission": "safe",
        },
    ]
