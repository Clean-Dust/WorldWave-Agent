"""ww/coding/__init__.py — WorldWave Programming Module (WW-PM)

Default engineering harness (productized path PM 0.11):
  coding mode auto → orchestrator (map/grep/graph → edit → verify → circuit)
  → steerable redirect → autocompact → require-test default ON.
  Live multi-turn, corpus stress, model route, CodingMetrics, loop bridge.
  Coding arena (hidden-test pass@1 vs reference baseline).
"""

from __future__ import annotations
from typing import Dict, List, Optional

# Version
PM_VERSION = "0.11.0"


# ── Lazy-init module singletons ───────────────────────────────────────

_aci_tools: List[Dict] = None
_shell_tools: List[Dict] = None
_planning_tools: List[Dict] = None
_search_tools: List[Dict] = None
_rag_tools: List[Dict] = None
_lsp_tools: List[Dict] = None
_circuit_tools: List[Dict] = None
_sandbox_tools: List[Dict] = None
_tool_retrieval_tools: List[Dict] = None
_dense_tools: List[Dict] = None
_allure_tools: List[Dict] = None
_debug_tools: List[Dict] = None
_graph_tools: List[Dict] = None
_perception_tools: List[Dict] = None
_harness_tools: List[Dict] = None
_orchestrator_tools: List[Dict] = None
_autocompact_tools: List[Dict] = None
_retriever_populated: bool = False


def register_tools(registry):
    """Register WW-PM tools in the WW tool registry.

    Honors per-tool ``permission`` key (does not force all SAFE).
    """
    from tools.registry import ToolDef, PERMISSION_SAFE

    tools = get_all_tools()
    for t in tools:
        perm = t.get("permission", PERMISSION_SAFE)
        registry.register(ToolDef(
            t["name"],
            t["description"],
            t["handler"],
            parameters=t.get("parameters", {}),
            category=t.get("category", "code_aci"),
            permission=perm,
        ))
    return len(tools)


def get_all_tools() -> List[Dict]:
    """Get all WW-PM tool definitions for registration.

    Coding tool handlers are wrapped with microcompact so model-bound
    outputs stay length-bounded with fingerprints.
    """
    global _aci_tools, _shell_tools, _planning_tools, _search_tools, _rag_tools, _lsp_tools, _circuit_tools, _sandbox_tools
    global _tool_retrieval_tools, _dense_tools, _retriever_populated
    global _allure_tools, _debug_tools, _graph_tools, _perception_tools, _harness_tools
    global _orchestrator_tools, _autocompact_tools

    tools = []

    if _aci_tools is None:
        from coding.aci import get_aci_tools
        _aci_tools = get_aci_tools()
    tools.extend(_aci_tools)

    if _shell_tools is None:
        from coding.shell import get_shell_tools
        _shell_tools = get_shell_tools()
    tools.extend(_shell_tools)

    if _planning_tools is None:
        from coding.planning import get_planning_tools
        _planning_tools = get_planning_tools()
    tools.extend(_planning_tools)

    if _search_tools is None:
        from coding.code_search import create_code_search_tools
        _search_tools = create_code_search_tools()
    tools.extend(_search_tools)

    if _rag_tools is None:
        from coding.code_rag import get_rag_tools
        _rag_tools = get_rag_tools()
    tools.extend(_rag_tools)

    if _lsp_tools is None:
        from coding.lsp import get_lsp_tools
        _lsp_tools = get_lsp_tools()
    tools.extend(_lsp_tools)

    if _circuit_tools is None:
        from coding.circuit import get_circuit_tools
        _circuit_tools = get_circuit_tools()
    tools.extend(_circuit_tools)

    if _sandbox_tools is None:
        from coding.sandbox import get_sandbox_tools
        _sandbox_tools = get_sandbox_tools()
    tools.extend(_sandbox_tools)

    if _graph_tools is None:
        from coding.code_graph import get_code_graph_tools
        _graph_tools = get_code_graph_tools()
    tools.extend(_graph_tools)

    if _perception_tools is None:
        from coding.perception import get_perception_tools
        _perception_tools = get_perception_tools()
    tools.extend(_perception_tools)

    if _harness_tools is None:
        from coding.harness import get_harness_tools
        _harness_tools = get_harness_tools()
    tools.extend(_harness_tools)

    if _orchestrator_tools is None:
        from coding.orchestrator import get_orchestrator_tools
        _orchestrator_tools = get_orchestrator_tools()
    tools.extend(_orchestrator_tools)

    if _autocompact_tools is None:
        from coding.autocompact import get_autocompact_tools
        _autocompact_tools = get_autocompact_tools()
    tools.extend(_autocompact_tools)

    # Tool retrieval and dense vector depend on the full tool list
    # Populate retriever with all tools after they're collected
    if _tool_retrieval_tools is None:
        from coding.tool_retrieval import get_tool_retrieval_tools, get_retriever
        _tool_retrieval_tools = get_tool_retrieval_tools()
    if not _retriever_populated and tools:
        from coding.tool_retrieval import get_retriever
        get_retriever().register_tools(tools)
        _retriever_populated = True
    tools.extend(_tool_retrieval_tools)

    if _dense_tools is None:
        from coding.dense_vector import get_dense_tools
        _dense_tools = get_dense_tools()
    tools.extend(_dense_tools)

    if _allure_tools is None:
        from coding.allure import get_allure_tools
        _allure_tools = get_allure_tools()
    tools.extend(_allure_tools)

    if _debug_tools is None:
        from coding.debug_integration import get_debug_tools
        _debug_tools = get_debug_tools()
    tools.extend(_debug_tools)

    # Microcompact all coding_* tool outputs bound for the model
    from coding.microcompact import wrap_tools
    tools = wrap_tools(tools, prefix="coding_")

    return tools


def get_tool_count() -> int:
    return len(get_all_tools())


def get_status() -> Dict:
    """Get WW-PM module status."""
    return {
        "version": PM_VERSION,
        "tools_available": get_tool_count(),
        "modules": [
            "aci",
            "shell",
            "planning",
            "code_search",
            "code_rag",
            "lsp",
            "circuit",
            "sandbox",
            "tool_retrieval",
            "dense_vector",
            "allure",
            "debug_integration",
            "progressive",
            "treesitter_engine",
            "code_graph",
            "microcompact",
            "policy",
            "perception",
            "harness",
            "mode",
            "orchestrator",
            "autocompact",
            "model_route",
            "loop_bridge",
        ],
        "defaults": {
            "require_test": True,
            "samples": 0,
            "role": "coder",
            "max_tool_rounds": 20,
            "max_same_fp": 3,
            "live_llm": False,
        },
    }
