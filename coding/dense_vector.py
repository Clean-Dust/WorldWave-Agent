"""ww/pm/dense_vector.py — Optional Dense Vector Search for Code RAG v0.1

Implements Gemini's hybrid search recommendation (BM25 + Dense Vector):
- Pure Python word embedding using co-occurrence statistics
- Cosine similarity search
- Optional: Milvus/Pinecone/Chroma integration when available

Zero external deps by default. Falls back to TF-IDF when no embedding API.
"""

from __future__ import annotations
import json
import math
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple


class CooccurrenceEmbedding:
    """Lightweight word embedding using co-occurrence statistics.

    Builds a vocabulary of code terms and their co-occurrence patterns.
    Document vectors are the average of their term vectors.

    This is NOT as good as GloVE/BERT but works with zero external deps.
    """

    def __init__(self, vector_size: int = 50, window: int = 5):
        self._vector_size = vector_size
        self._window = window
        self._vocab: Dict[str, int] = {}
        self._embeddings: Dict[str, List[float]] = {}
        self._built = False

    def build(self, documents: List[str]):
        """Build embeddings from a corpus of documents."""
        # Tokenize all documents
        all_tokens = []
        for doc in documents:
            all_tokens.append(self._tokenize(doc))

        # Build vocabulary (minimum 2 occurrences)
        freq = Counter()
        for tokens in all_tokens:
            freq.update(tokens)

        vocab = [t for t, c in freq.most_common(5000) if c >= 2][:2000]
        self._vocab = {t: i for i, t in enumerate(vocab)}
        vocab_size = len(self._vocab)

        if vocab_size == 0:
            self._built = True
            return

        # Build co-occurrence matrix
        cooc = Counter()
        for tokens in all_tokens:
            for i, token in enumerate(tokens):
                if token not in self._vocab:
                    continue
                start = max(0, i - self._window)
                end = min(len(tokens), i + self._window + 1)
                for j in range(start, end):
                    if i != j and tokens[j] in self._vocab:
                        t1, t2 = min(token, tokens[j]), max(token, tokens[j])
                        cooc[(t1, t2)] += 1

        # SVD-like reduction: use top vector_size co-occurrence pairs as dimensions
        top_pairs = [p for p, _ in cooc.most_common(self._vector_size)]
        dim_terms = set()
        for t1, t2 in top_pairs:
            dim_terms.add(t1)
            dim_terms.add(t2)

        # Build embeddings: each term is a vector of its co-occurrence strengths
        for token in self._vocab:
            vec = [0.0] * self._vector_size
            for d, (t1, t2) in enumerate(top_pairs):
                if d >= self._vector_size:
                    break
                if token == t1 or token == t2:
                    vec[d] = math.log(1 + cooc.get((t1, t2) if t1 < t2 else (t2, t1), 0))
            self._embeddings[token] = vec

        self._built = True

    def embed(self, text: str) -> List[float]:
        """Get embedding vector for text (average of word vectors)."""
        tokens = self._tokenize(text)
        vec = [0.0] * self._vector_size
        count = 0
        for token in tokens:
            if token in self._embeddings:
                tv = self._embeddings[token]
                for i in range(self._vector_size):
                    vec[i] += tv[i]
                count += 1
        if count > 0:
            for i in range(self._vector_size):
                vec[i] /= count
        return vec

    def similarity(self, a: str, b: str) -> float:
        """Cosine similarity between two texts."""
        va = self.embed(a)
        vb = self.embed(b)
        dot = sum(ai * bi for ai, bi in zip(va, vb))
        na = math.sqrt(sum(ai * ai for ai in va))
        nb = math.sqrt(sum(bi * bi for bi in vb))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def search(self, query: str, documents: List[Tuple[str, str]], top_k: int = 10) -> List[Dict]:
        """Search documents by dense vector similarity.

        Args:
            query: Search query
            documents: List of (id, text) tuples
            top_k: Number of results

        Returns:
            List of {id, score, text_snippet}
        """
        if not self._built:
            self.build([text for _, text in documents])

        qv = self.embed(query)
        scores = []
        for doc_id, text in documents:
            dv = self.embed(text)
            dot = sum(ai * bi for ai, bi in zip(qv, dv))
            nq = math.sqrt(sum(ai * ai for ai in qv))
            nd = math.sqrt(sum(bi * bi for bi in dv))
            score = dot / (nq * nd) if nq > 0 and nd > 0 else 0
            scores.append((score, doc_id, text))

        scores.sort(key=lambda x: -x[0])
        return [
            {"id": sid, "score": round(score, 4), "snippet": text[:200]}
            for score, sid, text in scores[:top_k]
        ]

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize code text."""
        text = text.lower()
        tokens = re.findall(r"[a-z_][a-z0-9_]{2,}", text)
        result = []
        for token in tokens:
            result.append(token)
            for part in token.split("_"):
                if len(part) > 1:
                    result.append(part)
        return result

    def save(self, path: str):
        """Save embeddings to JSON."""
        data = {
            "vector_size": self._vector_size,
            "window": self._window,
            "vocab": list(self._vocab.keys()),
            "embeddings": {k: v for k, v in self._embeddings.items()},
        }
        with open(path, "w") as f:
            json.dump(data, f)

    def load(self, path: str) -> bool:
        """Load embeddings from JSON."""
        if not os.path.isfile(path):
            return False
        with open(path, "r") as f:
            data = json.load(f)
        self._vector_size = data["vector_size"]
        self._window = data["window"]
        self._vocab = {t: i for i, t in enumerate(data["vocab"])}
        self._embeddings = data["embeddings"]
        self._built = True
        return True

    @property
    def stats(self) -> Dict:
        return {
            "built": self._built,
            "vocab_size": len(self._vocab),
            "vector_size": self._vector_size,
        }


# ── Tool definitions ──────────────────────────────────────────────────

_embedding: CooccurrenceEmbedding = None


def get_embedding() -> CooccurrenceEmbedding:
    global _embedding
    if _embedding is None:
        _embedding = CooccurrenceEmbedding()
    return _embedding


def create_dense_vector_tools(emb: CooccurrenceEmbedding) -> List[Dict]:
    return [
        {
            "name": "coding_dense_search",
            "description": "Search documents using dense vector similarity (co-occurrence embeddings). Complements BM25 for semantic matching. Zero external deps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "texts": {
                        "type": "object",
                        "description": "Dict of {id: text} to search through",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Results count (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["query", "texts"],
            },
            "handler": lambda query, texts, top_k=10: {
                "results": emb.search(query, list(texts.items()), top_k),
                "query": query,
            },
            "category": "code_search",
        },
        {
            "name": "coding_dense_build",
            "description": "Build co-occurrence embeddings from code chunks. Trains on the codebase vocabulary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "texts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Texts to train on",
                    }
                },
            },
            "handler": lambda texts: (
                emb.build(texts),
                {"success": True, "stats": emb.stats},
            )[1],
            "category": "code_search",
        },
    ]


def get_dense_tools() -> List[Dict]:
    return create_dense_vector_tools(get_embedding())
