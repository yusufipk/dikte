"""Clipboard and key injection, through whichever pair of programs is here.

A Wayland session has wl-clipboard and ydotool, an X11 one has xclip and
xdotool, and a session is one or the other. macOS has pbcopy, and presses the
key itself rather than asking a program to. Which it is gets decided in one
place, and each desktop is a small group of functions below it: another
desktop, or another operating system, adds a group and a line to the chooser
rather than a branch inside every function here.
"""

import collections
import os
import shutil
import subprocess
import sys
import time

import hotkey
import macos
from i18n import t

# Linux input event codes (linux/input-event-codes.h), which is what ydotool
# takes. They are also the list of keys a paste shortcut may be built from, so
# the other two are held to the same table rather than being handed the text as
# typed. "cmd" is in here because it is the name macOS gives the key
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


def _quartz_keys(shortcut):
    """'cmd+v' -> (CGEventFlags, virtual key code).

    macOS is the one of the three where the key press is not a program to run:
    Dikte posts the event itself. Which is not a detail — permission to press a
    key belongs to whoever posts it, and a helper Dikte shells out to is judged
    by an application that may not be the one anybody allowed.
    """
    keys = _keys(shortcut)
    modifiers = 0
    rest = []
    for key in keys:
        if key in macos.CG_FLAGS:
            modifiers |= macos.CG_FLAGS[key]
        else:
            rest.append(key)
    if len(rest) != 1 or rest[0] not in hotkey.MAC_KEYS:
        raise PasteError(t("{shortcut} is not one key and its modifiers.",
                           shortcut=shortcut))
    return modifiers, hotkey.MAC_KEYS[rest[0]]


Desktop = collections.namedtuple(
    "Desktop",
    # The two programs, the packages to install them from, how to build the key
    # press, and what else to say when the key press fails.
    "clipboard keyboard packages read_command copy_command key_command "
    "type_command key_hint",
)

WAYLAND = Desktop(
    clipboard="wl-copy",
    keyboard="ydotool",
    packages="wl-clipboard and ydotool",
    read_command=["wl-paste", "--no-newline"],
    copy_command=["wl-copy"],
    key_command=_ydotool_command,
    # `--` so that a transcript beginning with a dash is text and not a flag,
    # and no delay between keys: this is a paragraph, not a demonstration.
    type_command=["ydotool", "type", "--key-delay", "0", "--"],
    key_hint="Is ydotoold running? (systemctl --user status ydotool)",
)

X11 = Desktop(
    clipboard="xclip",
    keyboard="xdotool",
    packages="xclip and xdotool",
    read_command=["xclip", "-selection", "clipboard", "-out"],
    copy_command=["xclip", "-selection", "clipboard", "-in"],
    key_command=_xdotool_command,
    type_command=["xdotool", "type", "--clearmodifiers", "--"],
    key_hint="",
)

# Both of these ship with macOS, so `packages` names no package: there is
# nothing to install, and a missing one means something is wrong with the
# system rather than with the setup.
MACOS = Desktop(
    clipboard="pbcopy",
    # The key press is not a program here: `keyboard` names what does it, which
    # is Dikte itself, and doctor reports the permission rather than a path.
    keyboard="Quartz",
    packages="the macOS command line tools",
    read_command=["pbpaste"],
    copy_command=["pbcopy"],
    key_command=_quartz_keys,
    # Not a program: macos.type_text carries the characters in the event.
    type_command=[],
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
    """Whether a key press would land anywhere.

    Two different questions behind one name. On Linux it is whether the program
    that presses keys is installed; on macOS the program is the operating
    system, and what can be missing is permission to use it. Asked here rather
    than discovered afterwards, because macOS answers a key press it did not
    allow by saying it went through.
    """
    if desktop() is MACOS:
        return macos.trusted_to_type()
    return shutil.which(desktop().keyboard) is not None


def _press_here(shortcut):
    """macOS: post the event as this process, no program in between."""
    modifiers, key_code = _quartz_keys(shortcut)
    if not macos.press_keys(modifiers, key_code):
        raise PasteError(t("Could not press {shortcut}.", shortcut=shortcut))


def _press_through_tool(here, shortcut):
    """Linux: run ydotool or xdotool and hold it to its exit code."""
    command = here.key_command(shortcut)
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


def type_out(text, delay=0.12):
    """Put `text` where the keyboard is, without going through the clipboard.

    The other way round from press(): instead of copying and asking the window
    to paste, the characters are sent as if they had been typed. Nothing
    anybody had copied is lost, no key combination has to mean paste in the
    window receiving it, and there is no clipboard left holding a dictation
    afterwards.

    Slower for a long transcript, which is why it is a setting rather than the
    way it works.
    """
    here = desktop()
    if not paste_ready():
        raise PasteError(
            f"{t('Dikte is not allowed to press keys.')}\n{t(here.key_hint)}"
            if here is MACOS else
            t("{tool} not found, cannot paste automatically.", tool=here.keyboard))

    time.sleep(delay)
    if here is MACOS:
        if not macos.type_text(text):
            raise PasteError(t("Could not type the transcript."))
        return
    try:
        res = subprocess.run([*here.type_command, text],
                             capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as exc:
        raise PasteError(t("Could not run {tool}: {error}",
                           tool=here.keyboard, error=exc)) from exc
    if res.returncode != 0:
        message = t("{tool} failed: {error}", tool=here.keyboard,
                    error=res.stderr.strip() or "unknown error")
        raise PasteError(f"{message}\n{t(here.key_hint)}" if here.key_hint
                         else message)


def press(shortcut="ctrl+v", delay=0.12):
    """Press a key combination, e.g. 'ctrl+v'."""
    here = desktop()
    if not paste_ready():
        raise PasteError(
            f"{t('Dikte is not allowed to press keys.')}\n{t(here.key_hint)}"
            if here is MACOS else
            t("{tool} not found, cannot paste automatically.", tool=here.keyboard))

    time.sleep(delay)  # let the selection settle and focus come back
    if here is MACOS:
        _press_here(shortcut)
    else:
        _press_through_tool(here, shortcut)
