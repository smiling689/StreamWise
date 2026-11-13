"""End-to-end encryption of the StreamWise protocol (extension E-02).

Handshake (inside the plaintext ``HELLO``/``WELCOME`` exchange)::

    client -> server  HELLO   {"client": ..., "encryption": {"kex": "X25519",
                                "cipher": "AES-256-GCM", "public_key": Pc, "nonce": Nc}}
    server -> client  WELCOME {..., "encryption": {"kex": "X25519",
                                "cipher": "AES-256-GCM", "public_key": Ps, "nonce": Ns,
                                "finished": F}}
    every later message: AES-256-GCM, type byte | 0x80

Key schedule (HKDF-SHA256, RFC 5869)::

    shared   = X25519(own private key, peer public key)
    salt     = SHA-256(HELLO payload || Ps || Ns)       # binds the whole client offer
    keys     = HKDF(ikm = shared || PSK, salt, info = "StreamWise v1 keys", 96 bytes)
    k_c2s, k_s2c, k_confirm = keys[0:32], keys[32:64], keys[64:96]
    F        = HMAC-SHA256(k_confirm, SHA-256(HELLO payload) || canonical WELCOME without F)

Both sides generate fresh ephemeral key pairs and 16-byte nonces per connection, so every
connection has new keys (forward secrecy). The optional pre-shared key (PSK) authenticates
the peers: without it an active man-in-the-middle could run a separate handshake with each
side; with it, a party without the PSK derives different keys and ``F`` does not verify.

Record protection: the payload of every message after ``WELCOME`` is
``AES-256-GCM(key, nonce, plaintext, aad)`` = ciphertext || 16-byte tag. The 96-bit nonce is a
4-byte direction label followed by a 64-bit message counter that both sides keep implicitly
(TCP delivers in order), so a replayed, reordered, or dropped message fails authentication.
The AAD is the first 12 header bytes (magic, version, type, request id, payload length), so
the header cannot be altered either. The CRC-32 in the header is computed over the
ciphertext and still detects transport corruption before decryption.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from streamwise.common.protocol import ENCRYPTED_FLAG, MAGIC, VERSION

KEX = "X25519"
CIPHER = "AES-256-GCM"
TAG_SIZE = 16
NONCE_SIZE = 16
"""Size of the handshake nonces (not the AES-GCM nonce)."""
KEY_INFO = b"StreamWise v1 keys"
AAD = struct.Struct("!2sBBII")
"""The header without the CRC-32: magic, version, type, request id, payload length."""
MIN_PSK_BYTES = 16

_C2S = b"c2s\x00"
_S2C = b"s2c\x00"
_MAX_COUNTER = 2**64 - 1


class HandshakeError(Exception):
    """The peer's handshake is malformed or key confirmation failed."""


class DecryptionError(Exception):
    """A record failed authentication (tampered, replayed, reordered, or wrong key)."""


def load_psk(path: Path) -> bytes:
    """Read a pre-shared key file. Surrounding whitespace is ignored."""
    psk = path.read_bytes().strip()
    if len(psk) < MIN_PSK_BYTES:
        raise ValueError(
            f"{path}: pre-shared key must have at least {MIN_PSK_BYTES} bytes "
            "(e.g. `openssl rand -hex 32`)"
        )
    return psk


def canonical_json(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: Any, size: int, what: str) -> bytes:
    if not isinstance(value, str):
        raise HandshakeError(f"{what} missing")
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise HandshakeError(f"{what} is not valid base64") from exc
    if len(data) != size:
        raise HandshakeError(f"{what} must be {size} bytes, got {len(data)}")
    return data


def _public_bytes(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _derive(
    private: X25519PrivateKey,
    peer_public: bytes,
    hello_payload: bytes,
    server_public: bytes,
    server_nonce: bytes,
    psk: bytes | None,
) -> tuple[bytes, bytes, bytes]:
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer_public))
    except ValueError as exc:  # e.g. a low-order point gives an all-zero secret
        raise HandshakeError(f"key exchange failed: {exc}") from exc
    salt = hashlib.sha256(hello_payload + server_public + server_nonce).digest()
    okm = HKDF(algorithm=hashes.SHA256(), length=96, salt=salt, info=KEY_INFO).derive(
        shared + (psk or b"")
    )
    return okm[:32], okm[32:64], okm[64:]


def _finished(confirm_key: bytes, hello_payload: bytes, welcome: dict[str, Any]) -> bytes:
    transcript = hashlib.sha256(hello_payload).digest() + canonical_json(welcome)
    return hmac.new(confirm_key, transcript, hashlib.sha256).digest()


class SecureChannel:
    """Per-connection record protection with implicit per-direction counters."""

    def __init__(self, send_key: bytes, recv_key: bytes, send_label: bytes, recv_label: bytes):
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_label = send_label
        self._recv_label = recv_label
        self._send_counter = 0
        self._recv_counter = 0

    @property
    def messages_sent(self) -> int:
        return self._send_counter

    @property
    def messages_received(self) -> int:
        return self._recv_counter

    @staticmethod
    def aad(type_byte: int, request_id: int, payload_length: int) -> bytes:
        return AAD.pack(MAGIC, VERSION, type_byte, request_id, payload_length)

    def seal(self, msg_type: int, request_id: int, plaintext: bytes) -> bytes:
        """Encrypt one message payload; returns ciphertext || tag."""
        if self._send_counter >= _MAX_COUNTER:
            raise OverflowError("message counter exhausted; reconnect")
        nonce = self._send_label + self._send_counter.to_bytes(8, "big")
        self._send_counter += 1
        aad = self.aad(msg_type | ENCRYPTED_FLAG, request_id, len(plaintext) + TAG_SIZE)
        return self._send.encrypt(nonce, plaintext, aad)

    def open(self, msg_type: int, request_id: int, payload: bytes) -> bytes:
        """Decrypt and authenticate one message payload (ciphertext || tag)."""
        if len(payload) < TAG_SIZE:
            raise DecryptionError("encrypted payload shorter than the authentication tag")
        nonce = self._recv_label + self._recv_counter.to_bytes(8, "big")
        aad = self.aad(msg_type | ENCRYPTED_FLAG, request_id, len(payload))
        try:
            plaintext = self._recv.decrypt(nonce, payload, aad)
        except InvalidTag as exc:
            raise DecryptionError(
                "authentication failed (tampered, replayed or reordered message, or wrong key)"
            ) from exc
        self._recv_counter += 1
        return plaintext


class ClientHandshake:
    """Client side: create the offer for ``HELLO``, then verify the ``WELCOME`` answer."""

    def __init__(self, psk: bytes | None = None) -> None:
        self._private = X25519PrivateKey.generate()
        self._nonce = os.urandom(NONCE_SIZE)
        self._psk = psk

    def offer(self) -> dict[str, str]:
        return {
            "kex": KEX,
            "cipher": CIPHER,
            "public_key": _b64(_public_bytes(self._private)),
            "nonce": _b64(self._nonce),
        }

    def finish(self, hello_payload: bytes, welcome: dict[str, Any]) -> SecureChannel:
        """Derive the keys from the server's answer and check its key confirmation.

        ``hello_payload`` must be the exact bytes sent in ``HELLO``.
        """
        answer = welcome.get("encryption")
        if not isinstance(answer, dict):
            raise HandshakeError("server did not accept encryption")
        if answer.get("kex") != KEX or answer.get("cipher") != CIPHER:
            raise HandshakeError(
                f"server chose unsupported {answer.get('kex')}/{answer.get('cipher')}"
            )
        server_public = _unb64(answer.get("public_key"), 32, "server public key")
        server_nonce = _unb64(answer.get("nonce"), NONCE_SIZE, "server nonce")
        finished = _unb64(answer.get("finished"), 32, "server key confirmation")
        k_c2s, k_s2c, k_confirm = _derive(
            self._private, server_public, hello_payload, server_public, server_nonce, self._psk
        )
        unsigned = dict(welcome)
        unsigned["encryption"] = {k: v for k, v in answer.items() if k != "finished"}
        if not hmac.compare_digest(finished, _finished(k_confirm, hello_payload, unsigned)):
            raise HandshakeError(
                "key confirmation failed: pre-shared keys differ or the handshake was tampered with"
            )
        return SecureChannel(k_c2s, k_s2c, _C2S, _S2C)


def server_accept(
    hello_payload: bytes, offer: Any, welcome: dict[str, Any], psk: bytes | None = None
) -> tuple[dict[str, Any], SecureChannel]:
    """Server side: answer a client offer.

    ``welcome`` holds the other ``WELCOME`` fields; returns the complete ``WELCOME`` object
    (with ``encryption`` including the key confirmation) and the channel.
    """
    if not isinstance(offer, dict):
        raise HandshakeError("encryption offer must be an object")
    if offer.get("kex") != KEX or offer.get("cipher") != CIPHER:
        raise HandshakeError(
            f"unsupported key exchange or cipher {offer.get('kex')}/{offer.get('cipher')}; "
            f"this server supports {KEX}/{CIPHER}"
        )
    client_public = _unb64(offer.get("public_key"), 32, "client public key")
    _unb64(offer.get("nonce"), NONCE_SIZE, "client nonce")  # bound through hello_payload
    private = X25519PrivateKey.generate()
    server_public = _public_bytes(private)
    server_nonce = os.urandom(NONCE_SIZE)
    k_c2s, k_s2c, k_confirm = _derive(
        private, client_public, hello_payload, server_public, server_nonce, psk
    )
    answer = {
        "kex": KEX,
        "cipher": CIPHER,
        "public_key": _b64(server_public),
        "nonce": _b64(server_nonce),
    }
    finished = _finished(k_confirm, hello_payload, {**welcome, "encryption": answer})
    full = {**welcome, "encryption": {**answer, "finished": _b64(finished)}}
    return full, SecureChannel(k_s2c, k_c2s, _S2C, _C2S)
