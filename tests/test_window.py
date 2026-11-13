"""Tests for the sliding-window transport (extension E-03)."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from streamwise.abr import create_abr
from streamwise.client.connection import ServerError, StreamClient
from streamwise.client.session import ClientConfig, StreamingSession
from streamwise.client.window import SlidingWindowFetcher, WindowConfig, _Reassembly
from streamwise.common import protocol as proto
from streamwise.common.protocol import ErrorCode, Message, MessageType, ProtocolError
from streamwise.server.app import ServerConfig, StreamServer
from streamwise.server.catalog import MediaCatalog
from streamwise.server.shaping import BandwidthTrace, ShapingConfig
from streamwise.server.txlog import TransmissionLog


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


def connector(port: int, name: str = "w", **kw):
    async def connect() -> StreamClient:
        return await StreamClient.connect("127.0.0.1", port, name, **kw)

    return connect


def segment(media_dir: Path, manifest, level: int, seq: int) -> tuple[str, bytes]:
    name = manifest.segment_filename(level, seq)
    return name, (media_dir / manifest.video / name).read_bytes()


def test_chunk_request_roundtrip():
    payload = proto.chunk_request("bbb-720p-2500k-3.ts", 2**40, 65536)
    assert proto.parse_chunk_request(payload) == ("bbb-720p-2500k-3.ts", 2**40, 65536)
    with pytest.raises(ProtocolError):
        proto.parse_chunk_request(b"\x00" * 12)
    with pytest.raises(ProtocolError):
        proto.parse_chunk_request(b"\x00" * 12 + b"\xff")


def test_reassembly_delivers_in_order():
    r = _Reassembly(chunks=5)
    for index in (2, 4, 0):
        r.accept(index, b"%d" % index)
    assert r.base == 1 and sorted(r.held) == [2, 4]
    r.accept(1, b"1")
    assert r.base == 3 and list(r.held) == [4]
    r.accept(2, b"x")  # duplicate of a delivered chunk is ignored
    r.accept(3, b"3")
    assert r.base == 5 and not r.held and b"".join(r.parts) == b"01234"


def test_window_config_validation():
    with pytest.raises(ValueError):
        WindowConfig(window=0)
    with pytest.raises(ValueError):
        WindowConfig(chunk_size=proto.MAX_CHUNK + 1)
    with pytest.raises(ValueError):
        WindowConfig(connections=0)
    with pytest.raises(ValueError):
        ClientConfig(transport="udp")


async def test_server_answers_pipelined_requests_in_order(fake_media, tmp_path: Path):
    media_dir, manifest = fake_media
    name, data = segment(media_dir, manifest, 1, 0)
    async with server_with(media_dir, tmp_path / "s.db") as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(proto.encode_message(proto.json_message(MessageType.HELLO, 1, {})))
        ranges = [(0, 100), (100, 50), (len(data) - 10, 10), (len(data) - 10, 11), (0, 1)]
        for i, (off, length) in enumerate(ranges, start=2):
            msg = Message(MessageType.GET_CHUNK, i, proto.chunk_request(name, off, length))
            writer.write(proto.encode_message(msg))
        writer.write(
            proto.encode_message(Message(MessageType.GET_CHUNK, 9, proto.chunk_request("x", 0, 1)))
        )
        await writer.drain()
        welcome = await proto.read_message(reader, proto.MAX_RESPONSE_PAYLOAD)
        assert welcome is not None and welcome.type is MessageType.WELCOME
        replies = [await proto.read_message(reader, proto.MAX_RESPONSE_PAYLOAD) for _ in range(6)]
        assert [r.request_id for r in replies] == [2, 3, 4, 5, 6, 9]
        assert replies[0].payload == data[:100] and replies[1].payload == data[100:150]
        assert replies[2].payload == data[-10:]
        assert replies[3].type is MessageType.ERROR  # past the end; connection stays usable
        assert replies[3].json()["code"] == ErrorCode.RANGE_NOT_SATISFIABLE
        assert replies[4].payload == data[:1]
        assert replies[5].json()["code"] == ErrorCode.NOT_FOUND
        writer.close()


async def test_concurrent_get_chunk_is_pipelined(fake_media, tmp_path: Path):
    media_dir, manifest = fake_media
    name, data = segment(media_dir, manifest, 2, 1)
    async with server_with(media_dir, tmp_path / "s.db", delay_s=0.05) as server:
        client = await StreamClient.connect("127.0.0.1", server.port, "p")
        step = len(data) // 10
        started = time.monotonic()
        parts = await asyncio.gather(*(client.get_chunk(name, i * step, step) for i in range(10)))
        elapsed = time.monotonic() - started
        assert b"".join(parts) == data[: 10 * step]
        assert elapsed < 0.3  # ten requests share one 50 ms delay instead of paying 0.5 s
        with pytest.raises(ServerError) as info:
            await client.get_chunk(name, 0, proto.MAX_CHUNK + 1)
        assert info.value.code == ErrorCode.RANGE_NOT_SATISFIABLE
        assert not client.broken
        await client.close()


@pytest.mark.parametrize(
    ("window", "chunk", "connections"), [(1, 4096, 1), (4, 1000, 1), (8, 3000, 3), (64, 65536, 2)]
)
async def test_fetcher_reassembles_segments(fake_media, tmp_path: Path, window, chunk, connections):
    media_dir, manifest = fake_media
    async with server_with(media_dir, tmp_path / "s.db") as server:
        fetcher = SlidingWindowFetcher(
            connector(server.port), WindowConfig(window, chunk, connections)
        )
        await fetcher.start()
        for level, seq in [(0, 0), (2, 1), (1, 5)]:
            name, data = segment(media_dir, manifest, level, seq)
            fetched = await fetcher.fetch_segment(name, len(data))
            assert fetched.data == data
        assert fetcher.stats.max_in_flight <= window
        assert fetcher.stats.max_reassembly_chunks < window
        assert fetcher.stats.retransmitted_chunks == 0
        await fetcher.aclose()


async def test_fetcher_propagates_server_errors(fake_media, tmp_path: Path):
    media_dir, manifest = fake_media
    async with server_with(media_dir, tmp_path / "s.db") as server:
        fetcher = SlidingWindowFetcher(connector(server.port), WindowConfig(4, 1000, 2))
        await fetcher.start()
        with pytest.raises(ServerError):
            await fetcher.fetch_segment("test-360p-800k-99.ts", 5000)
        name, data = segment(media_dir, manifest, 0, 0)  # the pool recovers afterwards
        assert (await fetcher.fetch_segment(name, len(data))).data == data
        await fetcher.aclose()


async def test_fetcher_survives_connection_resets(fake_media, tmp_path: Path):
    media_dir, manifest = fake_media
    trace = BandwidthTrace.constant(2.0)
    async with server_with(
        media_dir,
        tmp_path / "s.db",
        shaping=ShapingConfig(trace, "client", 8 * 1024),
        reset_after_s=0.15,
    ) as server:
        fetcher = SlidingWindowFetcher(connector(server.port), WindowConfig(8, 4096, 2))
        await fetcher.start()
        for seq in range(3):
            name, data = segment(media_dir, manifest, 2, seq)  # ~0.5 s each at 2 Mbit/s
            assert (await fetcher.fetch_segment(name, len(data))).data == data
        stats = fetcher.stats
        assert stats.connection_failures >= 2 and stats.reconnects >= 2
        assert stats.retransmitted_chunks > 0
        await fetcher.aclose()
    with sqlite3.connect(tmp_path / "s.db") as conn:
        reasons = {r for (r,) in conn.execute("SELECT close_reason FROM sessions")}
    assert "injected_reset" in reasons


async def test_window_limits_throughput_by_rtt(fake_media, tmp_path: Path):
    media_dir, manifest = fake_media
    name, data = segment(media_dir, manifest, 2, 0)  # ~125 kB
    elapsed = {}
    async with server_with(media_dir, tmp_path / "s.db", delay_s=0.02) as server:
        for window in (1, 8):
            fetcher = SlidingWindowFetcher(connector(server.port), WindowConfig(window, 16384))
            await fetcher.start()
            fetched = await fetcher.fetch_segment(name, len(data))
            assert fetched.data == data
            elapsed[window] = fetched.download_time
            await fetcher.aclose()
    chunks = -(-len(data) // 16384)
    assert elapsed[1] >= chunks * 0.02  # stop-and-wait: one RTT per chunk
    assert elapsed[8] < elapsed[1] / 3


async def test_client_scope_shares_rate_between_connections(fake_media, tmp_path: Path):
    media_dir, manifest = fake_media
    name, data = segment(media_dir, manifest, 2, 2)
    rate = 2.0
    async with server_with(
        media_dir, tmp_path / "s.db", shaping=ShapingConfig(BandwidthTrace.constant(rate), "client")
    ) as server:
        fetcher = SlidingWindowFetcher(connector(server.port, "one"), WindowConfig(16, 8192, 3))
        await fetcher.start()
        await asyncio.sleep(0.1)  # all connections up; the first burst is spent below
        await fetcher.fetch_segment(name, len(data))
        fetched = await fetcher.fetch_segment(name, len(data))
        await fetcher.aclose()
    assert fetched.throughput_bps / 1e6 == pytest.approx(rate, rel=0.15)


def session_config(tmp_path: Path, port: int, name: str, **kw) -> ClientConfig:
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


async def test_session_window_transport_rides_out_resets(fake_media, tmp_path: Path):
    media_dir, _ = fake_media
    trace = BandwidthTrace.constant(3.0)
    async with server_with(
        media_dir,
        tmp_path / "s.db",
        shaping=ShapingConfig(trace, "client", 8 * 1024),
        reset_after_s=0.3,
        encryption="required",
    ) as server:
        window = session_config(
            tmp_path,
            server.port,
            "win",
            transport="window",
            window=8,
            chunk_kb=4,
            connections=2,
            encrypt=True,
        )
        segment_cfg = session_config(tmp_path, server.port, "seg", encrypt=True)
        win, seg = await asyncio.gather(
            StreamingSession(window, create_abr("fixed:max")).run(),
            StreamingSession(segment_cfg, create_abr("fixed:max")).run(),
        )
    assert win.status == "completed", win.error
    assert win.transport is not None and win.transport.reconnects >= 1
    assert seg.status == "failed"  # one request per segment on one connection: no recovery
    with sqlite3.connect(tmp_path / "client.db") as conn:
        row = conn.execute(
            "SELECT status, reconnects, retransmitted_chunks FROM sessions WHERE client_name='win'"
        ).fetchone()
    assert row[0] == "completed" and row[1] == win.transport.reconnects


def test_segment_time_model_limits():
    from streamwise.experiments.windowbench import segment_time

    rate_mbps, rtt, chunk = 8.0, 0.1, 10_000
    per_chunk = chunk * 8 / 8e6
    # Stop-and-wait: one RTT plus one transmission per chunk.
    assert segment_time(50_000, rate_mbps, rtt, 1, chunk) == pytest.approx(5 * (rtt + per_chunk))
    # A window that covers the bandwidth-delay product: one RTT, then the link is saturated.
    assert segment_time(50_000, rate_mbps, rtt, 64, chunk) == pytest.approx(rtt + 0.05)
    # One request per segment is a single chunk as large as the segment.
    assert segment_time(50_000, rate_mbps, rtt, 1, 50_000) == pytest.approx(rtt + 0.05)
