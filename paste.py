"""Clipboard and key injection.

Linux goes through wl-clipboard and ydotool, two programs that have to be
installed. Windows has both built into the system, reached here through the
Win32 API directly.
"""

import shutil
import subprocess
import time

from i18n import t
from platform_utils import IS_WINDOWS


class PasteError(Exception):
    pass


# Another program can hold the clipboard open for a moment at a time — a
# clipboard manager reading what was just put there is the usual one — and the
# open fails outright rather than waiting. Asking again a few times over turns
# that into the non-event it should be.
CLIPBOARD_TRIES = 6
CLIPBOARD_WAIT = 0.03


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))
        ]

    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", ctypes.c_ulong * 7), ("hi", ctypes.c_ulong * 4)]
        _anonymous_ = ("_input",)
        _fields_ = [("type", wintypes.DWORD), ("_input", _INPUT)]

    KEYCODES = {
        "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "super": 0x5B, "meta": 0x5B,
        "v": 0x56, "insert": 0x2D, "enter": 0x0D, "return": 0x0D,
    }

    def _open_clipboard():
        for attempt in range(CLIPBOARD_TRIES):
            if user32.OpenClipboard(0):
                return True
            time.sleep(CLIPBOARD_WAIT * (attempt + 1))
        return False

    def read_clipboard():
        if not _open_clipboard():
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                value = ctypes.c_wchar_p(ptr).value
            finally:
                kernel32.GlobalUnlock(handle)
            return value.encode("utf-8") if value is not None else None
        finally:
            user32.CloseClipboard()

    def copy(text):
        if not _open_clipboard():
            raise PasteError(t("Could not open clipboard."))
        try:
            user32.EmptyClipboard()
            data = text + "\0"
            size = len(data) * ctypes.sizeof(ctypes.c_wchar)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                raise PasteError(t("Could not allocate memory for clipboard."))
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                raise PasteError(t("Could not lock clipboard memory."))
            try:
                ctypes.memmove(ptr, ctypes.c_wchar_p(data), size)
            finally:
                kernel32.GlobalUnlock(handle)
            # On success the clipboard owns the block; on failure nobody does.
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                raise PasteError(t("Could not set clipboard data."))
        finally:
            user32.CloseClipboard()

    def copy_bytes(data):
        if data is None:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        try:
            copy(text)
        except PasteError:
            pass

    def ydotool_ready():
        return True

    def press(shortcut="ctrl+v", delay=0.12):
        codes = []
        for key in (k.strip().lower() for k in shortcut.split("+") if k.strip()):
            code = KEYCODES.get(key)
            if code is None:
                raise PasteError(t("Unknown key: {key}", key=key))
            codes.append(code)

        time.sleep(delay)

        inputs = []
        for code in codes:
            inputs.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=code, dwFlags=0)))
        for code in reversed(codes):
            inputs.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=code, dwFlags=KEYEVENTF_KEYUP)))

        n = len(inputs)
        array = (INPUT * n)(*inputs)
        sent = user32.SendInput(n, ctypes.byref(array), ctypes.sizeof(INPUT))
        if sent != n:
            # Windows refuses to send keys to a window running with more
            # privilege than the sender, and says so only through this count.
            # The text is on the clipboard either way, so this is a warning.
            raise PasteError(t(
                "Windows blocked the paste. The focused window is probably "
                "running as administrator; press {shortcut} yourself, the text "
                "is on the clipboard.", shortcut=shortcut,
            ))

else:
    # Linux input event codes (linux/input-event-codes.h)
    KEYCODES = {
        "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125,
        "v": 47, "insert": 110, "enter": 28, "return": 28,
    }

    def read_clipboard():
        if not shutil.which("wl-paste"):
            return None
        try:
            wl_paste = shutil.which("wl-paste")
            res = subprocess.run([wl_paste, "--no-newline"], capture_output=True, timeout=5, check=False)
        except (subprocess.SubprocessError, OSError):
            return None
        return res.stdout if res.returncode == 0 else None

    def _run_wl_copy(payload):
        """wl-copy forks to keep owning the selection; leaving its pipes open makes
        subprocess.run wait for EOF forever, hence DEVNULL."""
        wl_copy = shutil.which("wl-copy")
        return subprocess.run(
            [wl_copy],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )

    def copy(text):
        if not shutil.which("wl-copy"):
            raise PasteError(t("wl-copy not found. Install wl-clipboard."))
        try:
            res = _run_wl_copy(text.encode("utf-8"))
        except (subprocess.SubprocessError, OSError) as exc:
            raise PasteError(t("Could not copy to clipboard: {error}", error=exc)) from exc
        if res.returncode != 0:
            raise PasteError(t("wl-copy exited with code {code}.", code=res.returncode))

    def copy_bytes(data):
        if data is None or not shutil.which("wl-copy"):
            return
        try:
            _run_wl_copy(data)
        except (subprocess.SubprocessError, OSError):
            pass

    def ydotool_ready():
        return shutil.which("ydotool") is not None

    def press(shortcut="ctrl+v", delay=0.12):
        """Press a key combination through ydotool, e.g. 'ctrl+v'."""
        if not ydotool_ready():
            raise PasteError(t("ydotool not found, cannot paste automatically."))

        codes = []
        for key in (k.strip().lower() for k in shortcut.split("+") if k.strip()):
            code = KEYCODES.get(key)
            if code is None:
                raise PasteError(t("Unknown key: {key}", key=key))
            codes.append(code)

        seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        time.sleep(delay)  # let the selection settle and focus come back
        try:
            ydotool = shutil.which("ydotool")
            res = subprocess.run([ydotool, "key", *seq], capture_output=True,
                                 text=True, timeout=10, check=True)
        except (subprocess.SubprocessError, OSError) as exc:
            raise PasteError(t("Could not run ydotool: {error}", error=exc)) from exc
        if res.returncode != 0:
            raise PasteError(t(
                "ydotool failed: {error}\nIs ydotoold running? "
                "(systemctl --user status ydotool)",
                error=res.stderr.strip() or "unknown error",
            ))
