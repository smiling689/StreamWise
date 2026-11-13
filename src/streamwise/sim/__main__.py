"""Command-line interface: ``streamwise-sim validate|train|evaluate``."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tomllib
from pathlib import Path

from streamwise.common.manifest import Manifest
from streamwise.experiments.runner import scenario_trace_path
from streamwise.server.shaping import BandwidthTrace
from streamwise.sim import tracesets as ts
from streamwise.sim.env import simulate


def _manifest(path: Path) -> Manifest:
    return Manifest.from_json(path.read_text())


def cmd_validate(args: argparse.Namespace) -> int:
    """Re-run a real experiment in the simulator and compare session by session."""
    from streamwise.abr import create_abr

    manifest = _manifest(args.manifest)
    cfg = tomllib.loads((args.experiment / "experiment.toml").read_text())
    with sqlite3.connect(args.experiment / "client.db") as conn:
        real = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT client_name, avg_bitrate_kbps, stall_time_s, switch_count, "
                "startup_delay_s, qoe_per_segment FROM sessions WHERE status = 'completed'"
            )
        }
    rows = []
    for scenario in cfg["scenario"]:
        trace_path = scenario_trace_path(scenario, Path.cwd(), args.experiment)
        if trace_path is not None:
            trace = BandwidthTrace.load(trace_path)
        else:
            trace = BandwidthTrace.constant(float(scenario["rate_mbps"]))
        for spec in cfg["abrs"]:
            name = f"{scenario['name']}.{re.sub(r'[^A-Za-z0-9]+', '_', spec).strip('_')}.0"
            if name not in real:
                continue
            sim = simulate(manifest, trace, create_abr(spec)).metrics
            br, stall, switches, startup, qoe = real[name]
            rows.append(
                (
                    scenario["name"],
                    spec,
                    br,
                    sim.avg_bitrate_kbps,
                    stall,
                    sim.stall_time_s,
                    switches,
                    sim.switch_count,
                    qoe,
                    sim.qoe_per_segment,
                )
            )
    lines = [
        "| Scenario | ABR | Bitrate real / sim (kbps) | Stall real / sim (s) "
        "| Switches real / sim | QoE/seg real / sim |",
        "|---|---|---|---|---|---|",
    ]
    same = 0
    diffs = []
    for sc, spec, br, sbr, st, sst, sw, ssw, q, sq in rows:
        same += round(br) == round(sbr) and sw == ssw
        diffs.append(abs(q - sq))
        lines.append(
            f"| {sc} | `{spec}` | {br:.0f} / {sbr:.0f} | {st:.2f} / {sst:.2f} | "
            f"{sw} / {ssw} | {q:.3f} / {sq:.3f} |"
        )
    diffs.sort()
    summary = (
        f"{len(rows)} sessions; identical bitrate and switch count in {same}; "
        f"|QoE/seg difference| median {diffs[len(diffs) // 2]:.4f}, max {diffs[-1]:.4f}"
    )
    print("\n".join(lines))
    print(f"\n{summary}")
    return 0


def _load_sets(
    args: argparse.Namespace, split: str, synthetic: int
) -> dict[str, list[ts.NamedTrace]]:
    sets: dict[str, list[ts.NamedTrace]] = {}
    if synthetic:
        sets["synthetic"] = ts.synthetic_set(split, synthetic)
    if args.hsdpa_dir is not None:
        sets["hsdpa"] = ts.measured_split(args.hsdpa_dir, split)
    if not sets:
        raise ValueError(f"empty {split} set")
    return sets


def cmd_train(args: argparse.Namespace) -> int:
    from streamwise.sim.train import TrainConfig, train

    manifest = _manifest(args.manifest)
    train_set = _load_sets(args, "train", args.synthetic_train)
    validation_set = _load_sets(args, "validation", args.synthetic_validation)

    def describe(sets: dict[str, list[ts.NamedTrace]]) -> str:
        return ", ".join(f"{len(v)} {k}" for k, v in sets.items())

    print(f"training on {describe(train_set)}; validating on {describe(validation_set)}")
    config = TrainConfig(
        episodes=args.episodes,
        gamma=args.gamma,
        alpha_min=args.alpha_min,
        eval_every=args.eval_every,
        seed=args.seed,
    )
    model = train(manifest, train_set, validation_set, config)
    model.metadata["hsdpa"] = args.hsdpa_dir is not None
    model.save(args.out)
    print(f"saved {args.out} (best validation QoE/seg {model.metadata['best_validation_qoe']})")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from streamwise.sim.evaluate import evaluate_sets, write_report

    manifest = _manifest(args.manifest)
    sets = {}
    if args.hsdpa_dir is not None:
        sets["hsdpa_test"] = ts.measured_split(args.hsdpa_dir, "test")
    if args.synthetic_test:
        sets["synthetic_test"] = ts.synthetic_set("test", args.synthetic_test)
    results = evaluate_sets(manifest, sets, args.abr)
    path = write_report(results, args.abr, args.out, args.reference)
    print(path.read_text())
    return 0


def cmd_curves(args: argparse.Namespace) -> int:
    from streamwise.sim.evaluate import plot_training_curves

    plot_training_curves(args.models, args.out)
    print(f"wrote {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamwise-sim")
    parser.add_argument("--manifest", type=Path, default=Path("media/bbb/manifest.json"))
    sub = parser.add_subparsers(dest="command", required=True)

    val = sub.add_parser("validate", help="compare the simulator with a real experiment")
    val.add_argument("experiment", type=Path, help="experiment output directory")

    tr = sub.add_parser("train", help="train the Q-learning ABR")
    tr.add_argument("--out", type=Path, default=Path("models/qlearning-bbb.json"))
    tr.add_argument("--hsdpa-dir", type=Path, default=Path("traces/hsdpa"))
    tr.add_argument("--synthetic-train", type=int, default=1000)
    tr.add_argument("--synthetic-validation", type=int, default=100)
    tr.add_argument("--episodes", type=int, default=400_000)
    tr.add_argument("--gamma", type=float, default=0.90)
    tr.add_argument("--alpha-min", type=float, default=0.005)
    tr.add_argument("--eval-every", type=int, default=20_000)
    tr.add_argument("--seed", type=int, default=1)

    ev = sub.add_parser("evaluate", help="evaluate ABRs on the held-out test traces")
    ev.add_argument("--abr", action="append", required=True, help="ABR spec (repeatable)")
    ev.add_argument("--reference", help="ABR compared pairwise with the others (default: first)")
    ev.add_argument("--hsdpa-dir", type=Path, default=Path("traces/hsdpa"))
    ev.add_argument("--synthetic-test", type=int, default=200)
    ev.add_argument("--out", type=Path, default=Path("results/sim-eval"))

    cu = sub.add_parser("curves", help="plot validation curves stored in trained models")
    cu.add_argument("models", type=Path, nargs="+")
    cu.add_argument("--out", type=Path, default=Path("results/training-curves.png"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("train", "evaluate") and args.hsdpa_dir and not args.hsdpa_dir.exists():
        print(
            f"note: {args.hsdpa_dir} missing, using synthetic traces only "
            "(scripts/fetch_hsdpa_traces.sh downloads them)",
            file=sys.stderr,
        )
        args.hsdpa_dir = None
    handlers = {
        "validate": cmd_validate,
        "train": cmd_train,
        "evaluate": cmd_evaluate,
        "curves": cmd_curves,
    }
    try:
        return handlers[args.command](args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
