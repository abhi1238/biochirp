#!/usr/bin/env bash
# Rotate the local Postgres password for role `biochirp` to the value already
# written into .env by the 2026-05-17 security pass.
#
# What this script does:
#   1. Reads the new password from .env (DB_PASSWORD line).
#   2. Issues ALTER ROLE against the local Postgres instance.
#   3. Verifies the new password works by opening a probe connection.
#   4. Tells you which services to restart so they pick up the new env.
#
# What it does NOT do:
#   - Restart anything. You decide when the maintenance window starts.
#   - Connect to a remote DB; assumes Postgres is bound to 127.0.0.1.
#
# Run as the user who owns the local Postgres superuser (typically `postgres`):
#   sudo -u postgres bash scripts/security/rotate_postgres_password.sh
# or as a Postgres superuser via PGUSER:
#   PGUSER=postgres bash scripts/security/rotate_postgres_password.sh
set -euo pipefail

ENV_FILE="${ENV_FILE:-$(dirname "$(realpath "$0")")/../../.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "FATAL: cannot read $ENV_FILE" >&2
  exit 2
fi

NEW_PW="$(grep -E '^DB_PASSWORD=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
if [[ -z "$NEW_PW" ]]; then
  echo "FATAL: DB_PASSWORD not set in $ENV_FILE" >&2
  exit 2
fi

if [[ -z "${OLD_PW:-}" ]]; then
  echo "FATAL: OLD_PW must be set explicitly (no default) — export OLD_PW=<current password> before running" >&2
  exit 2
fi

PG_HOST="${PGHOST:-127.0.0.1}"
PG_PORT="${PGPORT:-5432}"
PG_DB="${PGDATABASE:-postgres}"

echo "→ Rotating role 'biochirp' password on ${PG_HOST}:${PG_PORT}/${PG_DB}"
echo "  Old hint (env override OLD_PW=...): ${OLD_PW:0:4}***  →  new (from .env): ${NEW_PW:0:4}***"
read -r -p "  Continue? [y/N] " yn
[[ "$yn" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }

# psql -c quoting: pass password via psql variable to avoid SQL-injection on
# odd characters. \set + :'var' interpolates as a quoted literal.
psql -h "$PG_HOST" -p "$PG_PORT" -d "$PG_DB" -v new_pw="$NEW_PW" -c \
  "ALTER ROLE biochirp WITH PASSWORD :'new_pw';"

# Probe with the new password on each biochirp DB to confirm.
for db in ctd hcdt; do
  echo "→ Probing $db with new password..."
  PGPASSWORD="$NEW_PW" psql -h "$PG_HOST" -p "$PG_PORT" -U biochirp -d "$db" \
    -c "select current_user, now();" >/dev/null
  echo "  OK ($db)"
done

cat <<'EOF'

✔ Password rotated.

Next steps (you decide the maintenance window):
  sudo systemctl restart 'biochirp-mcp@*.service'
  # plus every chat-/tool-service container/process that reads .env:
  docker compose restart $(docker compose ps --services | grep -E 'ctd|hcdt|drugcentral|chat')

If anything fails to connect after restart:
  - Confirm the running container actually sees the new .env (env_file: in
    docker-compose.yml; bake-time .env is stale).
  - Check Postgres log: sudo journalctl -u postgresql -n 100

The pre-rotation .env is backed up at:
  .env.bak_security_2026_05_17   (mode 0600, NOT committed)
EOF
