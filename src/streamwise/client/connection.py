"""Client side of the StreamWise protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass

from streamwise.common import crypto
from streamwise.common import protocol as proto
from streamwise.common.manifest import Manifest
from streamwise.common.protocol import Message, MessageType, ProtocolError

READ_CHUNK = 64 * 1024
OFFLOAD_BYTES = 256 * 1024
"""Decrypt payloads at least this large in a worker thread, off the event loop."""


class ServerError(Exception):
    """The server answered a request with an ERROR message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"server error {code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class FetchResult:
    filename: str
    data: bytes
    request_time: float
    """``time.monotonic()`` when the request was sent."""
    first_byte_time: float
    """When the response header arrived."""
    done_time: float
    """When the last payload byte arrived (and, if encrypted, was decrypted)."""

    @property
    def download_time(self) -> float:
        return self.done_time - self.request_time

    @property
    def throughput_bps(self) -> float:
        """Application-level throughput including request latency (as seen by the player)."""
        return len(self.data) * 8 / max(self.download_time, 1e-6)


class StreamClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        stall_timeout: float,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 1
        self._stall_timeout = stall_timeout
        self._lock = asyncio.Lock()
        self.session_id: str | None = None
        self.server_name: str | None = None
        self.videos: list[str] = []
        self.channel: crypto.SecureChannel | None = None
        self.encryption = "none"
        """Negotiated protection: ``none``, ``AES-256-GCM`` or ``AES-256-GCM+PSK``."""

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        client_name: str,
        *,
        connect_timeout: float = 10.0,
        stall_timeout: float = 30.0,
        encrypt: bool = False,
        psk: bytes | None = None,
    ) -> StreamClient:
        """Open a connection and perform the HELLO/WELCOME handshake.

        ``stall_timeout`` bounds how long the client waits without receiving *any* byte; a slow
        but progressing download never times out. With ``encrypt`` the handshake also agrees on
        AES-256-GCM keys (authenticated by ``psk`` if given), and the connection fails unless
        the server accepts encryption.
        """
        if psk is not None and not encrypt:
            raise ValueError("a pre-shared key requires encryption")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), connect_timeout
            )
        except TimeoutError as exc:
            raise TimeoutError(f"connecting to {host}:{port} timed out") from exc
        client = cls(reader, writer, stall_timeout=stall_timeout)
        try:
            hello: dict[str, object] = {"client": client_name}
            handshake = crypto.ClientHandshake(psk) if encrypt else None
            if handshake is not None:
                hello["encryption"] = handshake.offer()
            hello_payload = json.dumps(hello).encode()
            welcome = await client._request(MessageType.HELLO, hello_payload, MessageType.WELCOME)
            info = welcome.json()
            if handshake is not None:
                try:
                    client.channel = handshake.finish(hello_payload, info)
                except crypto.HandshakeError as exc:
                    raise ProtocolError(f"encryption handshake failed: {exc}") from exc
                client.encryption = crypto.CIPHER + ("+PSK" if psk else "")
            client.session_id = str(info.get("session_id"))
            client.server_name = str(info.get("server"))
            client.videos = list(info.get("videos", []))
        except BaseException:
            await client.close(send_bye=False)
            raise
        return client

    async def get_manifest(self, video: str) -> Manifest:
        msg = await self._request(MessageType.GET_MANIFEST, video.encode(), MessageType.MANIFEST)
        try:
            manifest = Manifest.from_json(msg.payload)
        except (ValueError, KeyError, TypeError) as exc:
            raise ProtocolError(f"invalid manifest: {exc}") from exc
        if manifest.video != video:
            raise ProtocolError(f"asked for {video!r}, got manifest for {manifest.video!r}")
        return manifest

    async def fetch_segment(self, filename: str) -> FetchResult:
        async with self._lock:
            request_id = self._take_id()
            request_time = time.monotonic()
            await self._write(Message(MessageType.GET_SEGMENT, request_id, filename.encode()))
            header = await self._read_header()
            first_byte_time = time.monotonic()
            payload = await self._open(header, await self._read_payload(header))
            done_time = time.monotonic()
        self._check_response(
            header.type, header.request_id, request_id, payload, MessageType.SEGMENT
        )
        return FetchResult(filename, payload, request_time, first_byte_time, done_time)

    async def close(self, *, send_bye: bool = True) -> None:
        if send_bye and not self._writer.is_closing():
            with contextlib.suppress(Exception):
                await self._write(Message(MessageType.BYE, self._take_id()))
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    # -- internals ---------------------------------------------------------------------

    def _take_id(self) -> int:
        request_id = self._next_id
        self._next_id = (self._next_id + 1) & 0xFFFFFFFF or 1
        return request_id

    async def _write(self, msg: Message) -> None:
        if self.channel is None:
            self._writer.write(proto.encode_message(msg))
        else:
            sealed = self.channel.seal(msg.type, msg.request_id, msg.payload)
            header = proto.encode_header(
                msg.type, msg.request_id, len(sealed), proto.crc32(sealed), encrypted=True
            )
            self._writer.write(header + sealed)
        await self._writer.drain()

    async def _open(self, header: proto.Header, payload: bytes) -> bytes:
        """Authenticate and decrypt a received payload (no-op on a plaintext connection)."""
        if self.channel is None:
            if header.encrypted:
                raise ProtocolError("encrypted message without a key exchange")
            return payload
        if not header.encrypted:
            if header.type is MessageType.ERROR:
                return payload  # the server could not decrypt our request (e.g. wrong PSK)
            raise ProtocolError(f"plaintext {header.type.name} on an encrypted connection")
        try:
            if len(payload) >= OFFLOAD_BYTES:
                return await asyncio.to_thread(
                    self.channel.open, header.type, header.request_id, payload
                )
            return self.channel.open(header.type, header.request_id, payload)
        except crypto.DecryptionError as exc:
            raise ProtocolError(f"{header.type.name}: {exc}") from exc

    async def _read_header(self) -> proto.Header:
        header = await asyncio.wait_for(
            proto.read_header(self._reader, proto.MAX_RESPONSE_PAYLOAD), self._stall_timeout
        )
        if header is None:
            raise ConnectionError("server closed the connection")
        return header

    async def _read_payload(self, header: proto.Header) -> bytes:
        buf = bytearray()
        while len(buf) < header.payload_length:
            chunk = await asyncio.wait_for(
                self._reader.read(min(READ_CHUNK, header.payload_length - len(buf))),
                self._stall_timeout,
            )
            if not chunk:
                raise ProtocolError(
                    f"connection closed after {len(buf)}/{header.payload_length} payload bytes"
                )
            buf += chunk
        payload = bytes(buf)
        if proto.crc32(payload) != header.payload_crc32:
            raise ProtocolError(f"CRC-32 mismatch in {header.type.name} payload")
        return payload

    async def _request(
        self, msg_type: MessageType, payload: bytes, expected: MessageType
    ) -> Message:
        async with self._lock:
            request_id = self._take_id()
            await self._write(Message(msg_type, request_id, payload))
            header = await self._read_header()
            body = await self._open(header, await self._read_payload(header))
        self._check_response(header.type, header.request_id, request_id, body, expected)
        return Message(header.type, header.request_id, body)

    @staticmethod
    def _check_response(
        got: MessageType, got_id: int, want_id: int, payload: bytes, expected: MessageType
    ) -> None:
        if got is MessageType.ERROR:
            info = Message(got, got_id, payload).json()
            raise ServerError(int(info.get("code", 0)), str(info.get("message", "")))
        if got_id != want_id:
            raise ProtocolError(f"response id {got_id} does not match request id {want_id}")
        if got is not expected:
            raise ProtocolError(f"expected {expected.name}, got {got.name}")
