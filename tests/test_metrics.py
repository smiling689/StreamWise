import pytest

from streamwise.analysis.metrics import QoeWeights, compute_metrics


def test_metrics_and_qoe() -> None:
    m = compute_metrics(
        bitrates_kbps=[800, 2500, 2500, 5000],
        durations_s=[5, 5, 5, 5],
        startup_delay_s=1.0,
        stall_durations_s=[0.5, 1.5],
        max_bitrate_kbps=8000,
    )
    assert m.segments == 4
    assert m.played_s == 20
    assert m.avg_bitrate_kbps == pytest.approx(2700)
    assert m.switch_count == 2
    assert m.avg_switch_kbps == pytest.approx((1700 + 2500) / 2)
    assert m.stall_count == 2
    assert m.rebuffer_ratio == pytest.approx(2 / 22)
    # 10.8 - 8*2 - 8*1 - 4.2
    assert m.qoe == pytest.approx(10.8 - 16 - 8 - 4.2)
    assert m.qoe_per_segment == pytest.approx(m.qoe / 4)


def test_custom_weights() -> None:
    m = compute_metrics([1000, 2000], [1, 1], 2.0, [1.0], 4000, QoeWeights(1, 0, 0.5))
    assert m.qoe == pytest.approx(3.0 - 1.0 - 0 - 0.5)
