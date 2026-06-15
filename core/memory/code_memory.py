"""ww/core/memory/code_memory.py — Immutable Code Memory Store v0.1

Separates code memory from conversational memory. Code fragments are stored
with exact content hashes and NEVER pruned, abstracted, or 5-factor scored.

Design:
  - SHA256 exact content hash as primary key
  - Merkle tree incremental change tracking (from code_rag.py)
  - Call graph for semantic recall (who calls what)
  - LSP-level precision: stores full source text, not summaries
  - Integration: code_memory atoms carry is_immutable=True flag,
    which sleep.py respects — immutable atoms skip Phase 3 abstraction/pruning

This solves the "def calculate_tax(income) → 'calculates tax function'"
semantic drift problem. Code RAG retrieves 100% accurate AST nodes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ── Data types ───────────────────────────────────────────────────────

@dataclass
class CodeAtom:
    """An immutable code fragment — exact source, never abstracted."""

    hash: str                      # SHA256 hex of content (primary key)
    content: str                   # FULL source code, never truncated
    ast_type: str                  # module, class, function, method, expression
    name: str                      # e.g. "calculate_tax", "MyClass"
    filepath: str                  # Source file path
    line_start: int
    line_end: int
    timestamp: float = 0.0         # When stored
    is_immutable: bool = True      # Always True for CodeAtom

    # Optional metadata
    docstring: str = ""
    decorators: List[str] = field(default_factory=list)
    parent: str = ""               # Parent class/function name
    call_targets: List[str] = field(default_factory=list)  # Functions called
    callers: List[str] = field(default_factory=list)        # Functions that call this

    def to_dict(self) -> Dict:
        return {
            "hash": self.hash,
            "content": self.content,
            "ast_type": self.ast_type,
            "name": self.name,
            "filepath": self.filepath,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "timestamp": self.timestamp,
            "is_immutable": self.is_immutable,
            "docstring": self.docstring,
            "decorators": self.decorators,
            "parent": self.parent,
            "call_targets": self.call_targets,
            "callers": self.callers,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "CodeAtom":
        return cls(
            hash=d["hash"],
            content=d["content"],
            ast_type=d.get("ast_type", "function"),
            name=d.get("name", ""),
            filepath=d.get("filepath", ""),
            line_start=d.get("line_start", 0),
            line_end=d.get("line_end", 0),
            timestamp=d.get("timestamp", 0.0),
            docstring=d.get("docstring", ""),
            decorators=d.get("decorators", []),
            parent=d.get("parent", ""),
            call_targets=d.get("call_targets", []),
            callers=d.get("callers", []),
        )


# ── Code Memory Store ────────────────────────────────────────────────

class CodeMemoryStore:
    """Immutable code memory — separate from conversational memory.

    Code atoms are stored by exact SHA256 hash.
    NEVER pruned, NEVER abstracted, NEVER 5-factor scored.
    Integrates with sleep.py via is_immutable flag.
    """

    def __init__(self, persist_dir: str = ""):
        self._atoms: Dict[str, CodeAtom] = {}          # hash → atom
        self._call_graph: Dict[str, Set[str]] = defaultdict(set)  # name → callee names
        self._reverse_graph: Dict[str, Set[str]] = defaultdict(set)  # callee → caller names
        self._name_index: Dict[str, List[str]] = defaultdict(list)   # name → [hashes]
        self._file_index: Dict[str, List[str]] = defaultdict(list)   # filepath → [hashes]
        self._total_atoms = 0

        self._persist_dir = persist_dir or os.path.expanduser("~/.ww")
        self._store_path = os.path.join(self._persist_dir, "code_memory.json")
        os.makedirs(self._persist_dir, exist_ok=True)

        self._load()

    # ── Storage ──────────────────────────────────────────────────

    def store(self, atom: CodeAtom) -> str:
        """Store a code atom. Idempotent — same content = same hash."""
        if not atom.hash:
            atom.hash = self._compute_hash(atom.content)
        if not atom.timestamp:
            atom.timestamp = time.time()

        # Idempotent: if hash exists, update metadata only
        existing = self._atoms.get(atom.hash)
        if existing:
            # Update call graph info if new calls discovered
            existing.call_targets = list(set(existing.call_targets + atom.call_targets))
            existing.callers = list(set(existing.callers + atom.callers))
        else:
            self._atoms[atom.hash] = atom
            self._total_atoms += 1

        # Update indexes
        if atom.name:
            if atom.hash not in self._name_index[atom.name]:
                self._name_index[atom.name].append(atom.hash)
        if atom.filepath:
            if atom.hash not in self._file_index[atom.filepath]:
                self._file_index[atom.filepath].append(atom.hash)

        # Update call graph
        for target in atom.call_targets:
            self._call_graph[atom.name].add(target)
            self._reverse_graph[target].add(atom.name)

        return atom.hash

    def store_chunk(self, chunk: Any) -> Optional[str]:
        """Store a CodeChunk from code_rag.py (duck-typed, no import dependency).

        The chunk is expected to have: content, chunk_type, name, filepath,
        start_line, end_line attributes.
        """
        try:
            atom = CodeAtom(
                hash=self._compute_hash(chunk.content),
                content=chunk.content,
                ast_type=getattr(chunk, 'chunk_type', 'function'),
                name=getattr(chunk, 'name', ''),
                filepath=getattr(chunk, 'filepath', ''),
                line_start=getattr(chunk, 'start_line', 0),
                line_end=getattr(chunk, 'end_line', 0),
                parent=getattr(chunk, 'parent', ''),
            )
            return self.store(atom)
        except Exception:
            return None

    # ── Recall ───────────────────────────────────────────────────

    def recall_by_hash(self, hash_val: str) -> Optional[CodeAtom]:
        """Exact hash lookup — 100% precise."""
        return self._atoms.get(hash_val)

    def recall_by_name(self, name: str) -> List[CodeAtom]:
        """Find all code atoms with a given name (e.g., 'calculate_tax')."""
        hashes = self._name_index.get(name, [])
        return [self._atoms[h] for h in hashes if h in self._atoms]

    def recall_callers(self, name: str) -> List[CodeAtom]:
        """Find all functions that call `name` — uses the reverse call graph."""
        caller_names = self._reverse_graph.get(name, set())
        atoms = []
        for cn in caller_names:
            atoms.extend(self.recall_by_name(cn))
        return atoms

    def recall_callees(self, name: str) -> List[CodeAtom]:
        """Find all functions called by `name`."""
        callee_names = self._call_graph.get(name, set())
        atoms = []
        for cn in callee_names:
            atoms.extend(self.recall_by_name(cn))
        return atoms

    def search_keyword(self, keyword: str, top_k: int = 10) -> List[CodeAtom]:
        """Simple keyword search across all code atoms (case-insensitive)."""
        kw = keyword.lower()
        scored = []
        for atom in self._atoms.values():
            content_lower = atom.content.lower()
            if kw not in content_lower:
                continue
            # Score by number of occurrences and position
            count = content_lower.count(kw)
            pos = content_lower.find(kw)
            score = count * 10 + max(0, 100 - pos)
            scored.append((score, atom))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [atom for _, atom in scored[:top_k]]

    def search_by_file(self, filepath: str) -> List[CodeAtom]:
        """Find all code atoms from a specific file."""
        hashes = self._file_index.get(filepath, [])
        return [self._atoms[h] for h in hashes if h in self._atoms]

    # ── Call graph traversal ─────────────────────────────────────

    def get_call_graph_subgraph(self, root_name: str, depth: int = 2) -> Dict:
        """Traverse the call graph from root_name to `depth` levels.

        Returns a tree: {name: {"atom": CodeAtom, "callees": {name: {...}}}}
        """
        visited = set()
        def traverse(name: str, current_depth: int) -> Dict:
            if current_depth > depth or name in visited:
                return {}
            visited.add(name)
            atoms = self.recall_by_name(name)
            atom = atoms[0] if atoms else None
            node = {"atom": atom.to_dict() if atom else None, "callees": {}}
            for callee in self._call_graph.get(name, set()):
                child = traverse(callee, current_depth + 1)
                if child:
                    node["callees"][callee] = child
            return node
        return traverse(root_name, 0)

    # ── Maintenance ──────────────────────────────────────────────

    def remove_file(self, filepath: str) -> int:
        """Remove all code atoms for a file (on file deletion)."""
        hashes = list(self._file_index.get(filepath, []))
        count = 0
        for h in hashes:
            atom = self._atoms.pop(h, None)
            if atom:
                count += 1
                # Clean up name index
                if atom.name and atom.name in self._name_index:
                    self._name_index[atom.name] = [
                        x for x in self._name_index[atom.name] if x != h
                    ]
                # Clean up call graph
                if atom.name in self._call_graph:
                    del self._call_graph[atom.name]
                for callers in self._reverse_graph.values():
                    callers.discard(atom.name)
        if filepath in self._file_index:
            del self._file_index[filepath]
        return count

    def stats(self) -> Dict:
        """Return aggregate statistics."""
        return {
            "total_atoms": len(self._atoms),
            "total_files_indexed": len(self._file_index),
            "total_names": len(self._name_index),
            "graph_edges": sum(len(v) for v in self._call_graph.values()),
            "reverse_edges": sum(len(v) for v in self._reverse_graph.values()),
            "persist_path": self._store_path,
        }

    # ── Persistence ──────────────────────────────────────────────

    def save(self):
        """Persist all code atoms to JSON."""
        data = {
            "atoms": [a.to_dict() for a in self._atoms.values()],
            "call_graph": {k: list(v) for k, v in self._call_graph.items()},
            "reverse_graph": {k: list(v) for k, v in self._reverse_graph.items()},
        }
        with open(self._store_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _load(self):
        """Load persisted code atoms from JSON."""
        if not os.path.isfile(self._store_path):
            return
        try:
            with open(self._store_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return

        for ad in data.get("atoms", []):
            atom = CodeAtom.from_dict(ad)
            self._atoms[atom.hash] = atom
            if atom.name:
                self._name_index[atom.name].append(atom.hash)
            if atom.filepath:
                self._file_index[atom.filepath].append(atom.hash)
            for target in atom.call_targets:
                self._call_graph[atom.name].add(target)
                self._reverse_graph[target].add(atom.name)

        self._total_atoms = len(self._atoms)

        # Restore call graph
        for k, v in data.get("call_graph", {}).items():
            self._call_graph[k] = set(v)
        for k, v in data.get("reverse_graph", {}).items():
            self._reverse_graph[k] = set(v)

    # ── Utility ──────────────────────────────────────────────────

    @staticmethod
    def _compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


def create_code_memory(persist_dir: str = "") -> CodeMemoryStore:
    """Factory function."""
    return CodeMemoryStore(persist_dir=persist_dir)
