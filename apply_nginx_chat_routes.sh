#!/usr/bin/env bash
# Apply BioChirp chat WebSocket routes to live nginx (v2 — proper parser).
#
# Run as: sudo bash /home/abhishekh/abhi/biochirp/apply_nginx_chat_routes.sh
set -euo pipefail

CONF=/etc/nginx/sites-available/biochirp
SNIPPETS=/etc/nginx/snippets
SRC=/home/abhishekh/abhi/biochirp
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="${CONF}.bak.${STAMP}"

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root: sudo bash $0" >&2
  exit 1
fi
[[ -f "$CONF" ]] || { echo "Missing $CONF" >&2; exit 1; }
[[ -f "$SRC/ws_common.conf" ]] || { echo "Missing $SRC/ws_common.conf" >&2; exit 1; }

echo "[1/6] Backup → $BACKUP"
cp -a "$CONF" "$BACKUP"

echo "[2/6] Installing snippets/ws_common.conf"
install -m 644 "$SRC/ws_common.conf" "$SNIPPETS/ws_common.conf"

echo "[3/6] Ensuring \$connection_upgrade map exists"
MAP_FILE=/etc/nginx/conf.d/ws_upgrade_map.conf
# Strict check: look for the actual `map $http_upgrade $connection_upgrade`
# DIRECTIVE, not just any string match. (The string "$connection_upgrade"
# appears in proxy_set_header lines too — those USE the variable, they don't
# DEFINE it. Earlier version of this script grepped too broadly.)
if grep -rqE 'map[[:space:]]+\$http_upgrade[[:space:]]+\$connection_upgrade' /etc/nginx/ 2>/dev/null; then
  echo "    already present (map directive found)"
else
  cat > "$MAP_FILE" <<'EOF'
# Auto-installed by apply_nginx_chat_routes.sh
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
EOF
  echo "    added $MAP_FILE"
fi

echo "[4/6] Injecting chat routes (Python brace-parser)"
ROUTES_FILE=$(mktemp)
cat > "$ROUTES_FILE" <<'EOF'

    # === BioChirp chat WebSocket routes (auto-installed 2026-05-17) ===
    location ^~ /bio_chat/ {
        proxy_pass http://127.0.0.1:8030;
        include /etc/nginx/snippets/ws_common.conf;
    }
    location ^~ /bio_chat_v2/ {
        proxy_pass http://127.0.0.1:8030;
        include /etc/nginx/snippets/ws_common.conf;
    }
    # === end BioChirp chat routes ===

EOF

if grep -q "BioChirp chat WebSocket routes (auto-installed" "$CONF"; then
  echo "    chat routes already present — skipping injection"
else
  PYOUT=$(mktemp)
  python3 - "$CONF" "$ROUTES_FILE" "$PYOUT" <<'PY'
"""
Strip ANY existing `location ^~ /<db>_chat/` blocks (and matching `location =
/<db>_chat` static-page blocks) from the config, then inject the unified
BioChirp chat-route block into the server { listen 443; server_name biochirp }
block, just before its closing brace.

This handles the case where an older config has duplicate or stale per-DB
chat routes pointing to wrong upstream ports.
"""
import sys, re
conf_path, routes_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

src = open(conf_path).read()
routes = open(routes_path).read()

CHAT_DB_NAMES = [
    "bio_chat","bio_chat_v2",
    # per-DB agent_chat routes removed (branch remove-per-db-agent-chat)
    "ttd_chat","hcdt_chat","ctd_chat","pharmgkb_chat","drugcentral_chat",
    "trrust_chat","hgnc_chat","civic_chat","doid_chat","mondo_chat","clinvar_chat",
    "reactome_chat","dgidb_chat","hpo_chat","biogrid_chat","chebi_chat","chembl_chat",
    "mesh_chat","msigdb_chat","omnipath_chat","orphanet_chat","pubtator_chat",
    "string_chat","uniprot_chat","wikipathways_chat",
]
NAMES_RE = "|".join(re.escape(n) for n in CHAT_DB_NAMES)

# ── Helper: skip a {...} block starting at index i (where src[i] == '{') ──
def skip_block(s, i):
    assert s[i] == '{'
    depth = 1
    j = i + 1
    n = len(s)
    while j < n and depth > 0:
        c = s[j]
        if c == '#':
            nl = s.find('\n', j); j = n if nl < 0 else nl + 1; continue
        if c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0: return j + 1
        j += 1
    return j

# ── Strip ANY existing location blocks for our chat names (^~ or =) ───────
def strip_chat_locations(s):
    pat = re.compile(
        r'(?:^[ \t]*)?location\s+(?:\^~|=)\s*/(?:' + NAMES_RE + r')\b[^\{]*\{',
        re.MULTILINE)
    out = []
    i = 0
    removed = 0
    for m in pat.finditer(s):
        start = m.start()
        brace_pos = s.find('{', m.start())
        if brace_pos < 0: continue
        end = skip_block(s, brace_pos)
        # also consume trailing newline if any
        if end < len(s) and s[end] == '\n': end += 1
        out.append(s[i:start])
        i = end
        removed += 1
    out.append(s[i:])
    return ''.join(out), removed

src, removed = strip_chat_locations(src)
print(f"    stripped {removed} existing chat-route block(s)")

# ── Find top-level server blocks and pick the 443 biochirp one ────────────
def find_top_server_blocks(s):
    blocks = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == '#':
            j = s.find('\n', i); i = n if j < 0 else j + 1; continue
        m = re.match(r'server\s*\{', s[i:])
        if m:
            body_start = i + m.end()
            close_pos = skip_block(s, body_start - 1) - 1
            blocks.append((i, body_start, close_pos))
            i = close_pos + 1
        else:
            i += 1
    return blocks

blocks = find_top_server_blocks(src)
if not blocks:
    print("ERR: no top-level server blocks found", file=sys.stderr); sys.exit(2)

def looks_like_443_biochirp(body):
    has_443 = bool(re.search(r'listen\s+[^;]*\b443\b', body))
    has_host = bool(re.search(r'server_name[^;]*biochirp', body, re.IGNORECASE))
    return has_443 and has_host, has_443

matches = []
for (s_pos, body_start, close_pos) in blocks:
    body = src[body_start:close_pos]
    is_match, is_443 = looks_like_443_biochirp(body)
    matches.append((is_match, is_443, s_pos, body_start, close_pos, len(body)))

exact = [m for m in matches if m[0]]
if exact:
    pick = max(exact, key=lambda x: x[5])
elif any(m[1] for m in matches):
    pick = max([m for m in matches if m[1]], key=lambda x: x[5])
else:
    print("ERR: no listen-443 server block found", file=sys.stderr); sys.exit(3)

_, _, s_pos, body_start, close_pos, body_len = pick
print(f"    injecting at byte {close_pos} (server block size={body_len}b)")

new_src = src[:close_pos] + routes + src[close_pos:]
open(out_path, 'w').write(new_src)
PY
  install -m 600 -o root -g root "$PYOUT" "$CONF"
  rm -f "$PYOUT"
  echo "    injection done"
fi
rm -f "$ROUTES_FILE"

echo "[5/6] Validating nginx config"
if ! nginx -t 2>&1; then
  echo "!! validation FAILED — restoring backup" >&2
  cp -a "$BACKUP" "$CONF"
  exit 3
fi

echo "[6/6] Reloading nginx + verifying"
systemctl reload nginx

sleep 1
# IMPORTANT: force HTTP/1.1 here. curl defaults to HTTP/2 on https, and HTTP/2
# WebSocket upgrade (RFC 8441 extended CONNECT) isn't handled by nginx's
# classic Connection:Upgrade proxy_pass path. Browsers use HTTP/1.1 for WS, so
# we test the way real clients connect.
CODE=$(curl -s -o /dev/null -w "%{http_code}" --http1.1 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://biochirp.iiitd.edu.in/bio_chat/)

if [[ "$CODE" == "101" ]]; then
  echo "✅ SUCCESS — /bio_chat/ now upgrades to WebSocket (HTTP 101)"
  echo "   Reload https://biochirp.iiitd.edu.in/multi_db_test.html"
else
  echo "⚠️  Got HTTP $CODE (expected 101). Backup at: $BACKUP"
  echo "   Run: sudo bash $0      # to retry, or restore manually:"
  echo "   sudo cp $BACKUP $CONF && sudo systemctl reload nginx"
fi
