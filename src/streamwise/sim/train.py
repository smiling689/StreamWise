"""Offline Q-learning training in the trace-driven simulator."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass

import numpy as np

from streamwise.abr.base import AbrAlgorithm
from streamwise.abr.qlearning import QLearningAbr, QModel, StateEncoder
from streamwise.common.manifest import Manifest
from streamwise.sim.env import SimConfig, StreamingEnv, simulate
from streamwise.sim.tracesets import NamedTrace


@dataclass(frozen=True, slots=True)
class TrainConfig:
    episodes: int = 400_000
    gamma: float = 0.90
    alpha_min: float = 0.005
    """Learning rate is ``max(alpha_min, (1 + visits) ** -alpha_power)`` per state-action."""
    alpha_power: float = 0.6
    epsilon_start: float = 1.0
    epsilon_end: float = 0.02
    epsilon_decay_fraction: float = 0.8
    """Exploration decays linearly over this fraction of the episodes, then stays at the end."""
    random_offset: bool = True
    """Start each episode at a random point of its trace."""
    eval_every: int = 20_000
    seed: int = 1


def evaluate(
    manifest: Manifest,
    traces: Sequence[NamedTrace],
    make_abr: Callable[[], AbrAlgorithm],
    config: SimConfig | None = None,
) -> list[float]:
    """QoE per segment of one session per trace (from the start of each trace)."""
    return [simulate(manifest, t.trace, make_abr(), config).metrics.qoe_per_segment for t in traces]


TraceSets = Mapping[str, Sequence[NamedTrace]] | Sequence[NamedTrace]


def _as_sets(traces: TraceSets) -> dict[str, list[NamedTrace]]:
    sets = {"all": list(traces)} if not isinstance(traces, Mapping) else dict(traces)
    sets = {name: list(ts) for name, ts in sets.items() if ts}
    if not sets:
        raise ValueError("no traces")
    return sets


def train(
    manifest: Manifest,
    train_traces: TraceSets,
    validation_traces: TraceSets,
    config: TrainConfig,
    sim_config: SimConfig | None = None,
    log: Callable[[str], None] = print,
) -> QModel:
    """Train with epsilon-greedy Q-learning; return the best model on the validation set.

    ``train_traces`` and ``validation_traces`` may be plain lists or dictionaries of named
    domains (e.g. synthetic and measured traces). Each episode first picks a domain uniformly,
    then a trace, so a small domain is not drowned out by a large one. Every ``eval_every``
    episodes the greedy policy is evaluated on the validation traces (never on test traces);
    the score is the mean of the per-domain mean QoE, and the best snapshot is kept (early
    stopping).
    """
    train_sets = _as_sets(train_traces)
    validation_sets = _as_sets(validation_traces)
    domains = list(train_sets.values())
    rng = random.Random(config.seed)
    levels = len(manifest.levels)
    encoder = StateEncoder(levels=levels)
    bandwidths = tuple(manifest.level_bandwidth_bps(i) for i in range(levels))
    model = QModel.empty(encoder, bandwidths)
    env = StreamingEnv(manifest, sim_config)
    q, visits = model.q, model.visits
    decay_episodes = max(1, int(config.episodes * config.epsilon_decay_fraction))

    best_score, best = -np.inf, None
    history = []
    started = time.monotonic()
    for episode in range(1, config.episodes + 1):
        frac = min(1.0, episode / decay_episodes)
        epsilon = config.epsilon_start + frac * (config.epsilon_end - config.epsilon_start)
        domain = domains[rng.randrange(len(domains))]
        named = domain[rng.randrange(len(domain))]
        trace = named.trace
        if config.random_offset and trace.end_time > 0:
            trace = trace.shifted(rng.uniform(0, trace.end_time / 2))
        s = encoder.encode(env.reset(trace))
        while True:
            if rng.random() < epsilon:
                a = rng.randrange(levels)
            else:
                row = np.where(visits[s] > 0, q[s], 0.0)
                a = int(np.argmax(row))
            reward = env.step(a)
            if env.done:
                target = reward
                s_next = -1
            else:
                s_next = encoder.encode(env.state())
                seen = visits[s_next] > 0
                future = float(np.max(q[s_next][seen])) if seen.any() else 0.0
                target = reward + config.gamma * future
            visits[s, a] += 1
            alpha = max(config.alpha_min, (1.0 + visits[s, a]) ** -config.alpha_power)
            q[s, a] += alpha * (target - q[s, a])
            if s_next < 0:
                break
            s = s_next

        if episode % config.eval_every == 0 or episode == config.episodes:
            snapshot = QModel(encoder, q.copy(), visits.copy(), bandwidths, {})
            score = float(
                np.mean(
                    [
                        np.mean(
                            evaluate(manifest, vs, lambda m=snapshot: QLearningAbr(m), sim_config)
                        )
                        for vs in validation_sets.values()
                    ]
                )
            )
            history.append(
                {
                    "episode": episode,
                    "epsilon": round(epsilon, 4),
                    "validation_qoe": round(score, 4),
                }
            )
            marker = ""
            if score > best_score:
                best_score, best = score, snapshot
                marker = "  *best"
            log(
                f"episode {episode:>7}  epsilon {epsilon:.3f}  validation QoE/seg {score:7.3f}  "
                f"states {int((visits.sum(axis=1) > 0).sum())}  "
                f"{time.monotonic() - started:5.0f}s{marker}"
            )

    assert best is not None
    best.metadata = {
        "train": asdict(config),
        "train_traces": {name: len(ts) for name, ts in train_sets.items()},
        "validation_traces": {name: len(ts) for name, ts in validation_sets.items()},
        "best_validation_qoe": round(best_score, 4),
        "history": history,
        "video": manifest.video,
    }
    return best
