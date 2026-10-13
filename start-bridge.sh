#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
SETUP="./setup-$([ "$(uname)" = Darwin ] && echo mac || echo linux).sh"

if [ ! -d .venv ]; then
  echo "No .venv here. Run $SETUP first."
  exit 1
fi

PY="$(pwd)/.venv/bin/python"
[ -x "$PY" ] || { echo "No venv. Run $SETUP" >&2; exit 1; }
exec "$PY" server/marina_server.py
