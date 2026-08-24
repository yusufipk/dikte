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

# The checkout, one level up: this script lives in scripts/, everything it
# touches is at the top of the tree.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The one file that starts the application, whoever is asking: the wrapper in
# ~/.local/bin, the bundle, and every shortcut Dikte registers.
ENTRY="$DIR/dikte/__main__.py"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/Dikte.app"
BIN_DIR="$HOME/.local/bin"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT_ID="io.github.yusufipk.dikte"
AGENT="$AGENT_DIR/$AGENT_ID.plist"
# Only the one: the discard key's default is the settings' own, read back below.
DEFAULT_SHORTCUT="Ctrl+Option+Space"
# Given as arguments, or asked of the settings further down. An installer run
# again, which is what every update does, must not undo a key you chose in
# Settings, so silence here means "keep whatever is there".
SHORTCUT="${1:-}"
CANCEL_SHORTCUT="${2-}"
# Passed as "" means the discard key is off, which is not the same answer as
# not being passed at all.
CANCEL_GIVEN=$(( $# >= 2 ))
# One caller says both without meaning either: an updater from before Dikte
# became a package looks for the settings at a path that no longer exists, and
# so passes the default key and an empty discard key rather than yours. This
# can go once nobody is updating across that commit any more.
if [[ "$SHORTCUT" == "$DEFAULT_SHORTCUT" && $CANCEL_GIVEN == 1 && -z "$CANCEL_SHORTCUT" ]]; then
  SHORTCUT=""
  CANCEL_GIVEN=0
fi

# The two places Homebrew installs to, in front, for the same reason the app
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
# Everything below MacOS and the old signature is generated by this installer.
# Rebuild them from scratch so abandoned launchers from an interrupted update
# cannot become unsigned nested code and make the whole bundle fail validation.
rm -rf "$APP/Contents/MacOS" "$APP/Contents/_CodeSignature"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Finder starts the native executable below as Dikte. It must remain that
# executable for the life of the GUI: replacing it with Python via execv makes
# LaunchServices treat the process as Python.app on macOS 26, and Qt's status
# item then never becomes visible. Load the Python framework at runtime and run
# its public Py_BytesMain entry point instead. That keeps Dikte's application
# identity without linking the launcher to one Homebrew Cellar version before
# main() can show a useful reinstall message.
PY_HOME="$("$PY" -c 'import sys; print(sys.base_prefix)')"
PY_SITE="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
PY_LIBRARY="$("$PY" -c 'import os, sys, sysconfig; print(os.path.realpath(os.path.join(sys.base_prefix, "Python")) if sysconfig.get_config_var("PYTHONFRAMEWORK") else os.path.realpath(os.path.join(sysconfig.get_config_var("LIBDIR"), sysconfig.get_config_var("LDLIBRARY"))))')"

# Encode every path byte as a fixed-width octal escape. Quoting only backslashes
# and double quotes is not enough for legal macOS names containing newlines or
# other control characters, and adjacent octal escapes cannot consume each
# other's digits the way hexadecimal escapes can.
c_string_literal() {
    "$PY" -c 'import os, sys; print("\"" + "".join(f"\\{byte:03o}" for byte in os.fsencode(sys.argv[1])) + "\"")' "$1"
}
C_PY_HOME="$(c_string_literal "${PY_HOME:-}")"
C_PY_SITE="$(c_string_literal "${PY_SITE:-}")"
C_PY_LIBRARY="$(c_string_literal "${PY_LIBRARY:-}")"
C_ENTRY="$(c_string_literal "$ENTRY")"

# On macOS 26 (Tahoe), source installs launched through the shell wrapper have
# been observed to create no visible NSStatusItem. Compile a native Mach-O
# executable that wraps Python execution; if clang fails, retain the wrapper.
LAUNCHER_SRC="$(mktemp -t dikte_launcher.XXXXXX.c)"
cat > "$LAUNCHER_SRC" <<EOF
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <sys/stat.h>
#include <dlfcn.h>
#include <string.h>

typedef int (*PyBytesMain)(int, char **);

int main(int argc, char *argv[]) {
    const char *py_home = $C_PY_HOME;
    const char *py_site = $C_PY_SITE;
    const char *py_library = $C_PY_LIBRARY;
    const char *entry   = $C_ENTRY;

    struct stat st;
    if (stat(py_home, &st) != 0 || !S_ISDIR(st.st_mode) ||
            stat(py_library, &st) != 0 || !S_ISREG(st.st_mode)) {
        system("osascript -e 'display alert \"Dikte\" message \"The Python this was installed against is gone, most likely after a brew upgrade. Run ./install.sh again.\"' >/dev/null 2>&1");
        return 1;
    }

    setenv("PYTHONHOME", py_home, 1);
    setenv("PYTHONPATH", py_site, 1);

    void *python = dlopen(py_library, RTLD_NOW | RTLD_GLOBAL);
    if (!python) {
        system("osascript -e 'display alert \"Dikte\" message \"Python could not be loaded. Run ./install.sh again.\"' >/dev/null 2>&1");
        return 1;
    }
    PyBytesMain py_main = (PyBytesMain)dlsym(python, "Py_BytesMain");
    if (!py_main) {
        system("osascript -e 'display alert \"Dikte\" message \"This Python cannot start Dikte. Run ./install.sh again.\"' >/dev/null 2>&1");
        return 1;
    }

    // ipc.launcher() includes the entry script when it re-executes this native
    // host. In that one case the argv is already a complete Python command;
    // adding the script and --gui again would leak the script path into Dikte's
    // own command arguments.
    if (argc > 1 && strcmp(argv[1], entry) == 0) {
        return py_main(argc, argv);
    }

    int new_argc = argc + 2;
    char **new_argv = malloc((new_argc + 1) * sizeof(char*));
    if (!new_argv) return 1;

    new_argv[0] = argv[0];
    new_argv[1] = (char*)entry;
    new_argv[2] = "--gui";
    for (int i = 1; i < argc; i++) {
        new_argv[i+2] = argv[i];
    }
    new_argv[new_argc] = NULL;

    int result = py_main(new_argc, new_argv);
    free(new_argv);
    return result;
}
EOF

# Try compiling the lightweight C wrapper
COMPILED_NATIVE=0
if command -v clang >/dev/null 2>&1; then
    if clang -x c "$LAUNCHER_SRC" -o "$APP/Contents/MacOS/Dikte" 2>/dev/null; then
        COMPILED_NATIVE=1
    fi
fi
rm -f "$LAUNCHER_SRC"

if [ $COMPILED_NATIVE -eq 0 ]; then
    warn "Could not compile native launcher. Falling back to bash wrapper (macOS 26 menu bar icon may break)."

    PY_REAL="$("$PY" -c 'import os, sys; print(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))')"
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
exec "\$HERE/python3" "$ENTRY" --gui "\$@"
EOF
fi
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
if PYTHONPATH="$DIR" "$PY" -m dikte.trayicon "$iconset" >/dev/null 2>&1 \
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
if codesign --force --sign - --identifier "$AGENT_ID" "$APP" 2>/dev/null \
   && codesign --verify --deep --strict "$APP" 2>/dev/null; then
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
# A wrapper, where Linux gets a symlink to the entry point. The shebang there is
# `env python3`, and on a Mac that is Apple's 3.9: the symlink would resolve to
# the one interpreter that cannot run this. Naming the interpreter here also
# gives update.sh and uninstall.sh somewhere to read it from, so that the three
# scripts cannot disagree about which Python this installation is on.
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/dikte" <<EOF
#!/bin/sh
# Written by install-mac.sh. Edit that, not this.
exec "$PY" "$ENTRY" "\$@"
EOF
chmod +x "$BIN_DIR/dikte" "$ENTRY"
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
#
# What was not asked for is read back out of the settings, which is what makes
# a second run of this script leave the keys you chose alone.
stored() { PYTHONPATH="$DIR" "$PY" -m dikte config get "$1" 2>/dev/null || true; }
if [[ -z "$SHORTCUT" ]]; then
  SHORTCUT="$(stored shortcut)"
  SHORTCUT="${SHORTCUT:-$DEFAULT_SHORTCUT}"
fi
if [[ $CANCEL_GIVEN == 0 ]]; then
  # An empty answer here is a discard key that was turned off, and it stays off.
  CANCEL_SHORTCUT="$(stored cancel_shortcut)"
fi

if [[ -n "$CANCEL_SHORTCUT" && "$SHORTCUT" == "$CANCEL_SHORTCUT" ]]; then
  warn "Both keys are $SHORTCUT, so the discard key was left out."
  say  "Pass two different combinations, or set it in Settings → Shortcuts."
  CANCEL_SHORTCUT=""
fi

register() {   # which  combination  label
  if out="$("$PY" "$ENTRY" shortcut install "$1" --combo "$2" 2>&1)"; then
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
