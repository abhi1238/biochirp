#!/usr/bin/env bash
# Interactive key-rotation checklist.
#
# Walks through every secret in .env that needs to be rotated in its
# provider's console (BioChirp cannot rotate these for you — each provider
# requires you to click "Revoke" / "Generate new" in their dashboard).
#
# For each item, this script:
#   1. Prints the provider's rotation URL.
#   2. Pauses for you to paste the new value (or "skip" to leave unchanged).
#   3. Updates the value in-place in .env using sed (preserves comments,
#      ordering, and surrounding whitespace).
#   4. Backs up the prior .env to .env.bak_rotate_<timestamp> (mode 0600,
#      gitignored).
#
# Run interactively. Do NOT redirect stdin from a file — the script
# deliberately blocks on each prompt so you can verify each rotation.
#
# Usage:
#   bash scripts/security/rotate_api_keys.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$(realpath "$0")")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
[[ -r "$ENV_FILE" ]] || { echo "FATAL: cannot read $ENV_FILE" >&2; exit 2; }

# Single backup at start so a multi-rotation session has one rollback target.
BACKUP="${ENV_FILE}.bak_rotate_$(date +%Y%m%d_%H%M%S)"
cp "$ENV_FILE" "$BACKUP"
chmod 600 "$BACKUP"
echo "→ backed up current .env → $BACKUP"
echo

# Rotation table: env-var-name | provider | rotation URL
ROTATIONS=(
  "OPENAI_API_KEY|OpenAI|https://platform.openai.com/api-keys"
  "OPENROUTER_API_KEY|OpenRouter|https://openrouter.ai/keys"
  "GROQ_API_KEY|Groq|https://console.groq.com/keys"
  "GEMINI_API_KEY|Google Gemini|https://aistudio.google.com/app/apikey"
  "GROK_KEY|xAI Grok|https://console.x.ai/team/default/api-keys"
  "TAVILY_API_KEY|Tavily|https://app.tavily.com/home"
  "github_access_token|GitHub PAT|https://github.com/settings/tokens"
  "SYNAPSE_AUTH_TOKEN|Synapse (Sage Bionetworks)|https://www.synapse.org/PersonalAccessTokens:0"
  "DISGENET_API_KEY|DisGeNET (legacy; remove if unused)|https://www.disgenet.org/api/"
  "ONCOKB_TOKEN|OncoKB (legacy; remove if unused)|https://www.oncokb.org/account/settings"
  "COSMIC_PASSWORD|COSMIC account password|https://cancer.sanger.ac.uk/cosmic/login"
)

update_env() {
  local key="$1" value="$2"
  # Escape sed delimiter and slashes. We use # as delimiter to survive
  # values containing / (common in JWTs and URLs).
  local esc
  esc=$(printf '%s' "$value" | sed -e 's/[#&\\]/\\&/g' -e 's/$/\\/' -e '$s/\\$//')
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s#^${key}=.*#${key}=${esc}#" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

for row in "${ROTATIONS[@]}"; do
  IFS='|' read -r key provider url <<<"$row"
  current=$(grep -E "^${key}=" "$ENV_FILE" | head -n1 | cut -d= -f2- || true)
  printf '────────────────────────────────────────────────────────────────\n'
  printf '  Var:        %s\n'  "$key"
  printf '  Provider:   %s\n'  "$provider"
  printf '  Rotate at:  %s\n'  "$url"
  printf '  Current:    %s***\n' "${current:0:6}"
  echo
  read -r -p "  Paste new value (or 'skip' / 'unset'): " newval
  case "$newval" in
    skip|"") echo "  → skipped, unchanged"; ;;
    unset)
      sed -i "/^${key}=/d" "$ENV_FILE"
      echo "  → removed from .env"
      ;;
    *)
      update_env "$key" "$newval"
      echo "  → updated"
      ;;
  esac
  echo
done

chmod 600 "$ENV_FILE"
cat <<EOF

✔ Rotation pass complete.
  Backup of pre-rotation .env: $BACKUP
  (mode 0600, gitignored via .env.bak_*)

Next steps:
  1. Restart services so they reload the new keys.
     Local processes:  pkill -HUP -f 'mcp_server.http_server' || true
     Containers:       docker compose up -d --force-recreate \$(docker compose ps --services)
  2. Verify no service is still using a revoked key — tail provider error logs
     (OpenAI dashboard "Errors" tab, Synapse audit log, …) for 401/403 spikes.
  3. Once everything is green, shred the backup:
        shred -u "$BACKUP"
EOF
