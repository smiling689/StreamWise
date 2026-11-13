"""Evaluation of the sliding-window transport (extension E-03).

Two experiments, each with a real server and real clients on loopback, in one process:

1. **Window size vs. round-trip time.** Download a few segments with different window sizes
   under emulated round-trip delays. The measured goodput is compared with the sliding-window
   model (:func:`segment_time`), and with one request per segment.
2. **Long stream with connection failures.** Play a long video in real time while the server
   aborts every connection a fixed time after it opens. The segment transport is compared
   with the window transport over one and two connections.
"""

from __future__ import annotations

import asyncio
import csv
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from streamwise.abr import create_abr
from streamwise.client.connection import StreamClient
from streamwise.client.session import ClientConfig, SessionResult, StreamingSession
from streamwise.client.window import SlidingWindowFetcher, WindowConfig
from streamwise.common.manifest import Manifest
from streamwise.server.app import ServerConfig, StreamServer
from streamwise.server.catalog import MediaCatalog
from streamwise.server.shaping import BandwidthTrace, ShapingConfig
from streamwise.server.txlog import TransmissionLog

RTT_MS = (0, 25, 50, 100)
WINDOWS = (1, 2, 4, 8, 16, 32)
CHUNK = 64 * 1024
BENCH_BURST = 16 * 1024
"""Token bucket of the emulated link. Smaller than the server default (32 KiB), so that
little of a chunk leaves as an idle burst; large enough not to lose tokens to timer jitter."""


class _Server:
    def __init__(self, media_dir: Path, db: Path, config: ServerConfig) -> None:
        self.txlog = TransmissionLog(db)
        self.server = StreamServer(config, MediaCatalog.load(media_dir), self.txlog)

    async def __aenter__(self) -> StreamServer:
        await self.server.start()
        return self.server

    async def __aexit__(self, *exc: object) -> None:
        await self.server.close(grace=2.0)
        self.txlog.close()


# -- 1. window size vs. RTT -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RttPoint:
    rtt_ms: float
    transport: str  # "window" or "segment"
    window: int | None
    goodput_mbps: float
    model_mbps: float


def segment_time(size: int, rate_mbps: float, rtt_s: float, window: int, chunk: int) -> float:
    """Model of the time to fetch one segment of ``size`` bytes.

    The first chunk arrives after ``RTT + L/C``. After that, a new chunk can be requested
    whenever one arrives, so a chunk completes every ``max(L/C, (RTT + L/C) / W)``: the link
    is the bottleneck once the window covers the bandwidth-delay product. The window starts
    empty at every segment, so each segment pays one RTT. With ``window=1`` this is
    stop-and-wait; with one chunk as large as the segment it is the per-segment transport.
    """
    rate = rate_mbps * 1e6 / 8
    chunks = -(-size // chunk)
    per_chunk = chunk / rate
    return rtt_s + size / rate + (chunks - 1) * max(0.0, (rtt_s + per_chunk) / window - per_chunk)


async def _goodput(
    media_dir: Path,
    tmp: Path,
    manifest: Manifest,
    level: int,
    segments: int,
    rate_mbps: float,
    rtt_s: float,
    window: int | None,
) -> float:
    tag = f"{int(rtt_s * 1000)}-{window}"
    config = ServerConfig(
        host="127.0.0.1",
        port=0,
        shaping=ShapingConfig(BandwidthTrace.constant(rate_mbps), "client", BENCH_BURST),
        delay_s=rtt_s,
    )
    async with _Server(media_dir, tmp / f"rtt-{tag}.db", config) as server:

        async def connect() -> StreamClient:
            return await StreamClient.connect("127.0.0.1", server.port, f"bench-{tag}")

        names = [manifest.segment_filename(level, s) for s in range(segments)]
        sizes = [manifest.segment_size(level, s) for s in range(segments)]
        if window is None:
            client = await connect()
            started = time.monotonic()
            for name in names:
                await client.fetch_segment(name)
            elapsed = time.monotonic() - started
            await client.close()
        else:
            fetcher = SlidingWindowFetcher(connect, WindowConfig(window, CHUNK, 1))
            await fetcher.start()
            await fetcher.fetch_segment(names[0], sizes[0])  # warm-up: connection and burst
            started = time.monotonic()
            for name, size in zip(names, sizes, strict=True):
                await fetcher.fetch_segment(name, size)
            elapsed = time.monotonic() - started
            await fetcher.aclose()
    return sum(sizes) * 8 / elapsed / 1e6


async def rtt_experiment(
    media_dir: Path, video: str, rate_mbps: float = 20.0, level: int = 3, segments: int = 3
) -> list[RttPoint]:
    manifest = Manifest.from_json((media_dir / video / "manifest.json").read_text())
    points: list[RttPoint] = []
    with tempfile.TemporaryDirectory() as tmp:
        for rtt_ms in RTT_MS:
            rtt = rtt_ms / 1000
            configs: list[int | None] = [*WINDOWS, None]
            # One configuration at a time: concurrent servers would add timer jitter.
            results = [
                await _goodput(media_dir, Path(tmp), manifest, level, segments, rate_mbps, rtt, w)
                for w in configs
            ]
            sizes = [manifest.segment_size(level, s) for s in range(segments)]
            for w, goodput in zip(configs, results, strict=True):
                total = sum(
                    segment_time(size, rate_mbps, rtt, w or 1, CHUNK if w else size)
                    for size in sizes
                )
                model = sum(sizes) * 8 / total / 1e6
                points.append(RttPoint(rtt_ms, "window" if w else "segment", w, goodput, model))
            row = "  ".join(f"{p.goodput_mbps:5.1f}" for p in points[-len(configs) :])
            print(f"  RTT {rtt_ms:>3} ms: {row}", flush=True)
    return points


# -- 2. long stream with connection failures -------------------------------------------


@dataclass(frozen=True, slots=True)
class FaultCase:
    name: str
    transport: str
    connections: int
    reset_after_s: float | None


@dataclass(slots=True)
class FaultRun:
    case: FaultCase
    result: SessionResult
    sent_bytes: int = 0
    """Payload bytes the server handed to TCP, including partial and lost transfers."""

    @property
    def delivered_bytes(self) -> int:
        return sum(r.bytes for r in self.result.segments)

    @property
    def extra_bytes(self) -> int:
        """Bytes sent but not part of a delivered segment (lost to connection resets)."""
        return max(0, self.sent_bytes - self.delivered_bytes)


def fault_cases(resets: tuple[float | None, ...]) -> list[FaultCase]:
    cases = []
    for reset in resets:
        tag = "none" if reset is None else f"{reset:g}s"
        cases += [
            FaultCase(f"segment/reset-{tag}", "segment", 1, reset),
            FaultCase(f"window-1conn/reset-{tag}", "window", 1, reset),
            FaultCase(f"window-2conn/reset-{tag}", "window", 2, reset),
        ]
    return cases


async def _fault_session(
    media_dir: Path, out: Path, video: str, case: FaultCase, trace: BandwidthTrace, abr: str
) -> FaultRun:
    slug = case.name.replace("/", "_")
    server_db = out / f"server-{slug}.db"
    config = ServerConfig(
        host="127.0.0.1",
        port=0,
        shaping=ShapingConfig(trace, "client"),
        reset_after_s=case.reset_after_s,
    )
    async with _Server(media_dir, server_db, config) as server:
        client_config = ClientConfig(
            host="127.0.0.1",
            port=server.port,
            video=video,
            name=slug,
            download_dir=out / "download",
            log_db=out / "client.db",
            transport=case.transport,
            connections=case.connections,
        )
        session = StreamingSession(client_config, create_abr(abr))
        result = await session.run()
    m = result.metrics
    print(
        f"  {case.name:<26} {result.status:<10} "
        + (f"QoE/seg {m.qoe_per_segment:6.3f} stalls {m.stall_count}" if m else "")
        + (f" reconnects {result.transport.reconnects}" if result.transport else ""),
        flush=True,
    )
    with sqlite3.connect(server_db) as conn:
        (sent,) = conn.execute("SELECT coalesce(sum(bytes), 0) FROM transmissions").fetchone()
    return FaultRun(case, result, int(sent))


async def fault_experiment(
    media_dir: Path,
    out: Path,
    video: str,
    rate_mbps: float,
    resets: tuple[float | None, ...],
    abr: str = "buffer",
) -> list[FaultRun]:
    trace = BandwidthTrace.constant(rate_mbps)
    cases = fault_cases(resets)
    return list(
        await asyncio.gather(*(_fault_session(media_dir, out, video, c, trace, abr) for c in cases))
    )


# -- report ----------------------------------------------------------------------------


def _plot_rtt(points: list[RttPoint], rate_mbps: float, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from streamwise.experiments.report import INK_2, SERIES_COLORS, TRACE_COLOR, _style

    _style()
    fig, axes = plt.subplots(1, len(RTT_MS), figsize=(12, 3.6), sharey=True)
    for ax, rtt in zip(axes, RTT_MS, strict=True):
        pts = [p for p in points if p.rtt_ms == rtt and p.transport == "window"]
        seg = next(p for p in points if p.rtt_ms == rtt and p.transport == "segment")
        ws = [p.window for p in pts]
        ax.plot(
            ws,
            [p.model_mbps for p in pts],
            color=TRACE_COLOR,
            linestyle="--",
            linewidth=1.5,
            label="model (window restarts at each segment)",
        )
        ax.plot(
            ws,
            [p.goodput_mbps for p in pts],
            color=SERIES_COLORS[0],
            linewidth=2,
            marker="o",
            markersize=6,
            markeredgecolor="#fcfcfb",
            label="sliding window, 64 KiB chunks (measured)",
        )
        ax.axhline(
            seg.goodput_mbps,
            color=SERIES_COLORS[1],
            linewidth=2,
            label="one request per segment (measured)",
        )
        ax.set_xscale("log", base=2)
        ax.set_xticks(ws, [str(w) for w in ws])
        ax.set_ylim(0, rate_mbps * 1.1)
        ax.set_title(f"RTT {rtt} ms", loc="left")
        ax.set_xlabel("window W (chunks)")
        ax.grid(True, axis="y")
    axes[0].set_ylabel("goodput (Mbit/s)")
    axes[0].text(
        1, rate_mbps * 1.02, f"link {rate_mbps:g} Mbit/s", color=INK_2, fontsize=8, va="bottom"
    )
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(path)
    plt.close(fig)


def _plot_faults(runs: list[FaultRun], reset: float, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from streamwise.experiments.report import INK_2, SERIES_COLORS, _style

    _style()
    fig, ax = plt.subplots(figsize=(11, 3.8))
    selected = [r for r in runs if r.case.reset_after_s == reset]
    for color, run in zip(SERIES_COLORS, selected, strict=False):
        segs = run.result.segments
        xs = [s.done_t for s in segs]
        ys = [s.buffer_after_s for s in segs]
        label = run.case.name.split("/")[0]
        if run.result.status != "completed":
            label += f" (failed after {len(segs)} segments)"
        ax.plot(xs, ys, color=color, linewidth=2, label=label)
        if run.result.status != "completed" and xs:
            ax.plot(xs[-1], ys[-1], marker="X", markersize=10, color=color,
                    markeredgecolor="#fcfcfb")  # fmt: skip
            ax.annotate(
                f"{label.split(' (')[0]} transport: the session fails at the first reset "
                f"(last segment at t = {xs[-1]:.0f} s)",
                xy=(xs[-1], ys[-1]),
                xytext=(0.06, 0.25),
                textcoords="axes fraction",
                color=INK_2,
                fontsize=8,
                arrowprops={"arrowstyle": "-", "color": INK_2, "linewidth": 0.8},
            )
    ax.set_title(
        f"Buffer level while the server aborts every connection after {reset:g} s",
        loc="left",
    )
    ax.set_xlabel("session time (s)")
    ax.set_ylabel("buffer after each download (s)")
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y")
    ax.legend(loc="lower right", frameon=False, labelcolor=INK_2)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def write_report(
    out: Path,
    rate_mbps: float,
    points: list[RttPoint],
    runs: list[FaultRun],
    video: str,
    fault_rate_mbps: float,
) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    lines = ["# Sliding-window transport evaluation\n"]
    if points:
        with (out / "rtt.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["rtt_ms", "transport", "window", "goodput_mbps", "model_mbps"])
            for p in points:
                w.writerow([p.rtt_ms, p.transport, p.window, p.goodput_mbps, p.model_mbps])
        _plot_rtt(points, rate_mbps, out / "window-rtt.png")
        lines += [
            f"## Goodput vs. window size (link {rate_mbps:g} Mbit/s, 64 KiB chunks)\n",
            "| RTT | " + " | ".join(f"W={w}" for w in WINDOWS) + " | per segment |",
            "|---|" + "---|" * (len(WINDOWS) + 1),
        ]
        for rtt in RTT_MS:
            row = [p for p in points if p.rtt_ms == rtt]
            cells = [f"{p.goodput_mbps:.1f} ({p.model_mbps:.1f})" for p in row]
            lines.append(f"| {rtt} ms | " + " | ".join(cells) + " |")
        lines.append("\nMeasured goodput in Mbit/s; the model value in parentheses.\n")
    if runs:
        lines += [
            f"## {video} streamed in real time at {fault_rate_mbps:g} Mbit/s with connection "
            "resets\n",
            "| Case | Status | Segments | Avg bitrate (kbps) | Stalls | Stall time (s) "
            "| QoE/seg | Connection failures | Reconnects | Re-requested (MB) "
            "| Lost in transit (MB) |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for run in runs:
            m, t = run.result.metrics, run.result.transport
            lines.append(
                f"| {run.case.name} | {run.result.status} | {len(run.result.segments)} | "
                + (
                    f"{m.avg_bitrate_kbps:.0f} | {m.stall_count} | {m.stall_time_s:.2f} | "
                    f"{m.qoe_per_segment:.3f} | "
                    if m
                    else "– | – | – | – | "
                )
                + (
                    f"{t.connection_failures} | {t.reconnects} | "
                    f"{t.retransmitted_bytes / 1e6:.2f} | "
                    if t
                    else "– | – | – | "
                )
                + f"{run.extra_bytes / 1e6:.2f} "
                f"({100 * run.extra_bytes / max(1, run.sent_bytes):.2f} %) |"
            )
        lines.append(
            "\nRe-requested: chunks that were in flight on a failed connection and requested "
            "again. Lost in transit: bytes the server sent that did not become part of a "
            "delivered segment (share of all bytes sent).\n"
        )
        failing = sorted({r.case.reset_after_s for r in runs if r.case.reset_after_s})
        for reset in failing:
            _plot_faults(runs, reset, out / f"window-faults-{reset:g}s.png")
    path = out / "summary.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def run(
    media_dir: Path,
    out: Path,
    rtt_video: str,
    long_video: str,
    rate_mbps: float,
    fault_rate_mbps: float,
    resets: tuple[float | None, ...],
    skip_rtt: bool = False,
    skip_faults: bool = False,
) -> Path:
    if out.exists():
        raise FileExistsError(f"{out} already exists; choose a new output directory")
    out.mkdir(parents=True)
    points: list[RttPoint] = []
    runs: list[FaultRun] = []
    if not skip_rtt:
        print("window size vs. RTT ...", flush=True)
        points = asyncio.run(rtt_experiment(media_dir, rtt_video, rate_mbps))
    if not skip_faults:
        print(f"long stream with connection resets ({long_video}, real time) ...", flush=True)
        runs = asyncio.run(fault_experiment(media_dir, out, long_video, fault_rate_mbps, resets))
    return write_report(out, rate_mbps, points, runs, long_video, fault_rate_mbps)
