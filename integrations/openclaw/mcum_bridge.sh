#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PS1_PATH="${SCRIPT_DIR}/mcum_bridge.ps1"
PS1_WIN="$(wslpath -w "${PS1_PATH}")"
ARGS_FILE="$(mktemp)"
ARGS_FILE_WIN="$(wslpath -w "${ARGS_FILE}")"

cleanup() {
  rm -f "${ARGS_FILE}"
}

trap cleanup EXIT

python3 - "${ARGS_FILE}" "$@" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(sys.argv[2:], ensure_ascii=False),
    encoding="utf-8",
)
PY

exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "${PS1_WIN}" -ArgsFile "${ARGS_FILE_WIN}"
