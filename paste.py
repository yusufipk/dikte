"""Platform clipboard and paste-key injection."""

import ctypes
import shutil
import subprocess
import sys
import time
from functools import lru_cache

from i18n import t

IS_MACOS = sys.platform == "darwin"

# Linux input event codes (linux/input-event-codes.h)
KEYCODES = {
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125,
    "v": 47, "insert": 110, "enter": 28, "return": 28,
}

# macOS hardware key codes are layout-independent positions. Automatic paste
# normally uses Command+V, but keep the full ANSI letter row available for the
# editable shortcut field.
MAC_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "`": 50,
}
MAC_MODIFIER_FLAGS = {
    "shift": 1 << 17,
    "ctrl": 1 << 18,
    "alt": 1 << 19,
    "command": 1 << 20,
}


class PasteError(Exception):
    pass


def read_clipboard():
    if IS_MACOS:
        try:
            res = subprocess.run(
                ["pbpaste"], capture_output=True, timeout=5, check=False
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return res.stdout if res.returncode == 0 else None
    if not shutil.which("wl-paste"):
        return None
    try:
        res = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return None
    return res.stdout if res.returncode == 0 else None


def _run_wl_copy(payload):
    """wl-copy forks to keep owning the selection; leaving its pipes open makes
    subprocess.run wait for EOF forever, hence DEVNULL."""
    return subprocess.run(
        ["wl-copy"],
        input=payload,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )


def copy(text):
    if IS_MACOS:
        try:
            res = subprocess.run(
                ["pbcopy"], input=text.encode("utf-8"),
                capture_output=True, timeout=10, check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise PasteError(
                t("Could not copy to clipboard: {error}", error=exc)
            ) from exc
        if res.returncode != 0:
            raise PasteError(t("Could not copy to clipboard: {error}",
                               error=res.stderr.decode("utf-8", "replace").strip()))
        return
    if not shutil.which("wl-copy"):
        raise PasteError(t("wl-copy not found. Install wl-clipboard."))
    try:
        res = _run_wl_copy(text.encode("utf-8"))
    except (subprocess.SubprocessError, OSError) as exc:
        raise PasteError(t("Could not copy to clipboard: {error}", error=exc)) from exc
    if res.returncode != 0:
        raise PasteError(t("wl-copy exited with code {code}.", code=res.returncode))


def copy_bytes(data):
    command = "pbcopy" if IS_MACOS else "wl-copy"
    if data is None or not shutil.which(command):
        return
    try:
        if IS_MACOS:
            subprocess.run(
                ["pbcopy"], input=data, capture_output=True,
                timeout=10, check=False,
            )
        else:
            _run_wl_copy(data)
    except (subprocess.SubprocessError, OSError):
        pass


def ydotool_ready():
    if IS_MACOS:
        return True
    return shutil.which("ydotool") is not None


def press(shortcut="ctrl+v", delay=0.12):
    """Press a key combination through the platform input API."""
    if not ydotool_ready():
        raise PasteError(t("ydotool not found, cannot paste automatically."))

    if IS_MACOS:
        _press_macos(shortcut, delay)
        return

    codes = []
    for key in (k.strip().lower() for k in shortcut.split("+") if k.strip()):
        code = KEYCODES.get(key)
        if code is None:
            raise PasteError(t("Unknown key: {key}", key=key))
        codes.append(code)

    seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    time.sleep(delay)  # let the selection settle and focus come back
    try:
        res = subprocess.run(["ydotool", "key", *seq], capture_output=True,
                             text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        raise PasteError(t("Could not run ydotool: {error}", error=exc)) from exc
    if res.returncode != 0:
        raise PasteError(t(
            "ydotool failed: {error}\nIs ydotoold running? "
            "(systemctl --user status ydotool)",
            error=res.stderr.strip() or "unknown error",
        ))


def _press_macos(shortcut, delay):
    """Send a shortcut directly through CoreGraphics, without System Events."""
    keycode, flags = _parse_macos_shortcut(shortcut)
    api, core_foundation = _macos_event_api()
    if not macos_accessibility_trusted():
        _open_accessibility_settings()
        raise PasteError(t(
            "macOS Accessibility permission is required for automatic paste. "
            "Enable Dikte in System Settings."
        ))

    time.sleep(delay)
    down = api.CGEventCreateKeyboardEvent(None, keycode, True)
    up = api.CGEventCreateKeyboardEvent(None, keycode, False)
    if not down or not up:
        if down:
            core_foundation.CFRelease(down)
        if up:
            core_foundation.CFRelease(up)
        raise PasteError(t(
            "Could not paste automatically: {error}",
            error="CoreGraphics could not create a keyboard event",
        ))
    try:
        for event in (down, up):
            api.CGEventSetFlags(event, flags)
            api.CGEventPost(0, event)
            time.sleep(0.01)
    finally:
        core_foundation.CFRelease(down)
        core_foundation.CFRelease(up)


def macos_accessibility_trusted():
    """Whether this process may synthesize input through Accessibility."""
    return bool(IS_MACOS and _macos_event_api()[0].AXIsProcessTrusted())


def _parse_macos_shortcut(shortcut):
    """Return the hardware key code and CoreGraphics modifier flags."""
    parts = [part.strip().lower() for part in shortcut.split("+") if part.strip()]
    if not parts:
        raise PasteError(t("Unknown key: {key}", key=shortcut))

    aliases = {"cmd": "command", "meta": "command", "super": "command",
               "control": "ctrl", "option": "alt"}
    parts = [aliases.get(part, part) for part in parts]
    key = parts[-1]
    unknown = [part for part in parts[:-1] if part not in MAC_MODIFIER_FLAGS]
    if unknown or key not in MAC_KEYCODES:
        raise PasteError(t("Unknown key: {key}", key=shortcut))
    flags = 0
    for modifier in parts[:-1]:
        flags |= MAC_MODIFIER_FLAGS[modifier]
    return MAC_KEYCODES[key], flags


@lru_cache(maxsize=1)
def _macos_event_api():
    """Load and type the small CoreGraphics/Accessibility surface we use."""
    api = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/"
        "ApplicationServices"
    )
    core_foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    api.AXIsProcessTrusted.argtypes = []
    api.AXIsProcessTrusted.restype = ctypes.c_bool
    api.CGEventCreateKeyboardEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_ushort, ctypes.c_bool,
    ]
    api.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    api.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    api.CGEventSetFlags.restype = None
    api.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    api.CGEventPost.restype = None
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None
    return api, core_foundation


def _open_accessibility_settings():
    """Take the user directly to the one permission automatic paste needs."""
    try:
        subprocess.Popen(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security"
                "?Privacy_Accessibility",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        pass
