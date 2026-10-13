#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

command -v rsync >/dev/null || { echo "rsync is needed to build." >&2; exit 1; }

case "$(uname -m)" in
  x86_64)  PY_ARCH="x86_64";  BUILD_ARCH="x64";   UNPACKED="linux-unpacked" ;;
  aarch64) PY_ARCH="aarch64"; BUILD_ARCH="arm64"; UNPACKED="linux-arm64-unpacked" ;;
  *) echo "Can't build for $(uname -m): only x86_64 and aarch64." >&2; exit 1 ;;
esac

PY_VERSION="3.12.14"
PY_RELEASE="20260901"
PY_ASSET="cpython-${PY_VERSION}+${PY_RELEASE}-${PY_ARCH}-unknown-linux-gnu-install_only.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/${PY_ASSET}"

WHISPER_MODEL="base.en"
CACHE="app/build/cache"
STAGE="app/build/backend"
PY="$STAGE/python/bin/python3.12"

if [ ! -d models/kokoro ] || [ ! -s models/kokoro/kokoro-v1.0.onnx ]; then
  echo "No Kokoro voice model. Run ./download-kokoro.sh first." >&2
  exit 1
fi

if [ ! -f app/models/model.vrm ]; then
  echo "No avatar at app/models/model.vrm. Add a .vrm there first." >&2
  exit 1
fi

echo "==> Rebuilding the renderer vendor bundle"
( cd app && npx esbuild src/vendor-entry.js --bundle --format=esm \
    --outfile=renderer/vendor/vrm-bundle.js --log-level=warning )

mkdir -p "$CACHE"
if [ ! -s "$CACHE/$PY_ASSET" ]; then
  echo "==> Downloading a relocatable Python $PY_VERSION"
  curl -# -fL --retry 3 -o "$CACHE/$PY_ASSET" "$PY_URL"
fi

echo "==> Staging the backend"
rm -rf "$STAGE"
mkdir -p "$STAGE"
tar -xzf "$CACHE/$PY_ASSET" -C "$STAGE"

echo "==> Installing backend dependencies"
"$PY" -m pip install --quiet --no-cache-dir --upgrade pip
"$PY" -m pip install --quiet --no-cache-dir -r server/requirements-linux.txt

echo "==> Fetching Whisper $WHISPER_MODEL"
mkdir -p "$STAGE/models/whisper"
"$PY" - "$STAGE/models/whisper/$WHISPER_MODEL" <<'PYEOF'
import sys
from huggingface_hub import snapshot_download

snapshot_download(
    f"Systran/faster-whisper-{sys.argv[1].rsplit('/', 1)[-1]}",
    local_dir=sys.argv[1],
    allow_patterns=["*.json", "*.txt", "*.bin"],
)
PYEOF

echo "==> Trimming what the app will never run"
rm -rf "$STAGE/models/whisper/$WHISPER_MODEL/.cache"
rm -rf "$STAGE/python/include" "$STAGE/python/share"
rm -rf "$STAGE"/python/lib/tcl* "$STAGE"/python/lib/libtcl* "$STAGE"/python/lib/libtk*
rm -rf "$STAGE"/python/lib/python3.12/{test,idlelib,tkinter,turtledemo,lib2to3,ensurepip}
rm -rf "$STAGE"/python/lib/python3.12/site-packages/{pip,setuptools,wheel}
rm -rf "$STAGE"/python/lib/python3.12/site-packages/{pip,setuptools,wheel}-*.dist-info
find "$STAGE/python" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/python" -name '*.pyc' -delete 2>/dev/null || true

echo "==> Copying the backend and its assets"
rsync -a --exclude '__pycache__' --exclude 'tests' server "$STAGE/"
rsync -a models/kokoro "$STAGE/models/"
cp character_config.example.yaml "$STAGE/character_config.example.yaml"

echo "==> Packing the app"
( cd app && npx electron-builder --linux dir "--$BUILD_ARCH" )

echo "==> Building the AppImage"
( cd app && npx electron-builder --linux AppImage "--$BUILD_ARCH" --prepackaged "dist/$UNPACKED" )

APPIMAGE="$(ls -t app/dist/Marina-*.AppImage 2>/dev/null | head -1 || true)"

echo
echo "Done."
if [ -n "$APPIMAGE" ]; then
  echo "  $APPIMAGE ($(du -h "$APPIMAGE" | cut -f1))"
  echo "  Run it with: $APPIMAGE"
else
  echo "  No AppImage was produced." >&2
fi
