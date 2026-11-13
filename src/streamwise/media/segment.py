"""Multi-bitrate transcoding and fixed-duration MPEG-TS segmentation.

For every rung of the bitrate ladder the source is transcoded with H.264/AAC and cut into
segments of ``segment_duration`` seconds. A key frame is forced at every segment boundary and
scene-cut key frames are disabled, so every segment starts with an IDR frame and boundaries
are aligned across all levels: a client can switch level at any segment boundary and still
produce a decodable, continuous stream. Timestamps are not reset between segments, so
segments of any level can be concatenated in order and played as one stream.
"""

from __future__ import annotations

import json
import math
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from streamwise.common.manifest import Level, Manifest
from streamwise.common.naming import segment_filename, validate_video_name
from streamwise.media.ffmpeg import FfmpegError, SourceInfo, probe, require_tool, run

AUDIO_BITRATE_KBPS = 128
SOURCE_FILE = "source.json"


@dataclass(frozen=True, slots=True)
class Rung:
    height: int
    bitrate_kbps: int


DEFAULT_LADDER: tuple[Rung, ...] = (
    Rung(360, 800),
    Rung(480, 1200),
    Rung(720, 2500),
    Rung(1080, 5000),
    Rung(1080, 8000),
)


def parse_ladder(text: str) -> tuple[Rung, ...]:
    """Parse ``"360:800,720:2500"`` (height:kbps pairs) into a ladder sorted by bitrate."""
    rungs = []
    for item in text.split(","):
        height, _, kbps = item.strip().partition(":")
        if not height or not kbps:
            raise ValueError(f"bad ladder entry {item!r}, expected HEIGHT:KBPS")
        rungs.append(Rung(int(height), int(kbps)))
    return tuple(sorted(rungs, key=lambda r: r.bitrate_kbps))


@dataclass(frozen=True, slots=True)
class SegmentOptions:
    video: str
    segment_duration: float = 5.0
    start: float = 0.0
    duration: float | None = None
    """Seconds of source to use; ``None`` means until the end. Rounded down to whole segments."""
    frame_rate: Fraction | None = None
    """Output frame rate; ``None`` keeps the source rate."""
    ladder: tuple[Rung, ...] = DEFAULT_LADDER
    preset: str = "fast"
    jobs: int = 0
    """Levels encoded in parallel; 0 means all at once."""


def scaled_width(source: SourceInfo, height: int) -> int:
    """Width that preserves the source aspect ratio, rounded to an even number."""
    return max(2, 2 * round(source.width * height / source.height / 2))


def plan_segment_count(source: SourceInfo, opts: SegmentOptions, fps: Fraction) -> int:
    available = source.duration - opts.start
    wanted = available if opts.duration is None else min(opts.duration, available)
    count = math.floor(wanted / opts.segment_duration + 1e-9)
    if count < 1:
        raise ValueError("source is shorter than one segment")
    frames = Fraction(opts.segment_duration).limit_denominator(1000) * fps
    if frames.denominator != 1:
        raise ValueError(
            f"segment duration {opts.segment_duration}s is not a whole number of frames "
            f"at {fps} fps"
        )
    return count


def video_filter(fps: Fraction, width: int, height: int) -> str:
    return f"fps={fps},scale={width}:{height}:flags=lanczos,setsar=1,format=yuv420p"


def frames_per_segment(opts: SegmentOptions, fps: Fraction) -> int:
    return int(Fraction(opts.segment_duration).limit_denominator(1000) * fps)


def inspect_segment(path: Path) -> tuple[int, bool]:
    """Return ``(video frame count, first video packet is a key frame)`` of a TS segment."""
    proc = run(
        [
            require_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=flags", "-of", "csv=p=0", str(path),
        ]
    )  # fmt: skip
    flags = [line for line in proc.stdout.splitlines() if line.strip()]
    return len(flags), bool(flags) and flags[0].startswith("K")


def encode_level(
    source: SourceInfo,
    opts: SegmentOptions,
    fps: Fraction,
    count: int,
    level_index: int,
    out_dir: Path,
) -> Level:
    """Encode one level and verify every segment has exactly one GOP starting with an IDR.

    Cut points are given as frame numbers (``-segment_frames``) rather than times: the MPEG-TS
    muxer offsets timestamps and audio may start slightly before video, which makes
    time-based cutting skip boundaries. Every cut frame is a forced key frame.
    """
    rung = opts.ladder[level_index]
    width = scaled_width(source, rung.height)
    gop = frames_per_segment(opts, fps)
    total = count * opts.segment_duration
    names = [segment_filename(opts.video, rung.height, rung.bitrate_kbps, s) for s in range(count)]
    pattern = names[0].removesuffix("-0.ts") + "-%d.ts"
    cut_frames = ",".join(str(gop * k) for k in range(1, count)) or str(gop)
    args = [
        require_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-ss", f"{opts.start:.3f}", "-t", f"{total:.3f}", "-i", str(source.path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", video_filter(fps, width, rung.height),
        "-frames:v", str(gop * count),
        "-c:v", "libx264", "-preset", opts.preset, "-profile:v", "high",
        "-b:v", f"{rung.bitrate_kbps}k",
        "-maxrate", f"{int(rung.bitrate_kbps * 1.1)}k",
        "-bufsize", f"{rung.bitrate_kbps * 2}k",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-force_key_frames", f"expr:eq(mod(n,{gop}),0)",
        "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k", "-ac", "2", "-ar", "48000",
        "-f", "segment", "-segment_format", "mpegts",
        # Each segment is muxed independently, so continuity counters restart; flag the
        # discontinuity so concatenated segments (also across levels) demux without errors.
        "-segment_format_options", "mpegts_flags=+initial_discontinuity",
        "-segment_frames", cut_frames,
        "-reset_timestamps", "0",
        str(out_dir / pattern),
    ]  # fmt: skip
    run(args)

    produced = sorted(p.name for p in out_dir.glob(pattern.replace("%d", "*")))
    if sorted(names) != produced:
        raise FfmpegError(f"level {rung}: expected {count} segments, got {len(produced)}")
    sizes = []
    for name in names:
        frames, starts_with_key = inspect_segment(out_dir / name)
        if frames != gop or not starts_with_key:
            raise FfmpegError(
                f"{name}: {frames} frames (expected {gop}), key frame first: {starts_with_key}"
            )
        sizes.append((out_dir / name).stat().st_size)
    return Level(level_index, width, rung.height, rung.bitrate_kbps, tuple(sizes))


def segment_video(source_path: Path, media_dir: Path, opts: SegmentOptions) -> Manifest:
    """Transcode and segment ``source_path`` into ``media_dir/<video>/`` and write the manifest.

    Output is built in a temporary directory and moved into place only on success, so an
    interrupted run never leaves a half-written video directory behind.
    """
    validate_video_name(opts.video)
    source = probe(source_path)
    fps = opts.frame_rate or source.frame_rate
    count = plan_segment_count(source, opts, fps)
    if any(r.height > source.height for r in opts.ladder):
        raise ValueError("ladder contains a resolution above the source resolution")

    final_dir = media_dir / opts.video
    work_dir = media_dir / f".{opts.video}.partial"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    try:
        jobs = opts.jobs or len(opts.ladder)
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [
                pool.submit(encode_level, source, opts, fps, count, i, work_dir)
                for i in range(len(opts.ladder))
            ]
            levels = tuple(f.result() for f in futures)

        segment_seconds = float(Fraction(frames_per_segment(opts, fps)) / fps)
        manifest = Manifest(
            video=opts.video,
            segment_duration=opts.segment_duration,
            segment_durations=(segment_seconds,) * count,
            levels=levels,
        )
        (work_dir / "manifest.json").write_text(manifest.to_json() + "\n")
        source_meta = {
            "path": str(source_path.resolve()),
            "start": opts.start,
            "duration": count * segment_seconds,
            "frame_rate": str(fps),
            "width": source.width,
            "height": source.height,
        }
        (work_dir / SOURCE_FILE).write_text(json.dumps(source_meta, indent=2) + "\n")
        shutil.rmtree(final_dir, ignore_errors=True)
        work_dir.rename(final_dir)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return manifest
