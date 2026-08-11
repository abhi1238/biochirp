#!/usr/bin/env bash
# warm_qdrant_cache.sh — keep the Qdrant vector store resident in the OS page
# cache so semantic-filter searches stay fast.
#
# Why this exists
# ---------------
# The three embedding collections (~27 GB total) are mmap'd by Qdrant
# (hnsw on_disk=false, but vectors are mmap-backed). When the host hits a
# memory spike the kernel evicts those clean mmap pages. The *next* semantic
# search then faults every page back in from disk one random read at a time,
# so a single search balloons from <0.1 s to 40–60 s. The semantic_filter tool
# has a 60 s per-search timeout, so a cold Qdrant makes every expand_and_match
# call (and therefore every DB query and every multi-DB answer) time out — the
# whole site looks dead even though every container is "healthy".
#
# Sequentially reading the segment files pulls them back into the page cache.
# When the pages are already resident this is a near-instant cache read, so it
# is cheap to run often. Install via cron (see bottom of file).
set -euo pipefail

CONTAINER="${QDRANT_CONTAINER:-bioc_qdrant}"
STORAGE="${QDRANT_STORAGE:-/qdrant/storage/collections}"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "[warm_qdrant_cache] container $CONTAINER not running — skipping"
  exit 0
fi

start=$(date +%s)
docker exec "$CONTAINER" sh -c "find $STORAGE -type f -exec cat {} + > /dev/null 2>&1" || {
  echo "[warm_qdrant_cache] read failed"; exit 1; }
echo "[warm_qdrant_cache] $(date '+%F %T') warmed $STORAGE in $(( $(date +%s) - start ))s"

# Install (runs every 15 min; ~2 min cold, near-instant when already cached):
#   (crontab -l 2>/dev/null; \
#    echo '*/15 * * * * /home/abhishekh/abhi/biochirp/scripts/warm_qdrant_cache.sh >> /home/abhishekh/abhi/biochirp/results/.qdrant_warm.log 2>&1') \
#   | crontab -
