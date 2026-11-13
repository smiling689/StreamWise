# StreamWise

Course project for CS3953 Computer Networks — **Topic 8: Streaming Video Transmission System Design and AI-Driven Adaptive Bitrate**.

StreamWise streams segmented video over a custom TCP protocol and plays it in real time. Each client adapts its bitrate with a rule-based algorithm, and the evaluation tooling measures rebuffering, PSNR, and QoE under emulated network conditions.

- **Media pipeline** — FFmpeg turns a video into a 6-level bitrate ladder (240p–1080p) of 5 s MPEG-TS segments. Every segment is checked to be aligned across levels, so segments of different levels can be concatenated.
- **Server** — asyncio TCP server handling many clients at once. A token-bucket bandwidth shaper replays traces, per client or on one shared link, and every transmission is logged to SQLite.
- **Client** — downloader, real-time playback model (buffer, startup, stalls), ffplay player, per-session download cache, and an SQLite session log.
- **ABR** — throughput-based (harmonic mean), buffer-based (BBA-0), and fixed baselines behind a pluggable interface.
- **Evaluation** — parallel scenario runner, bandwidth traces, QoE/PSNR report with charts, a Wireshark dissector, and a three-client live demo.

## Quick Start

```bash
conda env create -f environment.yml
conda activate streamwise
pytest
```

Prepare the media once (see the [user manual](docs/user-manual.md#2-preparing-media)), then:

```bash
streamwise-server --rate-mbps 6           # terminal 1
streamwise-client --name client1          # terminal 2: plays in an ffplay window
scripts/demo.sh                           # or: 3 clients on a shared link with a bandwidth drop
streamwise-experiment run experiments/default.toml   # full evaluation and report
```

## Documents

- [Design document](docs/design.md): architecture, segment protocol, server and client internals, ABR algorithms, and metrics
- [User manual](docs/user-manual.md): setup, usage, demo, experiments, Wireshark, and troubleshooting
- [Test report](docs/test-report.md): rebuffering and PSNR under different bandwidths
- [Requirement checklist](docs/requirements.md) ([中文](docs/requirements.zh.md))
- [Assignment](docs/assignment.md) ([中文](docs/assignment.zh.md))

## Repository Layout

```
src/streamwise/
  common/       protocol, manifest, segment naming, SQLite writer
  media/        segmentation and PSNR measurement (streamwise-media)
  server/       TCP server, catalog, bandwidth shaping, transmission log (streamwise-server)
  client/       session, playback model, player, cache, session log (streamwise-client)
  abr/          ABR interface and rule-based algorithms
  analysis/     session metrics and QoE
  experiments/  scenario runner, traces, report (streamwise-experiment)
experiments/    experiment configurations
traces/         bandwidth traces
scripts/        live demo and packet capture
tools/wireshark Wireshark dissector
tests/          unit, end-to-end, and FFmpeg pipeline tests
docs/           documentation
```

Source videos, segments, downloads, logs, and experiment results are git-ignored. They can all be regenerated with the commands above.

## Media Credit

Big Buck Bunny © 2008 Blender Foundation, www.bigbuckbunny.org, licensed under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).
