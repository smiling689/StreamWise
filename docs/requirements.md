# StreamWise Requirement Checklist

> Derived from the [assignment](assignment.md). Every item has an ID so commits, issues and the test report can reference it (e.g. `S-02`).
> Status: `[ ]` todo, `[~]` in progress, `[x]` done.
> Chinese version: [requirements.zh.md](requirements.zh.md).

## Scope

The mandatory part plus three extensions worth 50 points in total (E-01, E-02, E-03; see §8). The bonus is capped at 25 points, so the extra extensions are insurance against partial credit.

## 0. Technical Choices

| Item | Choice | Rationale |
| --- | --- | --- |
| Language | Python 3.12 | Allowed by the assignment; fastest path for player integration, ABR and data analysis |
| Environment | Conda (`environment.yml`) | Reproducible; provides FFmpeg without root access |
| Concurrency | `asyncio` server (one task per connection) | Concurrent clients without thread bookkeeping |
| Segmentation | FFmpeg multi-bitrate transcode + 5 s TS segments | Required by the assignment |
| Player | `ffplay` (or `mpv`) reading a TS stream from stdin | TS segments can be concatenated directly; WSLg provides a display |
| Logging | SQLite (`sqlite3` stdlib module) | Required by the assignment |
| Network emulation | Application-level token-bucket shaper with trace replay (primary, reproducible); Linux `tc tbf/netem` (secondary, for demos) | |
| Packet capture | Wireshark / tshark | Required by the assignment |

## 1. Media Preparation (offline, server side)

- [x] **V-01** Prepare a source video (≥ 2 min, 1080p, varied content so quality differences are visible), e.g. `ocean`
- [x] **V-02** Define the bitrate ladder, e.g.:

  | Level | Resolution | Bitrate |
  | --- | --- | --- |
  | 0 | 240p | 400k |
  | 1 | 360p | 800k |
  | 2 | 480p | 1200k |
  | 3 | 720p | 2500k |
  | 4 | 1080p | 5000k |
  | 5 | 1080p | 8000k |

- [x] **V-03** Transcode every level with FFmpeg and cut into **fixed 5 s** TS segments. Use a fixed GOP so segment boundaries align across levels, making bitrate switches seamless
- [x] **V-04** Segment naming: `{name}-{resolution}-{bitrate}-{seq}.ts`, e.g. `ocean-1080p-8000k-0.ts`
- [x] **V-05** Generate a JSON manifest: video name, segment duration, segment count, bitrate ladder, per-segment sizes. The client fetches it first
- [x] **V-06** One-command segmentation: `streamwise-media segment <input> --name <name>`

## 2. Server (mandatory)

- [x] **S-01** Listen on a TCP port and accept client connections
- [x] **S-02** Serve multiple clients concurrently (≥ 3 simultaneous clients, no head-of-line blocking between them)
- [x] **S-03** Implement the application protocol (§4): fetch manifest, request segment by name, error responses (not found / malformed)
- [x] **S-04** Log every segment transmission to SQLite: client ID, segment name, sequence number, bitrate, byte count, send start/end time, throughput
- [x] **S-05** Log export (CSV) or query script for the test report
- [x] **S-06** Configurable application-level rate limiting (token bucket) that can replay a bandwidth trace — used for the bandwidth-drop demo and reproducible experiments
- [x] **S-07** Graceful shutdown; a client disconnecting mid-transfer must not affect other clients

## 3. Client (mandatory)

- [x] **C-01** Connect to the server and fetch the manifest
- [x] **C-02** Request segments in a loop, building the file name from `name-resolution-bitrate-seq`
- [x] **C-03** Store segments in the `download/` directory
- [x] **C-04** Feed received segments to the player for real-time playback (downloader and player decoupled by a buffer)
- [x] **C-05** Playback buffer model: buffer level, rebuffering detection and timing, startup delay
- [x] **C-06** **Rule-based ABR** — implement two for comparison:
  - Throughput-based: harmonic mean of the last N segment throughputs × safety factor
  - Buffer-based (BBA): lowest level below the reservoir, highest above the cushion, linear mapping in between
- [x] **C-07** Common ABR interface `choose(state) -> level` so algorithms are interchangeable
- [x] **C-08** Cache management: delete each segment after it has been played; clear `download/` on exit
- [x] **C-09** Client-side log per segment: chosen level, download time, measured throughput, buffer level, rebuffer time (for QoE computation)
- [x] **C-10** Headless mode: no player window, simulated playback clock — for batch experiments

## 4. Segment Protocol (needed for the design document)

- [x] **P-01** Define the message format, e.g. (big-endian):

  ```
  +--------+---------+--------+--------+-------------+----------------+
  | magic  | version | type   | flags  | request_id  | payload_len    |
  | 2 B    | 1 B     | 1 B    | 1 B    | 4 B         | 4 B            |
  +--------+---------+--------+--------+-------------+----------------+
  | payload (payload_len bytes)                                       |
  +-------------------------------------------------------------------+
  ```

  Types: `GET_MANIFEST` / `MANIFEST` / `GET_SEGMENT` / `SEGMENT` / `ERROR` / `BYE`
- [x] **P-02** Optional integrity check for segment payloads (CRC32 / SHA-256)
- [x] **P-03** Message format diagram and interaction sequence diagram in the design document

## 5. Testing and Evaluation (mandatory)

- [x] **T-01** Network scenarios: fixed bandwidth (e.g. 1 / 2 / 4 / 8 Mbps), sudden drop (8 → 1 Mbps), fluctuating traces
- [x] **T-02** Metrics:
  - Rebuffering ratio (rebuffer time / total playback time), rebuffer count, startup delay
  - Average bitrate, number and magnitude of switches
  - **PSNR**: compute each level's PSNR against the source with `ffmpeg -lavfi psnr`; weight by the levels actually played to get session PSNR
  - QoE (linear model): `QoE = Σ q(R_k) − μ·Σ rebuffer_k − Σ |q(R_{k+1}) − q(R_k)|`
- [x] **T-03** Compare the ABR algorithms across bandwidths and produce charts
- [~] **T-04** Wireshark capture showing the protocol exchange and TCP throughput behaviour

## 6. Demo (week 16)

- [x] **D-01** At least **3 clients** streaming and playing simultaneously
- [x] **D-02** Simulated sudden bandwidth drop with visible bitrate switching (live terminal status: level, throughput, buffer; optionally a live chart)
- [ ] **D-03** Recorded demo video as a backup

## 7. Deliverables

- [x] **R-01** Complete source code (server, client, segmentation, ABR)
- [x] **R-02** Design document: system architecture diagram, segment protocol format, ABR flowchart
- [x] **R-03** Test report: rebuffering ratio and PSNR under different bandwidths
- [x] **R-04** User manual: environment setup and player debugging
- [ ] **R-05** Academic-conference-style poster
- [ ] **R-06** Demo materials: screenshots, videos

## 8. Extensions

| ID | Extension | Points | Status |
| --- | --- | --- | --- |
| E-01 | Intelligent ABR (Q-Learning) | 25 | done — [docs/intelligent-abr.md](intelligent-abr.md) |
| E-02 | End-to-end AES encryption | 10 | done — [docs/encryption.md](encryption.md) |
| E-03 | Sliding-window streaming | 15 | in scope |
| E-04 | Dynamic path optimization | 25 | not planned |
| E-05 | Student-defined open problem | 10–25 | not planned (proposal deadline passed) |

**E-01 Intelligent ABR**

- [x] **E-01a** Trace-driven simulator mirroring the real client and server, validated against real sessions
- [x] **E-01b** Tabular Q-learning agent behind the common ABR interface (state: previous level, buffer, throughput estimate, trend; reward: per-segment QoE)
- [x] **E-01c** Training on synthetic and real 3G traces, with model selection on a validation split only
- [x] **E-01d** Trained model runs unchanged in the real client (`--abr qlearning`)
- [x] **E-01e** QoE comparison with the rule-based algorithms on held-out traces (simulator) and in the real system

**E-02 End-to-end AES encryption**

- [x] **E-02a** Key exchange inside the handshake (X25519 + HKDF-SHA256), fresh keys per connection, optional pre-shared key for authentication
- [x] **E-02b** Every message after the handshake encrypted with AES-256-GCM; header authenticated; replay and reordering detected
- [x] **E-02c** Server modes off / optional / required; clear errors for missing encryption and wrong keys
- [x] **E-02d** Tests for tampering, replay, wrong keys and plaintext leakage; Wireshark shows encrypted messages
- [x] **E-02e** Overhead measured (CPU, bytes, unshaped throughput) and QoE compared with plaintext streaming

## Milestones

1. **M1 Environment + segmentation** — conda environment, multi-bitrate FFmpeg segmentation, manifest (V-*)
2. **M2 End-to-end streaming** — protocol, server, client, playback, cache cleanup (S-01–03, C-01–04, C-08)
3. **M3 ABR, logging, shaping** (S-04–07, C-05–07, C-09–10)
4. **M4 Experiments and evaluation** (T-*)
5. **M5 Documentation, poster, demo rehearsal** (D-*, R-*)
6. **M6 Extensions** — E-01 Q-learning ABR, E-02 AES encryption, E-03 sliding-window streaming
