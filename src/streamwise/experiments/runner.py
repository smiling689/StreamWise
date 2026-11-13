"""Experiment configuration and runner.

An experiment is a set of network *scenarios* (constant rate or bandwidth trace) times a set
of ABR algorithms. Every scenario gets its own server process with per-connection shaping, so
each client sees exactly the scenario's bandwidth, with the trace clock starting at its own
connection. All clients run headless and concurrently as separate processes, so a whole
experiment takes about as long as its slowest session.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import signal
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from streamwise.server.shaping import BandwidthTrace


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    rate_mbps: float | None = None
    trace: str | None = None
    measured_trace: str | None = None
    """A measured trace (e.g. HSDPA): normalized and looped into the output directory first."""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.name):
            raise ValueError(f"scenario {self.name!r}: use letters, digits, underscores")
        if sum(x is not None for x in (self.rate_mbps, self.trace, self.measured_trace)) != 1:
            raise ValueError(
                f"scenario {self.name!r}: set exactly one of rate_mbps, trace, measured_trace"
            )

    def server_args(self, base_dir: Path, out_dir: Path) -> list[str]:
        if self.rate_mbps is not None:
            return ["--rate-mbps", str(self.rate_mbps)]
        return ["--trace", str(scenario_trace_path(asdict(self), base_dir, out_dir))]


def scenario_trace_path(scenario: dict, base_dir: Path, out_dir: Path) -> Path | None:
    """Trace file a scenario replays (``None`` for a constant rate)."""
    if scenario.get("trace"):
        return (base_dir / scenario["trace"]).resolve()
    if scenario.get("measured_trace"):
        return (out_dir / "traces" / f"{scenario['name']}.txt").resolve()
    return None


def prepare_measured_trace(scenario: Scenario, base_dir: Path, out_dir: Path) -> None:
    from streamwise.sim.tracesets import MIN_TRACE_S

    assert scenario.measured_trace is not None
    source = base_dir / scenario.measured_trace
    trace = BandwidthTrace.load_samples(source).repeated(MIN_TRACE_S)
    target = scenario_trace_path(asdict(scenario), base_dir, out_dir)
    assert target is not None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(trace.to_text(f"Normalized and looped from {scenario.measured_trace}"))


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    video: str
    abrs: tuple[str, ...]
    scenarios: tuple[Scenario, ...]
    repeats: int = 1
    max_buffer_s: float = 30.0
    startup_buffer_s: float = 5.0
    segments: int | None = None
    base_port: int = 9200
    media_dir: str = "media"
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ExperimentConfig:
        data = tomllib.loads(path.read_text())
        scenarios = tuple(Scenario(**s) for s in data.pop("scenario"))
        names = [s.name for s in scenarios]
        if len(set(names)) != len(names):
            raise ValueError("scenario names must be unique")
        return cls(
            video=data.pop("video"),
            abrs=tuple(data.pop("abrs")),
            scenarios=scenarios,
            repeats=int(data.pop("repeats", 1)),
            max_buffer_s=float(data.pop("max_buffer_s", 30.0)),
            startup_buffer_s=float(data.pop("startup_buffer_s", 5.0)),
            segments=data.pop("segments", None),
            base_port=int(data.pop("base_port", 9200)),
            media_dir=str(data.pop("media_dir", "media")),
            extra=data,
        )


@dataclass(frozen=True, slots=True)
class Run:
    scenario: str
    abr: str
    repeat: int
    client_name: str
    exit_code: int | None = None
    elapsed_s: float | None = None


def abr_slug(spec: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", spec).strip("_")


async def _wait_for_port(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"server on port {port} did not start") from None
            await asyncio.sleep(0.2)
        else:
            writer.close()
            await writer.wait_closed()
            return


async def run_experiment(config: ExperimentConfig, config_path: Path, out_dir: Path) -> list[Run]:
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} already exists; choose a new output directory")
    logs = out_dir / "logs"
    logs.mkdir(parents=True)
    shutil.copy(config_path, out_dir / "experiment.toml")
    base_dir = Path.cwd()  # paths in the config are relative to the working directory
    media_dir = (base_dir / config.media_dir).resolve()
    python = sys.executable

    for scenario in config.scenarios:
        if scenario.measured_trace is not None:
            prepare_measured_trace(scenario, base_dir, out_dir)

    servers: list[asyncio.subprocess.Process] = []
    server_logs = []
    try:
        ports: dict[str, int] = {}
        for i, scenario in enumerate(config.scenarios):
            port = config.base_port + i
            ports[scenario.name] = port
            log_file = (logs / f"server-{scenario.name}.log").open("wb")
            server_logs.append(log_file)
            servers.append(
                await asyncio.create_subprocess_exec(
                    python,
                    "-m",
                    "streamwise.server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--media-dir",
                    str(media_dir),
                    "--log-db",
                    str(out_dir / f"server-{scenario.name}.db"),
                    "--shaping-scope",
                    "connection",
                    *scenario.server_args(base_dir, out_dir),
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                )  # fmt: skip
            )
        await asyncio.gather(*(_wait_for_port(p, 15) for p in ports.values()))

        async def run_client(scenario: Scenario, abr: str, repeat: int) -> Run:
            name = f"{scenario.name}.{abr_slug(abr)}.{repeat}"
            args = [
                python, "-m", "streamwise.client",
                "--host", "127.0.0.1", "--port", str(ports[scenario.name]),
                "--video", config.video, "--name", name, "--abr", abr,
                "--player", "none", "--max-buffer", str(config.max_buffer_s),
                "--startup-buffer", str(config.startup_buffer_s),
                "--log-db", str(out_dir / "client.db"),
                "--download-dir", str(out_dir / "download"),
            ]  # fmt: skip
            if config.segments:
                args += ["--segments", str(config.segments)]
            started = time.monotonic()
            with (logs / f"client-{name}.log").open("wb") as log_file:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdout=log_file, stderr=asyncio.subprocess.STDOUT
                )
                code = await proc.wait()
            elapsed = time.monotonic() - started
            status = "ok" if code == 0 else f"FAILED ({code})"
            print(f"  {name:<40} {status:<12} {elapsed:6.0f}s", flush=True)
            return Run(scenario.name, abr, repeat, name, code, elapsed)

        jobs = [
            run_client(s, a, r)
            for r in range(config.repeats)
            for s in config.scenarios
            for a in config.abrs
        ]
        print(f"running {len(jobs)} sessions on {len(servers)} servers ...", flush=True)
        runs = await asyncio.gather(*jobs)
    finally:
        for proc in servers:
            if proc.returncode is None:
                proc.send_signal(signal.SIGINT)
        for proc in servers:
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for log_file in server_logs:
            log_file.close()

    (out_dir / "runs.json").write_text(json.dumps([asdict(r) for r in runs], indent=2) + "\n")
    return list(runs)
