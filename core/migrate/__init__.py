"""WW Migration Engine — public API.

Exposes the MigrateEngine and all source-system parsers for
`ww migrate` CLI integration.
"""

from __future__ import annotations
import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from .engine import MigrateEngine, MigrationPhase, MigrationStatus, MigrationReport
from .os_layer import OSLayer, SnapshotHandle
from .translator import HeterogeneousTranslator, TranslatedConfig
from .alias_layer import AliasLayer, AliasScanResult
from .transpiler import SkillTranspiler, TranspiledSkill
from .secret_vault import SecretVault
from .tui import MigrationWizard, ConversationImporter, semantic_summarize
from .slash_commands import SlashCompat, install_slash_compat

logger = logging.getLogger("ww.migrate")

__all__ = [
    "MigrateEngine",
    "MigrationPhase",
    "MigrationStatus",
    "MigrationReport",
    "OSLayer",
    "SnapshotHandle",
    "HeterogeneousTranslator",
    "TranslatedConfig",
    "detect_and_list",
    "migrate_source",
    "SourceKind",
    "MigrationResult",
    "MigrationEngine",
]

SourceKind = str  # "openclaw" | "hermes" | "claude-code" | "codex"


class MigrationResult:
    """Result of a single-source migration."""
    def __init__(self, success: bool, items_migrated: int = 0,
                 snapshot_id: Optional[str] = None, errors: Optional[List[str]] = None):
        self.success = success
        self.items_migrated = items_migrated
        self.snapshot_id = snapshot_id
        self.errors = errors or []


# Legacy alias for cmd_migrate compatibility
MigrationEngine = MigrateEngine


def detect_and_list() -> List[Dict[str, Any]]:
    """Scan the environment for detectable AI agent installations.

    Returns a list of dicts with: source, items, running, services, warnings.
    Used by `ww migrate scan`.
    """
    import os
    from pathlib import Path

    results = []

    probes = {
        "openclaw": {
            "paths": ["~/.openclaw/openclaw.json", "~/.openclaw/"],
            "services": ["openclaw", "oc-gateway"],
        },
        "hermes": {
            "paths": ["~/.hermes/config.yaml", "~/.hermes/profiles/"],
            "services": ["hermes-gateway"],
        },
        "claude_code": {
            "paths": ["CLAUDE.md", "~/.claude/"],
            "services": [],
        },
        "codex": {
            "paths": ["~/.agents/skills/", "~/.codex/"],
            "services": [],
        },
    }

    for source, info in probes.items():
        found = []
        for p in info["paths"]:
            expanded = os.path.expanduser(p)
            if os.path.exists(expanded):
                found.append(p)

        if found:
            # Check for running services
            running = False
            active_services = []
            try:
                import subprocess
                for svc in info["services"]:
                    r = subprocess.run(
                        ["systemctl", "--user", "is-active", f"{svc}.service"],
                        capture_output=True, text=True, timeout=3,
                    )
                    if r.stdout.strip() == "active":
                        running = True
                        active_services.append(svc)
            except Exception:
                pass

            results.append({
                "source": source,
                "items": len(found),
                "paths": found,
                "running": running,
                "services": active_services,
                "warnings": [],
            })

    return results


def migrate_source(source: str, dry_run: bool = False) -> MigrationResult:
    """Migrate from a single source system. Thin wrapper around MigrateEngine."""
    engine = MigrateEngine()
    report = engine.run(sources=[source], dry_run=dry_run)

    success = report.status in (MigrationStatus.COMPLETED,)
    items = 0

    # Count migrated items
    translations = report.artifacts.get("translations", {})
    for system, tcfg in translations.items():
        if hasattr(tcfg, "skills"):
            items += len(tcfg.skills)
        if hasattr(tcfg, "memory_entries"):
            items += len(tcfg.memory_entries)
        if hasattr(tcfg, "aliases"):
            items += len(tcfg.aliases)

    return MigrationResult(
        success=success,
        items_migrated=items,
        snapshot_id=report.snapshot_id,
        errors=[report.error] if report.error else [],
    )
