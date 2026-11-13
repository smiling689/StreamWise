"""Objective quality (PSNR) of every segment of every level against the source.

Each level is decoded as one continuous stream (its segments concatenated), upscaled to the
display resolution (the resolution of the highest level) and compared frame by frame with the
same span of the source, resampled to the same frame rate. Per-frame MSE values are then
grouped into segments. A segment's PSNR is computed from its mean MSE (not the mean of
per-frame PSNRs), which is the conventional way to aggregate PSNR over a sequence.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from streamwise.common.manifest import Manifest
from streamwise.media.ffmpeg import FfmpegError, require_tool, run
from streamwise.media.segment import SOURCE_FILE

QUALITY_FILE = "quality.json"
MAX_PSNR = 100.0
PEAK = 255.0

_STATS_RE = re.compile(r"\bn:(\d+)\s+mse_avg:([0-9.]+|inf)")


def mse_to_psnr(mse: float) -> float:
    if mse <= 0:
        return MAX_PSNR
    return min(MAX_PSNR, 10.0 * math.log10(PEAK * PEAK / mse))


@dataclass(frozen=True, slots=True)
class LevelQuality:
    index: int
    label: str
    segment_mse: tuple[float, ...]

    @property
    def segment_psnr(self) -> tuple[float, ...]:
        return tuple(mse_to_psnr(m) for m in self.segment_mse)


@dataclass(frozen=True, slots=True)
class QualityTable:
    video: str
    reference_width: int
    reference_height: int
    levels: tuple[LevelQuality, ...]

    def psnr(self, level: int, seq: int) -> float:
        return mse_to_psnr(self.levels[level].segment_mse[seq])

    def mse(self, level: int, seq: int) -> float:
        return self.levels[level].segment_mse[seq]

    def to_dict(self) -> dict[str, Any]:
        return {
            "video": self.video,
            "metric": "psnr",
            "reference_resolution": [self.reference_width, self.reference_height],
            "levels": [
                {
                    "index": lv.index,
                    "label": lv.label,
                    # Two common aggregates: PSNR of the mean MSE (dominated by the worst
                    # segments) and the mean of per-segment PSNRs (used for session PSNR).
                    "psnr_of_mean_mse": mse_to_psnr(sum(lv.segment_mse) / len(lv.segment_mse)),
                    "mean_segment_psnr": sum(lv.segment_psnr) / len(lv.segment_psnr),
                    "segment_mse": list(lv.segment_mse),
                    "segment_psnr": [round(p, 4) for p in lv.segment_psnr],
                }
                for lv in self.levels
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityTable:
        width, height = data["reference_resolution"]
        return cls(
            video=data["video"],
            reference_width=int(width),
            reference_height=int(height),
            levels=tuple(
                LevelQuality(int(lv["index"]), lv["label"], tuple(lv["segment_mse"]))
                for lv in data["levels"]
            ),
        )

    @classmethod
    def load(cls, path: Path) -> QualityTable:
        return cls.from_dict(json.loads(path.read_text()))


def parse_psnr_stats(text: str) -> list[float]:
    """Return per-frame ``mse_avg`` values (in frame order) from a psnr filter stats file."""
    frames: dict[int, float] = {}
    for line in text.splitlines():
        match = _STATS_RE.search(line)
        if match:
            frames[int(match[1])] = float(match[2])
    if not frames:
        raise FfmpegError("psnr stats file is empty")
    if sorted(frames) != list(range(1, len(frames) + 1)):
        raise FfmpegError("psnr stats file has missing frames")
    return [frames[n] for n in range(1, len(frames) + 1)]


def measure_level(
    video_dir: Path, manifest: Manifest, source: dict[str, Any], level: int, ref_w: int, ref_h: int
) -> LevelQuality:
    lv = manifest.levels[level]
    fps = Fraction(source["frame_rate"])
    segments = [
        str(video_dir / manifest.segment_filename(level, s))
        for s in range(len(manifest.segment_durations))
    ]
    with tempfile.TemporaryDirectory() as tmp:
        stats = Path(tmp) / "psnr.log"
        graph = (
            f"[0:v]setpts=PTS-STARTPTS,scale={ref_w}:{ref_h}:flags=bicubic,format=yuv420p[dist];"
            f"[1:v]fps={fps},scale={ref_w}:{ref_h}:flags=lanczos,setsar=1,format=yuv420p,"
            f"setpts=PTS-STARTPTS[ref];"
            f"[dist][ref]psnr=stats_file={stats}"
        )
        run(
            [
                require_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", "concat:" + "|".join(segments),
                "-ss", f"{source['start']:.3f}", "-t", f"{source['duration']:.3f}",
                "-i", source["path"],
                "-lavfi", graph, "-f", "null", "-",
            ]
        )  # fmt: skip
        frame_mse = parse_psnr_stats(stats.read_text())

    frames_per_segment = [round(d * fps) for d in manifest.segment_durations]
    if sum(frames_per_segment) != len(frame_mse):
        raise FfmpegError(
            f"level {lv.label}: expected {sum(frames_per_segment)} frames, got {len(frame_mse)}"
        )
    segment_mse = []
    pos = 0
    for count in frames_per_segment:
        chunk = frame_mse[pos : pos + count]
        segment_mse.append(sum(chunk) / len(chunk))
        pos += count
    return LevelQuality(level, lv.label, tuple(segment_mse))


def measure_quality(video_dir: Path, jobs: int = 0) -> QualityTable:
    manifest = Manifest.from_json((video_dir / "manifest.json").read_text())
    source = json.loads((video_dir / SOURCE_FILE).read_text())
    top = manifest.levels[-1]
    ref_w, ref_h = top.width, top.height
    with ThreadPoolExecutor(max_workers=jobs or len(manifest.levels)) as pool:
        futures = [
            pool.submit(measure_level, video_dir, manifest, source, i, ref_w, ref_h)
            for i in range(len(manifest.levels))
        ]
        levels = tuple(f.result() for f in futures)
    table = QualityTable(manifest.video, ref_w, ref_h, levels)
    (video_dir / QUALITY_FILE).write_text(json.dumps(table.to_dict(), indent=2) + "\n")
    return table
