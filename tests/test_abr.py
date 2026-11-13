import pytest

from streamwise.abr import AbrState, BufferAbr, FixedAbr, ThroughputAbr, create_abr
from streamwise.abr.rules import harmonic_mean

from .conftest import make_manifest

MANIFEST = make_manifest(
    segments=10, duration=5.0, ladder=((360, 800), (480, 1200), (720, 2500), (1080, 5000))
)
# Actual bandwidth per level is nominal * 1.05 on average (see make_manifest).
BW = [MANIFEST.level_bandwidth_bps(i) for i in range(4)]


def state(buffer_s: float = 0.0, last: int | None = None, tput: tuple = ()) -> AbrState:
    return AbrState(
        seq=len(tput),
        manifest=MANIFEST,
        buffer_s=buffer_s,
        max_buffer_s=30.0,
        last_level=last,
        throughputs_bps=tput,
        download_times_s=tuple(1.0 for _ in tput),
    )


def test_harmonic_mean() -> None:
    assert harmonic_mean([1.0, 1.0]) == 1.0
    assert harmonic_mean([1.0, 3.0]) == pytest.approx(1.5)
    assert harmonic_mean([]) == 0.0


def test_throughput_first_segment_lowest() -> None:
    assert ThroughputAbr().choose(state()) == 0


@pytest.mark.parametrize(("mbps", "level"), [(0.5, 0), (1.6, 1), (3.0, 2), (5.0, 2), (6.0, 3)])
def test_throughput_picks_highest_sustainable(mbps: float, level: int) -> None:
    abr = ThroughputAbr(window=3, safety=0.9)
    assert abr.choose(state(tput=(mbps * 1e6,) * 3)) == level
    assert abr.choose(state(tput=(mbps * 1e6,) * 3)) == max(
        [0] + [i for i, b in enumerate(BW) if b <= 0.9 * mbps * 1e6]
    )


def test_throughput_uses_window_only() -> None:
    abr = ThroughputAbr(window=2, safety=1.0)
    assert abr.choose(state(tput=(0.1e6, 10e6, 10e6))) == 3


def test_bba_reservoir_and_cushion() -> None:
    abr = BufferAbr(reservoir_s=5, cushion_s=15)
    assert abr.choose(state(buffer_s=0, last=3)) == 0
    assert abr.choose(state(buffer_s=5, last=3)) == 0
    assert abr.choose(state(buffer_s=20, last=0)) == 3
    assert abr.choose(state(buffer_s=29, last=0)) == 3


def test_bba_hysteresis() -> None:
    abr = BufferAbr(reservoir_s=5, cushion_s=15)
    # f(B) between rate(1) and rate(2): from level 1 stay, since f < rate_plus = rate(2).
    b = 5 + 15 * (((BW[1] + BW[2]) / 2) - BW[0]) / (BW[-1] - BW[0])
    assert abr.choose(state(buffer_s=b, last=1)) == 1
    # From level 0, f >= rate(1) -> switch up to the highest rate strictly below f (level 1).
    assert abr.choose(state(buffer_s=b, last=0)) == 1
    # From level 3, f <= rate(2) -> switch down to the lowest rate strictly above f (level 2).
    assert abr.choose(state(buffer_s=b, last=3)) == 2


def test_bba_monotonic_in_buffer_from_lowest() -> None:
    abr = BufferAbr()
    levels = [abr.choose(state(buffer_s=b / 2, last=0)) for b in range(0, 61)]
    assert levels == sorted(levels)
    assert levels[0] == 0 and levels[-1] == 3


def test_fixed_and_factory() -> None:
    assert FixedAbr("max").choose(state()) == 3
    assert create_abr("fixed:2").choose(state()) == 2
    assert create_abr("fixed:level=min").choose(state()) == 0
    t = create_abr("throughput:window=3,safety=0.8")
    assert isinstance(t, ThroughputAbr) and t.window == 3 and t.safety == 0.8
    b = create_abr("buffer:reservoir_s=4,cushion_s=10")
    assert isinstance(b, BufferAbr) and b.cushion_s == 10
    with pytest.raises(ValueError):
        create_abr("nope")
    with pytest.raises(ValueError):
        create_abr("throughput:bogus=1")
    with pytest.raises(ValueError):
        FixedAbr(9).choose(state())
