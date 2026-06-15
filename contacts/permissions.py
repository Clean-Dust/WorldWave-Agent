"""ww/contacts/permissions.py — Capability-based Permission Levels

Three-tier permission model for agent-to-agent relationships:

- CONTACT (Level 1): Text messages only, no data access, no tool execution
- PARTNER (Level 2): + read shared context/memory, see agent status
- TRUSTED (Level 3): + cross-node tool calling, code execution, file access

Each contact gets a permission level stored in their roster entry.
A contact's effective permissions are the MIN of:
  (a) their assigned level in OUR roster
  (b) the level WE advertise in our capabilities
"""

from __future__ import annotations
from enum import IntEnum
from typing import Dict, Any


class PermissionLevel(IntEnum):
    """Permission levels — higher number = more trust."""

    NONE = 0       # Not a contact (stranger)
    CONTACT = 1    # Can send/receive text messages
    PARTNER = 2    # Can read shared context/memory
    TRUSTED = 3    # Can call tools on this node


# ── Human-readable labels ──

LEVEL_LABELS: Dict[PermissionLevel, str] = {
    PermissionLevel.NONE: "Stranger",
    PermissionLevel.CONTACT: "Contact",
    PermissionLevel.PARTNER: "Partner",
    PermissionLevel.TRUSTED: "Trusted",
}

LEVEL_DESCRIPTIONS: Dict[PermissionLevel, str] = {
    PermissionLevel.NONE: "No access — blocked or unknown",
    PermissionLevel.CONTACT: "Text messages only",
    PermissionLevel.PARTNER: "Text + shared context/memory read",
    PermissionLevel.TRUSTED: "Full access — tools, files, code execution",
}


# ── Capability matrix ──

CAPABILITIES: Dict[str, Dict[str, PermissionLevel]] = {
    "message": {
        "send_text": PermissionLevel.CONTACT,
        "send_file": PermissionLevel.CONTACT,
    },
    "data": {
        "read_context": PermissionLevel.PARTNER,
        "read_memory": PermissionLevel.PARTNER,
        "share_state": PermissionLevel.PARTNER,
    },
    "tools": {
        "shell": PermissionLevel.TRUSTED,
        "file_read": PermissionLevel.TRUSTED,
        "file_write": PermissionLevel.TRUSTED,
        "code_exec": PermissionLevel.TRUSTED,
        "agent_invoke": PermissionLevel.TRUSTED,
    },
}


def check_permission(
    contact_level: PermissionLevel, required_level: PermissionLevel
) -> bool:
    """Check if a contact has sufficient permission for an action.

    Args:
        contact_level: The contact's assigned permission level
        required_level: The minimum level required for the action

    Returns:
        True if contact_level >= required_level
    """
    return int(contact_level) >= int(required_level)


def required_level_for(capability: str) -> PermissionLevel:
    """Look up the minimum permission level for a capability.

    Args:
        capability: Dot-notation path like 'tools.shell' or 'message.send_text'

    Returns:
        The minimum PermissionLevel, or TRUSTED if unknown (fail-safe).
    """
    parts = capability.split(".", 1)
    if len(parts) == 2:
        category, action = parts
        cat = CAPABILITIES.get(category, {})
        return cat.get(action, PermissionLevel.TRUSTED)
    # Unknown capability → require TRUSTED (deny by default)
    return PermissionLevel.TRUSTED


def advertise_capabilities() -> Dict[str, Any]:
    """Return the node's capability matrix for sharing with contacts.

    During handshake, agents exchange these to negotiate effective permissions.
    """
    return {
        "max_level": int(PermissionLevel.TRUSTED),
        "capabilities": {
            cat: {act: int(level) for act, level in actions.items()}
            for cat, actions in CAPABILITIES.items()
        },
    }


def parse_level(value) -> PermissionLevel:
    """Parse a permission level from int, str, or PermissionLevel.

    Args:
        value: 0-3, 'NONE', 'CONTACT', 'PARTNER', 'TRUSTED, or PermissionLevel

    Returns:
        Parsed PermissionLevel, defaults to CONTACT on failure.
    """
    if isinstance(value, PermissionLevel):
        return value
    if isinstance(value, str):
        try:
            return PermissionLevel[value.upper()]
        except (KeyError, AttributeError):
            pass
        try:
            return PermissionLevel(int(value))
        except (ValueError, TypeError):
            pass
    if isinstance(value, (int, float)):
        try:
            return PermissionLevel(int(value))
        except ValueError:
            pass
    return PermissionLevel.CONTACT
