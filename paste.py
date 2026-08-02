"""Clipboard and key injection, through whichever pair of programs is here.

A Wayland session has wl-clipboard and ydotool, an X11 one has xclip and
xdotool, and a session is one or the other. macOS has pbcopy and osascript and
is neither. Which it is gets decided in one place, and each desktop is a small
group of functions below it: another desktop, or another operating system, adds
a group and a line to the chooser rather than a branch inside every function
here.
"""

import collections
import os
import shutil
import subprocess
import sys
import time

from i18n import t

# Linux input event codes (linux/input-event-codes.h), which is what ydotool
# takes. They are also the list of keys a paste shortcut may be built from, so
# xdotool and osascript are held to the same table rather than being handed the
# text as typed. "cmd" is in here because it is the name macOS gives the key
# Linux calls super, not because ydotool has ever been asked for one.
KEYCODES = {
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "option": 56,
    "super": 125, "meta": 125, "cmd": 125, "command": 125,
    "v": 47, "insert": 110, "enter": 28, "return": 28,
}

# xdotool speaks X keysyms, which spell some of those differently.
KEYSYMS = {"control": "ctrl", "meta": "super", "cmd": "super", "command": "super",
           "option": "alt", "insert": "Insert",
           "enter": "Return", "return": "Return"}

# AppleScript names the modifiers in a `using {…}` clause, and the same physical
# key answers to both names: what Linux calls super is what macOS calls command.
MODIFIER_PHRASES = {
    "cmd": "command down", "command": "command down",
    "super": "command down", "meta": "command down",
    "ctrl": "control down", "control": "control down",
    "shift": "shift down", "alt": "option down", "option": "option down",
}

# The keys AppleScript cannot type as a character, by their virtual key code.
# There is no insert key on a Mac keyboard; the code is the one macOS maps it to
# when an external keyboard has one.
MAC_KEY_CODES = {"enter": 36, "return": 36, "insert": 114}


class PasteError(Exception):
    pass


def _keys(shortcut):
    """'Ctrl+V' -> ['ctrl', 'v'], every one of them a key we know."""
    parts = [key.strip().lower() for key in str(shortcut).split("+") if key.strip()]
    for key in parts:
        if key not in KEYCODES:
            raise PasteError(t("Unknown key: {key}", key=key))
    return parts


def _ydotool_command(shortcut):
    """ydotool wants a press event per key, then a release in reverse."""
    codes = [KEYCODES[key] for key in _keys(shortcut)]
    return ["ydotool", "key", *[f"{code}:1" for code in codes],
            *[f"{code}:0" for code in reversed(codes)]]


def _xdotool_command(shortcut):
    """xdotool takes the whole combination as one argument."""
    keys = [KEYSYMS.get(key, key) for key in _keys(shortcut)]
    return ["xdotool", "key", "--clearmodifiers", "+".join(keys)]


def _osascript_command(shortcut):
    """AppleScript presses the combination as one keystroke with its modifiers.

    There is no press-and-release pair to build here: `keystroke "v" using
    {command down}` is the whole event, and the modifiers are what it is held
    down with rather than keys of their own.
    """
    keys = _keys(shortcut)
    modifiers = [MODIFIER_PHRASES[key] for key in keys if key in MODIFIER_PHRASES]
    rest = [key for key in keys if key not in MODIFIER_PHRASES]
    if len(rest) != 1:
        raise PasteError(t("{shortcut} is not one key and its modifiers.",
                           shortcut=shortcut))
    key = rest[0]
    press = (f"key code {MAC_KEY_CODES[key]}" if key in MAC_KEY_CODES
             else f'keystroke "{key}"')
    using = f" using {{{', '.join(modifiers)}}}" if modifiers else ""
    return ["osascript", "-e",
            f'tell application "System Events" to {press}{using}']


Desktop = collections.namedtuple(
    "Desktop",
    # The two programs, the packages to install them from, how to build the key
    # press, and what else to say when the key press fails.
    "clipboard keyboard packages read_command copy_command key_command key_hint",
)

WAYLAND = Desktop(
    clipboard="wl-copy",
    keyboard="ydotool",
    packages="wl-clipboard and ydotool",
    read_command=["wl-paste", "--no-newline"],
    copy_command=["wl-copy"],
    key_command=_ydotool_command,
    key_hint="Is ydotoold running? (systemctl --user status ydotool)",
)

X11 = Desktop(
    clipboard="xclip",
    keyboard="xdotool",
    packages="xclip and xdotool",
    read_command=["xclip", "-selection", "clipboard", "-out"],
    copy_command=["xclip", "-selection", "clipboard", "-in"],
    key_command=_xdotool_command,
    key_hint="",
)

# Both of these ship with macOS, so `packages` names no package: there is
# nothing to install, and a missing one means something is wrong with the
# system rather than with the setup.
MACOS = Desktop(
    clipboard="pbcopy",
    keyboard="osascript",
    packages="the macOS command line tools",
    read_command=["pbpaste"],
    copy_command=["pbcopy"],
    key_command=_osascript_command,
    key_hint="Allow the application running Dikte to control your computer, "
             "under System Settings → Privacy & Security → Accessibility.",
)


def desktop():
    """The pair of programs this session's clipboard and keyboard go through.

    Read every time rather than settled at import: a session started before the
    display server was up would otherwise be stuck with the wrong answer, and a
    test would have nowhere to say which one it means.
    """
    if sys.platform == "darwin":
        return MACOS
    if os.environ.get("XDG_SESSION_TYPE") == "x11":
        return X11
    if os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return X11
    return WAYLAND


# --- the clipboard ---------------------------------------------------------

def read_clipboard():
    here = desktop()
    if not shutil.which(here.read_command[0]):
        return None
    try:
        res = subprocess.run(here.read_command, capture_output=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return None
    return res.stdout if res.returncode == 0 else None


def _run_copy(payload):
    """The clipboard owner forks to keep holding the selection; leaving its
    pipes open makes subprocess.run wait for EOF forever, hence DEVNULL."""
    return subprocess.run(
        desktop().copy_command,
        input=payload,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )


def copy(text):
    here = desktop()
    if not shutil.which(here.clipboard):
        raise PasteError(t("{tool} not found. Install {packages}.",
                           tool=here.clipboard, packages=t(here.packages)))
    try:
        res = _run_copy(text.encode("utf-8"))
    except (subprocess.SubprocessError, OSError) as exc:
        raise PasteError(t("Could not copy to clipboard: {error}", error=exc)) from exc
    if res.returncode != 0:
        raise PasteError(t("{tool} exited with code {code}.",
                           tool=here.clipboard, code=res.returncode))


def copy_bytes(data):
    if data is None or not shutil.which(desktop().clipboard):
        return
    try:
        _run_copy(data)
    except (subprocess.SubprocessError, OSError):
        pass


# --- the key press ---------------------------------------------------------

def paste_ready():
    return shutil.which(desktop().keyboard) is not None


def press(shortcut="ctrl+v", delay=0.12):
    """Press a key combination, e.g. 'ctrl+v'."""
    here = desktop()
    if not paste_ready():
        raise PasteError(t("{tool} not found, cannot paste automatically.",
                           tool=here.keyboard))

    command = here.key_command(shortcut)
    time.sleep(delay)  # let the selection settle and focus come back
    try:
        res = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        raise PasteError(t("Could not run {tool}: {error}",
                           tool=here.keyboard, error=exc)) from exc
    if res.returncode != 0:
        message = t("{tool} failed: {error}", tool=here.keyboard,
                    error=res.stderr.strip() or "unknown error")
        raise PasteError(f"{message}\n{t(here.key_hint)}" if here.key_hint
                         else message)
