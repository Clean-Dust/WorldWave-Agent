"""ww/contacts/identity.py — Agent Identity & DID

Every Worldwave agent generates a unique identity on first launch:
- Ed25519 key pair (via cryptography library)
- DID = fingerprint of the public key (first 16 bytes → hex, like a crypto address)
- Human-readable "friend code" = first 8 chars of DID (like Venmo/微信 ID)

No central registry needed — identity is self-sovereign.
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import time
from typing import Optional, Tuple

logger = logging.getLogger("ww.contacts.identity")

# Path for persistent identity storage
IDENTITY_DIR = os.environ.get(
    "WW_CONTACTS_DIR",
    os.path.join(os.path.expanduser("~"), ".ww_data", "contacts"),
)

IDENTITY_FILE = os.path.join(IDENTITY_DIR, "identity.json")


class AgentIdentity:
    """Self-sovereign identity for a Worldwave agent.

    On init, loads existing identity or generates a fresh one.
    """

    def __init__(self, identity_dir: str = IDENTITY_DIR):
        self._dir = identity_dir
        os.makedirs(self._dir, exist_ok=True)
        self._private_key: Optional[bytes] = None
        self._public_key: Optional[bytes] = None
        self._did: str = ""
        self._friend_code: str = ""
        self._label: str = ""  # Optional human-friendly name

        self._load_or_create()

    # ── Properties ──

    @property
    def did(self) -> str:
        return self._did

    @property
    def friend_code(self) -> str:
        return self._friend_code

    @property
    def label(self) -> str:
        return self._label if self._label else f"agent-{self._friend_code}"

    @label.setter
    def label(self, value: str):
        self._label = value.strip()
        self._save()

    # ── Public API ──

    def sign(self, message: bytes) -> Optional[bytes]:
        """Sign a message with the agent's private key.

        Returns signature bytes, or None if crypto library unavailable.
        """
        try:
            from cryptography.hazmat.primitives.asymmetric import ed25519
            from cryptography.hazmat.primitives import serialization

            priv = serialization.load_der_private_key(self._private_key, password=None)
            if isinstance(priv, ed25519.Ed25519PrivateKey):
                return priv.sign(message)
        except Exception as e:
            logger.warning("Sign failed: %s", e)
        return None

    def verify(self, message: bytes, signature: bytes, public_key: bytes) -> bool:
        """Verify a signature against a public key.

        Returns True if valid, False otherwise.
        """
        try:
            from cryptography.hazmat.primitives.asymmetric import ed25519
            from cryptography.hazmat.primitives import serialization

            pub = serialization.load_der_public_key(public_key)
            if isinstance(pub, ed25519.Ed25519PublicKey):
                pub.verify(signature, message)
                return True
        except Exception:
            pass
        return False

    def to_dict(self) -> dict:
        """Export public identity (safe to share)."""
        return {
            "did": self._did,
            "friend_code": self._friend_code,
            "label": self.label,
            "public_key_hex": self._public_key.hex() if self._public_key else "",
            "created_at": getattr(self, "_created_at", 0),
        }

    def export_public_key(self) -> bytes:
        """Export DER-encoded public key for sharing during handshake."""
        return self._public_key if self._public_key else b""

    def export_private_key(self) -> bytes:
        """Export DER-encoded private key (NEVER share this)."""
        return self._private_key if self._private_key else b""

    # ── Internal ──

    @property
    def _file_path(self) -> str:
        """Path to the identity file within the instance directory."""
        return os.path.join(self._dir, "identity.json")

    def _load_or_create(self):
        """Load identity from disk or generate a fresh one."""
        fp = self._file_path
        if os.path.isfile(fp):
            try:
                with open(fp) as f:
                    data = json.load(f)
                self._private_key = bytes.fromhex(data["private_key"])
                self._public_key = bytes.fromhex(data["public_key"])
                self._did = data["did"]
                self._friend_code = data["friend_code"]
                self._label = data.get("label", "")
                self._created_at = data.get("created_at", 0)
                logger.info(
                    "Identity loaded: %s (%s)", self._did[:16], self._friend_code
                )
                return
            except Exception as e:
                logger.warning("Failed to load identity: %s. Regenerating.", e)

        # Generate fresh identity
        self._generate()
        self._save()
        logger.info(
            "New identity created: %s (%s)", self._did[:16], self._friend_code
        )

    def _generate(self):
        """Generate a new Ed25519 key pair and derive DID."""
        try:
            from cryptography.hazmat.primitives.asymmetric import ed25519
            from cryptography.hazmat.primitives import serialization

            private_key = ed25519.Ed25519PrivateKey.generate()
            public_key = private_key.public_key()

            self._private_key = private_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            self._public_key = public_key.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        except Exception as e:
            # Fallback: use HMAC-based identity (no ed25519)
            logger.warning("Ed25519 unavailable (%s), using HMAC fallback.", e)
            seed = os.urandom(32)
            h = hashlib.sha256(seed)
            key_material = h.digest()
            self._private_key = key_material + os.urandom(32)
            self._public_key = hashlib.sha256(self._private_key).digest()

        # DID = first 16 bytes of public key hash → hex
        pk_hash = hashlib.sha256(self._public_key).digest()
        self._did = "did:ww:" + pk_hash[:16].hex()
        self._friend_code = pk_hash[:4].hex()  # 8 chars, like Venmo ID
        self._created_at = int(time.time())

    def _save(self):
        """Persist identity to disk."""
        data = {
            "private_key": self._private_key.hex(),
            "public_key": self._public_key.hex(),
            "did": self._did,
            "friend_code": self._friend_code,
            "label": self._label,
            "created_at": self._created_at,
        }
        with open(self._file_path, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(self._file_path, 0o600)  # Protect private key


# ── Helper: validate a DID string ──

DID_PATTERN = r"^did:ww:[0-9a-f]{32}$"


def is_valid_did(did: str) -> bool:
    """Check if a string is a valid WW DID."""
    import re
    return bool(re.match(DID_PATTERN, did))


def did_from_public_key(pub_key_bytes: bytes) -> str:
    """Derive DID from raw public key bytes."""
    pk_hash = hashlib.sha256(pub_key_bytes).digest()
    return "did:ww:" + pk_hash[:16].hex()
