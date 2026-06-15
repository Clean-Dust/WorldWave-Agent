"""ww/contacts/ — Contact Management Module

A Worldwave agent's address book and communication layer.

Main entry point: ContactManager — start here.
"""

from __future__ import annotations
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .identity import AgentIdentity, is_valid_did
from .permissions import (
    PermissionLevel,
    check_permission,
    required_level_for,
    LEVEL_LABELS,
    advertise_capabilities,
    parse_level,
)
from .roster import (
    Roster,
    RosterEntry,
    TRUST_ACTIVE,
    TRUST_PENDING,
    TRUST_BLOCKED,
)
from .protocol import AgentMessage, INTENT_CHAT, INTENT_TASK, INTENT_HANDSHAKE
from .handshake import HandshakeProtocol, HandshakeResult, encrypt_message, decrypt_message
from .discovery import NetworkDiscovery, DiscoveredPeer

logger = logging.getLogger("ww.contacts")

CONTACTS_DIR = os.environ.get(
    "WW_CONTACTS_DIR",
    os.path.join(os.path.expanduser("~"), ".ww_data", "contacts"),
)


class ContactManager:
    """Central entry point for the contacts system.

    Usage:
        contacts = ContactManager()
        contacts.start()

        # Send friend request
        contacts.send_invitation("did:ww:abc123...")

        # List contacts
        for c in contacts.list_contacts():
            print(c.alias, c.level)

        # Send message
        contacts.send_message("did:ww:abc123...", "Hello!")

        # Receive message handler
        contacts.on_message = lambda msg: print(f"Received: {msg.body}")
    """

    def __init__(self, contacts_dir: str = CONTACTS_DIR):
        self._dir = contacts_dir
        os.makedirs(self._dir, exist_ok=True)

        # Identity
        self.identity = AgentIdentity(contacts_dir)

        # Roster
        self.roster = Roster(os.path.join(self._dir, "roster.db"))

        # Discovery
        self.discovery = NetworkDiscovery(self.identity)
        self.discovery.on_discover = self._on_discovered_peer

        # Handshake
        self.handshake = HandshakeProtocol(
            identity=self.identity,
            roster_store=self._save_contact_from_handshake,
            on_accept=self._on_handshake_accept,
            on_reject=self._on_handshake_reject,
        )

        # Message handlers
        self.on_message: Optional[Callable[[AgentMessage], None]] = None
        self.on_friend_request: Optional[Callable[[str, str], None]] = None
        self.on_friend_accept: Optional[Callable[[str], None]] = None

        # Internal state
        self._running = False
        self._message_queue: List[AgentMessage] = []
        self._queue_lock = threading.Lock()

    # ── Lifecycle ──

    def start(self):
        """Start discovery and background services."""
        if self._running:
            return
        self._running = True
        self.discovery.start()
        logger.info(
            "Contacts started: %s (%s)",
            self.identity.label, self.identity.friend_code,
        )

    def stop(self):
        """Stop all background services."""
        self._running = False
        self.discovery.stop()
        logger.info("Contacts stopped")

    @property
    def status(self) -> dict:
        """Get overall status of the contacts system."""
        return {
            "did": self.identity.did,
            "friend_code": self.identity.friend_code,
            "label": self.identity.label,
            "contacts": self.roster.count(),
            "peers_on_network": len(self.discovery.discovered_peers),
        }

    # ── Friend management ──

    def my_id(self) -> dict:
        """Get our own identity info (share this to make friends)."""
        return self.identity.to_dict()

    def list_contacts(self) -> list:
        """List all active contacts."""
        return [c.to_dict() for c in self.roster.list_active()]

    def list_all_contacts(self) -> list:
        """List every contact including blocked."""
        return [c.to_dict() for c in self.roster.list_all()]

    def search_contacts(self, query: str) -> list:
        """Search contacts by name or code."""
        return [c.to_dict() for c in self.roster.search(query)]

    def get_contact(self, did_or_code: str) -> Optional[dict]:
        """Get a contact by DID or friend code."""
        contact = self.roster.get(did_or_code)
        if not contact:
            contact = self.roster.get_by_friend_code(did_or_code)
        return contact.to_dict() if contact else None

    def get_discovered_peers(self) -> list:
        """List agents discovered on the network."""
        return [p.to_dict() for p in self.discovery.discovered_peers]

    def send_invitation(self, did: str) -> dict:
        """Send a friend request to another agent.

        Args:
            did: The target agent's DID

        Returns:
            Result dict with success/error
        """
        msg = self.handshake.create_invitation(did)
        if not msg:
            return {"success": False, "error": f"Invalid DID: {did}"}

        # Try to send via discovered peer
        delivered, transport = self._send_via_transport(msg.to_json(), did, "invitation")
        if delivered:
            return {
                "success": True,
                "message": f"Invitation sent to {did[:16]}",
                "transport": transport,
            }

        # Fallback: queue for relay
        self._queue_message(msg)
        return {
            "success": True,
            "message": f"Invitation queued for {did[:16]} (peer offline)",
            "queued": True,
        }

    def accept_invitation(self, did: str, level: int = 1) -> dict:
        """Accept a pending friend request.

        Args:
            did: The DID of the agent who invited us
            level: Permission level to grant (1=CONTACT, 2=PARTNER, 3=TRUSTED)

        Returns:
            Result dict
        """
        pending = self.handshake.pending_invitations()
        match = next((p for p in pending if p["did"] == did), None)
        if not match:
            return {"success": False, "error": f"No pending invitation from {did[:16]}"}

        # Create a synthetic invitation message to accept
        inv = AgentMessage(
            from_did=did,
            to_did=self.identity.did,
            intent=INTENT_HANDSHAKE,
            body={
                "action": "request",
                "public_key_hex": "",  # We don't have the key from pending
                "agent_info": {"label": match["alias"]},
            },
        )
        accept_msg = self.handshake.create_acceptance(inv, parse_level(level))

        # Try to send
        self._send_via_transport(accept_msg.to_json(), did, "acceptance")

        return {
            "success": True,
            "message": f"Accepted {match['alias']} (level={level})",
            "contact": did,
            "level": int(level),
        }

    def reject_invitation(self, did: str, reason: str = "Declined") -> dict:
        """Reject a pending friend request."""
        inv = AgentMessage(
            from_did=did,
            to_did=self.identity.did,
            intent=INTENT_HANDSHAKE,
            body={"action": "request"},
        )
        reject_msg = self.handshake.create_rejection(inv, reason)

        self._send_via_transport(reject_msg.to_json(), did, "rejection")

        return {"success": True, "message": f"Rejected {did[:16]}"}

    def pending_invitations(self) -> list:
        """List pending friend requests."""
        return self.handshake.pending_invitations()

    def set_permission(self, did: str, level: int) -> dict:
        """Change a contact's permission level.

        Args:
            did: Contact's DID
            level: 1=CONTACT, 2=PARTNER, 3=TRUSTED

        Returns:
            Result dict
        """
        entry = self.roster.get(did)
        if not entry:
            return {"success": False, "error": f"Contact {did[:16]} not found"}
        parsed = parse_level(level)
        entry.level = parsed
        self.roster.update(entry)
        logger.info("Permission updated: %s → %s", did[:16], LEVEL_LABELS.get(parsed, parsed))
        return {"success": True, "did": did, "level": int(parsed), "label": LEVEL_LABELS.get(parsed, "")}

    def remove_contact(self, did: str) -> dict:
        """Remove a contact."""
        removed = self.roster.remove(did)
        return {"success": removed, "error": "" if removed else f"Contact {did[:16]} not found"}

    # ── Messaging ──

    def send_message(self, to_did: str, text: str, intent: str = INTENT_CHAT) -> dict:
        """Send a message to a contact.

        Args:
            to_did: Recipient's DID
            text: Message content
            intent: Message intent (chat, task, query, etc.)

        Returns:
            Result dict
        """
        # Check permission
        contact = self.roster.get(to_did)
        if not contact:
            # Try friend code
            contact = self.roster.get_by_friend_code(to_did)
        if not contact:
            return {"success": False, "error": f"Not a contact: {to_did[:16]}"}

        if contact.trust_state != TRUST_ACTIVE:
            return {"success": False, "error": "Contact not active (handshake incomplete)"}

        if not check_permission(contact.level, PermissionLevel.CONTACT):
            return {"success": False, "error": "Insufficient permission to send messages"}

        # Build message
        msg = AgentMessage(
            from_did=self.identity.did,
            to_did=contact.did,
            intent=intent,
            body=text,
        )

        # Try to encrypt (best-effort)
        if contact.public_key_hex:
            try:
                from .handshake import encrypt_message
                # Derive shared secret (simplified: use HMAC of our priv + their pub)
                import hashlib, hmac
                secret = hmac.new(
                    self.identity.export_private_key(),
                    bytes.fromhex(contact.public_key_hex),
                    hashlib.sha256,
                ).digest()
                encrypted = encrypt_message(text, secret)
                if encrypted:
                    msg.body = {"encrypted": encrypted, "method": "hmac-sha256"}
                    msg.metadata["e2ee"] = True
            except Exception:
                pass  # Send in plaintext (no E2EE)

        # Deliver via available transports (HTTP > MQTT)
        payload = msg.to_json()
        peer = self.discovery.find_peer(contact.did)

        delivered = False
        transport_used = "none"

        if peer:
            # Try HTTP direct
            from .transports import http as http_transport
            if http_transport.send(payload, peer.ip, peer.port):
                delivered = True
                transport_used = "http"
                self.roster.update_last_seen(contact.did, peer.ip, peer.port)

        if not delivered:
            # Try MQTT (requires Mosquitto + peer's friend code)
            from .transports import mqtt as mqtt_transport
            from .transports import mqtt_available
            if mqtt_available():
                topic = mqtt_transport.topic_for(contact.friend_code)
                if mqtt_transport.send(payload, topic):
                    delivered = True
                    transport_used = "mqtt"

        if delivered:
            return {"success": True, "delivered": True, "transport": transport_used, "to": contact.alias}
        else:
            self._queue_message(msg)
            return {"success": True, "delivered": False, "to": contact.alias, "queued": True}

    def receive_message(self, raw: str) -> Optional[AgentMessage]:
        """Receive and process an incoming message.

        Returns:
            Parsed AgentMessage, or None if invalid.
        """
        try:
            msg = AgentMessage.from_json(raw)
        except Exception as e:
            logger.warning("Failed to parse message: %s", e)
            return None

        # Handle handshake
        if msg.intent == INTENT_HANDSHAKE:
            result = self.handshake.receive_invitation(msg)
            if result.success:
                body = msg.body if isinstance(msg.body, dict) else {}
                action = body.get("action", "")
                if action == "request":
                    if self.on_friend_request:
                        self.on_friend_request(result.contact_did, result.contact_alias)
                elif action == "accept":
                    if self.on_friend_accept:
                        self.on_friend_accept(result.contact_did)
            return msg

        # Handle regular messages
        # Verify sender is a contact
        contact = self.roster.get(msg.from_did)
        if not contact or contact.trust_state != TRUST_ACTIVE:
            logger.warning("Message from unknown/non-active contact: %s", msg.from_did[:16])
            return msg  # Still return it, caller decides how to handle

        # Update last seen
        self.roster.update_last_seen(msg.from_did)

        # Try to decrypt
        if isinstance(msg.body, dict) and msg.body.get("encrypted"):
            if contact.public_key_hex:
                import hashlib, hmac
                secret = hmac.new(
                    self.identity.export_private_key(),
                    bytes.fromhex(contact.public_key_hex),
                    hashlib.sha256,
                ).digest()
                decrypted = decrypt_message(msg.body["encrypted"], secret)
                if decrypted:
                    msg.body = decrypted

        # Dispatch to handler
        if self.on_message:
            self.on_message(msg)

        return msg

    # ── Tools for WW agent ──

    def register_tools(self, registry):
        """Register contacts-related tools in the WW tool registry."""
        from tools.registry import ToolDef, PERMISSION_SAFE

        tools = [
            ToolDef(
                "contacts_my_id", "Show my agent identity (DID + friend code). Share this to let others add you.",
                lambda _: {"success": True, **self.my_id()},
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_list", "List all active contacts with their permission levels.",
                lambda _: {"success": True, "contacts": self.list_contacts()},
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_peers", "List other WW agents discovered on the local network.",
                lambda _: {"success": True, "peers": self.get_discovered_peers()},
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_add", "Send a friend request to another agent by DID or friend code.",
                lambda params: self.send_invitation(params.get("did", "")),
                parameters={"did": {"type": "string", "description": "Target agent's DID or friend code"}},
                examples=['contacts_add(did="did:ww:abc123...")'],
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_accept", "Accept a pending friend request and set permission level.",
                lambda params: self.accept_invitation(params.get("did", ""), params.get("level", 1)),
                parameters={
                    "did": {"type": "string", "description": "DID of the agent to accept"},
                    "level": {"type": "integer", "description": "Permission level (1=Contact, 2=Partner, 3=Trusted)", "default": 1},
                },
                examples=['contacts_accept(did="did:ww:abc...", level=2)'],
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_pending", "List pending friend requests.",
                lambda _: {"success": True, "pending": self.pending_invitations()},
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_set_permission", "Change a contact's permission level (1=Contact, 2=Partner, 3=Trusted).",
                lambda params: self.set_permission(params.get("did", ""), params.get("level", 1)),
                parameters={
                    "did": {"type": "string", "description": "Contact's DID"},
                    "level": {"type": "integer", "description": "New permission level (1, 2, or 3)"},
                },
                examples=['contacts_set_permission(did="did:ww:abc...", level=3)'],
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_send", "Send a message to a contact.",
                lambda params: self.send_message(params.get("to", ""), params.get("text", "")),
                parameters={
                    "to": {"type": "string", "description": "Contact DID or friend code"},
                    "text": {"type": "string", "description": "Message content"},
                },
                examples=['contacts_send(to="did:ww:abc...", text="Need help with a task")'],
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_search", "Search contacts by name, DID, or friend code.",
                lambda params: {"success": True, "results": self.search_contacts(params.get("query", ""))},
                parameters={"query": {"type": "string", "description": "Search term"}},
                category="contacts", permission=PERMISSION_SAFE,
            ),
            ToolDef(
                "contacts_status", "Show contacts module status: your DID, friend code, contact count, network peers.",
                lambda _: {"success": True, **self.status},
                category="contacts", permission=PERMISSION_SAFE,
            ),
        ]

        for tool in tools:
            registry.register(tool)

    # ── Internal callbacks ──

    def _save_contact_from_handshake(self, did: str, label: str, level: PermissionLevel):
        """Save a contact after successful handshake."""
        entry = RosterEntry(
            did=did,
            friend_code=did[-8:],
            alias=label,
            level=level,
            trust_state=TRUST_ACTIVE,
        )
        if not self.roster.add(entry):
            # Already exists — update
            existing = self.roster.get(did)
            if existing:
                existing.alias = label
                existing.level = level
                existing.trust_state = TRUST_ACTIVE
                self.roster.update(existing)

    def _on_discovered_peer(self, peer: DiscoveredPeer):
        """Called when a new peer is discovered on the network."""
        logger.debug("Discovered peer: %s (%s) at %s", peer.label, peer.friend_code, peer.ip)

    def _on_handshake_accept(self, did: str):
        """Called when a handshake is accepted."""
        logger.info("Handshake completed with %s", did[:16])
        if self.on_friend_accept:
            self.on_friend_accept(did)

    def _on_handshake_reject(self, did: str, reason: str):
        """Called when a handshake is rejected."""
        logger.info("Handshake rejected by %s: %s", did[:16], reason)

    # ── Internal transport ──

    def _send_via_transport(self, payload: str, did: str, friend_code: str) -> tuple:
        """Deliver a payload via available transports.

        Returns:
            (delivered: bool, transport_name: str)
        """
        peer = self.discovery.find_peer(did)
        if peer:
            from .transports import http as http_transport
            if http_transport.send(payload, peer.ip, peer.port):
                self.roster.update_last_seen(did, peer.ip, peer.port)
                return (True, "http")

        from .transports import mqtt as mqtt_transport
        from .transports import mqtt_available
        if mqtt_available():
            topic = mqtt_transport.topic_for(friend_code)
            if mqtt_transport.send(payload, topic):
                return (True, "mqtt")

        return (False, "none")

    def _queue_message(self, msg: AgentMessage):
        """Queue a message for later delivery (not yet persisted)."""
        with self._queue_lock:
            self._message_queue.append(msg)


# ── API endpoints (for server.py integration) ──


def register_api_routes(app, contact_manager: ContactManager):
    """Register contact-related API endpoints on the FastAPI app."""

    @app.get("/contacts/identity")
    def get_identity():
        return {"success": True, **contact_manager.my_id()}

    @app.get("/contacts/list")
    def list_contacts():
        return {"success": True, "contacts": contact_manager.list_contacts()}

    @app.get("/contacts/peers")
    def list_peers():
        return {"success": True, "peers": contact_manager.get_discovered_peers()}

    @app.get("/contacts/pending")
    def pending_invitations():
        return {"success": True, "pending": contact_manager.pending_invitations()}

    @app.get("/contacts/search")
    def search_contacts(query: str = ""):
        return {"success": True, "results": contact_manager.search_contacts(query)}

    @app.post("/contacts/invite")
    def invite_contact(data: dict):
        did = data.get("did", "")
        return contact_manager.send_invitation(did)

    @app.post("/contacts/accept")
    def accept_contact(data: dict):
        did = data.get("did", "")
        level = data.get("level", 1)
        return contact_manager.accept_invitation(did, level)

    @app.post("/contacts/set_permission")
    def set_contact_permission(data: dict):
        did = data.get("did", "")
        level = data.get("level", 1)
        return contact_manager.set_permission(did, level)

    @app.post("/contacts/remove")
    def remove_contact(data: dict):
        did = data.get("did", "")
        return contact_manager.remove_contact(did)

    @app.post("/contacts/send")
    def send_contact_message(data: dict):
        to_did = data.get("to", "")
        text = data.get("text", "")
        intent = data.get("intent", "chat")
        return contact_manager.send_message(to_did, text, intent)

    @app.post("/contacts/inbox")
    async def contact_inbox(request):
        """Inbox endpoint — other agents send messages here."""
        body = await request.body()
        raw = body.decode()
        msg = contact_manager.receive_message(raw)
        if msg:
            return {"success": True, "received": msg.msg_id}
        return {"success": False, "error": "Invalid message"}
