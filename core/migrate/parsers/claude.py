"""Claude Code config parser v0.1

Parses Claude Code configuration:
- CLAUDE.md (project-level instructions in Markdown)
- ~/.claude/settings.json (user settings)
- ~/.claude/skills/ (custom skills)
"""

from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww.migrate.parsers.claude")


class ClaudeCodeParser:
    """Parse Claude Code configuration."""

    def parse(self) -> Dict[str, Any]:
        """Load and parse Claude Code configuration."""
        result: Dict[str, Any] = {}

        # Parse CLAUDE.md (project-level, in cwd)
        claude_md = self._find_claude_md()
        if claude_md:
            content = self._read_file(claude_md)
            result["rules"] = self._extract_rules(content)
            result["model_preferences"] = self._extract_model_prefs(content)
            result["raw_claude_md"] = content

        # Parse user settings
        result.update(self._parse_user_settings())

        # Parse skills
        result["skills"] = self._parse_skills()

        return result

    @staticmethod
    def _find_claude_md() -> Optional[str]:
        """Find CLAUDE.md in cwd or up the directory tree."""
        cwd = os.getcwd()
        path = Path(cwd)
        while path != path.parent:
            candidate = path / "CLAUDE.md"
            if candidate.is_file():
                return str(candidate)
            path = path.parent
        return None

    @staticmethod
    def _read_file(path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _extract_rules(content: str) -> List[str]:
        """Extract coding rules/directives from CLAUDE.md.

        Heuristic: lines that look like rules (bullet points, numbered items,
        lines starting with 'Always', 'Never', 'Use', 'Do not', etc.).
        """
        rules = []
        for line in content.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            # Bullet points, numbered lists
            if stripped.startswith(('- ', '* ', '1.', '2.', '3.')):
                rules.append(stripped)
            # Imperative directives
            elif any(stripped.lower().startswith(prefix) for prefix in
                     ('always', 'never', 'use ', 'do not', "don't", 'prefer',
                      'avoid', 'ensure', 'make sure')):
                rules.append(stripped)
        return rules

    @staticmethod
    def _extract_model_prefs(content: str) -> Optional[str]:
        """Try to extract model preference from CLAUDE.md."""
        import re
        match = re.search(r'model[:\s]+([\w\-.]+)', content, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _parse_user_settings() -> Dict[str, Any]:
        """Parse ~/.claude/settings.json."""
        settings_path = os.path.expanduser("~/.claude/settings.json")
        if not os.path.isfile(settings_path):
            return {}
        try:
            with open(settings_path, "r") as f:
                return {"user_settings": json.load(f)}
        except (json.JSONDecodeError, FileNotFoundError):
            logger.debug("Failed to parse ~/.claude/settings.json")
            return {}

    @staticmethod
    def _parse_skills() -> Dict[str, str]:
        """Parse ~/.claude/skills/ and project .claude/skills/."""
        skills = {}
        for base in [
            os.path.expanduser("~/.claude/skills"),
            os.path.join(os.getcwd(), ".claude", "skills"),
        ]:
            if not os.path.isdir(base):
                continue
            for root, _, files in os.walk(base):
                for fname in files:
                    if fname.endswith(".md"):
                        skill_name = os.path.splitext(fname)[0]
                        full_path = os.path.join(root, fname)
                        with open(full_path, "r") as f:
                            skills[skill_name] = f.read()
        return skills
