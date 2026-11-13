"""Command-line interface: ``streamwise-client``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from streamwise.abr import create_abr
from streamwise.client.player import FfplayPlayer, NullPlayer, Player
from streamwise.client.session import ClientConfig, StreamingSession


def _pair(text: str) -> tuple[int, int]:
    sep = "x" if "x" in text else ","
    a, _, b = text.partition(sep)
    try:
        return int(a), int(b)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected two integers like 640x360, got {text!r}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streamwise-client", description="Stream and play a segmented video over TCP."
    )
    parser.add_argument("--host", default="127.0.0.1", help="server address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=9000, help="server port (default: %(default)s)")
    parser.add_argument("--video", default="bbb", help="video name (default: %(default)s)")
    parser.add_argument("--name", default="client", help="client name shown in logs")
    parser.add_argument(
        "--abr",
        default="buffer",
        help="ABR spec: throughput | buffer | fixed:<level|min|max>, optionally with "
        "parameters, e.g. 'throughput:window=5,safety=0.9' (default: %(default)s)",
    )
    parser.add_argument("--max-buffer", type=float, default=30.0, help="buffer cap in seconds")
    parser.add_argument(
        "--startup-buffer", type=float, default=5.0, help="seconds buffered before playback starts"
    )
    parser.add_argument("--segments", type=int, help="only play the first N segments")
    parser.add_argument(
        "--download-dir", type=Path, default=Path("download"), help="segment cache root"
    )
    parser.add_argument(
        "--log-db", type=Path, default=Path("logs/client.db"), help="SQLite session log"
    )
    parser.add_argument("--no-log", action="store_true", help="do not write the session log")
    parser.add_argument(
        "--player",
        choices=["ffplay", "none"],
        default="ffplay",
        help="'none' runs headless (no window) (default: %(default)s)",
    )
    parser.add_argument("--mute", action="store_true", help="disable audio output")
    parser.add_argument(
        "--window-size", type=_pair, default=(640, 360), help="player window WxH (default: 640x360)"
    )
    parser.add_argument("--window-pos", type=_pair, help="player window position X,Y")
    parser.add_argument(
        "--encrypt", action="store_true", help="require an AES-256-GCM encrypted connection"
    )
    parser.add_argument(
        "--psk-file",
        type=Path,
        help="pre-shared key file that authenticates the server (implies --encrypt)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only log warnings and errors")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        abr = create_abr(args.abr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    config = ClientConfig(
        host=args.host,
        port=args.port,
        video=args.video,
        name=args.name,
        max_buffer_s=args.max_buffer,
        startup_buffer_s=args.startup_buffer,
        max_segments=args.segments,
        download_dir=args.download_dir,
        log_db=None if args.no_log else args.log_db,
        encrypt=args.encrypt or args.psk_file is not None,
        psk_file=args.psk_file,
    )
    player: Player
    if args.player == "ffplay":
        player = FfplayPlayer(
            f"StreamWise - {args.name}",
            mute=args.mute,
            window_size=args.window_size,
            window_pos=args.window_pos,
        )
    else:
        player = NullPlayer()
    session = StreamingSession(config, abr, player)

    async def amain() -> int:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            result = await session.run()
        except asyncio.CancelledError:
            print(f"[{args.name}] interrupted; cache cleaned up", file=sys.stderr)
            return 130
        m = result.metrics
        if m is not None:
            print(
                f"[{args.name}] {result.status}: {m.segments} segments, "
                f"avg bitrate {m.avg_bitrate_kbps:.0f} kbps, startup {m.startup_delay_s:.2f}s, "
                f"stalls {m.stall_count} ({m.stall_time_s:.2f}s, ratio {m.rebuffer_ratio:.2%}), "
                f"switches {m.switch_count}, QoE/seg {m.qoe_per_segment:.3f}"
            )
        return 0 if result.status == "completed" else 1

    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
