"""Client side of the StreamWise protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass

from streamwise.common import protocol as proto
from streamwise.common.manifest import Manifest
from streamwise.common.protocol import Message, MessageType, ProtocolError

READ_CHUNK = 64 * 1024


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
    """When the last payload byte arrived."""

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

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        client_name: str,
        *,
        connect_timeout: float = 10.0,
        stall_timeout: float = 30.0,
    ) -> StreamClient:
        """Open a connection and perform the HELLO/WELCOME handshake.

        ``stall_timeout`` bounds how long the client waits without receiving *any* byte; a slow
        but progressing download never times out.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), connect_timeout
        )
        client = cls(reader, writer, stall_timeout=stall_timeout)
        try:
            welcome = await client._request(
                MessageType.HELLO,
                json.dumps({"client": client_name}).encode(),
                MessageType.WELCOME,
            )
            info = welcome.json()
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
            payload = await self._read_payload(header)
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
        self._writer.write(proto.encode_message(msg))
        await self._writer.drain()

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
            body = await self._read_payload(header)
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
