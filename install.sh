#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"

if ! "$VENV_DIR/bin/python" -c 'import tkinter' >/dev/null 2>&1; then
  echo "Tkinter is missing. Install python3-tk (Debian/Ubuntu) or python3-tkinter (Fedora)." >&2
  exit 1
fi

echo "Installed. Run: $SCRIPT_DIR/run_elevenlabs_tts.sh"
