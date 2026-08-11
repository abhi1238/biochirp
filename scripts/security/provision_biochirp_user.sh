#!/usr/bin/env bash
# Provision a dedicated `biochirp` system user and relocate secrets so the
# MCP service stops running as your personal `abhishekh` account.
#
# After running this script:
#   • A locked system account `biochirp` owns the runtime.
#   • Secrets live at /etc/biochirp/env (root:biochirp 0640) instead of
#     /home/abhishekh/abhi/biochirp/.env (which becomes a symlink for
#     dev convenience).
#   • /var/lib/biochirp/{results,logs,artifacts} are the writable paths the
#     hardened systemd unit allows.
#   • The systemd unit at /etc/systemd/system/biochirp-mcp.service is
#     installed from deploy/biochirp-mcp.production.service (which this
#     repo ships alongside) and runs as User=biochirp.
#
# Idempotent — re-running is safe.
#
# Usage (as root):
#   sudo bash scripts/security/provision_biochirp_user.sh
#
# Roll-back: see ROLLBACK section at the bottom for the reverse commands.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "FATAL: must run as root (try: sudo bash $0)" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$(realpath "$0")")/../.." && pwd)"
DEV_USER="${SUDO_USER:-abhishekh}"
SVC_USER=biochirp
SVC_GROUP=biochirp
ETC_DIR=/etc/biochirp
ETC_ENV=${ETC_DIR}/env
STATE_DIR=/var/lib/biochirp
UNIT_SRC="${REPO_ROOT}/deploy/biochirp-mcp.production.service"
UNIT_DST=/etc/systemd/system/biochirp-mcp.service

echo "→ repo:        ${REPO_ROOT}"
echo "→ dev user:    ${DEV_USER}"
echo "→ service uid: ${SVC_USER}"
echo "→ env file:    ${ETC_ENV}"
echo "→ state dir:   ${STATE_DIR}"
echo "→ unit file:   ${UNIT_DST}"
echo
read -r -p "Continue? [y/N] " yn
[[ "$yn" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }

# ─ 1. user/group ───────────────────────────────────────────────────────────
if ! getent group "$SVC_GROUP" >/dev/null; then
  groupadd --system "$SVC_GROUP"
  echo "  + created group ${SVC_GROUP}"
fi
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SVC_GROUP" \
          --home-dir /nonexistent --no-create-home \
          --shell /usr/sbin/nologin \
          "$SVC_USER"
  echo "  + created system user ${SVC_USER}"
fi
# Add the dev user to the biochirp group so they can read/edit /etc/biochirp/env
# without sudo each time.
if ! id -nG "$DEV_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$SVC_GROUP"; then
  usermod -a -G "$SVC_GROUP" "$DEV_USER"
  echo "  + added ${DEV_USER} to ${SVC_GROUP} (re-login required for it to take effect)"
fi

# ─ 2. relocate .env to /etc/biochirp/env ──────────────────────────────────
install -d -m 0750 -o root -g "$SVC_GROUP" "$ETC_DIR"
if [[ -L "${REPO_ROOT}/.env" && "$(readlink -f "${REPO_ROOT}/.env")" == "$ETC_ENV" ]]; then
  echo "  = .env already symlinked to ${ETC_ENV}"
elif [[ -f "${REPO_ROOT}/.env" ]]; then
  if [[ ! -e "$ETC_ENV" ]]; then
    install -m 0640 -o root -g "$SVC_GROUP" "${REPO_ROOT}/.env" "$ETC_ENV"
    echo "  + copied ${REPO_ROOT}/.env → ${ETC_ENV}  (mode 0640, root:${SVC_GROUP})"
  else
    echo "  = ${ETC_ENV} already exists; leaving repo .env in place (not overwriting)"
  fi
  # Replace the repo .env with a symlink so dev tooling keeps working.
  mv "${REPO_ROOT}/.env" "${REPO_ROOT}/.env.pre_relocate.$(date +%s)"
  ln -s "$ETC_ENV" "${REPO_ROOT}/.env"
  chmod 600 "${REPO_ROOT}/.env.pre_relocate."* 2>/dev/null || true
  echo "  + symlinked repo .env → ${ETC_ENV}; previous copy backed up beside it"
else
  echo "  ! no .env in ${REPO_ROOT}; creating empty placeholder at ${ETC_ENV}"
  : >"$ETC_ENV"
  chown root:"$SVC_GROUP" "$ETC_ENV"
  chmod 0640 "$ETC_ENV"
fi

# ─ 3. state dirs ──────────────────────────────────────────────────────────
for d in results logs artifacts; do
  install -d -m 0775 -o "$SVC_USER" -g "$SVC_GROUP" "${STATE_DIR}/${d}"
done
echo "  + provisioned ${STATE_DIR}/{results,logs,artifacts}"

# ─ 4. systemd unit ────────────────────────────────────────────────────────
if [[ ! -f "$UNIT_SRC" ]]; then
  echo "FATAL: ${UNIT_SRC} missing — pull latest repo before re-running" >&2
  exit 3
fi
install -m 0644 -o root -g root "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
echo "  + installed ${UNIT_DST}"

cat <<EOF

✔ Provisioning complete.

WHEN YOU'RE READY to cut over (this is the disruption window):

  # Stop the old worker(s):
  sudo systemctl stop 'biochirp-mcp@*.service' biochirp-mcp.service 2>/dev/null || true

  # Start the new (hardened, biochirp-user) service:
  sudo systemctl enable --now biochirp-mcp.service
  sudo systemctl status biochirp-mcp.service

  # Tail logs:
  journalctl -u biochirp-mcp.service -f

Things to verify after cut-over:
  • The MCP /mcp/sse endpoint still responds:
       curl -s -N https://biochirp.iiitd.edu.in/mcp/health
  • The service is actually running as biochirp:
       ps -o user,cmd -p \$(pidof -s python3) | head
  • /etc/biochirp/env is readable to the service only via group membership:
       sudo -u biochirp test -r /etc/biochirp/env && echo OK

ROLLBACK (in order, if anything regresses):
  sudo systemctl disable --now biochirp-mcp.service
  sudo mv ${REPO_ROOT}/.env ${REPO_ROOT}/.env.symlink_to_etc
  sudo cp ${REPO_ROOT}/.env.pre_relocate.* ${REPO_ROOT}/.env   # newest one
  sudo chmod 600 ${REPO_ROOT}/.env && sudo chown ${DEV_USER}:${DEV_USER} ${REPO_ROOT}/.env
  sudo systemctl enable --now 'biochirp-mcp@8765.service'      # or whichever ports you used

EOF
