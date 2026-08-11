#!/usr/bin/env bash
# Build the biochirp/base shared image. Run from repo root, or anywhere — the
# script cd's to the repo root before invoking docker.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f Dockerfile.base ]]; then
  echo "error: Dockerfile.base not found at $(pwd)/Dockerfile.base" >&2
  exit 1
fi

echo "[build_base_image] Building biochirp/base:latest from Dockerfile.base..."
docker build -f Dockerfile.base -t biochirp/base:latest .
echo "[build_base_image] Done. Now run: docker compose build  (to rebuild per-service images on the new base)"
