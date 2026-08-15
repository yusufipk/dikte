#!/usr/bin/env bash
# Dikte on macOS: dependency check, the application bundle, the command, and
# the login item. install.sh hands over to this on a Mac; run it directly and
# it does the same thing.
#
# A Mac needs a bundle where Linux needs a .desktop file, and for the same
# reason plus one: it is what puts a name and an icon in the Finder, and it is
# also the identity macOS files the microphone and Accessibility permissions
# under. Run as a bare script instead, every permission is granted to whatever
# copy of python3 happened to run it, and it is granted again the next time
# Homebrew moves that copy.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/Dikte.app"
BIN_DIR="$HOME/.local/bin"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT_ID="io.github.yusufipk.dikte"
AGENT="$AGENT_DIR/$AGENT_ID.plist"
SHORTCUT="${1:-Ctrl+Option+Space}"
# Without the colon, so that a second argument given as "" stays empty. That is
# how update.sh says "this one was turned off", as against not saying anything.
CANCEL_SHORTCUT="${2-Ctrl+Option+D}"

# The two places Homebrew installs to, in front, for the same reason dikte.py
# puts them there: a shell that has not been logged into since Homebrew was
# installed does not have them, and this script would then report ffmpeg as
# missing while the application finds it perfectly well.
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

say()  { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1"; echo; exit 1; }

echo
echo "Installing Dikte"
echo "────────────────"

# 1. An interpreter new enough ---------------------------------------------
# The system python3 is 3.9 and will stay 3.9: Apple ships it for its own
# scripts, not for anybody's application, and Dikte needs 3.11. So the first
# question is not "is python3 there" but "which python3", and the answer is
# written into the bundle rather than looked up again at launch, because a
# bundle started from the Finder gets none of the shell's PATH.
find_python() {
  local candidate
  for candidate in \
    "${DIKTE_PYTHON:-}" \
    /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
    /usr/local/bin/python3.14 /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
    python3.14 python3.13 python3.12 python3.11 python3
  do
    [[ -n "$candidate" ]] || continue
    candidate="$(command -v "$candidate" 2>/dev/null)" || continue
    # Resolved, so that the bundle does not point at a Homebrew shim that a
    # later `brew upgrade` repoints at a version Dikte cannot run on.
    candidate="$(cd "$(dirname "$candidate")" && pwd)/$(basename "$candidate")"
    # Exit status, not printed output: a candidate that cannot run Python at
    # all and one that is too old are the same answer here.
    if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [[ -z "$PY" ]]; then
  warn "No Python 3.11 or newer found (the one Apple ships is 3.9)."
  say  "brew install python@3.13"
  die  "Nothing was installed."
fi
ok "Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# 2. The rest of the dependencies ------------------------------------------
missing=()
"$PY" -c 'import PyQt6.QtWidgets' 2>/dev/null || missing+=("PyQt6")
command -v ffmpeg >/dev/null || missing+=("ffmpeg")

if ((${#missing[@]})); then
  warn "Missing: ${missing[*]}"
  for item in "${missing[@]}"; do
    case "$item" in
      # Not pip: Homebrew's python is marked externally managed, so installing
      # into it is refused. `brew install pyqt` puts PyQt6 where its own
      # interpreter already looks. A virtualenv is the other answer, and then
      # DIKTE_PYTHON is how this script is pointed at it.
      PyQt6)  say "PyQt6:   brew install pyqt" ;;
      ffmpeg) say "ffmpeg:  brew install ffmpeg" ;;
    esac
  done
  # ffmpeg alone is survivable: it is what records, so nothing works without
  # it, but the settings window opens and says so. PyQt6 is not: there is no
  # window to say anything in.
  if [[ " ${missing[*]} " == *" PyQt6 "* ]]; then
    echo
    die "PyQt6 is what the whole interface is; install it and run this again."
  fi
  echo
else
  ok "All dependencies present"
fi

# 3. The application bundle -------------------------------------------------
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# The interpreter is copied in rather than run where it lies, and that copy is
# what the whole of macOS's idea of "which application is this" rests on.
#
# A bundle whose launcher runs an interpreter somewhere else is not that
# interpreter's application: the process is /opt/homebrew/…/python3.13, so that
# is the name in the microphone dialog, that is the row in Accessibility, and
# every application on the machine sharing that interpreter shares the
# permission. Copied to Contents/MacOS and exec'd from there, the running
# executable sits inside Dikte.app, macOS reads the Info.plist above it, and
# the permissions are Dikte's: its name, its icon, its row.
#
# The copy runs because the framework it links against is named by an absolute
# path; what it loses is the tree it was found in, which is what PYTHONHOME and
# PYTHONPATH below hand back. Both of those are what a `brew upgrade python`
# moves out from under it, so the launcher checks before it starts: a bundle
# that fails silently is a menu bar with nothing in it and no way to guess why.
PY_REAL="$("$PY" -c 'import os, sys; print(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))')"
PY_HOME="$("$PY" -c 'import sys; print(sys.base_prefix)')"
PY_SITE="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
cp -f "$PY_REAL" "$APP/Contents/MacOS/python3"

# exec, and this time it is right. The earlier version of this script did not
# exec, because exec'ing an interpreter outside the bundle throws away the
# application registration LaunchServices handed to the process it started, and
# what is left answers the socket while drawing no menu bar icon at all. The
# target here is inside the bundle, so the registration survives it.
cat > "$APP/Contents/MacOS/Dikte" <<EOF
#!/bin/sh
# Written by install-mac.sh. Edit that, not this.
HERE=\$(cd "\$(dirname "\$0")" && pwd)
export PYTHONHOME="$PY_HOME"
export PYTHONPATH="$PY_SITE"
# Started from the Finder there is no terminal to print to, so the one thing
# that can go wrong on its own says so in a dialog.
if [ ! -d "\$PYTHONHOME" ]; then
  osascript -e 'display alert "Dikte" message "The Python this was installed against is gone, most likely after a brew upgrade. Run ./install.sh again."' >/dev/null 2>&1
  exit 1
fi
exec "\$HERE/python3" "$DIR/dikte.py" --gui "\$@"
EOF
chmod +x "$APP/Contents/MacOS/Dikte"

# LSUIElement is the line that makes this a menu bar application: no Dock icon,
# no menu bar of its own, nothing in the app switcher. The usage strings are
# not decoration either, they are what the permission dialog reads out, and a
# bundle that asks for the microphone without one is killed rather than asked
# about.
version="$(cd "$DIR" && git describe --tags --always 2>/dev/null || echo 0)"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>Dikte</string>
  <key>CFBundleDisplayName</key>       <string>Dikte</string>
  <key>CFBundleIdentifier</key>        <string>$AGENT_ID</string>
  <key>CFBundleExecutable</key>        <string>Dikte</string>
  <key>CFBundleIconFile</key>          <string>Dikte</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key>           <string>$version</string>
  <key>LSMinimumSystemVersion</key>    <string>11.0</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>LSUIElement</key>               <true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Dikte records what you dictate so that it can be transcribed.</string>
  <key>NSAppleEventsUsageDescription</key>
  <string>Dikte puts the transcript on the clipboard and pastes it into the window you were typing in.</string>
</dict>
</plist>
EOF
printf 'APPL????' > "$APP/Contents/PkgInfo"

# The icon, drawn by trayicon.py so that there is no binary in the repository
# and no second place to change what Dikte looks like. Failing to draw it is
# not worth stopping for: a bundle with no icon gets the generic one.
iconset="$(mktemp -d)/Dikte.iconset"
if "$PY" "$DIR/trayicon.py" "$iconset" >/dev/null 2>&1 \
   && iconutil -c icns "$iconset" -o "$APP/Contents/Resources/Dikte.icns" 2>/dev/null; then
  ok "Icon drawn"
else
  warn "Could not build the icon; the bundle gets the generic one"
fi
rm -rf "$(dirname "$iconset")"

# Signed, ad-hoc, and this is the load-bearing step. macOS remembers a
# permission against a code signature; with none, it remembers a path and a
# hash of the bundle, and every reinstall is a bundle it has never seen, which
# means granting Accessibility and the microphone again each time. --force
# because a reinstall is signing over the last signature.
if codesign --force --sign - --identifier "$AGENT_ID" "$APP" 2>/dev/null; then
  ok "Application bundle: $APP"
else
  warn "Could not sign $APP; macOS will ask for its permissions again on every"
  say  "reinstall. Install the command line tools: xcode-select --install"
fi

# LaunchServices does not necessarily notice a bundle written under a directory
# it was not watching, and until it does, `open -a Dikte` cannot find it.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true

# 4. The command ------------------------------------------------------------
# A wrapper, where Linux gets a symlink to dikte.py. The shebang there is
# `env python3`, and on a Mac that is Apple's 3.9: the symlink would resolve to
# the one interpreter that cannot run this. Naming the interpreter here also
# gives update.sh and uninstall.sh somewhere to read it from, so that the three
# scripts cannot disagree about which Python this installation is on.
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/dikte" <<EOF
#!/bin/sh
# Written by install-mac.sh. Edit that, not this.
exec "$PY" "$DIR/dikte.py" "\$@"
EOF
chmod +x "$BIN_DIR/dikte" "$DIR/dikte.py"
ok "Command installed: $BIN_DIR/dikte"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add it in ~/.zprofile:"
     say  "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# 5. Starting at login ------------------------------------------------------
# A LaunchAgent rather than a Login Item: it is a file this script can write
# and uninstall.sh can delete, where a Login Item is a database only the user
# can edit by hand. KeepAlive is off on purpose: quitting from the tray menu
# should quit it, not restart it.
mkdir -p "$AGENT_DIR"
cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>       <string>$AGENT_ID</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-a</string>
    <string>$APP</string>
  </array>
  <key>RunAtLoad</key>   <true/>
  <key>KeepAlive</key>   <false/>
  <key>ProcessType</key> <string>Interactive</string>
</dict>
</plist>
EOF
# Through `open` rather than running the launcher directly, so that the process
# is one LaunchServices started: that is what gives it the bundle's identity,
# and so the permissions granted to Dikte rather than to launchd.
launchctl bootout "gui/$(id -u)/$AGENT_ID" >/dev/null 2>&1 || true
if launchctl bootstrap "gui/$(id -u)" "$AGENT" >/dev/null 2>&1; then
  ok "Will start automatically on login"
else
  warn "Could not register the login item; add $APP under"
  say  "System Settings → General → Login Items instead."
fi

# 6. The shortcuts ----------------------------------------------------------
# Dikte registers them rather than this script writing a file: macOS keeps no
# shortcut registry at all, so "installed" means the running application is
# holding the combination, and the settings file is where it reads it from
# after a restart. Nothing here needs a logout, unlike KDE.
if [[ "$SHORTCUT" == "$CANCEL_SHORTCUT" ]]; then
  warn "Both arguments are $SHORTCUT, so the discard key was left out."
  say  "Pass two different combinations, or set it in Settings → Shortcuts."
  CANCEL_SHORTCUT=""
fi

register() {   # which  combination  label
  if out="$("$PY" "$DIR/dikte.py" shortcut install "$1" --combo "$2" 2>&1)"; then
    ok "$3: $2"
  else
    warn "${out%%$'\n'*}"
  fi
}
register toggle "$SHORTCUT" "Start and stop"
if [[ -n "$CANCEL_SHORTCUT" ]]; then
  register cancel "$CANCEL_SHORTCUT" "Discard the recording"
fi

# 7. What is left to do by hand ---------------------------------------------
# Two things this script cannot do, because macOS only takes them from the
# person at the keyboard.
echo
say "Two permissions have to be granted the first time, and macOS will ask:"
say "  • Microphone,     when the first recording starts"
say "  • Accessibility,  when the first transcript is pasted"
say "Both are under System Settings → Privacy & Security."
if ! system_profiler SPAudioDataType 2>/dev/null | grep -qi 'blackhole\|loopback\|soundflower'; then
  echo
  say "Recording a meeting also needs a loopback driver: macOS does not offer"
  say "what the speakers are playing as something to record. Install one with:"
  say "  brew install blackhole-2ch"
  say "Dictation does not need it."
fi

echo
ok "Done. Start it with:  open -a Dikte"
say "The settings window opens on first run: download a speech model, or add"
say "an OpenAI or OpenRouter key instead."
echo
