#!/usr/bin/env bash
# cleanup_results.sh — delete cached query CSVs older than $RETENTION_DAYS
# from RESULTS_ROOT. Default retention is 7 days. Share-link TTL upstream is
# only 24 h (SHARE_TTL_SECONDS=86400 in app/per_db_chat/_main.py), so a 7-day
# floor still allows for direct /download?path= debugging of recent queries.
#
# Audit 2026-05-18: 15,294 files / 6.9 GB total before first sweep. ~70% of
# files were older than 7 days at the time.
#
# Usage:
#   scripts/cleanup_results.sh                 # delete with default retention
#   scripts/cleanup_results.sh --dry-run       # show what would be deleted
#   RETENTION_DAYS=2 scripts/cleanup_results.sh
#
# Schedule via user cron (no sudo required):
#   crontab -e
#   # daily at 03:30
#   30 3 * * * /home/abhishekh/abhi/biochirp/scripts/cleanup_results.sh >> /home/abhishekh/abhi/biochirp/results/.cleanup.log 2>&1
set -euo pipefail

ROOT="${RESULTS_ROOT:-/home/abhishekh/abhi/biochirp/results}"
DAYS="${RETENTION_DAYS:-7}"
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; fi

if [[ ! -d "$ROOT" ]]; then
  echo "[cleanup_results] $ROOT does not exist — nothing to do" >&2
  exit 0
fi

ts="$(date '+%Y-%m-%d %H:%M:%S')"
before_count=$(find "$ROOT" -maxdepth 1 -type f -name "*.csv" | wc -l)
before_bytes=$(du -sb "$ROOT" 2>/dev/null | awk '{print $1}')
candidates=$(find "$ROOT" -maxdepth 1 -type f -name "*.csv" -mtime +"$DAYS" | wc -l)

echo "[cleanup_results] [$ts] root=$ROOT retention=${DAYS}d"
echo "[cleanup_results]   before:    $before_count CSV files, $((before_bytes / 1024 / 1024)) MiB"
echo "[cleanup_results]   to delete: $candidates files older than ${DAYS}d"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[cleanup_results]   (dry-run — no deletions performed)"
  exit 0
fi

# -delete on find is atomic-per-file; safe on a live mount.
find "$ROOT" -maxdepth 1 -type f -name "*.csv" -mtime +"$DAYS" -delete

after_count=$(find "$ROOT" -maxdepth 1 -type f -name "*.csv" | wc -l)
after_bytes=$(du -sb "$ROOT" 2>/dev/null | awk '{print $1}')
freed=$((before_bytes - after_bytes))
echo "[cleanup_results]   after:     $after_count CSV files, $((after_bytes / 1024 / 1024)) MiB"
echo "[cleanup_results]   freed:     $((freed / 1024 / 1024)) MiB"
