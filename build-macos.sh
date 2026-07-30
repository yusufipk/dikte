#!/usr/bin/env bash
# Build a self-contained Dikte.app. ffmpeg remains a Homebrew runtime dependency.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_VENV="$ROOT_DIR/.venv-build"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dikte-build.XXXXXX")"
trap 'rm -rf "$BUILD_ROOT"' EXIT

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-macos.sh must run on macOS." >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null; then
  echo "ffmpeg is required. Install it with: brew install ffmpeg" >&2
  exit 1
fi

"$PYTHON_BIN" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install --upgrade pip
"$BUILD_VENV/bin/python" -m pip install -r "$ROOT_DIR/requirements-macos.txt"

mkdir -p "$BUILD_ROOT"
"$BUILD_VENV/bin/python" "$ROOT_DIR/scripts/render_macos_icon.py" \
  "$ROOT_DIR/assets/dikte-app.svg" "$BUILD_ROOT/Dikte.iconset"
iconutil -c icns "$BUILD_ROOT/Dikte.iconset" -o "$BUILD_ROOT/Dikte.icns"

"$BUILD_VENV/bin/pyinstaller" \
  --noconfirm \
  --clean \
  --windowed \
  --name Dikte \
  --osx-bundle-identifier dev.dikte.app \
  --icon "$BUILD_ROOT/Dikte.icns" \
  --workpath "$BUILD_ROOT/work" \
  --distpath "$BUILD_ROOT/dist" \
  --specpath "$BUILD_ROOT/spec" \
  --add-data "$ROOT_DIR/assets:assets" \
  "$ROOT_DIR/dikte.py"

APP="$BUILD_ROOT/dist/Dikte.app"
PLIST="$APP/Contents/Info.plist"
set_plist() {
  local key="$1"
  local type="$2"
  local value="$3"
  /usr/libexec/PlistBuddy -c "Delete :$key" "$PLIST" >/dev/null 2>&1 || true
  /usr/libexec/PlistBuddy -c "Add :$key $type $value" "$PLIST"
}

set_plist "CFBundleDisplayName" string "Dikte"
set_plist "LSUIElement" bool true
set_plist "LSMinimumSystemVersion" string "13.0"
set_plist "LSApplicationCategoryType" string "public.app-category.utilities"
set_plist "NSMicrophoneUsageDescription" string \
  "Dikte records your voice only when you start dictation."
set_plist "NSAppleEventsUsageDescription" string \
  "Dikte uses System Events to paste the transcript into the focused application."

# Editing Info.plist invalidates PyInstaller's ad-hoc signature. Re-sign so
# Apple Silicon can launch the local build without a broken-signature error.
xattr -cr "$APP"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

mkdir -p "$ROOT_DIR/dist"
rm -f "$ROOT_DIR/dist/Dikte-macOS.zip"
ditto -c -k --sequesterRsrc --keepParent \
  "$APP" "$ROOT_DIR/dist/Dikte-macOS.zip"
echo
echo "Built: $ROOT_DIR/dist/Dikte-macOS.zip"
echo "Install ffmpeg with Homebrew, unzip, then drag Dikte.app to Applications."
