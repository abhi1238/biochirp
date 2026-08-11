#!/usr/bin/env bash
# Activate the graph router in shadow mode for the running biochirp_bio_chat
# container. Shadow mode = graph router runs IN PRODUCTION, but the legacy
# semantic_db_selector also runs in parallel and per-step diffs are logged.
# This is the safe activation path — risk is bounded to log volume.
#
# Usage:
#   bash scripts/activate_graph_router.sh         # shadow only
#   bash scripts/activate_graph_router.sh prod    # cut over entirely (no shadow)
#   bash scripts/activate_graph_router.sh off     # disable, revert to legacy
#
# Reads/writes .env so the change survives container restarts.

set -euo pipefail
ENV_FILE="${ENV_FILE:-.env}"
MODE="${1:-shadow}"

ensure_var() {
    local key="$1" val="$2"
    if [ -f "$ENV_FILE" ] && grep -q "^${key}=" "$ENV_FILE"; then
        # Update in place (portable sed)
        cp "$ENV_FILE" "${ENV_FILE}.bak"
        awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k{$2=v} 1' "${ENV_FILE}.bak" > "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
    echo "  ${key}=${val}"
}

case "$MODE" in
    shadow)
        echo "Enabling graph router in SHADOW mode (logs diffs vs legacy):"
        ensure_var BIOCHIRP_USE_GRAPH_ROUTER      1
        ensure_var BIOCHIRP_GRAPH_ROUTER_SHADOW   1
        ensure_var BIOCHIRP_GRAPH_LLM_FILTER_ENABLED 0
        ;;
    prod)
        echo "Activating graph router in PRODUCTION (no shadow, optional LLM filter):"
        ensure_var BIOCHIRP_USE_GRAPH_ROUTER      1
        ensure_var BIOCHIRP_GRAPH_ROUTER_SHADOW   0
        ensure_var BIOCHIRP_GRAPH_LLM_FILTER_ENABLED "${BIOCHIRP_GRAPH_LLM_FILTER_ENABLED:-0}"
        ;;
    off)
        echo "Reverting to legacy semantic_db_selector:"
        ensure_var BIOCHIRP_USE_GRAPH_ROUTER      0
        ensure_var BIOCHIRP_GRAPH_ROUTER_SHADOW   0
        ensure_var BIOCHIRP_GRAPH_LLM_FILTER_ENABLED 0
        ;;
    *)
        echo "Usage: $0 [shadow|prod|off]" >&2
        exit 2
        ;;
esac

echo ""
echo "Env updated. To apply to the running container, run:"
echo "  docker compose up -d biochirp_bio_chat"
echo ""
echo "Verify routing path with:"
echo "  python -m scripts.status | grep BIOCHIRP_USE_GRAPH_ROUTER"
echo "  docker logs --tail 40 biochirp_bio_chat | grep -i 'graph router'"
