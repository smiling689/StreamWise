"""Command-line interface: ``streamwise-server``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from streamwise.common.crypto import load_psk
from streamwise.server.app import ServerConfig, run_server
from streamwise.server.catalog import CatalogError
from streamwise.server.shaping import BandwidthTrace, ShapingConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streamwise-server", description="Serve segmented video over TCP."
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=9000, help="TCP port (default: %(default)s)")
    parser.add_argument("--media-dir", type=Path, default=Path("media"), help="segmented videos")
    parser.add_argument(
        "--log-db", type=Path, default=Path("logs/server.db"), help="SQLite transmission log"
    )
    rate = parser.add_mutually_exclusive_group()
    rate.add_argument("--rate-mbps", type=float, help="constant bandwidth limit in Mbit/s")
    rate.add_argument("--trace", type=Path, help="bandwidth trace file ('<time_s> <mbps>' lines)")
    parser.add_argument(
        "--shaping-scope",
        choices=["connection", "client", "global"],
        default="connection",
        help="'connection': independent limit per connection, trace clock starts at connect; "
        "'client': one limit per client name, shared by its connections; "
        "'global': one shared limit, trace clock starts at server start (default: %(default)s)",
    )
    parser.add_argument(
        "--burst-kb", type=int, default=32, help="token bucket size in KiB (default: %(default)s)"
    )
    parser.add_argument(
        "--encryption",
        choices=["off", "optional", "required"],
        default="optional",
        help="AES-256-GCM: 'optional' encrypts when the client asks, 'required' rejects "
        "plaintext clients (default: %(default)s)",
    )
    parser.add_argument(
        "--psk-file",
        type=Path,
        help="pre-shared key file mixed into the key derivation (authenticates clients)",
    )
    parser.add_argument(
        "--delay-ms",
        type=float,
        default=0.0,
        help="emulated round-trip delay added to every request (default: %(default)s)",
    )
    parser.add_argument(
        "--reset-after-s",
        type=float,
        help="fault injection: abort every connection this many seconds after it opens",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    trace = None
    if args.rate_mbps is not None:
        if args.rate_mbps <= 0:
            print("error: --rate-mbps must be positive", file=sys.stderr)
            return 2
        trace = BandwidthTrace.constant(args.rate_mbps)
    elif args.trace is not None:
        try:
            trace = BandwidthTrace.load(args.trace)
        except (OSError, ValueError) as exc:
            print(f"error: cannot load trace: {exc}", file=sys.stderr)
            return 2
    try:
        psk = load_psk(args.psk_file) if args.psk_file else None
        config = ServerConfig(
            host=args.host,
            port=args.port,
            shaping=ShapingConfig(trace, args.shaping_scope, args.burst_kb * 1024),
            encryption=args.encryption,
            psk=psk,
            delay_s=args.delay_ms / 1000,
            reset_after_s=args.reset_after_s,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    async def amain() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_server(config, args.media_dir, args.log_db, stop)

    try:
        asyncio.run(amain())
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
