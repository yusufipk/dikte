"""GNOME/KDE global-shortcut installation, an evdev listener, and Carbon.

Three desktops, and the shape of the problem is different on each. KDE and
GNOME are asked to run a command when a combination is pressed, which is a file
written now and read by the desktop later; the evdev listener is what covers
the gap until KWin has been restarted. macOS has neither the gap nor the file:
Carbon binds a combination to the running application and it is live at once,
so there `install_shortcut` is a promise that the application will register it
and `shortcut_status` has nothing on disk to report.
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

from i18n import t

DESKTOP_ID = "dikte-toggle.desktop"
CANCEL_DESKTOP_ID = "dikte-cancel.desktop"
MEETING_DESKTOP_ID = "dikte-meeting.desktop"
ASK_DESKTOP_ID = "dikte-ask.desktop"
APPLICATIONS_DIR = pathlib.Path.home() / ".local/share/applications"
DESKTOP_FILE = APPLICATIONS_DIR / DESKTOP_ID
SHORTCUTS_FILE = pathlib.Path.home() / ".config/kglobalshortcutsrc"
GNOME_MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_BINDING_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"

Shortcut = collections.namedtuple("Shortcut", "verb desktop_id name setting fallback")

# Every global shortcut in one place, because there are four of them and the
# command line, the settings window and the installer each used to carry their
# own copy of the list. `fallback` is what to register when the setting is
# empty: only the toggle has one, since it is the key the application is
# unusable without.
SHORTCUTS = {
    # Ctrl+Space is the input source switcher on macOS: registered here it
    # would be accepted and then never pressed, so the fallback there is the
    # same combination one modifier along, matching config.DEFAULTS.
    "toggle": Shortcut("toggle", DESKTOP_ID, "Dikte: start/stop recording",
                       "shortcut",
                       "Ctrl+Alt+Space" if sys.platform == "darwin"
                       else "Ctrl+Space"),
    "cancel": Shortcut("cancel", CANCEL_DESKTOP_ID, "Dikte: discard the recording",
                       "cancel_shortcut", ""),
    "ask": Shortcut("ask", ASK_DESKTOP_ID, "Dikte: ask Claude Code",
                    "assistant_shortcut", ""),
    "meeting": Shortcut("meeting", MEETING_DESKTOP_ID,
                        "Dikte: start/end a meeting recording",
                        "meeting_shortcut", ""),
}

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
    "alt": (56, 100), "option": (56, 100),
    # One key, two names for it: macOS calls it command, and a settings file
    # carried over from a Mac says so.
    "meta": (125, 126), "super": (125, 126),
    "cmd": (125, 126), "command": (125, 126),
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
            mods.add("ctrl" if part == "control"
                     else "alt" if part == "option"
                     else "super" if part in ("meta", "cmd", "command") else part)
        else:
            key = KEYS.get(part)
            if key is None:
                return None, None
    if key is None:
        return None, None
    return mods, key


def understands(shortcut):
    """Whether this platform can bind that combination at all.

    Asked before a shortcut is installed, and the two platforms count different
    keys: there is no F13 on the evdev table below and no Insert on a Mac.
    """
    parse = parse_mac_shortcut if sys.platform == "darwin" else parse_shortcut
    return parse(shortcut) != (None, None)


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


def _macos():
    return sys.platform == "darwin"


def install_shortcut(shortcut, exec_command, name="Dikte: start/stop recording",
                     desktop_id=DESKTOP_ID):
    if _macos():
        return install_macos_shortcut(shortcut)
    if _gnome():
        return install_gnome_shortcut(shortcut, exec_command, name, desktop_id)
    return install_kde_shortcut(shortcut, exec_command, name, desktop_id)


def remove_shortcut(desktop_id=DESKTOP_ID):
    if _macos():
        return          # nothing on disk to take away
    if _gnome():
        remove_gnome_shortcut(desktop_id)
    else:
        remove_kde_shortcut(desktop_id)


def shortcut_status(desktop_id=DESKTOP_ID):
    """The combination the desktop has on file, or None.

    None on macOS, always: Carbon holds a registration for as long as the
    application runs and writes nothing down, so the setting is not a copy of
    the binding, it is the binding. Whoever shows this to a person says so
    there rather than here.
    """
    if _macos():
        return None
    return (gnome_shortcut_status(desktop_id) if _gnome()
            else kde_shortcut_status(desktop_id))


def desktop_name():
    if _macos():
        return "macOS"
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
    if _macos():
        # macOS keeps its own shortcuts in a binary property list of symbolic
        # hot key numbers, with no names in it and no promise about the format.
        # A clash there shows up as RegisterEventHotKey refusing the key, which
        # is reported when it happens rather than guessed at beforehand.
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


# --- macOS ----------------------------------------------------------------
#
# Carbon's RegisterEventHotKey is the one way on a Mac to claim a combination
# system-wide and have the key not reach the focused window as well. It is
# reached through ctypes rather than through PyObjC because the whole of Dikte
# is the standard library and PyQt6, and this is six functions and two structs.

# Virtual key codes (HIToolbox/Events.h). The same list of key names as the
# evdev table above, minus the ones a Mac keyboard has no key for.
MAC_KEYS = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06,
    "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E,
    "r": 0x0F, "y": 0x10, "t": 0x11, "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
    "9": 0x19, "7": 0x1A, "8": 0x1C, "0": 0x1D,
    "enter": 0x24, "return": 0x24, "tab": 0x30, "space": 0x31,
    "backspace": 0x33, "esc": 0x35, "escape": 0x35,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
    "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    # The function keys past twelve are worth having here: nothing on macOS
    # claims them, which on a desktop this full is rare.
    "f13": 0x69, "f14": 0x6B, "f15": 0x71, "f16": 0x6A, "f17": 0x40,
    "f18": 0x4F, "f19": 0x50,
    "insert": 0x72, "home": 0x73, "pgup": 0x74, "delete": 0x75, "end": 0x77,
    "pgdown": 0x79, "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
}

# Carbon's modifier bits, which are not the ones Cocoa uses.
MAC_MODS = {
    "cmd": 0x0100, "command": 0x0100, "meta": 0x0100, "super": 0x0100,
    "shift": 0x0200,
    "alt": 0x0800, "option": 0x0800,
    "ctrl": 0x1000, "control": 0x1000,
}


def parse_mac_shortcut(text):
    """'Ctrl+Alt+Space' -> (0x1800, 0x31), or (None, None) when unparsable."""
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    if not parts:
        return None, None
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


def install_macos_shortcut(shortcut):
    """There is nothing to write; say whether the combination can be bound.

    The registration itself happens in CarbonHotkey when the application picks
    the setting up, which the command line asks it to do right after this
    returns.
    """
    if parse_mac_shortcut(shortcut) == (None, None):
        return False, t("Could not parse the shortcut: {shortcut}", shortcut=shortcut)
    return True, t("Shortcut saved: {shortcut}", shortcut=shortcut)


def _four(text):
    """'keyb' -> 0x6B657962, which is how Carbon spells its type codes."""
    return int.from_bytes(text.encode("ascii"), "big")


K_EVENT_CLASS_KEYBOARD = _four("keyb")
K_EVENT_HOTKEY_PRESSED = 5
K_EVENT_PARAM_DIRECT_OBJECT = _four("----")
TYPE_EVENT_HOTKEY_ID = _four("hkid")
DIKTE_SIGNATURE = _four("dikt")

_OSStatus = ctypes.c_int32
_OSType = ctypes.c_uint32


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", _OSType), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", _OSType), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(_OSStatus, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_void_p)
_carbon = None


def _load_carbon():
    """The Carbon framework with its argument types declared, loaded once.

    Declaring them matters more than it looks: a pointer returned as a C int is
    truncated on a 64-bit build, and the event target would arrive at
    RegisterEventHotKey as a different address than the one it was given.
    """
    global _carbon
    if _carbon is not None:
        return _carbon
    path = ctypes.util.find_library("Carbon")
    if not path:
        raise OSError("Carbon framework not found")
    lib = ctypes.CDLL(path)
    lib.GetApplicationEventTarget.restype = ctypes.c_void_p
    lib.GetApplicationEventTarget.argtypes = []
    lib.InstallEventHandler.restype = _OSStatus
    lib.InstallEventHandler.argtypes = [
        ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
        ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.RemoveEventHandler.restype = _OSStatus
    lib.RemoveEventHandler.argtypes = [ctypes.c_void_p]
    lib.RegisterEventHotKey.restype = _OSStatus
    lib.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.UnregisterEventHotKey.restype = _OSStatus
    lib.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    lib.GetEventParameter.restype = _OSStatus
    lib.GetEventParameter.argtypes = [
        ctypes.c_void_p, _OSType, _OSType, ctypes.POINTER(_OSType),
        ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
    ]
    _carbon = lib
    return _carbon


class CarbonHotkey(QObject):
    """Global shortcuts on macOS, through the API every Mac application uses.

    The same signals and the same three methods as EvdevHotkey, so the
    application binds one or the other and knows no difference. What is not the
    same is that this is not a fallback: it swallows the key rather than
    letting the focused window see it too, it asks for no permission and no
    group membership, and it is live the moment it is registered instead of
    after the next login.

    Carbon delivers the press on the main run loop, which under Qt on macOS is
    the one the event loop is already turning, so there is no thread here and
    `running` means "some combination is registered" rather than "a thread is
    alive".
    """

    triggered = pyqtSignal(str)   # the name the binding was registered under
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._handler = None       # the installed handler, while one is
        self._handler_fn = None    # the callback itself: Python's only
                                   # reference to it, and Carbon holds no other
        self._hotkeys = []         # [(ref, name)]
        self._names = {}           # hot key id -> the name it was given

    @property
    def running(self):
        return bool(self._hotkeys)

    def start(self, bindings):
        """`bindings` is {name: 'Ctrl+Alt+Space'}; an empty combination is skipped."""
        self.stop()
        try:
            carbon = _load_carbon()
        except OSError as exc:
            self.failed.emit(t("Could not reach the macOS shortcut API: {error}",
                               error=exc))
            return False

        wanted = {}
        for name, shortcut in bindings.items():
            if not shortcut:
                continue
            modifiers, key = parse_mac_shortcut(shortcut)
            if key is None:
                self.failed.emit(
                    t("Could not parse the shortcut: {shortcut}", shortcut=shortcut)
                )
                continue
            wanted[name] = (modifiers, key, shortcut)
        if not wanted:
            return False

        if not self._install_handler(carbon):
            return False

        for index, (name, (modifiers, key, shortcut)) in enumerate(wanted.items(), 1):
            ref = ctypes.c_void_p()
            status = carbon.RegisterEventHotKey(
                key, modifiers, _EventHotKeyID(DIKTE_SIGNATURE, index),
                carbon.GetApplicationEventTarget(), 0, ctypes.byref(ref),
            )
            if status != 0:
                # -9878, eventHotKeyExistsErr, and in practice it means one of
                # these four is bound to the combination twice: macOS hands out
                # a key its own shortcuts already use without complaining, and
                # the clash only shows up as a press that never arrives,
                # because a system shortcut sees the key first and stops there.
                self.failed.emit(t(
                    "{shortcut} could not be registered; another Dikte shortcut "
                    "is already on it. Pick another one in Settings → Shortcuts.",
                    shortcut=shortcut,
                ))
                continue
            self._names[index] = name
            self._hotkeys.append((ref, name))

        if not self._hotkeys:
            self._remove_handler()
        return bool(self._hotkeys)

    def stop(self):
        carbon = _carbon
        if carbon is not None:
            for ref, _name in self._hotkeys:
                carbon.UnregisterEventHotKey(ref)
        self._hotkeys = []
        self._names = {}
        self._remove_handler()

    def _install_handler(self, carbon):
        if self._handler is not None:
            return True
        # Held on the instance, not passed to Carbon to keep: a callback
        # collected while the handler is still installed is a crash the next
        # time the key is pressed.
        self._handler_fn = _HANDLER(self._on_event)
        spec = _EventTypeSpec(K_EVENT_CLASS_KEYBOARD, K_EVENT_HOTKEY_PRESSED)
        ref = ctypes.c_void_p()
        status = carbon.InstallEventHandler(
            carbon.GetApplicationEventTarget(), self._handler_fn, 1,
            ctypes.byref(spec), None, ctypes.byref(ref),
        )
        if status != 0:
            self._handler_fn = None
            self.failed.emit(t("Could not listen for shortcuts: error {code}",
                               code=status))
            return False
        self._handler = ref
        return True

    def _remove_handler(self):
        if self._handler is not None and _carbon is not None:
            _carbon.RemoveEventHandler(self._handler)
        self._handler = None
        self._handler_fn = None

    def _on_event(self, _call_ref, event, _user_data):
        """Called by Carbon on the main run loop, once per press."""
        try:
            got = _EventHotKeyID()
            _carbon.GetEventParameter(
                event, K_EVENT_PARAM_DIRECT_OBJECT, TYPE_EVENT_HOTKEY_ID,
                None, ctypes.sizeof(got), None, ctypes.byref(got),
            )
            name = self._names.get(got.id)
            if name:
                self.triggered.emit(name)
        except Exception:   # noqa: BLE001 - an exception thrown across the
            pass            # ctypes boundary would land inside Carbon
        return 0            # noErr: handled, and nobody else needs to see it
