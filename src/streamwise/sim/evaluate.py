"""Large-scale ABR evaluation in the simulator: tables, paired comparison and CDF plots."""

from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from streamwise.abr import QLearningAbr, create_abr
from streamwise.analysis.metrics import SessionMetrics
from streamwise.common.manifest import Manifest
from streamwise.media.quality import QUALITY_FILE, QualityTable
from streamwise.sim.env import SimConfig, simulate
from streamwise.sim.tracesets import NamedTrace


@dataclass(frozen=True, slots=True)
class Run:
    trace: str
    metrics: SessionMetrics
    psnr_db: float
    fallback_rate: float


def evaluate_sets(
    manifest: Manifest,
    sets: dict[str, Sequence[NamedTrace]],
    abr_specs: Sequence[str],
    config: SimConfig | None = None,
    media_dir: Path = Path("media"),
) -> dict[str, dict[str, list[Run]]]:
    quality_path = media_dir / manifest.video / QUALITY_FILE
    quality = QualityTable.load(quality_path) if quality_path.exists() else None
    results: dict[str, dict[str, list[Run]]] = {}
    for set_name, traces in sets.items():
        results[set_name] = {}
        for spec in abr_specs:
            runs = []
            abr = create_abr(spec)
            for named in traces:
                result = simulate(manifest, named.trace, abr, config)
                psnr = (
                    float(np.mean([quality.psnr(s.level, s.seq) for s in result.segments]))
                    if quality
                    else math.nan
                )
                fallback = (
                    abr.fallbacks / max(1, abr.decisions) if isinstance(abr, QLearningAbr) else 0.0
                )
                runs.append(Run(named.name, result.metrics, psnr, fallback))
            results[set_name][spec] = runs
    return results


def _mean_ci(values: Sequence[float]) -> tuple[float, float]:
    """Mean and half-width of the 95 % confidence interval (normal approximation)."""
    arr = np.asarray(values, dtype=float)
    if len(arr) < 2:
        return float(arr.mean()), 0.0
    return float(arr.mean()), float(1.96 * arr.std(ddof=1) / math.sqrt(len(arr)))


def write_report(
    results: dict[str, dict[str, list[Run]]], abr_specs: Sequence[str], out_dir: Path
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from streamwise.experiments.report import SERIES_COLORS, _style

    out_dir.mkdir(parents=True, exist_ok=True)
    _style()
    colors = {spec: SERIES_COLORS[i % len(SERIES_COLORS)] for i, spec in enumerate(abr_specs)}
    lines = ["# Simulation evaluation on held-out traces\n"]

    with (out_dir / "sessions.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "set",
                "abr",
                "trace",
                "qoe_per_segment",
                "avg_bitrate_kbps",
                "psnr_db",
                "rebuffer_ratio",
                "stall_count",
                "switch_count",
                "startup_delay_s",
                "fallback_rate",
            ]
        )
        for set_name, by_abr in results.items():
            for spec, runs in by_abr.items():
                for r in runs:
                    m = r.metrics
                    writer.writerow(
                        [
                            set_name,
                            spec,
                            r.trace,
                            m.qoe_per_segment,
                            m.avg_bitrate_kbps,
                            r.psnr_db,
                            m.rebuffer_ratio,
                            m.stall_count,
                            m.switch_count,
                            m.startup_delay_s,
                            r.fallback_rate,
                        ]
                    )

    fig, axes = plt.subplots(1, len(results), figsize=(5.5 * len(results), 4), squeeze=False)
    for ax, (set_name, by_abr) in zip(axes[0], results.items(), strict=True):
        n = len(next(iter(by_abr.values())))
        lines.append(f"## {set_name} ({n} traces)\n")
        lines.append(
            "| ABR | QoE/seg (mean ± 95% CI) | Avg bitrate (Mbit/s) | PSNR (dB) | "
            "Rebuffering (%) | Stalls | Switches | Startup (s) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for spec in abr_specs:
            runs = by_abr[spec]
            q, ci = _mean_ci([r.metrics.qoe_per_segment for r in runs])
            avg = lambda f, runs=runs: float(np.mean([f(r) for r in runs]))  # noqa: E731
            fallback = avg(lambda r: r.fallback_rate)
            note = f" (fallback {fallback:.1%})" if fallback > 0 else ""
            lines.append(
                f"| `{spec}`{note} | {q:.3f} ± {ci:.3f} | "
                f"{avg(lambda r: r.metrics.avg_bitrate_kbps) / 1000:.2f} | "
                f"{avg(lambda r: r.psnr_db):.2f} | "
                f"{100 * avg(lambda r: r.metrics.rebuffer_ratio):.2f} | "
                f"{avg(lambda r: r.metrics.stall_count):.2f} | "
                f"{avg(lambda r: r.metrics.switch_count):.1f} | "
                f"{avg(lambda r: r.metrics.startup_delay_s):.2f} |"
            )
            values = np.sort([r.metrics.qoe_per_segment for r in runs])
            ax.step(
                values,
                np.arange(1, len(values) + 1) / len(values),
                where="post",
                color=colors[spec],
                linewidth=2,
                label=spec,
            )
        ax.set_title(f"{set_name}: QoE per segment (CDF)", loc="left")
        ax.set_xlabel("QoE per segment (higher is better)")
        ax.set_ylabel("fraction of traces")
        ax.set_ylim(0, 1.02)

        # Paired comparison of the first ABR against each other one (same traces).
        first = abr_specs[0]
        lines.append("")
        lines.append(f"Paired comparison of `{first}` against each baseline on the same traces:\n")
        lines.append("| Baseline | Mean QoE/seg gain (95% CI) | Wins | Ties | Losses |")
        lines.append("|---|---|---|---|---|")
        a = np.array([r.metrics.qoe_per_segment for r in by_abr[first]])
        for spec in abr_specs[1:]:
            b = np.array([r.metrics.qoe_per_segment for r in by_abr[spec]])
            diff = a - b
            mean, ci = _mean_ci(diff)
            wins = int((diff > 1e-6).sum())
            losses = int((diff < -1e-6).sum())
            lines.append(
                f"| `{spec}` | {mean:+.3f} ({mean - ci:+.3f} … {mean + ci:+.3f}) | "
                f"{wins} | {len(diff) - wins - losses} | {losses} |"
            )
        lines.append("")
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(labels), bbox_to_anchor=(0.5, 1.02))
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out_dir / "qoe-cdf.png")
    plt.close(fig)
    path = out_dir / "summary.md"
    path.write_text("\n".join(lines) + "\n")
    return path
