#!/usr/bin/env bash
# Repin Dockerfile FROM bases and docker-compose 3rd-party images to fresh
# upstream digests. Run when intentionally bumping the base (e.g. python
# security release).
#
# Usage:
#   bash scripts/security/repin_docker_bases.sh
#
# Side-effects: edits every */dockerfile that uses `FROM python:3.11-slim`,
# plus the 3 remote-image lines in docker-compose.yml. Always run on a clean
# git tree so the diff is reviewable.
set -euo pipefail

cd "$(dirname "$(realpath "$0")")/../.."

repin_image() {
  local img="$1"     # e.g. python:3.11-slim
  local tag="${img##*:}"
  local repo="${img%:*}"

  echo "→ pulling $img ..."
  docker pull "$img" >/dev/null
  local digest
  digest=$(docker image inspect "$img" --format '{{ index .RepoDigests 0 }}' | awk -F@ '{print $2}')
  if [[ -z "$digest" ]]; then
    echo "FATAL: could not resolve digest for $img" >&2
    exit 2
  fi
  echo "  digest: $digest"
  printf '%s\n' "$digest"
}

# 1. python:3.11-slim  →  pin every Dockerfile
PY_DIGEST=$(repin_image python:3.11-slim | tail -n1)
NEW_PY="FROM python:3.11-slim@${PY_DIGEST}"
N=0
while IFS= read -r -d '' f; do
  # Match either the bare form or any prior pinned form
  if grep -q '^FROM python:3\.11-slim\(@sha256:[a-f0-9]\+\)\?\s*$' "$f"; then
    sed -i -E "s|^FROM python:3\.11-slim(@sha256:[a-f0-9]+)?\s*$|${NEW_PY}|" "$f"
    N=$((N+1))
  fi
done < <(find . -maxdepth 4 -iname 'dockerfile' -type f \
    -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/build/*' \
    -not -path '*/.claude/*' -not -path '*/dist_zenodo/*' -print0)
echo "  re-pinned $N Dockerfile(s)"

# 2. 3rd-party docker-compose images
for img in redis:7.4.1-alpine qdrant/qdrant:v1.17.1 ollama/ollama:latest; do
  digest=$(repin_image "$img" | tail -n1)
  pinned="${img}@${digest}"
  esc_img=$(printf '%s' "$img" | sed 's/[\/&]/\\&/g')
  esc_pinned=$(printf '%s' "$pinned" | sed 's/[\/&]/\\&/g')
  # Replace any prior pin (with or without digest)
  sed -i -E "s|image: ${esc_img}(@sha256:[a-f0-9]+)?|image: ${esc_pinned}|g" docker-compose.yml
done

echo
echo "✔ Repin pass complete. Review with: git diff -- docker-compose.yml '**/dockerfile'"
echo "  Rebuild changed services: docker compose build --pull \$(docker compose config --services)"
