#!/usr/bin/env bash
# Build node-red-local.tar.gz for offline server deploy (Linux/macOS with Node.js/npm).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$ROOT/deploy"
OUT_DIR="$ROOT/field_portal"
BUNDLE_DIR="$OUT_DIR/node-red-local"
TAR="$OUT_DIR/node-red-local.tar.gz"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }
need node
need npm

echo "==> Build Node-RED bundle (node-red from deploy/package.json)"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"
cp "$DEPLOY/package.json" "$BUNDLE_DIR/package.json"
(cd "$BUNDLE_DIR" && npm install --omit=dev --no-audit --no-fund)

NR_BIN="$BUNDLE_DIR/node_modules/.bin/node-red"
if [ ! -x "$NR_BIN" ]; then
  echo "ERROR: node-red binary not found after npm install"
  exit 1
fi

echo "==> Pack $TAR"
rm -f "$TAR"
tar -czf "$TAR" -C "$OUT_DIR" node-red-local
MB=$(du -m "$TAR" | cut -f1)
echo "Done: $TAR (${MB} MB)"
