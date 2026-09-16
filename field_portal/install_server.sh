#!/bin/bash
# Install Field Portal on Linux server — venv, systemd, bundled Node-RED.
set -eu

APP_DIR="${APP_DIR:-/home/zttg/netbox-field-portal}"
APP_USER="${APP_USER:-zttg}"
FIELD_HOST="${FIELD_HOST:-127.0.0.1}"
FIELD_PORT="${FIELD_PORT:-1880}"
NODERED_PORT="${NODERED_PORT:-1880}"
PYTHON="${PYTHON:-python3}"

NODE_RED_BIN="$APP_DIR/node-red-local/node_modules/.bin/node-red"
USE_NODERED=false
if [ -x "$NODE_RED_BIN" ]; then
  USE_NODERED=true
  FIELD_PORT=8765
  echo "==> Bundled Node-RED found — API internal :8765, Node-RED public :$NODERED_PORT"
else
  echo "==> No bundled Node-RED — Field API public :$FIELD_PORT"
fi

rm -f "$APP_DIR/field_service.py"

if [ ! -f "$APP_DIR/netbox_config.json" ] && [ -f "$APP_DIR/netbox_config.example.json" ]; then
  cp "$APP_DIR/netbox_config.example.json" "$APP_DIR/netbox_config.json"
  echo "WARN: copied netbox_config.example.json — edit token before production use"
fi
if [ ! -f "$APP_DIR/field_portal/portal_config.json" ] && [ -f "$APP_DIR/field_portal/portal_config.example.json" ]; then
  cp "$APP_DIR/field_portal/portal_config.example.json" "$APP_DIR/field_portal/portal_config.json"
fi

REQ="$APP_DIR/requirements.txt"
if [ ! -f "$REQ" ] && [ -f "$APP_DIR/deploy/requirements.txt" ]; then
  cp "$APP_DIR/deploy/requirements.txt" "$REQ"
fi
if [ ! -d "$APP_DIR/venv" ]; then
  echo "==> Create venv"
  $PYTHON -m venv "$APP_DIR/venv"
fi
if [ -f "$REQ" ]; then
  echo "==> pip install -r requirements.txt"
  "$APP_DIR/venv/bin/pip" install -q --upgrade pip
  "$APP_DIR/venv/bin/pip" install -q -r "$REQ"
else
  echo "WARN: requirements.txt missing — pip install requests openpyxl manually"
  "$APP_DIR/venv/bin/pip" install -q --upgrade pip requests openpyxl || true
fi

mkdir -p "$APP_DIR/.nodered"
if [ -f "$APP_DIR/field_portal/nodered_flow.json" ]; then
  cp "$APP_DIR/field_portal/nodered_flow.json" "$APP_DIR/.nodered/flows.json"
fi
cat > "$APP_DIR/.nodered/settings.js" <<'SETTINGS'
module.exports = {
    flowFile: 'flows.json',
    disableEditor: true,
    httpAdminRoot: '/admin',
    httpNodeRoot: '/',
    apiMaxLength: '25mb',
    functionGlobalContext: {
        fieldPortal: { field_api_url: 'http://127.0.0.1:8765' }
    }
};
SETTINGS

sudo_cmd() {
  if sudo -n true 2>/dev/null; then sudo "$@";
  elif [ -n "${SUDO_PASS:-}" ]; then echo "$SUDO_PASS" | sudo -S "$@";
  else return 1; fi
}

write_api_unit() {
  cat <<EOF
[Unit]
Description=NetBox ODF Field API
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=FIELD_CACHE_WARMUP=1
ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/field_portal/field_api.py --host 127.0.0.1 --port $FIELD_PORT
Restart=always
RestartSec=5
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
EOF
}

write_nr_unit() {
  cat <<EOF
[Unit]
Description=Node-RED ODF Field Portal
After=network.target netbox-field-api.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR/.nodered
Environment=NODE_OPTIONS=--max-old-space-size=256
ExecStart=$NODE_RED_BIN --userDir $APP_DIR/.nodered --port $NODERED_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

if sudo_cmd true; then
  write_api_unit | sudo_cmd tee /etc/systemd/system/netbox-field-api.service >/dev/null
  sudo_cmd systemctl unmask netbox-field-api.service 2>/dev/null || true
  sudo_cmd systemctl daemon-reload
  sudo_cmd systemctl enable netbox-field-api.service
  sudo_cmd systemctl restart netbox-field-api.service
  if $USE_NODERED; then
    write_nr_unit | sudo_cmd tee /etc/systemd/system/nodered.service >/dev/null
    sudo_cmd systemctl unmask nodered 2>/dev/null || true
    sudo_cmd systemctl enable nodered
    sudo_cmd systemctl restart nodered
  fi
  sleep 2
  systemctl is-active netbox-field-api && echo "  field-api: OK" || journalctl -u netbox-field-api -n 15 --no-pager
  if $USE_NODERED; then
    systemctl is-active nodered && echo "  nodered: OK" || journalctl -u nodered -n 15 --no-pager
  fi
else
  echo "WARN: no sudo — start manually: $APP_DIR/venv/bin/python3 field_portal/field_api.py"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
curl -sf "http://127.0.0.1:$FIELD_PORT/health" && echo "  API health OK" || echo "WARN: health check failed"
if $USE_NODERED; then
  echo "浏览: http://${IP}:${NODERED_PORT}/"
  echo "表单: http://${IP}:${NODERED_PORT}/odf?odf=轧钢机房-JG1-ODF-01"
else
  echo "表单: http://${IP}:${FIELD_PORT}/form?odf=轧钢机房-JG1-ODF-01"
fi
