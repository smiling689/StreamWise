from pathlib import Path

import numpy as np
import pytest

from streamwise.abr import AbrState, create_abr
from streamwise.abr.qlearning import QLearningAbr, QModel, StateEncoder
from streamwise.server.shaping import BandwidthTrace
from streamwise.sim import tracesets as ts
from streamwise.sim.env import simulate
from streamwise.sim.train import TrainConfig, train

from .conftest import make_manifest

MANIFEST = make_manifest(segments=10, duration=5.0, ladder=((360, 800), (720, 2500), (1080, 5000)))
BW = tuple(MANIFEST.level_bandwidth_bps(i) for i in range(3))


def state(buffer_s: float = 0.0, last: int | None = None, tput: tuple = ()) -> AbrState:
    return AbrState(len(tput), MANIFEST, buffer_s, 30.0, last, tput, tuple(1.0 for _ in tput))


def test_encoder_is_a_bijection_on_features() -> None:
    enc = StateEncoder(levels=3)
    assert enc.shape == (4, 11, 14, 3)
    seen = set()
    for last in [None, 0, 1, 2]:
        for buf in [0, 3, 12, 26]:
            for tput in [(), (0.4e6,), (3e6, 3e6, 3e6), (3e6, 3e6, 0.5e6)]:
                s = enc.encode(state(buf, last, tput))
                assert 0 <= s < enc.size
                seen.add((enc.features(state(buf, last, tput)), s))
    assert len({s for _, s in seen}) == len({f for f, _ in seen})
    # A sudden drop is visible as a falling trend.
    assert enc.features(state(10, 1, (3e6, 3e6, 0.5e6)))[3] == 0
    assert enc.features(state(10, 1, (3e6, 3e6, 3e6)))[3] == 1


def test_model_roundtrip_and_fallback(tmp_path: Path) -> None:
    enc = StateEncoder(levels=3)
    model = QModel.empty(enc, BW)
    s = enc.encode(state(12, 1, (3e6,) * 3))
    model.q[s] = [1.0, 5.0, 2.0]
    model.visits[s] = [1, 1, 0]
    model.metadata = {"note": "test"}
    path = tmp_path / "m.json"
    model.save(path)
    loaded = QModel.load(path)
    assert loaded.metadata == {"note": "test"}
    assert np.array_equal(loaded.visits, model.visits)
    assert loaded.greedy(s) == 1

    abr = create_abr(f"qlearning:model={path}")
    assert isinstance(abr, QLearningAbr)
    assert abr.choose(state(12, 1, (3e6,) * 3)) == 1
    assert abr.fallbacks == 0
    abr.choose(state(0, None, ()))  # never visited -> buffer-based fallback (level 0)
    assert abr.fallbacks == 1 and abr.decisions == 2


def test_unvisited_actions_are_never_chosen() -> None:
    enc = StateEncoder(levels=3)
    model = QModel.empty(enc, BW)
    s = enc.encode(state(12, 1, (3e6,) * 3))
    model.q[s] = [-1.0, -2.0, 0.0]  # action 2 has the best value but was never tried
    model.visits[s] = [3, 3, 0]
    assert model.greedy(s) == 0


def test_ladder_mismatch_detected() -> None:
    other = make_manifest(segments=10, duration=5.0, ladder=((360, 800), (1080, 5000)))
    abr = QLearningAbr(QModel.empty(StateEncoder(levels=2), (1e6, 5e6)))
    st = AbrState(0, other, 0.0, 30.0, None, (), ())
    with pytest.raises(ValueError, match="different bitrate ladder"):
        abr.choose(st)


def test_learns_obvious_policies() -> None:
    fast = [ts.NamedTrace("fast", BandwidthTrace.constant(20.0))]
    slow = [ts.NamedTrace("slow", BandwidthTrace.constant(0.7))]
    config = TrainConfig(episodes=3000, eval_every=1000, seed=3)
    for traces, expected in [(fast, 2), (slow, 0)]:
        model = train(MANIFEST, traces, traces, config, log=lambda _: None)
        result = simulate(MANIFEST, traces[0].trace, QLearningAbr(model))
        levels = [s.level for s in result.segments]
        assert levels.count(expected) >= 8, levels
        assert model.metadata["best_validation_qoe"] == pytest.approx(
            result.metrics.qoe_per_segment, abs=1e-4
        )


def test_trace_splits_are_disjoint_and_deterministic(tmp_path: Path) -> None:
    a = ts.synthetic_set("train", 20)
    assert [t.name for t in a] == [t.name for t in ts.synthetic_set("train", 20)]
    names = {split: {t.name for t in ts.synthetic_set(split, 20)} for split in ts.SEED_RANGES}
    assert not names["train"] & names["validation"]
    assert not names["train"] & names["test"]
    for split in ["train", "test"]:
        (tmp_path / split).mkdir()
    for i in range(10):
        (tmp_path / "train" / f"t{i}").write_text("0 1.5\n1 2.5\n2 0.5\n")
    (tmp_path / "test" / "x").write_text("0 1\n1 2\n")
    train_set = {t.name for t in ts.measured_split(tmp_path, "train")}
    val_set = {t.name for t in ts.measured_split(tmp_path, "validation")}
    assert len(val_set) == 2 and len(train_set) == 8 and not train_set & val_set
    test_set = ts.measured_split(tmp_path, "test")
    assert [t.name for t in test_set] == ["test/x"]
    assert test_set[0].trace.times[-1] >= ts.MIN_TRACE_S - 2
