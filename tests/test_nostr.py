"""ww/core/subconscious — Nostr communication layer test"""

import sys; sys.path.insert(0, ".")
import json
import os
import random

random.seed(42)


# ═══ 1. secp256k1 curve operations ═══
def test_point_addition():
    from p2p.nostr import _point_add, SECP256K1_G
    G2 = _point_add(SECP256K1_G, SECP256K1_G)
    assert G2 is not None
    assert G2 != SECP256K1_G


def test_scalar_multiplication():
    from p2p.nostr import _point_add, _point_mul, SECP256K1_G
    G2 = _point_add(SECP256K1_G, SECP256K1_G)
    G2_alt = _point_mul(2, SECP256K1_G)
    assert G2_alt == G2


def test_xonly_point():
    from p2p.nostr import _xonly_point, SECP256K1_G
    xonly = _xonly_point(SECP256K1_G)
    assert len(xonly) == 32
    assert int.from_bytes(xonly, "big") == SECP256K1_G[0]


def test_lift_x():
    from p2p.nostr import _lift_x, SECP256K1_G
    P = _lift_x(SECP256K1_G[0])
    assert P == SECP256K1_G


# ═══ 2. BIP-340 Schnorr signature ═══
def test_keypair_generation():
    from p2p.nostr import generate_keypair
    seckey, pubkey = generate_keypair()
    assert len(seckey) == 32
    assert len(pubkey) == 32


def test_schnorr_sign_and_verify():
    from p2p.nostr import generate_keypair, schnorr_sign, schnorr_verify
    seckey, pubkey = generate_keypair()
    msg = b"\x00" * 32
    sig = schnorr_sign(msg, seckey)
    assert len(sig) == 64
    assert schnorr_verify(msg, pubkey, sig)


def test_schnorr_rejects_wrong_key():
    from p2p.nostr import generate_keypair, schnorr_sign, schnorr_verify
    seckey, pubkey = generate_keypair()
    _, wrong_pubkey = generate_keypair()
    msg = b"\x00" * 32
    sig = schnorr_sign(msg, seckey)
    assert not schnorr_verify(msg, wrong_pubkey, sig)


def test_schnorr_rejects_wrong_msg():
    from p2p.nostr import generate_keypair, schnorr_sign, schnorr_verify
    seckey, pubkey = generate_keypair()
    msg = b"\x00" * 32
    sig = schnorr_sign(msg, seckey)
    wrong_msg = b"\xff" * 32
    assert not schnorr_verify(wrong_msg, pubkey, sig)


def test_schnorr_rejects_tampered_sig():
    from p2p.nostr import generate_keypair, schnorr_sign, schnorr_verify
    seckey, pubkey = generate_keypair()
    msg = b"\x00" * 32
    sig = schnorr_sign(msg, seckey)
    tampered_sig = bytearray(sig)
    tampered_sig[0] ^= 1
    assert not schnorr_verify(msg, pubkey, bytes(tampered_sig))


def test_schnorr_random_nonce():
    from p2p.nostr import generate_keypair, schnorr_sign
    seckey, _ = generate_keypair()
    msg = b"\x00" * 32
    sig1 = schnorr_sign(msg, seckey)
    sig2 = schnorr_sign(msg, seckey)
    assert sig1 != sig2  # random nonce


# ═══ 3. Nostr Event ═══
def test_nostr_event_sign():
    from p2p.nostr import generate_keypair, NostrEvent
    seckey, pubkey = generate_keypair()
    event = NostrEvent(
        pubkey=pubkey,
        kind=39393,
        tags=[["t", "ww-subconscious"], ["v", "7"]],
        content='{"test": true}',
        created_at=1710000000,
    )
    event.sign(seckey)
    assert len(event.id) == 64
    assert len(event.sig) == 128


def test_nostr_event_verify():
    from p2p.nostr import generate_keypair, NostrEvent
    seckey, pubkey = generate_keypair()
    event = NostrEvent(
        pubkey=pubkey,
        kind=39393,
        tags=[["t", "ww-subconscious"]],
        content='{"test": true}',
        created_at=1710000000,
    )
    event.sign(seckey)
    event_dict = event.to_dict()
    assert NostrEvent.verify(event_dict)


def test_nostr_event_rejects_tampered():
    from p2p.nostr import generate_keypair, NostrEvent
    seckey, pubkey = generate_keypair()
    event = NostrEvent(pubkey=pubkey, kind=39393, tags=[], content="hello", created_at=1710000000)
    event.sign(seckey)
    event_dict = event.to_dict()
    tampered = dict(event_dict)
    tampered["content"] = "world"
    assert not NostrEvent.verify(tampered)


def test_nostr_event_from_dict():
    from p2p.nostr import generate_keypair, NostrEvent
    seckey, pubkey = generate_keypair()
    event = NostrEvent(pubkey=pubkey, kind=39393, tags=[], content="hello", created_at=1710000000)
    event.sign(seckey)
    event2 = NostrEvent.from_dict(event.to_dict())
    assert event2.id == event.id
    assert event2.sig == event.sig


# ═══ 4. Pack/Unpack Model Update ═══
def test_pack_unpack_model_update():
    from p2p.nostr import generate_keypair, pack_model_update, unpack_model_update, NostrEvent
    seckey, pubkey = generate_keypair()
    delta = {
        "node_id": "test_node",
        "model": {"tree": {"is_leaf": True, "value": 0.5}},
    }
    pow_proof = {"nonce": 42, "hash": "abcd" * 16, "bits": 16}
    n_event = pack_model_update(delta, seckey, pubkey, pow_proof=pow_proof)
    event_d = n_event.to_dict()
    assert NostrEvent.verify(event_d)
    update = unpack_model_update(event_d)
    assert update is not None
    assert update["node_id"] == pubkey.hex()[:12]
    assert update["delta"] == delta
    assert update["pow"] == pow_proof


def test_unpack_rejects_wrong_kind():
    from p2p.nostr import generate_keypair, pack_model_update, unpack_model_update
    seckey, pubkey = generate_keypair()
    delta = {"node_id": "test", "model": {"value": 0.5}}
    n_event = pack_model_update(delta, seckey, pubkey)
    event_d = n_event.to_dict()
    bad_event = dict(event_d)
    bad_event["kind"] = 1
    assert unpack_model_update(bad_event) is None


def test_unpack_rejects_bad_signature():
    from p2p.nostr import generate_keypair, pack_model_update, unpack_model_update
    seckey, pubkey = generate_keypair()
    delta = {"node_id": "test", "model": {"value": 0.5}}
    n_event = pack_model_update(delta, seckey, pubkey)
    event_d = n_event.to_dict()
    bad_event2 = dict(event_d)
    bad_event2["sig"] = "0" * 128
    assert unpack_model_update(bad_event2) is None


# ═══ 5. Keypair Persistence ═══
def test_keypair_save_and_load():
    import shutil
    from p2p.nostr import generate_keypair, RelayPool
    seckey, pubkey = generate_keypair()
    nostr_dir = os.path.expanduser("~/worldwave/data/subconscious/nostr")
    if os.path.isdir(nostr_dir):
        shutil.rmtree(nostr_dir)
    pool = RelayPool(seckey=seckey, pubkey=pubkey)
    pool.save_keypair()
    assert os.path.isfile(os.path.join(nostr_dir, "keypair.json"))
    loaded = RelayPool.load_keypair()
    assert loaded is not None
    loaded_sec, loaded_pub = loaded
    assert loaded_sec == seckey
    assert loaded_pub == pubkey


def test_relaypool_auto_generates_keypair():
    from p2p.nostr import RelayPool
    pool = RelayPool()
    assert pool.seckey is not None
    assert pool.pubkey is not None
    assert len(pool.pubkey) == 32


# ═══ 6. Stats API ═══
def test_relaypool_stats():
    from p2p.nostr import RelayPool
    pool = RelayPool()
    stats = pool.get_stats()
    assert "relays" in stats
    assert "seen_events" in stats
    assert "pubkey" in stats
