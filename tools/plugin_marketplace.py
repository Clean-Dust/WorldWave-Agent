"""Plugin marketplace tools."""

from tools.registry import ToolRegistry, ToolDef
from core.plugin_marketplace import get_plugin_registry


def register_tools(registry: ToolRegistry):

    _reg = get_plugin_registry()

    def handle_plugin_search(query: str = "", category: str = "", **kwargs) -> dict:
        """Search for plugins."""
        plugins = _reg.search(query, category)
        return {
            "total": len(plugins),
            "plugins": [
                {
                    "name": p.name,
                    "version": p.version,
                    "description": p.description,
                    "author": p.author,
                    "category": p.category,
                    "installed": p.installed,
                }
                for p in plugins
            ],
        }

    def handle_plugin_install(name: str, **kwargs) -> dict:
        """Install a plugin."""
        success = _reg.install(name)
        if success:
            plugin = _reg.get(name)
            return {"installed": True, "plugin": plugin.to_dict() if plugin else {}}
        return {"error": f"Failed to install {name}"}

    def handle_plugin_uninstall(name: str, **kwargs) -> dict:
        """Uninstall a plugin."""
        success = _reg.uninstall(name)
        return {"uninstalled": success}

    def handle_plugin_list(**kwargs) -> dict:
        """List installed plugins."""
        plugins = _reg.list_installed()
        return {
            "total": len(plugins),
            "plugins": [
                {"name": p.name, "version": p.installed_version, "description": p.description}
                for p in plugins
            ],
        }

    def handle_plugin_sync(**kwargs) -> dict:
        """Sync with remote registry."""
        count = _reg.sync()
        return {"synced": count, "total_plugins": len(_reg._plugins)}

    registry.register(ToolDef(
        name="plugin_search",
        description="Search the WW plugin registry by name, keyword, or category.",
        handler=handle_plugin_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query.", "default": ""},
                "category": {"type": "string", "description": "Filter by category.", "default": ""},
            },
            "required": [],
        },
        category="skill",
    ))

    registry.register(ToolDef(
        name="plugin_install",
        description="Install a WW plugin from the registry.",
        handler=handle_plugin_install,
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Plugin name."},
            },
            "required": ["name"],
        },
        category="skill",
    ))

    registry.register(ToolDef(
        name="plugin_uninstall",
        description="Uninstall a WW plugin.",
        handler=handle_plugin_uninstall,
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Plugin name."},
            },
            "required": ["name"],
        },
        category="skill",
    ))

    registry.register(ToolDef(
        name="plugin_list",
        description="List installed WW plugins.",
        handler=handle_plugin_list,
        parameters={"type": "object", "properties": {}, "required": []},
        category="skill",
    ))

    registry.register(ToolDef(
        name="plugin_sync",
        description="Sync with the remote WW plugin registry.",
        handler=handle_plugin_sync,
        parameters={"type": "object", "properties": {}, "required": []},
        category="skill",
    ))
