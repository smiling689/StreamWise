# StreamWise

Course project for CS3953 Computer Networks — **Topic 8: Streaming Video Transmission System Design and AI-Driven Adaptive Bitrate**.

StreamWise implements TCP-based segmented video transmission with real-time playback and rule-based adaptive bitrate (ABR).

> This project is under development. A full README will be added once it is complete.

## Documents

- Assignment: [docs/assignment.md](docs/assignment.md) ([中文](docs/assignment.zh.md))
- Requirement checklist: [docs/requirements.md](docs/requirements.md) ([中文](docs/requirements.zh.md))

## Getting Started

Requires [Conda](https://docs.conda.io/) (Miniconda is enough). FFmpeg, ffplay and the sqlite3 CLI are installed into the environment, so no system packages are needed.

```bash
conda env create -f environment.yml
conda activate streamwise
pytest
```

To update an existing environment after `environment.yml` changes:

```bash
conda env update -f environment.yml --prune
```

## Repository Layout

```
src/streamwise/
  common/   protocol encoding/decoding and configuration
  server/   TCP segment server, rate shaping, SQLite logging
  client/   segment fetching, playback buffer, player integration, cache
  abr/      adaptive bitrate algorithms
tests/      unit and integration tests
docs/       assignment, requirements, design documents
```

Source videos, generated segments, downloads, logs and experiment outputs are git-ignored and can be regenerated with the project scripts.
