"""ww/core/subconscious — Nostr communication layer test"""

import sys; sys.path.insert(0, ".")
import json
import os
import random

random.seed(42)

print("=" * 50)
print("Nostr Relay communication layer v1 test")
print("=" * 50)

# ══ 1. secp256k1 curve operations ══
print("\n=== 1. secp256k1 curve computation ===")
from core.subconscious.nostr import (
    _point_add, _point_mul, _xonly_point, _lift_x,
    SECP256K1_G, SECP256K1_N,
)

# G + G = 2G
G2 = _point_add(SECP256K1_G, SECP256K1_G)
assert G2 is not None
assert G2 != SECP256K1_G
print("✅ Point addition: G+G works")

# Scalar multiplication consistency
G2_alt = _point_mul(2, SECP256K1_G)
assert G2_alt == G2
print("✅ Point multiplication: 2*G = G+G")

# x-only point
xonly = _xonly_point(SECP256K1_G)
assert len(xonly) == 32
assert int.from_bytes(xonly, "big") == SECP256K1_G[0]
print("✅ x-only point encoding")

# lift_x
P = _lift_x(SECP256K1_G[0])
assert P == SECP256K1_G
print("✅ lift_x: recovers original G")

print("🎯 Curve operations: ALL PASSED")

# ══ 2. BIP-340 Schnorr signature ══
print("\n=== 2. BIP-340 Schnorr ===")
from core.subconscious.nostr import (
    generate_keypair, schnorr_sign, schnorr_verify,
)

# Generate keypair
seckey, pubkey = generate_keypair()
assert len(seckey) == 32
assert len(pubkey) == 32
print(f"✅ Keypair generated: pubkey={pubkey.hex()[:16]}...")

# Sign and verify
msg = b"\x00" * 32  # 32-byte message
sig = schnorr_sign(msg, seckey)
assert len(sig) == 64
print(f"✅ Signature: {sig.hex()[:32]}...")

# Verify with correct key
assert schnorr_verify(msg, pubkey, sig)
print("✅ Signature verification: correct key")

# Verify with wrong key
wrong_seckey, wrong_pubkey = generate_keypair()
assert not schnorr_verify(msg, wrong_pubkey, sig)
print("✅ Signature verification: wrong key rejected")

# Verify with wrong message
wrong_msg = b"\xff" * 32
assert not schnorr_verify(wrong_msg, pubkey, sig)
print("✅ Signature verification: wrong message rejected")

# Verify with tampered signature
tampered_sig = bytearray(sig)
tampered_sig[0] ^= 1  # flip first bit
assert not schnorr_verify(msg, pubkey, bytes(tampered_sig))
print("✅ Signature verification: tampered sig rejected")

# Determinism check: same key, same msg → different sig (random nonce)
sig2 = schnorr_sign(msg, seckey)
assert sig != sig2  # should differ due to random nonce
print("✅ Signature: random nonce (non-deterministic)")

print("🎯 BIP-340 Schnorr: ALL PASSED")

# ══ 3. Nostr Event ══
print("\n=== 3. Nostr Event ===")
from core.subconscious.nostr import NostrEvent

event = NostrEvent(
    pubkey=pubkey,
    kind=39393,
    tags=[["t", "ww-subconscious"], ["v", "7"]],
    content='{"test": true}',
    created_at=1710000000,
)
event.sign(seckey)

assert len(event.id) == 64
assert len(event.sig) == 128  # 64 bytes → 128 hex chars
print(f"✅ Event signed: id={event.id[:16]}..., sig valid")

# Verify event
event_dict = event.to_dict()
assert NostrEvent.verify(event_dict)
print("✅ Event verification: self-consistent")

# Tampered event should fail
tampered = dict(event_dict)
tampered["content"] = '{"test": false}'
assert not NostrEvent.verify(tampered)
print("✅ Event verification: tampered rejected")

# Verify from_dict
event2 = NostrEvent.from_dict(event_dict)
assert event2.id == event.id
assert event2.sig == event.sig
print("✅ Event: from_dict roundtrip")

print("🎯 Nostr Event: ALL PASSED")

# ══ 4. Pack/Unpack Model Update ══
print("\n=== 4. Model Update Pack/Unpack ===")
from core.subconscious.nostr import pack_model_update, unpack_model_update

delta = {
    "node_id": "test_node",
    "model": {"tree": {"is_leaf": True, "value": 0.5}},
}
pow_proof = {"nonce": 42, "hash": "abcd" * 16, "bits": 16}

n_event = pack_model_update(delta, seckey, pubkey, pow_proof=pow_proof)
event_d = n_event.to_dict()

# Verify the event is valid
assert NostrEvent.verify(event_d)
print("✅ Packed event: valid Nostr event")

# Unpack
update = unpack_model_update(event_d)
assert update is not None
assert update["node_id"] == pubkey.hex()[:12]
assert update["delta"] == delta
assert update["pow"] == pow_proof
assert update["event_id"] == event_d["id"]
print("✅ Unpack: all fields correct")

# Invalid kind should be rejected
bad_event = dict(event_d)
bad_event["kind"] = 1
assert unpack_model_update(bad_event) is None
print("✅ Unpack: wrong kind rejected")

# Invalid signature should be rejected
bad_event2 = dict(event_d)
bad_event2["sig"] = "0" * 128
assert unpack_model_update(bad_event2) is None
print("✅ Unpack: bad signature rejected")

print("🎯 Pack/Unpack: ALL PASSED")

# ══ 5. Keypair Persistence ══
print("\n=== 5. Keypair Persistence ===")
import shutil
from core.subconscious.nostr import RelayPool

# Clean
nostr_dir = os.path.expanduser("~/worldwave/data/subconscious/nostr")
if os.path.isdir(nostr_dir):
    shutil.rmtree(nostr_dir)

# Save
pool = RelayPool(seckey=seckey, pubkey=pubkey)
pool.save_keypair()
assert os.path.isfile(os.path.join(nostr_dir, "keypair.json"))
print("✅ Keypair saved")

# Load
loaded = RelayPool.load_keypair()
assert loaded is not None
loaded_sec, loaded_pub = loaded
assert loaded_sec == seckey
assert loaded_pub == pubkey
print("✅ Keypair loaded correctly")

# Auto-generate on no-arg init
pool2 = RelayPool()
assert pool2.seckey is not None
assert pool2.pubkey is not None
assert len(pool2.pubkey) == 32
print("✅ Auto-generated keypair on no-arg init")

print("🎯 Keypair Persistence: ALL PASSED")

# ══ 6. Stats API ══
print("\n=== 6. Stats ===")
stats = pool.get_stats()
assert "relays" in stats
assert "seen_events" in stats
assert "pubkey" in stats
print(f"✅ Stats: {json.dumps(stats, indent=2)}")

print("\n" + "=" * 50)
print("🎉 ALL NOSTR LAYER TESTS PASSED 🎉")
print("=" * 50)
