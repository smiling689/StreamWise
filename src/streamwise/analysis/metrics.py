"""Per-session streaming metrics and QoE.

Definitions (used consistently by the client, the experiment runner and the report):

* **startup delay** — time from the first segment request until playback starts.
* **stall (rebuffering) time** — time the playhead is frozen after playback started because
  the next segment has not arrived; the startup delay is *not* a stall.
* **rebuffering ratio** — ``stall_time / (played_media_time + stall_time)``, i.e. the
  fraction of viewing time (after startup) spent frozen.
* **average bitrate** — duration-weighted mean of the nominal bitrate of played segments.
* **switches** — number of consecutive segment pairs with different levels, and the mean
  absolute bitrate change per switch.
* **QoE** — the linear model of Yin et al. (MPC, SIGCOMM 2015) / Mao et al. (Pensieve, 2017)::

      QoE = sum_k q(R_k) - mu * T_stall - mu_s * T_startup - sum_k |q(R_k) - q(R_{k-1})|

  with ``q(R)`` the bitrate in Mbit/s. As in Pensieve, ``mu`` defaults to the highest ladder
  bitrate in Mbit/s, so one second of stalling costs as much as one segment at top quality.
  ``qoe_per_segment`` divides by the number of segments so videos of different lengths are
  comparable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class QoeWeights:
    rebuffer: float | None = None
    """Penalty per stalled second (``mu``); ``None`` = highest bitrate in Mbit/s."""
    startup: float | None = None
    """Penalty per second of startup delay (``mu_s``); ``None`` = same as ``rebuffer``."""
    smoothness: float = 1.0


@dataclass(frozen=True, slots=True)
class SessionMetrics:
    segments: int
    played_s: float
    startup_delay_s: float
    stall_count: int
    stall_time_s: float
    rebuffer_ratio: float
    avg_bitrate_kbps: float
    switch_count: int
    avg_switch_kbps: float
    qoe: float
    qoe_per_segment: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_metrics(
    bitrates_kbps: Sequence[int],
    durations_s: Sequence[float],
    startup_delay_s: float,
    stall_durations_s: Sequence[float],
    max_bitrate_kbps: int,
    weights: QoeWeights | None = None,
) -> SessionMetrics:
    """Metrics for the played segments ``bitrates_kbps`` (in play order)."""
    if len(bitrates_kbps) != len(durations_s):
        raise ValueError("bitrates and durations must have the same length")
    weights = weights or QoeWeights()
    n = len(bitrates_kbps)
    played = float(sum(durations_s))
    stall_time = float(sum(stall_durations_s))
    switches = [
        abs(b - a) for a, b in zip(bitrates_kbps, bitrates_kbps[1:], strict=False) if a != b
    ]
    avg_bitrate = (
        sum(b * d for b, d in zip(bitrates_kbps, durations_s, strict=True)) / played
        if played > 0
        else 0.0
    )

    mu = weights.rebuffer if weights.rebuffer is not None else max_bitrate_kbps / 1000
    mu_s = weights.startup if weights.startup is not None else mu
    quality = sum(b / 1000 for b in bitrates_kbps)
    smooth = sum(switches) / 1000
    qoe = quality - mu * stall_time - mu_s * startup_delay_s - weights.smoothness * smooth

    return SessionMetrics(
        segments=n,
        played_s=played,
        startup_delay_s=startup_delay_s,
        stall_count=sum(1 for s in stall_durations_s if s > 0),
        stall_time_s=stall_time,
        rebuffer_ratio=stall_time / (played + stall_time) if played + stall_time > 0 else 0.0,
        avg_bitrate_kbps=avg_bitrate,
        switch_count=len(switches),
        avg_switch_kbps=sum(switches) / len(switches) if switches else 0.0,
        qoe=qoe,
        qoe_per_segment=qoe / n if n else 0.0,
    )
