#!/usr/bin/env bash
# Automated setup for build123d-cad skill.
# Creates a Python 3.12 virtual environment and installs build123d.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Require either uv or plain python3/virtualenv
if command -v uv &>/dev/null; then
    echo "Creating venv with uv …"
    uv venv --python 3.12 "$VENV_DIR"
    uv pip install --python "$VENV_DIR/bin/python" build123d
elif command -v python3 &>/dev/null; then
    echo "Creating venv with python3 …"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install build123d
else
    echo "Error: neither 'uv' nor 'python3' found on PATH." >&2
    exit 1
fi

echo "Setup complete. venv at $VENV_DIR"
