#!/usr/bin/env bash
# Dikte installer: dependency check, launchers, global shortcuts.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"

MACOS=0
[[ "$(uname -s)" == "Darwin" ]] && MACOS=1
MAC_APP="$HOME/Applications/Dikte.app"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.github.yusufipk.dikte.plist"
LOG_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/dikte.log"

# Ctrl+Space belongs to the input source switcher on macOS: a key pressed there
# never reaches Dikte, so the default is one modifier along.
if ((MACOS)); then
  SHORTCUT="${1:-Ctrl+Alt+Space}"
  CANCEL_SHORTCUT="${2-Ctrl+Alt+D}"
else
  SHORTCUT="${1:-Ctrl+Space}"
  # Without the colon, so that a second argument given as "" stays empty. That
  # is how update.sh says "this one was turned off", as against not saying
  # anything.
  CANCEL_SHORTCUT="${2-Ctrl+Alt+Space}"
fi

# The interpreter that can actually run this: PyQt6 is a system package on
# Linux and a pip install on macOS, and on a Mac `python3` is Apple's 3.9 with
# nothing in it. Asked of each candidate rather than assumed, and $DIKTE_PYTHON
# wins so that a virtualenv can be pointed at.
find_python() {
  local candidate found
  for candidate in "${DIKTE_PYTHON:-}" "$DIR/.venv/bin/python3" \
                   python3 python3.13 python3.12 python3.11; do
    [[ -n "$candidate" ]] || continue
    found="$(command -v "$candidate" 2>/dev/null)" || continue
    if "$found" -c 'import PyQt6.QtWidgets' 2>/dev/null; then
      printf '%s' "$found"
      return 0
    fi
  done
  command -v python3 || true
}
PY="$(find_python)"

say()  { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

echo
echo "Installing Dikte"
echo "────────────────"

# 1. Dependencies ----------------------------------------------------------
missing=()
if ((MACOS)); then
  # pbcopy and osascript come with the system; ffmpeg is the recorder as well
  # as the file reader here, and whisper-cpp is Homebrew's because ggml-org
  # publishes no macOS build of it.
  for cmd in ffmpeg whisper-server; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
  done
  "$PY" -c 'import PyQt6.QtWidgets' 2>/dev/null || missing+=("PyQt6")
  if ((${#missing[@]})); then
    warn "Missing: ${missing[*]}"
    say  "brew install ffmpeg whisper-cpp python@3.13"
    say  "python3.13 -m pip install PyQt6"
    say  "A virtualenv works too; point DIKTE_PYTHON at its python3."
    echo
  else
    ok "All dependencies present"
  fi
else
  audio_cmds=(ffmpeg)
  if command -v parec >/dev/null || command -v pw-record >/dev/null; then
    :
  else
    missing+=("pulseaudio-utils-or-pipewire-audio")
  fi
  if [[ "${XDG_SESSION_TYPE:-}" == "x11" ]]; then
    desktop_cmds=(xclip xdotool)
  else
    desktop_cmds=(wl-copy wl-paste ydotool)
  fi
  for cmd in "${audio_cmds[@]}" "${desktop_cmds[@]}"; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
  done
  "$PY" -c 'import PyQt6.QtWidgets' 2>/dev/null || missing+=("python-pyqt6")

  if ((${#missing[@]})); then
    warn "Missing: ${missing[*]}"
    say  "Ubuntu X11:    sudo apt install pulseaudio-utils xclip xdotool ffmpeg"
    say  "Arch Wayland:  sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6"
    echo
  else
    ok "All dependencies present"
  fi
fi

# 2. Permission to type on your behalf --------------------------------------
# ydotool on Linux needs a daemon running; macOS needs the terminal or the app
# bundle to have been allowed to control the computer. Neither can be arranged
# from a script, so both are said out loud.
if ((MACOS)); then
  say  "Auto-paste presses Cmd+V itself, which macOS grants per application:"
  say  "System Settings → Privacy & Security → Accessibility → allow Dikte"
  say  "(or the terminal you start it from). Dikte asks on its first run, and"
  say  "says so in the menu bar when it has not been allowed."
elif [[ "${XDG_SESSION_TYPE:-}" != "x11" ]] && command -v ydotool >/dev/null; then
  if systemctl --user is-active --quiet ydotool 2>/dev/null \
     || systemctl --user is-active --quiet ydotoold 2>/dev/null; then
    ok "ydotoold is running (auto-paste ready)"
  else
    warn "ydotoold is not running, auto-paste will not work"
    say  "systemctl --user enable --now ydotool"
  fi
fi

# 3. Launchers -------------------------------------------------------------
mkdir -p "$BIN_DIR"
chmod +x "$DIR/dikte.py"
if ((MACOS)); then
  # A wrapper rather than a symlink: dikte.py's shebang finds `python3`, and on
  # a Mac that is Apple's 3.9 with no PyQt6 in it. The interpreter that was
  # found above is written in here instead.
  cat > "$BIN_DIR/dikte" <<EOF
#!/bin/sh
# Written by Dikte's install.sh; re-run it to point this somewhere else.
exec "$PY" "$DIR/dikte.py" "\$@"
EOF
  chmod +x "$BIN_DIR/dikte"
else
  mkdir -p "$APP_DIR" "$AUTOSTART_DIR"
  ln -sf "$DIR/dikte.py" "$BIN_DIR/dikte"
fi
ok "Command installed: $BIN_DIR/dikte"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. For fish: fish_add_path $BIN_DIR" ;;
esac

if ((MACOS)); then
  # An .app so that macOS has something to show in Spotlight and in Login
  # Items, and LSUIElement so that it stays out of the Dock: the tray icon in
  # the menu bar is the whole of Dikte's presence on screen.
  mkdir -p "$MAC_APP/Contents/MacOS"
  cat > "$MAC_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Dikte</string>
  <key>CFBundleDisplayName</key><string>Dikte</string>
  <key>CFBundleIdentifier</key><string>com.github.yusufipk.dikte</string>
  <key>CFBundleExecutable</key><string>dikte</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Dikte records what you dictate so it can be transcribed.</string>
</dict>
</plist>
EOF
  # --gui, because LaunchServices starts this with no arguments and dikte.py
  # with none of those is the command line looking for an instance to talk to.
  cat > "$MAC_APP/Contents/MacOS/dikte" <<EOF
#!/bin/sh
exec "$PY" "$DIR/dikte.py" --gui "\$@"
EOF
  chmod +x "$MAC_APP/Contents/MacOS/dikte"
  ok "Application bundle: $MAC_APP"

  mkdir -p "$(dirname "$LAUNCH_AGENT")" "$(dirname "$LOG_FILE")"
  cat > "$LAUNCH_AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.github.yusufipk.dikte</string>
  <key>ProgramArguments</key>
  <array>
    <string>$MAC_APP/Contents/MacOS/dikte</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>ProcessType</key><string>Interactive</string>
  <!-- Started by launchd, so there is no terminal for it to complain to.
       Without these, a Dikte that dies on startup does so in silence. -->
  <key>StandardOutPath</key><string>$LOG_FILE</string>
  <key>StandardErrorPath</key><string>$LOG_FILE</string>
</dict>
</plist>
EOF
  # Already loaded from a previous install, most likely; booting it out first
  # is what makes this re-runnable. Neither step is worth failing over, since
  # the next login loads it either way.
  launchctl bootout "gui/$(id -u)/com.github.yusufipk.dikte" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT" 2>/dev/null; then
    ok "Will start automatically on login (and is starting now)"
  else
    warn "Could not load the launch agent; it will start at your next login."
  fi
else
  cat > "$APP_DIR/dikte.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Dikte
Comment=Voice dictation: record, transcribe, clean up, paste
Exec=$PY $DIR/dikte.py
Icon=audio-input-microphone
Categories=Utility;AudioVideo;
StartupNotify=false
EOF
  ok "Application menu entry added"

  cat > "$AUTOSTART_DIR/dikte.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Dikte
Exec=$PY $DIR/dikte.py
Icon=audio-input-microphone
X-GNOME-Autostart-enabled=true
StartupNotify=false
EOF
  ok "Will start automatically on login"
fi

# 4. Global shortcuts ------------------------------------------------------
# Two of them: one to start and stop, one to throw the recording away. The
# second is worth a key of its own because stopping is the step there is no
# taking back, being what sends the audio off to be transcribed.
#
# Dikte registers them rather than this script writing the files itself: it
# knows which desktop it is on, and it stores the combination in the settings
# as well, which is where the built-in listener reads it from. A key written
# to only one of the two places is a key that half works.
if [[ "$SHORTCUT" == "$CANCEL_SHORTCUT" ]]; then
  warn "Both arguments are $SHORTCUT, so the discard key was left out."
  say  "Pass two different combinations, or set it in Settings → Shortcuts."
  CANCEL_SHORTCUT=""
fi

register() {   # which  combination  label
  if out="$("$PY" "$DIR/dikte.py" shortcut install "$1" --combo "$2" 2>&1)"; then
    ok "$3: $2"
  else
    # One line: the rest of what it has to say about KWin is printed below.
    warn "${out%%$'\n'*}"
  fi
}

if "$PY" -c 'import PyQt6.QtWidgets' 2>/dev/null; then
  register toggle "$SHORTCUT" "Start and stop"
  if [[ -n "$CANCEL_SHORTCUT" ]]; then
    register cancel "$CANCEL_SHORTCUT" "Discard the recording"
  fi
  if ((MACOS)); then
    say  "Dikte registers these itself, so they work as soon as it runs. macOS"
    say  "keeps its own shortcuts to itself: if a key does nothing, something"
    say  "else already has it — pick another in Settings → Shortcuts."
  elif [[ "${XDG_CURRENT_DESKTOP:-}" != *[Gg][Nn][Oo][Mm][Ee]* ]]; then
    warn "KWin only reads these at startup, so they go live after your next"
    say  "login. Until then open Settings → Shortcuts and turn on the"
    say  "built-in listener to use them right away."
  fi
else
  warn "PyQt6 is missing, so no shortcut was registered. Install it, then run:"
  say  "dikte shortcut install toggle --combo '$SHORTCUT'"
fi

echo
ok "Done. Start it with:  dikte"
say "The settings window opens on first run: download a speech model, or add"
say "an OpenAI or OpenRouter key instead."
echo
