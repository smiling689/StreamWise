"""Concurrent TCP segment server.

Each accepted connection is served by its own asyncio task, so any number of clients are
handled concurrently on one thread; a slow or stalled client only blocks its own task. Within
a connection, requests may be pipelined: a reader task queues them while a responder answers
them strictly in order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from streamwise.common import crypto
from streamwise.common import protocol as proto
from streamwise.common.protocol import ErrorCode, Message, MessageType, ProtocolError
from streamwise.server.catalog import MediaCatalog
from streamwise.server.shaping import Shaper, ShaperFactory, ShapingConfig
from streamwise.server.txlog import Transmission, TransmissionLog

log = logging.getLogger(__name__)

SERVER_NAME = "StreamWise/0.1"
ENCRYPTION_MODES = ("off", "optional", "required")
OFFLOAD_BYTES = 256 * 1024
"""Encrypt payloads at least this large in a worker thread, off the event loop."""
MAX_PIPELINE = 256
"""Requests read ahead per connection; beyond this TCP flow control pushes back."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 9000
    shaping: ShapingConfig = field(default_factory=ShapingConfig)
    chunk_size: int = 16 * 1024
    """Bytes written per shaping step."""
    handshake_timeout: float = 10.0
    idle_timeout: float = 120.0
    """Close a connection that sends no request for this long."""
    encryption: str = "optional"
    """'off': ignore encryption offers; 'optional': encrypt when the client offers it;
    'required': reject clients that do not."""
    psk: bytes | None = field(default=None, repr=False)
    """Pre-shared key mixed into the key derivation; authenticates both peers."""
    delay_s: float = 0.0
    """Emulated round-trip delay: each request is answered this long after it arrived."""
    reset_after_s: float | None = None
    """Fault injection: abort every connection this long after it was accepted."""

    def __post_init__(self) -> None:
        if self.encryption not in ENCRYPTION_MODES:
            raise ValueError(f"encryption must be one of {ENCRYPTION_MODES}")
        if self.psk is not None and self.encryption == "off":
            raise ValueError("a pre-shared key needs encryption 'optional' or 'required'")
        if self.delay_s < 0:
            raise ValueError("delay must not be negative")
        if self.reset_after_s is not None and self.reset_after_s <= 0:
            raise ValueError("reset_after_s must be positive")


@dataclass(slots=True)
class Session:
    session_id: str
    peer: str
    shaper: Shaper
    client_name: str | None = None
    channel: crypto.SecureChannel | None = None
    reset_injected: bool = False
    bytes_written: int = 0
    """Bytes handed to TCP on this connection so far (also counts partial messages)."""

    @property
    def label(self) -> str:
        return f"{self.client_name or '?'}[{self.session_id}]"


class StreamServer:
    def __init__(self, config: ServerConfig, catalog: MediaCatalog, txlog: TransmissionLog):
        self.config = config
        self.catalog = catalog
        self.txlog = txlog
        self._shapers = ShaperFactory(config.shaping)
        # A write chunk must fit in the token bucket with room to spare: tokens that accrue
        # while a task oversleeps are capped at the bucket size, so a chunk equal to the bucket
        # would lose them and undershoot the configured rate.
        self._chunk_size = max(1024, min(config.chunk_size, config.shaping.burst_bytes // 2))
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server not started")
        return self._server.sockets[0].getsockname()[1]

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connect, self.config.host, self.config.port, reuse_address=True
        )
        log.info("listening on %s:%d", self.config.host, self.port)

    async def close(self, grace: float = 0.0) -> None:
        """Stop accepting connections, give active ones ``grace`` seconds to finish, then
        cancel and await the rest."""
        if self._server is not None:
            self._server.close()
        if grace > 0 and self._connections:
            await asyncio.wait(list(self._connections), timeout=grace)
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else "unknown"
        session = Session(uuid.uuid4().hex[:12], peer, self._shapers.for_connection())
        self.txlog.session_opened(session.session_id, peer, time.time())
        log.info("connection %s from %s", session.session_id, peer)
        reason = "closed"
        reset_timer = None
        if self.config.reset_after_s is not None:

            def inject_reset() -> None:
                session.reset_injected = True
                writer.transport.abort()

            reset_timer = asyncio.get_running_loop().call_later(
                self.config.reset_after_s, inject_reset
            )
        try:
            reason = await self._serve(session, reader, writer)
        except crypto.DecryptionError as exc:
            reason = "decryption_failed"
            log.warning("%s decryption failed: %s", session.label, exc)
            with contextlib.suppress(OSError):
                await self._send(
                    session,
                    writer,
                    proto.error_message(0, ErrorCode.DECRYPTION_FAILED, str(exc)),
                    encrypt=False,
                )
        except ProtocolError as exc:
            reason = f"protocol_error: {exc}"
            log.warning("%s protocol error: %s", session.label, exc)
            with contextlib.suppress(OSError):
                await self._send(
                    session,
                    writer,
                    proto.error_message(0, ErrorCode.BAD_REQUEST, str(exc)),
                    encrypt=False,
                )
        except TimeoutError:
            reason = "timeout"
            log.info("%s timed out", session.label)
        except (ConnectionError, OSError) as exc:
            reason = f"connection_lost: {exc.__class__.__name__}"
            log.info("%s connection lost (%s)", session.label, exc.__class__.__name__)
        except asyncio.CancelledError:
            reason = "server_shutdown"
            raise
        except Exception:
            reason = "internal_error"
            log.exception("%s internal error", session.label)
        finally:
            if reset_timer is not None:
                reset_timer.cancel()
            if session.reset_injected:
                reason = "injected_reset"
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.txlog.session_closed(session.session_id, time.time(), reason)
            self._connections.discard(task)
            log.info("%s disconnected (%s)", session.label, reason)

    async def _serve(
        self, session: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> str:
        hello = await asyncio.wait_for(
            self._read_request(session, reader), self.config.handshake_timeout
        )
        if hello is None:
            return "closed_before_hello"
        if hello.type is not MessageType.HELLO:
            raise ProtocolError(f"expected HELLO, got {hello.type.name}")
        info = hello.json()
        name = info.get("client")
        session.client_name = str(name)[:64] if name else f"anon-{session.session_id[:4]}"
        session.shaper = self._shapers.for_client(session.client_name, session.shaper)
        welcome = {
            "session_id": session.session_id,
            "server": SERVER_NAME,
            "videos": self.catalog.video_names,
        }
        offer = info.get("encryption")
        channel = None
        if offer is not None and self.config.encryption != "off":
            try:
                welcome, channel = crypto.server_accept(
                    hello.payload, offer, welcome, self.config.psk
                )
            except crypto.HandshakeError as exc:
                raise ProtocolError(f"encryption handshake: {exc}") from exc
        encryption = "none"
        if channel is not None:
            encryption = crypto.CIPHER + ("+PSK" if self.config.psk else "")
        self.txlog.session_named(session.session_id, session.client_name, encryption)
        if channel is None and self.config.encryption == "required":
            await self._send(
                session,
                writer,
                proto.error_message(
                    hello.request_id,
                    ErrorCode.ENCRYPTION_REQUIRED,
                    "this server only accepts encrypted connections",
                ),
            )
            return "encryption_required"
        await self._send(
            session, writer, proto.json_message(MessageType.WELCOME, hello.request_id, welcome)
        )
        session.channel = channel  # everything after WELCOME is encrypted
        log.info("%s hello from %s (encryption: %s)", session.label, session.peer, encryption)

        return await self._serve_requests(session, reader, writer)

    async def _serve_requests(
        self, session: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> str:
        """Answer pipelined requests in order.

        A reader task queues requests with their arrival time; this task answers them, each
        no earlier than ``delay_s`` after its arrival. A failure in the reader is queued as
        well, so it surfaces only after the responses to earlier requests have been sent.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[Message, float] | BaseException | None] = asyncio.Queue(
            MAX_PIPELINE
        )

        async def read_requests() -> None:
            try:
                while True:
                    msg = await self._read_request(session, reader)
                    await queue.put(None if msg is None else (msg, loop.time()))
                    if msg is None or msg.type is MessageType.BYE:
                        return
            except Exception as exc:
                await queue.put(exc)

        reader_task = asyncio.create_task(read_requests())
        try:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    item = await asyncio.wait_for(queue.get(), self.config.idle_timeout)
                if item is None:
                    return "client_closed"
                if isinstance(item, BaseException):
                    raise item
                msg, arrived = item
                if self.config.delay_s > 0:
                    await asyncio.sleep(max(0.0, arrived + self.config.delay_s - loop.time()))
                match msg.type:
                    case MessageType.GET_MANIFEST:
                        await self._handle_manifest(session, writer, msg)
                    case MessageType.GET_SEGMENT:
                        await self._handle_segment(session, writer, msg)
                    case MessageType.GET_CHUNK:
                        await self._handle_chunk(session, writer, msg)
                    case MessageType.BYE:
                        return "bye"
                    case _:
                        await self._send(
                            session,
                            writer,
                            proto.error_message(
                                msg.request_id,
                                ErrorCode.UNEXPECTED_MESSAGE,
                                f"unexpected {msg.type.name}",
                            ),
                        )
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task

    async def _handle_manifest(self, session: Session, writer: asyncio.StreamWriter, msg: Message):
        name = msg.text()
        entry = self.catalog.video(name)
        if entry is None:
            reply = proto.error_message(msg.request_id, ErrorCode.NOT_FOUND, f"no video {name!r}")
        else:
            reply = Message(MessageType.MANIFEST, msg.request_id, entry.manifest_bytes)
        await self._send(session, writer, reply)

    async def _handle_segment(self, session: Session, writer: asyncio.StreamWriter, msg: Message):
        filename = msg.text()
        started_wall, started = time.time(), time.perf_counter()
        seg = self.catalog.resolve(filename)
        if seg is None:
            await self._send(
                session,
                writer,
                proto.error_message(
                    msg.request_id, ErrorCode.NOT_FOUND, f"no segment {filename!r}"
                ),
            )
            self.txlog.transmission(
                Transmission(
                    session.session_id,
                    msg.request_id,
                    filename[:128],
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    started_wall,
                    time.perf_counter() - started,
                    "not_found",
                )  # fmt: skip
            )
            return

        data = await asyncio.to_thread(seg.path.read_bytes)
        if len(data) != seg.size:
            log.error("%s changed on disk (size %d != %d)", seg.path, len(data), seg.size)
            await self._send(
                session,
                writer,
                proto.error_message(msg.request_id, ErrorCode.INTERNAL_ERROR, "segment changed"),
            )
            return

        reply = Message(MessageType.SEGMENT, msg.request_id, data)
        before = session.bytes_written
        status = "error"
        try:
            await self._send(session, writer, reply)
            status = "ok"
        except (ConnectionError, OSError, asyncio.CancelledError):
            status = "aborted"
            raise
        finally:
            sent = session.bytes_written - before
            duration = time.perf_counter() - started
            self.txlog.transmission(
                Transmission(
                    session.session_id,
                    msg.request_id,
                    seg.filename,
                    seg.video,
                    seq=seg.seq,
                    level=seg.level,
                    resolution=seg.resolution,
                    bitrate_kbps=seg.bitrate_kbps,
                    bytes=max(0, sent - proto.HEADER_SIZE),
                    start_time=started_wall,
                    duration_s=duration,
                    status=status,
                )  # fmt: skip
            )
            if status == "ok":
                log.info(
                    "%s sent %s (%.2f MB) in %.2fs, %.2f Mbps",
                    session.label,
                    seg.filename,
                    len(data) / 1e6,
                    duration,
                    len(data) * 8 / duration / 1e6 if duration > 0 else float("inf"),
                )

    async def _handle_chunk(self, session: Session, writer: asyncio.StreamWriter, msg: Message):
        filename, offset, length = proto.parse_chunk_request(msg.payload)
        started_wall, started = time.time(), time.perf_counter()
        seg = self.catalog.resolve(filename)
        error = None
        if seg is None:
            error = proto.error_message(
                msg.request_id, ErrorCode.NOT_FOUND, f"no segment {filename!r}"
            )
        elif length == 0 or length > proto.MAX_CHUNK or offset + length > seg.size:
            error = proto.error_message(
                msg.request_id,
                ErrorCode.RANGE_NOT_SATISFIABLE,
                f"range {offset}+{length} invalid for {seg.size}-byte segment "
                f"(at most {proto.MAX_CHUNK} bytes per chunk)",
            )
        if error is not None:
            await self._send(session, writer, error)
            return
        assert seg is not None
        data = await asyncio.to_thread(_read_range, seg.path, offset, length)
        if len(data) != length:
            await self._send(
                session,
                writer,
                proto.error_message(msg.request_id, ErrorCode.INTERNAL_ERROR, "segment changed"),
            )
            return
        before, status = session.bytes_written, "error"
        try:
            await self._send(session, writer, Message(MessageType.CHUNK, msg.request_id, data))
            status = "ok"
        except (ConnectionError, OSError, asyncio.CancelledError):
            status = "aborted"
            raise
        finally:
            sent = session.bytes_written - before
            self.txlog.transmission(
                Transmission(
                    session.session_id,
                    msg.request_id,
                    seg.filename,
                    seg.video,
                    seq=seg.seq,
                    level=seg.level,
                    resolution=seg.resolution,
                    bitrate_kbps=seg.bitrate_kbps,
                    bytes=max(0, sent - proto.HEADER_SIZE),
                    start_time=started_wall,
                    duration_s=time.perf_counter() - started,
                    status=status,
                    chunk_offset=offset,
                )  # fmt: skip
            )

    async def _read_request(self, session: Session, reader: asyncio.StreamReader) -> Message | None:
        """Read one request and, on an encrypted connection, authenticate and decrypt it."""
        header = await proto.read_header(reader, proto.MAX_REQUEST_PAYLOAD)
        if header is None:
            return None
        payload = await proto.read_payload(reader, header)
        if session.channel is None:
            if header.encrypted:
                raise ProtocolError("encrypted message without a key exchange")
        else:
            if not header.encrypted:
                raise ProtocolError(f"plaintext {header.type.name} on an encrypted connection")
            payload = session.channel.open(header.type, header.request_id, payload)
        return Message(header.type, header.request_id, payload)

    async def _send(
        self,
        session: Session,
        writer: asyncio.StreamWriter,
        msg: Message,
        *,
        encrypt: bool = True,
    ) -> int:
        """Write a message through the connection's shaper. Returns bytes written.

        On an encrypted connection the payload is sealed first; ``encrypt=False`` sends a
        plaintext message (used for fatal errors the client may not be able to decrypt).
        """
        payload = msg.payload
        channel = session.channel if encrypt else None
        if channel is not None:
            if len(payload) >= OFFLOAD_BYTES:
                payload = await asyncio.to_thread(channel.seal, msg.type, msg.request_id, payload)
            else:
                payload = channel.seal(msg.type, msg.request_id, payload)
        header = proto.encode_header(
            msg.type,
            msg.request_id,
            len(payload),
            proto.crc32(payload),
            encrypted=channel is not None,
        )
        view = memoryview(header + payload) if len(payload) < 65536 else None
        written = 0
        chunk = self._chunk_size

        async def write(buf: bytes | memoryview) -> None:
            nonlocal written
            await session.shaper.consume(len(buf))
            writer.write(buf)
            session.bytes_written += len(buf)
            await writer.drain()
            written += len(buf)

        if view is not None:
            for off in range(0, len(view), chunk):
                await write(view[off : off + chunk])
        else:
            await write(header)
            body = memoryview(payload)
            for off in range(0, len(body), chunk):
                await write(body[off : off + chunk])
        return written


def _read_range(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(offset)
        return fh.read(length)


async def run_server(
    config: ServerConfig, media_dir: Path, log_db: Path, stop: asyncio.Event
) -> None:
    catalog = MediaCatalog.load(media_dir)
    txlog = TransmissionLog(log_db)
    server = StreamServer(config, catalog, txlog)
    try:
        await server.start()
        await stop.wait()
        log.info("shutting down (%d active connections)", server.active_connections)
    finally:
        await server.close()
        txlog.close()
