#!/usr/bin/env bash
# Install from extracted release package on the server (no Windows required).
set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${APP_DIR:-$HOME/netbox-field-portal}"

echo "==> Install to $APP_DIR from $PKG_DIR"
mkdir -p "$APP_DIR"

if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude 'dist/' --exclude '.git/' --exclude 'venv/' \
    --exclude 'field_portal/node-red-local/' \
    --exclude 'netbox_config.json' \
    --exclude 'field_portal/portal_config.json' \
    "$PKG_DIR/" "$APP_DIR/"
else
  echo "==> rsync not found, using cp"
  cp -a "$PKG_DIR/." "$APP_DIR/"
fi

TAR="$PKG_DIR/field_portal/node-red-local.tar.gz"
if [ -f "$TAR" ] && [ ! -x "$APP_DIR/node-red-local/node_modules/.bin/node-red" ]; then
  echo "==> Extract Node-RED bundle"
  tar -xzf "$TAR" -C "$APP_DIR"
  chmod +x "$APP_DIR/node-red-local/node_modules/.bin/node-red" 2>/dev/null || true
fi

sed -i 's/\r$//' "$APP_DIR/deploy/install_server.sh" "$APP_DIR/field_portal/install_server.sh" 2>/dev/null || true
chmod +x "$APP_DIR/deploy/install_server.sh" "$APP_DIR/field_portal/install_server.sh"

APP_DIR="$APP_DIR" bash "$APP_DIR/deploy/install_server.sh"
