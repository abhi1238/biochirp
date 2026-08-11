#!/usr/bin/env bash
# Print the structure of /etc/nginx/sites-available/biochirp so we can see
# which server block handles biochirp.iiitd.edu.in and where the chat routes
# ended up. Run as: sudo bash /home/abhishekh/abhi/biochirp/diagnose_nginx.sh
set -e
CONF=/etc/nginx/sites-available/biochirp
[[ $EUID -eq 0 ]] || { echo "Run as: sudo bash $0" >&2; exit 1; }

echo "=== server blocks overview (line-numbered, key directives only) ==="
grep -n -E '^[[:space:]]*(server[[:space:]]*\{|server_name|listen|root|return|location[[:space:]]+(\^~|=)?[[:space:]]*/(bio_chat|chembl_chat|ttd_chat|chat|api))' "$CONF" | head -80
echo
echo "=== nginx -T resolved config (relevant location blocks for biochirp.iiitd.edu.in) ==="
nginx -T 2>/dev/null | awk '
  /^# configuration file/ { cf = $0 }
  /server[[:space:]]*\{/  { in_server = 1; depth = 1; buf = $0 "\n"; next }
  in_server               { buf = buf $0 "\n";
                            if ($0 ~ /\{/) depth++
                            if ($0 ~ /\}/) { depth--; if (depth==0){ if (buf ~ /biochirp\.iiitd/) { print cf; print buf; print "---" } in_server=0; buf="" } } }
' | head -120
echo
echo "=== curl trace headers for /bio_chat/ (which upstream returned 404?) ==="
curl -sv -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGVzdGtleTEyMzQ1Njc4OTAxMg==" \
  https://biochirp.iiitd.edu.in/bio_chat/ 2>&1 | grep -E "^[<>*]" | head -30
echo
echo "=== nginx error log last 20 lines ==="
tail -20 /var/log/nginx/error.log 2>/dev/null || echo "(no error log)"
