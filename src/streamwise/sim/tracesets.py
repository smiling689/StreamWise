"""Bandwidth trace sets for training and evaluating ABR algorithms.

Splits are disjoint by construction:

* synthetic traces are generated from integer seeds, and every split uses its own seed range;
* measured traces (Norway HSDPA, fetched with ``scripts/fetch_hsdpa_traces.sh``) come as
  ``train/`` and ``test/`` file sets. A fixed, seeded 20 % of the train files is held out for
  validation (model selection), and the test files are never used for training or tuning.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path

from streamwise.experiments.traces import fluctuating_trace
from streamwise.server.shaping import BandwidthTrace

MIN_TRACE_S = 600.0
"""Every trace is looped to at least this length (longer than any session)."""

SEED_RANGES = {"train": (0, 100_000), "validation": (100_000, 200_000), "test": (200_000, 300_000)}


@dataclass(frozen=True, slots=True)
class NamedTrace:
    name: str
    trace: BandwidthTrace


def _log_uniform(rng: random.Random, low: float, high: float) -> float:
    return math.exp(rng.uniform(math.log(low), math.log(high)))


def synthetic_trace(seed: int) -> NamedTrace:
    """One synthetic trace; the seed picks a family and its parameters."""
    rng = random.Random(seed)
    family = rng.choices(["fluctuating", "steps", "constant"], weights=[0.6, 0.3, 0.1])[0]
    if family == "fluctuating":
        points = fluctuating_trace(
            seed=rng.randrange(2**31),
            duration_s=MIN_TRACE_S,
            mean_mbps=_log_uniform(rng, 0.6, 9.0),
            min_mbps=0.3,
            max_mbps=14.0,
            hold_s=(rng.uniform(0.5, 2.0), rng.uniform(3.0, 8.0)),
            volatility=rng.uniform(0.1, 0.8),
        )
    elif family == "steps":  # long stable periods with sudden jumps (handovers, congestion)
        points, t = [], 0.0
        while t < MIN_TRACE_S:
            points.append((t, round(_log_uniform(rng, 0.4, 12.0), 3)))
            t += rng.uniform(10.0, 60.0)
    else:
        points = [(0.0, round(_log_uniform(rng, 0.5, 10.0), 3))]
    return NamedTrace(f"synthetic-{seed}-{family}", BandwidthTrace.from_samples(points))


def synthetic_set(split: str, count: int) -> list[NamedTrace]:
    start, end = SEED_RANGES[split]
    if count > end - start:
        raise ValueError("too many traces requested for one split")
    return [synthetic_trace(start + i) for i in range(count)]


def load_directory(directory: Path) -> list[NamedTrace]:
    if not directory.is_dir():
        raise FileNotFoundError(
            f"{directory} not found; run scripts/fetch_hsdpa_traces.sh to download the traces"
        )
    traces = []
    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        trace = BandwidthTrace.load_samples(path).repeated(MIN_TRACE_S)
        traces.append(NamedTrace(f"{directory.name}/{path.name}", trace))
    return traces


def measured_split(root: Path, split: str, validation_fraction: float = 0.2) -> list[NamedTrace]:
    """``train``/``validation`` partition ``root/train``; ``test`` is ``root/test``."""
    if split == "test":
        return load_directory(root / "test")
    traces = load_directory(root / "train")
    order = list(range(len(traces)))
    random.Random(2024).shuffle(order)
    n_val = round(len(traces) * validation_fraction)
    chosen = set(order[:n_val]) if split == "validation" else set(order[n_val:])
    return [t for i, t in enumerate(traces) if i in chosen]
