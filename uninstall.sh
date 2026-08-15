#!/usr/bin/env bash
# Dikte uninstaller: takes back what install.sh put down, and nothing else
# unless asked. Your settings and your dictations survive a plain run; --purge
# is the word that deletes them.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
BIN_DIR="$HOME/.local/bin"

# What an installation is made of differs by platform, but what --purge deletes
# and what it asks before deleting it does not, which is why this is one script
# with two sets of paths rather than two scripts.
if [[ "$(uname -s)" == "Darwin" ]]; then
  MACOS=1
  MAC_APP="$HOME/Applications/Dikte.app"
  AGENT_ID="io.github.yusufipk.dikte"
  AGENT="$HOME/Library/LaunchAgents/$AGENT_ID.plist"
  CONFIG_DIR="$HOME/Library/Application Support/Dikte"
  DATA_DIR="$CONFIG_DIR"
  # The interpreter this installation was put on, which is not necessarily the
  # python3 on PATH: Apple's is 3.9 and cannot run Dikte. The wrapper written
  # by install-mac.sh names it, so it is read back out of there.
  # `|| true` because there may be no wrapper to read: pipefail would otherwise
  # make a missing file the end of the script rather than a question answered no.
  PY="$(sed -n 's/^exec "\([^"]*\)".*/\1/p' "$BIN_DIR/dikte" 2>/dev/null | head -1 || true)"
  [[ -x "$PY" ]] || PY="$(command -v python3 || true)"
else
  MACOS=0
  APP_DIR="$HOME/.local/share/applications"
  AUTOSTART_DIR="$HOME/.config/autostart"
  CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/dikte"
  DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/dikte"
  PY="$(command -v python3 || true)"
fi

PURGE=0
ASSUME_YES=0

say()  { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
gone() { printf '  \033[90m·\033[0m %s\n' "$1"; }
# "1 dictation", "3 dictations": how many is the point of printing it at all.
count() {
  if (( $1 == 1 )); then printf '%s %s' "$1" "$2"; else printf '%s %s' "$1" "$3"; fi
}

usage() {
  cat <<EOF
Usage: ./uninstall.sh [--purge] [--yes]

  --purge   also delete the settings ($CONFIG_DIR)
            and the dictations, meetings and recordings ($DATA_DIR)
  --yes     do not ask before deleting those

Without --purge nothing you have written is touched, and the source directory
is left alone either way.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'uninstall.sh: unknown option: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done

# A symlink whose target is gone is still a file to remove, hence -L.
remove() {
  if [[ -e "$1" || -L "$1" ]]; then
    rm -f "$1"
    ok "Removed $1"
  else
    gone "Was not there: $1"
  fi
}

echo
echo "Uninstalling Dikte"
echo "──────────────────"

# 1. Global shortcuts ------------------------------------------------------
# Handed to Dikte while it can still run, because it is the half that knows
# whether they went into KDE's kglobalshortcutsrc or GNOME's gsettings. macOS
# keeps no registry: the combinations are held by the running process and are
# gone the moment it stops, so there is nothing here to take back.
if ((MACOS)); then
  say "Nothing to unregister: macOS shortcuts live only while Dikte runs."
elif [[ -n "$PY" ]] && "$PY" -c 'import PyQt6.QtWidgets' 2>/dev/null; then
  for which in toggle cancel ask meeting; do
    "$PY" "$DIR/dikte.py" shortcut remove "$which" >/dev/null 2>&1 || true
  done
  ok "Global shortcuts unregistered"
  say "KWin reads that file at startup, so the keys are free after your next login."
else
  warn "PyQt6 is missing, so the shortcuts were left registered."
  say  "Remove them in your desktop's shortcut settings."
fi

# 2. The running instance --------------------------------------------------
# It holds a tray icon and a socket; asking it to quit is tidier than pulling
# its launchers out from under it.
if pgrep -u "$USER_NAME" -f 'dikte\.py' >/dev/null 2>&1; then
  [[ -n "$PY" ]] && "$PY" "$DIR/dikte.py" quit >/dev/null 2>&1 || true
  sleep 0.5
  if pgrep -u "$USER_NAME" -f 'dikte\.py' >/dev/null 2>&1; then
    warn "Dikte is still running; close it from the tray icon"
  else
    ok "Stopped the running instance"
  fi
fi

# 3. Launchers -------------------------------------------------------------
# Only what this installer wrote goes: a file of the same name that somebody
# else put there is not ours to delete. On Linux ours is a symlink; on macOS it
# is a generated wrapper, which says so in its second line.
if [[ -L "$BIN_DIR/dikte" ]]; then
  remove "$BIN_DIR/dikte"
elif ((MACOS)) && grep -q 'Written by install-mac.sh' "$BIN_DIR/dikte" 2>/dev/null; then
  remove "$BIN_DIR/dikte"
elif [[ -e "$BIN_DIR/dikte" ]]; then
  warn "$BIN_DIR/dikte is not ours, leaving it alone"
else
  gone "Was not there: $BIN_DIR/dikte"
fi

if ((MACOS)); then
  # The login item first: bootout while the plist is still there, because
  # launchctl is told which job by the file as much as by the label.
  if [[ -e "$AGENT" ]]; then
    launchctl bootout "gui/$(id -u)/$AGENT_ID" >/dev/null 2>&1 || true
  fi
  remove "$AGENT"
  if [[ -d "$MAC_APP" ]]; then
    rm -rf "$MAC_APP"
    ok "Removed $MAC_APP"
  else
    gone "Was not there: $MAC_APP"
  fi
  # macOS keeps the permissions filed against the bundle that asked for them,
  # and deleting the bundle does not withdraw them.
  say "Microphone and Accessibility are still granted to Dikte; take them back"
  say "under System Settings → Privacy & Security if you want them gone."
fi
if ((!MACOS)); then
  remove "$APP_DIR/dikte.desktop"
  remove "$AUTOSTART_DIR/dikte.desktop"
  # Removing the shortcut takes its desktop file with it, but an install from
  # before this script existed may have left one behind on a desktop that never
  # used them.
  for id in dikte-toggle dikte-cancel dikte-ask dikte-meeting; do
    if [[ -e "$APP_DIR/$id.desktop" ]]; then
      remove "$APP_DIR/$id.desktop"
    fi
  done
fi

# 4. Settings and dictations -----------------------------------------------
echo
if ((PURGE)); then
  warn "--purge also deletes:"
  if [[ -f "$CONFIG_DIR/config.json" ]]; then
    say "$CONFIG_DIR/config.json  (your API keys and every setting)"
  fi
  if [[ -f "$DATA_DIR/history.jsonl" ]]; then
    # grep -c rather than wc -l: a last line with no newline is still a dictation.
    say "$DATA_DIR/history.jsonl  ($(count "$(grep -c '' "$DATA_DIR/history.jsonl" 2>/dev/null || echo 0)" dictation dictations))"
  fi
  if [[ -d "$DATA_DIR/meetings" ]]; then
    say "$DATA_DIR/meetings  ($(count "$(find "$DATA_DIR/meetings" -name '*.md' | wc -l)" meeting meetings))"
  fi
  if [[ -d "$DATA_DIR/recordings" ]]; then
    say "$DATA_DIR/recordings  ($(du -sh "$DATA_DIR/recordings" | cut -f1) of audio)"
  fi

  if ((!ASSUME_YES)); then
    if [[ -t 0 ]]; then
      printf '  Type yes to delete them: '
      read -r reply
      [[ "$reply" == "yes" ]] || { PURGE=0; say "Kept."; }
    else
      PURGE=0
      warn "Not a terminal, so nothing was deleted. Pass --yes if you meant it."
    fi
  fi
fi

if ((PURGE)); then
  rm -rf "$CONFIG_DIR" "$DATA_DIR"
  ok "Settings and dictations deleted"
elif [[ "$CONFIG_DIR" == "$DATA_DIR" ]]; then
  # macOS keeps both in the one directory a Mac user's backup already knows
  # about, so naming it twice would only look like two things were kept.
  say "Settings and dictations kept:  $CONFIG_DIR"
  say "Delete them too with:  ./uninstall.sh --purge"
else
  say "Settings kept:     $CONFIG_DIR"
  say "Dictations kept:   $DATA_DIR"
  say "Delete them too with:  ./uninstall.sh --purge"
fi

echo
ok "Done."
say "The source directory is untouched: $DIR"
echo
