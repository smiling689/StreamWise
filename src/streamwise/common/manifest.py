"""Video manifest: the bitrate ladder and per-segment metadata a client needs.

The manifest is produced by the segmentation tool, stored as ``manifest.json`` next to the
segments, and sent verbatim to clients in a ``MANIFEST`` message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from streamwise.common.naming import segment_filename, validate_video_name

MANIFEST_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class Level:
    """One rendition of the bitrate ladder."""

    index: int
    width: int
    height: int
    bitrate_kbps: int
    segment_sizes: tuple[int, ...]

    @property
    def resolution(self) -> str:
        return f"{self.height}p"

    @property
    def label(self) -> str:
        return f"{self.resolution}-{self.bitrate_kbps}k"


@dataclass(frozen=True, slots=True)
class Manifest:
    video: str
    segment_duration: float
    """Nominal (target) segment duration in seconds."""
    segment_durations: tuple[float, ...]
    """Actual duration of every segment in seconds; shared by all levels."""
    levels: tuple[Level, ...]
    """Ladder sorted by ascending bitrate; ``levels[i].index == i``."""

    def __post_init__(self) -> None:
        validate_video_name(self.video)
        if self.segment_duration <= 0:
            raise ValueError("segment_duration must be positive")
        if not self.segment_durations:
            raise ValueError("manifest has no segments")
        if any(d <= 0 for d in self.segment_durations):
            raise ValueError("segment durations must be positive")
        if not self.levels:
            raise ValueError("manifest has no levels")
        for i, level in enumerate(self.levels):
            if level.index != i:
                raise ValueError("level indices must be 0..n-1 in order")
            if len(level.segment_sizes) != len(self.segment_durations):
                raise ValueError(f"level {i} has a wrong number of segment sizes")
            if any(size <= 0 for size in level.segment_sizes):
                raise ValueError(f"level {i} has a non-positive segment size")
            if i > 0 and level.bitrate_kbps <= self.levels[i - 1].bitrate_kbps:
                raise ValueError("levels must be sorted by strictly increasing bitrate")
        labels = [level.label for level in self.levels]
        if len(set(labels)) != len(labels):
            raise ValueError("levels must have unique resolution/bitrate pairs")

    @property
    def segment_count(self) -> int:
        return len(self.segment_durations)

    @property
    def duration(self) -> float:
        return sum(self.segment_durations)

    @property
    def bitrates_kbps(self) -> tuple[int, ...]:
        return tuple(level.bitrate_kbps for level in self.levels)

    def segment_filename(self, level: int, seq: int) -> str:
        if not 0 <= seq < self.segment_count:
            raise IndexError(f"segment {seq} out of range")
        lv = self.levels[level]
        return segment_filename(self.video, lv.height, lv.bitrate_kbps, seq)

    def level_bandwidth_bps(self, level: int) -> float:
        """Average bandwidth needed to stream ``level`` in real time (all bytes incl. audio/TS
        overhead), the equivalent of DASH ``@bandwidth``."""
        return sum(self.levels[level].segment_sizes) * 8 / self.duration

    def segment_size(self, level: int, seq: int) -> int:
        return self.levels[level].segment_sizes[seq]

    def find_level(self, height: int, bitrate_kbps: int) -> Level | None:
        for level in self.levels:
            if level.height == height and level.bitrate_kbps == bitrate_kbps:
                return level
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": MANIFEST_FORMAT_VERSION,
            "video": self.video,
            "segment_duration": self.segment_duration,
            "segment_count": self.segment_count,
            "segment_durations": list(self.segment_durations),
            "levels": [
                {
                    "index": lv.index,
                    "width": lv.width,
                    "height": lv.height,
                    "bitrate_kbps": lv.bitrate_kbps,
                    "segment_sizes": list(lv.segment_sizes),
                }
                for lv in self.levels
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        if data.get("format_version") != MANIFEST_FORMAT_VERSION:
            raise ValueError(f"unsupported manifest format {data.get('format_version')!r}")
        durations = tuple(float(d) for d in data["segment_durations"])
        if data.get("segment_count", len(durations)) != len(durations):
            raise ValueError("segment_count does not match segment_durations")
        levels = tuple(
            Level(
                index=int(lv["index"]),
                width=int(lv["width"]),
                height=int(lv["height"]),
                bitrate_kbps=int(lv["bitrate_kbps"]),
                segment_sizes=tuple(int(s) for s in lv["segment_sizes"]),
            )
            for lv in data["levels"]
        )
        return cls(
            video=str(data["video"]),
            segment_duration=float(data["segment_duration"]),
            segment_durations=durations,
            levels=levels,
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str | bytes) -> Manifest:
        return cls.from_dict(json.loads(text))
