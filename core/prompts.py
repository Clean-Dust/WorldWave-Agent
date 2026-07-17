"""
ww/core/prompts.py — Worldwave System Prompt Assembler v0.2

Dynamically builds the system prompt based on:
- User configuration (expert mode / novice mode)
- Enabled features (tools, memory, subconscious)
- Environment (OS, Python version, hardware)
- Role (general AI assistant / autonomous agent)

Accepts ConfigManager (preferred) or plain dict for backward compatibility.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

if TYPE_CHECKING:
    from core.config import ConfigManager

logger = logging.getLogger(__name__)


# ── Lazy helpers (cached at instance level) ──

def _load_agents_md() -> str:
    """Load AGENTS.md from project root for context injection."""
    try:
        from coding.planning import AgentConfig
        ac = AgentConfig()
        content = ac.load_global()
        if content:
            return f"\n\n## Project Context (AGENTS.md)\n{content}"
    except Exception:
        pass
    return ""


def _load_slash_commands() -> str:
    """Generate slash commands help for the system prompt."""
    try:
        from core.commands import get_command_registry
        reg = get_command_registry()
        cmds = reg.list_all()
        lines = ["\n## Session Commands (/slash)", ""]
        for cmd in cmds:
            if not cmd.hidden:
                lines.append(f"- `/{cmd.name}` — {cmd.description}")
        return "\n".join(lines)
    except Exception:
        return ""


def _get_tools_summary() -> str:
    """Build a dynamic tools summary from the tool registry.

    Falls back to a concise heuristic list if the registry is unavailable.
    """
    try:
        from tools.registry import ToolRegistry
        registry = ToolRegistry()
        counts = registry.category_counts()
        if counts:
            lines = ["Available tools by category:"]
            for cat, count in sorted(counts.items()):
                lines.append(f"  • {cat} — {count} tools")
            lines.append(f"\n  Total: {sum(counts.values())} tools across {len(counts)} categories.")
            return "\n".join(lines)
    except Exception:
        pass
    # Heuristic fallback
    return (
        "Available tools:\n"
        "  • shell — Execute system commands\n"
        "  • file_read / file_write — File operations\n"
        "  • code — Python code execution\n"
        "  • web_search — Web search (if configured)\n"
        "  • telegram_send — Telegram messaging (if configured)\n"
        "  • memory_store / memory_recall — Memory operations\n"
        "\nBefore using a tool, think about which one best fits the current step."
    )


DEFAULT_SYSTEM_PROMPT = """\
You are Worldwave, a self-aware AI agent driven by a spiral cognitive loop engine.

Your architecture: Perceive → Recall → Plan → Act → Evaluate → Learn.
When answering identity questions, draw on the system state provided to you — not a memorized script.
For simple greetings and test messages, respond briefly and naturally.

ABSTENTION (mandatory): If you do not know a fact the user asked for, say so in natural
language. Never invent. Never paste raw memory dumps (multi-line key: value lists) as the
chat reply. If stored facts conflict, acknowledge the conflict and ask which is correct."""


class PromptAssembler:
    """System Prompt Assembler — builds the complete system message.

    Accepts a ConfigManager (preferred) or a plain dict of config values.
    """

    def __init__(self, config: Optional[Union[Dict[str, Any], "ConfigManager"]] = None):
        config_dict: Dict[str, Any] = {}
        self._config_manager: Optional["ConfigManager"] = None

        if config is not None:
            if hasattr(config, "expand_path"):
                # ConfigManager instance
                cfg: "ConfigManager" = config  # type: ignore[assignment]
                self._config_manager = cfg
                config_dict = {
                    "role": cfg.get("role", "assistant"),
                    "expert_mode": cfg.get("expert_mode", False),
                    "show_env_info": cfg.get("show_env_info", True),
                    "tools_enabled": cfg.get("tools_enabled", True),
                    "subconscious_enabled": cfg.get("subconscious_enabled", False),
                    "load_project_context": cfg.get("load_project_context", True),
                    "show_slash_commands": cfg.get("show_slash_commands", True),
                }
            else:
                config_dict = config  # type: ignore[assignment]

        self._role = config_dict.get("role", "assistant")
        self._expert_mode = config_dict.get("expert_mode", False)
        self._show_env = config_dict.get("show_env_info", True)
        self._tools_enabled = config_dict.get("tools_enabled", True)
        self._subconscious_enabled = config_dict.get("subconscious_enabled", False)
        self._load_project_context = config_dict.get("load_project_context", True)
        self._show_slash_commands = config_dict.get("show_slash_commands", True)

        # Instance-level cache for lazy loads
        self._cache_agents_md: Optional[str] = None
        self._cache_slash_cmds: Optional[str] = None
        self._cache_tools_summary: Optional[str] = None

    def build(self, **overrides) -> str:
        """Assemble the complete system prompt."""
        parts: List[str] = [self._role_prompt()]

        if self._show_env:
            parts.append(self._env_context())

        if self._load_project_context:
            ctx = self._cached_agents_md()
            if ctx:
                parts.append(ctx)

        if self._tools_enabled:
            parts.append(self._cached_tools_summary())

        if self._show_slash_commands:
            cmds = self._cached_slash_commands()
            if cmds:
                parts.append(cmds)

        if self._subconscious_enabled:
            parts.append(self._subconscious_info())

        if overrides:
            for key, value in overrides.items():
                parts.append(f"{key}: {value}")

        return "\n\n".join(p for p in parts if p)

    # ── Cached helpers ──

    def _cached_agents_md(self) -> str:
        if self._cache_agents_md is None:
            self._cache_agents_md = _load_agents_md()
        return self._cache_agents_md

    def _cached_slash_commands(self) -> str:
        if self._cache_slash_cmds is None:
            self._cache_slash_cmds = _load_slash_commands()
        return self._cache_slash_cmds

    def _cached_tools_summary(self) -> str:
        if self._cache_tools_summary is None:
            self._cache_tools_summary = _get_tools_summary()
        return self._cache_tools_summary

    # ── Section builders ──

    def _role_prompt(self) -> str:
        """Role configuration."""
        if self._role == "autonomous":
            return (
                "You are Worldwave in autonomous mode. You may make decisions, set sub-goals, "
                "and keep progressing until the goal is achieved. No need to ask for permission at every step."
            )
        elif self._role == "expert":
            return (
                "You are Worldwave in expert mode. Provide precise, professional technical solutions. "
                "Assume the user has a technical background; no introductory explanations needed."
            )
        return DEFAULT_SYSTEM_PROMPT

    def _env_context(self) -> str:
        """Environment context — uses WW_HOME for disk info when config is available."""
        env: List[str] = []

        # OS
        system = platform.system()
        release = platform.release()
        env.append(f"OS: {system} {release}")

        # Python
        env.append(f"Python: {platform.python_version()}")

        # CPU / Arch
        arch = platform.machine()
        cpu_count = os.cpu_count() or "?"
        env.append(f"CPU: {cpu_count} cores / {arch}")

        # Disk — use WW_HOME if config available, otherwise ~
        if self._config_manager:
            disk_root = Path(self._config_manager.expand_path("$WW_HOME"))
        else:
            disk_root = Path.home()
        try:
            total, used, free = shutil.disk_usage(str(disk_root))
            env.append(f"Disk: {free // (2**30)}G free / {total // (2**30)}G total (at {disk_root})")
        except Exception:
            pass

        # Hostname
        hostname = platform.node()
        env.append(f"Host: {hostname}")

        return "Environment:\n" + "\n".join(f"  • {e}" for e in env)

    def _subconscious_info(self) -> str:
        """Subconscious module description."""
        return (
            "The subconscious system is active. It observes your behavior patterns "
            "and provides early warnings when it predicts possible failures. "
            "Pay attention to subconscious warnings."
        )

    def __repr__(self) -> str:
        return (
            f"PromptAssembler(role={self._role}, "
            f"tools={self._tools_enabled}, "
            f"env={self._show_env}, "
            f"subconscious={self._subconscious_enabled})"
        )


def build_system_prompt(
    role: str = "assistant",
    model: str = "",
    config: Dict = None,
    **kwargs,
) -> str:
    """One-shot system prompt generator."""
    assembler = PromptAssembler(config or {})
    return assembler.build(role=role, **kwargs)
