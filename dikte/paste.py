"""Clipboard and key injection, through whatever this machine gives us.

A Wayland session has wl-clipboard and ydotool, an X11 one has xclip and
xdotool, and macOS has pbcopy with the key press going straight to
CoreGraphics. Which of them is here gets decided in one place, and each is a
small group of functions below it: another desktop, or another operating
system, adds a group and a line to the chooser rather than a branch inside
every function here.
"""

import collections
import ctypes
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from .i18n import t

# Linux input event codes (linux/input-event-codes.h), which is what ydotool
# takes. They are also the list of keys a paste shortcut may be built from, so
# xdotool is held to the same table rather than being handed the text as typed.
KEYCODES = {
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125,
    "v": 47, "insert": 110, "enter": 28, "return": 28,
}

# xdotool speaks X keysyms, which spell some of those differently.
KEYSYMS = {"control": "ctrl", "meta": "super", "insert": "Insert",
           "enter": "Return", "return": "Return"}

# Apple virtual key codes, which say where a key sits rather than what is
# printed on it: the same numbers on a Turkish and a US keyboard.
MAC_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "`": 50, "enter": 36, "return": 36,
}
MAC_FLAGS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "command": 1 << 20}
# What the same modifier is called on a Mac keyboard.
MAC_ALIASES = {"cmd": "command", "meta": "command", "super": "command",
               "control": "ctrl", "option": "alt"}
HID_EVENT_TAP = 0   # kCGHIDEventTap: the event goes in where the keyboard does


# pbpaste only reads text, EPS and RTF.  In particular, an image on a Mac's
# clipboard comes back as an empty byte string and pbcopy then replaces it with
# empty plain text.  Keep every NSPasteboard representation in short-lived
# files instead.  The manifest stays small even when the clipboard holds a
# large TIFF, and no additional Python package is needed.
_MAC_SNAPSHOT = collections.namedtuple("MacClipboardSnapshot", "directory manifest")

_MAC_SNAPSHOT_SCRIPT = r'''
ObjC.import("AppKit");
const root = ObjC.unwrap(
  $.NSProcessInfo.processInfo.environment.objectForKey("DIKTE_PASTEBOARD_DIR")
);
const pasteboard = $.NSPasteboard.generalPasteboard;
const items = pasteboard.pasteboardItems;
const result = [];
for (let i = 0; i < items.count; i++) {
  const item = items.objectAtIndex(i);
  const representations = [];
  const types = item.types;
  for (let j = 0; j < types.count; j++) {
    const type = ObjC.unwrap(types.objectAtIndex(j));
    const data = item.dataForType(type);
    if (!data) continue;
    const file = `${i}-${j}.bin`;
    if (data.writeToFileAtomically(`${root}/${file}`, true)) {
      representations.push({type, file});
    }
  }
  result.push(representations);
}
JSON.stringify(result);
'''

_MAC_RESTORE_SCRIPT = r'''
ObjC.import("AppKit");
const root = ObjC.unwrap(
  $.NSProcessInfo.processInfo.environment.objectForKey("DIKTE_PASTEBOARD_DIR")
);
const input = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
const source = $.NSString.alloc.initWithDataEncoding(input, $.NSUTF8StringEncoding);
const rows = JSON.parse(ObjC.unwrap(source));
const items = [];
for (const representations of rows) {
  const item = $.NSPasteboardItem.alloc.init;
  for (const representation of representations) {
    const data = $.NSData.dataWithContentsOfFile(
      `${root}/${representation.file}`
    );
    if (data) item.setDataForType(data, representation.type);
  }
  items.push(item);
}
const pasteboard = $.NSPasteboard.generalPasteboard;
pasteboard.clearContents;
pasteboard.writeObjects($(items));
'''


class PasteError(Exception):
    pass


# --- the key press, one group per system ----------------------------------

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


def _program_keyboard(program, command, hint=""):
    """A desktop that presses keys by running another program.

    Returns the three fields an entry below is built from: the program's name,
    whether it is here at all, and the press itself.
    """

    def ready():
        return shutil.which(program) is not None

    def press(shortcut, delay, _focus=None):
        # Nothing here takes the front from the window being dictated into, so
        # there is nothing to hand back: the process id is a macOS concern.
        if not ready():
            raise PasteError(t("{tool} not found, cannot paste automatically.",
                               tool=program))
        argv = command(shortcut)
        time.sleep(delay)  # let the selection settle and focus come back
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError) as exc:
            raise PasteError(t("Could not run {tool}: {error}",
                               tool=program, error=exc)) from exc
        if res.returncode != 0:
            message = t("{tool} failed: {error}", tool=program,
                        error=res.stderr.strip() or "unknown error")
            raise PasteError(f"{message}\n{t(hint)}" if hint else message)

    return {"keyboard": program, "ready": ready, "press": press}


def _macos_keys(shortcut):
    """'Cmd+V' -> (9, 0x100000): where the key sits, and the modifiers on it."""
    parts = [key.strip().lower() for key in str(shortcut).split("+") if key.strip()]
    parts = [MAC_ALIASES.get(part, part) for part in parts]
    if not parts or parts[-1] not in MAC_KEYCODES:
        raise PasteError(t("Unknown key: {key}", key=parts[-1] if parts else shortcut))
    flags = 0
    for part in parts[:-1]:
        if part not in MAC_FLAGS:
            raise PasteError(t("Unknown key: {key}", key=part))
        flags |= MAC_FLAGS[part]
    return MAC_KEYCODES[parts[-1]], flags


@functools.lru_cache(maxsize=1)
def _macos_api():
    """The bit of CoreGraphics and Accessibility a paste goes through.

    Loaded on the first paste rather than at import: this module is read on
    every system, and these two frameworks exist on one of them.
    """
    try:
        services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework"
            "/ApplicationServices"
        )
        core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
    except OSError as exc:
        raise PasteError(t("Could not run {tool}: {error}",
                           tool="CoreGraphics", error=exc)) from exc
    services.AXIsProcessTrusted.argtypes = []
    services.AXIsProcessTrusted.restype = ctypes.c_bool
    services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    core.CFDictionaryCreate.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    core.CFDictionaryCreate.restype = ctypes.c_void_p
    services.CGEventCreateKeyboardEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_ushort, ctypes.c_bool,
    ]
    services.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    services.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    services.CGEventSetFlags.restype = None
    services.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    services.CGEventPost.restype = None
    core.CFRelease.argtypes = [ctypes.c_void_p]
    core.CFRelease.restype = None
    return services, core


def _macos_trusted():
    """Whether macOS lets this process type into another application."""
    try:
        return bool(_macos_api()[0].AXIsProcessTrusted())
    except PasteError:
        return False


_asked_for_permission = False


def _macos_prompt_options(services, core):
    """{kAXTrustedCheckOptionPrompt: true}, as a CFDictionary, or 0.

    Built by hand because there is no Objective-C bridge here and this is the
    only dictionary Dikte ever makes. Its own function so that a test can hand
    back something without a framework to read the constants out of.
    """
    keys = (ctypes.c_void_p * 1)(
        ctypes.c_void_p.in_dll(services, "kAXTrustedCheckOptionPrompt"))
    values = (ctypes.c_void_p * 1)(
        ctypes.c_void_p.in_dll(core, "kCFBooleanTrue"))
    return core.CFDictionaryCreate(
        None, keys, values, 1,
        ctypes.byref(ctypes.c_void_p.in_dll(core, "kCFTypeDictionaryKeyCallBacks")),
        ctypes.byref(ctypes.c_void_p.in_dll(core, "kCFTypeDictionaryValueCallBacks")),
    )


def _macos_put_us_in_the_list():
    """Ask with the prompt, which is what creates the row to switch on.

    AXIsProcessTrusted only answers the question, and an application that has
    only ever asked it is not in Accessibility at all: the pane opens on a list
    Dikte is not in, and the only way through is the + button and a trip to the
    Applications folder. Asking with kAXTrustedCheckOptionPrompt puts it there,
    and macOS shows its own dialog with the button that opens the pane.
    """
    services, core = _macos_api()
    options = _macos_prompt_options(services, core)
    if not options:
        return
    try:
        services.AXIsProcessTrustedWithOptions(options)
    finally:
        core.CFRelease(options)


def _ask_for_permission():
    """Get Dikte into the Accessibility list, and only the first time.

    Every dictation would otherwise reopen the pane until the box is ticked,
    which is a window in the user's face on top of the paste that did not
    happen.
    """
    global _asked_for_permission
    if _asked_for_permission:
        return
    _asked_for_permission = True
    try:
        _macos_put_us_in_the_list()
    except (PasteError, OSError, ValueError):
        # An older macOS, or a framework that would not load: the pane below is
        # still worth opening, even if the row has to be added by hand.
        pass
    try:
        subprocess.Popen(
            ["open", ("x-apple.systempreferences:com.apple.preference.security"
                      "?Privacy_Accessibility")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        )
    except OSError:
        pass


def _macos_press(shortcut, delay, focus=None):
    """Post the key down and up straight into the window system.

    Nothing is typed anywhere until macOS has been told to trust Dikte, and it
    only asks once, when the paste it was granted for is first tried.

    `focus` is the application that was in front when the recording began. The
    keys land wherever the window system is pointing, so a Dikte that has ended
    up in front would swallow its own transcript; when that has happened the
    front is handed back before pressing. Nothing is taken from anyone else: an
    application the user went to while the transcription ran is where they want
    the text now.

    This runs on the transcription's own thread rather than the main one, and
    the two calls it makes are the kind AppKit documents as answering
    atomically wherever they are asked from: NSRunningApplication is thread
    safe by its own header, and the workspace lookup behind it returns a
    reference rather than anything that has to be held. Stressed with four
    threads and 32000 lookups against a running main loop without a fault; if
    one ever does happen, mac_window answers None and the press goes ahead
    where it would have gone anyway.
    """
    keycode, flags = _macos_keys(shortcut)
    services, core = _macos_api()
    if not _macos_trusted():
        _ask_for_permission()
        raise PasteError(t(
            "macOS has not been told to let Dikte press keys. Turn Dikte on "
            "under System Settings → Privacy & Security → Accessibility."
        ))
    if focus:
        # Imported here rather than at the top: it reaches for QtGui, and a
        # terminal that only wants the clipboard should not pay for that.
        from . import mac_window
        if mac_window.is_frontmost():
            mac_window.activate(focus)

    time.sleep(delay)  # let the selection settle and focus come back
    down = services.CGEventCreateKeyboardEvent(None, keycode, True)
    up = services.CGEventCreateKeyboardEvent(None, keycode, False)
    if not down or not up:
        for event in (down, up):
            if event:
                core.CFRelease(event)
        raise PasteError(t("Could not run {tool}: {error}", tool="CoreGraphics",
                           error="it would not make a keyboard event"))
    try:
        for event in (down, up):
            services.CGEventSetFlags(event, flags)
            services.CGEventPost(HID_EVENT_TAP, event)
            time.sleep(0.01)
    finally:
        core.CFRelease(down)
        core.CFRelease(up)


def _win_keys(shortcut):
    """'Ctrl+V' -> [0x11, 0x56]: Windows virtual-key codes, modifiers first."""
    codes = []
    for key in _keys(shortcut):
        if key not in WIN_KEYCODES:
            raise PasteError(t("Unknown key: {key}", key=key))
        codes.append(WIN_KEYCODES[key])
    return codes


# Windows virtual-key codes (winuser.h). Like Apple's, they say where the key
# sits rather than what a layout prints on it.
WIN_KEYCODES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "super": 0x5B, "meta": 0x5B,
    "v": 0x56, "insert": 0x2D, "enter": 0x0D, "return": 0x0D,
}
_WIN_KEYUP = 0x0002        # KEYEVENTF_KEYUP
_WIN_CF_UNICODETEXT = 13   # what the clipboard calls UTF-16 text
_WIN_GMEM_MOVEABLE = 0x0002


@functools.lru_cache(maxsize=1)
def _win_api():
    """user32 and kernel32 with their prototypes spelled out.

    The default return type is a 32-bit int, which silently truncates the
    64-bit handles and pointers every one of these calls trades in.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    return user32, kernel32


def _win_error():
    """GetLastError where it exists, so the failure paths run under any test."""
    return getattr(ctypes, "get_last_error", lambda: 0)()


def _win_open_clipboard(user32):
    """The clipboard is a lock another program may hold for a moment."""
    for _ in range(10):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _win_read_text():
    """The clipboard's text, '' when it holds none, None when it cannot be read."""
    user32, kernel32 = _win_api()
    if not _win_open_clipboard(user32):
        return None
    try:
        handle = user32.GetClipboardData(_WIN_CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _win_write_text(text):
    user32, kernel32 = _win_api()
    payload = str(text).encode("utf-16-le") + b"\x00\x00"
    # Filled before the clipboard is opened at all. EmptyClipboard is what
    # throws away whatever was there, and a failure after it and before the
    # SetClipboardData would leave the clipboard holding nothing: the one way
    # this function could lose what it was called to put back.
    handle = kernel32.GlobalAlloc(_WIN_GMEM_MOVEABLE, len(payload))
    pointer = kernel32.GlobalLock(handle) if handle else None
    if not pointer:
        if handle:
            kernel32.GlobalFree(handle)
        raise PasteError(t("Could not copy to clipboard: {error}",
                           error="out of memory"))
    ctypes.memmove(pointer, payload, len(payload))
    kernel32.GlobalUnlock(handle)

    if not _win_open_clipboard(user32):
        kernel32.GlobalFree(handle)
        raise PasteError(t("Could not copy to clipboard: {error}",
                           error="the clipboard is held by another program"))
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(_WIN_CF_UNICODETEXT, handle):
            raise PasteError(t("Could not copy to clipboard: {error}",
                               error=f"error {_win_error()}"))
        handle = None  # the clipboard owns it now
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


class _WinKeybdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class _WinMouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _WinInputUnion(ctypes.Union):
    _fields_ = [("mi", _WinMouseInput), ("ki", _WinKeybdInput)]


class _WinInput(ctypes.Structure):
    # The union carries the mouse shape too: SendInput sizes its argument by
    # the biggest member whether or not it is the one being sent.
    _fields_ = [("type", ctypes.c_ulong), ("union", _WinInputUnion)]


def _win_press(shortcut, delay, _focus=None):
    """Post the presses and releases straight into the input queue.

    No permission stands in front of SendInput the way Accessibility does on
    macOS: whatever window has focus receives the combination.

    Nothing here takes the front from the window being dictated into, so the
    remembered process id has nothing to hand back to: it is a macOS concern.
    """
    codes = _win_keys(shortcut)
    user32, _ = _win_api()
    time.sleep(delay)  # let the selection settle and focus come back

    events = ([(code, 0) for code in codes]
              + [(code, _WIN_KEYUP) for code in reversed(codes)])
    inputs = (_WinInput * len(events))()
    for entry, (code, flags) in zip(inputs, events):
        entry.type = 1  # INPUT_KEYBOARD
        entry.union.ki = _WinKeybdInput(code, 0, flags, 0, 0)
    sent = user32.SendInput(len(inputs), inputs, ctypes.sizeof(_WinInput))
    if sent != len(inputs):
        raise PasteError(t("Could not run {tool}: {error}", tool="SendInput",
                           error=f"error {_win_error()}"))


def _win_ready():
    return True


# --- which of them is here -------------------------------------------------

Desktop = collections.namedtuple(
    "Desktop",
    # The clipboard program and the two commands it is run with, what to
    # install when it is missing, the paste combinations Settings offers, and
    # the key press: the program that does it, whether it can happen at all,
    # and the pressing itself.
    "clipboard packages read_command copy_command shortcuts keyboard ready press",
)

WAYLAND = Desktop(
    clipboard="wl-copy",
    packages="wl-clipboard and ydotool",
    read_command=["wl-paste", "--no-newline"],
    copy_command=["wl-copy"],
    shortcuts=["ctrl+v", "ctrl+shift+v", "shift+insert"],
    **_program_keyboard(
        "ydotool", _ydotool_command,
        hint="Is ydotoold running? (systemctl --user status ydotool)",
    ),
)

X11 = Desktop(
    clipboard="xclip",
    packages="xclip and xdotool",
    read_command=["xclip", "-selection", "clipboard", "-out"],
    copy_command=["xclip", "-selection", "clipboard", "-in"],
    shortcuts=["ctrl+v", "ctrl+shift+v", "shift+insert"],
    **_program_keyboard("xdotool", _xdotool_command),
)

WINDOWS = Desktop(
    clipboard="",  # no program: both directions are calls into the system
    packages="",
    read_command=[],
    copy_command=[],
    shortcuts=["ctrl+v", "ctrl+shift+v", "shift+insert"],
    keyboard="",
    ready=_win_ready,
    press=_win_press,
)

MACOS = Desktop(
    clipboard="pbcopy",
    packages="",   # both are part of macOS; there is nothing to install
    read_command=["pbpaste"],
    copy_command=["pbcopy"],
    shortcuts=["cmd+v", "cmd+shift+v", "cmd+alt+shift+v"],
    keyboard="",   # no program: the key press is a call into the system
    ready=_macos_trusted,
    press=_macos_press,
)


def desktop():
    """The programs this session's clipboard and key press go through.

    Read every time rather than settled at import: a session started before the
    display server was up would otherwise be stuck with the wrong answer, and a
    test would have nowhere to say which one it means.
    """
    if sys.platform == "darwin":
        return MACOS
    if sys.platform == "win32":
        return WINDOWS
    if os.environ.get("XDG_SESSION_TYPE") == "x11":
        return X11
    if os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return X11
    return WAYLAND


# --- the clipboard ---------------------------------------------------------

def _macos_snapshot():
    """Copy every native pasteboard type to a temporary, file-backed snapshot."""
    directory = tempfile.mkdtemp(prefix="dikte-clipboard-")
    environment = dict(os.environ, DIKTE_PASTEBOARD_DIR=directory)
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _MAC_SNAPSHOT_SCRIPT],
            capture_output=True, text=True, timeout=15, env=environment,
        )
        manifest = result.stdout.strip()
        rows = json.loads(manifest) if result.returncode == 0 else None
        if not isinstance(rows, list):
            raise ValueError("the pasteboard helper returned no manifest")
        return _MAC_SNAPSHOT(directory, manifest)
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError, ValueError):
        shutil.rmtree(directory, ignore_errors=True)
        return None


def _macos_restore(snapshot):
    """Put a native snapshot back, then discard its short-lived files."""
    environment = dict(os.environ, DIKTE_PASTEBOARD_DIR=snapshot.directory)
    try:
        subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _MAC_RESTORE_SCRIPT],
            input=snapshot.manifest, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, timeout=15, env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        shutil.rmtree(snapshot.directory, ignore_errors=True)

def read_clipboard():
    here = desktop()
    if here is WINDOWS:
        text = _win_read_text()
        return None if text is None else text.encode("utf-8")
    if here is MACOS and shutil.which("osascript"):
        snapshot = _macos_snapshot()
        if snapshot is not None:
            return snapshot
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
    if here is WINDOWS:
        _win_write_text(text)
        return
    if not shutil.which(here.clipboard):
        raise PasteError(
            t("{tool} not found. Install {packages}.",
              tool=here.clipboard, packages=here.packages) if here.packages
            else t("{tool} not found.", tool=here.clipboard)
        )
    try:
        res = _run_copy(text.encode("utf-8"))
    except (subprocess.SubprocessError, OSError) as exc:
        raise PasteError(t("Could not copy to clipboard: {error}", error=exc)) from exc
    if res.returncode != 0:
        raise PasteError(t("{tool} exited with code {code}.",
                           tool=here.clipboard, code=res.returncode))


def copy_bytes(data):
    if isinstance(data, _MAC_SNAPSHOT):
        _macos_restore(data)
        return
    if data is None:
        return
    if desktop() is WINDOWS:
        try:
            _win_write_text(data.decode("utf-8", "replace"))
        except PasteError:
            pass
        return
    if not shutil.which(desktop().clipboard):
        return
    try:
        _run_copy(data)
    except (subprocess.SubprocessError, OSError):
        pass


# --- the key press ---------------------------------------------------------

def paste_ready():
    """Whether a paste can be sent: the program is here, or macOS trusts us."""
    return desktop().ready()


def press(shortcut="", delay=0.12, focus=None):
    """Press a paste combination, e.g. 'ctrl+v', or this desktop's own.

    `focus` is the process the keys are meant for, remembered when the
    recording started: see the macOS press for what is done with it.
    """
    here = desktop()
    here.press(shortcut or here.shortcuts[0], delay, focus)
