"""Segment file naming.

Segments are named ``{video}-{height}p-{kbps}k-{seq}.ts``, e.g. ``ocean-1080p-8000k-0.ts``.
Video names may contain letters, digits and underscores only, because ``-`` is the field
separator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VIDEO_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SEGMENT_NAME_RE = re.compile(
    r"^(?P<video>[A-Za-z0-9_]{1,64})-(?P<height>[1-9][0-9]{0,4})p"
    r"-(?P<kbps>[1-9][0-9]{0,6})k-(?P<seq>0|[1-9][0-9]{0,6})\.ts$"
)


@dataclass(frozen=True, slots=True)
class SegmentId:
    video: str
    height: int
    bitrate_kbps: int
    seq: int

    @property
    def filename(self) -> str:
        return segment_filename(self.video, self.height, self.bitrate_kbps, self.seq)


def validate_video_name(name: str) -> str:
    if not VIDEO_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid video name {name!r}: use letters, digits and underscores")
    return name


def segment_filename(video: str, height: int, bitrate_kbps: int, seq: int) -> str:
    validate_video_name(video)
    if height <= 0 or bitrate_kbps <= 0 or seq < 0:
        raise ValueError("height and bitrate must be positive and seq non-negative")
    return f"{video}-{height}p-{bitrate_kbps}k-{seq}.ts"


def parse_segment_filename(filename: str) -> SegmentId:
    match = SEGMENT_NAME_RE.fullmatch(filename)
    if match is None:
        raise ValueError(f"invalid segment file name {filename!r}")
    return SegmentId(
        video=match["video"],
        height=int(match["height"]),
        bitrate_kbps=int(match["kbps"]),
        seq=int(match["seq"]),
    )
