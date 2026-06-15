"""ACID Migration State Machine v0.1

The MigrateEngine orchestrates the dual-layer migration architecture:
  Layer 1 (OS): graceful shutdown of old services, atomic snapshots, file locking
  Layer 2 (Translation): config parsing, normalization, semantic mapping

Phases are atomic — if any phase fails, the engine rolls back to the
previous snapshot and reports the failure.
"""

from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .os_layer import OSLayer, SnapshotHandle
from .translator import HeterogeneousTranslator, TranslatedConfig

logger = logging.getLogger("ww.migrate.engine")


class MigrationPhase(Enum):
    """Ordered phases of the ACID migration state machine."""
    DISCOVER = "discover"           # Scan for old-system artifacts
    VALIDATE = "validate"           # Schema validation, conflict detection
    SNAPSHOT = "snapshot"           # Atomic backup of all affected paths
    SHUTDOWN = "shutdown"           # Graceful stop of old system services
    PARSE     = "parse"             # Read and normalize source configs
    TRANSLATE = "translate"         # Map to WW native schema
    APPLY     = "apply"             # Write translated config, aliases, skills
    VERIFY    = "verify"            # Health check — can the new config load?
    COMPLETE  = "complete"          # Finalize, archive snapshot
    ROLLBACK  = "rollback"          # Restore from snapshot (error path)


class MigrationStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class MigrationReport:
    """Structured result of a migration run."""
    status: MigrationStatus = MigrationStatus.PENDING
    source_systems: List[str] = field(default_factory=list)
    phases_completed: List[MigrationPhase] = field(default_factory=list)
    failed_phase: Optional[MigrationPhase] = None
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    snapshot_id: Optional[str] = None
    duration_ms: int = 0


class MigrationError(Exception):
    """Fatal migration error — triggers rollback."""
    def __init__(self, phase: MigrationPhase, message: str, detail: Any = None):
        super().__init__(f"[{phase.value}] {message}")
        self.phase = phase
        self.detail = detail


@dataclass
class MigrateEngine:
    """ACID migration orchestrator.

    Usage:
        engine = MigrateEngine()
        report = engine.run(sources=["openclaw", "hermes"])
    """

    os_layer: OSLayer = field(default_factory=OSLayer)
    translator: HeterogeneousTranslator = field(default_factory=HeterogeneousTranslator)
    report: MigrationReport = field(default_factory=MigrationReport)
    _snapshot: Optional[SnapshotHandle] = None
    _phase: Optional[MigrationPhase] = None

    # ── Public API ────────────────────────────────────────────────

    def run(
        self,
        sources: Optional[List[str]] = None,
        dry_run: bool = False,
        interactive: bool = False,
    ) -> MigrationReport:
        """Execute the full migration pipeline.

        Args:
            sources: Source systems to migrate from.
                      None = auto-discover all.
                      Options: "openclaw", "hermes", "claude-code", "codex"
            dry_run: Parse and translate but don't write anything.
            interactive: Enable interactive TUI prompts (P2 feature).
        """
        start = time.monotonic()
        self.report = MigrationReport(status=MigrationStatus.IN_PROGRESS)

        try:
            self._execute_phase(MigrationPhase.DISCOVER, sources)
            self._execute_phase(MigrationPhase.VALIDATE)

            if not dry_run:
                self._execute_phase(MigrationPhase.SNAPSHOT)
                self._execute_phase(MigrationPhase.SHUTDOWN)
            else:
                logger.info("Dry run — skipping snapshot and shutdown phases")

            self._execute_phase(MigrationPhase.PARSE)
            self._execute_phase(MigrationPhase.TRANSLATE)

            if not dry_run:
                self._execute_phase(MigrationPhase.APPLY)
                self._execute_phase(MigrationPhase.VERIFY)
                self._execute_phase("import_history")  # Non-standard phase

            self._execute_phase(MigrationPhase.COMPLETE)
            self.report.status = MigrationStatus.COMPLETED

        except MigrationError as e:
            logger.error("Migration failed at phase %s: %s", e.phase.value, e)
            self.report.failed_phase = e.phase
            self.report.error = str(e)
            self._rollback()
            self.report.status = MigrationStatus.ROLLED_BACK

        except Exception as e:
            logger.exception("Unexpected migration error")
            self.report.error = f"Unexpected: {e}"
            self._rollback()
            self.report.status = MigrationStatus.FAILED

        finally:
            self.report.duration_ms = int((time.monotonic() - start) * 1000)

        return self.report

    # ── Phase executors ──────────────────────────────────────────

    def _execute_phase(self, phase: MigrationPhase, sources: Optional[List[str]] = None):
        self._phase = phase
        logger.info("Phase: %s", phase.value)

        if phase == MigrationPhase.DISCOVER:
            self._phase_discover(sources)
        elif phase == MigrationPhase.VALIDATE:
            self._phase_validate()
        elif phase == MigrationPhase.SNAPSHOT:
            self._phase_snapshot()
        elif phase == MigrationPhase.SHUTDOWN:
            self._phase_shutdown()
        elif phase == MigrationPhase.PARSE:
            self._phase_parse()
        elif phase == MigrationPhase.TRANSLATE:
            self._phase_translate()
        elif phase == MigrationPhase.APPLY:
            self._phase_apply()
        elif phase == MigrationPhase.VERIFY:
            self._phase_verify()
        elif phase == MigrationPhase.COMPLETE:
            self._phase_complete()
        elif phase == "import_history":
            self._phase_import_history()

    def _phase_import_history(self):
        """Import conversation histories from source systems."""
        try:
            from .tui import ConversationImporter
            importer = ConversationImporter()
            results = importer.import_all(self.report.source_systems)
            if results:
                total = sum(results.values())
                self.report.artifacts["conversations_imported"] = results
                logger.info("Imported %d conversation entries from: %s", total, list(results.keys()))
        except Exception as e:
            self.report.warnings.append(f"Conversation import failed: {e}")

    # ── Phase: DISCOVER ──────────────────────────────────────────

    def _phase_discover(self, sources: Optional[List[str]]):
        """Scan filesystem for detectable old-system installations."""
        discovered = []

        # Known artifact paths per source system
        probes = {
            "openclaw": [
                "~/.openclaw/openclaw.json",
                "~/.openclaw/config.json5",
            ],
            "hermes": [
                "~/.hermes/config.yaml",
                "~/.hermes/profiles/default/SOUL.md",
            ],
            "claude-code": [
                "CLAUDE.md",
                "~/.claude/settings.json",
                "~/.claude/CLAUDE.md",
            ],
            "codex": [
                "~/.agents/skills/",
                "~/.codex/config.json",
            ],
        }

        # If sources specified, only probe those
        if sources:
            probes = {k: v for k, v in probes.items() if k in sources}

        for system, paths in probes.items():
            for p in paths:
                expanded = Path(os.path.expanduser(p))
                if expanded.exists():
                    discovered.append(system)
                    break  # One match is enough to confirm this system exists

        if not discovered:
            raise MigrationError(
                MigrationPhase.DISCOVER,
                "No supported source systems detected. "
                "Checked: openclaw, hermes, claude-code, codex",
            )

        self.report.source_systems = discovered
        logger.info("Discovered source systems: %s", discovered)

    # ── Phase: VALIDATE ──────────────────────────────────────────

    def _phase_validate(self):
        """Pre-flight checks — conflicts, permissions, disk space."""
        # Check write permissions
        test_path = Path(os.path.expanduser("~/.worldwave/.migrate_test"))
        try:
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.touch()
            test_path.unlink()
        except (OSError, PermissionError) as e:
            raise MigrationError(
                MigrationPhase.VALIDATE,
                f"Cannot write to WW config directory: {e}",
            )

        # Check for existing WW config (conflict detection)
        ww_config = Path(os.path.expanduser("~/.worldwave/config.json"))
        if ww_config.exists():
            self.report.warnings.append(
                "Existing WW config found. Migration will merge, not overwrite."
            )

        # Check that old system services aren't running
        self.os_layer.detect_running_services(self.report.source_systems)

    # ── Phase: SNAPSHOT ──────────────────────────────────────────

    def _phase_snapshot(self):
        """Create atomic snapshot backup for rollback guarantee."""
        paths_to_backup = [
            os.path.expanduser("~/.worldwave/"),
            os.path.expanduser("~/.bashrc"),
            os.path.expanduser("~/.zshrc"),
        ]

        # Also backup source system dirs (read-only, for reference)
        source_dirs = {
            "openclaw": "~/.openclaw/",
            "hermes": "~/.hermes/",
            "claude-code": "~/.claude/",
            "codex": "~/.agents/",
        }
        for system in self.report.source_systems:
            if system in source_dirs:
                paths_to_backup.append(os.path.expanduser(source_dirs[system]))

        self._snapshot = self.os_layer.create_snapshot(paths_to_backup)
        self.report.snapshot_id = self._snapshot.id
        logger.info("Snapshot created: %s", self._snapshot.id)

    # ── Phase: SHUTDOWN ──────────────────────────────────────────

    def _phase_shutdown(self):
        """Gracefully stop old system background services."""
        self.os_layer.shutdown_services(self.report.source_systems)

    # ── Phase: PARSE ────────────────────────────────────────────

    def _phase_parse(self):
        """Parse source configurations into intermediate representation."""
        for system in self.report.source_systems:
            try:
                parsed = self.translator.parse_source(system)
                self.report.artifacts[f"parsed_{system}"] = parsed
            except Exception as e:
                self.report.warnings.append(f"Parse warning for {system}: {e}")
                # Non-fatal — continue with other systems

    # ── Phase: TRANSLATE ─────────────────────────────────────────

    def _phase_translate(self):
        """Translate parsed configs to WW native schema."""
        translations: Dict[str, TranslatedConfig] = {}
        for system in self.report.source_systems:
            parsed = self.report.artifacts.get(f"parsed_{system}")
            if parsed is None:
                continue
            try:
                translated = self.translator.translate(system, parsed)
                translations[system] = translated
            except Exception as e:
                self.report.warnings.append(f"Translation warning for {system}: {e}")

        self.report.artifacts["translations"] = translations

    # ── Phase: APPLY ─────────────────────────────────────────────

    def _phase_apply(self):
        """Write translated config, aliases, skills, and transpiled skills to disk."""
        translations = self.report.artifacts.get("translations", {})
        if not translations:
            raise MigrationError(
                MigrationPhase.APPLY,
                "No translations to apply",
            )

        self.os_layer.apply_translations(translations)

        # Inject compatibility aliases
        self._apply_alias_layer(translations)

        # Transpile foreign skills
        self._apply_skill_transpiler()

        # Inherit MCP server configs (Gemini Pillar 4: Trojan horse)
        self._apply_mcp_inheritance(translations)

        # Install slash-command compatibility map (Gemini Pillar 9: daily ops)
        self._apply_slash_compat(translations)

    def _apply_alias_layer(self, translations: Dict[str, Any]):
        """Inject compatibility aliases for detected source systems."""
        try:
            from .alias_layer import AliasLayer
            alias = AliasLayer()
            scan_results = alias.scan_all()
            needed = set()
            for result in scan_results:
                needed.update(result.compat_wrappers_needed)
            if needed:
                alias.inject_compat_wrappers(list(needed))
                self.report.artifacts["aliases_injected"] = list(needed)
                logger.info("Compat aliases injected for: %s", needed)
        except Exception as e:
            self.report.warnings.append(f"Alias layer failed: {e}")

    def _apply_skill_transpiler(self):
        """Transpile foreign skills to WW native format."""
        try:
            from .transpiler import SkillTranspiler
            transpiler = SkillTranspiler()
            transpiled = transpiler.transpile_all()
            if transpiled:
                skills_dir = os.path.expanduser("~/.worldwave/skills/")
                os.makedirs(skills_dir, exist_ok=True)
                for key, skill in transpiled.items():
                    skill_path = os.path.join(skills_dir, f"{key}.md")
                    with open(skill_path, "w") as f:
                        f.write(skill.content)
                self.report.artifacts["skills_transpiled"] = len(transpiled)
                logger.info("Transpiled %d skills to WW native format", len(transpiled))
        except Exception as e:
            self.report.warnings.append(f"Skill transpiler failed: {e}")

    def _apply_mcp_inheritance(self, translations: Dict[str, Any]):
        """Inherit MCP server configs from source systems (Gemini Pillar 4).

        Scans each translated source's parsed config for MCP server definitions
        and converts them to WW's MCPServerConfig format. API keys found in MCP
        configs are automatically vaulted via SecretVault.
        """
        mcp_servers = []

        for system, tcfg in translations.items():
            parsed = self.report.artifacts.get(f"parsed_{system}", {})
            if not parsed or parsed.get("_skipped"):
                continue

            # Extract MCP servers from parsed config
            source_mcp = self._extract_mcp_from_parsed(system, parsed)
            for srv in source_mcp:
                # Vault any API keys found
                if srv.get("api_key"):
                    try:
                        from .secret_vault import SecretVault
                        vault = SecretVault()
                        vault_key = f"mcp_{system}_{srv['name']}_api_key"
                        if vault.store(system, vault_key, srv["api_key"]):
                            srv["api_key"] = f"vault:{system}:{vault_key}"
                            logger.info("Vaulted MCP API key for %s/%s", system, srv["name"])
                    except Exception as e:
                        self.report.warnings.append(
                            f"MCP key vault failed for {system}/{srv['name']}: {e}"
                        )
                mcp_servers.append(srv)

        if mcp_servers:
            mcp_path = os.path.expanduser("~/.worldwave/mcp_servers.json")
            os.makedirs(os.path.dirname(mcp_path), exist_ok=True)

            # Merge with existing if present
            existing = []
            if os.path.isfile(mcp_path):
                try:
                    with open(mcp_path, "r") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    pass

            # Deduplicate by name
            seen = {s["name"] for s in existing}
            for srv in mcp_servers:
                if srv["name"] not in seen:
                    existing.append(srv)
                    seen.add(srv["name"])

            with open(mcp_path, "w") as f:
                json.dump(existing, f, indent=2)

            self.report.artifacts["mcp_servers_inherited"] = len(mcp_servers)
            logger.info("Inherited %d MCP servers from source systems", len(mcp_servers))

    @staticmethod
    def _extract_mcp_from_parsed(system: str, parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract MCP server configs from a parsed source system config.

        Handles the two common MCP formats:
        1. mcpServers dict (Claude Desktop standard):
           {"server-name": {"command": "npx", "args": [...], "env": {...}}}
        2. mcp list (OpenClaw, Hermes):
           [{"name": "...", "command": "...", "args": [...]}]

        Also handles HTTP-based servers with "url" field.
        """
        servers = []

        def _normalize_server(name: str, cfg: Dict) -> Dict:
            srv = {
                "name": name,
                "source": system,
                "transport": cfg.get("transport", cfg.get("type", "stdio")),
                "enabled": cfg.get("enabled", cfg.get("disabled", True) is not True),
            }
            if srv["transport"] == "http" or "url" in cfg:
                srv["transport"] = "http"
                srv["url"] = cfg.get("url", "")
                srv["api_key"] = cfg.get("api_key", cfg.get("headers", {}).get("Authorization", ""))
                if srv["api_key"].startswith("Bearer "):
                    srv["api_key"] = srv["api_key"][7:]
            else:
                srv["command"] = cfg.get("command", "")
                srv["args"] = cfg.get("args", [])
                srv["env"] = cfg.get("env", {})
            return srv

        # Format 1: mcpServers dict (Claude Desktop standard)
        for key in ("mcpServers", "mcp_servers", "mcp.servers"):
            mcp_dict = parsed.get(key)
            if isinstance(mcp_dict, dict):
                for name, cfg in mcp_dict.items():
                    if isinstance(cfg, dict):
                        servers.append(_normalize_server(name, cfg))

        # Format 2: nested under a top-level "mcp" key
        mcp_section = parsed.get("mcp")
        if isinstance(mcp_section, dict):
            # Could be mcp.servers or mcp directly
            for sub_key in ("servers", "mcpServers"):
                sub = mcp_section.get(sub_key)
                if isinstance(sub, dict):
                    for name, cfg in sub.items():
                        if isinstance(cfg, dict):
                            servers.append(_normalize_server(name, cfg))
                elif isinstance(sub, list):
                    for item in sub:
                        if isinstance(item, dict) and "name" in item:
                            servers.append(_normalize_server(
                                item["name"], item
                            ))

        # Format 3: list under mcp.servers (Hermes, OpenClaw)
        for key in ("mcp", "mcp_servers"):
            val = parsed.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict) and "name" in item:
                        servers.append(_normalize_server(
                            item["name"], item
                        ))

        # Format 4: Claude Desktop's mcpServerConfig in settings
        user_settings = parsed.get("user_settings", {})
        for key in ("mcpServers", "mcp_servers"):
            mcp_dict = user_settings.get(key)
            if isinstance(mcp_dict, dict):
                for name, cfg in mcp_dict.items():
                    if isinstance(cfg, dict):
                        servers.append(_normalize_server(name, cfg))

        return servers

    def _apply_slash_compat(self, translations: Dict[str, Any]):
        """Install slash-command compatibility map (Gemini Pillar 9).

        Writes ~/.worldwave/slash_compat.json so the gateway can translate
        old slash commands (/compact, /cost, /plan) to WW equivalents.
        """
        try:
            sources = list(translations.keys())
            if not sources:
                return

            from .slash_commands import install_slash_compat
            count = install_slash_compat(sources)
            if count > 0:
                self.report.artifacts["slash_commands_mapped"] = count
                logger.info("Installed %d slash-command compat mappings for %s", count, sources)
        except Exception as e:
            self.report.warnings.append(f"Slash command compat failed: {e}")

    # ── Phase: VERIFY ────────────────────────────────────────────

    def _phase_verify(self):
        """Health check — can WW load the new configuration?"""
        errors = self.os_layer.verify_config()
        if errors:
            raise MigrationError(
                MigrationPhase.VERIFY,
                f"Post-migration verification failed: {'; '.join(errors)}",
            )

    # ── Phase: COMPLETE ──────────────────────────────────────────

    def _phase_complete(self):
        """Archive snapshot, print summary."""
        if self._snapshot:
            self.os_layer.archive_snapshot(self._snapshot)

    # ── Rollback ─────────────────────────────────────────────────

    def _rollback(self):
        """Restore from snapshot. Best-effort — always runs."""
        try:
            self._execute_phase = lambda *a, **k: None  # no-op
            if self._snapshot:
                logger.warning("Rolling back to snapshot %s", self._snapshot.id)
                self.os_layer.restore_snapshot(self._snapshot)
                self.report.phases_completed.append(MigrationPhase.ROLLBACK)
        except Exception as e:
            logger.critical("Rollback failed: %s", e)
            self.report.warnings.append(f"CRITICAL: Rollback failed: {e}")
