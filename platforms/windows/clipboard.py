"""The Win32 clipboard, and a key combination sent with SendInput.

Two things about the Windows clipboard shape this file. It is opened rather
than written to: one process at a time owns it, so anything else that reaches
for it at the same moment, and a great many programs watch it, makes the open
fail, which is a wait and another go rather than an error. And what is on it is
a set of formats rather than a string, so putting back what was there before
means putting back all of them, not only the text.

The key press goes through SendInput, which is what Windows offers a program
that wants to type into somebody else's window. Two limits come with it. The
combination has to be released in the reverse of the order it was pressed, or
the target sees a modifier that never came back up. And a program running at a
lower integrity level cannot send input to a higher one, so a paste into an
application started as administrator is refused by Windows itself; the text is
still on the clipboard, and saying so is better than looking like a hang.
"""

import ctypes
import time
from ctypes import wintypes

from i18n import t

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Every one of these hands back or takes a handle, and a handle is 64 bits
# here. ctypes assumes a C int for a function nobody described to it, so an
# undeclared GetClipboardData would quietly cut the top half off the handle it
# returns and the clipboard would appear to be empty on any machine unlucky
# enough to get a high address. Declared, all of them.
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL

CF_TEXT = 1
CF_UNICODETEXT = 13

GMEM_MOVEABLE = 0x0002

# Only the ones worth putting back. A snapshot of every format on the clipboard
# would include the ones a program registered for its own use, several of which
# are handles into that program rather than bytes.
EXTRA_FORMATS = ("HTML Format", "Rich Text Format")

# What a saved clipboard looks like when it held more than plain text. The
# contract with the rest of Dikte is bytes in and bytes out, so the formats are
# packed into one buffer behind a marker; anything without it is text, which is
# also what a snapshot taken on Linux looks like.
SNAPSHOT_MAGIC = b"\x00dikte-clipboard\x00"

ERROR_ACCESS_DENIED = 5

# Windows is asked for the clipboard for about a second in total. A program
# holding it for longer than that is not going to let go because we waited.
OPEN_ATTEMPTS = 12
OPEN_DELAY = 0.02


class PasteError(Exception):
    pass


# --- the keys ---------------------------------------------------------------

# Virtual-key codes. The names are the ones the Linux side takes, so a shortcut
# stored on one machine means the same thing on the other.
KEYCODES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "super": 0x5B, "meta": 0x5B,
    "v": 0x56, "insert": 0x2D, "enter": 0x0D, "return": 0x0D,
}

# Keys that live on the extended part of the keyboard. Sent without this flag
# they arrive as their numeric-keypad twins, so Insert would be a 0.
EXTENDED = {0x2D, 0x5B}

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
MAPVK_VK_TO_VSC = 0


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


def _keys(shortcut):
    """'Ctrl+V' -> [0x11, 0x56], every one of them a key we know."""
    parts = [key.strip().lower() for key in str(shortcut).split("+") if key.strip()]
    codes = []
    for key in parts:
        if key not in KEYCODES:
            raise PasteError(t("Unknown key: {key}", key=key))
        codes.append(KEYCODES[key])
    return codes


def _event(code, up):
    flags = KEYEVENTF_KEYUP if up else 0
    if code in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    event = _Input(type=INPUT_KEYBOARD)
    event.ki = _KeyboardInput(
        wVk=code,
        # The scan code as well as the virtual key: an application reading raw
        # scan codes rather than translated keys, which is games, terminal
        # emulators and some remote-desktop clients, sees nothing without it.
        wScan=user32.MapVirtualKeyW(code, MAPVK_VK_TO_VSC),
        dwFlags=flags, time=0, dwExtraInfo=None,
    )
    return event


def key_events(shortcut):
    """The press-then-release sequence for a combination, as virtual-key codes.

    Down in the order written, up in the reverse: releasing Ctrl before V would
    leave the application with a plain V, and releasing nothing at all leaves a
    modifier stuck down for whatever the user types next.
    """
    codes = _keys(shortcut)
    return [(code, False) for code in codes] + [
        (code, True) for code in reversed(codes)]


# --- owning the clipboard ---------------------------------------------------


def _open():
    """Open the clipboard, waiting out whoever else has it. True when open."""
    for attempt in range(OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            return True
        time.sleep(OPEN_DELAY * (attempt + 1))
    return False


def _format_id(name):
    return user32.RegisterClipboardFormatW(name)


def _read_format(fmt):
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return b""
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return b""
    try:
        size = kernel32.GlobalSize(handle)
        return ctypes.string_at(pointer, size) if size else b""
    finally:
        kernel32.GlobalUnlock(handle)


def _write_format(fmt, payload):
    """Hand a buffer to the clipboard, which then owns the memory."""
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload) or 1)
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(pointer, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(handle)
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)
        return False
    return True


def _pack(parts):
    """[(format name, bytes)] -> one buffer, for copy_bytes to unpack later."""
    out = bytearray(SNAPSHOT_MAGIC)
    for name, payload in parts:
        encoded = name.encode("utf-8")
        out += len(encoded).to_bytes(4, "little") + encoded
        out += len(payload).to_bytes(8, "little") + payload
    return bytes(out)


def _unpack(data):
    """The inverse, or None when this is not one of ours.

    Strict about lengths: a snapshot that was cut short is not half a
    clipboard, it is something that did not come from here, and putting the
    readable part of it back would be inventing what somebody had copied.
    """
    if not data.startswith(SNAPSHOT_MAGIC):
        return None
    parts = []
    at = len(SNAPSHOT_MAGIC)

    def take(count):
        nonlocal at
        if at + count > len(data):
            raise ValueError("short")
        chunk = data[at:at + count]
        at += count
        return chunk

    try:
        while at < len(data):
            name = take(int.from_bytes(take(4), "little")).decode("utf-8")
            parts.append((name, take(int.from_bytes(take(8), "little"))))
    except (ValueError, UnicodeDecodeError):
        return None
    return parts


# --- the contract -----------------------------------------------------------


def read_clipboard():
    """What is on the clipboard now, as bytes to hand back to copy_bytes.

    Plain UTF-8 when there is only text on it, which is what a dictation
    replaces, and a packed snapshot when there is more than that, so restoring
    it afterwards gives back the formatting somebody had copied rather than a
    flattened version of it.
    """
    if not _open():
        return None
    try:
        text = _read_format(CF_UNICODETEXT)
        if not text:
            return None
        parts = []
        for name in EXTRA_FORMATS:
            fmt = _format_id(name)
            if fmt and user32.IsClipboardFormatAvailable(fmt):
                payload = _read_format(fmt)
                if payload:
                    parts.append((name, payload))
        # A UTF-16 buffer arrives with its terminator; the bytes handed out
        # here are text, and a stray NUL at the end of a dictation would be
        # pasted along with it.
        plain = text.decode("utf-16-le", "replace").split("\x00", 1)[0]
        if not parts:
            return plain.encode("utf-8")
        return _pack([("text", plain.encode("utf-8"))] + parts)
    finally:
        user32.CloseClipboard()


def _put(parts):
    """Own the clipboard and write every format in one go."""
    if not _open():
        return False
    try:
        user32.EmptyClipboard()
        for name, payload in parts:
            fmt = CF_UNICODETEXT if name == "text" else _format_id(name)
            if not fmt:
                continue
            if name == "text":
                payload = payload.decode("utf-8", "replace").encode("utf-16-le") + b"\x00\x00"
            _write_format(fmt, payload)
        return True
    finally:
        user32.CloseClipboard()


def copy(text):
    if not _put([("text", str(text).encode("utf-8"))]):
        raise PasteError(t(
            "Could not open the Windows clipboard: another program is holding "
            "it. Try again in a moment."
        ))


def copy_bytes(data):
    """Put a snapshot back. Never raises: it runs after the paste went in."""
    if data is None:
        return
    parts = _unpack(data)
    if parts is None:
        parts = [("text", bytes(data))]
    try:
        _put(parts)
    except OSError:
        pass


def paste_ready():
    """Windows can always send input; whether it lands is another question."""
    return True


def press(shortcut="ctrl+v", delay=0.12):
    """Press a key combination, e.g. 'ctrl+v'."""
    events = key_events(shortcut)          # raises before anything is sent
    time.sleep(delay)                      # let the clipboard settle and focus return

    array = (_Input * len(events))(*[_event(code, up) for code, up in events])
    ctypes.set_last_error(0)
    sent = user32.SendInput(len(events), ctypes.byref(array),
                            ctypes.sizeof(_Input))
    if sent == len(events):
        return

    error = ctypes.get_last_error()
    if error == ERROR_ACCESS_DENIED:
        raise PasteError(t(
            "Windows would not let Dikte type into that window, because it is "
            "running as administrator and Dikte is not. The text is on the "
            "clipboard: press {shortcut} yourself.", shortcut=shortcut.upper(),
        ))
    raise PasteError(t("Could not send the key press: Windows error {code}.",
                       code=error or "unknown"))
