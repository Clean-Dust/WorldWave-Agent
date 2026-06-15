"""Polymorphic CLI Compatibility Layer v0.1

Scans shell profiles (.bashrc, .zshrc, .profile) for existing AI tool
aliases, then provides a polymorphic CLI resolver that:
  1. Detects when WW is invoked through an old alias (e.g. `claude -p "...")
  2. Switches to compatibility mode matching the original tool's CLI
  3. Translates old flags to WW equivalents transparently

The compatibility wrapper is installed as shell functions during migration
so users keep their muscle memory while gaining WW's performance.
"""

from __future__ import annotations
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ww.migrate.alias")


# ── Known alias patterns ────────────────────────────────────────

KNOWN_ALIASES = {
    "claude": {
        "patterns": ["claude", "claude-code", "cc"],
        "compat_mode": "claude",
        "flag_map": {
            "-p": "--query",
            "--print": "--query",
            "--resume": "--session",
            "-c": "--continue",
            "--continue": "--session",
            "-m": "--model",
            "--model": "--model",
            "--max-turns": "--max-turns",
            "--dangerously-skip-permissions": "--yes",
            "--output-format": "--output",
            "--verbose": "--verbose",
            "-v": "--verbose",
        },
    },
    "openclaw": {
        "patterns": ["openclaw", "oc", "claw"],
        "compat_mode": "openclaw",
        "flag_map": {
            "doctor": "doctor",
            "--fix": "--fix",
            "run": "run",
            "--config": "--config",
            "--model": "--model",
        },
    },
    "hermes": {
        "patterns": ["hermes", "hm"],
        "compat_mode": "hermes",
        "flag_map": {
            "chat": "chat",
            "run": "run",
            "--model": "--model",
            "--tools": "--tools",
            "config": "config",
            "tools": "tools",
        },
    },
    "codex": {
        "patterns": ["codex", "cx"],
        "compat_mode": "codex",
        "flag_map": {
            "run": "run",
            "--model": "--model",
            "--skill": "--skill",
        },
    },
}


@dataclass
class AliasScanResult:
    """Result of scanning a shell profile for AI tool aliases."""
    path: str
    aliases_found: Dict[str, str]  # alias_name → expansion
    functions_found: Dict[str, str]  # function_name → body
    compat_wrappers_needed: List[str]  # tools that need wrapping


@dataclass
class AliasLayer:
    """Polymorphic CLI compatibility layer."""

    shell_rc_paths: List[str] = field(default_factory=lambda: [
        os.path.expanduser("~/.bashrc"),
        os.path.expanduser("~/.zshrc"),
        os.path.expanduser("~/.profile"),
        os.path.expanduser("~/.bash_aliases"),
        os.path.expanduser("~/.zsh_aliases"),
    ])

    # ── Scanning ─────────────────────────────────────────────────

    def scan_all(self) -> List[AliasScanResult]:
        """Scan all shell profiles for AI tool aliases."""
        results = []
        for rc_path in self.shell_rc_paths:
            if not os.path.isfile(rc_path):
                continue
            result = self._scan_file(rc_path)
            if result.aliases_found or result.functions_found:
                results.append(result)
        return results

    def _scan_file(self, path: str) -> AliasScanResult:
        """Parse a single shell RC file for aliases and functions."""
        aliases = {}
        functions = {}
        current_function = None
        function_body: List[str] = []

        with open(path, "r") as f:
            lines = f.readlines()

        for line in lines:
            stripped = line.strip()

            # Skip comments
            if stripped.startswith("#"):
                continue

            # Detect alias definitions: alias name='command'
            m = re.match(r"alias\s+(\w+)=['\"]?(.+?)['\"]?\s*$", stripped)
            if m:
                name = m.group(1)
                expansion = m.group(2).rstrip("'\"")
                # Only capture AI-tool related aliases
                for tool, info in KNOWN_ALIASES.items():
                    if name in info["patterns"] or any(
                        p in expansion.lower() for p in info["patterns"]
                    ):
                        aliases[name] = expansion
                        break

            # Detect shell functions
            m = re.match(r"(\w+)\s*\(\s*\)\s*\{", stripped)
            if m and current_function is None:
                current_function = m.group(1)
                function_body = []
                continue

            if current_function:
                function_body.append(line)
                if stripped == "}":
                    body = "".join(function_body)
                    # Only capture AI-tool related functions
                    for tool, info in KNOWN_ALIASES.items():
                        if current_function in info["patterns"] or any(
                            p in body.lower() for p in info["patterns"]
                        ):
                            functions[current_function] = body
                            break
                    current_function = None
                    function_body = []

        # Determine which compat wrappers are needed
        needed = []
        all_names = set(aliases.keys()) | set(functions.keys())
        for tool, info in KNOWN_ALIASES.items():
            if any(p in all_names for p in info["patterns"]):
                needed.append(tool)

        return AliasScanResult(
            path=path,
            aliases_found=aliases,
            functions_found=functions,
            compat_wrappers_needed=needed,
        )

    # ── Compatibility Wrapper Generation ─────────────────────────

    def generate_compat_wrappers(self) -> Dict[str, str]:
        """Generate shell function wrappers for each detected tool.

        Returns {tool_name: shell_function_body} for injection into .bashrc/.zshrc.
        """
        wrappers = {}

        # Claude Code wrapper
        wrappers["claude"] = '''# WW compatibility wrapper — drop-in replacement for Claude Code
claude() {
    local args=("$@")
    local compat_flags=()
    local i=1
    while [ $i -le $# ]; do
        eval "local arg=\\${$i}"
        case "$arg" in
            -p|--print)
                compat_flags+=("--query")
                i=$((i+1))
                ;;
            --resume)
                i=$((i+1))
                eval "local val=\\${$i}"
                compat_flags+=("--session" "$val")
                i=$((i+1))
                ;;
            -c|--continue)
                compat_flags+=("--session")
                i=$((i+1))
                ;;
            -m|--model)
                i=$((i+1))
                eval "local val=\\${$i}"
                compat_flags+=("--model" "$val")
                i=$((i+1))
                ;;
            --dangerously-skip-permissions)
                compat_flags+=("--yes")
                i=$((i+1))
                ;;
            -v|--verbose)
                compat_flags+=("--verbose")
                i=$((i+1))
                ;;
            *)
                compat_flags+=("$arg")
                i=$((i+1))
                ;;
        esac
    done

    ww run --compat claude "${compat_flags[@]}"
}'''

        # OpenClaw wrapper
        wrappers["openclaw"] = '''# WW compatibility wrapper — drop-in replacement for OpenClaw
openclaw() {
    ww run --compat openclaw "$@"
}
oc() { openclaw "$@"; }
claw() { openclaw "$@"; }'''

        # Hermes wrapper
        wrappers["hermes"] = '''# WW compatibility wrapper — drop-in replacement for Hermes Agent
hermes() {
    local subcommand="${1:-chat}"
    shift 2>/dev/null || true
    case "$subcommand" in
        chat|run)
            ww "$subcommand" "$@"
            ;;
        config|tools|model)
            ww "$subcommand" "$@"
            ;;
        *)
            ww run "$subcommand" "$@"
            ;;
    esac
}'''

        # Codex wrapper
        wrappers["codex"] = '''# WW compatibility wrapper — drop-in replacement for Codex
codex() {
    ww run --compat codex "$@"
}'''

        return wrappers

    def inject_compat_wrappers(self, tools: List[str]) -> List[str]:
        """Inject compatibility wrappers into all shell RC files.

        Returns list of modified file paths.
        """
        wrappers = self.generate_compat_wrappers()
        block_header = "# >>> WW compatibility wrappers (auto-generated) >>>"
        block_footer = "# <<< WW compatibility wrappers <<<"

        modified = []
        for tool in tools:
            if tool not in wrappers:
                continue

            wrapper_block = f"\n{block_header}\n"
            wrapper_block += f"# Drop-in replacement for {tool}\n"
            wrapper_block += wrappers[tool] + "\n"
            wrapper_block += f"{block_footer}\n"

            for rc_file in self.shell_rc_paths:
                if not os.path.isfile(rc_file):
                    continue

                with open(rc_file, "r") as f:
                    content = f.read()

                # Remove old block
                if block_header in content:
                    before = content.split(block_header)[0]
                    after_parts = content.split(block_footer)
                    after = after_parts[-1] if len(after_parts) > 1 else ""
                    content = before + after

                with open(rc_file, "a") as f:
                    f.write(wrapper_block)

                if rc_file not in modified:
                    modified.append(rc_file)
                logger.info("Compat wrapper for %s injected into %s", tool, rc_file)

        return modified

    # ── CLI Polymorphic Resolver ─────────────────────────────────

    def resolve_compat_mode(self, argv: List[str]) -> Tuple[Optional[str], List[str]]:
        """Detect if WW was invoked through a compat alias and translate args.

        Args:
            argv: Original command-line arguments

        Returns:
            (compat_mode, translated_argv) — compat_mode is None if not detected
        """
        if not argv or len(argv) < 1:
            return None, argv

        # Check if argv[0] matches a known tool
        prog = os.path.basename(argv[0])
        for tool, info in KNOWN_ALIASES.items():
            if prog in info["patterns"]:
                translated = self._translate_flags(tool, argv[1:])
                return info["compat_mode"], translated

        return None, argv

    def _translate_flags(self, tool: str, args: List[str]) -> List[str]:
        """Translate old tool flags to WW equivalents."""
        info = KNOWN_ALIASES.get(tool)
        if not info:
            return list(args)

        flag_map = info["flag_map"]
        translated = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in flag_map:
                translated.append(flag_map[arg])
                i += 1
            elif arg.startswith("--") and "=" in arg:
                # Handle --flag=value format
                flag, _, value = arg.partition("=")
                if flag in flag_map:
                    translated.append(f"{flag_map[flag]}={value}")
                else:
                    translated.append(arg)
                i += 1
            else:
                translated.append(arg)
                i += 1

        return translated
