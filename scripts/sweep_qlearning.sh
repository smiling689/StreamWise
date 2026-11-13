#!/usr/bin/env bash
# Q-learning hyperparameter sweep. Each configuration is trained in parallel and selected on
# the validation traces only; compare the "best validation QoE/seg" lines in the logs.
#
# Usage: scripts/sweep_qlearning.sh [out_dir]   (default: results/sweep; git-ignored)
set -euo pipefail
OUT=${1:-results/sweep}
mkdir -p "$OUT"

run() {
    local name=$1; shift
    streamwise-sim train --out "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
    echo "$name: $(grep -o 'best validation QoE/seg [-0-9.]*' "$OUT/$name.log")"
}

run gamma0.95_alpha0.005 --gamma 0.95 --alpha-min 0.005 &
run gamma0.90_alpha0.005 --gamma 0.90 --alpha-min 0.005 &
run gamma0.99_alpha0.005 --gamma 0.99 --alpha-min 0.005 &
run gamma0.95_alpha0.02  --gamma 0.95 --alpha-min 0.02 &
wait
