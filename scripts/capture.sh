#!/usr/bin/env bash
# Capture StreamWise traffic on the loopback interface for Wireshark analysis.
# Usage: scripts/capture.sh [port] [output.pcapng]   (stop with Ctrl-C)
# Requires capture rights: sudo usermod -aG wireshark "$USER" (then log in again).
set -euo pipefail
PORT=${1:-9000}
OUT=${2:-logs/capture-$(date +%Y%m%d-%H%M%S).pcapng}
mkdir -p "$(dirname "$OUT")"
echo "capturing tcp port $PORT on lo -> $OUT"
# Full packets are needed so Wireshark can reassemble messages that span TCP segments.
tshark -i lo -f "tcp port $PORT" -w "$OUT"
