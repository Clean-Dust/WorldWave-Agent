"""Heterogeneous Translation Engine v0.1

Parses foreign system configurations and maps them to WW native schema.
Fault-tolerant: unknown fields are logged and isolated, never abort the pipeline.
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger("ww.migrate.translator")


@dataclass
class TranslatedConfig:
    """Output of the translation engine — ready to apply."""
    source_system: str
    ww_config: Dict[str, Any] = field(default_factory=dict)
    aliases: Dict[str, str] = field(default_factory=dict)
    skills: Dict[str, str] = field(default_factory=dict)      # skill_name → content
    memory_entries: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class HeterogeneousTranslator:
    """Main translation engine.

    parse_source() → returns raw parsed data
    translate()    → returns TranslatedConfig
    """

    _parsers: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self._parsers:
            self._load_parsers()

    def _load_parsers(self):
        """Lazy-load parser modules to avoid circular imports."""
        try:
            from .parsers.openclaw import OpenClawParser
            self._parsers["openclaw"] = OpenClawParser()
        except ImportError:
            logger.debug("OpenClaw parser not available")

        try:
            from .parsers.hermes import HermesParser
            self._parsers["hermes"] = HermesParser()
        except ImportError:
            logger.debug("Hermes parser not available")

        try:
            from .parsers.claude import ClaudeCodeParser
            self._parsers["claude-code"] = ClaudeCodeParser()
        except ImportError:
            logger.debug("Claude Code parser not available")

        try:
            from .parsers.codex import CodexParser
            self._parsers["codex"] = CodexParser()
        except ImportError:
            logger.debug("Codex parser not available")

    # ── Public API ────────────────────────────────────────────────

    def parse_source(self, system: str) -> Dict[str, Any]:
        """Parse a source system's configuration into an intermediate dict."""
        if system not in self._parsers:
            logger.warning("No parser registered for %s — skipping", system)
            return {"_skipped": True, "_reason": f"No parser for {system}"}

        parser = self._parsers[system]
        try:
            return parser.parse()
        except Exception as e:
            logger.warning("Parser %s failed: %s", system, e)
            return {"_error": str(e), "_source": system}

    def translate(self, system: str, parsed: Dict[str, Any]) -> TranslatedConfig:
        """Translate parsed config to WW native schema."""
        tcfg = TranslatedConfig(source_system=system)

        if parsed.get("_skipped") or parsed.get("_error"):
            tcfg.warnings.append(f"Source {system} had parse issues: {parsed}")
            return tcfg

        # Route to system-specific translator
        translators = {
            "openclaw": self._translate_openclaw,
            "hermes": self._translate_hermes,
            "claude-code": self._translate_claude_code,
            "codex": self._translate_codex,
        }

        translator_fn = translators.get(system)
        if translator_fn:
            try:
                translator_fn(parsed, tcfg)
            except Exception as e:
                tcfg.warnings.append(f"Translation error for {system}: {e}")
                logger.warning("Translation error for %s: %s", system, e)

        return tcfg

    # ── System-specific translators ──────────────────────────────

    def _translate_openclaw(self, parsed: Dict[str, Any], tcfg: TranslatedConfig):
        """OpenClaw → WW mapping."""
        # Model config
        if "agents" in parsed and "defaults" in parsed["agents"]:
            defaults = parsed["agents"]["defaults"]
            if "models" in defaults:
                # Map OpenClaw model format to WW
                tcfg.ww_config["model"] = defaults["models"]

        # Gateways → WW platform adapters
        if "gateways" in parsed:
            gateways = parsed["gateways"]
            # OpenClaw gateway configs map to WW platform adapters
            tcfg.ww_config["platforms"] = {}
            for gw_name, gw_config in gateways.items():
                if gw_name in ("discord", "telegram", "slack", "matrix"):
                    tcfg.ww_config["platforms"][gw_name] = gw_config

        # Tools → WW tool registry
        if "tools" in parsed:
            tcfg.ww_config["tools"] = parsed["tools"]

        # TTS
        if "tts" in parsed:
            tcfg.ww_config["tts"] = parsed["tts"]

        # Alias
        tcfg.aliases["openclaw"] = "ww run"
        tcfg.aliases["oc"] = "ww run"

    def _translate_hermes(self, parsed: Dict[str, Any], tcfg: TranslatedConfig):
        """Hermes Agent → WW mapping."""
        # Core config
        if "config" in parsed:
            hermes_cfg = parsed["config"]
            # Model
            if "model" in hermes_cfg:
                tcfg.ww_config["model"] = hermes_cfg["model"]
            if "provider" in hermes_cfg:
                tcfg.ww_config["provider"] = hermes_cfg["provider"]

        # SOUL.md / MEMORY.md / USER.md → persona
        if "persona" in parsed:
            tcfg.ww_config["persona"] = parsed["persona"]

        # Skills
        if "skills" in parsed:
            for skill_name, skill_data in parsed["skills"].items():
                if isinstance(skill_data, dict):
                    content = skill_data.get("content", json.dumps(skill_data))
                else:
                    content = str(skill_data)
                tcfg.skills[skill_name] = content

        # Memory import
        if "memory" in parsed:
            for entry in parsed["memory"]:
                tcfg.memory_entries.append({
                    "source": "hermes",
                    "type": entry.get("type", "fact"),
                    "content": entry.get("content", ""),
                    "trust": entry.get("trust", 0.5),
                    "imported_at": None,  # Set at write time
                })

        # Alias
        tcfg.aliases["hermes"] = "ww run"

    def _translate_claude_code(self, parsed: Dict[str, Any], tcfg: TranslatedConfig):
        """Claude Code → WW mapping."""
        # CLAUDE.md directives → project guardrails
        if "rules" in parsed:
            tcfg.ww_config["project_guardrails"] = parsed["rules"]

        if "model_preferences" in parsed:
            tcfg.ww_config["model"] = parsed["model_preferences"]

        if "hooks" in parsed:
            tcfg.ww_config["hooks"] = parsed["hooks"]

        # Skills from .claude/skills/
        if "skills" in parsed:
            for skill_name, skill_content in parsed["skills"].items():
                tcfg.skills[f"claude_{skill_name}"] = skill_content

        # Alias
        tcfg.aliases["claude"] = "ww run --compat claude"

    def _translate_codex(self, parsed: Dict[str, Any], tcfg: TranslatedConfig):
        """Codex → WW mapping."""
        # Agent skills → WW skills
        if "skills" in parsed:
            for skill_name, skill_data in parsed["skills"].items():
                if isinstance(skill_data, dict):
                    content = skill_data.get("instructions", "")
                    if "scripts" in skill_data:
                        content += "\n\n## Scripts\n" + json.dumps(skill_data["scripts"])
                else:
                    content = str(skill_data)
                tcfg.skills[f"codex_{skill_name}"] = content

        # 4-tier RBAC → WW virtual scope
        if "rbac" in parsed:
            tcfg.ww_config["rbac_mapping"] = parsed["rbac"]

        # Alias
        tcfg.aliases["codex"] = "ww run --compat codex"
