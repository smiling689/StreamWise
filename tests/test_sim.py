import pytest

from streamwise.abr import create_abr
from streamwise.server.shaping import MBPS, BandwidthTrace
from streamwise.sim.env import PREAMBLE_OVERHEAD_BYTES, SimConfig, StreamingEnv, simulate

from .conftest import make_manifest

MANIFEST = make_manifest(segments=8, duration=5.0, ladder=((360, 800), (720, 2500), (1080, 5000)))
NO_BURST = SimConfig(burst_bytes=0)


def test_constant_link_closed_form() -> None:
    trace = BandwidthTrace.constant(2.0)  # 250 kB/s
    result = simulate(MANIFEST, trace, create_abr("fixed:0"), NO_BURST)
    sizes = MANIFEST.levels[0].segment_sizes
    first = result.segments[0]
    assert first.download_s == pytest.approx(sizes[0] / 250_000)
    assert result.startup_delay_s == pytest.approx(first.download_s)
    assert result.stalls_s == []
    for seg, size in zip(result.segments, sizes, strict=True):
        assert seg.throughput_bps == pytest.approx(2 * MBPS)
        assert seg.bytes == size


def test_stalls_when_level_exceeds_link() -> None:
    # Top level needs ~5.25 Mbit/s; at 2 Mbit/s every segment takes ~2.6x its duration.
    result = simulate(MANIFEST, BandwidthTrace.constant(2.0), create_abr("fixed:2"), NO_BURST)
    assert len(result.stalls_s) == MANIFEST.segment_count - 1
    for seg in result.segments[1:]:
        assert seg.stall_s == pytest.approx(seg.download_s - seg.buffer_before_s)
        assert seg.buffer_after_s == pytest.approx(5.0)


def test_rewards_sum_to_session_qoe() -> None:
    trace = BandwidthTrace.parse("0 8\n20 0.5\n50 3")
    for spec in ["throughput", "buffer", "fixed:1"]:
        env = StreamingEnv(MANIFEST)
        abr = create_abr(spec)
        state = env.reset(trace)
        total = 0.0
        while not env.done:
            total += env.step(abr.choose(state))
            state = env.state()
        assert total == pytest.approx(env.result().metrics.qoe)


def test_buffer_never_exceeds_cap() -> None:
    result = simulate(MANIFEST, BandwidthTrace.constant(50.0), create_abr("fixed:0"))
    assert max(s.buffer_after_s for s in result.segments) <= 30.0 + 1e-9
    assert any(s.wait_s > 0 for s in result.segments)


def test_burst_and_preamble() -> None:
    trace = BandwidthTrace.constant(1.0)  # 125 kB/s
    burst = 64 * 1024
    result = simulate(MANIFEST, trace, create_abr("fixed:0"), SimConfig(burst_bytes=burst))
    preamble = len(MANIFEST.to_json()) + 1 + PREAMBLE_OVERHEAD_BYTES
    size = MANIFEST.levels[0].segment_sizes[0]
    assert result.segments[0].download_s == pytest.approx((size - (burst - preamble)) / 125_000)


def test_env_rejects_misuse() -> None:
    env = StreamingEnv(MANIFEST)
    with pytest.raises(RuntimeError):
        env.step(0)
    env.reset(BandwidthTrace.constant(1.0), segments=1)
    with pytest.raises(ValueError):
        env.step(7)
    env.step(0)
    assert env.done
    with pytest.raises(ValueError):
        StreamingEnv(MANIFEST, SimConfig(max_buffer_s=8, startup_buffer_s=5))


def test_trace_from_samples_normalizes() -> None:
    trace = BandwidthTrace.from_samples([(10, 2.0), (11, -1), (11, 5), (12, 3), (13, 0), (14, 0)])
    assert trace.times == (0, 1, 2)
    assert trace.rates_bps == (2 * MBPS, 0, 3 * MBPS)
    with pytest.raises(ValueError):
        BandwidthTrace.from_samples([(0, 0)])


def test_trace_repeat_and_shift() -> None:
    trace = BandwidthTrace.parse("0 1\n1 2\n2 3")
    looped = trace.repeated(10)
    assert looped.times[:6] == (0, 1, 2, 3, 4, 5)  # period = last time + median spacing
    assert looped.times[-1] >= 9
    assert looped.rate_at(4.5) == 2 * MBPS
    shifted = looped.shifted(4.5)
    assert shifted.times[:2] == (0, 0.5)
    assert shifted.rate_at(0) == 2 * MBPS
    assert shifted.bytes_between(0, 3) == pytest.approx(looped.bytes_between(4.5, 7.5))
    assert BandwidthTrace.parse("0 2\n10 4").mean_bps == pytest.approx(2 * MBPS)


def test_trace_text_roundtrip() -> None:
    trace = BandwidthTrace.from_samples([(0.0, 1.234567891), (0.3333333, 0.0), (1.1, 7.5)])
    assert BandwidthTrace.parse(trace.to_text("header\nline 2")) == trace


def test_measured_trace_scenario(tmp_path) -> None:
    from pathlib import Path

    from streamwise.experiments.runner import (
        Scenario,
        prepare_measured_trace,
        scenario_trace_path,
    )

    (tmp_path / "raw").write_text("5.0 1.5\n5.5 2.5\n5.5 9\n6.0 0.5\n")
    scenario = Scenario("m", measured_trace="raw")
    out = tmp_path / "exp"
    prepare_measured_trace(scenario, tmp_path, out)
    path = scenario_trace_path({"name": "m", "measured_trace": "raw"}, tmp_path, out)
    assert path == (out / "traces" / "m.txt").resolve()
    trace = BandwidthTrace.load(path)
    assert trace.times[:3] == (0.0, 0.5, 1.0)
    assert trace.times[-1] >= 590
    assert scenario.server_args(tmp_path, out) == ["--trace", str(path)]
    assert scenario_trace_path({"name": "c", "rate_mbps": 2}, tmp_path, out) is None
    with pytest.raises(ValueError, match="exactly one"):
        Scenario("bad", rate_mbps=1.0, measured_trace="raw")
    assert Path(path).read_text().startswith("# Normalized and looped from raw")
