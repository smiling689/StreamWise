import pytest

from streamwise.common.manifest import Level, Manifest
from streamwise.common.naming import parse_segment_filename, segment_filename

from .conftest import make_manifest


def test_segment_filename_roundtrip() -> None:
    name = segment_filename("ocean", 1080, 8000, 0)
    assert name == "ocean-1080p-8000k-0.ts"
    seg = parse_segment_filename(name)
    assert (seg.video, seg.height, seg.bitrate_kbps, seg.seq) == ("ocean", 1080, 8000, 0)
    assert seg.filename == name


@pytest.mark.parametrize(
    "bad",
    [
        "../ocean-1080p-8000k-0.ts",
        "ocean-1080p-8000k-0.ts/../../etc/passwd",
        "ocean-1080p-8000k-01.ts",  # leading zero: not canonical
        "ocean-1080-8000k-0.ts",
        "ocean-1080p-8000k-0.mp4",
        "oc-ean-1080p-8000k-0.ts",
        "ocean-0p-8000k-0.ts",
        "",
    ],
)
def test_parse_rejects_invalid_names(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_segment_filename(bad)


def test_invalid_video_name() -> None:
    with pytest.raises(ValueError):
        segment_filename("bad-name", 360, 800, 0)


def test_manifest_json_roundtrip() -> None:
    m = make_manifest(segments=4)
    again = Manifest.from_json(m.to_json())
    assert again == m
    assert again.segment_count == 4
    assert again.segment_filename(2, 3) == "test-1080p-5000k-3.ts"
    assert again.duration == pytest.approx(0.8)
    assert again.level_bandwidth_bps(0) == pytest.approx(
        sum(m.levels[0].segment_sizes) * 8 / m.duration
    )


def test_manifest_validation() -> None:
    ok = Level(0, 640, 360, 800, (1, 2))
    with pytest.raises(ValueError, match="sorted"):
        Manifest("v", 1.0, (1.0, 1.0), (ok, Level(1, 640, 360, 700, (1, 2))))
    with pytest.raises(ValueError, match="segment sizes"):
        Manifest("v", 1.0, (1.0, 1.0), (Level(0, 640, 360, 800, (1,)),))
    with pytest.raises(ValueError, match="indices"):
        Manifest("v", 1.0, (1.0, 1.0), (Level(1, 640, 360, 800, (1, 2)),))
    with pytest.raises(IndexError):
        Manifest("v", 1.0, (1.0, 1.0), (ok,)).segment_filename(0, 2)
