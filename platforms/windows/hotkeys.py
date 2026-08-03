"""Global shortcuts on Windows: RegisterHotKey and the message that follows.

Nothing is written to a file for a desktop to read later. A Windows program
asks the system for a combination while it runs, gets it or does not, and hands
it back on the way out. So Dikte's shortcuts are live exactly as long as Dikte
is, and the settings are the only record of what they should be.

The registration belongs to the thread that made it, and WM_HOTKEY is posted to
that thread's own message queue, so all of it happens on a thread of ours with
a message loop on it. Qt never sees the message; what crosses back is a signal.

Two things RegisterHotKey does that the Linux listener does not. It takes the
key away from whoever had focus, so Ctrl+Space no longer reaches the editor
underneath. And it refuses a combination somebody else already holds, which is
the only conflict report Windows offers: it will not say who.
"""

import ctypes
import json
import threading
from ctypes import wintypes

from PyQt6.QtCore import QObject, pyqtSignal

from i18n import t
from platforms.common.shortcuts import (  # noqa: F401
    ASK_DESKTOP_ID,
    CANCEL_DESKTOP_ID,
    DESKTOP_ID,
    MEETING_DESKTOP_ID,
)
from platforms.windows import runtime

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Described rather than guessed at: ctypes assumes a C int for anything it was
# not told about, and a message pointer cut to 32 bits is a crash rather than a
# wrong answer. GetMessageW is the one that also answers -1, which is why its
# result is checked for that and not only for zero.
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT,
                                  wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                               wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      ctypes.c_void_p, ctypes.c_void_p]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Holding the key down repeats it at the keyboard's own rate, which for a
# start/stop toggle means starting and stopping several times a second.
MOD_NOREPEAT = 0x4000

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_USER = 0x0400
PM_NOREMOVE = 0x0000

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# The name Dikte's own bookkeeping goes under. RegisterHotKey has nothing to
# read back, so what was installed is remembered here for the command line,
# which asks about it from a process that is not the one holding the key.
REGISTRY_NAME = "shortcuts.json"

MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "meta": MOD_WIN, "super": MOD_WIN,
}

# Virtual-key codes. The names are the ones the settings store, so a
# combination typed on Linux means the same key here.
KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pgdown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}
KEYS.update({letter: 0x41 + index
             for index, letter in enumerate("abcdefghijklmnopqrstuvwxyz")})


def parse_shortcut(text):
    """'Ctrl+Space' -> ({'ctrl'}, 0x20), or (None, None) when unparsable."""
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    if not parts:
        return None, None
    mods, key = set(), None
    for part in parts:
        if part in MODS:
            mods.add("ctrl" if part == "control" else "super" if part == "meta"
                     else part)
        else:
            key = KEYS.get(part)
            if key is None:
                return None, None
    if key is None:
        return None, None
    return mods, key


def _modifier_flags(mods):
    flags = 0
    for name in mods:
        flags |= MODS.get(name, 0)
    return flags


# --- the listener ---------------------------------------------------------


class WindowsHotkeys(QObject):
    """Holds the combinations for as long as it runs, on a thread of its own.

    RegisterHotKey binds to the calling thread and WM_HOTKEY comes back to the
    same one, so the registration, the message loop and the unregistration all
    happen on the thread this starts; what leaves it is a Qt signal, which
    crosses back to the main thread the way any queued connection does.
    """

    triggered = pyqtSignal(str)   # the name the binding was registered under
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._thread_id = 0
        self._live = {}          # name -> combination, as registered

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def registered(self):
        """{name: combination} for what the system is actually delivering."""
        return dict(self._live)

    def start(self, bindings):
        """`bindings` is {name: 'Ctrl+Space'}; an empty combination is skipped."""
        self.stop()
        wanted = []
        for name, shortcut in bindings.items():
            if not shortcut:
                continue
            mods, key = parse_shortcut(shortcut)
            if key is None:
                self.failed.emit(
                    t("Could not parse the shortcut: {shortcut}", shortcut=shortcut)
                )
                continue
            wanted.append((name, shortcut, _modifier_flags(mods), key))
        if not wanted:
            return False

        ready = threading.Event()
        self._live = {}
        self._thread = threading.Thread(
            target=self._loop, args=(wanted, ready), daemon=True)
        self._thread.start()
        # The registration happens on that thread; waiting for it here is what
        # lets start() answer whether the keys are live, the way the caller
        # expects, rather than "a thread was started".
        ready.wait(timeout=5)
        return bool(self._live)

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0
        self._live = {}

    def _loop(self, wanted, ready):
        message = wintypes.MSG()
        # A thread has no message queue until something asks for one, and
        # PostThreadMessage to a thread without one is dropped. This makes it.
        user32.PeekMessageW(ctypes.byref(message), None, WM_USER, WM_USER,
                            PM_NOREMOVE)
        self._thread_id = kernel32.GetCurrentThreadId()

        ids = {}
        for index, (name, shortcut, flags, key) in enumerate(wanted, start=1):
            ctypes.set_last_error(0)
            if user32.RegisterHotKey(None, index, flags | MOD_NOREPEAT, key):
                ids[index] = name
                self._live[name] = shortcut
                continue
            error = ctypes.get_last_error()
            self.failed.emit(
                t("{shortcut} is already taken by another application.",
                  shortcut=shortcut)
                if error == ERROR_HOTKEY_ALREADY_REGISTERED else
                t("Windows refused the shortcut {shortcut} (error {code}).",
                  shortcut=shortcut, code=error)
            )
        # Set last, so that start() coming back means every combination has
        # been asked for and every refusal already said out loud.
        ready.set()

        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if got in (0, -1):        # WM_QUIT, or the queue went wrong
                    break
                if message.message == WM_HOTKEY:
                    name = ids.get(int(message.wParam))
                    if name:
                        self.triggered.emit(name)
        finally:
            for index in ids:
                user32.UnregisterHotKey(None, index)


Listener = WindowsHotkeys


# --- what was installed ---------------------------------------------------


def _registry_path():
    return runtime.data_dir() / REGISTRY_NAME


def _stored():
    try:
        found = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _store(rows):
    path = _registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        pass


def _free(flags, key):
    """Whether Windows would hand that combination over right now.

    Asked by registering it and giving it straight back, which is the only way
    to find out: there is nothing to read that lists what is taken.
    """
    ctypes.set_last_error(0)
    if not user32.RegisterHotKey(None, 0xBFFF, flags | MOD_NOREPEAT, key):
        return ctypes.get_last_error() != ERROR_HOTKEY_ALREADY_REGISTERED
    user32.UnregisterHotKey(None, 0xBFFF)
    return True


def conflicting_shortcuts(shortcut, desktop_id=DESKTOP_ID):
    """Whether something else holds that combination.

    Windows will not say what, only that the answer is no, so the one line
    this can return says exactly that rather than inventing a name.
    """
    mods, key = parse_shortcut(shortcut)
    if key is None:
        return []
    # Dikte's own registration would otherwise look like somebody else's.
    if _stored().get(desktop_id) == shortcut:
        return []
    if _free(_modifier_flags(mods), key):
        return []
    return [t("another application (Windows does not say which)")]


def install_shortcut(shortcut, exec_command="", name="", desktop_id=DESKTOP_ID):
    """Take the combination, and remember that Dikte has it.

    `exec_command` is what Linux writes into a .desktop file for the session to
    run. Nothing launches Dikte here: the running instance holds the key
    itself, so the argument is accepted and ignored.
    """
    mods, key = parse_shortcut(shortcut)
    if key is None:
        return False, t("Could not parse the shortcut: {shortcut}",
                        shortcut=shortcut)
    if (_stored().get(desktop_id) != shortcut
            and not _free(_modifier_flags(mods), key)):
        return False, t(
            "{shortcut} is already taken by another application. Windows only "
            "gives a combination to one program at a time; pick another one.",
            shortcut=shortcut,
        )
    rows = _stored()
    rows[desktop_id] = shortcut
    _store(rows)
    return True, t("Shortcut saved: {shortcut}", shortcut=shortcut)


def remove_shortcut(desktop_id=DESKTOP_ID):
    rows = _stored()
    if rows.pop(desktop_id, None) is not None:
        _store(rows)


def shortcut_status(desktop_id=DESKTOP_ID):
    """The combination Dikte holds for that verb, or None."""
    return _stored().get(desktop_id) or None


def desktop_name():
    return "Windows"
