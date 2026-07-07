"""
core/self_model.py — WW Self-Model: Introspection of real architecture

Provides a structured view of WW's actual loaded state so the
agent can describe itself based on facts, not a hardcoded prompt.
"""

from __future__ import annotations
from typing import Dict, List, Any, Optional


class SelfModel:
    """Dynamic introspection of the running WW instance.

    Every method tries to import/lookup the real thing; falls back
    gracefully so missing optional modules don't crash the agent.
    """

    def __init__(self, tools=None, memory=None, evolution=None,
                 subconscious=None, gateways: Optional[List[str]] = None):
        self._tools = tools
        self._memory = memory
        self._evolution = evolution
        self._subconscious = subconscious
        self._gateways = gateways or []

    # ── version ────────────────────────────────────────────────

    @staticmethod
    def version() -> str:
        try:
            from core.config import VERSION
            return VERSION
        except Exception:
            pass
        try:
            with open("version.txt") as f:
                return f.read().strip()
        except Exception:
            pass
        return "0.3"

    # ── modules ────────────────────────────────────────────────

    @staticmethod
    def loaded_modules() -> Dict[str, bool]:
        """Which WW modules are loaded right now."""
        mods = {
            "spiral_loop": True,  # always
            "tools_registry": True,
            "memory_v2": False,
            "subconscious_v8": False,
            "coding": False,
            "computer_use": False,
            "evolution": False,
            "gateway_multi_platform": False,
            "wavegate_grpc": False,
            "p2p": False,
            "contacts": False,
            "webui": False,
            "scheduler": False,
            "global_workspace": False,
            "basal_ganglia": False,
        }
        for name, modpath in [
            ("coding", "coding"),
            ("wavegate_grpc", "wavegate"),
            ("p2p", "p2p"),
            ("contacts", "contacts"),
        ]:
            try:
                __import__(modpath)
                mods[name] = True
            except ImportError:
                pass

        for submod in [
            "core.subconscious",
            "core.computer_use",
            "core.global_workspace",
        ]:
            try:
                __import__(submod)
                # map dotted path to key
                key_map = {
                    "core.subconscious": "subconscious_v8",
                    "core.computer_use": "computer_use",
                    "core.global_workspace": "global_workspace",
                }
                if submod in key_map:
                    mods[key_map[submod]] = True
            except ImportError:
                pass
        return mods

    # ── tools ──────────────────────────────────────────────────

    def tool_summary(self) -> Dict[str, Any]:
        try:
            from tools.registry import default_registry
            reg = self._tools or default_registry()
            cats = reg.category_counts()
            total = sum(cats.values())
            return {
                "total": total,
                "categories": cats,
                "names": reg.tool_names() if total < 20 else [],
            }
        except Exception:
            return {"total": 0, "categories": {}, "names": []}

    # ── memory ─────────────────────────────────────────────────

    def memory_status(self) -> Dict[str, Any]:
        if self._memory is None:
            return {"available": False}
        try:
            stats = self._memory.stats()
            return {"available": True, **stats}
        except Exception:
            return {"available": True}

    # ── gateways ───────────────────────────────────────────────

    def gateway_status(self) -> Dict[str, Any]:
        platforms = []
        try:
            from gateway.bridge import GatewayBridge
            bridge = GatewayBridge.get_instance()
            for g in bridge._gateways:
                platforms.append(g.platform_name())
        except Exception:
            pass
        return {"active": self._gateways or platforms or []}

    # ── full snapshot ──────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        mods = self.loaded_modules()
        tools = self.tool_summary()
        mem = self.memory_status()
        gw = self.gateway_status()

        active_modules = [k for k, v in mods.items() if v]
        return {
            "version": self.version(),
            "modules": {
                "count": len(active_modules),
                "active": active_modules,
            },
            "tools": {
                "count": tools["total"],
                "categories": tools["categories"],
            },
            "memory": mem,
            "gateways": gw["active"],
            "subconscious_active": mods.get("subconscious_v8", False),
            "basal_ganglia_active": mods.get("basal_ganglia", False),
        }

    # ── natural-language description ──────────────────────────

    def describe(self) -> str:
        """Generate a terse identity block based on real state.

        STYLE: conversational, not documentation. The LLM mirrors this tone.
        """
        s = self.snapshot()
        import os

        provider = os.environ.get("WW_PROVIDER", "deepseek")
        model = os.environ.get("WW_MODEL", "deepseek-v4-flash")
        tool_count = s["tools"]["count"]
        active_modules = s["modules"]["active"]
        gateways = s["gateways"]

        lines = [
            f"You are Worldwave v{s['version']}.",
            f"Backend: {provider}/{model}. You are an API consumer — you do NOT own the model.",
            f"Loaded: {len(active_modules)} modules, {tool_count} tools.",
        ]
        if gateways:
            lines.append(f"Connected to: {', '.join(gateways)}.")
        lines.append("")
        lines.append(
            "RULE: Reply like a human, not a product sheet. "
            "One sentence unless asked for detail. "
            "Never dump system info unless specifically asked."
        )
        return "\n".join(lines)


# ── singleton for easy reuse ────────────────────────────────

_self_model: Optional[SelfModel] = None


def get_self_model() -> SelfModel:
    global _self_model
    if _self_model is None:
        _self_model = SelfModel()
    return _self_model


def init_self_model(tools=None, memory=None, evolution=None,
                    subconscious=None, gateways=None) -> SelfModel:
    global _self_model
    _self_model = SelfModel(
        tools=tools, memory=memory, evolution=evolution,
        subconscious=subconscious, gateways=gateways,
    )
    return _self_model
