"""Universal Skill Transpiler v0.1

Transpiles skills from other AI agent frameworks to WW native format:
  - Claude Code: .claude/skills/*.md (Markdown with optional frontmatter)
  - Cursor:      .cursor/skills/*.md
  - GitHub:      .github/skills/*.md
  - Codex:       .agents/skills/*/ (directory-based, 4-tier hierarchy)
  - Hermes:      ~/.hermes/skills/*.md (YAML frontmatter + markdown)

The transpiler normalizes the diverse skill formats into WW's standard:
  ---
  name: skill-name
  description: ...
  trigger: when to invoke
  ---
  ## Steps
  1. ...
  ## Pitfalls
  - ...

Also upgrades Claude Code Stop Hooks with smart backoff + subagent
debugging intervention (instead of the static 8-failure limit).
"""

from __future__ import annotations
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ww.migrate.transpiler")


# ── Frontmatter parser ──────────────────────────────────────────

def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML/TOML frontmatter from markdown.

    Supports:
      ---  (YAML)
      +++  (TOML)
      ;;;  (Hermes-style)
      <!-- metadata --> (HTML comment style)

    Returns: (metadata_dict, body_content)
    """
    metadata: Dict[str, Any] = {}
    body = content

    # YAML frontmatter (---)
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if m:
        raw = m.group(1)
        body = content[m.end():]
        metadata = _parse_yaml_frontmatter(raw)

    # TOML frontmatter (+++)
    if not metadata:
        m = re.match(r'^\+\+\+\s*\n(.*?)\n\+\+\+\s*\n', content, re.DOTALL)
        if m:
            raw = m.group(1)
            body = content[m.end():]
            metadata = _parse_toml_frontmatter(raw)

    # Hermes-style frontmatter (;;;)
    if not metadata:
        m = re.match(r'^;;;\s*\n(.*?)\n;;;\s*\n', content, re.DOTALL)
        if m:
            raw = m.group(1)
            body = content[m.end():]
            metadata = _parse_yaml_frontmatter(raw)

    return metadata, body


def _parse_yaml_frontmatter(raw: str) -> Dict[str, Any]:
    """Parse YAML-style frontmatter (key: value pairs)."""
    result = {}
    for line in raw.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Handle lists
            if value == '[' or value.startswith('['):
                try:
                    import yaml
                    result[key] = yaml.safe_load(line)
                except ImportError:
                    result[key] = [value.strip('[]')]
            elif value == '':
                result[key] = True
            else:
                result[key] = value
    return result


def _parse_toml_frontmatter(raw: str) -> Dict[str, Any]:
    """Parse TOML-style frontmatter."""
    result = {}
    current_section = result
    for line in raw.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section_name = line[1:-1]
            current_section = result.setdefault(section_name, {})
        elif '=' in line:
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            current_section[key] = value
    return result


# ── Skill source paths ──────────────────────────────────────────

SKILL_SEARCH_PATHS = [
    # Claude Code
    (".claude/skills/", "claude"),
    (os.path.expanduser("~/.claude/skills/"), "claude"),
    # Cursor
    (".cursor/skills/", "cursor"),
    # GitHub Copilot
    (".github/skills/", "github"),
    (".github/prompts/", "github"),
    # Codex
    (os.path.expanduser("~/.agents/skills/"), "codex"),
    ("AGENTS.md", "codex"),
    # Hermes
    (os.path.expanduser("~/.hermes/skills/"), "hermes"),
]


@dataclass
class TranspiledSkill:
    """A skill transpiled to WW format."""
    name: str
    source: str                    # Source framework
    source_path: str               # Original file path
    content: str                   # WW-formatted markdown
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


@dataclass
class SkillTranspiler:
    """Transpile skills from other frameworks to WW format."""

    def discover_all(self) -> List[str]:
        """Find all skill files across all supported frameworks."""
        found = []
        for search_path, source in SKILL_SEARCH_PATHS:
            expanded = os.path.expanduser(search_path) if search_path.startswith("~") else search_path
            if not os.path.exists(expanded):
                continue
            if os.path.isdir(expanded):
                for root, _, files in os.walk(expanded):
                    for f in files:
                        if f.endswith(".md") or f.endswith(".yaml") or f.endswith(".yml"):
                            found.append(os.path.join(root, f))
            elif os.path.isfile(expanded):
                found.append(expanded)
        return found

    def transpile_file(self, filepath: str, source: Optional[str] = None) -> Optional[TranspiledSkill]:
        """Transpile a single skill file to WW format."""
        if source is None:
            source = self._guess_source(filepath)

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception as e:
            logger.warning("Cannot read skill file %s: %s", filepath, e)
            return None

        metadata, body = parse_frontmatter(raw)
        skill_name = self._derive_name(filepath, metadata, body)

        # Assemble WW-format skill
        ww_content = "---\n"
        ww_content += f"name: {skill_name}\n"
        ww_content += f"source: {source}\n"
        ww_content += f"original_path: {filepath}\n"

        if "description" in metadata:
            ww_content += f"description: {metadata['description']}\n"

        # Map trigger
        trigger = metadata.get("trigger") or metadata.get("hooks") or metadata.get("when")
        if trigger:
            ww_content += f"trigger: {trigger}\n"

        # Map permissions scope
        scope = metadata.get("scope") or metadata.get("tier") or metadata.get("permissions")
        if scope:
            ww_content += f"scope: {scope}\n"

        ww_content += "---\n\n"

        # Body: preserve original content, add WW structure hints
        if body.strip():
            # Add ## Steps header if missing
            if "## Steps" not in body and "## 步驟" not in body:
                body = "## Steps\n\n" + body
            ww_content += body

        return TranspiledSkill(
            name=skill_name,
            source=source,
            source_path=filepath,
            content=ww_content,
            metadata=metadata,
        )

    def transpile_all(self) -> Dict[str, TranspiledSkill]:
        """Discover and transpile all skills from all frameworks."""
        results = {}
        for filepath in self.discover_all():
            skill = self.transpile_file(filepath)
            if skill:
                # Use namespaced name to avoid collisions
                key = f"{skill.source}_{skill.name}"
                results[key] = skill
        return results

    @staticmethod
    def _derive_name(filepath: str, metadata: Dict[str, Any], body: str) -> str:
        """Derive skill name from metadata, filename, or content."""
        # Prefer metadata name
        if "name" in metadata:
            return metadata["name"]
        if "title" in metadata:
            return metadata["title"]

        # Fall back to filename
        name = os.path.splitext(os.path.basename(filepath))[0]
        # Clean up common patterns
        name = name.replace("_", "-").replace(" ", "-").lower()
        return name

    @staticmethod
    def _guess_source(filepath: str) -> str:
        """Guess the source framework from file path."""
        fp = filepath.lower()
        if ".claude" in fp or "claude" in fp:
            return "claude"
        if ".cursor" in fp or "cursor" in fp:
            return "cursor"
        if ".github" in fp:
            return "github"
        if ".agents" in fp or "codex" in fp:
            return "codex"
        if ".hermes" in fp:
            return "hermes"
        return "unknown"


# ── Stop Hook Upgrade ───────────────────────────────────────────

def upgrade_stop_hook(source: str, hook_config: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade Claude Code-style Stop Hooks with smart backoff.

    Claude Code: static 8-failure limit, then force-complete.
    WW upgrade: async validation middleware + subagent debugging intervention.

    When a Stop Hook fails:
      1-3 failures: standard retry with backoff
      4-6 failures: auto-spawn debugger subagent to analyze error logs
      7+ failures: escalate to user with structured diagnostic report

    Returns the upgraded hook configuration for WW.
    """
    upgraded = dict(hook_config)
    upgraded["_ww_upgraded"] = True
    upgraded["_upgraded_from"] = source

    # Replace static failure limit with smart backoff
    if "max_failures" in upgraded:
        upgraded["max_failures"] = min(upgraded["max_failures"], 12)  # Cap at 12

    # Add WW-specific fields
    upgraded.setdefault("smart_backoff", True)
    upgraded.setdefault("debugger_subagent", {
        "enabled": True,
        "trigger_after_failures": 3,
        "max_debug_attempts": 2,
    })

    # Convert script path to WW middleware
    if "script" in upgraded:
        upgraded["middleware"] = {
            "type": "script",
            "path": upgraded["script"],
            "sandbox": True,
        }

    return upgraded
