"""Experiment report: summary table (CSV + Markdown) and charts.

Session PSNR is the mean of the per-segment PSNR of the segments actually played (each
segment has the same duration). Rows are averaged over repeats.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from streamwise.media.quality import QUALITY_FILE, QualityTable  # noqa: E402
from streamwise.server.shaping import BandwidthTrace  # noqa: E402

# Categorical slots 1-3 of the validated reference palette (all-pairs CVD-safe); an ABR keeps
# its color in every chart. Text and axes use neutral inks, never series colors.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
EXTRA_COLORS = ["#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
TRACE_COLOR = "#8a8985"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"

METRICS = [
    ("avg_bitrate_mbps", "Average bitrate (Mbit/s)"),
    ("psnr_db", "PSNR (dB)"),
    ("rebuffer_pct", "Rebuffering ratio (%)"),
    ("stall_count", "Stalls"),
    ("startup_delay_s", "Startup delay (s)"),
    ("switch_count", "Bitrate switches"),
    ("qoe_per_segment", "QoE per segment"),
]


def _style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "axes.edgecolor": GRID,
            "axes.labelcolor": INK_2,
            "axes.titlecolor": INK,
            "axes.titlesize": 11,
            "axes.titleweight": "bold",
            "axes.labelsize": 9,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "xtick.color": INK_2,
            "ytick.color": INK_2,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "legend.frameon": False,
            "legend.fontsize": 8,
            "font.size": 9,
            "savefig.dpi": 160,
            "savefig.bbox": "tight",
        }
    )


def _colors(abrs: list[str]) -> dict[str, str]:
    palette = SERIES_COLORS + EXTRA_COLORS
    if len(abrs) > len(palette):
        raise ValueError("too many ABRs to plot distinctly; split the experiment")
    return {abr: palette[i] for i, abr in enumerate(abrs)}


def load(out_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    config = tomllib.loads((out_dir / "experiment.toml").read_text())
    runs = pd.DataFrame(json.loads((out_dir / "runs.json").read_text()))
    with sqlite3.connect(out_dir / "client.db") as conn:
        sessions = pd.read_sql_query("SELECT * FROM sessions", conn)
        segments = pd.read_sql_query("SELECT * FROM segments", conn)
    sessions = sessions.merge(
        runs[["client_name", "scenario", "abr", "repeat"]],
        on="client_name",
        suffixes=("_impl", ""),
    )
    segments = segments.merge(
        sessions[["session_id", "scenario", "abr", "repeat"]], on="session_id"
    )
    return sessions, segments, config


def summarize(
    sessions: pd.DataFrame, segments: pd.DataFrame, quality: QualityTable | None
) -> pd.DataFrame:
    sessions = sessions.copy()
    if quality is not None:
        segments = segments.copy()
        segments["psnr_db"] = [
            quality.psnr(lv, sq) for lv, sq in zip(segments.level, segments.seq, strict=True)
        ]
        psnr = segments.groupby("session_id")["psnr_db"].mean()
        sessions["psnr_db"] = sessions["session_id"].map(psnr)
    else:
        sessions["psnr_db"] = float("nan")
    sessions["avg_bitrate_mbps"] = sessions["avg_bitrate_kbps"] / 1000
    sessions["rebuffer_pct"] = sessions["rebuffer_ratio"] * 100
    completed = sessions[sessions["status"] == "completed"]
    columns = [m for m, _ in METRICS]
    summary = completed.groupby(["scenario", "abr"], sort=False)[columns].mean()
    summary["runs"] = completed.groupby(["scenario", "abr"], sort=False).size()
    return summary.reset_index()


def to_markdown(summary: pd.DataFrame) -> str:
    header = ["Scenario", "ABR", *[label for _, label in METRICS]]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    fmt = {
        "avg_bitrate_mbps": "{:.2f}",
        "psnr_db": "{:.2f}",
        "rebuffer_pct": "{:.2f}",
        "stall_count": "{:.1f}",
        "startup_delay_s": "{:.2f}",
        "switch_count": "{:.1f}",
        "qoe_per_segment": "{:.3f}",
    }
    for _, row in summary.iterrows():
        cells = [row["scenario"], f"`{row['abr']}`", *[fmt[m].format(row[m]) for m, _ in METRICS]]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def plot_metrics(summary: pd.DataFrame, scenarios: list[str], abrs: list[str], path: Path) -> None:
    chosen = [
        ("rebuffer_pct", "Rebuffering ratio (%)"),
        ("psnr_db", "PSNR (dB)"),
        ("avg_bitrate_mbps", "Average bitrate (Mbit/s)"),
        ("qoe_per_segment", "QoE per segment"),
    ]
    colors = _colors(abrs)
    fig, axes = plt.subplots(2, 2, figsize=(11, 6.6))
    width = 0.8 / len(abrs)
    for ax, (metric, label) in zip(axes.flat, chosen, strict=True):
        for i, abr in enumerate(abrs):
            rows = summary[summary["abr"] == abr].set_index("scenario").reindex(scenarios)
            xs = [x + (i - (len(abrs) - 1) / 2) * width for x in range(len(scenarios))]
            if metric == "psnr_db":
                # PSNR differences are small relative to its magnitude, so the axis cannot
                # start at zero; a dot plot (not bars) keeps a non-zero baseline honest.
                ax.scatter(
                    xs,
                    rows[metric],
                    s=42,
                    color=colors[abr],
                    edgecolor=SURFACE,
                    linewidth=1.5,
                    zorder=3,
                    label=abr,
                )
            else:
                # A surface-colored edge leaves a visible gap between adjacent bars.
                ax.bar(
                    xs,
                    rows[metric],
                    width,
                    color=colors[abr],
                    edgecolor=SURFACE,
                    linewidth=1.5,
                    label=abr,
                )
        ax.set_title(label, loc="left")
        ax.set_xticks(range(len(scenarios)), scenarios)
        ax.grid(axis="x", visible=False)
        values = summary[metric].dropna()
        if metric == "psnr_db":
            if not values.empty:
                ax.set_ylim(values.min() - 1.5, values.max() + 1.5)
        elif metric == "qoe_per_segment":
            ax.axhline(0, color=INK_2, linewidth=0.8)
        else:  # non-negative quantities
            ax.set_ylim(0, max(1.0, float(values.max()) * 1.1) if not values.empty else 1.0)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(abrs), bbox_to_anchor=(0.5, 1.02))
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path)
    plt.close(fig)


def _trace_steps(trace: BandwidthTrace, end: float) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for t, r in zip(trace.times, trace.rates_bps, strict=True):
        if t > end:
            break
        xs.append(t)
        ys.append(r / 1e6)
    xs.append(end)
    ys.append(ys[-1])
    return xs, ys


def plot_timeline(
    segments: pd.DataFrame,
    sessions: pd.DataFrame,
    scenario: str,
    trace: BandwidthTrace,
    abrs: list[str],
    path: Path,
) -> None:
    """Bandwidth and chosen bitrate (top, same unit) and buffer level (bottom) over time."""
    colors = _colors(abrs)
    data = segments[(segments["scenario"] == scenario) & (segments["repeat"] == 0)]
    stalls = sessions[(sessions["scenario"] == scenario) & (sessions["repeat"] == 0)]
    end = float(data["done_t"].max()) + 5 if not data.empty else trace.end_time + 10
    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 2]}
    )
    xs, ys = _trace_steps(trace, end)
    top.step(
        xs, ys, where="post", color=TRACE_COLOR, linewidth=2, linestyle="--", label="link bandwidth"
    )
    for abr in abrs:
        rows = data[data["abr"] == abr].sort_values("seq")
        if rows.empty:
            continue
        t = [*rows["request_t"], float(rows["done_t"].iloc[-1])]
        b = [*(rows["bitrate_kbps"] / 1000), float(rows["bitrate_kbps"].iloc[-1]) / 1000]
        top.step(t, b, where="post", color=colors[abr], linewidth=2, label=abr)
        bottom.plot(
            rows["done_t"],
            rows["buffer_after_s"],
            color=colors[abr],
            linewidth=2,
            marker="o",
            markersize=3,
            label=abr,
        )
        n_stalls = stalls[stalls["abr"] == abr]["stall_count"]
        if not n_stalls.empty and n_stalls.iloc[0] > 0:
            bottom.annotate(
                f"{int(n_stalls.iloc[0])} stall(s)",
                (rows["done_t"].iloc[-1], rows["buffer_after_s"].iloc[-1]),
                textcoords="offset points",
                xytext=(4, 0),
                fontsize=7,
                color=INK_2,
            )
    top.set_title(f"{scenario}: requested bitrate vs. link bandwidth", loc="left")
    top.set_ylabel("Mbit/s")
    top.set_ylim(0, max(ys) * 1.1)
    top.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=len(abrs) + 1)
    bottom.set_title("Buffer level after each download", loc="left")
    bottom.set_ylabel("seconds")
    bottom.set_xlabel("session time (s)")
    bottom.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def build_report(out_dir: Path, media_dir: Path = Path("media")) -> Path:
    _style()
    sessions, segments, config = load(out_dir)
    quality_path = media_dir / config["video"] / QUALITY_FILE
    quality = QualityTable.load(quality_path) if quality_path.exists() else None
    summary = summarize(sessions, segments, quality)
    scenarios = [s["name"] for s in config["scenario"]]
    abrs = list(config["abrs"])
    summary = summary.sort_values(
        ["scenario", "abr"],
        key=lambda col: col.map(
            {n: i for i, n in enumerate(scenarios if col.name == "scenario" else abrs)}
        ),
    ).reset_index(drop=True)

    summary.to_csv(out_dir / "summary.csv", index=False)
    failed = sessions[sessions["status"] != "completed"]
    md = ["# Experiment summary\n", to_markdown(summary)]
    if not failed.empty:
        md.append(f"\n{len(failed)} session(s) did not complete: {', '.join(failed.client_name)}\n")
    (out_dir / "summary.md").write_text("\n".join(md))

    figures = out_dir / "figures"
    figures.mkdir(exist_ok=True)
    plot_metrics(summary, scenarios, abrs, figures / "metrics.png")
    for scenario in config["scenario"]:
        if "trace" in scenario:
            trace = BandwidthTrace.load(Path(scenario["trace"]))
        else:
            trace = BandwidthTrace.constant(float(scenario["rate_mbps"]))
        plot_timeline(
            segments,
            sessions,
            scenario["name"],
            trace,
            abrs,
            figures / f"timeline-{scenario['name']}.png",
        )
    return out_dir / "summary.md"
