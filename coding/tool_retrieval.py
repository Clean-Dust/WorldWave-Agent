"""ww/pm/tool_retrieval.py — Dynamic Tool Retrieval (JIT Tool Loading) v0.1

Implements Gemini's 'Tool Search' / JIT tool loading from Section 3.1.1:
- Semantic tool retrieval from large tool registries
- Only loads 3-5 tools per request instead of all ~50
- Reduces token consumption by ~85% for tool descriptions

Architecture:
  ToolRegistry — stores tool definitions with semantic tags
  ToolRetriever — semantic search over tool descriptions + examples
"""

from __future__ import annotations
import re
import math
from collections import Counter
from typing import Dict, List


# ── Simple TF-IDF Tool Retriever ──────────────────────────────────────

class ToolRetriever:
    """Retrieve relevant tools from a large registry using TF-IDF + keyword matching.

    When the agent has hundreds of tools, only the most relevant 3-5
    are loaded into context, reducing token consumption dramatically.
    """

    def __init__(self):
        self._tools: List[Dict] = []
        self._index: Dict[str, List[float]] = {}  # tool_id -> tfidf vector
        self._vocab: Dict[str, int] = {}
        self._doc_count = 0

    def register_tools(self, tools: List[Dict]):
        """Register all tools and build search index."""
        self._tools = list(tools)
        self._rebuild_index()

    def register_tool(self, tool: Dict):
        """Register a single tool."""
        self._tools.append(tool)
        self._rebuild_index()

    def retrieve(self, query: str, top_k: int = 5) -> Dict:
        """Retrieve the most relevant tools for a query.

        Args:
            query: Natural language description of what the agent wants to do
            top_k: Number of tools to return (default: 5)

        Returns:
            Dict with matched tools, count, and metadata
        """
        if not self._tools:
            return {"tools": [], "count": 0, "query": query}

        query_tokens = self._tokenize(query)
        query_vec = self._vectorize(query_tokens)

        scores = []
        for tool in self._tools:
            tid = tool.get("name", "")
            if tid in self._index:
                score = self._cosine_sim(query_vec, self._index[tid])
                if score > 0:
                    scores.append((score, tool))

        scores.sort(key=lambda x: -x[0])

        selected = [s[1] for s in scores[:top_k]]
        return {
            "tools": selected,
            "count": len(selected),
            "total_available": len(self._tools),
            "query": query,
            "token_savings": {
                "loaded": len(selected),
                "total": len(self._tools),
                "saved": len(self._tools) - len(selected),
                "savings_pct": round((1 - len(selected) / max(len(self._tools), 1)) * 100, 1),
            },
        }

    def retrieve_by_names(self, names: List[str]) -> List[Dict]:
        """Retrieve specific tools by name."""
        name_set = set(names)
        return [t for t in self._tools if t.get("name", "") in name_set]

    def get_all_tools(self) -> List[Dict]:
        return list(self._tools)

    def _rebuild_index(self):
        """Rebuild TF-IDF index for all registered tools."""
        self._vocab.clear()
        self._doc_count = len(self._tools)

        # Build term frequency per document
        doc_terms = []
        for tool in self._tools:
            text = self._tool_text(tool)
            tokens = self._tokenize(text)
            doc_terms.append(Counter(tokens))
            for token in tokens:
                if token not in self._vocab:
                    self._vocab[token] = len(self._vocab)

        # Compute TF-IDF vectors
        for i, tool in enumerate(self._tools):
            tid = tool.get("name", f"tool_{i}")
            vec = [0.0] * len(self._vocab)
            tf = doc_terms[i]

            for token, count in tf.items():
                if token in self._vocab:
                    idx = self._vocab[token]
                    df = sum(1 for dt in doc_terms if token in dt)
                    idf = math.log((self._doc_count - df + 0.5) / (df + 0.5) + 1.0)
                    vec[idx] = (1 + math.log(count)) * idf

            self._index[tid] = vec

    def _vectorize(self, tokens: List[str]) -> List[float]:
        """Create a query vector."""
        vec = [0.0] * len(self._vocab)
        tf = Counter(tokens)
        for token, count in tf.items():
            if token in self._vocab:
                idx = self._vocab[token]
                vec[idx] = 1 + math.log(count)
        return vec

    def _cosine_sim(self, a: List[float], b: List[float]) -> float:
        """Cosine similarity between two vectors."""
        dot = sum(ai * bi for ai, bi in zip(a, b))
        na = math.sqrt(sum(ai * ai for ai in a))
        nb = math.sqrt(sum(bi * bi for bi in b))
        if na == 0 or nb == 0:
            return 0
        return dot / (na * nb)

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text into lowercase terms."""
        text = text.lower()
        tokens = re.findall(r"[a-z_][a-z0-9_]{2,}", text)
        # Split camelCase and snake_case
        result = []
        for token in tokens:
            result.append(token)
            parts = re.split(r"[_]+", token)
            for p in parts:
                if len(p) > 1:
                    result.append(p)
            # Split camelCase
            parts = re.findall(r"[a-z]+|[A-Z][a-z]*", token)
            for p in parts:
                if len(p) > 1 and p.lower() != token:
                    result.append(p.lower())
        return result

    def _tool_text(self, tool: Dict) -> str:
        """Get searchable text from a tool definition."""
        text = tool.get("name", "") + " " + tool.get("description", "")
        params = tool.get("parameters", {})
        props = params.get("properties", {}) if isinstance(params, dict) else {}
        for pname, pinfo in props.items():
            if isinstance(pinfo, dict):
                text += " " + pname + " " + pinfo.get("description", "")
        examples = tool.get("examples", [])
        if examples:
            text += " " + " ".join(examples)
        return text

    @property
    def stats(self) -> Dict:
        return {
            "total_tools": len(self._tools),
            "vocab_size": len(self._vocab),
        }


_retriever: ToolRetriever = None


def get_retriever() -> ToolRetriever:
    global _retriever
    if _retriever is None:
        _retriever = ToolRetriever()
    return _retriever


def create_tool_retrieval_tools(retriever: ToolRetriever) -> List[Dict]:
    return [
        {
            "name": "coding_tool_search",
            "description": "Search for relevant tools by semantic query. Instead of loading all tools, find the 3-5 tools most relevant to your current task. Reduces token consumption by ~85%.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Describe what you want to do — finds matching tools semantically",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of tools to return (default: 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            "handler": lambda query, top_k=5: retriever.retrieve(query, top_k),
            "category": "code_tools",
        },
        {
            "name": "coding_tool_list_all",
            "description": "List all registered tool names and categories (without full descriptions). Use to discover what's available before searching.",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda: {
                "tools": [
                    {"name": t.get("name", "?"), "category": t.get("category", "?")}
                    for t in retriever.get_all_tools()
                ],
                "count": len(retriever.get_all_tools()),
            },
            "category": "code_tools",
        },
        {
            "name": "coding_tool_retrieve_by_name",
            "description": "Retrieve full definitions of specific tools by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tool names to retrieve",
                    }
                },
                "required": ["names"],
            },
            "handler": lambda names: {
                "tools": retriever.retrieve_by_names(names),
                "count": len(names),
            },
            "category": "code_tools",
        },
    ]


def get_tool_retrieval_tools() -> List[Dict]:
    return create_tool_retrieval_tools(get_retriever())
