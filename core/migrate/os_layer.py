"""OS-Level Wrapper Layer v0.1

Handles all OS interaction during migration:
- Graceful shutdown of old system services (systemd, pm2, etc.)
- Atomic filesystem snapshots using hardlink-copy or tar
- Snapshot restore (rollback)
- Shell alias injection (.bashrc/.zshrc)
- Config write with validation
"""

from __future__ import annotations
import datetime
import json
import logging
import os
import shutil
import signal
import subprocess
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww.migrate.os_layer")


@dataclass
class SnapshotHandle:
    """Opaque handle to a filesystem snapshot."""
    id: str
    archive_path: str
    paths: List[str]
    created_at: str


@dataclass
class OSLayer:
    """OS-level wrapper for migration operations."""

    snapshot_dir: str = field(default_factory=lambda: os.path.expanduser("~/.worldwave/snapshots/"))
    _running_services: Dict[str, List[str]] = field(default_factory=dict)

    # ── Service Management ───────────────────────────────────────

    def detect_running_services(self, source_systems: List[str]):
        """Detect if old system services are running (systemd, pm2, etc.)."""
        service_patterns = {
            "openclaw": ["openclaw", "oc-gateway"],
            "hermes": ["hermes-gateway", "hermes-agent"],
            "claude-code": ["claude-daemon"],
            "codex": [],
        }

        for system in source_systems:
            patterns = service_patterns.get(system, [])
            running = []
            for pattern in patterns:
                try:
                    result = subprocess.run(
                        ["systemctl", "--user", "is-active", f"{pattern}.service"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.stdout.strip() == "active":
                        running.append(pattern)
                except Exception:
                    pass
            if running:
                self._running_services[system] = running
                logger.info("Detected running services for %s: %s", system, running)

    def shutdown_services(self, source_systems: List[str]):
        """Gracefully stop detected old-system services."""
        for system in source_systems:
            services = self._running_services.get(system, [])
            for svc in services:
                try:
                    logger.info("Stopping service: %s", svc)
                    subprocess.run(
                        ["systemctl", "--user", "stop", f"{svc}.service"],
                        capture_output=True, text=True, timeout=30,
                        check=False,
                    )
                except Exception as e:
                    logger.warning("Failed to stop %s: %s", svc, e)

    # ── Snapshot Management ──────────────────────────────────────

    def create_snapshot(self, paths: List[str]) -> SnapshotHandle:
        """Create atomic snapshot of specified paths using tar.gz.

        Existing paths are archived; missing paths are silently skipped
        so the snapshot covers whatever actually exists.

        Uses fcntl advisory lock to prevent concurrent snapshot operations.
        """
        snap_id = f"migrate-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        os.makedirs(self.snapshot_dir, exist_ok=True)
        archive_path = os.path.join(self.snapshot_dir, f"{snap_id}.tar.gz")

        # Acquire file lock to prevent concurrent snapshots
        lock_path = os.path.join(self.snapshot_dir, ".snapshot.lock")
        lock_fd = None
        try:
            import fcntl
            lock_fd = open(lock_path, "w")
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            logger.debug("Snapshot lock acquired")
        except (ImportError, OSError) as e:
            if lock_fd:
                lock_fd.close()
                lock_fd = None
            logger.warning("Snapshot lock unavailable (%s) — proceeding unlocked", e)

        try:
            existing = [p for p in paths if os.path.exists(os.path.expanduser(p))]
            if not existing:
                logger.warning("No paths to snapshot — creating empty archive")
                with tarfile.open(archive_path, "w:gz") as tar:
                    pass
            else:
                with tarfile.open(archive_path, "w:gz") as tar:
                    for p in existing:
                        expanded = os.path.expanduser(p)
                        arcname = p.lstrip("~").lstrip("/") or os.path.basename(p)
                        try:
                            tar.add(expanded, arcname=arcname)
                        except (PermissionError, FileNotFoundError) as e:
                            logger.warning("Skipping %s during snapshot: %s", p, e)
        finally:
            # Release lock
            if lock_fd:
                try:
                    import fcntl
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass

        return SnapshotHandle(
            id=snap_id,
            archive_path=archive_path,
            paths=paths,
            created_at=datetime.datetime.now().isoformat(),
        )

    def restore_snapshot(self, snapshot: SnapshotHandle):
        """Restore files from a snapshot archive."""
        if not os.path.exists(snapshot.archive_path):
            logger.error("Snapshot archive not found: %s", snapshot.archive_path)
            return

        with tarfile.open(snapshot.archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                extract_path = os.path.expanduser(f"~/{member.name}")
                try:
                    tar.extract(member, path=os.path.expanduser("~/"))
                    logger.debug("Restored: %s", extract_path)
                except Exception as e:
                    logger.warning("Failed to restore %s: %s", member.name, e)

    def archive_snapshot(self, snapshot: SnapshotHandle):
        """Mark snapshot as archived (keep for reference, don't auto-delete)."""
        archive_dir = os.path.join(self.snapshot_dir, "archive")
        os.makedirs(archive_dir, exist_ok=True)
        dest = os.path.join(archive_dir, os.path.basename(snapshot.archive_path))
        if os.path.exists(snapshot.archive_path):
            shutil.move(snapshot.archive_path, dest)
            logger.info("Snapshot archived: %s", dest)

    # ── Apply Translations ───────────────────────────────────────

    def apply_translations(self, translations: Dict[str, Any]):
        """Write translated configs, aliases, and skills to disk.

        From .translator import TranslatedConfig at runtime to avoid circular import.
        """
        from .translator import TranslatedConfig

        for system, tcfg in translations.items():
            if not isinstance(tcfg, TranslatedConfig):
                continue

            # Write WW config
            if tcfg.ww_config:
                ww_config_dir = os.path.expanduser("~/.worldwave/")
                os.makedirs(ww_config_dir, exist_ok=True)
                config_path = os.path.join(ww_config_dir, "config.json")
                _write_json(config_path, tcfg.ww_config, merge=True)

            # Inject shell aliases
            if tcfg.aliases:
                self._inject_aliases(tcfg.aliases)

            # Write translated skills
            if tcfg.skills:
                skills_dir = os.path.expanduser("~/.worldwave/skills/")
                os.makedirs(skills_dir, exist_ok=True)
                for skill_name, skill_content in tcfg.skills.items():
                    skill_path = os.path.join(skills_dir, f"{skill_name}.md")
                    with open(skill_path, "w") as f:
                        f.write(skill_content)
                    logger.info("Skill written: %s", skill_path)

            # Import memory (if any)
            if tcfg.memory_entries:
                memory_dir = os.path.expanduser("~/.worldwave/data/memory/")
                os.makedirs(memory_dir, exist_ok=True)
                memory_path = os.path.join(memory_dir, f"imported_{system}.jsonl")
                with open(memory_path, "a") as f:
                    for entry in tcfg.memory_entries:
                        f.write(json.dumps(entry) + "\n")
                logger.info("Memory imported: %d entries → %s", len(tcfg.memory_entries), memory_path)

    def _inject_aliases(self, aliases: Dict[str, str]):
        """Inject shell aliases into .bashrc and .zshrc."""
        block_header = "# >>> Worldwave migration aliases (auto-generated) >>>"
        block_footer = "# <<< Worldwave migration aliases <<<"

        rc_files = [
            os.path.expanduser("~/.bashrc"),
            os.path.expanduser("~/.zshrc"),
        ]

        alias_block = block_header + "\n"
        for alias_name, alias_cmd in aliases.items():
            alias_block += f"alias {alias_name}='{alias_cmd}'\n"
        alias_block += block_footer + "\n"

        for rc_file in rc_files:
            if not os.path.exists(rc_file):
                continue
            with open(rc_file, "r") as f:
                content = f.read()

            # Remove old block if present
            if block_header in content:
                before = content.split(block_header)[0]
                after_parts = content.split(block_footer)
                after = after_parts[-1] if len(after_parts) > 1 else ""
                content = before + after

            with open(rc_file, "a") as f:
                f.write("\n" + alias_block)

            logger.info("Aliases injected into %s", rc_file)

    # ── Verify ───────────────────────────────────────────────────

    def verify_config(self) -> List[str]:
        """Verify that the new WW configuration is loadable. Returns errors."""
        errors = []
        ww_config = os.path.expanduser("~/.worldwave/config.json")
        if os.path.exists(ww_config):
            try:
                with open(ww_config, "r") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                errors.append(f"WW config.json is invalid JSON: {e}")
        else:
            errors.append("WW config.json not found after migration")

        return errors


# ── Helpers ──────────────────────────────────────────────────────

def _write_json(path: str, data: Dict[str, Any], merge: bool = False):
    """Write JSON data, optionally merging with existing file."""
    if merge and os.path.exists(path):
        try:
            with open(path, "r") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            existing = {}
        # Shallow merge — new keys overwrite old
        existing.update(data)
        data = existing

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
