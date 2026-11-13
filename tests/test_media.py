"""FFmpeg pipeline test on a short synthetic clip (skipped if FFmpeg is not installed)."""

from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from streamwise.media.quality import QualityTable, measure_quality
from streamwise.media.segment import Rung, SegmentOptions, inspect_segment, segment_video

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs FFmpeg"
)


@pytest.fixture(scope="module")
def segmented(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("media")
    source = root / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=4.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4.5",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-c:a", "aac",
            str(source),
        ],
        check=True,
    )  # fmt: skip
    opts = SegmentOptions(
        video="synth",
        segment_duration=1.0,
        ladder=(Rung(180, 150), Rung(360, 1500)),
        preset="ultrafast",
    )
    manifest = segment_video(source, root, opts)
    return root, manifest


def test_segments_are_aligned_and_complete(segmented) -> None:
    root, manifest = segmented
    video_dir = root / "synth"
    assert manifest.segment_count == 4  # 4.5 s rounded down to whole 1 s segments
    assert manifest.segment_durations == (1.0,) * 4
    assert [lv.width for lv in manifest.levels] == [320, 640]
    assert not (root / ".synth.partial").exists()
    for level in manifest.levels:
        for seq in range(manifest.segment_count):
            path = video_dir / manifest.segment_filename(level.index, seq)
            assert path.stat().st_size == level.segment_sizes[seq]
            assert inspect_segment(path) == (25, True)


def test_mixed_level_stream_decodes_cleanly(segmented, tmp_path: Path) -> None:
    root, manifest = segmented
    video_dir = root / "synth"
    mixed = tmp_path / "mixed.ts"
    with mixed.open("wb") as out:
        for seq, level in enumerate([0, 1, 0, 1]):
            out.write((video_dir / manifest.segment_filename(level, seq)).read_bytes())
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "warning", "-i", str(mixed), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "corrupt" not in proc.stderr.lower()
    assert "error" not in proc.stderr.lower()


def test_quality_table(segmented) -> None:
    root, manifest = segmented
    table = measure_quality(root / "synth")
    assert table == QualityTable.load(root / "synth" / "quality.json")
    assert (table.reference_width, table.reference_height) == (640, 360)
    low, high = table.levels
    assert len(low.segment_mse) == manifest.segment_count
    for seq in range(manifest.segment_count):
        assert 15 < table.psnr(0, seq) < table.psnr(1, seq) <= 100


def test_rejects_fractional_gop(tmp_path: Path, segmented) -> None:
    root, _ = segmented
    opts = SegmentOptions(video="bad", segment_duration=1.01, frame_rate=Fraction(25))
    with pytest.raises(ValueError, match="whole number of frames"):
        segment_video(root / "source.mp4", tmp_path, opts)
