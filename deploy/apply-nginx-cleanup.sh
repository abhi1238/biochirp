#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# One-shot nginx cleanup for the 2026-06-18 decommission work.
#
#   sudo bash deploy/apply-nginx-cleanup.sh            # apply (default)
#   sudo bash deploy/apply-nginx-cleanup.sh --dry-run  # show what would change
#
# Does two things, then a SINGLE `nginx -t` + reload. If `nginx -t` fails it
# ROLLS BACK both changes and aborts — so a bad edit never reaches a reload.
#
#   1. Reinstall the regenerated chat-routes snippet
#      (/etc/nginx/snippets/biochirp-chat-routes.conf ← repo nginx_chat_routes.conf):
#      removes the dead /bio_chat + /bio_chat_v2 routes (port 8030, backend gone);
#      keeps the 10 live per-DB /<db>_chat/ routes.
#   2. Disable the orphaned biochirp.net site (sites-enabled/biochirp.conf →
#      dead localhost:3000, not part of this repo). Unlink only — the config
#      stays in sites-available for revival.
#
# NOT touched: OpenTargets. Its /opentarget/ → 127.0.0.1:8026 WS route in the
# main site config is already correct; the frontend (chat-bootstrap.js) was
# repointed to /opentarget/ to match. No nginx change needed for OpenTargets.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

REPO="/home/abhishekh/abhi/biochirp"
SNIPPET="/etc/nginx/snippets/biochirp-chat-routes.conf"
SRC="$REPO/nginx_chat_routes.conf"
NET_LINK="/etc/nginx/sites-enabled/biochirp.conf"
TS="$(date +%Y%m%d-%H%M%S)"

if [ "$(id -u)" -ne 0 ]; then echo "Run as root: sudo bash $0"; exit 1; fi
[ -f "$SRC" ] || { echo "ERROR: $SRC missing (run scripts/gen_compose.py first)"; exit 1; }

snippet_backup=""      # set if we replace the snippet
net_disabled=0         # set if we unlink the biochirp.net symlink

rollback() {
  echo "  ↩ rolling back..."
  [ -n "$snippet_backup" ] && cp -a "$snippet_backup" "$SNIPPET" && echo "    restored $SNIPPET"
  [ "$net_disabled" = "1" ] && ln -sf /etc/nginx/sites-available/biochirp.conf "$NET_LINK" \
    && echo "    re-enabled $NET_LINK"
}

echo "== 1. chat-routes snippet ($SNIPPET) =="
if [ ! -f "$SNIPPET" ]; then
  echo "  WARNING: snippet not found — skipping (verify the include path in the site config)"
elif diff -q "$SNIPPET" "$SRC" >/dev/null 2>&1; then
  echo "  already up to date — no change"
else
  echo "  diff (installed → repo):"; diff "$SNIPPET" "$SRC" || true
  if [ "$DRY_RUN" = "0" ]; then
    snippet_backup="$SNIPPET.bak.$TS"
    cp -a "$SNIPPET" "$snippet_backup"
    install -m 0644 -o root -g root "$SRC" "$SNIPPET"
    echo "  installed (backup: $snippet_backup)"
  fi
fi

echo "== 2. biochirp.net site ($NET_LINK) =="
if [ -L "$NET_LINK" ]; then
  if [ "$DRY_RUN" = "0" ]; then rm "$NET_LINK"; net_disabled=1; echo "  unlinked (config kept in sites-available)"; \
  else echo "  WOULD unlink (config kept in sites-available)"; fi
elif [ -e "$NET_LINK" ]; then
  echo "  NOT a symlink (regular file) — leaving it; review manually"
else
  echo "  already absent — no change"
fi

echo "== 3. nginx -t + reload =="
if [ "$DRY_RUN" = "1" ]; then echo "  (dry-run — skipped)"; echo "Dry run complete."; exit 0; fi
if nginx -t; then
  systemctl reload nginx
  echo "  ✓ nginx reloaded successfully"
else
  echo "  ✗ nginx -t FAILED"
  rollback
  nginx -t >/dev/null 2>&1 && echo "  config restored to a valid state (not reloaded)" || true
  exit 1
fi

echo
echo "Done. Verify OpenTargets chat: open https://<host>/opentarget and confirm the"
echo "WebSocket hits /opentarget/ → 101 (DevTools → Network → WS). Hard-reload to drop"
echo "the cached chat-bootstrap.js."
