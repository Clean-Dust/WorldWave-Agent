"""Codex config parser v0.1

Parses OpenAI Codex configuration:
- .agents/skills/ (4-tier directory hierarchy)
- ~/.codex/config.json (user config)
"""

from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww.migrate.parsers.codex")


class CodexParser:
    """Parse Codex Agent configuration."""

    def parse(self) -> Dict[str, Any]:
        """Load and parse Codex configuration."""
        result: Dict[str, Any] = {}

        # Parse skills (4-tier hierarchy)
        result["skills"] = self._parse_skills()

        # Parse RBAC mapping from 4-tier structure
        result["rbac"] = self._parse_rbac()

        # User config
        result.update(self._parse_user_config())

        return result

    @staticmethod
    def _parse_skills() -> Dict[str, Any]:
        """Parse Codex Agent Skills from .agents/skills/ (4-tier hierarchy).

        Tiers: REPO > USER > ADMIN > SYSTEM
        Each tier directory contains skill subdirectories with:
          - SKILL.md (or instructions.md)
          - scripts/ (executable scripts)
          - references/ (supporting docs)

        Returns: {skill_name: {instructions, scripts, tier, source_path}}
        """
        skills = {}

        # Scan all 4 tiers
        tiers = {
            "repo": [os.path.join(os.getcwd(), ".agents", "skills")],
            "user": [os.path.expanduser("~/.agents/skills")],
            "admin": [os.path.expanduser("~/.agents/admin/skills")],
            "system": [os.path.expanduser("~/.agents/system/skills")],
        }

        for tier_name, paths in tiers.items():
            for base in paths:
                if not os.path.isdir(base):
                    continue
                for entry in os.listdir(base):
                    skill_dir = os.path.join(base, entry)
                    if not os.path.isdir(skill_dir):
                        continue

                    skill_data: Dict[str, Any] = {
                        "tier": tier_name,
                        "source_path": skill_dir,
                    }

                    # Read instructions
                    for inst_name in ("SKILL.md", "instructions.md", "README.md"):
                        inst_path = os.path.join(skill_dir, inst_name)
                        if os.path.isfile(inst_path):
                            with open(inst_path, "r") as f:
                                skill_data["instructions"] = f.read()
                            break

                    # Read scripts
                    scripts_dir = os.path.join(skill_dir, "scripts")
                    if os.path.isdir(scripts_dir):
                        skill_data["scripts"] = {}
                        for sfile in os.listdir(scripts_dir):
                            spath = os.path.join(scripts_dir, sfile)
                            if os.path.isfile(spath):
                                try:
                                    with open(spath, "r") as f:
                                        skill_data["scripts"][sfile] = f.read()
                                except Exception:
                                    skill_data["scripts"][sfile] = f"<binary: {sfile}>"

                    # Read references
                    refs_dir = os.path.join(skill_dir, "references")
                    if os.path.isdir(refs_dir):
                        skill_data["references"] = {}
                        for rfile in os.listdir(refs_dir):
                            rpath = os.path.join(refs_dir, rfile)
                            if os.path.isfile(rpath):
                                try:
                                    with open(rpath, "r") as f:
                                        skill_data["references"][rfile] = f.read()
                                except Exception:
                                    skill_data["references"][rfile] = f"<binary: {rfile}>"

                    skills[entry] = skill_data

        return skills

    @staticmethod
    def _parse_rbac() -> Dict[str, List[str]]:
        """Map 4-tier filesystem hierarchy to WW virtual scopes.

        Returns: {tier: [skill_names]}
        """
        rbac = {}
        tiers = {
            "repo": [os.path.join(os.getcwd(), ".agents", "skills")],
            "user": [os.path.expanduser("~/.agents/skills")],
            "admin": [os.path.expanduser("~/.agents/admin/skills")],
            "system": [os.path.expanduser("~/.agents/system/skills")],
        }

        for tier_name, paths in tiers.items():
            for base in paths:
                if not os.path.isdir(base):
                    continue
                entries = [
                    e for e in os.listdir(base)
                    if os.path.isdir(os.path.join(base, e))
                ]
                if entries:
                    rbac[tier_name] = entries
                break  # Only first existing path per tier

        return rbac

    @staticmethod
    def _parse_user_config() -> Dict[str, Any]:
        """Parse ~/.codex/config.json."""
        config_path = os.path.expanduser("~/.codex/config.json")
        if not os.path.isfile(config_path):
            return {}
        try:
            with open(config_path, "r") as f:
                return {"user_settings": json.load(f)}
        except (json.JSONDecodeError, FileNotFoundError):
            logger.debug("Failed to parse ~/.codex/config.json")
            return {}
