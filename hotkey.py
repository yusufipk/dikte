"""Global shortcuts.

On Linux that is a KDE custom shortcut, with a built-in evdev listener to cover
the gap until the next login. On Windows it is RegisterHotKey, which needs no
installation step at all. `create_hotkey_listener` hands back whichever of the
two belongs to the machine it is running on.
"""

import glob
import os
import pathlib
import re
import shutil
import struct
import subprocess
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from i18n import t
from platform_utils import IS_WINDOWS

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
else:
    import select

DESKTOP_ID = "dikte-toggle.desktop"
MEETING_DESKTOP_ID = "dikte-meeting.desktop"
ASK_DESKTOP_ID = "dikte-ask.desktop"
APPLICATIONS_DIR = pathlib.Path.home() / ".local/share/applications"
DESKTOP_FILE = APPLICATIONS_DIR / DESKTOP_ID
SHORTCUTS_FILE = pathlib.Path.home() / ".config/kglobalshortcutsrc"

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


# --- KDE custom shortcut --------------------------------------------------

def install_kde_shortcut(shortcut, exec_command, name="Dikte: start/stop recording",
                         desktop_id=DESKTOP_ID):
    """Write the desktop file and the kglobalshortcutsrc entry.

    KWin only reads that file at startup, so the entry goes live after the next
    login. Returns (True, message) or (False, error).
    """
    if IS_WINDOWS:
        return False, t("Not available on Windows")

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

    kwriteconfig = shutil.which("kwriteconfig6")
    if not kwriteconfig:
        return False, t("kwriteconfig6 not found, so the shortcut could not be "
                        "registered with KDE.")
    try:
        subprocess.run(
            [kwriteconfig, "--notify", "--file", "kglobalshortcutsrc",
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
    if IS_WINDOWS:
        return

    try:
        (APPLICATIONS_DIR / desktop_id).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        kwriteconfig = shutil.which("kwriteconfig6")
        if not kwriteconfig:
            return
        subprocess.run(
            [kwriteconfig, "--notify", "--file", "kglobalshortcutsrc",
             "--group", "services", "--group", desktop_id, "--key", "_launch", "--delete"],
            capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def kde_shortcut_status(desktop_id=DESKTOP_ID):
    """The registered shortcut, or None."""
    if IS_WINDOWS:
        return None

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
    if IS_WINDOWS:
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


# --- Windows listener -----------------------------------------------------
#
# RegisterHotKey is the system's own shortcut table, so unlike the evdev
# listener it does swallow the key: the focused window never sees it. That is
# the behaviour Windows users expect of a global shortcut, and it is also why a
# combination already taken by another program cannot be had at all — the
# registration simply fails, and is reported rather than passed over in silence.

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312

VK_CODES = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pgdown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27,
}
VK_CODES.update({str(n): 0x30 + n for n in range(10)})
VK_CODES.update({chr(ord("a") + n): 0x41 + n for n in range(26)})
VK_CODES.update({f"f{n}": 0x6F + n for n in range(1, 13)})

WIN_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "meta": MOD_WIN, "super": MOD_WIN, "win": MOD_WIN,
}


def parse_win_shortcut(text):
    """'Ctrl+Space' -> (MOD_CONTROL, 0x20), or (None, None) when unparsable."""
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    if not parts:
        return None, None
    modifiers, key = 0, None
    for part in parts:
        if part in WIN_MODS:
            modifiers |= WIN_MODS[part]
        else:
            key = VK_CODES.get(part)
            if key is None:
                return None, None
    if key is None:
        return None, None
    # Without this a held-down combination repeats at the keyboard's own rate,
    # which for a start/stop toggle means the recording flickering on and off.
    return modifiers | MOD_NOREPEAT, key


class WindowsHotkey(QObject):
    """Catches global shortcuts through RegisterHotKey on a thread of its own."""

    triggered = pyqtSignal(str)   # the name the binding was registered under
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._thread_id = 0
        self._stop = threading.Event()
        self._bindings = {}
        # The thread reports back once it has either registered its shortcuts
        # or given up, so start() can answer truthfully instead of guessing.
        self._ready = threading.Event()
        self._registered_any = False

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, bindings):
        """`bindings` is {name: 'Ctrl+Space'}; an empty combination is skipped."""
        self.stop()
        if not any(bindings.values()):
            return False

        self._bindings = dict(bindings)
        self._stop.clear()
        self._ready.clear()
        self._registered_any = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # A shortcut another program already owns cannot be registered, and the
        # caller needs that answer now, not after the first key press fails to
        # do anything.
        self._ready.wait(timeout=2)
        return self._registered_any

    def stop(self):
        self._stop.set()
        thread, thread_id = self._thread, self._thread_id
        if thread:
            if thread_id:
                # The loop is parked in GetMessageW; only a message wakes it.
                ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = 0

    def _register(self, user32):
        """{hotkey id: name} for every binding the system accepted."""
        registered = {}
        next_id = 1
        for name, shortcut in self._bindings.items():
            if not shortcut:
                continue
            modifiers, key = parse_win_shortcut(shortcut)
            if key is None:
                self.failed.emit(
                    t("Could not parse the shortcut: {shortcut}", shortcut=shortcut)
                )
                continue
            if user32.RegisterHotKey(None, next_id, modifiers, key):
                registered[next_id] = name
                next_id += 1
            else:
                self.failed.emit(t(
                    "{shortcut} is already taken by another program, so Dikte "
                    "cannot use it. Pick a different combination.",
                    shortcut=shortcut,
                ))
        return registered

    def _loop(self):
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        # RegisterHotKey binds to the calling thread, and WM_HOTKEY is posted to
        # that same thread, so registration has to happen here rather than in
        # start().
        registered = self._register(user32)
        self._registered_any = bool(registered)
        self._ready.set()
        if not registered:
            self._thread_id = 0
            return

        try:
            msg = wintypes.MSG()
            while not self._stop.is_set():
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):   # WM_QUIT, or the queue broke
                    break
                if msg.message == WM_HOTKEY:
                    name = registered.get(msg.wParam)
                    if name:
                        self.triggered.emit(name)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)


def create_hotkey_listener(parent=None):
    return WindowsHotkey(parent) if IS_WINDOWS else EvdevHotkey(parent)
