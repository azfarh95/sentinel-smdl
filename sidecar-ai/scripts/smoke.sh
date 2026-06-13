#!/usr/bin/env bash
# Smoke test for the media-ai sidecar. Hits /healthz, then transcribes a media
# file under the downloads root and prints the transcript + speed.
#
#   BASE=http://localhost:8097 FILE="Instagram/sherwx/3893755561468095168.mp4" \
#     ./scripts/smoke.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8097}"
FILE="${FILE:-}"
MODEL="${MODEL:-base}"

echo "== /healthz =="
curl -fsS "$BASE/healthz"; echo

if [[ -z "$FILE" ]]; then
  echo "(set FILE=<path under downloads root> to test /transcribe)"
  exit 0
fi

echo "== /transcribe ($FILE, model=$MODEL) =="
curl -fsS "$BASE/transcribe" \
  -H 'content-type: application/json' \
  -d "{\"path\":\"$FILE\",\"model\":\"$MODEL\"}"
echo
