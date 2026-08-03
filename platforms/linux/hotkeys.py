"""GNOME/KDE global-shortcut installation plus a built-in evdev listener."""

import ast
import glob
import os
import pathlib
import re
import select
import shutil
import struct
import subprocess
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from i18n import t
from platforms.common.shortcuts import (  # noqa: F401
    ASK_DESKTOP_ID,
    CANCEL_DESKTOP_ID,
    DESKTOP_ID,
    MEETING_DESKTOP_ID,
)

APPLICATIONS_DIR = pathlib.Path.home() / ".local/share/applications"
DESKTOP_FILE = APPLICATIONS_DIR / DESKTOP_ID
SHORTCUTS_FILE = pathlib.Path.home() / ".config/kglobalshortcutsrc"
GNOME_MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_BINDING_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"

# --- evdev key codes (linux/input-event-codes.h) --------------------------

EV_KEY = 0x01
KEYS = {
    "space": 57, "tab": 15, "enter": 28, "return": 28, "esc": 1, "escape": 1,
    "backspace": 14, "insert": 110, "delete": 111, "home": 102, "end": 107,
    "pgup": 104, "pgdown": 109, "up": 103, "down": 108, "left": 105, "right": 106,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24,
    "p": 25, "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37,
    "l": 38, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66,
    "f9": 67, "f10": 68, "f11": 87, "f12": 88,
}
MODS = {
    "ctrl": (29, 97), "control": (29, 97),
    "shift": (42, 54),
    "alt": (56, 100),
    "meta": (125, 126), "super": (125, 126),
}
ALL_MOD_CODES = {code for pair in MODS.values() for code in pair}


def parse_shortcut(text):
    """'Ctrl+Space' -> ({'ctrl'}, 57), or (None, None) when unparsable."""
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    if not parts:
        return None, None
    mods, key = set(), None
    for part in parts:
        if part in MODS:
            mods.add("ctrl" if part == "control" else "super" if part == "meta" else part)
        else:
            key = KEYS.get(part)
            if key is None:
                return None, None
    if key is None:
        return None, None
    return mods, key


# --- built-in listener ----------------------------------------------------

class EvdevHotkey(QObject):
    """Catches global shortcuts by reading /dev/input directly.

    It does not swallow the key; the focused application sees the combination
    too. This is the fallback that works before the KDE shortcut goes live.
    """

    triggered = pyqtSignal(str)   # the name the binding was registered under
    failed = pyqtSignal(str)

    EVENT_FMT = "llHHi"
    EVENT_SIZE = struct.calcsize(EVENT_FMT)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._stop = threading.Event()
        self._bindings = {}   # key code -> [(mods, name)]

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, bindings):
        """`bindings` is {name: 'Ctrl+Space'}; an empty combination is skipped."""
        self.stop()
        parsed = {}
        for name, shortcut in bindings.items():
            if not shortcut:
                continue
            mods, key = parse_shortcut(shortcut)
            if key is None:
                self.failed.emit(
                    t("Could not parse the shortcut: {shortcut}", shortcut=shortcut)
                )
                continue
            parsed.setdefault(key, []).append((mods, name))
        if not parsed:
            return False
        devices = self._open_devices()
        if not devices:
            self.failed.emit(t(
                "Cannot read /dev/input. Your user needs to be in the 'input' group:\n"
                "  sudo usermod -aG input $USER   (then log out and back in)"
            ))
            return False
        self._bindings = parsed
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(devices,), daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
        self._thread = None

    def _open_devices(self):
        fds = []
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                continue
        return fds

    def _loop(self, fds):
        held = set()
        try:
            while not self._stop.is_set():
                # Short enough that stop() does not stall its caller waiting for
                # the read to come back around.
                ready, _, _ = select.select(fds, [], [], 0.15)
                for fd in ready:
                    try:
                        data = os.read(fd, self.EVENT_SIZE * 64)
                    except (BlockingIOError, OSError):
                        continue
                    for offset in range(0, len(data) - self.EVENT_SIZE + 1, self.EVENT_SIZE):
                        _s, _us, etype, code, value = struct.unpack(
                            self.EVENT_FMT, data[offset:offset + self.EVENT_SIZE]
                        )
                        if etype != EV_KEY:
                            continue
                        if code in ALL_MOD_CODES:
                            held.add(code) if value else held.discard(code)
                        elif value == 1:
                            for mods, name in self._bindings.get(code, ()):
                                if self._mods_match(held, mods):
                                    self.triggered.emit(name)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    @staticmethod
    def _mods_match(held, wanted):
        for name, codes in MODS.items():
            if name in ("control", "super"):
                continue
            pressed = any(code in held for code in codes)
            if pressed != (name in wanted):
                return False
        return True


# The listener the application starts when the desktop's own shortcut is not
# live yet. Windows has no equivalent and registers its combinations directly.
Listener = EvdevHotkey


# --- the desktop's own shortcut -------------------------------------------

def _gnome():
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    return "gnome" in desktop and shutil.which("gsettings") is not None


def _gnome_path(desktop_id):
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", desktop_id.removesuffix(".desktop"))
    return f"/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/{name}/"


def gnome_accelerator(shortcut):
    """Translate Qt-style Ctrl+Alt+A into GNOME's <Primary><Alt>a syntax."""
    parts = [part.strip() for part in str(shortcut).split("+") if part.strip()]
    modifiers = []
    key = ""
    names = {
        "ctrl": "<Primary>", "control": "<Primary>",
        "alt": "<Alt>", "shift": "<Shift>",
        "super": "<Super>", "meta": "<Super>",
    }
    for part in parts:
        modifier = names.get(part.lower())
        if modifier:
            if modifier not in modifiers:
                modifiers.append(modifier)
        else:
            key = part.lower() if len(part) == 1 else part
    return "".join(modifiers) + key if key else ""


def display_accelerator(accelerator):
    """Translate a GNOME accelerator back to the form shown in Dikte."""
    text = str(accelerator)
    parts = []
    for token, label in (("<Primary>", "Ctrl"), ("<Control>", "Ctrl"),
                         ("<Alt>", "Alt"), ("<Shift>", "Shift"),
                         ("<Super>", "Super")):
        if token.lower() in text.lower():
            parts.append(label)
            text = re.sub(re.escape(token), "", text, flags=re.IGNORECASE)
    key = text.strip()
    if len(key) == 1:
        key = key.upper()
    if key:
        parts.append(key)
    return "+".join(parts)


def _gsettings(*args, check=True):
    return subprocess.run(
        ["gsettings", *args], capture_output=True, text=True, timeout=10, check=check,
    )


def _gsettings_array(value):
    """Parse a gsettings string-array, including the empty `@as []` form."""
    text = str(value).strip()
    if text.startswith("@as "):
        text = text[4:].strip()
    parsed = ast.literal_eval(text) if text else []
    if not isinstance(parsed, (list, tuple)):
        raise ValueError(f"not a string array: {value}")
    return list(parsed)


def install_gnome_shortcut(shortcut, exec_command,
                           name="Dikte: start/stop recording",
                           desktop_id=DESKTOP_ID):
    path = _gnome_path(desktop_id)
    try:
        current = _gsettings(
            "get", GNOME_MEDIA_SCHEMA, "custom-keybindings"
        ).stdout.strip()
        paths = _gsettings_array(current)
        if path not in paths:
            paths.append(path)
            _gsettings("set", GNOME_MEDIA_SCHEMA, "custom-keybindings", repr(paths))
        schema = f"{GNOME_BINDING_SCHEMA}:{path}"
        _gsettings("set", schema, "name", repr(name))
        _gsettings("set", schema, "command", repr(exec_command))
        accelerator = gnome_accelerator(shortcut)
        if not accelerator:
            raise ValueError(t("Could not parse the shortcut: {shortcut}",
                               shortcut=shortcut))
        _gsettings("set", schema, "binding", repr(accelerator))
    except (ValueError, SyntaxError, subprocess.SubprocessError, OSError) as exc:
        return False, t("Could not register the GNOME shortcut: {error}", error=exc)
    return True, t("Shortcut saved: {shortcut}", shortcut=shortcut)


def remove_gnome_shortcut(desktop_id=DESKTOP_ID):
    path = _gnome_path(desktop_id)
    try:
        current = _gsettings(
            "get", GNOME_MEDIA_SCHEMA, "custom-keybindings"
        ).stdout.strip()
        paths = _gsettings_array(current)
        if path in paths:
            paths.remove(path)
            _gsettings("set", GNOME_MEDIA_SCHEMA, "custom-keybindings", repr(paths))
    except (ValueError, SyntaxError, subprocess.SubprocessError, OSError):
        pass


def gnome_shortcut_status(desktop_id=DESKTOP_ID):
    path = _gnome_path(desktop_id)
    try:
        current = _gsettings(
            "get", GNOME_MEDIA_SCHEMA, "custom-keybindings"
        ).stdout.strip()
        paths = _gsettings_array(current)
        if path not in paths:
            return None
        value = _gsettings(
            "get", f"{GNOME_BINDING_SCHEMA}:{path}", "binding"
        ).stdout.strip()
        accelerator = ast.literal_eval(value)
        return display_accelerator(accelerator) if accelerator else None
    except (ValueError, SyntaxError, subprocess.SubprocessError, OSError):
        return None


def install_shortcut(shortcut, exec_command, name="Dikte: start/stop recording",
                     desktop_id=DESKTOP_ID):
    if _gnome():
        return install_gnome_shortcut(shortcut, exec_command, name, desktop_id)
    return install_kde_shortcut(shortcut, exec_command, name, desktop_id)


def remove_shortcut(desktop_id=DESKTOP_ID):
    if _gnome():
        remove_gnome_shortcut(desktop_id)
    else:
        remove_kde_shortcut(desktop_id)


def shortcut_status(desktop_id=DESKTOP_ID):
    return (gnome_shortcut_status(desktop_id) if _gnome()
            else kde_shortcut_status(desktop_id))


def desktop_name():
    return "GNOME" if _gnome() else "KDE"


# --- KDE ------------------------------------------------------------------

def install_kde_shortcut(shortcut, exec_command, name="Dikte: start/stop recording",
                         desktop_id=DESKTOP_ID):
    """Write the desktop file and the kglobalshortcutsrc entry.

    KWin only reads that file at startup, so the entry goes live after the next
    login. Returns (True, message) or (False, error).
    """
    desktop_file = APPLICATIONS_DIR / desktop_id
    try:
        desktop_file.parent.mkdir(parents=True, exist_ok=True)
        desktop_file.write_text(
            "[Desktop Entry]\n"
            f"Exec={exec_command}\n"
            f"Name={name}\n"
            "NoDisplay=true\n"
            "StartupNotify=false\n"
            "Type=Application\n"
            "X-KDE-GlobalAccel-CommandShortcut=true\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return False, t("Could not write the desktop file: {error}", error=exc)

    try:
        subprocess.run(
            ["kwriteconfig6", "--notify", "--file", "kglobalshortcutsrc",
             "--group", "services", "--group", desktop_id,
             "--key", "_launch", shortcut],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, t("Could not write kglobalshortcutsrc: {error}", error=exc)

    return True, t(
        "Shortcut saved: {shortcut}\nKWin only reads this file at startup, so it "
        "will not fire until you log out and back in. To use it right away, turn "
        "on the built-in listener.",
        shortcut=shortcut,
    )


def remove_kde_shortcut(desktop_id=DESKTOP_ID):
    try:
        (APPLICATIONS_DIR / desktop_id).unlink(missing_ok=True)
    except OSError:
        pass
    # kwriteconfig6 deletes keys rather than groups, so both of the ones KDE
    # keeps in there go and the empty group is left behind harmlessly.
    for key in ("_launch", "_k_friendly_name"):
        try:
            subprocess.run(
                ["kwriteconfig6", "--notify", "--file", "kglobalshortcutsrc",
                 "--group", "services", "--group", desktop_id,
                 "--key", key, "--delete"],
                capture_output=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            pass


def kde_shortcut_status(desktop_id=DESKTOP_ID):
    """The registered shortcut, or None."""
    if not (APPLICATIONS_DIR / desktop_id).exists():
        return None
    try:
        text = SHORTCUTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(
        r"\[services\]\[" + re.escape(desktop_id) + r"\]\n_launch=([^\n]*)", text
    )
    if not match:
        return None
    value = match.group(1).split("\t")[0].strip()
    return value or None


def conflicting_shortcuts(shortcut, desktop_id=DESKTOP_ID):
    """Names of other KDE entries bound to the same combination."""
    try:
        text = SHORTCUTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    hits, section = [], ""
    for line in text.splitlines():
        if line.startswith("["):
            section = line.strip("[]").replace("][", " / ")
            continue
        if "=" not in line or desktop_id in section:
            continue
        key, _, value = line.partition("=")
        if shortcut.lower() in value.lower().split(","):
            hits.append(f"{section} → {key}")
        elif any(shortcut.lower() == part.strip().lower()
                 for part in re.split(r"[,\t]", value)):
            hits.append(f"{section} → {key}")
    return hits
