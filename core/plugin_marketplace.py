"""
Plugin Marketplace — discover, install, and publish WW plugins.

Lightweight registry that works offline (local plugins dir) and
can sync with a remote registry (GitHub-based, npm-like).

Structure:
  ~/.ww/plugins/         — installed plugins
  core/plugins.py        — plugin loader
  registry.json          — local registry cache

Each plugin is a directory with:
  plugin.json            — metadata (name, version, author, description)
  __init__.py            — plugin entry point
  SKILL.md               — optional skill file

Usage:
  ww plugin install <name>     # Install from registry
  ww plugin list               # List installed
  ww plugin search <query>     # Search registry
  ww plugin publish            # Publish current plugin
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("ww.plugins.marketplace")


# ── Data structures ──────────────────────────────────────────────

@dataclass
class PluginMeta:
    """Plugin metadata (from plugin.json)."""
    name: str
    version: str
    description: str
    author: str = ""
    license: str = "MIT"
    homepage: str = ""
    repository: str = ""
    keywords: List[str] = field(default_factory=list)
    dependencies: Dict[str, str] = field(default_factory=dict)  # pkg → version
    ww_min_version: str = "0.5.0"
    category: str = "general"
    installed: bool = False
    install_path: str = ""
    installed_version: str = ""

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "license": self.license,
            "homepage": self.homepage,
            "repository": self.repository,
            "keywords": self.keywords,
            "dependencies": self.dependencies,
            "ww_min_version": self.ww_min_version,
            "category": self.category,
            "installed": self.installed,
            "installed_version": self.installed_version,
        }

    @staticmethod
    def from_dict(d: dict) -> "PluginMeta":
        return PluginMeta(
            name=d.get("name", ""),
            version=d.get("version", "0.1.0"),
            description=d.get("description", ""),
            author=d.get("author", ""),
            license=d.get("license", "MIT"),
            homepage=d.get("homepage", ""),
            repository=d.get("repository", ""),
            keywords=d.get("keywords", []),
            dependencies=d.get("dependencies", {}),
            ww_min_version=d.get("ww_min_version", "0.5.0"),
            category=d.get("category", "general"),
            installed=d.get("installed", False),
            install_path=d.get("install_path", ""),
            installed_version=d.get("installed_version", ""),
        )


# ── Registry ─────────────────────────────────────────────────────

DEFAULT_REGISTRY_URL = "https://raw.githubusercontent.com/Clean-Dust/ww-plugins/main/registry.json"
LOCAL_REGISTRY_PATH = os.path.expanduser("~/.ww/plugins/registry.json")
PLUGINS_DIR = os.path.expanduser("~/.ww/plugins")


class PluginRegistry:
    """Local plugin registry with remote sync."""

    def __init__(self, registry_url: str = ""):
        self._registry_url = registry_url or os.environ.get(
            "WW_PLUGIN_REGISTRY", DEFAULT_REGISTRY_URL
        )
        self._plugins: Dict[str, PluginMeta] = {}
        self._load_local()

    # ── Public API ───────────────────────────────────────────────

    def search(self, query: str = "", category: str = "") -> List[PluginMeta]:
        """Search plugins by name, keyword, or category."""
        results = []
        q = query.lower()
        for plugin in self._plugins.values():
            if q:
                match = (
                    q in plugin.name.lower()
                    or q in plugin.description.lower()
                    or any(q in kw.lower() for kw in plugin.keywords)
                )
                if not match:
                    continue
            if category and plugin.category != category:
                continue
            results.append(plugin)
        return sorted(results, key=lambda p: p.name)

    def get(self, name: str) -> Optional[PluginMeta]:
        """Get plugin by name."""
        return self._plugins.get(name)

    def list_installed(self) -> List[PluginMeta]:
        """List installed plugins."""
        return [p for p in self._plugins.values() if p.installed]

    def list_categories(self) -> List[str]:
        """List all categories."""
        cats = set(p.category for p in self._plugins.values())
        return sorted(cats)

    def sync(self) -> int:
        """Sync with remote registry. Returns number of new plugins."""
        try:
            req = urllib.request.Request(self._registry_url)
            req.add_header("User-Agent", "WW-Plugin-Registry/1.0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            new_count = 0
            for item in data.get("plugins", []):
                plugin = PluginMeta.from_dict(item)
                if plugin.name not in self._plugins:
                    self._plugins[plugin.name] = plugin
                    new_count += 1
                else:
                    # Update if newer version
                    existing = self._plugins[plugin.name]
                    if self._version_gt(plugin.version, existing.version):
                        self._plugins[plugin.name] = plugin

            self._save_local()
            return new_count
        except Exception as e:
            log.warning("Failed to sync registry: %s", e)
            return 0

    def install(self, name: str, version: str = "") -> bool:
        """Install a plugin by name. Returns True on success."""
        plugin = self._plugins.get(name)
        if not plugin:
            # Try syncing first
            self.sync()
            plugin = self._plugins.get(name)
            if not plugin:
                log.error("Plugin not found: %s", name)
                return False

        target_version = version or plugin.version
        install_dir = os.path.join(PLUGINS_DIR, name)

        # Download from GitHub repository
        if plugin.repository:
            success = self._install_from_github(plugin, install_dir, target_version)
        else:
            log.error("No repository URL for plugin: %s", name)
            return False

        if success:
            plugin.installed = True
            plugin.install_path = install_dir
            plugin.installed_version = target_version
            self._save_local()
            self._post_install(install_dir, plugin)

        return success

    def uninstall(self, name: str) -> bool:
        """Uninstall a plugin."""
        plugin = self._plugins.get(name)
        if not plugin or not plugin.installed:
            return False

        install_dir = plugin.install_path
        if install_dir and os.path.exists(install_dir):
            shutil.rmtree(install_dir, ignore_errors=True)

        plugin.installed = False
        plugin.install_path = ""
        plugin.installed_version = ""
        self._save_local()
        return True

    def publish(self, plugin_dir: str) -> Optional[PluginMeta]:
        """Publish a local plugin to the registry (adds to local DB)."""
        plugin_json = os.path.join(plugin_dir, "plugin.json")
        if not os.path.exists(plugin_json):
            log.error("No plugin.json found in %s", plugin_dir)
            return None

        with open(plugin_json) as f:
            meta = PluginMeta.from_dict(json.load(f))

        self._plugins[meta.name] = meta
        self._save_local()
        log.info("Published %s@%s", meta.name, meta.version)
        return meta

    # ── Internal ─────────────────────────────────────────────────

    def _load_local(self):
        """Load local registry cache."""
        os.makedirs(os.path.dirname(LOCAL_REGISTRY_PATH), exist_ok=True)
        if os.path.exists(LOCAL_REGISTRY_PATH):
            with open(LOCAL_REGISTRY_PATH) as f:
                data = json.load(f)
                for item in data.get("plugins", []):
                    plugin = PluginMeta.from_dict(item)
                    self._plugins[plugin.name] = plugin

    def _save_local(self):
        """Save local registry cache."""
        os.makedirs(os.path.dirname(LOCAL_REGISTRY_PATH), exist_ok=True)
        data = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "plugins": [p.to_dict() for p in self._plugins.values()],
        }
        with open(LOCAL_REGISTRY_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _install_from_github(self, plugin: PluginMeta, install_dir: str, version: str) -> bool:
        """Clone a plugin from its GitHub repository."""
        try:
            if os.path.exists(install_dir):
                shutil.rmtree(install_dir, ignore_errors=True)

            subprocess.run(
                ["git", "clone", "--depth=1", plugin.repository, install_dir],
                capture_output=True, timeout=60, check=True,
            )

            # Install dependencies if requirements.txt exists
            req_file = os.path.join(install_dir, "requirements.txt")
            if os.path.exists(req_file):
                subprocess.run(
                    ["pip", "install", "-r", req_file],
                    capture_output=True, timeout=120,
                )

            return True
        except Exception as e:
            log.error("Failed to install %s: %s", plugin.name, e)
            return False

    def _post_install(self, install_dir: str, plugin: PluginMeta):
        """Run post-install hooks if any."""
        hook_script = os.path.join(install_dir, "post_install.py")
        if os.path.exists(hook_script):
            try:
                subprocess.run(
                    ["python", hook_script],
                    capture_output=True, timeout=30,
                )
            except Exception as e:
                log.warning("Post-install hook failed for %s: %s", plugin.name, e)

    @staticmethod
    def _version_gt(a: str, b: str) -> bool:
        """Compare semantic versions: a > b."""
        try:
            parts_a = [int(x) for x in a.split(".")]
            parts_b = [int(x) for x in b.split(".")]
            # Pad to same length
            while len(parts_a) < 3:
                parts_a.append(0)
            while len(parts_b) < 3:
                parts_b.append(0)
            return parts_a > parts_b
        except Exception:
            return False


# ── Singleton ────────────────────────────────────────────────────

_registry: Optional[PluginRegistry] = None


def get_plugin_registry() -> PluginRegistry:
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry
