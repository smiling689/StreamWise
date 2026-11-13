"""Cost of the AES-256-GCM encryption extension: CPU, wire bytes and unshaped throughput."""

from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from streamwise.client.connection import StreamClient
from streamwise.common import crypto
from streamwise.common.manifest import Manifest
from streamwise.common.protocol import MessageType
from streamwise.server.app import ServerConfig, StreamServer
from streamwise.server.catalog import MediaCatalog
from streamwise.server.txlog import TransmissionLog


def _channel_pair() -> tuple[crypto.SecureChannel, crypto.SecureChannel]:
    """(client channel, server channel) from a full handshake."""
    client = crypto.ClientHandshake()
    hello = json.dumps({"client": "bench", "encryption": client.offer()}).encode()
    welcome, server = crypto.server_accept(hello, client.offer(), {})
    return client.finish(hello, json.loads(json.dumps(welcome))), server


def _best(fn, repeats: int) -> float:
    """Fastest of ``repeats`` runs (seconds); the minimum is the least noisy estimate."""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def handshake_cost(n: int = 500) -> dict[str, float]:
    times = []
    for _ in range(n):
        start = time.perf_counter()
        client = crypto.ClientHandshake(b"k" * 32)
        hello = json.dumps({"client": "bench", "encryption": client.offer()}).encode()
        welcome, _ = crypto.server_accept(hello, client.offer(), {"session_id": "x"}, b"k" * 32)
        client.finish(hello, json.loads(json.dumps(welcome)))
        times.append(time.perf_counter() - start)
    plain_hello = len(json.dumps({"client": "bench"}).encode())
    extra_hello = len(hello) - plain_hello
    extra_welcome = len(json.dumps(welcome["encryption"])) + len(',"encryption":')
    return {
        "handshake_us": statistics.median(times) * 1e6,
        "extra_handshake_bytes": extra_hello + extra_welcome,
    }


def cipher_cost(manifest: Manifest, video_dir: Path, repeats: int = 5) -> list[dict[str, float]]:
    """Seal and open every segment of each level once (best of ``repeats``)."""
    _, server = _channel_pair()
    rows = []
    for level in manifest.levels:
        data = [
            (video_dir / manifest.segment_filename(level.index, s)).read_bytes()
            for s in range(manifest.segment_count)
        ]
        total = sum(map(len, data))
        sealed: list[bytes] = []

        def seal(data=data, sealed=sealed) -> None:
            sealed.clear()
            sealed.extend(server.seal(MessageType.SEGMENT, 1, d) for d in data)

        seal_s = _best(seal, repeats)

        # Opening needs matching counters: re-key a fresh pair for each repetition.
        def open_all(data=data) -> float:
            ch, srv = _channel_pair()
            boxes = [srv.seal(MessageType.SEGMENT, 1, d) for d in data]
            start = time.perf_counter()
            for b in boxes:
                ch.open(MessageType.SEGMENT, 1, b)
            return time.perf_counter() - start

        open_s = min(open_all() for _ in range(repeats))
        rows.append(
            {
                "level": level.index,
                "label": level.label,
                "mean_segment_bytes": total / len(data),
                "seal_ms_per_segment": seal_s / len(data) * 1e3,
                "open_ms_per_segment": open_s / len(data) * 1e3,
                "seal_MBps": total / seal_s / 1e6,
                "open_MBps": total / open_s / 1e6,
                "tag_overhead_pct": 100 * crypto.TAG_SIZE / (total / len(data)),
            }
        )
    return rows


async def loopback_throughput(
    media_dir: Path, video: str, level: int, rounds: int = 3
) -> dict[str, float]:
    """Download every segment of one level over unshaped loopback, plain vs. encrypted."""
    with tempfile.TemporaryDirectory() as tmp:
        txlog = TransmissionLog(Path(tmp) / "bench.db")
        server = StreamServer(
            ServerConfig(host="127.0.0.1", port=0, encryption="optional"),
            MediaCatalog.load(media_dir),
            txlog,
        )
        await server.start()
        try:
            result: dict[str, float] = {}
            for encrypt in (False, True, False, True) * rounds:
                client = await StreamClient.connect(
                    "127.0.0.1", server.port, "bench", encrypt=encrypt
                )
                manifest = await client.get_manifest(video)
                total = 0
                start = time.perf_counter()
                for seq in range(manifest.segment_count):
                    fetched = await client.fetch_segment(manifest.segment_filename(level, seq))
                    total += len(fetched.data)
                elapsed = time.perf_counter() - start
                await client.close()
                key = "encrypted_MBps" if encrypt else "plaintext_MBps"
                result[key] = max(result.get(key, 0.0), total / elapsed / 1e6)
            return result
        finally:
            await server.close(grace=1.0)
            txlog.close()


def run(media_dir: Path, video: str) -> str:
    video_dir = media_dir / video
    manifest = Manifest.from_json((video_dir / "manifest.json").read_text())
    hs = handshake_cost()
    rows = cipher_cost(manifest, video_dir)
    top = len(manifest.levels) - 1
    loop = asyncio.run(loopback_throughput(media_dir, video, top))
    lines = [
        "# Encryption cost (AES-256-GCM)\n",
        f"Handshake (X25519 + HKDF + HMAC, both sides): {hs['handshake_us']:.0f} µs; "
        f"{hs['extra_handshake_bytes']:.0f} extra bytes in HELLO/WELCOME; "
        f"{crypto.TAG_SIZE} bytes per message afterwards.\n",
        "| Level | Mean segment | Encrypt / segment | Decrypt / segment | Encrypt | Decrypt "
        "| Tag overhead |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['label']} | {r['mean_segment_bytes'] / 1e6:.2f} MB | "
            f"{r['seal_ms_per_segment']:.2f} ms | {r['open_ms_per_segment']:.2f} ms | "
            f"{r['seal_MBps']:.0f} MB/s | {r['open_MBps']:.0f} MB/s | "
            f"{r['tag_overhead_pct']:.4f} % |"
        )
    slowdown = 100 * (1 - loop["encrypted_MBps"] / loop["plaintext_MBps"])
    lines += [
        "",
        f"Unshaped loopback download of all {manifest.segment_count} segments of "
        f"{manifest.levels[top].label} (best of 3): plaintext {loop['plaintext_MBps']:.0f} MB/s "
        f"({loop['plaintext_MBps'] * 8:.0f} Mbit/s), encrypted {loop['encrypted_MBps']:.0f} MB/s "
        f"({loop['encrypted_MBps'] * 8:.0f} Mbit/s), {slowdown:.1f} % lower.",
    ]
    return "\n".join(lines) + "\n"
