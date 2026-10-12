#!/usr/bin/env bash
# Builds Marina.app and a mountable Marina.dmg.
#
# The .app is self-contained: it carries its own Python 3.12, the backend's
# dependencies, the Kokoro voice, the Whisper model and a starter config, so
# it runs on a Mac that has never seen this repo.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

PY_VERSION="3.12.14"
PY_RELEASE="20260901"
PY_ASSET="cpython-${PY_VERSION}+${PY_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
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
"$PY" -m pip install --quiet --no-cache-dir -r server/requirements-mac.txt

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

echo "==> Building Marina.app"
( cd app && npx electron-builder --mac dir )

APP="app/dist/mac-arm64/Marina.app"

# Sign before the dmg is built, or the dmg ships an unsigned copy. With a real
# signing identity, macOS keeps Screen Recording and Accessibility across
# rebuilds; an ad-hoc signature changes every build, so they'd be asked for
# again each time. Set MARINA_SIGN_IDENTITY to choose one; otherwise the first
# Apple Development identity in the keychain is used, or ad-hoc if there's none.
IDENTITY="${MARINA_SIGN_IDENTITY:-$(security find-identity -v -p codesigning 2>/dev/null \
  | awk -F'"' '/Apple Development/ {print $2; exit}')}"
echo "==> Signing (${IDENTITY:-ad-hoc})"
xattr -cr "$APP"
codesign --force --deep --sign "${IDENTITY:--}" "$APP"
codesign --verify --deep "$APP" && echo "    signature verified"
xattr -cr "$APP"

echo "==> Building the dmg from the signed app"
( cd app && npx electron-builder --mac dmg --prepackaged dist/mac-arm64/Marina.app )

DMG="$(ls -t app/dist/Marina-*-arm64.dmg 2>/dev/null | head -1 || true)"

echo "==> Installing to /Applications"
rm -rf /Applications/Marina.app
cp -R "$APP" /Applications/Marina.app
xattr -cr /Applications/Marina.app

echo
echo "Done."
echo "  Marina.app is in /Applications ($(du -sh "$APP" | cut -f1))."
if [ -n "$DMG" ]; then
  echo "  $DMG ($(du -h "$DMG" | cut -f1))"
else
  echo "  No dmg was produced." >&2
fi
echo
echo "Open it once, then right-click its Dock icon -> Options -> Keep in Dock."
