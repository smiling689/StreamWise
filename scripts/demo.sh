#!/usr/bin/env bash
# Live demo: one server and three clients with player windows on a shared link whose
# bandwidth drops suddenly (traces/demo_shared.txt: 30 -> 4 -> 30 Mbit/s).
#
# Usage: scripts/demo.sh [trace] [port]
# Set SEGMENTS=N to stream only the first N segments (quick rehearsal).
# Run from the repository root inside the activated "streamwise" conda environment.
set -euo pipefail

TRACE=${1:-traces/demo_shared.txt}
PORT=${2:-9000}
LOG_DIR=logs/demo-$(date +%Y%m%d-%H%M%S)
mkdir -p "$LOG_DIR"

pids=()
cleanup() {
    trap - INT TERM EXIT
    for pid in "${pids[@]}"; do kill -INT "$pid" 2>/dev/null || true; done
    wait 2>/dev/null || true
    echo "logs: $LOG_DIR"
}
trap cleanup INT TERM EXIT

streamwise-server --port "$PORT" --trace "$TRACE" --shaping-scope global \
    --log-db "$LOG_DIR/server.db" > "$LOG_DIR/server.log" 2>&1 &
pids+=($!)
echo "server log: $LOG_DIR/server.log"

for _ in $(seq 50); do  # wait until the server accepts connections
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
    sleep 0.2
done

start_client() {  # name abr x-position [extra args]
    local name=$1 abr=$2 x=$3; shift 3
    streamwise-client --port "$PORT" --name "$name" --abr "$abr" \
        --window-pos "$x,40" --log-db "$LOG_DIR/client.db" \
        ${SEGMENTS:+--segments "$SEGMENTS"} "$@" &
    pids+=($!)
}
start_client client1 throughput 0
start_client client2 buffer 660 --mute
start_client client3 fixed:3 1320 --mute   # non-adaptive baseline (720p)

# Wait for the three clients (the server keeps running until cleanup).
wait "${pids[@]:1}"
