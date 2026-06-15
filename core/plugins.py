"""
ww/core/plugins.py — Plugin System v0.1

Plugin loading/unloading with lifecycle management.
Plugins are Python packages or single .py files in plugins/ directory.
Supports: install, enable, disable, uninstall, health check.
"""

from __future__ import annotations
import importlib
import importlib.util
import json
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ww.plugins")


class PluginStatus(Enum):
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"
    UNINSTALLED = "uninstalled"


@dataclass
class PluginManifest:
    """Plugin metadata (from plugin.json or __manifest__ dict)."""
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    license: str = "MIT"
    homepage: str = ""
    dependencies: List[str] = field(default_factory=list)
    min_ww_version: str = "0.5.0"
    category: str = "general"  # channel, tool, memory, ui, integration
    caps: List[str] = field(default_factory=list)  # Capabilities this plugin provides
    
    @classmethod
    def from_dict(cls, data: Dict) -> "PluginManifest":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class LoadedPlugin:
    """A loaded plugin instance."""
    manifest: PluginManifest
    module: Any = None
    status: PluginStatus = PluginStatus.LOADED
    load_time: float = 0
    error: Optional[str] = None
    
    # Lifecycle hooks (optional, called if defined)
    on_enable: Optional[Callable] = None
    on_disable: Optional[Callable] = None
    on_uninstall: Optional[Callable] = None
    
    @property
    def name(self) -> str:
        return self.manifest.name


class PluginManager:
    """Manages plugin lifecycle: discovery, loading, enabling, disabling."""
    
    def __init__(self, plugins_dir: str = None):
        self._plugins_dir = plugins_dir or os.path.expanduser("~/.worldwave/plugins")
        self._plugins: Dict[str, LoadedPlugin] = {}
        self._hooks: Dict[str, List[Callable]] = {}  # event_name → [callbacks]
        
    @property
    def plugins_dir(self) -> str:
        return self._plugins_dir
        
    def discover(self) -> List[str]:
        """Discover available plugins (not yet loaded). Returns list of names."""
        discovered = []
        if not os.path.isdir(self._plugins_dir):
            return discovered
            
        for entry in sorted(os.listdir(self._plugins_dir)):
            plugin_path = os.path.join(self._plugins_dir, entry)
            
            # Single-file plugin
            if entry.endswith('.py') and os.path.isfile(plugin_path):
                name = entry[:-3]
                if name not in self._plugins and not name.startswith('_'):
                    discovered.append(name)
                    
            # Package plugin
            elif os.path.isdir(plugin_path):
                init_file = os.path.join(plugin_path, '__init__.py')
                manifest_file = os.path.join(plugin_path, 'plugin.json')
                if os.path.isfile(init_file):
                    if entry not in self._plugins and not entry.startswith('_'):
                        discovered.append(entry)
                        
        return discovered
        
    def load(self, name: str) -> Optional[LoadedPlugin]:
        """Load a plugin by name."""
        if name in self._plugins:
            logger.info(f"Plugin '{name}' already loaded")
            return self._plugins[name]
            
        import time
        start = time.time()
        
        # Try single-file first
        plugin_file = os.path.join(self._plugins_dir, f"{name}.py")
        plugin_dir = os.path.join(self._plugins_dir, name)
        
        manifest = None
        module = None
        
        try:
            if os.path.isfile(plugin_file):
                # Single-file plugin
                spec = importlib.util.spec_from_file_location(f"ww_plugin_{name}", plugin_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[f"ww_plugin_{name}"] = module
                    spec.loader.exec_module(module)
                    
            elif os.path.isdir(plugin_dir):
                # Package plugin
                module = importlib.import_module(f"plugins.{name}")
                
            else:
                logger.error(f"Plugin '{name}' not found")
                return None
                
            # Extract manifest
            manifest_data = getattr(module, '__manifest__', None)
            if manifest_data:
                manifest = PluginManifest.from_dict(manifest_data)
            else:
                # Try plugin.json
                manifest_file = os.path.join(plugin_dir, 'plugin.json')
                if os.path.isfile(manifest_file):
                    with open(manifest_file) as f:
                        manifest = PluginManifest.from_dict(json.load(f))
                else:
                    manifest = PluginManifest(name=name)
                    
            # Extract lifecycle hooks
            plugin = LoadedPlugin(
                manifest=manifest,
                module=module,
                status=PluginStatus.LOADED,
                load_time=time.time() - start,
                on_enable=getattr(module, 'on_enable', None),
                on_disable=getattr(module, 'on_disable', None),
                on_uninstall=getattr(module, 'on_uninstall', None),
            )
            
            self._plugins[name] = plugin
            logger.info(f"Plugin '{name}' v{manifest.version} loaded in {plugin.load_time:.3f}s")
            return plugin
            
        except Exception as e:
            logger.error(f"Failed to load plugin '{name}': {e}\n{traceback.format_exc()}")
            self._plugins[name] = LoadedPlugin(
                manifest=PluginManifest(name=name),
                status=PluginStatus.ERROR,
                error=str(e),
            )
            return None
            
    def enable(self, name: str) -> bool:
        """Enable a loaded plugin."""
        plugin = self._plugins.get(name)
        if not plugin:
            plugin = self.load(name)
            if not plugin:
                return False
                
        if plugin.status == PluginStatus.ENABLED:
            return True
            
        try:
            if plugin.on_enable:
                plugin.on_enable()
            plugin.status = PluginStatus.ENABLED
            logger.info(f"Plugin '{name}' enabled")
            return True
        except Exception as e:
            logger.error(f"Failed to enable plugin '{name}': {e}")
            plugin.status = PluginStatus.ERROR
            plugin.error = str(e)
            return False
            
    def disable(self, name: str) -> bool:
        """Disable a plugin."""
        plugin = self._plugins.get(name)
        if not plugin or plugin.status != PluginStatus.ENABLED:
            return True
            
        try:
            if plugin.on_disable:
                plugin.on_disable()
            plugin.status = PluginStatus.DISABLED
            logger.info(f"Plugin '{name}' disabled")
            return True
        except Exception as e:
            logger.error(f"Failed to disable plugin '{name}': {e}")
            return False
            
    def unload(self, name: str):
        """Fully unload a plugin."""
        if name in self._plugins:
            plugin = self._plugins[name]
            if plugin.status == PluginStatus.ENABLED:
                self.disable(name)
            # Remove from sys.modules
            mod_name = f"ww_plugin_{name}"
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            del self._plugins[name]
            logger.info(f"Plugin '{name}' unloaded")
            
    def install(self, source: str, name: str = None) -> bool:
        """Install a plugin from a source (URL, local path, or pip package).
        
        Sources:
        - https://.../plugin.py → download to plugins_dir
        - /path/to/plugin.py → copy to plugins_dir
        - pip:package-name → pip install
        """
        os.makedirs(self._plugins_dir, exist_ok=True)
        
        if source.startswith('pip:'):
            package = source[4:]
            import subprocess
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', package],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                logger.info(f"Plugin '{package}' installed via pip")
                return True
            else:
                logger.error(f"pip install failed: {result.stderr}")
                return False
                
        elif source.startswith('http://') or source.startswith('https://'):
            import urllib.request
            dest_name = name or source.split('/')[-1]
            dest_path = os.path.join(self._plugins_dir, dest_name)
            urllib.request.urlretrieve(source, dest_path)
            logger.info(f"Plugin downloaded to {dest_path}")
            return True
            
        elif os.path.isfile(source):
            dest_name = name or os.path.basename(source)
            dest_path = os.path.join(self._plugins_dir, dest_name)
            import shutil
            shutil.copy2(source, dest_path)
            logger.info(f"Plugin copied to {dest_path}")
            return True
            
        return False
        
    def list_plugins(self) -> List[Dict]:
        """List all plugins with status."""
        return [
            {
                "name": p.name,
                "version": p.manifest.version,
                "status": p.status.value,
                "description": p.manifest.description,
                "category": p.manifest.category,
                "error": p.error,
            }
            for p in self._plugins.values()
        ]
        
    def hook_register(self, event: str, callback: Callable):
        """Register a hook callback from a plugin."""
        self._hooks.setdefault(event, []).append(callback)
        
    def hook_emit(self, event: str, **kwargs):
        """Emit a hook event to all registered callbacks."""
        for cb in self._hooks.get(event, []):
            try:
                cb(**kwargs)
            except Exception as e:
                logger.error(f"Hook {event} callback failed: {e}")


# Singleton
_plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = PluginManager()
    return _plugin_manager
