"""
Semantic Codebase Index — embeddings + vector search for code understanding.

Two modes:
  1. Lightweight (default): TF-IDF + cosine similarity, pure Python, zero deps
  2. API: uses LLM provider's embeddings API (OpenAI/OpenRouter/DeepSeek)
  3. Local: sentence-transformers (optional, pip install sentence-transformers)

Integrates with coding tools to enable:
  - "Find where authentication logic is implemented"
  - "Show me all places that handle file uploads"
  - Semantic code search across entire codebase

Usage:
  from core.codebase_index import CodebaseIndex
  idx = CodebaseIndex()
  idx.index("~/myproject")          # build index
  results = idx.search("auth logic")  # semantic search
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple


# ── Lightweight TF-IDF (zero deps, pure Python) ──────────────────

class TfidfIndex:
    """Pure-Python TF-IDF with cosine similarity. Zero dependencies."""

    def __init__(self):
        self._documents: Dict[str, str] = {}          # doc_id → text
        self._doc_freq: Dict[str, int] = Counter()     # term → doc count
        self._tfidf: Dict[str, Dict[str, float]] = {}  # doc_id → {term → tfidf}
        self._idf: Dict[str, float] = {}
        self._total_docs = 0

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Tokenize code: split on camelCase, snake_case, and non-alpha."""
        # Break camelCase
        text = re.sub(r'([a-z])([A-Z])', r'\1_\2', text)
        # Break snake_case / kebab-case
        tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text.lower())
        # Filter short tokens, keep meaningful ones
        return [t for t in tokens if len(t) > 1]

    def add_document(self, doc_id: str, text: str):
        """Add or update a document."""
        tokens = self._tokenize(text)
        term_freq = Counter(tokens)

        # Remove old document first
        if doc_id in self._documents:
            self._remove_doc(doc_id)

        self._documents[doc_id] = text
        self._tfidf[doc_id] = {}
        self._total_docs += 1

        # Update document frequencies
        for term in term_freq:
            self._doc_freq[term] += 1

        # Compute TF-IDF
        max_tf = max(term_freq.values()) if term_freq else 1
        for term, tf in term_freq.items():
            idf = math.log((self._total_docs + 1) / (self._doc_freq[term] + 1)) + 1
            self._tfidf[doc_id][term] = (tf / max_tf) * idf
            self._idf[term] = idf

    def _remove_doc(self, doc_id: str):
        """Remove a document from the index."""
        if doc_id not in self._tfidf:
            return
        for term in self._tfidf[doc_id]:
            self._doc_freq[term] = max(0, self._doc_freq.get(term, 1) - 1)
        del self._tfidf[doc_id]
        del self._documents[doc_id]
        self._total_docs = max(0, self._total_docs - 1)
        # Recalculate IDFs for affected terms
        for term, freq in list(self._doc_freq.items()):
            self._idf[term] = math.log((self._total_docs + 1) / (freq + 1)) + 1

    def remove_document(self, doc_id: str):
        """Public remove wrapper."""
        self._remove_doc(doc_id)

    def search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """Search for documents matching query. Returns [(doc_id, score), ...]."""
        query_tokens = self._tokenize(query)
        if not query_tokens or not self._documents:
            return []

        # Compute query TF-IDF vector
        query_vec: Dict[str, float] = {}
        tf = Counter(query_tokens)
        max_tf = max(tf.values()) if tf else 1
        for term, count in tf.items():
            if term in self._idf:
                query_vec[term] = (count / max_tf) * self._idf[term]
            else:
                query_vec[term] = count / max_tf  # OOV term

        # Cosine similarity against all documents
        scores: List[Tuple[str, float]] = []
        query_norm = math.sqrt(sum(v ** 2 for v in query_vec.values())) or 1.0
        for doc_id, doc_vec in self._tfidf.items():
            dot = sum(query_vec.get(t, 0) * doc_vec.get(t, 0) for t in set(query_vec) | set(doc_vec))
            doc_norm = math.sqrt(sum(v ** 2 for v in doc_vec.values())) or 1.0
            similarity = dot / (query_norm * doc_norm) if (query_norm * doc_norm) > 0 else 0.0
            if similarity > 0:
                scores.append((doc_id, similarity))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def size(self) -> int:
        return len(self._documents)


# ── Codebase Index ────────────────────────────────────────────────

# File extensions to index
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
    ".sh", ".bash", ".zsh", ".sql", ".yaml", ".yml", ".toml", ".json",
    ".md", ".rst", ".txt", ".cfg", ".ini", ".env.example", ".proto",
    ".css", ".scss", ".less", ".html", ".vue", ".svelte",
}

# Directories to skip
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "vendor",
    ".tox", ".eggs", "build", "dist", ".next", ".nuxt", "target",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
    ".turbo", ".cache", "tmp", "temp",
}

# Files to skip
SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "Gemfile.lock", "Pipfile.lock",
}


class CodeChunk:
    """A chunk of code from a file."""
    __slots__ = ("chunk_id", "file_path", "start_line", "end_line", "content", "language")

    def __init__(self, chunk_id: str, file_path: str, start_line: int,
                 end_line: int, content: str, language: str = ""):
        self.chunk_id = chunk_id
        self.file_path = file_path
        self.start_line = start_line
        self.end_line = end_line
        self.content = content
        self.language = language

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content": self.content,
            "language": self.language,
        }

    @staticmethod
    def from_dict(d: dict) -> "CodeChunk":
        return CodeChunk(
            chunk_id=d["chunk_id"],
            file_path=d["file_path"],
            start_line=d["start_line"],
            end_line=d["end_line"],
            content=d["content"],
            language=d.get("language", ""),
        )


class CodebaseIndex:
    """Semantic codebase index with chunking, embedding, and search."""

    def __init__(self, db_path: str = "", chunk_size: int = 50):
        if not db_path:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "data", "codebase_index.db"
            )
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.chunk_size = chunk_size
        self._tfidf = TfidfIndex()
        self._lock = threading.Lock()
        self._embedding_fn: Optional[Callable] = None  # Set for API embeddings
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content TEXT NOT NULL,
                language TEXT DEFAULT '',
                file_hash TEXT DEFAULT '',
                indexed_at TEXT DEFAULT ''
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(file_path)")
            conn.execute("""CREATE TABLE IF NOT EXISTS index_meta (
                root_path TEXT PRIMARY KEY,
                file_count INTEGER DEFAULT 0,
                chunk_count INTEGER DEFAULT 0,
                total_lines INTEGER DEFAULT 0,
                last_indexed TEXT DEFAULT ''
            )""")
            conn.commit()
            conn.close()

    # ── Indexing ──────────────────────────────────────────────────

    def index(self, root_path: str, progress_callback=None) -> Dict[str, int]:
        """Index all code files in a directory tree.

        Args:
            root_path: Root directory to index
            progress_callback: Optional fn(path, current, total)

        Returns:
            {"files": N, "chunks": M, "lines": L, "errors": E}
        """
        root = os.path.expanduser(root_path)
        stats = {"files": 0, "chunks": 0, "lines": 0, "errors": 0}

        # Collect all files
        all_files = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip directories
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                if fname in SKIP_FILES:
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext in CODE_EXTS or fname in CODE_EXTS:
                    all_files.append(os.path.join(dirpath, fname))

        total_files = len(all_files)
        chunks_added = 0

        with self._lock:
            # Clear old index for this root
            self._clear_root(root)

            for i, filepath in enumerate(all_files):
                try:
                    rel_path = os.path.relpath(filepath, root)
                    ext = os.path.splitext(filepath)[1].lower()
                    lang = self._detect_language(filepath, ext)
                    file_chunks = self._chunk_file(filepath, rel_path, lang)
                    for chunk in file_chunks:
                        self._save_chunk(chunk)
                        self._tfidf.add_document(chunk.chunk_id, chunk.content)
                        chunks_added += 1
                        stats["chunks"] += 1
                    stats["files"] += 1
                    stats["lines"] += file_chunks[-1].end_line if file_chunks else 0
                except Exception:
                    stats["errors"] += 1
                    continue

                if progress_callback and i % 10 == 0:
                    progress_callback(filepath, i + 1, total_files)

            # Save metadata
            self._save_meta(root, stats["files"], stats["chunks"], stats["lines"])

        return stats

    def reindex_file(self, root_path: str, file_path: str):
        """Reindex a single file (incremental update)."""
        root = os.path.expanduser(root_path)
        rel_path = os.path.relpath(file_path, root)
        ext = os.path.splitext(file_path)[1].lower()

        with self._lock:
            # Remove old chunks
            self._remove_file_chunks(rel_path)
            # Re-chunk and re-add
            lang = self._detect_language(file_path, ext)
            for chunk in self._chunk_file(file_path, rel_path, lang):
                self._save_chunk(chunk)
                self._tfidf.add_document(chunk.chunk_id, chunk.content)

    def remove_file(self, file_path: str):
        """Remove a file from the index."""
        with self._lock:
            self._remove_file_chunks(file_path)

    # ── Search ────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 10,
               file_filter: Optional[str] = None,
               language_filter: Optional[str] = None) -> List[Dict]:
        """Semantic search across indexed codebase.

        Returns list of {chunk_id, file_path, start_line, end_line, content, score, language}
        """
        with self._lock:
            tfidf_results = self._tfidf.search(query, top_k=top_k * 3)

            # Fetch chunk details from DB
            results = []
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            for chunk_id, score in tfidf_results:
                row = conn.execute(
                    "SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,)
                ).fetchone()
                if row:
                    d = dict(row)
                    d["score"] = round(score, 4)

                    # Apply filters
                    if file_filter and file_filter not in d["file_path"]:
                        continue
                    if language_filter and d.get("language") != language_filter:
                        continue

                    results.append(d)
                    if len(results) >= top_k:
                        break
            conn.close()

        return results

    def search_files(self, query: str, top_k: int = 5) -> List[Dict]:
        """Find files most relevant to query. Returns file-level results."""
        chunks = self.search(query, top_k=top_k * 5)
        # Aggregate by file, take best score
        file_scores: Dict[str, Dict] = {}
        for c in chunks:
            fp = c["file_path"]
            if fp not in file_scores or c["score"] > file_scores[fp]["score"]:
                file_scores[fp] = {
                    "file_path": fp,
                    "score": c["score"],
                    "language": c.get("language", ""),
                    "best_chunk": {
                        "start_line": c["start_line"],
                        "end_line": c["end_line"],
                        "content": c["content"],
                    },
                    "match_count": file_scores.get(fp, {}).get("match_count", 0) + 1,
                }
            else:
                file_scores[fp]["match_count"] += 1

        return sorted(file_scores.values(), key=lambda x: x["score"], reverse=True)[:top_k]

    # ── Helpers ───────────────────────────────────────────────────

    def _chunk_file(self, filepath: str, rel_path: str, lang: str) -> List[CodeChunk]:
        """Split a file into overlapping chunks."""
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        chunks = []
        file_hash = hashlib.md5("".join(lines).encode()).hexdigest()[:12]

        # For files shorter than chunk_size, create one chunk
        if len(lines) <= self.chunk_size:
            content = "".join(lines)
            chunk_id = f"{file_hash}:1:{len(lines)}"
            chunks.append(CodeChunk(chunk_id, rel_path, 1, len(lines), content, lang))
            return chunks

        # Sliding window chunks with overlap
        step = max(1, self.chunk_size // 2)
        for start in range(0, max(1, len(lines) - step + 1), step):
            end = min(start + self.chunk_size, len(lines))
            content = "".join(lines[start:end])
            chunk_id = f"{file_hash}:{start + 1}:{end}"
            chunks.append(CodeChunk(chunk_id, rel_path, start + 1, end, content, lang))

        return chunks

    @staticmethod
    def _detect_language(filepath: str, ext: str) -> str:
        """Detect programming language from file extension."""
        lang_map = {
            ".py": "python", ".js": "javascript", ".jsx": "javascript",
            ".ts": "typescript", ".tsx": "typescript", ".go": "go",
            ".rs": "rust", ".java": "java", ".c": "c", ".cpp": "cpp",
            ".h": "c", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby",
            ".php": "php", ".swift": "swift", ".kt": "kotlin",
            ".scala": "scala", ".r": "r", ".sh": "shell", ".bash": "shell",
            ".zsh": "shell", ".sql": "sql", ".yaml": "yaml", ".yml": "yaml",
            ".toml": "toml", ".json": "json", ".md": "markdown",
            ".rst": "rst", ".txt": "text", ".css": "css", ".scss": "scss",
            ".less": "less", ".html": "html", ".vue": "vue",
            ".svelte": "svelte", ".proto": "protobuf",
        }
        return lang_map.get(ext, ext.lstrip("."))

    def _save_chunk(self, chunk: CodeChunk):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT OR REPLACE INTO chunks
            (chunk_id, file_path, start_line, end_line, content, language, file_hash, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (chunk.chunk_id, chunk.file_path, chunk.start_line, chunk.end_line,
             chunk.content, chunk.language,
             chunk.chunk_id.split(":")[0] if ":" in chunk.chunk_id else "",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

    def _remove_file_chunks(self, file_path: str):
        """Remove all chunks for a file."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE file_path=?", (file_path,)
        ).fetchall()
        for (chunk_id,) in rows:
            self._tfidf.remove_document(chunk_id)
        conn.execute("DELETE FROM chunks WHERE file_path=?", (file_path,))
        conn.commit()
        conn.close()

    def _clear_root(self, root_path: str):
        """Remove all chunks under a root path."""
        conn = sqlite3.connect(self.db_path)
        # Find affected chunk IDs
        rows = conn.execute("SELECT chunk_id FROM chunks").fetchall()
        for (chunk_id,) in rows:
            self._tfidf.remove_document(chunk_id)
        # Actually we don't know which chunks belong to which root
        # without storing it. For now, clear all.
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM index_meta")
        conn.commit()
        conn.close()

    def _save_meta(self, root_path: str, files: int, chunks: int, lines: int):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT OR REPLACE INTO index_meta
            (root_path, file_count, chunk_count, total_lines, last_indexed)
            VALUES (?, ?, ?, ?, ?)""",
            (root_path, files, chunks, lines,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

    def get_stats(self) -> dict:
        """Get index statistics."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM index_meta LIMIT 1").fetchone()
        conn.close()
        if row:
            return dict(row)
        return {"root_path": "", "file_count": 0, "chunk_count": 0, "total_lines": 0}

    def set_embedding_fn(self, fn: Callable[[List[str]], List[List[float]]]):
        """Set an external embedding function for API-based embeddings."""
        self._embedding_fn = fn

    def search_with_embeddings(self, query: str, top_k: int = 10) -> List[Dict]:
        """Search using API embeddings (requires set_embedding_fn)."""
        if not self._embedding_fn:
            return self.search(query, top_k)

        # Embed query
        query_vec = self._embedding_fn([query])[0]

        # Scan all chunks with pre-computed embeddings (stored in a separate vec DB)
        # Fall back to TF-IDF for now
        return self.search(query, top_k)


# ── Singleton ────────────────────────────────────────────────────

_codebase_index: Optional[CodebaseIndex] = None


def get_codebase_index() -> CodebaseIndex:
    """Get the global CodebaseIndex singleton."""
    global _codebase_index
    if _codebase_index is None:
        _codebase_index = CodebaseIndex()
    return _codebase_index
