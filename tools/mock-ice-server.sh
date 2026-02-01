#!/usr/bin/env bash
set -euo pipefail

ICECAST_CONTAINER="icecast-test"
ICECAST_IMAGE="docker.io/moul/icecast"
STREAM_LIQ="${1:-stream.liq}"

########################################
# Dependency Check
########################################
check_deps() {
  local missing=0

  for cmd in podman liquidsoap yt-dlp curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "Missing dependency: $cmd"
      missing=1
    fi
  done

  if [[ $missing -ne 0 ]]; then
    echo ""
    echo "Install missing dependencies and re-run."
    exit 1
  fi
}

########################################
# Main
########################################
check_deps

if [[ ! -f "$STREAM_LIQ" ]]; then
  echo "ERROR: stream file not found: $STREAM_LIQ"
  exit 1
fi

echo "==> Cleaning old Icecast container (if exists)..."
podman rm -f "$ICECAST_CONTAINER" >/dev/null 2>&1 || true

echo "==> Starting Icecast container..."
podman run -d \
  --name "$ICECAST_CONTAINER" \
  -p 8000:8000 \
  "$ICECAST_IMAGE" >/dev/null

echo "==> Waiting for Icecast to be reachable..."
for i in {1..30}; do
  if curl -fs http://localhost:8000 >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

echo ""
echo "==> Icecast running"
echo "==> Stream endpoint: http://localhost:8000/test.mp3"
echo ""
echo "==> Starting Liquidsoap (Ctrl+C to stop streaming)"
echo ""

exec liquidsoap "$STREAM_LIQ"

