"""Enterprise tools — RBAC, audit, user management."""

from tools.registry import ToolRegistry, ToolDef
from core.enterprise import get_enterprise, Role


def register_tools(registry: ToolRegistry):

    _ent = get_enterprise()

    def handle_user_list(role: str = "", **kwargs) -> dict:
        """List users."""
        users = _ent.rbac.list_users(role=role or "")
        return {
            "total": len(users),
            "users": [u.to_dict() for u in users],
        }

    def handle_user_create(email: str, name: str = "", role: str = Role.VIEWER, **kwargs) -> dict:
        """Create a new user."""
        if role not in Role.ALL:
            return {"error": f"Invalid role: {role}. Must be one of: {', '.join(Role.ALL)}"}
        user = _ent.rbac.create_user(email=email, name=name, role=role)
        _ent.audit.log_config_change("user.create", email, "", role)
        return {"created": True, "user": user.to_dict()}

    def handle_user_set_role(email: str, role: str, **kwargs) -> dict:
        """Change a user's role."""
        if role not in Role.ALL:
            return {"error": f"Invalid role: {role}"}
        success = _ent.rbac.update_role(email, role)
        _ent.audit.log_config_change("user.role", email, "", role)
        return {"updated": success}

    def handle_user_deactivate(email: str, **kwargs) -> dict:
        """Deactivate a user."""
        success = _ent.rbac.deactivate(email)
        _ent.audit.log_config_change("user.deactivate", email, "", "")
        return {"deactivated": success}

    def handle_audit_query(event_type: str = "", user_email: str = "", limit: int = 50, **kwargs) -> dict:
        """Query audit log."""
        events = _ent.audit.query(
            event_type=event_type or "",
            user_email=user_email or "",
            limit=limit,
        )
        return {"total": len(events), "events": events[:limit]}

    def handle_audit_stats(**kwargs) -> dict:
        """Get audit log statistics."""
        return _ent.audit.get_stats()

    registry.register(ToolDef(
        name="user_list",
        description="List all enterprise users. Optional role filter.",
        handler=handle_user_list,
        parameters={
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Filter by role: admin, developer, viewer", "default": ""},
            },
            "required": [],
        },
        category="config",
    ))

    registry.register(ToolDef(
        name="user_create",
        description="Create a new enterprise user with specified role.",
        handler=handle_user_create,
        parameters={
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "User email."},
                "name": {"type": "string", "description": "Display name.", "default": ""},
                "role": {"type": "string", "description": "Role: admin, developer, viewer", "default": Role.VIEWER},
            },
            "required": ["email"],
        },
        category="config",
    ))

    registry.register(ToolDef(
        name="user_set_role",
        description="Change a user's role (admin, developer, viewer).",
        handler=handle_user_set_role,
        parameters={
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "User email."},
                "role": {"type": "string", "description": "New role."},
            },
            "required": ["email", "role"],
        },
        category="config",
    ))

    registry.register(ToolDef(
        name="user_deactivate",
        description="Deactivate a user (soft delete).",
        handler=handle_user_deactivate,
        parameters={
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "User email."},
            },
            "required": ["email"],
        },
        category="config",
    ))

    registry.register(ToolDef(
        name="audit_query",
        description="Query the enterprise audit log for tool calls, config changes, auth events.",
        handler=handle_audit_query,
        parameters={
            "type": "object",
            "properties": {
                "event_type": {"type": "string", "description": "Event type filter: tool_call, config_change, auth, permission_denied", "default": ""},
                "user_email": {"type": "string", "description": "Filter by user email.", "default": ""},
                "limit": {"type": "integer", "description": "Max events.", "default": 50},
            },
            "required": [],
        },
        category="config",
    ))

    registry.register(ToolDef(
        name="audit_stats",
        description="Get audit log statistics: total events, size, breakdown by type.",
        handler=handle_audit_stats,
        parameters={"type": "object", "properties": {}, "required": []},
        category="config",
    ))
