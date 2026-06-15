"""OpenClaw config parser v0.1

Parses openclaw.json (JSON5 with comment support).
Handles $include directives for split config files.
"""

from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww.migrate.parsers.openclaw")


class OpenClawParser:
    """Parse OpenClaw configuration from ~/.openclaw/."""

    CONFIG_PATHS = [
        "~/.openclaw/openclaw.json",
        "~/.openclaw/config.json5",
    ]

    def parse(self) -> Dict[str, Any]:
        """Load and parse OpenClaw configuration."""
        config_path = self._find_config()
        if not config_path:
            logger.debug("No OpenClaw config file found")
            return {"_skipped": True, "_reason": "No config file found"}

        raw = self._read_file(config_path)

        # Strip JSON5 comments (// and /* */)
        raw = self._strip_json5_comments(raw)

        try:
            config = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("OpenClaw config JSON parse error: %s", e)
            return {"_error": str(e), "_path": config_path}

        # Resolve $include directives
        config = self._resolve_includes(config, os.path.dirname(config_path))

        return config

    def _find_config(self) -> Optional[str]:
        for p in self.CONFIG_PATHS:
            expanded = os.path.expanduser(p)
            if os.path.isfile(expanded):
                return expanded
        return None

    @staticmethod
    def _read_file(path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _strip_json5_comments(text: str) -> str:
        """Remove // and /* */ comments from JSON5 text."""
        # Remove block comments first
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
        # Remove line comments (but not URLs)
        lines = []
        for line in text.split('\n'):
            # Find // that is not inside a string
            in_string = False
            string_char = None
            for i, ch in enumerate(line):
                if ch in ('"', "'") and (i == 0 or line[i-1] != '\\'):
                    if not in_string:
                        in_string = True
                        string_char = ch
                    elif ch == string_char:
                        in_string = False
                if not in_string and ch == '/' and i+1 < len(line) and line[i+1] == '/':
                    line = line[:i]
                    break
            lines.append(line)
        return '\n'.join(lines)

    def _resolve_includes(self, config: Any, base_dir: str, depth: int = 0) -> Any:
        """Recursively resolve $include directives.

        OpenClaw supports: {"$include": "./other-config.json5"}
        Max recursion depth: 10.
        """
        if depth > 10:
            logger.warning("Max $include depth exceeded")
            return config

        if isinstance(config, dict):
            if "$include" in config:
                include_path = config["$include"]
                full_path = os.path.join(base_dir, include_path)
                logger.debug("Resolving $include: %s", full_path)
                if os.path.isfile(full_path):
                    raw = self._read_file(full_path)
                    raw = self._strip_json5_comments(raw)
                    try:
                        included = json.loads(raw)
                        return self._resolve_includes(included, os.path.dirname(full_path), depth + 1)
                    except json.JSONDecodeError:
                        logger.warning("Failed to parse included file: %s", full_path)
                        return config
                else:
                    logger.warning("$include target not found: %s", full_path)
                    return config
            else:
                return {k: self._resolve_includes(v, base_dir, depth) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._resolve_includes(item, base_dir, depth) for item in config]
        return config
