"""Hermes Agent config parser v0.1

Parses Hermes configuration:
- config.yaml (core settings: model, provider, tools, etc.)
- SOUL.md / MEMORY.md / USER.md (persona & memory files)
- SQLite memory database (session facts, user preferences)
"""

from __future__ import annotations
import logging
import os
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger("ww.migrate.parsers.hermes")


class HermesParser:
    """Parse Hermes Agent configuration from ~/.hermes/."""

    HERMES_DIR = "~/.hermes/"

    def parse(self) -> Dict[str, Any]:
        """Load and parse Hermes configuration."""
        result: Dict[str, Any] = {}

        base = os.path.expanduser(self.HERMES_DIR)
        if not os.path.isdir(base):
            return {"_skipped": True, "_reason": "~/.hermes/ not found"}

        # Parse config.yaml
        result["config"] = self._parse_config(base)

        # Parse persona files
        result["persona"] = self._parse_persona(base)

        # Parse skills
        result["skills"] = self._parse_skills(base)

        # Parse memory (SQLite)
        result["memory"] = self._parse_memory(base)

        return result

    @staticmethod
    def _parse_config(base: str) -> Dict[str, Any]:
        """Parse Hermes config.yaml into a dict.

        Uses a simple YAML subset parser to avoid yaml dependency.
        """
        try:
            import yaml
            config_path = os.path.join(base, "config.yaml")
            if os.path.isfile(config_path):
                with open(config_path, "r") as f:
                    return yaml.safe_load(f) or {}
        except ImportError:
            logger.debug("PyYAML not installed — using basic key=value parser")

        # Fallback: try config.yaml as flat key-value pairs
        config_path = os.path.join(base, "config.yaml")
        result = {}
        if os.path.isfile(config_path):
            with open(config_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ":" in line:
                        key, _, value = line.partition(":")
                        result[key.strip()] = value.strip().strip('"').strip("'")
        return result

    @staticmethod
    def _parse_persona(base: str) -> Dict[str, str]:
        """Read SOUL.md, MEMORY.md, USER.md."""
        persona = {}
        for name in ("SOUL.md", "MEMORY.md", "USER.md"):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                with open(path, "r") as f:
                    persona[name.replace(".md", "").lower()] = f.read()
        return persona

    @staticmethod
    def _parse_skills(base: str) -> Dict[str, Any]:
        """Read skills from ~/.hermes/skills/."""
        skills = {}
        skills_dir = os.path.join(base, "skills")
        if not os.path.isdir(skills_dir):
            return skills

        for root, _, files in os.walk(skills_dir):
            for fname in files:
                if fname.endswith(".md"):
                    skill_name = os.path.splitext(fname)[0]
                    full_path = os.path.join(root, fname)
                    with open(full_path, "r") as f:
                        content = f.read()
                    skills[skill_name] = content
        return skills

    @staticmethod
    def _parse_memory(base: str) -> List[Dict[str, Any]]:
        """Extract facts from Hermes SQLite memory database."""
        entries = []
        # Try the Hermes memory DB
        db_paths = [
            os.path.join(base, "memory.db"),
            os.path.join(base, "hermes.db"),
            os.path.join(base, "data", "memory.db"),
        ]
        for db_path in db_paths:
            if not os.path.isfile(db_path):
                continue
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                # Try known table names
                for table in ("memories", "facts", "memory_entries", "long_term_memory"):
                    try:
                        cursor.execute(f"SELECT * FROM {table} LIMIT 1000")
                        rows = cursor.fetchall()
                        for row in rows:
                            entry = dict(row)
                            entry["_source_table"] = table
                            entries.append(entry)
                        if rows:
                            break
                    except sqlite3.OperationalError:
                        continue
                conn.close()
            except Exception as e:
                logger.warning("Hermes memory DB read error: %s", e)

        return entries
