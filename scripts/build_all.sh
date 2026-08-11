#!/usr/bin/env bash
# Build the BioChirp stack from scratch. Builds the shared base image first
# so per-service Dockerfiles (which now `FROM biochirp/base:latest`) have
# their parent available.
#
# Usage:
#   scripts/build_all.sh                # build base + all services
#   scripts/build_all.sh <svc> [<svc>]  # build base + named services only
set -euo pipefail

cd "$(dirname "$0")/.."

scripts/build_base_image.sh

if [[ $# -gt 0 ]]; then
  echo "[build_all] docker compose build $*"
  docker compose build "$@"
else
  echo "[build_all] docker compose build (all services)"
  docker compose build
fi
