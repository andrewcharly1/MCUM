#!/bin/sh
# MCUM one-command installer (Linux / macOS).
# Usage:  curl -fsSL https://raw.githubusercontent.com/andrewcharly1/MCUM/main/install.sh | sh
set -e

REPO="https://github.com/andrewcharly1/MCUM.git"
# Permanent install location (override with MCUM_HOME). Folder MUST be "MCUM".
DEST="${MCUM_HOME:-$HOME/MCUM}"

echo "==> Installing MCUM to $DEST"

for tool in git node npm; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is required but not on PATH."; exit 1; }
done
command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1 || {
  echo "ERROR: Python is required but not on PATH."; exit 1; }

if [ -d "$DEST/.git" ]; then
  echo "==> Updating existing clone"
  git -C "$DEST" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DEST"
fi

cd "$DEST"
echo "==> npm install"
npm install
echo "==> mcum init"
node bin/mcum.mjs init

echo ""
echo "==> Done. MCUM installed at $DEST"
echo "    Restart your agent to load the 30 mcum_* tools."
echo "    Register another project:  python3 $DEST/mcum_install.py register --workspace <path>"
