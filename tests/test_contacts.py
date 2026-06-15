"""Tests for the contacts module (distributed agent address book)."""

import os
import json
import tempfile
import shutil
import pytest

from contacts.identity import AgentIdentity, did_from_public_key, is_valid_did
from contacts.roster import Roster, RosterEntry
from contacts.permissions import (
    PermissionLevel, check_permission, required_level_for, LEVEL_LABELS, parse_level
)
from contacts.protocol import AgentMessage, INTENT_CHAT, INTENT_HANDSHAKE, INTENT_QUERY, INTENT_STATUS
from contacts.handshake import encrypt_message, decrypt_message


# ── Helpers ──

def make_identity():
    """Create an AgentIdentity in a temp directory (file-backed)."""
    tmp = tempfile.mkdtemp()
    return AgentIdentity(tmp), tmp


# ── Identity ──

class TestIdentity:
    def test_generate_keys(self):
        ident, tmp = make_identity()
        # did:ww: (7) + 32 hex chars (SHA256[:16]) = 39
        assert ident.did.startswith("did:ww:")
        assert len(ident.did) == 39
        assert len(ident.friend_code) == 8
        shutil.rmtree(tmp)

    def test_sign_and_verify(self):
        ident, tmp = make_identity()
        msg = b"hello world"
        sig = ident.sign(msg)
        assert sig is not None
        assert ident.verify(msg, sig, ident.export_public_key())
        shutil.rmtree(tmp)

    def test_export(self):
        """Export private key DER bytes."""
        ident, tmp = make_identity()
        exported = ident.export_private_key()
        assert isinstance(exported, bytes) and len(exported) > 0
        shutil.rmtree(tmp)

    def test_did_from_public_key(self):
        did1 = did_from_public_key(b"key1")
        did2 = did_from_public_key(b"key2")
        assert did1 != did2
        assert did1.startswith("did:ww:")
        # Same key always produces same DID
        same = did_from_public_key(b"key1")
        assert same == did1

    def test_is_valid_did(self):
        # Must be 32 hex chars
        valid = "did:ww:" + "a" * 32
        assert is_valid_did(valid)
        # Wrong prefix
        assert not is_valid_did("did:xx:" + "a" * 32)
        # Wrong length
        assert not is_valid_did("did:ww:" + "a" * 16)
        # Invalid hex chars
        assert not is_valid_did("did:ww:" + "z" + "a" * 31)


# ── Permissions ──

class TestPermissions:
    def test_level_order(self):
        assert PermissionLevel.CONTACT < PermissionLevel.PARTNER < PermissionLevel.TRUSTED

    def test_check_permission(self):
        assert check_permission(PermissionLevel.CONTACT, PermissionLevel.CONTACT)
        assert check_permission(PermissionLevel.PARTNER, PermissionLevel.CONTACT)
        assert check_permission(PermissionLevel.TRUSTED, PermissionLevel.CONTACT)
        assert check_permission(PermissionLevel.TRUSTED, PermissionLevel.PARTNER)
        assert not check_permission(PermissionLevel.CONTACT, PermissionLevel.PARTNER)
        assert not check_permission(PermissionLevel.PARTNER, PermissionLevel.TRUSTED)

    def test_required_level_for(self):
        # Uses dot-notation: category.action
        assert required_level_for("message.send_text") == PermissionLevel.CONTACT
        assert required_level_for("data.read_memory") == PermissionLevel.PARTNER
        assert required_level_for("tools.shell") == PermissionLevel.TRUSTED
        # Unknown capability → TRUSTED (deny by default)
        assert required_level_for("unknown.action") == PermissionLevel.TRUSTED

    def test_parse_level(self):
        assert parse_level(1) == PermissionLevel.CONTACT
        assert parse_level(2) == PermissionLevel.PARTNER
        assert parse_level(3) == PermissionLevel.TRUSTED
        assert parse_level("contact") == PermissionLevel.CONTACT
        assert parse_level("PARTNER") == PermissionLevel.PARTNER
        assert parse_level("Trusted") == PermissionLevel.TRUSTED
        # Invalid values default to CONTACT (allow-safe default)
        assert parse_level(99) == PermissionLevel.CONTACT
        assert parse_level("garbage") == PermissionLevel.CONTACT

    def test_level_labels(self):
        assert LEVEL_LABELS[PermissionLevel.CONTACT] == "Contact"
        assert LEVEL_LABELS[PermissionLevel.PARTNER] == "Partner"
        assert LEVEL_LABELS[PermissionLevel.TRUSTED] == "Trusted"


# ── Roster (SQLite) ──

class TestRoster:
    @pytest.fixture
    def roster(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        r = Roster(path)
        yield r
        os.unlink(path)

    def test_add_and_get(self, roster):
        entry = RosterEntry(
            did="did:ww:abc123def4567890",
            friend_code="7890abcd",
            alias="Test Agent",
            level=PermissionLevel.CONTACT,
            trust_state="active",
        )
        assert roster.add(entry)
        fetched = roster.get(entry.did)
        assert fetched is not None
        assert fetched.alias == "Test Agent"
        assert fetched.level == PermissionLevel.CONTACT

    def test_no_duplicate(self, roster):
        entry = RosterEntry(
            did="did:ww:abc", friend_code="abc", alias="A",
            level=PermissionLevel.CONTACT, trust_state="active",
        )
        assert roster.add(entry)
        assert not roster.add(entry)  # Duplicate rejected

    def test_update(self, roster):
        entry = RosterEntry(
            did="did:ww:abc", friend_code="abc", alias="A",
            level=PermissionLevel.CONTACT, trust_state="active",
        )
        roster.add(entry)
        entry.alias = "Updated"
        entry.level = PermissionLevel.TRUSTED
        assert roster.update(entry)
        fetched = roster.get(entry.did)
        assert fetched.alias == "Updated"
        assert fetched.level == PermissionLevel.TRUSTED

    def test_remove(self, roster):
        entry = RosterEntry(
            did="did:ww:abc", friend_code="abc", alias="A",
            level=PermissionLevel.CONTACT, trust_state="active",
        )
        roster.add(entry)
        assert roster.remove(entry.did)
        assert not roster.get(entry.did)

    def test_list_and_count(self, roster):
        for i in range(5):
            entry = RosterEntry(
                did=f"did:ww:{i:04x}", friend_code=f"{i:04x}", alias=f"A{i}",
                level=PermissionLevel.CONTACT, trust_state="active" if i < 3 else "pending",
            )
            roster.add(entry)
        stats = roster.count()
        assert stats["total"] == 5
        assert stats["active"] == 3
        assert stats["pending"] == 2
        # list_active includes both pending and active
        active = roster.list_active()
        assert len(active) == 5

    def test_get_by_friend_code(self, roster):
        entry = RosterEntry(
            did="did:ww:abc", friend_code="xyz78901", alias="Test",
            level=PermissionLevel.CONTACT, trust_state="active",
        )
        roster.add(entry)
        fetched = roster.get_by_friend_code("xyz78901")
        assert fetched is not None
        assert fetched.alias == "Test"


# ── Protocol (AgentMessage) ──

class TestProtocol:
    def test_create_message(self):
        msg = AgentMessage(
            from_did="did:ww:sender",
            to_did="did:ww:receiver",
            intent=INTENT_CHAT,
            body="Hello!",
        )
        assert msg.from_did == "did:ww:sender"
        assert msg.intent == INTENT_CHAT
        assert msg.msg_id is not None

    def test_serialization_roundtrip(self):
        msg = AgentMessage(
            from_did="did:ww:a",
            to_did="did:ww:b",
            intent=INTENT_QUERY,
            body={"question": "Are you there?"},
        )
        json_str = msg.to_json()
        parsed = AgentMessage.from_json(json_str)
        assert parsed.from_did == msg.from_did
        assert parsed.to_did == msg.to_did
        assert parsed.intent == msg.intent
        assert parsed.body == msg.body
        assert parsed.msg_id == msg.msg_id

    def test_intents_defined(self):
        assert INTENT_CHAT == "chat"
        assert INTENT_HANDSHAKE == "handshake"
        assert INTENT_QUERY == "query"
        assert INTENT_STATUS == "status"

    def test_from_json(self):
        data = '{"from_did":"a","to_did":"b","intent":"query","body":"hi","id":"test123"}'
        msg = AgentMessage.from_json(data)
        assert msg.from_did == "a"
        assert msg.msg_id == "test123"


# ── Handshake (E2EE) ──

class TestHandshake:
    def test_encrypt_decrypt_roundtrip(self):
        data = "This is a secret message"
        key = b"0123456789abcdef"  # 16 bytes for AES
        encrypted = encrypt_message(data, key)
        assert encrypted != data
        decrypted = decrypt_message(encrypted, key)
        assert decrypted == data

    def test_wrong_key_fails(self):
        data = "Secret"
        key = b"0123456789abcdef"
        wrong_key = b"fedcba9876543210"
        encrypted = encrypt_message(data, key)
        result = decrypt_message(encrypted, wrong_key)
        assert result is None  # Should fail gracefully

    def test_message_flow(self):
        alice, _ = make_identity()
        bob, _ = make_identity()

        # Build a handshake request message
        inv_msg = AgentMessage(
            from_did=bob.did,
            to_did=alice.did,
            intent=INTENT_HANDSHAKE,
            body={
                "action": "request",
                "public_key_hex": bob.to_dict()["public_key_hex"],
            },
        )
        _json = inv_msg.to_json()
        parsed = AgentMessage.from_json(_json)
        assert parsed.from_did == bob.did
        assert parsed.intent == INTENT_HANDSHAKE

        # Build an acceptance message
        accept_msg = AgentMessage(
            from_did=alice.did,
            to_did=bob.did,
            intent=INTENT_HANDSHAKE,
            body={"action": "accept", "level": "partner"},
        )
        assert accept_msg.to_json() is not None

        # Build a rejection message
        reject_msg = AgentMessage(
            from_did=alice.did,
            to_did=bob.did,
            intent=INTENT_HANDSHAKE,
            body={"action": "reject", "reason": "Too busy"},
        )
        assert reject_msg.to_json() is not None

    def test_e2ee_over_protocol(self):
        """E2EE encrypt/decrypt works with actual message bodies."""
        key = b"0123456789abcdef"
        data = json.dumps({"action": "handshake", "from": "alice", "level": 1})
        encrypted = encrypt_message(data, key)
        assert encrypted is not None
        decrypted = decrypt_message(encrypted, key)
        assert json.loads(decrypted)["action"] == "handshake"


# ── Identity + Roster integration ──

class TestIntegration:
    def test_add_contact_then_send(self):
        """Verify identity + roster work together for messaging."""
        alice, tmp1 = make_identity()
        bob, tmp2 = make_identity()

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name

        roster = Roster(path)
        entry = RosterEntry(
            did=bob.did,
            friend_code=bob.friend_code,
            alias="Bob",
            level=PermissionLevel.CONTACT,
            trust_state="active",
        )
        roster.add(entry)

        # Build a message
        msg = AgentMessage(
            from_did=alice.did,
            to_did=bob.did,
            intent=INTENT_CHAT,
            body="Hello Bob!",
        )

        # Roundtrip test
        json_str = msg.to_json()
        assert alice.did in json_str
        assert bob.did in json_str
        assert "Hello Bob!" in json_str

        os.unlink(path)
        shutil.rmtree(tmp1)
        shutil.rmtree(tmp2)

    def test_contact_data_persistence(self):
        """Verify roster data has correct structure."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name

        roster = Roster(path)
        # Add entries with various states
        for i, state in enumerate(["active", "pending", "active", "blocked", "expired"]):
            entry = RosterEntry(
                did=f"did:ww:test{i:04x}",
                friend_code=f"code{i:04x}",
                alias=f"Agent{i}",
                level=PermissionLevel.CONTACT if i % 2 == 0 else PermissionLevel.PARTNER,
                trust_state=state,
            )
            roster.add(entry)

        stats = roster.count()
        assert stats["total"] == 5
        assert stats["active"] == 2  # Two explicit "active" entries
        assert stats["pending"] == 1
        assert stats["blocked"] == 1
        assert stats["expired"] == 1

        # list_active = pending + active (not blocked/expired)
        active = roster.list_active()
        assert len(active) == 3

        os.unlink(path)
