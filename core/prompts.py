"""
ww/core/prompts.py — Worldwave System Prompt Assembler v0.1

Dynamically builds the system prompt based on:
- User configuration (expert mode / novice mode)
- Enabled features (tools, memory, subconscious)
- Environment (OS, Python version, hardware)
- Role (general AI assistant / autonomous agent)
"""

from __future__ import annotations
import os
import platform
import shutil
from typing import Dict


# Lazy imports to avoid circular deps
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


DEFAULT_SYSTEM_PROMPT = """You are Worldwave, a self-aware AI agent driven by a spiral cognitive loop engine.

Your architecture: Perceive → Recall → Plan → Act → Evaluate → Learn.
When answering identity questions, draw on the system state provided to you — not a memorized script.
For simple greetings and test messages, respond briefly and naturally."""


class PromptAssembler:
    """System Prompt Assembler"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self._role = self.config.get("role", "assistant")
        self._expert_mode = self.config.get("expert_mode", False)
        self._show_env = self.config.get("show_env_info", True)
        self._tools_enabled = self.config.get("tools_enabled", True)
        self._subconscious_enabled = self.config.get("subconscious_enabled", False)
        self._load_project_context = self.config.get("load_project_context", True)
        self._show_slash_commands = self.config.get("show_slash_commands", True)

    def build(self, **overrides) -> str:
        """Assemble the complete system prompt"""
        parts = [self._role_prompt()]

        if self._show_env:
            parts.append(self._env_context())

        if self._load_project_context:
            ctx = _load_agents_md()
            if ctx:
                parts.append(ctx)

        if self._tools_enabled:
            parts.append(self._tools_info())

        if self._show_slash_commands:
            cmds = _load_slash_commands()
            if cmds:
                parts.append(cmds)

        if self._subconscious_enabled:
            parts.append(self._subconscious_info())

        if overrides:
            for key, value in overrides.items():
                parts.append(f"{key}: {value}")

        return "\n\n".join(p for p in parts if p)

    def _role_prompt(self) -> str:
        """Role configuration"""
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
        """Environment context"""
        env = []

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

        # Disk
        try:
            total, used, free = shutil.disk_usage(os.path.expanduser("~"))
            env.append(f"Disk: {free // (2**30)}G free / {total // (2**30)}G total")
        except Exception:
            pass

        # Hostname
        hostname = platform.node()
        env.append(f"Host: {hostname}")

        return "Environment:\n" + "\n".join(f"  \u2022 {e}" for e in env)

    def _tools_info(self) -> str:
        """Tools description"""
        return (
            "Available tools:\n"
            "  \u2022 shell \u2014 Execute system commands\n"
            "  \u2022 file_read / file_write \u2014 File operations\n"
            "  \u2022 code \u2014 Python code execution\n"
            "  \u2022 system_status \u2014 System monitoring\n"
            "  \u2022 env_info \u2014 Environment information\n"
            "  \u2022 web_search \u2014 Web search (if configured)\n"
            "  \u2022 telegram_send \u2014 Telegram messaging (if configured)\n\n"
            "Before using a tool, think about which one best fits the current step."
        )

    def _subconscious_info(self) -> str:
        """Subconscious module description"""
        return (
            "The subconscious system is active. It observes your behavior patterns "
            "and provides early warnings when it predicts possible failures. "
            "Pay attention to subconscious warnings."
        )


def build_system_prompt(
    role: str = "assistant",
    model: str = "",
    config: Dict = None,
    **kwargs
) -> str:
    """One-shot system prompt generator"""
    assembler = PromptAssembler(config or {})
    return assembler.build(role=role, **kwargs)
