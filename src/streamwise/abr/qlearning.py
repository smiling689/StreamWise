"""Tabular Q-learning ABR.

The agent sees a discretized version of the same :class:`AbrState` the rule-based
algorithms use, and picks a level. It is trained offline in the trace-driven simulator
(:mod:`streamwise.sim`), with the per-segment QoE as reward, and then runs unchanged in the
real client.

State (one table row per combination):

* previous level (or "none" for the first segment);
* buffer level, in 2.5 s bins up to the 25 s download gate;
* throughput estimate: harmonic mean of the last 3 downloads, in roughly logarithmic bins
  (Mbit/s);
* throughput trend: last download / that estimate (falling, steady, rising). This lets the
  agent react to a sudden drop after a single sample, where a moving average lags.

The chosen action is ``argmax_a Q(s, a)`` over the actions visited during training. If a
state was never visited, the decision falls back to the buffer-based rule (BBA), and
:attr:`QLearningAbr.fallbacks` counts how often that happened.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from streamwise.abr.base import AbrAlgorithm, AbrState
from streamwise.abr.rules import BufferAbr, harmonic_mean

MODEL_FORMAT = 1
DEFAULT_MODEL = Path("models/qlearning-bbb.json")


@dataclass(frozen=True, slots=True)
class StateEncoder:
    levels: int
    buffer_edges_s: tuple[float, ...] = (2.5, 5, 7.5, 10, 12.5, 15, 17.5, 20, 22.5, 25)
    throughput_edges_mbps: tuple[float, ...] = (
        0.5,
        0.7,
        0.9,
        1.1,
        1.4,
        1.8,
        2.3,
        3.0,
        4.0,
        5.5,
        7.5,
        10.0,
    )
    trend_edges: tuple[float, ...] = (0.75, 1.25)
    estimate_window: int = 3

    @property
    def shape(self) -> tuple[int, int, int, int]:
        return (
            self.levels + 1,
            len(self.buffer_edges_s) + 1,
            len(self.throughput_edges_mbps) + 2,  # +1 bin for "no measurement yet"
            len(self.trend_edges) + 1,
        )

    @property
    def size(self) -> int:
        return int(np.prod(self.shape))

    def features(self, state: AbrState) -> tuple[int, int, int, int]:
        last = self.levels if state.last_level is None else state.last_level
        buf = bisect.bisect_right(self.buffer_edges_s, state.buffer_s)
        history = state.throughputs_bps
        if not history:
            return last, buf, 0, 1
        estimate = harmonic_mean(history[-self.estimate_window :])
        tput = 1 + bisect.bisect_right(self.throughput_edges_mbps, estimate / 1e6)
        trend = bisect.bisect_right(self.trend_edges, history[-1] / estimate)
        return last, buf, tput, trend

    def encode(self, state: AbrState) -> int:
        return int(np.ravel_multi_index(self.features(state), self.shape))

    def to_dict(self) -> dict[str, Any]:
        return {
            "levels": self.levels,
            "buffer_edges_s": list(self.buffer_edges_s),
            "throughput_edges_mbps": list(self.throughput_edges_mbps),
            "trend_edges": list(self.trend_edges),
            "estimate_window": self.estimate_window,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateEncoder:
        return cls(
            levels=int(data["levels"]),
            buffer_edges_s=tuple(data["buffer_edges_s"]),
            throughput_edges_mbps=tuple(data["throughput_edges_mbps"]),
            trend_edges=tuple(data["trend_edges"]),
            estimate_window=int(data["estimate_window"]),
        )


@dataclass(slots=True)
class QModel:
    encoder: StateEncoder
    q: np.ndarray
    """Shape ``(encoder.size, levels)``."""
    visits: np.ndarray
    """Training updates per state-action pair, same shape as ``q``."""
    level_bandwidths_bps: tuple[float, ...]
    """Ladder the model was trained for (checked against the manifest at run time)."""
    metadata: dict[str, Any]

    @classmethod
    def empty(cls, encoder: StateEncoder, level_bandwidths_bps: tuple[float, ...]) -> QModel:
        shape = (encoder.size, encoder.levels)
        return cls(
            encoder, np.zeros(shape), np.zeros(shape, dtype=np.int64), level_bandwidths_bps, {}
        )

    def greedy(self, s: int) -> int | None:
        """Best visited action in state ``s``, or ``None`` if the state was never visited."""
        visited = self.visits[s] > 0
        if not visited.any():
            return None
        values = np.where(visited, self.q[s], -np.inf)
        return int(np.argmax(values))

    def save(self, path: Path) -> None:
        rows = np.flatnonzero(self.visits.sum(axis=1))
        data = {
            "format": MODEL_FORMAT,
            "encoder": self.encoder.to_dict(),
            "level_bandwidths_bps": list(self.level_bandwidths_bps),
            "metadata": self.metadata,
            "states": {
                str(int(s)): {
                    "q": [round(float(v), 5) for v in self.q[s]],
                    "n": [int(v) for v in self.visits[s]],
                }
                for s in rows
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, separators=(",", ":")) + "\n")

    @classmethod
    def load(cls, path: Path) -> QModel:
        data = json.loads(path.read_text())
        if data.get("format") != MODEL_FORMAT:
            raise ValueError(f"{path}: unsupported model format {data.get('format')!r}")
        encoder = StateEncoder.from_dict(data["encoder"])
        model = cls.empty(encoder, tuple(data["level_bandwidths_bps"]))
        for key, row in data["states"].items():
            s = int(key)
            model.q[s] = row["q"]
            model.visits[s] = row["n"]
        model.metadata = data.get("metadata", {})
        return model


class QLearningAbr(AbrAlgorithm):
    name = "qlearning"

    def __init__(self, model: str | Path | QModel = DEFAULT_MODEL) -> None:
        self.model = model if isinstance(model, QModel) else QModel.load(Path(model))
        self._source = "in-memory" if isinstance(model, QModel) else str(model)
        self._fallback_abr = BufferAbr()
        self._checked = False
        self.decisions = 0
        self.fallbacks = 0

    def reset(self) -> None:
        self._fallback_abr.reset()
        self.decisions = 0
        self.fallbacks = 0

    def _check_ladder(self, state: AbrState) -> None:
        if self._checked:
            return
        trained = np.array(self.model.level_bandwidths_bps)
        actual = np.array([state.manifest.level_bandwidth_bps(i) for i in range(state.level_count)])
        if trained.shape != actual.shape or not np.allclose(trained, actual, rtol=0.1):
            raise ValueError(
                "the Q-learning model was trained for a different bitrate ladder "
                f"({len(trained)} levels); retrain it for this video"
            )
        self._checked = True

    def choose(self, state: AbrState) -> int:
        self._check_ladder(state)
        self.decisions += 1
        action = self.model.greedy(self.model.encoder.encode(state))
        if action is None:
            self.fallbacks += 1
            return self._fallback_abr.choose(state)
        return action

    def describe(self) -> str:
        return f"qlearning(model={self._source})"
