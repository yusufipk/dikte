#!/usr/bin/env bash
# The macOS download: a disk image with Dikte.app in it and the usual arrow at
# /Applications to drag it onto.
#
# Run from anywhere; it works in build/ at the top of the checkout and leaves
# the finished .dmg in dist/. One image per architecture, because PyQt6 has no
# universal wheel to build a universal binary out of, so the workflow runs this
# once on an Apple silicon runner and once on an Intel one.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
OUT="$ROOT/dist"
ARCH="$(uname -m)"
BUNDLE_ID="io.github.yusufipk.dikte"

VERSION="$(cd "$ROOT" && python3 -c 'import dikte; print(dikte.__version__)')"
APP="$BUILD/dist/Dikte.app"

# A pinned tag and a checksum rather than "whatever is newest": this binary
# goes out inside something people run, so what it is has to be decided here
# and not by whoever pushes to that repository next. GPL, which is what Dikte
# is licensed under too. 6.1.1 is behind the current release and stays there
# until something Dikte asks of it needs a newer one.
FFMPEG_TAG="b6.1.1"
case "$ARCH" in
  arm64)  FFMPEG_ASSET="ffmpeg-darwin-arm64.gz"
          FFMPEG_SHA="8923876afa8db5585022d7860ec7e589af192f441c56793971276d450ed3bbfa" ;;
  x86_64) FFMPEG_ASSET="ffmpeg-darwin-x64.gz"
          FFMPEG_SHA="929b375c1182d956c51f7ac25e0b2b0411fb01f6f407aa15c9758efeb4242106" ;;
  *)      echo "no ffmpeg pinned for $ARCH" >&2; exit 1 ;;
esac

rm -rf "$BUILD" "$OUT"
mkdir -p "$BUILD" "$OUT"

# 1. The icon --------------------------------------------------------------
# Before the application, because the bundle is built with it rather than
# having it copied in afterwards. Drawn by Dikte itself, offscreen, which is
# why there is no image file in the repository.
iconset="$BUILD/Dikte.iconset"
QT_QPA_PLATFORM=offscreen PYTHONPATH="$ROOT" python3 -m dikte.trayicon "$iconset"
iconutil -c icns "$iconset" -o "$BUILD/Dikte.icns"
export DIKTE_ICNS="$BUILD/Dikte.icns"

# 2. The application -------------------------------------------------------
python3 -m PyInstaller "$ROOT/packaging/dikte.spec" \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --noconfirm --clean

# 3. ffmpeg ----------------------------------------------------------------
# Recording on a Mac goes through ffmpeg, and macOS ships nothing like it, so
# without this the disk image would be an application that cannot record until
# the person who downloaded it installs Homebrew. Resources/bin because
# Contents/MacOS is for the executable the bundle names, and integrate.py puts
# this directory in front of PATH at startup.
bin="$APP/Contents/Resources/bin"
mkdir -p "$bin"
curl -fsSL -o "$BUILD/$FFMPEG_ASSET" \
  "https://github.com/eugeneware/ffmpeg-static/releases/download/$FFMPEG_TAG/$FFMPEG_ASSET"
echo "$FFMPEG_SHA  $BUILD/$FFMPEG_ASSET" | shasum -a 256 -c -
gunzip -c "$BUILD/$FFMPEG_ASSET" > "$bin/ffmpeg"
chmod +x "$bin/ffmpeg"

# 4. Signing ---------------------------------------------------------------
# Ad-hoc, because there is no Developer ID to sign with. It is not decoration:
# macOS files a microphone or Accessibility permission against a code
# signature, and an arm64 binary carrying none is refused by the kernel outright
# rather than merely warned about. What it does not buy is Gatekeeper, which is
# why the README tells people how to get past the first-launch refusal.
#
# --deep is the wrong tool for a real signature and the right one here: every
# dylib PyInstaller collected plus the ffmpeg added above all need one, and
# adding ffmpeg invalidated the signature PyInstaller left.
codesign --force --deep --sign - --identifier "$BUNDLE_ID" "$APP"
codesign --verify --deep "$APP"

# 5. The disk image --------------------------------------------------------
# A staging directory rather than the bundle on its own, so that the window
# that opens has the arrow to drag it onto. UDZO is the compressed read-only
# format every Mac has understood for twenty years.
stage="$BUILD/stage"
mkdir -p "$stage"
cp -a "$APP" "$stage/"
ln -s /Applications "$stage/Applications"
# On GitHub's Intel runners hdiutil now and then fails with a diskimages-helper
# left running behind it, and the same build passes on a second try.
# Not -quiet, so a failure that keeps coming back says why in the log.
# A volume a failed attempt left attached is let go before the next one.
for attempt in 1 2 3; do
  if hdiutil create -volname "Dikte $VERSION" -srcfolder "$stage" \
    -ov -format UDZO "$OUT/Dikte-$VERSION-$ARCH.dmg"; then
    break
  else
    status=$?
  fi
  echo "hdiutil create failed with $status, attempt $attempt of 3" >&2
  if [ "$attempt" = 3 ]; then
    exit "$status"
  fi
  hdiutil info >&2 || true
  hdiutil detach "/Volumes/Dikte $VERSION" -force >/dev/null 2>&1 || true
  sleep $((attempt * 15))
done

echo "dist/Dikte-$VERSION-$ARCH.dmg"
