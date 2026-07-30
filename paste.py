"""Platform clipboard and paste-key injection."""

import shutil
import subprocess
import sys
import time

from i18n import t

IS_MACOS = sys.platform == "darwin"

# Linux input event codes (linux/input-event-codes.h)
KEYCODES = {
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125,
    "v": 47, "insert": 110, "enter": 28, "return": 28,
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
        return shutil.which("osascript") is not None
    return shutil.which("ydotool") is not None


def press(shortcut="ctrl+v", delay=0.12):
    """Press a key combination through the platform input API."""
    if not ydotool_ready():
        message = ("osascript not found, cannot paste automatically." if IS_MACOS
                   else "ydotool not found, cannot paste automatically.")
        raise PasteError(t(message))

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
    """Send a shortcut through System Events.

    macOS asks for Accessibility permission the first time Dikte tries this.
    """
    parts = [part.strip().lower() for part in shortcut.split("+") if part.strip()]
    if not parts:
        raise PasteError(t("Unknown key: {key}", key=shortcut))

    aliases = {"cmd": "command", "meta": "command", "super": "command",
               "control": "ctrl", "option": "alt"}
    parts = [aliases.get(part, part) for part in parts]
    key = parts[-1]
    modifiers = {
        "command": "command down",
        "ctrl": "control down",
        "alt": "option down",
        "shift": "shift down",
    }
    unknown = [part for part in parts[:-1] if part not in modifiers]
    if unknown or len(key) != 1:
        raise PasteError(t("Unknown key: {key}", key=shortcut))

    using = ", ".join(modifiers[part] for part in parts[:-1])
    script = f'tell application "System Events" to keystroke "{key}"'
    if using:
        script += f" using {{{using}}}"
    time.sleep(delay)
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise PasteError(t("Could not paste automatically: {error}",
                           error=exc)) from exc
    if res.returncode != 0:
        raise PasteError(t(
            "Could not paste automatically: {error}",
            error=res.stderr.strip() or "unknown error",
        ))
