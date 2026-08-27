"""Global shortcuts: the desktop's own registry, plus a listener of our own.

Two things have to happen for a key combination to reach Dikte. Somewhere has
to be told about it, and something has to be listening. Two desktops keep a
registry we can write into and read back, and something outside Dikte acts on
it: KDE has a file, GNOME has gsettings. There the /dev/input listener only
covers the wait until the registry is live.

Everywhere else there is nothing to write into, so Dikte holds the combination
itself for as long as it runs: macOS asks Carbon for it, and every other Linux
session (i3, XFCE, Cinnamon, sway, whatever the session calls itself) leans on
the /dev/input listener. That is not a fallback there, it is the mechanism, so
nothing about installing or removing a registry entry should be offered.
"""

import ast
import collections
import ctypes
import ctypes.util
import glob
import os
import pathlib
import re
import select
import shutil
import struct
import subprocess
import sys
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from .i18n import t

DESKTOP_ID = "dikte-toggle.desktop"
CANCEL_DESKTOP_ID = "dikte-cancel.desktop"
PAUSE_DESKTOP_ID = "dikte-pause.desktop"
MEETING_DESKTOP_ID = "dikte-meeting.desktop"
ASK_DESKTOP_ID = "dikte-ask.desktop"
APPLICATIONS_DIR = pathlib.Path.home() / ".local/share/applications"
DESKTOP_FILE = APPLICATIONS_DIR / DESKTOP_ID
SHORTCUTS_FILE = pathlib.Path.home() / ".config/kglobalshortcutsrc"
GNOME_MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_BINDING_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"

Shortcut = collections.namedtuple("Shortcut", "verb desktop_id name setting fallback")

# Every global shortcut in one place, because there are five of them and the
# command line, the settings window and the installer each used to carry their
# own copy of the list. `fallback` is what to register when the setting is
# empty: only the toggle has one, since it is the key the application is
# unusable without.
SHORTCUTS = {
    "toggle": Shortcut("toggle", DESKTOP_ID, "Dikte: start/stop recording",
                       "shortcut", "Ctrl+Space"),
    "pause": Shortcut("pause", PAUSE_DESKTOP_ID,
                      "Dikte: pause/resume the recording", "pause_shortcut", ""),
    "cancel": Shortcut("cancel", CANCEL_DESKTOP_ID, "Dikte: discard the recording",
                       "cancel_shortcut", ""),
    "ask": Shortcut("ask", ASK_DESKTOP_ID, "Dikte: ask Claude Code",
                    "assistant_shortcut", ""),
    "meeting": Shortcut("meeting", MEETING_DESKTOP_ID,
                        "Dikte: start/end a meeting recording",
                        "meeting_shortcut", ""),
}

# The fallbacks above are Linux's. macOS holds Ctrl+Space for the input-source
# switch, so asking for it there gets a combination that either loses to the
# system or fires while the keyboard layout changes underneath the dictation.
MACOS_FALLBACKS = {"toggle": "Ctrl+Option+Space"}

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


# What the running listener holds. Where there is no registry this is the whole
# of "installed", and it lasts as long as the process does: there is no file,
# and no other program to read one. Written by the listener that is in use,
# read by the status line, so what Settings shows is what is actually being
# listened for.
_REGISTERED = {}


# --- built-in listener ----------------------------------------------------

class EvdevHotkey(QObject):
    """Catches global shortcuts by reading /dev/input directly.

    It does not swallow the key; the focused application sees the combination
    too. On KDE that is the price of not waiting for the next login, and the
    registry takes over once it is live. On a desktop with no registry at all
    it is the only way the keys arrive, and the price is permanent.
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
        for name, shortcut in bindings.items():
            spec = SHORTCUTS.get(name)
            if spec and shortcut:
                _REGISTERED[spec.desktop_id] = shortcut
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(devices,), daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
        self._thread = None
        _REGISTERED.clear()

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


# --- macOS: Carbon's hotkey service ---------------------------------------

# Apple virtual key codes: where a key sits, not what is printed on it.
MAC_KEYS = {
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
# Carbon's own modifier bits, which are not the ones CoreGraphics uses in
# paste.py: the same four modifiers, numbered differently by two APIs.
MAC_MODS = {
    "cmd": 1 << 8, "command": 1 << 8, "meta": 1 << 8, "super": 1 << 8,
    "shift": 1 << 9,
    "alt": 1 << 11, "option": 1 << 11,
    "ctrl": 1 << 12, "control": 1 << 12,
}
HOTKEY_SIGNATURE = "Dikt"          # what our registrations are labelled with
KEYBOARD_EVENT_CLASS = "keyb"
HOTKEY_PRESSED = 5                 # kEventHotKeyPressed
PARAMETER_ANY = "----"             # kEventParamDirectObject / typeWildCard
HOTKEY_ID_PARAMETER = "hkid"


def parse_macos_shortcut(text):
    """'Cmd+Space' -> (256, 49), or (None, None) when unusable."""
    parts = [part.strip().lower() for part in str(text).split("+") if part.strip()]
    modifiers, key = 0, None
    for part in parts:
        if part in MAC_MODS:
            modifiers |= MAC_MODS[part]
        elif key is None and part in MAC_KEYS:
            key = MAC_KEYS[part]
        else:
            return None, None
    if key is None:
        return None, None
    return modifiers, key


def _fourcc(text):
    """A Carbon four-character code, which is those four bytes as a number."""
    return int.from_bytes(text.encode("ascii"), "big")


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class CarbonHotkey(QObject):
    """Catches global shortcuts through macOS's own hotkey service.

    RegisterEventHotKey asks for one combination rather than reading the
    keyboard, so it needs no permission at all. Accessibility is a separate
    matter, and only for the Cmd+V that puts the text back (see paste.py).

    Unlike the evdev listener this one does swallow the key: while Dikte holds
    a combination, nothing else on the Mac receives it.
    """

    triggered = pyqtSignal(str)   # the name the binding was registered under
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
        """`bindings` is {name: 'Cmd+Space'}; an empty combination is skipped."""
        self.stop()
        try:
            self._carbon = _carbon()
        except OSError as exc:
            self.failed.emit(t("Could not reach the macOS shortcut service: "
                               "{error}", error=exc))
            return False
        if not self._install_handler():
            return False

        for identifier, (name, shortcut) in enumerate(bindings.items(), 1):
            if not shortcut:
                continue
            modifiers, key = parse_macos_shortcut(shortcut)
            if key is None:
                self.failed.emit(
                    t("Could not parse the shortcut: {shortcut}", shortcut=shortcut)
                )
                continue
            reference = ctypes.c_void_p()
            code = self._carbon.RegisterEventHotKey(
                key, modifiers,
                _EventHotKeyID(_fourcc(HOTKEY_SIGNATURE), identifier),
                self._carbon.GetApplicationEventTarget(), 0, ctypes.byref(reference),
            )
            if code != 0:
                # This is the conflict warning on macOS: there is no list to
                # read beforehand, the answer comes from asking for the key.
                self.failed.emit(t(
                    "macOS would not give Dikte {shortcut}; another application "
                    "already holds it.", shortcut=shortcut))
                continue
            self._registrations.append(reference)
            self._names[identifier] = name
            spec = SHORTCUTS.get(name)
            if spec:
                _REGISTERED[spec.desktop_id] = shortcut
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
        _REGISTERED.clear()

    def _install_handler(self):
        carbon = self._carbon
        callback_type = ctypes.CFUNCTYPE(
            ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )

        def pressed(_next_handler, event, _user_data):
            wanted = _EventHotKeyID()
            size = ctypes.c_uint32()
            code = carbon.GetEventParameter(
                event, _fourcc(PARAMETER_ANY), _fourcc(HOTKEY_ID_PARAMETER), None,
                ctypes.sizeof(wanted), ctypes.byref(size), ctypes.byref(wanted),
            )
            if code == 0:
                name = self._names.get(wanted.id)
                if name:
                    self.triggered.emit(name)
            return 0

        # Kept on self: Carbon holds the address of this function, and nothing
        # on the Python side would otherwise stop it being collected.
        self._callback = callback_type(pressed)
        event_type = _EventTypeSpec(_fourcc(KEYBOARD_EVENT_CLASS), HOTKEY_PRESSED)
        code = carbon.InstallEventHandler(
            carbon.GetApplicationEventTarget(), self._callback, 1,
            ctypes.byref(event_type), None, ctypes.byref(self._handler),
        )
        if code != 0:
            self.failed.emit(t("Could not reach the macOS shortcut service: "
                               "{error}", error=code))
            self._callback = None
            return False
        return True


def _carbon():
    """Carbon, with its calls typed the way they are used above."""
    path = (ctypes.util.find_library("Carbon")
            or "/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon = ctypes.CDLL(path)
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    carbon.InstallEventHandler.restype = ctypes.c_int32
    carbon.InstallEventHandler.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    carbon.RegisterEventHotKey.restype = ctypes.c_int32
    carbon.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
    ]
    carbon.UnregisterEventHotKey.restype = ctypes.c_int32
    carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    carbon.RemoveEventHandler.restype = ctypes.c_int32
    carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]
    carbon.GetEventParameter.restype = ctypes.c_int32
    carbon.GetEventParameter.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
    ]
    return carbon


# --- Windows: RegisterHotKey ------------------------------------------------

# Windows virtual-key codes: where a key sits, not what a layout prints on it.
WIN_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{str(digit): 0x30 + digit for digit in range(10)},
    **{chr(ord("a") + i): 0x41 + i for i in range(26)},
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}
WIN_MODS = {
    "alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004,
    "meta": 0x0008, "super": 0x0008, "win": 0x0008,
}
WIN_MOD_NOREPEAT = 0x4000   # holding the combination fires it once
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def _win_input():
    """user32 and kernel32, which is all the listener talks to.

    Loaded on the first start rather than at import: this module is read on
    every system, and these two libraries exist on one of them.
    """
    return ctypes.windll.user32, ctypes.windll.kernel32


def parse_windows_shortcut(text):
    """'Ctrl+Space' -> (2, 32), or (None, None) when unusable."""
    parts = [part.strip().lower() for part in str(text).split("+") if part.strip()]
    modifiers, key = 0, None
    for part in parts:
        if part in WIN_MODS:
            modifiers |= WIN_MODS[part]
        elif key is None and part in WIN_KEYS:
            key = WIN_KEYS[part]
        else:
            return None, None
    if key is None:
        return None, None
    return modifiers, key


class WinHotkey(QObject):
    """Catches global shortcuts through Windows' own hotkey service.

    RegisterHotKey asks for one combination rather than reading the keyboard,
    so it needs no permission at all. Like Carbon's and unlike the evdev
    listener it swallows the key: while Dikte holds a combination, nothing
    else on the machine receives it.

    RegisterHotKey only fires on the thread that called it, so registration
    and the message loop live together on one worker thread; start() hands the
    bindings over and waits for it to report what Windows actually gave us.
    """

    triggered = pyqtSignal(str)   # the name the binding was registered under
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._user32 = None
        self._kernel32 = None
        self._thread = None
        self._thread_id = None
        self._count = 0

    @property
    def running(self):
        return self._count > 0 and self._thread is not None and self._thread.is_alive()

    def start(self, bindings):
        """`bindings` is {name: 'Ctrl+Space'}; an empty combination is skipped."""
        self.stop()
        try:
            self._user32, self._kernel32 = _win_input()
        except (AttributeError, OSError) as exc:
            self.failed.emit(t("Could not reach the Windows shortcut service: "
                               "{error}", error=exc))
            return False
        wanted = []
        for identifier, (name, shortcut) in enumerate(bindings.items(), 1):
            if not shortcut:
                continue
            modifiers, key = parse_windows_shortcut(shortcut)
            if key is None:
                self.failed.emit(
                    t("Could not parse the shortcut: {shortcut}", shortcut=shortcut)
                )
                continue
            wanted.append((identifier, name, shortcut, modifiers, key))
        if not wanted:
            return False

        ready = threading.Event()
        outcome = {"count": 0, "thread_id": None}
        self._thread = threading.Thread(
            target=self._loop, args=(wanted, ready, outcome), daemon=True
        )
        self._thread.start()
        ready.wait(timeout=5)
        self._thread_id = outcome["thread_id"]
        self._count = outcome["count"]
        if not self._count:
            self._thread = None
        return self._count > 0

    def stop(self):
        if self._thread and self._thread_id and self._user32:
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = None
        self._count = 0
        _REGISTERED.clear()

    def _loop(self, wanted, ready, outcome):
        import ctypes.wintypes
        user32, kernel32 = self._user32, self._kernel32
        outcome["thread_id"] = kernel32.GetCurrentThreadId()

        # The message queue a PostThreadMessage needs only exists once the
        # thread has asked for messages; peek once before reporting ready.
        message = ctypes.wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, WM_QUIT, WM_QUIT, 0)

        names = {}
        for identifier, name, shortcut, modifiers, key in wanted:
            if user32.RegisterHotKey(None, identifier,
                                     modifiers | WIN_MOD_NOREPEAT, key):
                names[identifier] = name
                spec = SHORTCUTS.get(name)
                if spec:
                    _REGISTERED[spec.desktop_id] = shortcut
            else:
                # This is the conflict warning on Windows: there is no list to
                # read beforehand, the answer comes from asking for the key.
                self.failed.emit(t(
                    "Windows would not give Dikte {shortcut}; another "
                    "application already holds it.", shortcut=shortcut))
        outcome["count"] = len(names)
        ready.set()
        if not names:
            return

        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    name = names.get(int(message.wParam))
                    if name:
                        self.triggered.emit(name)
        finally:
            for identifier in names:
                user32.UnregisterHotKey(None, identifier)


# --- the desktop's own shortcut -------------------------------------------

# The five ways a combination can reach Dikte. Everything below asks backend()
# rather than looking at the session itself, so the name shown, the status read
# back, what Install writes and what the installer promises cannot disagree
# about which one this session got.
KDE = "kde"
GNOME = "gnome"
MACOS = "macos"
WINDOWS = "windows"
LISTENER = "listener"


def _macos():
    return sys.platform == "darwin"


def _windows():
    return sys.platform == "win32"


def backend():
    """Which shortcut mechanism this session has.

    A desktop only counts when the program that writes its registry is
    installed too: a GNOME session without gsettings, or a Plasma one without
    kwriteconfig6, has nothing we can register into. Anything unrecognised is
    the listener's, which is every other Linux desktop and needs nothing from
    the session at all.
    """
    if _macos():
        return MACOS
    if _windows():
        return WINDOWS
    names = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    names = [name.strip() for name in names if name.strip()]
    if any("gnome" in name for name in names) and shutil.which("gsettings"):
        return GNOME
    if (any("kde" in name or "plasma" in name for name in names)
            and shutil.which("kwriteconfig6")):
        return KDE
    return LISTENER


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


def listener(parent=None):
    """The thing that hears the key, for whichever system this is."""
    if _macos():
        return CarbonHotkey(parent)
    if _windows():
        return WinHotkey(parent)
    return EvdevHotkey(parent)


def default_combo(which):
    """What to register for `which` when the setting has been cleared.

    The one place the platforms disagree about a default, so that the command
    line and the settings window cannot drift apart on it.
    """
    if _macos():
        return MACOS_FALLBACKS.get(which, "")
    spec = SHORTCUTS.get(which)
    return spec.fallback if spec else ""


def valid_shortcut(text):
    """Whether this machine can bind the combination as it was typed."""
    if _macos():
        return parse_macos_shortcut(text)[1] is not None
    if _windows():
        return parse_windows_shortcut(text)[1] is not None
    return parse_shortcut(text)[1] is not None


def installs_shortcuts():
    """Whether this system keeps a shortcut registry to write into.

    KDE and GNOME do, and something outside Dikte reads it, so the combination
    survives Dikte being closed. macOS, Windows and the plain listener do not:
    there is nothing to install, nothing to remove, and Settings should not
    offer either.
    """
    return backend() in (KDE, GNOME)


def shortcut_needs_restart():
    """Whether an installed shortcut waits for the next login before it works.

    KWin reads kglobalshortcutsrc once, when it starts. GNOME picks a binding
    up as it is written, and the others never had one to write.
    """
    return backend() == KDE


def install_shortcut(shortcut, exec_command, name="Dikte: start/stop recording",
                     desktop_id=DESKTOP_ID):
    which = backend()
    if which == GNOME:
        return install_gnome_shortcut(shortcut, exec_command, name, desktop_id)
    if which == KDE:
        return install_kde_shortcut(shortcut, exec_command, name, desktop_id)
    _REGISTERED[desktop_id] = shortcut
    if which in (MACOS, WINDOWS):
        return True, t(
            "Shortcut saved: {shortcut}\nDikte holds this one itself while it "
            "is running, so it works as soon as the settings are saved.",
            shortcut=shortcut,
        )
    return True, t(
        "Shortcut saved: {shortcut}\n{desktop} has no shortcut registry to "
        "install into, so Dikte listens for this one itself while it is "
        "running. It works as soon as the settings are saved.",
        shortcut=shortcut, desktop=desktop_name(),
    )


def remove_shortcut(desktop_id=DESKTOP_ID):
    which = backend()
    if which == GNOME:
        remove_gnome_shortcut(desktop_id)
    elif which == KDE:
        remove_kde_shortcut(desktop_id)
    else:
        _REGISTERED.pop(desktop_id, None)


def shortcut_status(desktop_id=DESKTOP_ID):
    which = backend()
    if which == GNOME:
        return gnome_shortcut_status(desktop_id)
    if which == KDE:
        return kde_shortcut_status(desktop_id)
    return _REGISTERED.get(desktop_id)


def desktop_name():
    """What to call this session in the interface.

    The listener's desktops get the name the session gave itself, so an i3 user
    is told about i3 rather than about a KDE that is not running.
    """
    which = backend()
    if which == MACOS:
        return "macOS"
    if which == WINDOWS:
        return "Windows"
    if which == GNOME:
        return "GNOME"
    if which == KDE:
        return "KDE"
    name = os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")[0].strip()
    return name or "This desktop"


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
    if backend() != KDE:
        # Nowhere else has a list to read. macOS and Windows answer the question
        # by refusing the registration, which their listeners report when they
        # ask for the key; the other two would only be reading a file their
        # session never looks at, and a leftover one from a Plasma install the
        # user has since left would refuse perfectly good combinations.
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
        if any(shortcut.lower() == part.strip().lower()
               for part in re.split(r"[,\t]", value)):
            hits.append(f"{section} → {key}")
    return hits
