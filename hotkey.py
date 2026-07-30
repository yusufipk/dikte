"""Global shortcuts for KDE/evdev and macOS Carbon."""

import ctypes
import ctypes.util
import glob
import os
import pathlib
import re
import select
import struct
import subprocess
import sys
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from i18n import t

DESKTOP_ID = "dikte-toggle.desktop"
MEETING_DESKTOP_ID = "dikte-meeting.desktop"
ASK_DESKTOP_ID = "dikte-ask.desktop"
APPLICATIONS_DIR = pathlib.Path.home() / ".local/share/applications"
DESKTOP_FILE = APPLICATIONS_DIR / DESKTOP_ID
SHORTCUTS_FILE = pathlib.Path.home() / ".config/kglobalshortcutsrc"
IS_MACOS = sys.platform == "darwin"
_MAC_ACTIVE = {}

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

class LinuxHotkey(QObject):
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


# --- macOS Carbon hotkeys -------------------------------------------------

def _fourcc(text):
    return int.from_bytes(text.encode("ascii"), "big")


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32),
                ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32),
                ("id", ctypes.c_uint32)]


_MAC_KEYS = {
    "space": 49, "tab": 48, "enter": 36, "return": 36, "esc": 53, "escape": 53,
    "backspace": 51, "delete": 117, "home": 115, "end": 119,
    "pgup": 116, "pgdown": 121, "up": 126, "down": 125, "left": 123, "right": 124,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25, "0": 29,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31,
    "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9,
    "w": 13, "x": 7, "y": 16, "z": 6,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
_MAC_MODS = {
    "cmd": 1 << 8, "command": 1 << 8, "meta": 1 << 8, "super": 1 << 8,
    "shift": 1 << 9,
    "alt": 1 << 11, "option": 1 << 11,
    "ctrl": 1 << 12, "control": 1 << 12,
}


def _parse_macos_shortcut(text):
    parts = [part.strip().lower() for part in str(text).split("+") if part.strip()]
    if not parts:
        return None, None
    modifiers, key = 0, None
    for part in parts:
        if part in _MAC_MODS:
            modifiers |= _MAC_MODS[part]
        elif part in _MAC_KEYS and key is None:
            key = _MAC_KEYS[part]
        else:
            return None, None
    return modifiers, key


class MacHotkey(QObject):
    """Registers global shortcuts with Carbon's system hotkey API.

    RegisterEventHotKey does not read the whole keyboard stream and therefore
    does not need Input Monitoring permission. Accessibility is still needed
    later when Dikte sends Cmd+V into the focused application.
    """

    triggered = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._carbon = None
        self._callback = None
        self._handler = ctypes.c_void_p()
        self._registrations = []
        self._names = {}

    @property
    def running(self):
        return bool(self._registrations)

    def start(self, bindings):
        self.stop()
        path = ctypes.util.find_library("Carbon")
        if not path:
            self.failed.emit(
                "Carbon.framework is unavailable; global shortcuts cannot run."
            )
            return False
        self._carbon = ctypes.CDLL(path)
        self._configure_api()

        callback_type = ctypes.CFUNCTYPE(
            ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )

        def pressed(_next_handler, event, _user_data):
            hotkey_id = _EventHotKeyID()
            size = ctypes.c_uint32()
            result = self._carbon.GetEventParameter(
                event, _fourcc("----"), _fourcc("hkid"), None,
                ctypes.sizeof(hotkey_id), ctypes.byref(size), ctypes.byref(hotkey_id),
            )
            if result == 0:
                name = self._names.get(hotkey_id.id)
                if name:
                    self.triggered.emit(name)
            return 0

        self._callback = callback_type(pressed)
        event_type = _EventTypeSpec(_fourcc("keyb"), 5)  # kEventHotKeyPressed
        result = self._carbon.InstallEventHandler(
            self._carbon.GetApplicationEventTarget(), self._callback, 1,
            ctypes.byref(event_type), None, ctypes.byref(self._handler),
        )
        if result != 0:
            self.failed.emit(f"Could not install the macOS hotkey handler ({result}).")
            self._callback = None
            return False

        for identifier, (name, shortcut) in enumerate(bindings.items(), 1):
            if not shortcut:
                continue
            modifiers, key = _parse_macos_shortcut(shortcut)
            if key is None:
                self.failed.emit(
                    t("Could not parse the shortcut: {shortcut}", shortcut=shortcut)
                )
                continue
            reference = ctypes.c_void_p()
            result = self._carbon.RegisterEventHotKey(
                key, modifiers, _EventHotKeyID(_fourcc("Dikt"), identifier),
                self._carbon.GetApplicationEventTarget(), 0, ctypes.byref(reference),
            )
            if result != 0:
                self.failed.emit(
                    f"macOS could not register {shortcut} ({result}); "
                    "another app may already use it."
                )
                continue
            self._registrations.append(reference)
            self._names[identifier] = name
            desktop_id = {
                "toggle": DESKTOP_ID,
                "meeting": MEETING_DESKTOP_ID,
                "ask": ASK_DESKTOP_ID,
            }.get(name)
            if desktop_id:
                _MAC_ACTIVE[desktop_id] = shortcut
        return bool(self._registrations)

    def stop(self):
        if self._carbon:
            for reference in self._registrations:
                self._carbon.UnregisterEventHotKey(reference)
            if self._handler:
                self._carbon.RemoveEventHandler(self._handler)
        self._registrations = []
        self._names = {}
        self._handler = ctypes.c_void_p()
        self._callback = None
        _MAC_ACTIVE.clear()

    def _configure_api(self):
        carbon = self._carbon
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.restype = ctypes.c_int32
        carbon.RegisterEventHotKey.restype = ctypes.c_int32
        carbon.UnregisterEventHotKey.restype = ctypes.c_int32
        carbon.RemoveEventHandler.restype = ctypes.c_int32
        carbon.GetEventParameter.restype = ctypes.c_int32

        carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        carbon.GetEventParameter.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]


# --- KDE custom shortcut --------------------------------------------------

def install_kde_shortcut(shortcut, exec_command, name="Dikte: start/stop recording",
                         desktop_id=DESKTOP_ID):
    """Write the desktop file and the kglobalshortcutsrc entry.

    KWin only reads that file at startup, so the entry goes live after the next
    login. Returns (True, message) or (False, error).
    """
    if IS_MACOS:
        return True, t(
            "Shortcut saved: {shortcut}\nSave the settings to activate it on macOS.",
            shortcut=shortcut,
        )

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
    if IS_MACOS:
        _MAC_ACTIVE.pop(desktop_id, None)
        return
    try:
        (APPLICATIONS_DIR / desktop_id).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        subprocess.run(
            ["kwriteconfig6", "--notify", "--file", "kglobalshortcutsrc",
             "--group", "services", "--group", desktop_id, "--key", "_launch", "--delete"],
            capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def kde_shortcut_status(desktop_id=DESKTOP_ID):
    """The registered shortcut, or None."""
    if IS_MACOS:
        return _MAC_ACTIVE.get(desktop_id)
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
    if IS_MACOS:
        return []
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


EvdevHotkey = MacHotkey if IS_MACOS else LinuxHotkey
