#!/usr/bin/env bash
# Install BioChirp WS / MCP rate-limit and security snippets into the live
# nginx tree. Idempotent — safe to re-run after every repo update.
#
# Run with sudo from the repo root:
#   sudo bash scripts/install_nginx_security.sh
#
# What this does (in order):
#   1. Backs up any existing target file with a .pre-<timestamp> suffix.
#   2. Installs deploy/nginx-rate-limits.conf to /etc/nginx/conf.d/.
#      That brings the limit_*_zone declarations into http{} scope.
#   3. Refreshes /etc/nginx/snippets/ws_common.conf from the repo (now
#      carrying limit_req + limit_conn directives).
#   4. Validates the resulting config with `nginx -t`. If it fails, the
#      previous files are restored before nginx is reloaded.
#   5. Reloads nginx via systemctl.
#
# Pair this with deploy/nginx-mcp.conf, which already has limits wired on
# /mcp, /mcp/sse, /mcp/messages — verify it's included from your apex config
# (typically /etc/nginx/sites-available/biochirp) inside the listen 443 block.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run with sudo (need to write under /etc/nginx)" >&2
  exit 1
fi

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"

CONF_D_TARGET="/etc/nginx/conf.d/biochirp-rate-limits.conf"
CONF_D_SOURCE="${REPO_ROOT}/deploy/nginx-rate-limits.conf"

WS_TARGET="/etc/nginx/snippets/ws_common.conf"
WS_SOURCE="${REPO_ROOT}/ws_common.conf"

for src in "${CONF_D_SOURCE}" "${WS_SOURCE}"; do
  if [[ ! -f "${src}" ]]; then
    echo "ERROR: required source file missing: ${src}" >&2
    exit 1
  fi
done

backup_if_exists() {
  local path="$1"
  if [[ -f "${path}" ]]; then
    local bak="${path}.pre-${TS}"
    cp -a "${path}" "${bak}"
    echo "  backed up ${path} -> ${bak}"
  fi
}

install_file() {
  local src="$1" dst="$2"
  backup_if_exists "${dst}"
  install -m 0644 -T "${src}" "${dst}"
  echo "  installed ${dst}"
}

echo "[1/4] installing rate-limit zones into /etc/nginx/conf.d/"
install_file "${CONF_D_SOURCE}" "${CONF_D_TARGET}"

echo "[2/4] refreshing /etc/nginx/snippets/ws_common.conf"
mkdir -p /etc/nginx/snippets
install_file "${WS_SOURCE}" "${WS_TARGET}"

echo "[3/4] validating nginx config"
if ! nginx -t; then
  echo "ERROR: nginx -t failed. Rolling back..." >&2
  for path in "${CONF_D_TARGET}" "${WS_TARGET}"; do
    bak="${path}.pre-${TS}"
    if [[ -f "${bak}" ]]; then
      mv -f "${bak}" "${path}"
      echo "  restored ${path}"
    else
      rm -f "${path}"
      echo "  removed ${path} (no prior version)"
    fi
  done
  exit 1
fi

echo "[4/4] reloading nginx"
systemctl reload nginx
echo "done. tail /var/log/nginx/error.log for limit_req/limit_conn warnings."
