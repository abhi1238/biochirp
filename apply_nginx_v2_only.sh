#!/usr/bin/env bash
# One-shot: add the /bio_chat_v2/ location block to the live nginx config
# (the full apply_nginx_chat_routes.sh saw the existing marker and skipped
# re-injection, so the new v2 block was never added).
#
# Run as: sudo bash /home/abhishekh/abhi/biochirp/apply_nginx_v2_only.sh

set -euo pipefail

CONF=/etc/nginx/sites-available/biochirp
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="${CONF}.bak.${STAMP}"

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root: sudo bash $0" >&2
  exit 1
fi
[[ -f "$CONF" ]] || { echo "Missing $CONF" >&2; exit 1; }

if grep -q "/bio_chat_v2/" "$CONF"; then
  echo "✓ /bio_chat_v2/ already present in $CONF — nothing to do"
  echo "  testing live route…"
else
  echo "[1/3] Backup → $BACKUP"
  cp -a "$CONF" "$BACKUP"

  echo "[2/3] Inserting /bio_chat_v2/ block immediately after /bio_chat/"
  python3 - "$CONF" <<'PY'
import re, sys
conf = sys.argv[1]
src = open(conf).read()

# Find the /bio_chat/ location block and insert /bio_chat_v2/ right after it.
# The block looks like:
#     location ^~ /bio_chat/ {
#         proxy_pass http://127.0.0.1:8030;
#         include /etc/nginx/snippets/ws_common.conf;
#     }
pat = re.compile(
    r'(location\s+\^~\s+/bio_chat/\s*\{[^}]*\})',
    re.MULTILINE | re.DOTALL,
)
m = pat.search(src)
if not m:
    print("ERR: couldn't find /bio_chat/ location block", file=sys.stderr)
    sys.exit(2)

# Detect leading whitespace before the matched block so we mirror the indent.
line_start = src.rfind('\n', 0, m.start()) + 1
indent = re.match(r'[ \t]*', src[line_start:]).group(0)

v2_block = (
    f"\n{indent}location ^~ /bio_chat_v2/ {{\n"
    f"{indent}    proxy_pass http://127.0.0.1:8030;\n"
    f"{indent}    include /etc/nginx/snippets/ws_common.conf;\n"
    f"{indent}}}"
)
new = src[:m.end()] + v2_block + src[m.end():]
open(conf, 'w').write(new)
print("    inserted (indent={!r})".format(indent))
PY
fi

echo "[3/3] nginx -t + reload + curl WS handshake"
if ! nginx -t 2>&1; then
  echo "!! validation FAILED — restoring backup" >&2
  cp -a "$BACKUP" "$CONF"
  exit 3
fi

systemctl reload nginx
sleep 1

CODE=$(curl -s -o /dev/null -w "%{http_code}" --http1.1 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://biochirp.iiitd.edu.in/bio_chat_v2/)

if [[ "$CODE" == "101" ]]; then
  echo "✅ SUCCESS — /bio_chat_v2/ now upgrades to WebSocket (HTTP 101)"
  echo "   Reload https://biochirp.iiitd.edu.in/db_chat.html?db=multi_v2"
else
  echo "⚠️  Got HTTP $CODE (expected 101)."
  if [[ -f "$BACKUP" ]]; then
    echo "    Restore with: sudo cp $BACKUP $CONF && sudo systemctl reload nginx"
  fi
fi
