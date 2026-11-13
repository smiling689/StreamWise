"""Rule-based ABR algorithms.

Bitrates are compared using each level's *average required bandwidth* computed from the
actual segment sizes in the manifest (video + audio + container overhead), which is what the
network really has to carry; nominal encoder bitrates would underestimate it.
"""

from __future__ import annotations

from streamwise.abr.base import AbrAlgorithm, AbrState


def _bandwidths(state: AbrState) -> list[float]:
    return [state.manifest.level_bandwidth_bps(i) for i in range(state.level_count)]


def harmonic_mean(values: tuple[float, ...] | list[float]) -> float:
    positive = [v for v in values if v > 0]
    if not positive:
        return 0.0
    return len(positive) / sum(1.0 / v for v in positive)


class FixedAbr(AbrAlgorithm):
    """Always request the same level (baseline for experiments)."""

    def __init__(self, level: int | str = 0) -> None:
        self._level = level
        self.name = f"fixed:{level}"

    def choose(self, state: AbrState) -> int:
        if self._level == "max":
            return state.level_count - 1
        if self._level == "min":
            return 0
        level = int(self._level)
        if not 0 <= level < state.level_count:
            raise ValueError(f"fixed level {level} out of range")
        return level


class ThroughputAbr(AbrAlgorithm):
    """Rate-based ABR.

    Throughput is predicted as the harmonic mean of the last ``window`` segment downloads
    (robust to outliers, as in FESTIVE/MPC); the highest level whose bandwidth does not exceed
    ``safety * prediction`` is chosen. The first segment uses the lowest level.
    """

    name = "throughput"

    def __init__(self, window: int = 5, safety: float = 0.9) -> None:
        if window < 1 or not 0 < safety <= 1:
            raise ValueError("window must be >= 1 and 0 < safety <= 1")
        self.window = window
        self.safety = safety

    def predict(self, state: AbrState) -> float:
        return harmonic_mean(state.throughputs_bps[-self.window :])

    def choose(self, state: AbrState) -> int:
        estimate = self.predict(state) * self.safety
        if estimate <= 0:
            return 0
        best = 0
        for level, bandwidth in enumerate(_bandwidths(state)):
            if bandwidth <= estimate:
                best = level
        return best

    def describe(self) -> str:
        return f"throughput(window={self.window}, safety={self.safety})"


class BufferAbr(AbrAlgorithm):
    """Buffer-based ABR: BBA-0 (Huang et al., SIGCOMM 2014).

    The buffer level ``B`` is mapped to a target rate ``f(B)``: the lowest rate while ``B`` is
    below the *reservoir* ``r``, the highest rate once ``B >= r + cushion``, and a linear ramp
    in between. To avoid oscillation the level only changes when ``f(B)`` crosses the next
    higher rate (switch up to the highest rate strictly below ``f(B)``) or the next lower
    rate (switch down to the lowest rate strictly above ``f(B)``).

    Defaults for 5 s segments and a 30 s buffer cap: the reservoir covers two segments, so
    the lowest level is kept until a slow download can no longer drain the buffer, and
    ``reservoir + cushion`` (24 s) stays below the download gate (30 s - 5 s = 25 s), so the
    top level is reachable.
    """

    name = "buffer"

    def __init__(self, reservoir_s: float = 10.0, cushion_s: float = 14.0) -> None:
        if reservoir_s < 0 or cushion_s <= 0:
            raise ValueError("reservoir must be >= 0 and cushion > 0")
        self.reservoir_s = reservoir_s
        self.cushion_s = cushion_s

    def target_rate(self, buffer_s: float, rates: list[float]) -> float:
        r_min, r_max = rates[0], rates[-1]
        if buffer_s <= self.reservoir_s:
            return r_min
        if buffer_s >= self.reservoir_s + self.cushion_s:
            return r_max
        return r_min + (r_max - r_min) * (buffer_s - self.reservoir_s) / self.cushion_s

    def choose(self, state: AbrState) -> int:
        rates = _bandwidths(state)
        top = len(rates) - 1
        prev = 0 if state.last_level is None else state.last_level
        buffer_s = state.buffer_s
        if buffer_s <= self.reservoir_s:
            return 0
        if buffer_s >= self.reservoir_s + self.cushion_s:
            return top
        f = self.target_rate(buffer_s, rates)
        rate_plus = rates[min(prev + 1, top)]
        rate_minus = rates[max(prev - 1, 0)]
        if f >= rate_plus:
            below = [i for i, r in enumerate(rates) if r < f]
            return max(below) if below else 0
        if f <= rate_minus:
            above = [i for i, r in enumerate(rates) if r > f]
            return min(above) if above else top
        return prev

    def describe(self) -> str:
        return f"buffer(reservoir={self.reservoir_s}s, cushion={self.cushion_s}s)"
