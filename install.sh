#!/usr/bin/env bash
# Dikte installer: dependency check, launchers, global shortcuts.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Nothing below this line is true on a Mac: no XDG directories, no .desktop
# files, no shortcut registry, and an application is a bundle rather than a
# path. That is a second script rather than a branch through this one, and
# update.sh reaches it through here without having to know which it is on.
if [[ "$(uname -s)" == "Darwin" ]]; then
  exec "$DIR/scripts/install-mac.sh" "$@"
fi

PY="$(command -v python3)"
# The one file that starts the application, whoever is asking: the launcher
# below, both .desktop files, and every shortcut Dikte registers.
ENTRY="$DIR/dikte/__main__.py"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons"
# Only the one: the discard key's default is the settings' own, read back below.
DEFAULT_SHORTCUT="Ctrl+Space"
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

say()  { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

echo
echo "Installing Dikte"
echo "────────────────"

# 1. Dependencies ----------------------------------------------------------
missing=()
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
python3 -c 'import PyQt6.QtWidgets' 2>/dev/null || missing+=("python-pyqt6")

if ((${#missing[@]})); then
  warn "Missing: ${missing[*]}"
  say  "Ubuntu X11:     sudo apt install pulseaudio-utils xclip xdotool ffmpeg"
  say  "Arch Wayland:   sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6"
  say  "Fedora Wayland: sudo dnf install pipewire-utils wl-clipboard ydotool ffmpeg-free python3-pyqt6"
  echo
else
  ok "All dependencies present"
fi

# 2. ydotoold --------------------------------------------------------------
# What auto-paste needs is a socket it may write to, which is not the same
# question as whether the unit is up: Fedora ships ydotool as a system service
# only, and its socket stays root-owned at mode 600, so there the daemon can be
# running while every paste is refused. The socket file outlives the daemon,
# though, so the process has to be there as well for the answer to be yes.
if [[ "${XDG_SESSION_TYPE:-}" != "x11" ]] && command -v ydotool >/dev/null; then
  socket="${YDOTOOL_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/.ydotool_socket}"
  alive() { pgrep -x ydotoold >/dev/null 2>&1; }
  if [[ -w "$socket" ]] && alive; then
    ok "ydotoold is running (auto-paste ready)"
  elif systemctl is-active --quiet ydotool 2>/dev/null; then
    warn "ydotoold's socket is not yours to write to, so auto-paste will fail"
    say  "Hand it over with the drop-in in docs/install.md's Fedora section."
  elif alive; then
    warn "ydotoold is running, but it did not put its socket at $socket"
    say  "Point Dikte at the one it did make: export YDOTOOL_SOCKET=..."
  else
    warn "ydotoold is not running, auto-paste will not work"
    say  "systemctl --user enable --now ydotool   (on Fedora: see docs/install.md)"
  fi
fi

# 3. Launchers -------------------------------------------------------------
mkdir -p "$BIN_DIR" "$APP_DIR" "$AUTOSTART_DIR" "$ICON_DIR"
ln -sf "$ENTRY" "$BIN_DIR/dikte"
chmod +x "$ENTRY"
ok "Command installed: $BIN_DIR/dikte"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. For fish: fish_add_path $BIN_DIR" ;;
esac

# The icon, drawn by trayicon.py so that there is no binary in the repository,
# and installed under a name of our own. Naming a theme icon like
# audio-input-microphone instead only works where a theme has it: on i3 or a
# bare X11 login Qt is left with hicolor, which has no such name, and the entry
# comes out blank. hicolor is also where this goes, since it is the theme every
# desktop must fall back to.
if PYTHONPATH="$DIR" "$PY" -m dikte.trayicon --hicolor "$ICON_DIR" >/dev/null 2>&1; then
  ICON=dikte
  # Only GTK reads a cache, and only if one is already there; a stale cache
  # would otherwise hide the file we just wrote.
  if command -v gtk-update-icon-cache >/dev/null \
     && [[ -f "$ICON_DIR/hicolor/icon-theme.cache" ]]; then
    gtk-update-icon-cache -q -f -t "$ICON_DIR/hicolor" 2>/dev/null || true
  fi
  ok "Icon installed: $ICON_DIR/hicolor"
else
  ICON=audio-input-microphone
  warn "Could not draw the icon, so the entries name your theme's microphone"
fi

cat > "$APP_DIR/dikte.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Dikte
Comment=Voice dictation: record, transcribe, clean up, paste
Exec=$PY $ENTRY
Icon=$ICON
Categories=Utility;AudioVideo;
StartupNotify=false
EOF
ok "Application menu entry added"

cat > "$AUTOSTART_DIR/dikte.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Dikte
Exec=$PY $ENTRY --autostart
Icon=$ICON
X-GNOME-Autostart-enabled=true
StartupNotify=false
EOF
ok "Will start automatically on login"

# 4. Global shortcuts ------------------------------------------------------
# Two of them: one to start and stop, one to throw the recording away. The
# second is worth a key of its own because stopping is the step there is no
# taking back, being what sends the audio off to be transcribed.
#
# Dikte registers them rather than this script writing the files itself: it
# knows which desktop it is on, and it stores the combination in the settings
# as well, which is where the built-in listener reads it from. A key written
# to only one of the two places is a key that half works.
# What was not asked for is read back out of the settings, which is where Dikte
# keeps the keys and so what a second run of this script has to leave alone.
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
    # One line: the rest of what it has to say about KWin is printed below.
    warn "${out%%$'\n'*}"
  fi
}

if python3 -c 'import PyQt6.QtWidgets' 2>/dev/null; then
  register toggle "$SHORTCUT" "Start and stop"
  if [[ -n "$CANCEL_SHORTCUT" ]]; then
    register cancel "$CANCEL_SHORTCUT" "Discard the recording"
  fi
  # Which of the three mechanisms this session got is Dikte's answer to give,
  # not this script's. Guessing from XDG_CURRENT_DESKTOP here is how every
  # session that was neither GNOME nor KDE used to be promised a KWin that was
  # never running.
  case "$(PYTHONPATH="$DIR" "$PY" -c 'from dikte import hotkey; print(hotkey.backend())' 2>/dev/null)" in
    kde)
      warn "KWin only reads these at startup, so they go live after your next"
      say  "login. Until then open Settings → Shortcuts and turn on the"
      say  "built-in listener to use them right away."
      ;;
    gnome) ;;
    *)
      if id -nG 2>/dev/null | tr ' ' '\n' | grep -qx input; then
        say "Your desktop keeps no shortcut registry, so Dikte listens for these"
        say "keys itself while it is running."
      else
        warn "Your desktop keeps no shortcut registry, so Dikte listens for these"
        say  "keys itself, and it cannot read /dev/input yet:"
        say  "  sudo usermod -aG input $(id -un)   (then log out and back in)"
      fi
      ;;
  esac
else
  warn "PyQt6 is missing, so no shortcut was registered. Install it, then run:"
  say  "dikte shortcut install toggle --combo '$SHORTCUT'"
fi

echo
ok "Done. Start it with:  dikte"
say "The settings window opens on first run: download a speech model, or add"
say "an OpenAI or OpenRouter key instead."
echo
