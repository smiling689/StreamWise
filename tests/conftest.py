from __future__ import annotations

import os
from pathlib import Path

import pytest

from streamwise.common.manifest import Level, Manifest


def make_manifest(
    video: str = "test",
    segments: int = 6,
    duration: float = 0.2,
    ladder: tuple[tuple[int, int], ...] = ((360, 800), (720, 2500), (1080, 5000)),
) -> Manifest:
    levels = tuple(
        Level(
            index=i,
            width=height * 16 // 9 // 2 * 2,
            height=height,
            bitrate_kbps=kbps,
            # Size matches the nominal bitrate, with a little variation between segments.
            segment_sizes=tuple(
                int(kbps * 1000 / 8 * duration * (1 + 0.05 * (s % 3))) for s in range(segments)
            ),
        )
        for i, (height, kbps) in enumerate(ladder)
    )
    return Manifest(video, duration, (duration,) * segments, levels)


def write_fake_media(media_dir: Path, manifest: Manifest) -> Path:
    """Write random segment payloads; the server never parses TS, so any bytes will do."""
    video_dir = media_dir / manifest.video
    video_dir.mkdir(parents=True)
    for level in manifest.levels:
        for seq, size in enumerate(level.segment_sizes):
            (video_dir / manifest.segment_filename(level.index, seq)).write_bytes(os.urandom(size))
    (video_dir / "manifest.json").write_text(manifest.to_json())
    return video_dir


@pytest.fixture
def fake_media(tmp_path: Path) -> tuple[Path, Manifest]:
    manifest = make_manifest()
    media_dir = tmp_path / "media"
    write_fake_media(media_dir, manifest)
    return media_dir, manifest
