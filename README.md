# StreamWise

Course project for CS3953 Computer Networks — **Topic 8: Streaming Video Transmission System Design and AI-Driven Adaptive Bitrate**.

StreamWise streams segmented video over a custom TCP protocol and plays it in real time. Each client adapts its bitrate with a rule-based or a learned (Q-learning) algorithm, and the evaluation tooling measures rebuffering, PSNR, and QoE under emulated network conditions. Besides the mandatory part it implements three extensions: intelligent ABR (E-01), end-to-end AES encryption (E-02) and sliding-window streaming (E-03).

- **Media pipeline** — FFmpeg turns a video into a 6-level bitrate ladder (240p–1080p) of 5 s MPEG-TS segments. Every segment is checked to be aligned across levels, so segments of different levels can be concatenated.
- **Server** — asyncio TCP server handling many clients at once. A token-bucket bandwidth shaper replays traces, per client or on one shared link, and every transmission is logged to SQLite.
- **Client** — downloader, real-time playback model (buffer, startup, stalls), ffplay player, per-session download cache, and an SQLite session log.
- **ABR** — throughput-based (harmonic mean), buffer-based (BBA-0), and fixed baselines behind a pluggable interface.
- **Q-learning ABR** (extension) — trained offline in a simulator that is validated against the real system; evaluated on held-out real 3G traces, where it halves the share of sessions that stall.
- **Encryption** (extension) — optional end-to-end AES-256-GCM with an X25519 key exchange in the handshake and an optional pre-shared key; tampering, replay and wrong keys are detected.
- **Sliding-window streaming** (extension) — segments fetched as chunks through a receiver-side sliding window over several connections; long streams keep playing through connection resets.
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

Extensions:

```bash
streamwise-client --name c1 --abr qlearning                       # E-01: trained Q-learning ABR
streamwise-server --encryption required                           # E-02: encrypted connections only
streamwise-client --name c2 --encrypt
streamwise-server --rate-mbps 4 --shaping-scope client --reset-after-s 15   # E-03: connection faults
streamwise-client --name c3 --transport window --connections 2
```

## Results at a Glance

| Topic | Result | Details |
| --- | --- | --- |
| Shaping accuracy | Measured throughput within 1 % of the configured rate | [test report §2](docs/test-report.md#2-functional-verification) |
| Rule-based ABR | BBA (10 s reservoir) has no stall in all six scenarios; throughput-based ABR stalls 5 times after a bandwidth drop | [test report §3](docs/test-report.md#3-abr-comparison-under-different-bandwidths) |
| Q-learning ABR | On 142 held-out real 3G traces, 29.6 % of sessions stall vs. 52.8 % (throughput) and 61.3 % (BBA); best mean QoE in the real system | [intelligent-abr.md](docs/intelligent-abr.md) |
| Encryption | Identical ABR decisions in all 18 encrypted sessions; 1.5 ms to encrypt a 5 MB segment | [encryption.md](docs/encryption.md) |
| Sliding window | A 10-minute stream plays without a stall while every connection is reset after 10 s; the segment transport fails at the first reset | [sliding-window.md](docs/sliding-window.md) |

## Documents

- [Design document](docs/design.md): architecture, segment protocol, server and client internals, ABR algorithms, and metrics
- [User manual](docs/user-manual.md): setup, usage, demo, experiments, Wireshark, and troubleshooting
- [Test report](docs/test-report.md): rebuffering and PSNR under different bandwidths
- [Intelligent ABR](docs/intelligent-abr.md): Q-learning ABR, simulator, training and evaluation (extension E-01)
- [Encryption](docs/encryption.md): AES-256-GCM handshake, record format, tests and cost (extension E-02)
- [Sliding window](docs/sliding-window.md): chunked transport, selective repeat, window/RTT and fault evaluation (extension E-03)
- [Requirement checklist](docs/requirements.md) ([中文](docs/requirements.zh.md))
- [Assignment](docs/assignment.md) ([中文](docs/assignment.zh.md))

## Repository Layout

```
src/streamwise/
  common/       protocol, encryption, manifest, segment naming, SQLite writer
  media/        segmentation and PSNR measurement (streamwise-media)
  server/       TCP server, catalog, bandwidth shaping, transmission log (streamwise-server)
  client/       session, playback model, player, cache, sliding window, session log (streamwise-client)
  abr/          ABR interface, rule-based algorithms, Q-learning agent
  analysis/     session metrics and QoE
  sim/          trace-driven simulator, Q-learning training and evaluation (streamwise-sim)
  experiments/  scenario runner, traces, report, encryption and window benchmarks (streamwise-experiment)
experiments/    experiment configurations
models/         trained Q-learning model
traces/         bandwidth traces
scripts/        live demo, packet capture, 3G trace download, Q-learning sweep
tools/wireshark Wireshark dissector
tests/          unit, end-to-end, encryption, sliding-window, and FFmpeg pipeline tests
docs/           documentation
```

Source videos, segments, downloads, logs, measured 3G traces, pre-shared keys, and experiment results are git-ignored. They can all be regenerated with the commands in the user manual.

## Media Credit

Big Buck Bunny © 2008 Blender Foundation, www.bigbuckbunny.org, licensed under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).
