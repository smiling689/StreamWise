"""End-to-end tests: real server and clients over loopback TCP with fake media."""

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
from streamwise.common import protocol as proto
from streamwise.common.protocol import Message, MessageType
from streamwise.server.app import ServerConfig, StreamServer
from streamwise.server.catalog import CatalogError, MediaCatalog
from streamwise.server.shaping import BandwidthTrace, ShapingConfig
from streamwise.server.txlog import TransmissionLog


@asynccontextmanager
async def running_server(
    media_dir: Path, db: Path, trace: BandwidthTrace | None = None, scope: str = "connection"
) -> AsyncIterator[StreamServer]:
    txlog = TransmissionLog(db)
    server = StreamServer(
        ServerConfig(host="127.0.0.1", port=0, shaping=ShapingConfig(trace, scope, 32 * 1024)),
        MediaCatalog.load(media_dir),
        txlog,
    )
    await server.start()
    try:
        yield server
    finally:
        await server.close(grace=2.0)  # let clients' BYE be processed
        txlog.close()


def client_config(tmp_path: Path, port: int, name: str, **kw) -> ClientConfig:
    defaults = dict(
        host="127.0.0.1",
        port=port,
        video="test",
        name=name,
        max_buffer_s=1.0,
        startup_buffer_s=0.2,
        download_dir=tmp_path / "download",
        log_db=tmp_path / "client.db",
    )
    defaults.update(kw)
    return ClientConfig(**defaults)


def query(db: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


async def test_three_concurrent_clients(tmp_path: Path, fake_media) -> None:
    media_dir, manifest = fake_media
    server_db = tmp_path / "server.db"
    async with running_server(media_dir, server_db, BandwidthTrace.constant(4.0)) as server:
        sessions = [
            StreamingSession(client_config(tmp_path, server.port, f"c{i}"), create_abr(spec))
            for i, spec in enumerate(["throughput", "buffer", "fixed:max"])
        ]
        results = await asyncio.wait_for(asyncio.gather(*(s.run() for s in sessions)), 30)

    for result in results:
        assert result.status == "completed", result.error
        assert [r.seq for r in result.segments] == list(range(manifest.segment_count))
        assert result.metrics is not None
        assert result.metrics.segments == manifest.segment_count
    # Cache: every segment deleted after playback and the download root removed.
    assert not (tmp_path / "download").exists()

    rows = query(server_db, "SELECT status, COUNT(*) FROM transmissions GROUP BY status")
    assert rows == [("ok", 3 * manifest.segment_count)]
    sessions_rows = query(server_db, "SELECT client_name, close_reason FROM sessions ORDER BY 1")
    assert sessions_rows == [("c0", "bye"), ("c1", "bye"), ("c2", "bye")]
    # Every transmission respected the 4 Mbps shaping (allowing for the initial burst).
    for size, duration in query(server_db, "SELECT bytes, duration_s FROM transmissions"):
        assert size * 8 / duration < 4e6 * 1.6 or size < 40_000

    client_rows = query(tmp_path / "client.db", "SELECT status, segments FROM sessions")
    assert client_rows == [("completed", manifest.segment_count)] * 3
    seg_rows = query(tmp_path / "client.db", "SELECT COUNT(*) FROM segments")
    assert seg_rows == [(3 * manifest.segment_count,)]


async def test_fixed_max_level_on_slow_link_stalls(tmp_path: Path, fake_media) -> None:
    media_dir, manifest = fake_media
    async with running_server(media_dir, tmp_path / "s.db", BandwidthTrace.constant(2.0)) as srv:
        # Top level needs ~5.25 Mbps: at 2 Mbps every segment takes ~2.6x real time.
        result = await StreamingSession(
            client_config(tmp_path, srv.port, "slow"), create_abr("fixed:max")
        ).run()
    assert result.status == "completed"
    assert result.metrics is not None
    assert result.metrics.stall_count >= 3
    assert result.metrics.rebuffer_ratio > 0.3
    stall_rows = query(tmp_path / "client.db", "SELECT COUNT(*) FROM stalls")
    assert stall_rows == [(result.metrics.stall_count,)]


async def test_bandwidth_drop_triggers_downswitch(tmp_path: Path, fake_media) -> None:
    media_dir, _ = fake_media
    from .conftest import make_manifest, write_fake_media

    manifest = make_manifest(video="long", segments=16)
    write_fake_media(media_dir, manifest)
    trace = BandwidthTrace.parse("0 20\n1.2 1.0")
    async with running_server(media_dir, tmp_path / "s.db", trace) as srv:
        result = await StreamingSession(
            client_config(tmp_path, srv.port, "drop", video="long"), create_abr("throughput")
        ).run()
    assert result.status == "completed"
    levels = [r.level for r in result.segments]
    assert max(levels[:6]) == 2  # high bandwidth: top level reached
    assert levels[-1] == 0  # after the drop to 1 Mbps: lowest level


async def test_errors_and_path_traversal(tmp_path: Path, fake_media) -> None:
    media_dir, manifest = fake_media
    (tmp_path / "secret-360p-800k-0.ts").write_bytes(b"secret")
    async with running_server(media_dir, tmp_path / "s.db") as srv:
        client = await StreamClient.connect("127.0.0.1", srv.port, "probe")
        assert client.videos == ["test"]
        for bad in [
            "test-360p-800k-99.ts",
            "test-360p-999k-0.ts",
            "../secret-360p-800k-0.ts",
            "test-360p-800k-0.ts/../../x",
            "manifest.json",
        ]:
            with pytest.raises(ServerError) as info:
                await client.fetch_segment(bad)
            assert info.value.code == proto.ErrorCode.NOT_FOUND
        with pytest.raises(ServerError):
            await client.get_manifest("nope")
        # The connection is still usable after errors.
        ok = await client.fetch_segment(manifest.segment_filename(0, 0))
        assert len(ok.data) == manifest.segment_size(0, 0)
        assert ok.data == (media_dir / "test" / ok.filename).read_bytes()
        await client.close()
    rows = query(tmp_path / "s.db", "SELECT status, COUNT(*) FROM transmissions GROUP BY 1")
    assert dict(rows) == {"not_found": 5, "ok": 1}


async def test_misbehaving_peers_do_not_affect_others(tmp_path: Path, fake_media) -> None:
    media_dir, manifest = fake_media
    db = tmp_path / "s.db"
    async with running_server(media_dir, db, BandwidthTrace.constant(1.0)) as srv:
        good = asyncio.create_task(
            StreamingSession(client_config(tmp_path, srv.port, "good"), create_abr("fixed:0")).run()
        )

        # 1. Garbage instead of a header: server answers with ERROR and closes.
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        await writer.drain()
        reply = await proto.read_message(reader, proto.MAX_RESPONSE_PAYLOAD)
        assert reply is not None and reply.type is MessageType.ERROR
        assert await reader.read() == b""
        writer.close()

        # 2. Client vanishes in the middle of a large, slow download.
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        hello = json.dumps({"client": "quitter"}).encode()
        writer.write(proto.encode_message(Message(MessageType.HELLO, 1, hello)))
        name = manifest.segment_filename(2, 0).encode()
        writer.write(proto.encode_message(Message(MessageType.GET_SEGMENT, 2, name)))
        await writer.drain()
        await reader.readexactly(proto.HEADER_SIZE)  # WELCOME header
        await asyncio.sleep(0.15)
        writer.transport.abort()

        # 3. Silent connection that never says HELLO is closed by the server on shutdown.
        _, idle_writer = await asyncio.open_connection("127.0.0.1", srv.port)

        result = await asyncio.wait_for(good, 30)
        assert result.status == "completed"
        idle_writer.close()

    named = dict(
        query(db, "SELECT client_name, close_reason FROM sessions WHERE client_name IS NOT NULL")
    )
    assert named["good"] == "bye"
    assert named["quitter"].startswith("connection_lost")
    anonymous = sorted(
        r for (r,) in query(db, "SELECT close_reason FROM sessions WHERE client_name IS NULL")
    )
    assert len(anonymous) == 2
    assert anonymous[0] == "closed_before_hello"
    assert anonymous[1].startswith("protocol_error")
    aborted = query(db, "SELECT bytes FROM transmissions WHERE status = 'aborted'")
    assert len(aborted) == 1
    # The partial transfer is logged with the bytes that were sent before the abort.
    assert 0 < aborted[0][0] < manifest.segment_size(2, 0)


async def test_server_unreachable(tmp_path: Path) -> None:
    import socket

    with socket.socket() as sock:  # a port that was free a moment ago: connection refused
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = await StreamingSession(
        client_config(tmp_path, port, "nobody", connect_timeout=2.0), create_abr("buffer")
    ).run()
    assert result.status == "failed"
    assert "ConnectionRefusedError" in (result.error or "")
    assert not (tmp_path / "download").exists()


def test_catalog_detects_corrupt_media(tmp_path: Path, fake_media) -> None:
    media_dir, manifest = fake_media
    victim = media_dir / "test" / manifest.segment_filename(1, 2)
    victim.write_bytes(b"short")
    with pytest.raises(CatalogError, match="size"):
        MediaCatalog.load(media_dir)
    victim.unlink()
    with pytest.raises(CatalogError, match="missing"):
        MediaCatalog.load(media_dir)
