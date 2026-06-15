"""Slash Command Semantic Inheritance v0.1

Maps old-framework slash commands to WW equivalents so users keep
their muscle memory. Written to ~/.worldwave/slash_compat.json during
migration and loaded by the gateway at runtime.

Gemini Pillar 9: "Daily operation flow — semantic inheritance, performance transcendence"
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Command mapping tables ────────────────────────────────────────

CLAUDE_SLASH_MAP = {
    # Session management
    "/compact": {
        "ww_action": "compact",
        "description": "Compress conversation context (WW uses semantic compression, "
                       "not brute truncation)",
    },
    "/resume": {
        "ww_action": "session_resume",
        "description": "Resume a previous session",
    },
    "/clear": {
        "ww_action": "session_clear",
        "description": "Start a fresh conversation",
    },
    # Cost & budget
    "/cost": {
        "ww_action": "usage_stats",
        "description": "Show detailed usage statistics (tokens, cost, per-model breakdown)",
    },
    # Planning & workflow
    "/plan": {
        "ww_action": "plan",
        "description": "Enter planning mode — generate implementation plan before coding",
    },
    "/review": {
        "ww_action": "review",
        "description": "Code review with subagent isolation (no context pollution)",
    },
    # Model management
    "/model": {
        "ww_action": "model_switch",
        "description": "Switch models (Alt+P equivalent)",
    },
    # Agent management
    "/agents": {
        "ww_action": "subagent_list",
        "description": "List/manage subagents with per-agent resource quotas",
    },
    "/delegate": {
        "ww_action": "delegate",
        "description": "Delegate a task to a subagent",
    },
    # Connectivity
    "/remote-control": {
        "ww_action": "remote_control",
        "description": "Expose session for remote connection (--teleport equivalent)",
    },
    # Permissions
    "/permissions": {
        "ww_action": "permissions",
        "description": "Manage tool permission rules",
    },
    # Custom
    "/init": {
        "ww_action": "project_init",
        "description": "Initialize project-level WW config (CLAUDE.md equivalent)",
    },
    "/doctor": {
        "ww_action": "health_check",
        "description": "Run system health checks",
    },
}

HERMES_SLASH_MAP = {
    "/tools": {
        "ww_action": "tools_list",
        "description": "List available tools",
    },
    "/config": {
        "ww_action": "config_show",
        "description": "Show current configuration",
    },
    "/memory": {
        "ww_action": "memory_search",
        "description": "Search persistent memory",
    },
    "/skills": {
        "ww_action": "skills_list",
        "description": "List available skills",
    },
    "/profile": {
        "ww_action": "profile_switch",
        "description": "Switch agent profile",
    },
    "/new": {
        "ww_action": "session_new",
        "description": "Start a new session",
    },
}

OPENCLAW_SLASH_MAP = {
    "/doctor": {
        "ww_action": "health_check",
        "description": "Validate config and detect drift",
    },
    "/config": {
        "ww_action": "config_show",
        "description": "Show current configuration",
    },
    "/tools": {
        "ww_action": "tools_list",
        "description": "List available tools",
    },
}

CODEX_SLASH_MAP = {
    "/skill": {
        "ww_action": "skill_load",
        "description": "Load a skill by name",
    },
    "/skills": {
        "ww_action": "skills_list",
        "description": "List available skills",
    },
    "/scope": {
        "ww_action": "scope_show",
        "description": "Show current RBAC scope level",
    },
}

# ── Aggregated compatibility table ────────────────────────────────

SOURCE_MAPS = {
    "claude": CLAUDE_SLASH_MAP,
    "hermes": HERMES_SLASH_MAP,
    "openclaw": OPENCLAW_SLASH_MAP,
    "codex": CODEX_SLASH_MAP,
}


@dataclass
class SlashCompat:
    """Generates and writes slash-command compatibility config."""

    sources: List[str] = field(default_factory=list)

    def generate(self, sources: Optional[List[str]] = None) -> Dict[str, Any]:
        """Generate the full slash-command compatibility mapping.

        Returns a dict suitable for writing to ~/.worldwave/slash_compat.json
        """
        if sources is None:
            sources = list(SOURCE_MAPS.keys())

        compat_map: Dict[str, Dict[str, Any]] = {}

        for source in sources:
            source_map = SOURCE_MAPS.get(source, {})
            for old_cmd, ww_info in source_map.items():
                if old_cmd not in compat_map:
                    compat_map[old_cmd] = {
                        "ww_action": ww_info["ww_action"],
                        "sources": [source],
                        "description": ww_info["description"],
                    }
                else:
                    # Multiple sources have same command — merge sources
                    compat_map[old_cmd]["sources"].append(source)

        return {
            "version": 1,
            "description": "WW slash-command compatibility map — auto-generated by migration engine",
            "commands": compat_map,
        }

    def write(self, sources: Optional[List[str]] = None) -> Optional[str]:
        """Generate and write the compat map to disk.

        Returns the path written, or None on failure.
        """
        import json
        import os

        compat = self.generate(sources)
        if not compat.get("commands"):
            return None

        path = os.path.expanduser("~/.worldwave/slash_compat.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        # Merge with existing if present
        existing_commands = {}
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    old = json.load(f)
                    existing_commands = old.get("commands", {})
            except (json.JSONDecodeError, FileNotFoundError):
                pass

        # Newer entries overwrite older ones
        existing_commands.update(compat["commands"])
        compat["commands"] = existing_commands

        with open(path, "w") as f:
            json.dump(compat, f, indent=2)

        return path


def install_slash_compat(sources: List[str]) -> int:
    """Install slash-command compatibility during migration.

    Args:
        sources: List of source systems (e.g. ["claude", "hermes"])

    Returns:
        Number of commands mapped
    """
    compat = SlashCompat(sources=sources)
    path = compat.write(sources)
    if path:
        cmd_count = len(compat.generate(sources).get("commands", {}))
        return cmd_count
    return 0
