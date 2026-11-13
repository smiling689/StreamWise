"""Tests for the AES-256-GCM encryption extension (E-02)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from streamwise.abr import create_abr
from streamwise.client.connection import ServerError, StreamClient
from streamwise.client.session import ClientConfig, StreamingSession
from streamwise.common import crypto
from streamwise.common import protocol as proto
from streamwise.common.protocol import ErrorCode, Message, MessageType, ProtocolError
from streamwise.server.app import ServerConfig, StreamServer
from streamwise.server.catalog import MediaCatalog
from streamwise.server.txlog import TransmissionLog

PSK = b"0123456789abcdef0123456789abcdef"


def handshake(psk_client=None, psk_server=None, welcome=None):
    client = crypto.ClientHandshake(psk_client)
    hello = json.dumps({"client": "c", "encryption": client.offer()}).encode()
    full, server_channel = crypto.server_accept(
        hello, client.offer(), welcome or {"session_id": "s", "videos": ["v"]}, psk_server
    )
    # The welcome travels as JSON; the client sees the decoded object.
    received = json.loads(json.dumps(full))
    return client, hello, received, server_channel


def test_handshake_and_both_directions():
    client, hello, welcome, server = handshake(PSK, PSK)
    channel = client.finish(hello, welcome)
    for i in range(3):
        sealed = channel.seal(MessageType.GET_SEGMENT, i, b"seg-%d" % i)
        assert server.open(MessageType.GET_SEGMENT, i, sealed) == b"seg-%d" % i
        reply = server.seal(MessageType.SEGMENT, i, b"x" * 1000)
        assert len(reply) == 1000 + crypto.TAG_SIZE
        assert channel.open(MessageType.SEGMENT, i, reply) == b"x" * 1000
    assert channel.messages_sent == channel.messages_received == 3


def test_keys_are_fresh_per_connection():
    a_client, a_hello, a_welcome, _ = handshake()
    b_client, b_hello, b_welcome, _ = handshake()
    a = a_client.finish(a_hello, a_welcome).seal(MessageType.BYE, 1, b"same")
    b = b_client.finish(b_hello, b_welcome).seal(MessageType.BYE, 1, b"same")
    assert a != b


def test_wrong_psk_fails_key_confirmation():
    client, hello, welcome, _ = handshake(b"a" * 32, b"b" * 32)
    with pytest.raises(crypto.HandshakeError, match="key confirmation"):
        client.finish(hello, welcome)
    client, hello, welcome, _ = handshake(None, PSK)  # server has a PSK, client does not
    with pytest.raises(crypto.HandshakeError):
        client.finish(hello, welcome)


def test_tampered_welcome_fails_key_confirmation():
    client, hello, welcome, _ = handshake()
    welcome["videos"] = ["evil"]
    with pytest.raises(crypto.HandshakeError):
        client.finish(hello, welcome)
    client, hello, welcome, _ = handshake()
    with pytest.raises(crypto.HandshakeError):  # a different HELLO than the one answered
        client.finish(hello.replace(b'"c"', b'"d"'), welcome)


def test_server_rejects_bad_offers():
    with pytest.raises(crypto.HandshakeError, match="unsupported"):
        crypto.server_accept(b"{}", {"kex": "RSA", "cipher": "AES-256-GCM"}, {})
    offer = crypto.ClientHandshake().offer()
    offer["public_key"] = "AAAA"
    with pytest.raises(crypto.HandshakeError, match="32 bytes"):
        crypto.server_accept(b"{}", offer, {})
    offer["public_key"] = "@@@@"
    with pytest.raises(crypto.HandshakeError, match="base64"):
        crypto.server_accept(b"{}", offer, {})


@pytest.mark.parametrize("field", ["ciphertext", "tag", "request_id", "type"])
def test_tampering_is_detected(field):
    client, hello, welcome, server = handshake()
    channel = client.finish(hello, welcome)
    sealed = bytearray(server.seal(MessageType.SEGMENT, 7, b"payload bytes"))
    msg_type, request_id = MessageType.SEGMENT, 7
    if field == "ciphertext":
        sealed[0] ^= 1
    elif field == "tag":
        sealed[-1] ^= 1
    elif field == "request_id":
        request_id = 8
    else:
        msg_type = MessageType.MANIFEST
    with pytest.raises(crypto.DecryptionError):
        channel.open(msg_type, request_id, bytes(sealed))


def test_replay_reorder_and_reflection_are_detected():
    client, hello, welcome, server = handshake()
    channel = client.finish(hello, welcome)
    first = server.seal(MessageType.SEGMENT, 1, b"one")
    second = server.seal(MessageType.SEGMENT, 2, b"two")
    with pytest.raises(crypto.DecryptionError):  # reordered
        channel.open(MessageType.SEGMENT, 2, second)
    assert channel.open(MessageType.SEGMENT, 1, first) == b"one"
    with pytest.raises(crypto.DecryptionError):  # replayed
        channel.open(MessageType.SEGMENT, 1, first)
    own = channel.seal(MessageType.GET_SEGMENT, 3, b"req")
    with pytest.raises(crypto.DecryptionError):  # reflected back at the sender
        channel.open(MessageType.GET_SEGMENT, 3, own)


def test_header_flag_roundtrip():
    raw = proto.encode_header(MessageType.SEGMENT, 5, 32, 0, encrypted=True)
    assert raw[3] == 0x86
    header = proto.decode_header(raw, 1 << 20)
    assert header.type is MessageType.SEGMENT and header.encrypted
    assert not proto.decode_header(proto.encode_header(MessageType.SEGMENT, 5, 32, 0), 64).encrypted


def test_load_psk(tmp_path: Path):
    path = tmp_path / "psk"
    path.write_bytes(PSK + b"\n")
    assert crypto.load_psk(path) == PSK
    path.write_bytes(b"short\n")
    with pytest.raises(ValueError, match="at least"):
        crypto.load_psk(path)


# -- end to end ------------------------------------------------------------------------


@asynccontextmanager
async def server_with(media_dir: Path, db: Path, **kw) -> AsyncIterator[StreamServer]:
    txlog = TransmissionLog(db)
    server = StreamServer(
        ServerConfig(host="127.0.0.1", port=0, **kw), MediaCatalog.load(media_dir), txlog
    )
    await server.start()
    try:
        yield server
    finally:
        await server.close(grace=2.0)
        txlog.close()


@asynccontextmanager
async def recording_proxy(port: int) -> AsyncIterator[tuple[int, bytearray]]:
    """A TCP relay in front of the server that records every byte in both directions."""
    wire = bytearray()

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                wire.extend(data)
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    relay = await asyncio.start_server(on_connect, "127.0.0.1", 0)
    try:
        yield relay.sockets[0].getsockname()[1], wire
    finally:
        relay.close()


async def test_encrypted_session_hides_content(fake_media, tmp_path: Path):
    media_dir, manifest = fake_media
    psk_file = tmp_path / "psk"
    psk_file.write_bytes(PSK)
    db = tmp_path / "server.db"
    async with (
        server_with(media_dir, db, encryption="required", psk=PSK) as server,
        recording_proxy(server.port) as (port, wire),
    ):
        config = ClientConfig(
            host="127.0.0.1",
            port=port,
            video="test",
            name="secret-client",
            max_buffer_s=1.0,
            startup_buffer_s=0.2,
            download_dir=tmp_path / "download",
            log_db=tmp_path / "client.db",
            psk_file=psk_file,
        )
        result = await StreamingSession(config, create_abr("fixed:max")).run()
    assert result.status == "completed", result.error
    name = manifest.segment_filename(len(manifest.levels) - 1, 0)
    segment = (media_dir / "test" / name).read_bytes()
    assert len(wire) > len(segment)
    # Neither segment content, nor the manifest, nor requested file names appear on the wire.
    assert segment[:64] not in wire
    assert b"segment_sizes" not in wire and manifest.levels[-1].resolution.encode() not in wire
    assert name.encode() not in wire
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT encryption FROM sessions").fetchall() == [("AES-256-GCM+PSK",)]


async def test_required_mode_rejects_plaintext_client(fake_media, tmp_path: Path):
    media_dir, _ = fake_media
    async with server_with(media_dir, tmp_path / "s.db", encryption="required") as server:
        with pytest.raises(ServerError) as info:
            await StreamClient.connect("127.0.0.1", server.port, "plain")
    assert info.value.code == ErrorCode.ENCRYPTION_REQUIRED


async def test_optional_mode_serves_both(fake_media, tmp_path: Path):
    media_dir, manifest = fake_media
    async with server_with(media_dir, tmp_path / "s.db", encryption="optional") as server:
        plain = await StreamClient.connect("127.0.0.1", server.port, "plain")
        secure = await StreamClient.connect("127.0.0.1", server.port, "secure", encrypt=True)
        assert (plain.encryption, secure.encryption) == ("none", "AES-256-GCM")
        name = manifest.segment_filename(0, 1)
        a = await plain.fetch_segment(name)
        b = await secure.fetch_segment(name)
        assert a.data == b.data == (media_dir / "test" / name).read_bytes()
        assert (await secure.get_manifest("test")) == manifest
        await plain.close()
        await secure.close()


async def test_off_mode_fails_encrypting_client(fake_media, tmp_path: Path):
    media_dir, _ = fake_media
    async with server_with(media_dir, tmp_path / "s.db", encryption="off") as server:
        with pytest.raises(ProtocolError, match="did not accept encryption"):
            await StreamClient.connect("127.0.0.1", server.port, "c", encrypt=True)


async def test_wrong_psk_is_rejected_by_both_sides(fake_media, tmp_path: Path):
    media_dir, _ = fake_media
    async with server_with(media_dir, tmp_path / "s.db", encryption="required", psk=PSK) as server:
        with pytest.raises(ProtocolError, match="key confirmation"):
            await StreamClient.connect("127.0.0.1", server.port, "c", encrypt=True, psk=b"x" * 32)

        # A client that skips key confirmation: the server cannot decrypt its first request.
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        hs = crypto.ClientHandshake(b"x" * 32)
        hello = json.dumps({"client": "rogue", "encryption": hs.offer()}).encode()
        writer.write(proto.encode_message(Message(MessageType.HELLO, 1, hello)))
        welcome = await proto.read_message(reader, proto.MAX_RESPONSE_PAYLOAD)
        assert welcome is not None and welcome.type is MessageType.WELCOME
        bogus = crypto.SecureChannel(b"k" * 32, b"k" * 32, b"c2s\x00", b"s2c\x00")
        sealed = bogus.seal(MessageType.GET_MANIFEST, 2, b"test")
        writer.write(
            proto.encode_header(
                MessageType.GET_MANIFEST, 2, len(sealed), proto.crc32(sealed), encrypted=True
            )
            + sealed
        )
        reply = await proto.read_message(reader, proto.MAX_RESPONSE_PAYLOAD)
        assert reply is not None and reply.type is MessageType.ERROR
        assert reply.json()["code"] == ErrorCode.DECRYPTION_FAILED
        assert await reader.read() == b""  # and the connection is closed
        writer.close()


async def test_plaintext_request_on_encrypted_connection_is_rejected(fake_media, tmp_path: Path):
    media_dir, _ = fake_media
    async with server_with(media_dir, tmp_path / "s.db") as server:
        client = await StreamClient.connect("127.0.0.1", server.port, "c", encrypt=True)
        client._writer.write(
            proto.encode_message(Message(MessageType.GET_MANIFEST, 9, b"test"))  # no encryption
        )
        reply = await proto.read_message(client._reader, proto.MAX_RESPONSE_PAYLOAD)
        assert reply is not None and reply.type is MessageType.ERROR
        assert "plaintext" in reply.json()["message"]
        await client.close(send_bye=False)


def test_server_config_validation():
    with pytest.raises(ValueError):
        ServerConfig(encryption="maybe")
    with pytest.raises(ValueError, match="pre-shared"):
        ServerConfig(encryption="off", psk=PSK)


def test_old_transmission_log_gains_encryption_column(tmp_path: Path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, client_name TEXT, "
            "peer TEXT NOT NULL, connected_at REAL NOT NULL, disconnected_at REAL, "
            "close_reason TEXT)"
        )
    log = TransmissionLog(db)
    log.session_opened("s1", "peer", 0.0)
    log.session_named("s1", "c", "AES-256-GCM")
    log.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT encryption FROM sessions").fetchall() == [("AES-256-GCM",)]
