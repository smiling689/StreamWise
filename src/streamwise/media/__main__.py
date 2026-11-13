"""Command-line interface: ``streamwise-media segment|quality``."""

from __future__ import annotations

import argparse
import sys
import time
from fractions import Fraction
from pathlib import Path

from streamwise.media.ffmpeg import FfmpegError
from streamwise.media.quality import QUALITY_FILE, measure_quality
from streamwise.media.segment import DEFAULT_LADDER, SegmentOptions, parse_ladder, segment_video


def _ladder_text() -> str:
    return ",".join(f"{r.height}:{r.bitrate_kbps}" for r in DEFAULT_LADDER)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamwise-media", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seg = sub.add_parser("segment", help="transcode a source video into segmented renditions")
    seg.add_argument("source", type=Path, help="source video file")
    seg.add_argument("--name", required=True, help="video name used in segment file names")
    seg.add_argument("--media-dir", type=Path, default=Path("media"), help="output root")
    seg.add_argument("--segment-duration", type=float, default=5.0, help="seconds per segment")
    seg.add_argument("--start", type=float, default=0.0, help="source offset in seconds")
    seg.add_argument("--duration", type=float, help="seconds of source to use (default: all)")
    seg.add_argument("--fps", type=Fraction, help="output frame rate (default: source rate)")
    seg.add_argument(
        "--ladder", default=_ladder_text(), help="HEIGHT:KBPS pairs (default: %(default)s)"
    )
    seg.add_argument("--preset", default="fast", help="x264 preset (default: %(default)s)")
    seg.add_argument("--jobs", type=int, default=0, help="parallel encodes (default: all)")
    seg.add_argument("--no-quality", action="store_true", help="skip PSNR measurement")

    qual = sub.add_parser("quality", help="measure per-segment PSNR of a segmented video")
    qual.add_argument("video_dir", type=Path, help="directory containing manifest.json")
    qual.add_argument("--jobs", type=int, default=0, help="parallel measurements")
    return parser


def _report_quality(video_dir: Path, jobs: int) -> None:
    started = time.monotonic()
    table = measure_quality(video_dir, jobs)
    print(f"PSNR written to {video_dir / QUALITY_FILE} ({time.monotonic() - started:.0f}s)")
    for level in table.levels:
        psnr = level.segment_psnr
        print(f"  {level.label:>14}: mean segment PSNR {sum(psnr) / len(psnr):6.2f} dB")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "segment":
            opts = SegmentOptions(
                video=args.name,
                segment_duration=args.segment_duration,
                start=args.start,
                duration=args.duration,
                frame_rate=args.fps,
                ladder=parse_ladder(args.ladder),
                preset=args.preset,
                jobs=args.jobs,
            )
            started = time.monotonic()
            manifest = segment_video(args.source, args.media_dir, opts)
            video_dir = args.media_dir / manifest.video
            print(
                f"{manifest.video}: {manifest.segment_count} segments x {len(manifest.levels)} "
                f"levels ({manifest.duration:.1f}s) in {video_dir} "
                f"({time.monotonic() - started:.0f}s)"
            )
            for level in manifest.levels:
                actual = sum(level.segment_sizes) * 8 / manifest.duration / 1000
                print(
                    f"  {level.label:>14}: {level.width}x{level.height}, {actual:.0f} kbps actual"
                )
            if not args.no_quality:
                _report_quality(video_dir, args.jobs)
        else:
            _report_quality(args.video_dir, args.jobs)
    except (FfmpegError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
