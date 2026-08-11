#!/usr/bin/env bash
# duckdb_10q_check.sh — run one analytical (DuckDB-triggering) question against a
# DB chat WS and classify the outcome.
#   PASS-DUCKDB : answer carries the "auto-generated SQL" marker (text2sql fired)
#   RETRIEVAL-0 : 0 rows -> web fallback (planner found no anchor; not a DuckDB issue)
#   SYNTH       : rows returned but text2sql did not fire (synthesizer answered)
set -u
db=$1; port=$2; shift 2; q="$*"
out=$(cd /home/abhishekh/abhi/biochirp && timeout 160 python3 scripts/check_chat_ws.py \
        --db "$db" --port "$port" --query "$q" --timeout 130 2>&1)
rc=$(printf '%s\n' "$out" | sed -n 's/^row_count *: *//p' | head -1)
ans=$(printf '%s\n' "$out" | sed -n '/^answer (head):/,$p')
if printf '%s' "$ans" | grep -qi "auto-generated SQL"; then
  verdict="PASS-DUCKDB"
elif printf '%s' "$ans" | grep -qiE "from a \*\*web search\*\*|no data for this query"; then
  verdict="RETRIEVAL-0"
else
  verdict="SYNTH"
fi
# first non-empty answer line after the header (the computed count for DuckDB hits)
head_line=$(printf '%s' "$ans" | sed -n 's/^answer (head): *//p' | head -1)
printf '%-9s %-9s rows=%-6s | %s\n      Q: %s\n      A: %s\n' \
  "$verdict" "$db" "${rc:-?}" "$verdict" "$q" "${head_line:0:160}"
