#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

is_ok() {
  [ -x "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null
}

PY="${PYTHON:-}"

if [ -n "$PY" ] && ! is_ok "$PY"; then
  echo "PYTHON=$PY is older than 3.10; ignoring it." >&2
  PY=""
fi

if [ -z "$PY" ]; then
  for c in "$HOME/.local/bin/python3.12" python3.12 python3.13 python3.11 python3; do
    resolved="$(command -v "$c" 2>/dev/null || true)"
    if [ -n "$resolved" ] && is_ok "$resolved"; then PY="$resolved"; break; fi
  done
fi

if [ -z "$PY" ]; then
  echo "==> No Python 3.10+ found; installing one with uv"
  UV="$(command -v uv || true)"
  if [ -z "$UV" ]; then
    python3 -m pip install --quiet --user uv
    UV="$(python3 -c 'import site,os;print(os.path.join(site.USER_BASE,"bin","uv"))')"
  fi
  "$UV" python install 3.12
  PY="$("$UV" python find 3.12)"
fi

if [ -z "$PY" ] || ! is_ok "$PY"; then
  echo "Could not find or install Python 3.10+. Set PYTHON=/path/to/python3.12 and retry." >&2
  exit 1
fi

echo "Using $("$PY" --version) at $PY"

command -v npm >/dev/null || { echo "npm is needed for the desktop app and isn't installed." >&2; exit 1; }

echo "==> Creating .venv"
rm -rf .venv
"$PY" -m venv .venv || {
  echo "Couldn't create a venv. On Debian and Ubuntu that needs: sudo apt install python3-venv" >&2
  exit 1
}
source .venv/bin/activate
python -m pip install --quiet --upgrade pip wheel

echo "==> Installing Python dependencies (~500 MB the first time)"
pip install -r server/requirements-linux.txt

echo "==> Fetching the Kokoro voice model (~340 MB)"
./download-kokoro.sh

echo "==> Installing desktop app dependencies"
( cd app && npm install )

echo "==> Building the renderer vendor bundle (three.js + three-vrm)"
( cd app && npx esbuild src/vendor-entry.js --bundle --format=esm \
    --outfile=renderer/vendor/vrm-bundle.js --log-level=warning )

WARNINGS=""
if ! python -c 'import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library("portaudio") else 1)'; then
  WARNINGS="${WARNINGS}
  PortAudio is missing, so she can't hear you. Install it:
    Debian/Ubuntu: sudo apt install libportaudio2
    Fedora:        sudo dnf install portaudio
    Arch:          sudo pacman -S portaudio
"
fi
for tool in xdg-open; do
  command -v "$tool" >/dev/null || WARNINGS="${WARNINGS}
  $tool is missing, so she can't open links for you (xdg-utils).
"
done
case "${XDG_CURRENT_DESKTOP:-}" in
  *KDE*) command -v xprop >/dev/null || WARNINGS="${WARNINGS}
  xprop is missing, so she can't stay above full-screen apps on KDE (Debian/Ubuntu: sudo apt install x11-utils).
" ;;
esac
if ! command -v wl-paste >/dev/null && ! command -v xclip >/dev/null && ! command -v xsel >/dev/null; then
  WARNINGS="${WARNINGS}
  No clipboard tool: install wl-clipboard (Wayland) or xclip (X11) so she can read what you copied.
"
fi

cat <<'DONE'

Done. Next:
  1. Point llm.base_url at your server in character_config.yaml
  2. Drop your exported model at app/models/model.vrm
  3. ./start-bridge.sh     (terminal 1)
  4. ./start-app.sh        (terminal 2)

Only for letting her use this computer (optional): a screenshot tool
(spectacle, gnome-screenshot, grim or scrot) and xdotool on X11, or ydotool
with ydotoold running on Wayland.
DONE
if [ -n "$WARNINGS" ]; then
  echo "Before you run her:$WARNINGS"
fi
