"""ww/pm/code_rag.py — AST-aware Code Retrieval Augmented Generation v0.1

Implements Gemini's WW-PM Subsystem 3.1.2:
- AST-bounded code chunking (respects function/class boundaries)
- Hybrid search (BM25 keyword + AST structural)
- Merkle tree change tracking for incremental re-indexing

All pure Python, zero external dependencies.
"""

from __future__ import annotations
import ast
import hashlib
import json
import os
import re
import math
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Set


# ── AST-Bounded Code Chunking ─────────────────────────────────────────

class CodeChunk:
    """A single chunk of code bounded by AST syntax units."""

    def __init__(
        self,
        filepath: str,
        chunk_type: str,
        name: str,
        content: str,
        start_line: int,
        end_line: int,
        parent: str = "",
    ):
        self.filepath = filepath
        self.chunk_type = chunk_type  # module, class, function, method
        self.name = name
        self.content = content
        self.start_line = start_line
        self.end_line = end_line
        self.parent = parent  # parent class/function name
        self.id = self._compute_id()

    def _compute_id(self) -> str:
        """Unique ID for deduplication."""
        raw = f"{self.filepath}:{self.chunk_type}:{self.name}:{self.start_line}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "filepath": self.filepath,
            "type": self.chunk_type,
            "name": self.name,
            "content": self.content,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "parent": self.parent,
        }


class ASTChunker:
    """Split Python files into AST-bounded code chunks.

    Each chunk respects complete syntax boundaries:
    - Module-level: ClassDef, FunctionDef at module scope
    - Nested: Methods inside classes
    - Functions keep their full body
    """

    MIN_CHUNK_LINES = 3

    def chunk_file(self, filepath: str) -> List[CodeChunk]:
        """Chunk a single Python file into AST-bounded pieces."""
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        lines = content.split("\n")
        chunks = []
        filename = os.path.basename(filepath)

        # Module-level docstring/imports as a chunk
        module_header = self._extract_module_header(tree, lines)
        if module_header:
            chunks.append(CodeChunk(
                filepath, "module", f"{filename}:header",
                module_header, 1, module_header.count("\n") + 1,
            ))

        # Extract classes and functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_chunk = self._make_chunk(filepath, node, lines, "class")
                if class_chunk:
                    chunks.append(class_chunk)

                # Extract methods
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_chunk = self._make_chunk(
                            filepath, child, lines, "method", parent=node.name
                        )
                        if method_chunk:
                            chunks.append(method_chunk)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_chunk = self._make_chunk(filepath, node, lines, "function")
                if func_chunk:
                    chunks.append(func_chunk)

        # If no structural chunks found, use whole file
        if not chunks:
            chunks.append(CodeChunk(
                filepath, "module", filename, content, 1, len(lines),
            ))

        return chunks

    def _make_chunk(
        self, filepath: str, node: ast.AST, lines: List[str],
        chunk_type: str, parent: str = "",
    ) -> Optional[CodeChunk]:
        """Create a code chunk from an AST node."""
        start = node.lineno - 1
        end = (node.end_lineno or len(lines)) - 1

        if end - start < self.MIN_CHUNK_LINES:
            return None

        content = "\n".join(lines[start:end + 1])
        name = node.name if hasattr(node, "name") else f"anon_{start}"

        return CodeChunk(filepath, chunk_type, name, content, start + 1, end + 1, parent)

    def _extract_module_header(self, tree: ast.AST, lines: List[str]) -> Optional[str]:
        """Extract module-level docstring and imports."""
        header_lines = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                header_lines.append(lines[node.lineno - 1])
            elif isinstance(node, ast.Expr) and isinstance(node.value, (ast.Str, ast.Constant)):
                val = node.value.s if isinstance(node.value, ast.Str) else node.value.value
                if isinstance(val, str) and node.lineno == 1:
                    for i in range(node.lineno - 1, node.end_lineno or node.lineno):
                        if i < len(lines):
                            header_lines.append(lines[i])
                else:
                    break
            else:
                break

        return "\n".join(header_lines) if header_lines else None


# ── BM25 Keyword Index ────────────────────────────────────────────────

class BM25Index:
    """Simple BM25 keyword index for code search.

    No external deps — pure Python with IDF/term frequency scoring.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._doc_freq: Counter = Counter()
        self._doc_lengths: List[int] = []
        self._docs: List[Dict] = []
        self._avg_doc_length: float = 0
        self._total_docs: int = 0
        self._dirty: bool = False

    def add_document(self, doc: Dict, text: str):
        """Add a document to the index."""
        tokens = self._tokenize(text)
        self._docs.append({"id": doc["id"], "meta": doc})
        self._doc_lengths.append(len(tokens))

        for token in set(tokens):
            self._doc_freq[token] += 1

        self._total_docs += 1
        self._dirty = True

    def add_chunks(self, chunks: List[CodeChunk]):
        """Add multiple code chunks to the index."""
        for chunk in chunks:
            self.add_document(chunk.to_dict(), chunk.content)

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        """Search the index with BM25 scoring."""
        if not self._total_docs:
            return []

        self._avg_doc_length = sum(self._doc_lengths) / self._total_docs
        query_tokens = self._tokenize(query)

        scores = []
        for i, doc in enumerate(self._docs):
            score = self._score_document(i, query_tokens)
            if score > 0:
                scores.append((score, doc))

        scores.sort(key=lambda x: -x[0])
        results = []
        for score, doc in scores[:top_k]:
            results.append({
                "score": round(score, 4),
                "id": doc["id"],
                "meta": doc["meta"],
            })

        return results

    def _score_document(self, doc_idx: int, query_tokens: List[str]) -> float:
        """Compute BM25 score for a document."""
        doc_len = self._doc_lengths[doc_idx]
        score = 0.0

        term_counts = Counter()
        for token in query_tokens:
            term_counts[token] += 1

        for token, tf in term_counts.items():
            df = self._doc_freq.get(token, 0)
            if df == 0:
                continue

            idf = math.log((self._total_docs - df + 0.5) / (df + 0.5) + 1.0)
            norm_tf = tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_length))
            score += idf * norm_tf

        return score

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize code text into searchable tokens."""
        text = text.lower()
        # Split on non-alphanumeric, keep underscores
        tokens = re.findall(r"[a-z_][a-z0-9_]{1,}", text)
        # Split snake_case into parts
        result = []
        for token in tokens:
            result.append(token)
            parts = token.split("_")
            if len(parts) > 1:
                result.extend(p for p in parts if len(p) > 1)
        return result

    def clear(self):
        self._doc_freq.clear()
        self._doc_lengths.clear()
        self._docs.clear()
        self._total_docs = 0
        self._avg_doc_length = 0

    @property
    def stats(self) -> Dict:
        return {
            "total_docs": self._total_docs,
            "avg_doc_length": round(self._avg_doc_length, 1),
            "unique_terms": len(self._doc_freq),
        }


# ── Merkle Tree for Change Tracking ───────────────────────────────────

class MerkleNode:
    """A node in the Merkle tree tracking file changes."""

    def __init__(self, path: str, hash_val: str, is_dir: bool = False):
        self.path = path
        self.hash = hash_val
        self.is_dir = is_dir
        self.children: Dict[str, "MerkleNode"] = {}

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "hash": self.hash,
            "is_dir": self.is_dir,
            "children": {k: v.to_dict() for k, v in self.children.items()},
        }


class MerkleTree:
    """Merkle tree for tracking file changes in a codebase.

    Each file's hash is computed from its AST-normalized content.
    Directory hashes are computed from children's hashes.
    Used for incremental re-indexing — only changed files are reprocessed.
    """

    def __init__(self, root_dir: str = "."):
        self._root_dir = os.path.abspath(root_dir)
        self._root = MerkleNode(self._root_dir, "", is_dir=True)

    def build(self, file_globs: List[str] = None) -> Dict:
        """Build the Merkle tree from the filesystem."""
        file_globs = file_globs or ["*.py"]
        self._root = MerkleNode(self._root_dir, "", is_dir=True)

        for glob_pattern in file_globs:
            for filepath in sorted(Path(self._root_dir).rglob(glob_pattern)):
                if ".git" in filepath.parts or "__pycache__" in filepath.parts:
                    continue
                rel_path = str(filepath.relative_to(self._root_dir))
                file_hash = self._hash_file(str(filepath))
                self._insert(rel_path, file_hash)

        self._compute_root_hash(self._root)
        return {
            "root_hash": self._root.hash,
            "files": self._count_files(self._root),
        }

    def diff(self, other: "MerkleTree") -> Dict:
        """Compare two Merkle trees, return changed files."""
        changes = {"added": [], "modified": [], "removed": []}
        self._diff_nodes(self._root, other._root, "", changes)
        return changes

    def changed_files_since(self, previous_tree: "MerkleTree") -> List[str]:
        """Get list of files that changed since previous tree."""
        diff = self.diff(previous_tree)
        result = []
        result.extend(diff["added"])
        result.extend(diff["modified"])
        return result

    def _hash_file(self, filepath: str) -> str:
        """Hash file content with AST normalization for stable hashing."""
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            # AST-normalize for stable hashing (ignore whitespace-only changes)
            try:
                tree = ast.parse(content)
                # Unparse produces canonical form
                normalized = ast.unparse(tree)
            except SyntaxError:
                normalized = content

            return hashlib.sha256(normalized.encode()).hexdigest()[:16]
        except IOError:
            return ""

    def _insert(self, rel_path: str, file_hash: str):
        """Insert a file path into the Merkle tree."""
        parts = rel_path.replace("\\", "/").split("/")
        node = self._root

        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            if part not in node.children:
                node.children[part] = MerkleNode(
                    os.path.join(node.path, part) if node.path != self._root_dir else os.path.join(self._root_dir, part),
                    file_hash if is_last else "",
                    is_dir=not is_last,
                )
            elif is_last:
                node.children[part].hash = file_hash
            node = node.children[part]

    def _compute_root_hash(self, node: MerkleNode) -> str:
        """Compute hash for a node based on children's hashes."""
        if not node.is_dir:
            return node.hash

        child_hashes = []
        for name in sorted(node.children.keys()):
            child = node.children[name]
            child_hash = self._compute_root_hash(child)
            child_hashes.append(f"{name}:{child_hash}")

        combined = "|".join(child_hashes)
        node.hash = hashlib.sha256(combined.encode()).hexdigest()[:16]
        return node.hash

    def _diff_nodes(
        self,
        old_node: MerkleNode,
        new_node: MerkleNode,
        prefix: str,
        changes: Dict,
    ):
        """Recursively diff two Merkle tree nodes."""
        old_children = set(old_node.children.keys()) if old_node else set()
        new_children = set(new_node.children.keys()) if new_node else set()

        for name in new_children - old_children:
            path = os.path.join(prefix, name)
            if new_node.children[name].is_dir:
                self._collect_all(new_node.children[name], path, changes["added"])
            else:
                changes["added"].append(path)

        for name in old_children - new_children:
            path = os.path.join(prefix, name)
            if old_node.children[name].is_dir:
                self._collect_all(old_node.children[name], path, changes["removed"])
            else:
                changes["removed"].append(path)

        for name in old_children & new_children:
            path = os.path.join(prefix, name)
            old_child = old_node.children[name] if old_node else None
            new_child = new_node.children[name] if new_node else None

            if old_child and new_child:
                if old_child.is_dir != new_child.is_dir:
                    changes["modified"].append(path)
                elif old_child.is_dir:
                    self._diff_nodes(old_child, new_child, path, changes)
                elif old_child.hash != new_child.hash:
                    changes["modified"].append(path)

    def _collect_all(self, node: MerkleNode, prefix: str, result: List[str]):
        """Collect all file paths under a node."""
        if not node.is_dir:
            result.append(prefix)
        for name, child in node.children.items():
            child_path = os.path.join(prefix, name)
            self._collect_all(child, child_path, result)

    def _count_files(self, node: MerkleNode) -> int:
        """Count total files under a node."""
        if not node.is_dir:
            return 1
        return sum(self._count_files(c) for c in node.children.values())

    def to_dict(self) -> Dict:
        return self._root.to_dict()

    def save(self, path: str):
        """Save Merkle tree to JSON."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str) -> Optional["MerkleTree"]:
        """Load Merkle tree from JSON."""
        if not os.path.isfile(path):
            return None
        with open(path, "r") as f:
            data = json.load(f)
        tree = cls(data["path"])
        tree._root = cls._from_dict(data)
        return tree

    @classmethod
    def _from_dict(cls, data: Dict) -> MerkleNode:
        node = MerkleNode(data["path"], data["hash"], data["is_dir"])
        for name, child_data in data.get("children", {}).items():
            node.children[name] = cls._from_dict(child_data)
        return node


# ── Code RAG Engine ───────────────────────────────────────────────────

class CodeRAGEngine:
    """Code RAG engine with AST-aware chunking, BM25 indexing, and Merkle tracking."""

    def __init__(self, root_dir: str = "."):
        self._root_dir = root_dir
        self._chunker = ASTChunker()
        self._index = BM25Index()
        self._merkle = MerkleTree(root_dir)
        self._chunks: Dict[str, CodeChunk] = {}
        self._indexed_files: Set[str] = set()
        self._last_merkle_path = os.path.join(root_dir, ".ww", "code_rag_merkle.json")
        self._index_path = os.path.join(root_dir, ".ww", "code_rag_index.json")

    def build_index(self, file_globs: List[str] = None) -> Dict:
        """Build or rebuild the full index."""
        file_globs = file_globs or ["*.py"]
        new_merkle = MerkleTree(self._root_dir)
        result = new_merkle.build(file_globs)

        # Check if we need incremental update
        old_merkle = MerkleTree.load(self._last_merkle_path)
        if old_merkle:
            changed = new_merkle.changed_files_since(old_merkle)
            if not changed:
                # Load the existing index so chunks are populated
                self.load_index()
                return {"status": "uptodate", "total_chunks": len(self._chunks)}

            # Incremental update
            for filepath in changed:
                abs_path = os.path.join(self._root_dir, filepath)
                self._remove_file_chunks(filepath)
                self._index_file(abs_path)
        else:
            # Full rebuild
            self._index.clear()
            self._chunks.clear()
            self._indexed_files.clear()
            for glob_pattern in file_globs:
                for filepath in sorted(Path(self._root_dir).rglob(glob_pattern)):
                    if ".git" in filepath.parts or "__pycache__" in filepath.parts:
                        continue
                    self._index_file(str(filepath))

        # Save state
        os.makedirs(os.path.dirname(self._last_merkle_path), exist_ok=True)
        new_merkle.save(self._last_merkle_path)
        self._save_index()
        self._merkle = new_merkle

        return {
            "status": "updated",
            "total_chunks": len(self._chunks),
            "total_files": len(self._indexed_files),
            "merkle_root": self._merkle._root.hash,
        }

    def search(self, query: str, top_k: int = 10, hybrid: bool = True) -> Dict:
        """Search the code index. When hybrid=True, combines BM25 + Dense Vector scores."""
        bm25_results = self._index.search(query, top_k * 3)  # Get more candidates for re-ranking
        
        if not hybrid or not bm25_results:
            return {"query": query, "results": bm25_results[:top_k], "total": len(bm25_results[:top_k]), "mode": "bm25_only"}
        
        # Build dense vectors for BM25 results and re-rank
        try:
            from coding.dense_vector import CooccurrenceEmbedding
            emb = CooccurrenceEmbedding(vector_size=32)
            chunk_texts = []
            chunk_ids = []
            for r in bm25_results:
                cid = r["id"]
                if cid in self._chunks:
                    chunk_texts.append(self._chunks[cid].content)
                    chunk_ids.append(cid)
            
            if len(chunk_texts) >= 2:
                emb.build(chunk_texts)
                qv = emb.embed(query)
                
                # Compute hybrid scores (BM25 * 0.5 + Dense * 0.5)
                combined = []
                for i, r in enumerate(bm25_results):
                    bm25_score = r["score"]
                    if chunk_ids and i < len(chunk_ids):
                        dv = emb.embed(chunk_texts[i] if i < len(chunk_texts) else "")
                        import math
                        dot = sum(a*b for a,b in zip(qv, dv))
                        nq = math.sqrt(sum(a*a for a in qv))
                        nd = math.sqrt(sum(a*a for a in dv))
                        dense_score = dot / (nq * nd) if nq > 0 and nd > 0 else 0
                        hybrid_score = bm25_score * 0.5 + dense_score * 0.5
                    else:
                        hybrid_score = bm25_score
                    combined.append((hybrid_score, r))
                
                combined.sort(key=lambda x: -x[0])
                results = []
                for score, r in combined[:top_k]:
                    r = dict(r)
                    r["hybrid_score"] = round(score, 4)
                    results.append(r)
                
                return {"query": query, "results": results, "total": len(results), "mode": "hybrid"}
        except Exception:
            pass
        
        return {"query": query, "results": bm25_results[:top_k], "total": len(bm25_results[:top_k]), "mode": "bm25_only"}

    def get_context(self, query: str, max_chunks: int = 5) -> Dict:
        """Get code context for a query — chunks + file summaries."""
        results = self._index.search(query, max_chunks)

        chunks = []
        files_touched = set()
        for r in results:
            chunk_id = r["id"]
            if chunk_id in self._chunks:
                chunk = self._chunks[chunk_id]
                chunks.append(chunk.to_dict())
                files_touched.add(chunk.filepath)

        return {
            "query": query,
            "chunks": chunks,
            "files_touched": list(files_touched),
            "total_chunks_found": len(chunks),
        }

    def _index_file(self, filepath: str):
        """Index a single file."""
        if not os.path.isfile(filepath):
            return

        rel_path = os.path.relpath(filepath, self._root_dir)
        chunks = self._chunker.chunk_file(filepath)

        for chunk in chunks:
            self._chunks[chunk.id] = chunk
            self._index.add_chunks([chunk])

        self._indexed_files.add(rel_path)

    def _remove_file_chunks(self, rel_path: str):
        """Remove all chunks for a file."""
        abs_path = os.path.join(self._root_dir, rel_path)
        ids_to_remove = [
            cid for cid, chunk in self._chunks.items()
            if chunk.filepath == abs_path
        ]
        for cid in ids_to_remove:
            del self._chunks[cid]

        self._indexed_files.discard(rel_path)
        # Note: full index rebuild needed to remove from BM25
        # For simplicity, rebuild when files are removed
        if ids_to_remove:
            self._rebuild_index()

    def _rebuild_index(self):
        """Full rebuild from remaining chunks."""
        self._index.clear()
        for chunk in self._chunks.values():
            self._index.add_chunks([chunk])

    def _save_index(self):
        """Save index state to disk."""
        os.makedirs(os.path.dirname(self._index_path), exist_ok=True)
        data = {
            "chunks": {k: v.to_dict() for k, v in self._chunks.items()},
            "indexed_files": list(self._indexed_files),
        }
        with open(self._index_path, "w") as f:
            json.dump(data, f, indent=2)

    def load_index(self) -> bool:
        """Load index from disk."""
        if not os.path.isfile(self._index_path):
            return False
        try:
            with open(self._index_path, "r") as f:
                data = json.load(f)
            self._chunks.clear()
            for cid, cdata in data.get("chunks", {}).items():
                chunk = CodeChunk(
                    cdata["filepath"], cdata["type"], cdata["name"],
                    cdata["content"], cdata["start_line"], cdata["end_line"],
                    cdata.get("parent", ""),
                )
                chunk.id = cid
                self._chunks[cid] = chunk
                self._index.add_chunks([chunk])
            self._indexed_files = set(data.get("indexed_files", []))
            return True
        except (json.JSONDecodeError, KeyError):
            return False

    def get_stats(self) -> Dict:
        return {
            "total_chunks": len(self._chunks),
            "total_files": len(self._indexed_files),
            "index_stats": self._index.stats,
        }


# ── Tool definitions ──────────────────────────────────────────────────

_rag: CodeRAGEngine = None


def _update_rag_singleton(root_dir: str = ".") -> Dict:
    """Build index into the singleton CodeRAGEngine instance."""
    global _rag
    # Update root_dir and rebuild
    engine = CodeRAGEngine(root_dir)
    result = engine.build_index()
    _rag = engine
    return result


def get_rag() -> CodeRAGEngine:
    global _rag
    if _rag is None:
        _rag = CodeRAGEngine()
    return _rag


def create_code_rag_tools(rag: CodeRAGEngine) -> List[Dict]:
    return [
        {
            "name": "coding_rag_build",
            "description": "Build or update the code search index. Scans Python files, chunks by AST boundaries, indexes with BM25. Uses Merkle tree for incremental updates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_dir": {
                        "type": "string",
                        "description": "Project root directory",
                        "default": ".",
                    }
                },
            },
            "handler": lambda root_dir=".": _update_rag_singleton(root_dir),
            "category": "code_search",
        },
        {
            "name": "coding_rag_search",
            "description": "Search code using BM25 keyword index. Returns AST-bounded code chunks ranked by relevance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords (function names, concepts, patterns)",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            "handler": lambda query, top_k=10: rag.search(query, top_k),
            "category": "code_search",
        },
        {
            "name": "coding_rag_context",
            "description": "Get code context for a query — returns relevant code chunks plus file summaries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords",
                    },
                    "max_chunks": {
                        "type": "integer",
                        "description": "Max context chunks (default: 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            "handler": lambda query, max_chunks=5: rag.get_context(query, max_chunks),
            "category": "code_search",
        },
        {
            "name": "coding_rag_stats",
            "description": "Get code index statistics: total chunks, files, and index quality metrics.",
            "parameters": {"type": "object", "properties": {}},
            "handler": rag.get_stats,
            "category": "code_search",
        },
    ]


def get_rag_tools() -> List[Dict]:
    return create_code_rag_tools(get_rag())
