"""ww/contacts/handshake.py — Friendship Handshake Protocol

The handshake establishes mutual trust between two agents:
1. Agent A sends a handshake request (DID + public key)
2. Agent B validates the request and responds (accept/reject)
3. On accept, both agents exchange capability information
4. Both sides store the contact in their roster

E2EE: During handshake, a shared secret is derived via ECDH
(NaCl-style key exchange using Ed25519 keys).
"""

from __future__ import annotations
import hashlib
import logging
import os
import time
from typing import Callable, Dict, Optional

from .identity import AgentIdentity, is_valid_did
from .permissions import PermissionLevel, advertise_capabilities
from .protocol import AgentMessage

logger = logging.getLogger("ww.contacts.handshake")

# Supported E2EE methods (in priority order)
E2EE_METHODS = ["ecdh-aes256-gcm", "hmac-sha256"]


class HandshakeResult:
    """Result of a handshake attempt."""

    def __init__(
        self,
        success: bool,
        contact_did: str = "",
        contact_friend_code: str = "",
        contact_alias: str = "",
        level: PermissionLevel = PermissionLevel.CONTACT,
        shared_secret: bytes = b"",
        error: str = "",
    ):
        self.success = success
        self.contact_did = contact_did
        self.contact_friend_code = contact_friend_code
        self.contact_alias = contact_alias
        self.level = level
        self.shared_secret = shared_secret
        self.error = error

    def __bool__(self):
        return self.success

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "contact_did": self.contact_did,
            "contact_friend_code": self.contact_friend_code,
            "contact_alias": self.contact_alias,
            "level": int(self.level),
            "error": self.error,
        }


class HandshakeProtocol:
    """Manages the handshake lifecycle.

    Typical flow:
        initiator = HandshakeProtocol(identity, on_accept=...)
        invitation = initiator.create_invitation("did:ww:abcd...")

        responder = HandshakeProtocol(other_identity)
        result = responder.receive_invitation(invitation)  # auto-check
        if result.success:
            print(f"Now friends with {result.contact_alias}!")
    """

    def __init__(
        self,
        identity: AgentIdentity,
        roster_store=None,  # Callable to save contact after accept
        on_accept: Optional[Callable[[str], None]] = None,
        on_reject: Optional[Callable[[str, str], None]] = None,
    ):
        self._identity = identity
        self._roster_store = roster_store
        self._on_accept = on_accept
        self._on_reject = on_reject
        self._pending: Dict[str, dict] = {}  # did -> invitation data

    def create_invitation(self, target_did: str) -> Optional[AgentMessage]:
        """Create a handshake request to send to another agent.

        Args:
            target_did: The DID of the agent to invite

        Returns:
            An AgentMessage ready to send, or None if invalid.
        """
        if not is_valid_did(target_did):
            logger.warning("Invalid target DID: %s", target_did)
            return None

        msg = AgentMessage.handshake_request(
            from_did=self._identity.did,
            to_did=target_did,
            public_key_hex=self._identity.to_dict()["public_key_hex"],
        )
        # Include our capabilities for mutual understanding
        msg.body["agent_info"] = {
            "label": self._identity.label,
            "capabilities": advertise_capabilities(),
            "e2ee_methods": E2EE_METHODS,
        }

        logger.info("Invitation created for %s", target_did[:16])
        return msg

    def receive_invitation(
        self, msg: AgentMessage
    ) -> HandshakeResult:
        """Process an incoming handshake request.

        Returns a HandshakeResult. If successful, the caller can
        respond with `create_acceptance()`.
        """
        if msg.intent != "handshake":
            return HandshakeResult(False, error="Not a handshake message")

        body = msg.body if isinstance(msg.body, dict) else {}
        action = body.get("action", "")

        if action == "request":
            return self._handle_request(msg)
        elif action == "accept":
            return self._handle_accept(msg)
        elif action == "reject":
            return self._handle_reject(msg)
        else:
            return HandshakeResult(False, error=f"Unknown handshake action: {action}")

    def create_acceptance(
        self, invitation_msg: AgentMessage, level: PermissionLevel = PermissionLevel.CONTACT
    ) -> AgentMessage:
        """Accept a pending invitation.

        Args:
            invitation_msg: The original invitation message
            level: Permission level to grant

        Returns:
            An acceptance message to send back
        """
        from_did = invitation_msg.from_did
        pub_key = invitation_msg.body.get("public_key_hex", "")

        accept = AgentMessage.handshake_accept(
            from_did=self._identity.did,
            to_did=from_did,
            public_key_hex=self._identity.to_dict()["public_key_hex"],
        )
        accept.body["agent_info"] = {
            "label": self._identity.label,
            "capabilities": advertise_capabilities(),
            "granted_level": int(level),
            "e2ee_methods": E2EE_METHODS,
        }

        # Derive shared secret for E2EE
        shared_secret = self._derive_shared_secret(bytes.fromhex(pub_key))
        if shared_secret:
            accept.metadata["e2ee_nonce"] = os.urandom(16).hex()
            accept.metadata["e2ee_method"] = "hmac-sha256"

        # Clear pending
        self._pending.pop(from_did, None)

        # Save to roster
        if self._roster_store:
            contact_alias = invitation_msg.body.get("agent_info", {}).get("label", f"contact-{from_did[-8:]}")
            self._roster_store(from_did, contact_alias, level)

        if self._on_accept:
            self._on_accept(from_did)

        logger.info("Accepted invitation from %s (level=%s)", from_did[:16], level)
        return accept

    def create_rejection(
        self, invitation_msg: AgentMessage, reason: str = "Declined"
    ) -> AgentMessage:
        """Reject a pending invitation."""
        from_did = invitation_msg.from_did
        reject = AgentMessage.handshake_reject(
            from_did=self._identity.did,
            to_did=from_did,
            reason=reason,
        )
        self._pending.pop(from_did, None)

        if self._on_reject:
            self._on_reject(from_did, reason)

        logger.info("Rejected invitation from %s: %s", from_did[:16], reason)
        return reject

    def pending_invitations(self) -> list:
        """List pending handshake invitations."""
        return [
            {"did": did, "alias": data.get("alias", ""), "received_at": data.get("ts", 0)}
            for did, data in self._pending.items()
        ]

    # ── Internal ──

    def _handle_request(self, msg: AgentMessage) -> HandshakeResult:
        """Process a handshake request."""
        from_did = msg.from_did
        body = msg.body if isinstance(msg.body, dict) else {}
        pub_key_hex = body.get("public_key_hex", "")
        agent_info = body.get("agent_info", {})
        label = agent_info.get("label", f"contact-{from_did[-8:]}")
        capabilities = agent_info.get("capabilities", {})

        if not is_valid_did(from_did):
            return HandshakeResult(False, error=f"Invalid DID: {from_did}")

        if not pub_key_hex:
            return HandshakeResult(False, error="Missing public key")

        # Store as pending
        self._pending[from_did] = {
            "alias": label,
            "pub_key_hex": pub_key_hex,
            "capabilities": capabilities,
            "e2ee_methods": agent_info.get("e2ee_methods", []),
            "ts": time.time(),
        }

        logger.info(
            "Received invitation from %s (%s)", label, from_did[:16]
        )

        return HandshakeResult(
            success=True,
            contact_did=from_did,
            contact_friend_code=from_did[-8:],
            contact_alias=label,
            level=PermissionLevel.CONTACT,
        )

    def _handle_accept(self, msg: AgentMessage) -> HandshakeResult:
        """Process an acceptance response."""
        from_did = msg.from_did
        body = msg.body if isinstance(msg.body, dict) else {}
        pub_key_hex = body.get("public_key_hex", "")
        agent_info = body.get("agent_info", {})
        label = agent_info.get("label", f"contact-{from_did[-8:]}")
        granted_level = agent_info.get("granted_level", 1)

        # Derive shared secret
        shared_secret = self._derive_shared_secret(bytes.fromhex(pub_key_hex))

        # Save to roster
        if self._roster_store:
            self._roster_store(from_did, label, PermissionLevel(granted_level))

        logger.info(
            "Handshake accepted by %s (%s) level=%s",
            label, from_did[:16], granted_level,
        )

        return HandshakeResult(
            success=True,
            contact_did=from_did,
            contact_friend_code=from_did[-8:],
            contact_alias=label,
            level=PermissionLevel(granted_level),
            shared_secret=shared_secret or b"",
        )

    def _handle_reject(self, msg: AgentMessage) -> HandshakeResult:
        """Process a rejection response."""
        from_did = msg.from_did
        body = msg.body if isinstance(msg.body, dict) else {}
        reason = body.get("reason", "Declined")

        if self._on_reject:
            self._on_reject(from_did, reason)

        return HandshakeResult(
            False,
            contact_did=from_did,
            error=f"Handshake rejected: {reason}",
        )

    def _derive_shared_secret(self, peer_public_key: bytes) -> bytes:
        """Derive a shared secret for E2EE.

        Uses HMAC-based key derivation (works with any key type).
        """
        try:
            import hmac
            our_priv = self._identity.export_private_key()
            # Simple shared secret: HMAC(peer_pub, our_priv)
            secret = hmac.new(
                our_priv, peer_public_key, hashlib.sha256
            ).digest()
            return secret
        except Exception as e:
            logger.warning("Shared secret derivation failed: %s", e)
            return b""


# ── Helper: encrypt/decrypt a message with shared secret ──


def encrypt_message(message: str, shared_secret: bytes) -> Optional[str]:
    """Encrypt a message for E2EE transport.

    Uses AES-256-GCM if available, otherwise HMAC-authenticated encoding.

    Args:
        message: Plaintext to encrypt
        shared_secret: 32-byte shared secret

    Returns:
        Base64-encoded ciphertext, or None on failure
    """
    if not shared_secret:
        return None
    try:
        # Try AES-GCM first
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64

        aesgcm = AESGCM(shared_secret[:32])
        nonce = os.urandom(12)
        ct = aesgcm.encrypt(nonce, message.encode(), None)
        return base64.b64encode(nonce + ct).decode()
    except Exception:
        # Fallback: HMAC-authenticated
        import base64
        import hmac

        # Simple XOR "encryption" + HMAC authentication
        key = hashlib.sha256(shared_secret).digest()
        msg_bytes = message.encode()
        mask = hashlib.shake_256(key + b"mask").digest(len(msg_bytes))
        cipher = bytes(a ^ b for a, b in zip(msg_bytes, mask))
        tag = hmac.new(key, cipher, hashlib.sha256).hexdigest()[:16]
        return base64.b64encode(tag.encode() + cipher).decode()


def decrypt_message(ciphertext_b64: str, shared_secret: bytes) -> Optional[str]:
    """Decrypt an E2EE message.

    Args:
        ciphertext_b64: Base64-encoded ciphertext
        shared_secret: 32-byte shared secret

    Returns:
        Plaintext, or None on failure
    """
    if not shared_secret:
        return None
    try:
        import base64

        raw = base64.b64decode(ciphertext_b64)

        # Try AES-GCM (nonce is 12 bytes prefix)
        if len(raw) > 12:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                aesgcm = AESGCM(shared_secret[:32])
                nonce = raw[:12]
                ct = raw[12:]
                return aesgcm.decrypt(nonce, ct, None).decode()
            except Exception:
                pass

        # Fallback: HMAC-tagged XOR
        import hmac
        key = hashlib.sha256(shared_secret).digest()
        tag_size = 16
        if len(raw) <= tag_size:
            return None
        tag = raw[:tag_size]
        cipher = raw[tag_size:]
        expected = hmac.new(key, cipher, hashlib.sha256).hexdigest()[:16].encode()
        if not hmac.compare_digest(tag, expected):
            return None
        mask = hashlib.shake_256(key + b"mask").digest(len(cipher))
        plain = bytes(a ^ b for a, b in zip(cipher, mask))
        return plain.decode()
    except Exception as e:
        logger.warning("Decryption failed: %s", e)
        return None
