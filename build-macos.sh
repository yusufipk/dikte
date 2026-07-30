#!/usr/bin/env bash
# Build a self-contained Dikte.app. ffmpeg remains a Homebrew runtime dependency.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_VENV="$ROOT_DIR/.venv-build"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CODE_SIGN_IDENTITY="${DIKTE_CODESIGN_IDENTITY:--}"
OFFICIAL_TEAM_ID="PTQ5FN6P8U"
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

PYINSTALLER_SIGN_ARGS=()
if [[ "$CODE_SIGN_IDENTITY" != "-" ]]; then
  PYINSTALLER_SIGN_ARGS=(--codesign-identity "$CODE_SIGN_IDENTITY")
fi

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
  "${PYINSTALLER_SIGN_ARGS[@]}" \
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
APP_VERSION="$(PYTHONPATH="$ROOT_DIR" "$BUILD_VENV/bin/python" -c \
  'from app_version import APP_VERSION; print(APP_VERSION)')"
set_plist "CFBundleShortVersionString" string "$APP_VERSION"
set_plist "CFBundleVersion" string "$APP_VERSION"
set_plist "LSUIElement" bool true
set_plist "LSMinimumSystemVersion" string "13.0"
set_plist "LSApplicationCategoryType" string "public.app-category.utilities"
set_plist "NSMicrophoneUsageDescription" string \
  "Dikte records your voice only when you start dictation."

# Editing Info.plist invalidates PyInstaller's signature. Release builds use a
# stable identity so macOS Accessibility permission survives app updates; CI
# and local contributors without an identity retain the ad-hoc fallback.
xattr -cr "$APP"
if [[ "$CODE_SIGN_IDENTITY" == "-" ]]; then
  codesign --force --sign - "$APP"
else
  SIGN_REQUIREMENT="=designated => identifier \"dev.dikte.app\" and anchor apple generic and certificate leaf[subject.OU] = \"$OFFICIAL_TEAM_ID\""
  codesign --force --sign "$CODE_SIGN_IDENTITY" \
    --requirements "$SIGN_REQUIREMENT" "$APP"
fi
codesign --verify --deep --strict "$APP"

mkdir -p "$ROOT_DIR/dist"
rm -f "$ROOT_DIR/dist/Dikte-macOS.zip"
ditto -c -k --sequesterRsrc --keepParent \
  "$APP" "$ROOT_DIR/dist/Dikte-macOS.zip"
echo
echo "Built: $ROOT_DIR/dist/Dikte-macOS.zip"
echo "Install ffmpeg with Homebrew, unzip, then drag Dikte.app to Applications."
