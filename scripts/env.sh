#!/usr/bin/env bash
# Only way in. Builds the build-env image and runs the command inside it.
set -euo pipefail
cd "$(dirname "$0")/.."
IMG=polymarket-buildenv
docker build -q -t "$IMG" -f contrib/Dockerfile.buildenv . >/dev/null
TTY=(-i); [ -t 0 ] && TTY=(-i -t)
exec docker run --rm --label pmrun "${TTY[@]}" -v "$PWD:/app" -w /app "$IMG" \
  "${@:-bash}"
