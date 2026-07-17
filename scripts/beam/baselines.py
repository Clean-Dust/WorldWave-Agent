"""B1 context-only and B2 simple BM25 RAG baselines (no secret deps)."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Sequence, Tuple

_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.I)


def tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def chunk_text(text: str, chunk_chars: int = 800, overlap: int = 100) -> List[str]:
    text = text or ""
    if not text:
        return []
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [text]
    chunks = []
    step = max(1, chunk_chars - max(0, overlap))
    i = 0
    while i < len(text):
        chunks.append(text[i : i + chunk_chars])
        i += step
    return chunks


class SimpleBM25:
    """Minimal BM25 (Okapi-style) for in-repo RAG baseline."""

    def __init__(self, corpus: Sequence[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = [tokenize(d) for d in corpus]
        self.N = len(self.docs) or 1
        self.avgdl = sum(len(d) for d in self.docs) / self.N
        self.df: Counter = Counter()
        for d in self.docs:
            for t in set(d):
                self.df[t] += 1

    def _idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def score(self, query: str, idx: int) -> float:
        q = tokenize(query)
        doc = self.docs[idx]
        if not doc:
            return 0.0
        tf = Counter(doc)
        dl = len(doc)
        s = 0.0
        for t in q:
            if t not in tf:
                continue
            idf = self._idf(t)
            f = tf[t]
            denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
            s += idf * (f * (self.k1 + 1)) / (denom or 1.0)
        return s

    def top_k(self, query: str, k: int = 5) -> List[Tuple[int, float]]:
        scored = [(i, self.score(query, i)) for i in range(len(self.docs))]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: max(1, k)]


def b1_context_prompt(
    question: str,
    chat_blob: str,
    max_chars: int = 12000,
) -> str:
    blob = chat_blob or ""
    if max_chars > 0 and len(blob) > max_chars:
        blob = blob[-max_chars:]
    return (
        "You are answering from the conversation context only. "
        "Do not invent facts not present in the context.\n\n"
        f"=== CONVERSATION CONTEXT ===\n{blob}\n\n"
        f"=== QUESTION ===\n{question}\n\n"
        "Answer:"
    )


def b2_rag_prompt(
    question: str,
    chat_blob: str,
    top_k: int = 5,
    chunk_chars: int = 800,
    max_context_chars: int = 6000,
) -> str:
    chunks = chunk_text(chat_blob, chunk_chars=chunk_chars)
    if not chunks:
        return b1_context_prompt(question, chat_blob, max_chars=max_context_chars)
    bm25 = SimpleBM25(chunks)
    hits = bm25.top_k(question, k=top_k)
    selected = []
    total = 0
    for idx, _sc in hits:
        c = chunks[idx]
        if total + len(c) > max_context_chars and selected:
            break
        selected.append(c)
        total += len(c)
    ctx = "\n\n---\n\n".join(selected)
    return (
        "You are answering using retrieved conversation snippets (BM25). "
        "Prefer evidence from the snippets; refuse if insufficient.\n\n"
        f"=== RETRIEVED SNIPPETS ===\n{ctx}\n\n"
        f"=== QUESTION ===\n{question}\n\n"
        "Answer:"
    )
