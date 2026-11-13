#!/usr/bin/env bash
# Download the Norway HSDPA 3G bandwidth traces (Riiser et al., "Commute Path Bandwidth Traces
# from 3G Networks: Analysis and Applications", ACM MMSys 2013) in the cooked
# "<time_s> <mbps>" format distributed with Pensieve-PPO (BSD-2-Clause), pinned to a commit.
#
# Usage: scripts/fetch_hsdpa_traces.sh [dest]   (default: traces/hsdpa; git-ignored)
set -euo pipefail
DEST=${1:-traces/hsdpa}
REPO=https://github.com/godka/Pensieve-PPO.git
COMMIT=a1b2579ca325625a23fe7d329a186ef09e32a3f1

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git -C "$tmp" init -q
git -C "$tmp" remote add origin "$REPO"
git -C "$tmp" sparse-checkout set src/train src/test
git -C "$tmp" fetch -q --depth 1 --filter=blob:none origin "$COMMIT"
git -C "$tmp" checkout -q FETCH_HEAD

mkdir -p "$DEST/train" "$DEST/test"
cp "$tmp"/src/train/* "$DEST/train/"
cp "$tmp"/src/test/* "$DEST/test/"
echo "train: $(ls "$DEST/train" | wc -l) traces, test: $(ls "$DEST/test" | wc -l) traces -> $DEST"
