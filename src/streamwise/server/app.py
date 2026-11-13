"""Concurrent TCP segment server.

Each accepted connection is served by its own asyncio task, so any number of clients are
handled concurrently on one thread; a slow or stalled client only blocks its own task. Within
a connection the protocol is strictly request/response.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from streamwise.common import protocol as proto
from streamwise.common.protocol import ErrorCode, Message, MessageType, ProtocolError
from streamwise.server.catalog import MediaCatalog
from streamwise.server.shaping import Shaper, ShaperFactory, ShapingConfig
from streamwise.server.txlog import Transmission, TransmissionLog

log = logging.getLogger(__name__)

SERVER_NAME = "StreamWise/0.1"


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


@dataclass(slots=True)
class Session:
    session_id: str
    peer: str
    shaper: Shaper
    client_name: str | None = None

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
        try:
            reason = await self._serve(session, reader, writer)
        except ProtocolError as exc:
            reason = f"protocol_error: {exc}"
            log.warning("%s protocol error: %s", session.label, exc)
            with contextlib.suppress(OSError):
                await self._send(
                    session, writer, proto.error_message(0, ErrorCode.BAD_REQUEST, str(exc))
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
            proto.read_message(reader, proto.MAX_REQUEST_PAYLOAD), self.config.handshake_timeout
        )
        if hello is None:
            return "closed_before_hello"
        if hello.type is not MessageType.HELLO:
            raise ProtocolError(f"expected HELLO, got {hello.type.name}")
        name = hello.json().get("client")
        session.client_name = str(name)[:64] if name else f"anon-{session.session_id[:4]}"
        self.txlog.session_named(session.session_id, session.client_name)
        await self._send(
            session,
            writer,
            proto.json_message(
                MessageType.WELCOME,
                hello.request_id,
                {
                    "session_id": session.session_id,
                    "server": SERVER_NAME,
                    "videos": self.catalog.video_names,
                },
            ),
        )
        log.info("%s hello from %s", session.label, session.peer)

        while True:
            msg = await asyncio.wait_for(
                proto.read_message(reader, proto.MAX_REQUEST_PAYLOAD), self.config.idle_timeout
            )
            if msg is None:
                return "client_closed"
            match msg.type:
                case MessageType.GET_MANIFEST:
                    await self._handle_manifest(session, writer, msg)
                case MessageType.GET_SEGMENT:
                    await self._handle_segment(session, writer, msg)
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
        sent = 0
        status = "error"
        try:
            sent = await self._send(session, writer, reply)
            status = "ok"
        except (ConnectionError, OSError, asyncio.CancelledError):
            status = "aborted"
            raise
        finally:
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

    async def _send(self, session: Session, writer: asyncio.StreamWriter, msg: Message) -> int:
        """Write a message through the connection's shaper. Returns bytes written."""
        header = proto.encode_header(
            msg.type, msg.request_id, len(msg.payload), proto.crc32(msg.payload)
        )
        view = memoryview(header + msg.payload) if len(msg.payload) < 65536 else None
        written = 0
        chunk = self._chunk_size

        async def write(buf: bytes | memoryview) -> None:
            nonlocal written
            await session.shaper.consume(len(buf))
            writer.write(buf)
            await writer.drain()
            written += len(buf)

        if view is not None:
            for off in range(0, len(view), chunk):
                await write(view[off : off + chunk])
        else:
            await write(header)
            payload = memoryview(msg.payload)
            for off in range(0, len(payload), chunk):
                await write(payload[off : off + chunk])
        return written


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
