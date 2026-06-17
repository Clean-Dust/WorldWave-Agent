"""
ww/core/subconscious/nostr.py — Nostr Relay Communication Layer v1

Zero external dependencies, pure Python Nostr client.

contains ：
  1. secp256k1 curve operations + BIP-340 Schnorr signature (pure Python)
  2. Nostr Event (NIP-01) creation and signing
  3. Relay WebSocket client（use stdlib asyncio + websockets）
  4. Relay Pool (multiple relay links, auto-reconnect, deduplication)

WW subconscious uses kind=39393 to propagate model deltas.
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import asyncio
import time
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("ww.subconscious.nostr")

NOSTR_DIR = os.path.expanduser("~/worldwave/data/subconscious/nostr")

# WW custom event kind
KIND_WW_MODEL_UPDATE = 39393

# Default public Nostr Relays
DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.nostr.band",
    "wss://relay.snort.social",
]

# ────────────────────────────────────────────────────────────
# secp256k1 curve constants (BIP-340 / BIP-341)
# ────────────────────────────────────────────────────────────

SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
SECP256K1_G = (SECP256K1_GX, SECP256K1_GY)


# ────────────────────────────────────────────────────────────
#  Elliptic curve operations (pure Python)
# ────────────────────────────────────────────────────────────


def _modinv(a: int, m: int) -> int:
    """Modular inverse (extended Euclidean)."""
    if a < 0:
        a = a % m
    g, x, _ = _extended_gcd(a, m)
    if g != 1:
        raise ValueError("modular inverse does not exist")
    return x % m


def _extended_gcd(a: int, b: int) -> Tuple[int, int, int]:
    if a == 0:
        return b, 0, 1
    g, x1, y1 = _extended_gcd(b % a, a)
    return g, y1 - (b // a) * x1, x1


def _point_add(P1: Optional[Tuple[int, int]],
               P2: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """Elliptic curve point addition (affine coordinates)."""
    if P1 is None:
        return P2
    if P2 is None:
        return P1

    x1, y1 = P1
    x2, y2 = P2
    p = SECP256K1_P

    if x1 == x2:
        if y1 == y2:
            # Point doubling
            if y1 == 0:
                return None
            lam = (3 * x1 * x1) * _modinv(2 * y1, p) % p
        else:
            # Opposite point
            return None
    else:
        lam = (y2 - y1) * _modinv(x2 - x1, p) % p

    x3 = (lam * lam - x1 - x2) % p
    y3 = (lam * (x1 - x3) - y1) % p
    return (x3, y3)


def _point_mul(k: int, P: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """Scalar multiplication (double-and-add)."""
    if P is None or k == 0:
        return None
    result = None
    addend = P
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _xonly_point(P: Optional[Tuple[int, int]]) -> bytes:
    """Return point x coordinate (32 bytes, big-endian)."""
    if P is None:
        return b"\x00" * 32
    return P[0].to_bytes(32, "big")


def _lift_x(x: int) -> Optional[Tuple[int, int]]:
    """BIP-340 lift_x: given x coordinate, find corresponding y coordinate (even y)."""
    p = SECP256K1_P
    y_sq = (pow(x, 3, p) + 7) % p
    y = pow(y_sq, (p + 1) // 4, p)
    if (y * y - y_sq) % p != 0:
        return None
    if y % 2 == 1:
        y = p - y
    return (x, y)


def _tagged_hash(tag: str, data: bytes) -> bytes:
    """BIP-340 taghash：SHA256(SHA256(tag) || SHA256(tag) || data)。"""
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + data).digest()


# ────────────────────────────────────────────────────────────
#  keygenerate
# ────────────────────────────────────────────────────────────


def generate_keypair() -> Tuple[bytes, bytes]:
    """
    Generate secp256k1 key pair.

    Returns:
        (private_key_32bytes, public_key_32bytes_xonly)
    """
    # Secure random private key
    while True:
        priv = os.urandom(32)
        d = int.from_bytes(priv, "big")
        if 1 <= d < SECP256K1_N:
            break

    # public key = d * G（x-only）
    P = _point_mul(d, SECP256K1_G)
    pub = _xonly_point(P)

    return priv, pub


def pubkey_to_hex(pubkey: bytes) -> str:
    """Public key to hex."""
    return pubkey.hex()


def privkey_to_hex(privkey: bytes) -> str:
    """Private key to hex."""
    return privkey.hex()


def hex_to_pubkey(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def hex_to_privkey(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


# ────────────────────────────────────────────────────────────
#  BIP-340 Schnorr signature
# ────────────────────────────────────────────────────────────


def schnorr_sign(msg: bytes, seckey: bytes) -> bytes:
    """
    BIP-340 Schnorr signature。

    Args:
        msg: 32 bytes (event ID)
        seckey: 32 bytes（private key）

    Returns:
        64 bytes signature (r || s)
    """
    assert len(msg) == 32, "Message must be 32 bytes"
    assert len(seckey) == 32, "Secret key must be 32 bytes"

    d = int.from_bytes(seckey, "big")
    if d == 0 or d >= SECP256K1_N:
        raise ValueError("Invalid private key")

    # public key P = d * G
    P = _point_mul(d, SECP256K1_G)
    if P is None:
        raise ValueError("Invalid public key")
    Px = _xonly_point(P)

    # BIP-340: if y(P) is odd, negate d (ensure lift_x can recover)
    if P[1] % 2 == 1:
        d = SECP256K1_N - d
        P = _point_mul(d, SECP256K1_G)
        Px = _xonly_point(P)

    # Generate random nonce k
    while True:
        k_bytes = os.urandom(32)
        k = int.from_bytes(k_bytes, "big") % SECP256K1_N
        if k > 0:
            break

    # R = k * G
    R = _point_mul(k, SECP256K1_G)
    if R is None:
        raise ValueError("Invalid nonce")

    # BIP-340: if y(R) is odd, negate k
    if R[1] % 2 == 1:
        k = SECP256K1_N - k
        R = _point_mul(k, SECP256K1_G)

    Rx = _xonly_point(R)

    # e = tagged_hash("BIP0340/challenge", Rx || Px || msg)
    e_bytes = _tagged_hash("BIP0340/challenge", Rx + Px + msg)
    e = int.from_bytes(e_bytes, "big") % SECP256K1_N

    # s = k + e * d mod n
    s = (k + e * d) % SECP256K1_N

    # signature = 32 bytes R.x || 32 bytes s
    return Rx + s.to_bytes(32, "big")


def schnorr_verify(msg: bytes, pubkey: bytes, sig: bytes) -> bool:
    """
    BIP-340 Schnorr signaturevalidate。

    Args:
        msg: 32 bytes
        pubkey: 32 bytes（x-only public key）
        sig: 64 bytes

    Returns:
        bool
    """
    if len(msg) != 32:
        return False
    if len(pubkey) != 32:
        return False
    if len(sig) != 64:
        return False

    P = _lift_x(int.from_bytes(pubkey, "big"))
    if P is None:
        return False

    r_bytes = sig[:32]
    s_bytes = sig[32:]
    r = int.from_bytes(r_bytes, "big")
    s = int.from_bytes(s_bytes, "big")

    if r >= SECP256K1_P or s >= SECP256K1_N:
        return False

    # e = tagged_hash("BIP0340/challenge", r_bytes || pubkey || msg)
    e_bytes = _tagged_hash("BIP0340/challenge", r_bytes + pubkey + msg)
    e = int.from_bytes(e_bytes, "big") % SECP256K1_N

    # R' = s*G - e*P
    R1 = _point_mul(s, SECP256K1_G)
    R2 = _point_mul(e, P)
    if R2 is not None:
        R_check = _point_add(R1, (R2[0], (-R2[1]) % SECP256K1_P))
    else:
        R_check = R1

    if R_check is None:
        return False

    # validate R'.x == r
    return _xonly_point(R_check) == r_bytes


# ────────────────────────────────────────────────────────────
#  Nostr Event (NIP-01)
# ────────────────────────────────────────────────────────────


class NostrEvent:
    """
    Nostr event (compliant with NIP-01).

    Creation process:
      1. Fill fields (pubkey, created_at, kind, tags, content)
      2. Compute id = SHA256(serialization)
      3. Sign id → sig
      4. to_dict() outputs complete event
    """

    def __init__(
        self,
        pubkey: bytes,
        kind: int,
        tags: List[List[str]],
        content: str,
        created_at: Optional[int] = None,
    ):
        self.pubkey = pubkey.hex()
        self.created_at = created_at or int(time.time())
        self.kind = kind
        self.tags = tags
        self.content = content
        self.id: str = ""
        self.sig: str = ""

    def _serialize(self) -> bytes:
        """NIP-01 serialize：[0, pubkey, created_at, kind, tags, content]"""
        return json.dumps(
            [
                0,
                self.pubkey,
                self.created_at,
                self.kind,
                self.tags,
                self.content,
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()

    def compute_id(self) -> str:
        """Compute event ID = SHA256(serialize)."""
        return hashlib.sha256(self._serialize()).hexdigest()

    def sign(self, seckey: bytes):
        """
        Sign event.

        1. Compute id
        2. BIP-340 Schnorr signature
        3. Fill in sig
        """
        self.id = self.compute_id()
        msg = bytes.fromhex(self.id)
        sig_bytes = schnorr_sign(msg, seckey)
        self.sig = sig_bytes.hex()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "kind": self.kind,
            "tags": self.tags,
            "content": self.content,
            "sig": self.sig,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def verify(cls, event_dict: dict) -> bool:
        """Validate an external event authenticity."""
        try:
            # Rebuild event
            event = cls(
                pubkey=bytes.fromhex(event_dict["pubkey"]),
                kind=event_dict["kind"],
                tags=event_dict.get("tags", []),
                content=event_dict.get("content", ""),
                created_at=event_dict["created_at"],
            )
            # validate id
            computed_id = event.compute_id()
            if computed_id != event_dict.get("id", ""):
                return False

            # validatesignature
            msg = bytes.fromhex(computed_id)
            pubkey_bytes = bytes.fromhex(event_dict["pubkey"])
            sig_bytes = bytes.fromhex(event_dict.get("sig", ""))
            return schnorr_verify(msg, pubkey_bytes, sig_bytes)
        except Exception:
            return False

    @classmethod
    def from_dict(cls, d: dict) -> "NostrEvent":
        e = cls(
            pubkey=bytes.fromhex(d.get("pubkey", "0" * 64)),
            kind=d.get("kind", 0),
            tags=d.get("tags", []),
            content=d.get("content", ""),
            created_at=d.get("created_at"),
        )
        e.id = d.get("id", "")
        e.sig = d.get("sig", "")
        return e


# ────────────────────────────────────────────────────────────
#  Wrap WW model update as Nostr Event
# ────────────────────────────────────────────────────────────


def pack_model_update(
    model_delta: dict,
    seckey: bytes,
    pubkey: bytes,
    pow_proof: Optional[dict] = None,
) -> NostrEvent:
    """
    Will pack model delta as Nostr event (kind=39393).

    Args:
        model_delta: Model update payload
        seckey: 32 bytes private key
        pubkey: 32 bytes public key
        pow_proof: {"nonce": int, "hash": str, "bits": int}（optional）

    Returns:
        NostrEvent (signed)
    """
    content = json.dumps({
        "delta": model_delta,
        "pow": pow_proof,
        "version": "subconscious-v7",
    }, ensure_ascii=False)

    tags = [
        ["t", "ww-subconscious"],
        ["v", "7"],
    ]

    event = NostrEvent(
        pubkey=pubkey,
        kind=KIND_WW_MODEL_UPDATE,
        tags=tags,
        content=content,
    )
    event.sign(seckey)
    return event


def unpack_model_update(event_dict: dict) -> Optional[dict]:
    """
    Unpack model update from Nostr event.

    Returns:
        {
            "node_id": str,
            "pubkey": str,
            "created_at": int,
            "delta": dict,
            "pow": dict or None,
            "event_id": str,
        } or None
    """
    if not NostrEvent.verify(event_dict):
        logger.warning("Nostr event verification failed")
        return None

    if event_dict.get("kind") != KIND_WW_MODEL_UPDATE:
        return None

    try:
        content = json.loads(event_dict.get("content", "{}"))
    except (json.JSONDecodeError, TypeError):
        return None

    return {
        "node_id": event_dict["pubkey"][:12],
        "pubkey": event_dict["pubkey"],
        "created_at": event_dict.get("created_at", 0),
        "delta": content.get("delta", {}),
        "pow": content.get("pow"),
        "event_id": event_dict.get("id", ""),
    }


# ────────────────────────────────────────────────────────────
#  Relay WebSocket client
# ────────────────────────────────────────────────────────────


class NostrRelayClient:
    """
    Single Nostr relay WebSocket client.

    supports：
    - Publish event (EVENT)
    - Subscribe to events (REQ)
    - Auto reconnect (exponential backoff)
    """

    def __init__(
        self,
        relay_url: str,
        on_event: Optional[Callable] = None,
        reconnect: bool = True,
    ):
        self.relay_url = relay_url
        self.on_event = on_event
        self.reconnect = reconnect

        self.running = False
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._subscription_id: str = ""

    def connect(self, subscription_id: str = "ww-sub"):
        """Connect to relay and start subscription."""
        self._subscription_id = subscription_id
        self.running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"nostr-{self.relay_url[:20]}",
        )
        self._thread.start()

    def disconnect(self):
        self.running = False
        if self._ws:
            try:
                import asyncio
                asyncio.run(self._ws.close())
            except Exception:
                pass

    def publish(self, event: NostrEvent) -> bool:
        """Publish an event to relay (sync wrapper)."""
        try:
            result = asyncio.run(self._async_publish(event))
            return result
        except Exception as e:
            logger.debug(f"Publish to {self.relay_url} failed: {e}")
            return False

    async def _async_publish(self, event: NostrEvent) -> bool:
        import websockets
        try:
            async with websockets.connect(
                self.relay_url,
                open_timeout=5,
                ping_interval=30,
            ) as ws:
                msg = json.dumps(["EVENT", event.to_dict()])
                await ws.send(msg)
                resp = await asyncio.wait_for(ws.recv(), timeout=5)
                resp_data = json.loads(resp if isinstance(resp, str) else resp.decode())
                if isinstance(resp_data, list) and len(resp_data) >= 3:
                    ok = resp_data[2]
                    if not ok:
                        reason = resp_data[3] if len(resp_data) > 3 else "unknown"
                        logger.warning(f"Relay {self.relay_url} rejected: {reason}")
                    return ok
                return True
        except Exception as e:
            logger.debug(f"Publish WS error: {e}")
            return False

    def _run_loop(self):
        """Background thread: maintain WebSocket connection + receive events."""

        backoff = 1

        while self.running:
            try:
                asyncio.run(self._listen_loop())
            except Exception as e:
                logger.debug(f"Relay {self.relay_url} disconnected: {e}")

            if not self.reconnect or not self.running:
                break

            # Exponential backoff reconnection
            time.sleep(min(backoff, 60))
            backoff = min(backoff * 2, 60)

    async def _listen_loop(self):
        """WebSocket listening loop."""
        import websockets

        async with websockets.connect(
            self.relay_url,
            open_timeout=10,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info(f"🔗 Connected to relay: {self.relay_url}")

            # Subscribe to WW model updates
            sub = json.dumps([
                "REQ",
                self._subscription_id,
                {"kinds": [KIND_WW_MODEL_UPDATE], "limit": 0},
            ])
            await ws.send(sub)

            # Continuously receive
            while self.running:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    text = msg if isinstance(msg, str) else msg.decode()
                    self._handle_message(text)
                except asyncio.TimeoutError:
                    continue

    def _handle_message(self, raw: str):
        """Process messages received from relay."""
        try:
            data = json.loads(raw)
            if not isinstance(data, list) or len(data) < 2:
                return

            msg_type = data[0]

            if msg_type == "EVENT":
                # subscription_id = data[1], event = data[2]
                if len(data) >= 3:
                    event_dict = data[2]
                    if self.on_event:
                        self.on_event(event_dict, self.relay_url)

            elif msg_type == "EOSE":
                # End of stored events
                pass

        except (json.JSONDecodeError, IndexError):
            pass


# ────────────────────────────────────────────────────────────
#  Relay Pool (multi-relay management)
# ────────────────────────────────────────────────────────────


class RelayPool:
    """
    Multi-relay connection management.

    Responsible for:
    - Connect to N relays
    - Receive and deduplicate events
    - Publish to all relays
    - Auto reconnect
    - Health check
    """

    def __init__(
        self,
        seckey: Optional[bytes] = None,
        pubkey: Optional[bytes] = None,
        relay_urls: Optional[List[str]] = None,
        on_model_update: Optional[Callable] = None,
    ):
        self.seckey = seckey
        self.pubkey = pubkey

        # if no key，autogenerate
        if self.seckey is None or self.pubkey is None:
            self.seckey, self.pubkey = generate_keypair()

        self.relay_urls = relay_urls or DEFAULT_RELAYS
        self.on_model_update = on_model_update

        self._clients: List[NostrRelayClient] = []
        self._seen_events: Set[str] = set()  # Deduplication
        self.running = False

    def start(self, subscription_id: str = "ww-sub"):
        """Connect to all relays."""
        self.running = True

        for url in self.relay_urls:
            client = NostrRelayClient(
                relay_url=url,
                on_event=self._handle_event,
                reconnect=True,
            )
            client.connect(subscription_id=subscription_id)
            self._clients.append(client)

        logger.info(
            f"🌐 Nostr pool started: {len(self._clients)} relays, "
            f"pubkey={self.pubkey.hex()[:16]}..."
        )

    def stop(self):
        self.running = False
        for client in self._clients:
            client.disconnect()
        self._clients.clear()
        logger.info("🌐 Nostr pool stopped")

    def publish_model_update(self, model_delta: dict,
                             pow_proof: Optional[dict] = None) -> int:
        """
        Publish model update to all relays.

        Args:
            model_delta: Model update payload
            pow_proof: PoW proof（optional）

        Returns:
            Successfully published relay count
        """
        if not self.seckey or not self.pubkey:
            logger.error("No keypair configured for signing")
            return 0

        event = pack_model_update(
            model_delta=model_delta,
            seckey=self.seckey,
            pubkey=self.pubkey,
            pow_proof=pow_proof,
        )

        success = 0
        for client in self._clients:
            if client.publish(event):
                success += 1

        if success > 0:
            logger.info(
                f"📡 Published model update to {success}/{len(self._clients)} relays, "
                f"event_id={event.id[:16]}"
            )
        return success

    def _handle_event(self, event_dict: dict, relay_url: str):
        """Received event callback."""
        event_id = event_dict.get("id", "")

        # Deduplicate
        if event_id in self._seen_events:
            return
        self._seen_events.add(event_id)

        # Do not process own events
        if event_dict.get("pubkey") == self.pubkey.hex():
            return

        # Only process WW model updates
        if event_dict.get("kind") != KIND_WW_MODEL_UPDATE:
            return

        # Unpack
        update = unpack_model_update(event_dict)
        if update is None:
            return

        logger.info(
            f"📩 Received model update from {update['node_id']} "
            f"via {relay_url}"
        )

        # callback
        if self.on_model_update:
            self.on_model_update(update, relay_url)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "relays": len(self._clients),
            "connected": sum(
                1 for c in self._clients if c._ws is not None
            ),
            "pubkey": (self.pubkey.hex()[:16] + "...") if self.pubkey else "none",
            "seen_events": len(self._seen_events),
            "running": self.running,
        }

    def save_keypair(self):
        """Save key pair to local file."""
        os.makedirs(NOSTR_DIR, exist_ok=True)
        path = os.path.join(NOSTR_DIR, "keypair.json")
        data = {
            "private_key": self.seckey.hex(),
            "public_key": self.pubkey.hex(),
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(path, 0o600)  # only owner can read

    @classmethod
    def load_keypair(cls) -> Optional[Tuple[bytes, bytes]]:
        """Load key pair from local file."""
        path = os.path.join(NOSTR_DIR, "keypair.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                seckey = bytes.fromhex(data["private_key"])
                pubkey = bytes.fromhex(data["public_key"])
                return seckey, pubkey
            except Exception:
                pass
        return None
