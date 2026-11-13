"""Synthetic bandwidth trace generation."""

from __future__ import annotations

import math
import random
from pathlib import Path


def fluctuating_trace(
    seed: int,
    duration_s: float = 400.0,
    mean_mbps: float = 3.5,
    min_mbps: float = 0.4,
    max_mbps: float = 12.0,
    hold_s: tuple[float, float] = (2.0, 6.0),
    volatility: float = 0.45,
) -> list[tuple[float, float]]:
    """Piecewise-constant trace whose log-bandwidth follows a mean-reverting random walk.

    Every ``hold_s`` seconds (uniformly random) the bandwidth changes multiplicatively by
    ``exp(N(0, volatility))`` with a pull towards ``mean_mbps``, clipped to
    ``[min_mbps, max_mbps]``. This mimics the bursty variation of cellular links.
    """
    rng = random.Random(seed)
    log_mean = math.log(mean_mbps)
    level = log_mean
    t = 0.0
    points = []
    while t < duration_s:
        mbps = min(max_mbps, max(min_mbps, math.exp(level)))
        points.append((round(t, 3), round(mbps, 3)))
        t += rng.uniform(*hold_s)
        level += 0.3 * (log_mean - level) + rng.gauss(0.0, volatility)
    return points


def write_trace(path: Path, points: list[tuple[float, float]], header: str) -> None:
    lines = [f"# {line}" for line in header.splitlines()]
    lines += [f"{t:g} {mbps:g}" for t, mbps in points]
    path.write_text("\n".join(lines) + "\n")
