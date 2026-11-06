#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/items-for-sale
APP_USER=items-for-sale
APP_GROUP=items-for-sale
DATA_DIR=/var/lib/items-for-sale
ENV_FILE="$APP_DIR/.env"
SERVICE_FILE=/etc/systemd/system/items-for-sale.service
UPDATE_SERVICE_FILE=/etc/systemd/system/items-for-sale-update.service
UPDATE_HELPER_DIR=/usr/local/lib/items-for-sale
UPDATE_HELPER="$UPDATE_HELPER_DIR/update_server.py"
UPDATE_SUDOERS_FILE=/etc/sudoers.d/items-for-sale-update

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || fail "Run this script as root: sudo bash $APP_DIR/scripts/deploy.sh"
[[ ! -L "$APP_DIR" ]] || fail "$APP_DIR must not be a symbolic link."
[[ -d "$APP_DIR/.git" ]] || fail "Expected a Git clone at $APP_DIR. Clone the repository there first."
[[ -f "$APP_DIR/requirements.txt" ]] || fail "requirements.txt is missing from $APP_DIR."
[[ -f "$APP_DIR/.env.example" ]] || fail ".env.example is missing from $APP_DIR."

for command in adduser addgroup chown chmod curl getent git id install journalctl python3 sed sudo systemctl visudo; do
  command -v "$command" >/dev/null 2>&1 || fail "Required command '$command' is missing. Install the Raspberry Pi OS prerequisites from the deployment guide."
done

[[ ! -L "$ENV_FILE" ]] || fail "$ENV_FILE must not be a symbolic link."
[[ ! -L "$APP_DIR/.venv" ]] || fail "$APP_DIR/.venv must not be a symbolic link."
[[ ! -L "$SERVICE_FILE" ]] || fail "$SERVICE_FILE must not be a symbolic link."
[[ ! -L "$UPDATE_SERVICE_FILE" ]] || fail "$UPDATE_SERVICE_FILE must not be a symbolic link."
[[ ! -L "$UPDATE_HELPER_DIR" ]] || fail "$UPDATE_HELPER_DIR must not be a symbolic link."
[[ ! -L "$UPDATE_SUDOERS_FILE" ]] || fail "$UPDATE_SUDOERS_FILE must not be a symbolic link."

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || fail "Python 3.11 or newer is required."

if ! getent group "$APP_GROUP" >/dev/null; then
  addgroup --system "$APP_GROUP"
fi

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  adduser --system --ingroup "$APP_GROUP" --home "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
elif [[ "$(id -gn "$APP_USER")" != "$APP_GROUP" ]]; then
  fail "The existing $APP_USER account must use the $APP_GROUP group."
fi

chown -R "root:$APP_GROUP" "$APP_DIR"
chmod -R o-rwx "$APP_DIR"
chmod 750 "$APP_DIR"

install -d -o "$APP_USER" -g "$APP_GROUP" -m 750 "$DATA_DIR"

created_env=false
if [[ ! -e "$ENV_FILE" ]]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"
  sed -i 's|^INVENTORY_DATA_DIR=.*$|INVENTORY_DATA_DIR=/var/lib/items-for-sale|' "$ENV_FILE"
  sed -i 's|^INVENTORY_AGENT_OWNERS=.*$|INVENTORY_AGENT_OWNERS={}|' "$ENV_FILE"
  created_env=true
fi
chown "root:$APP_GROUP" "$ENV_FILE"
chmod 640 "$ENV_FILE"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
chown -R "root:$APP_GROUP" "$APP_DIR"
chmod -R o-rwx "$APP_DIR"

install -d -o root -g root -m 755 "$UPDATE_HELPER_DIR"
install -o root -g root -m 755 "$APP_DIR/scripts/update_server.py" "$UPDATE_HELPER"

cat > "$UPDATE_SERVICE_FILE" <<'UNIT'
[Unit]
Description=Update Items for Sale from origin/main
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
Group=root
WorkingDirectory=/opt/items-for-sale
ExecStart=/opt/items-for-sale/.venv/bin/python /usr/local/lib/items-for-sale/update_server.py
Environment=PYTHONUNBUFFERED=1
Environment=PIP_NO_CACHE_DIR=1
TimeoutStartSec=30min
UMask=0077
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "$UPDATE_SERVICE_FILE"

temporary_sudoers=$(mktemp /etc/sudoers.d/items-for-sale-update.XXXXXX)
printf '%s\n' 'items-for-sale ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block items-for-sale-update.service' > "$temporary_sudoers"
chmod 440 "$temporary_sudoers"
if ! visudo -cf "$temporary_sudoers"; then
  rm -f -- "$temporary_sudoers"
  fail "Could not validate the system-update sudoers rule."
fi
mv -f -- "$temporary_sudoers" "$UPDATE_SUDOERS_FILE"

if [[ ! -e "$SERVICE_FILE" ]]; then
  cat > "$SERVICE_FILE" <<'UNIT'
[Unit]
Description=Items for Sale application
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=items-for-sale
Group=items-for-sale
WorkingDirectory=/opt/items-for-sale
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/items-for-sale/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
  chmod 644 "$SERVICE_FILE"
else
  printf 'Preserving existing systemd unit: %s\n' "$SERVICE_FILE"
fi

systemctl daemon-reload
systemctl enable items-for-sale

if [[ "$created_env" == true ]]; then
  printf '\nCreated %s from .env.example. Configure INVENTORY_USERS before starting the app.\n' "$ENV_FILE"
  printf 'Generate a password hash with: sudo -u %s %s/.venv/bin/python -m scripts.create_user\n' "$APP_USER" "$APP_DIR"
  printf 'Then edit .env and start the service with: sudo systemctl start items-for-sale\n'
  exit 0
fi

if ! CONFIG_STATUS=$("$APP_DIR/.venv/bin/python" - "$ENV_FILE" <<'PY'
import json
import sys

from dotenv import dotenv_values

users = json.loads(dotenv_values(sys.argv[1]).get("INVENTORY_USERS") or "{}")
if not isinstance(users, dict):
    raise SystemExit("INVENTORY_USERS must be a JSON object.")
print("configured" if users else "empty")
PY
); then
  fail "Could not read INVENTORY_USERS from $ENV_FILE. Fix the configuration before starting the service."
fi

if [[ "$CONFIG_STATUS" != configured ]]; then
  printf 'INVENTORY_USERS is empty. The service was enabled but not started; configure an owner account in %s first.\n' "$ENV_FILE"
  exit 0
fi

systemctl restart items-for-sale

for attempt in {1..20}; do
  if curl --fail --silent http://127.0.0.1:8000/health; then
    printf '\nDeployment complete. Check Caddy and HTTPS separately using the deployment guide.\n'
    exit 0
  fi
  sleep 1
done

systemctl status items-for-sale --no-pager >&2 || true
journalctl -u items-for-sale -n 50 --no-pager >&2 || true
fail "The service did not become healthy at http://127.0.0.1:8000/health."
