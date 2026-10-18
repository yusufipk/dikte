"""Which program is in front, by name, as far as this system will say.

Asked when a dictation starts, to pick the writing profile for it. Every answer
is best effort and "" means unknown, which the caller turns into the Default
profile and says so: a dictation is never worth failing over the question of
where it is going.

What each system offers, one function each and one chooser between them, the
way paste.py and audio.py are laid out:

  macOS     NSRunningApplication for the process id app.py already asks for,
            and the name of its executable
  Windows   the foreground window, its process, and the image file of that
            process, through user32 and kernel32
  X11       xprop: the active window from the root, then its class, or its
            process name from /proc when it has none
  Wayland   no protocol tells a client which window has focus, so each
            compositor is asked in its own way, where it has one:
              KDE       kdotool, when it is installed
              Hyprland  hyprctl activewindow -j
              Sway      swaymsg -t get_tree, the focused node
              niri      niri msg --json focused-window
              GNOME     the "Window Calls" Shell extension over D-Bus
            anywhere else (river, Wayfire, labwc, COSMIC...) it is unknown

The name is the program's own (thunderbird, slack, code, firefox), lower case
and without an .exe, which is what a profile lists.

For a browser, the window title can be read as well, so that a profile can be
chosen by the site in the tab. Every backend answers (name, title), and takes
a `title_for` test: the title is asked for only once the program is known and
the test says yes, in one more call (X11, KDE, GNOME, Windows). Hyprland, Sway
and niri hand the title over in the one answer they give, so there it is
dropped rather than not asked for. macOS would need the Screen Recording
permission for other programs' titles, so it reads none. A title is returned
to the caller and nothing else: it is never logged or kept here.
"""

import ast
import ctypes
import json
import os
import shutil
import subprocess
import sys

# Each call's limit: long enough for a busy X server. At worst three calls on
# X11, four under KDE, one on Hyprland, Sway and niri, and under GNOME one
# List, GNOME_DETAILS_LIMIT Details and one GetTitle. All of them run on a
# thread of their own, and app.py waits at most _ProfileLookup.WAIT (3 s) for
# them, which the worst case here stays inside.
TIMEOUT = 0.5


def _clean(name):
    # Either separator, so a Windows path reads the same wherever this runs.
    name = _undeleted((name or "").strip())
    name = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _call(argv):
    """A finished command, or None when it could not run or timed out."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def _run(argv):
    res = _call(argv)
    return res.stdout if res is not None and res.returncode == 0 else ""


_DELETED = " (deleted)"


def _undeleted(name):
    """What /proc says of a program upgraded while it ran: "thunderbird
    (deleted)", the file it was started from being gone."""
    return name[:-len(_DELETED)] if name.endswith(_DELETED) else name


def _process_name(pid):
    """A Linux process's name, from /proc: the executable's base name, or comm
    (cut at 15 characters) when that is not readable. Only the fallback: a
    window's class names the program better, since this says "electron",
    "python3.14" or "java" for whatever runs on one of those."""
    try:
        return _undeleted(os.path.basename(os.readlink(f"/proc/{int(pid)}/exe")))
    except (OSError, ValueError):
        pass
    try:
        with open(f"/proc/{int(pid)}/comm", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


def _wants(name, title_for):
    return bool(name) and title_for is not None and title_for(_clean(name))


def _answer(name, title, title_for):
    """(name, title), the title kept only for a program `title_for` wants."""
    name = name if isinstance(name, str) else ""
    title = title if isinstance(title, str) else ""
    return name, (title if _wants(name, title_for) else "")


NOTHING = ("", "")


# --- macOS ------------------------------------------------------------------

def _macos(pid, title_for=None):
    """The name only: no title on macOS (see the module docstring)."""
    return _macos_name(pid), ""


def _macos_name(pid):
    from . import mac_window
    if not pid:
        pid = mac_window.frontmost_pid()
    if not pid:
        return ""
    try:
        api = mac_window._appkit()
        ask_text = api._as(ctypes.c_char_p)
        running = ctypes.c_void_p(api.ask_of_pid(
            ctypes.c_void_p(api.objc.objc_getClass(b"NSRunningApplication")),
            api.selector(b"runningApplicationWithProcessIdentifier:"),
            int(pid)))
        if not running:
            return ""
        url = ctypes.c_void_p(api.ask(running, api.selector(b"executableURL")))
        if not url:
            return ""
        component = ctypes.c_void_p(api.ask(url, api.selector(b"lastPathComponent")))
        if not component:
            return ""
        name = ask_text(component, api.selector(b"UTF8String"))
        return name.decode("utf-8", "replace") if name else ""
    except Exception:
        return ""


# --- Windows ----------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _win_api():
    """user32 and kernel32 with the prototypes used here spelled out; the one
    function a test replaces to stand on Windows."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return user32, kernel32


def _windows(_pid=None, title_for=None):
    name, title = "", ""
    try:
        user32, kernel32 = _win_api()
        window = user32.GetForegroundWindow()
        if window:
            name = _win_image(user32, kernel32, window)
            if _wants(name, title_for):
                length = user32.GetWindowTextLengthW(window)
                if length > 0:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(window, buffer, length + 1)
                    title = buffer.value
    except Exception:
        pass
    return name, title


def _win_image(user32, kernel32, window):
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid.value)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                   ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


# --- X11 --------------------------------------------------------------------

_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _xprop_string(value, encoding="utf-8"):
    """A string as xprop prints one: in quotes, with backslash escapes (\\n,
    \\t, \\\\, \\" and octal for bytes it would not print). The bytes are UTF-8
    for _NET_WM_NAME and Latin-1 for the older WM_NAME."""
    value = value.strip()
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return ""
    out, i, inner = bytearray(), 0, value[1:-1]
    while i < len(inner):
        char = inner[i]
        if char == "\\" and i + 1 < len(inner):
            digits = inner[i + 1:i + 4]
            if len(digits) == 3 and all(c in "01234567" for c in digits):
                out.append(int(digits, 8) & 0xFF)
                i += 4
                continue
            following = inner[i + 1]
            out.extend(_ESCAPES.get(following, following).encode(encoding, "replace"))
            i += 2
            continue
        out.extend(char.encode(encoding, "replace"))
        i += 1
    return out.decode(encoding, "replace")


def _xprop_value(found, prefix):
    for line in found.splitlines():
        if line.startswith(prefix) and "=" in line:
            return line.split("=", 1)[1]
    return ""


def _x11(_pid=None, title_for=None):
    """The active window, its class (or process), and only for a program that
    wants it, its title, in a request of its own."""
    if not shutil.which("xprop"):
        return NOTHING
    active = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    # "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007"
    window = active.rsplit("#", 1)[-1].strip().split(",")[0].strip()
    if not window.startswith("0x") or int(window, 16) == 0:
        return NOTHING
    found = _run(["xprop", "-id", window, "_NET_WM_PID", "WM_CLASS"])
    pid = _xprop_value(found, "_NET_WM_PID").strip()
    # 'WM_CLASS(STRING) = "Navigator", "firefox"': the second is the class,
    # which is the program; the first is one of its windows.
    parts = [p.strip().strip('"') for p in _xprop_value(found, "WM_CLASS").split(",")]
    klass = parts[-1] if parts else ""
    # The class first: the process behind a window is often a runtime shared
    # by many programs, and a Flatpak's pid is not even one of this system's.
    name = klass or (_process_name(pid) if pid.isdigit() else "")
    title = ""
    if _wants(name, title_for):
        named = _run(["xprop", "-id", window, "_NET_WM_NAME", "WM_NAME"])
        title = (_xprop_string(_xprop_value(named, "_NET_WM_NAME"))
                 or _xprop_string(_xprop_value(named, "WM_NAME"), "latin-1"))
    return name, title


# --- Wayland ----------------------------------------------------------------

def _kde_wayland(_pid=None, title_for=None):
    """KWin through kdotool. The window class first, for the same reason as
    under X11, and the process only when there is no class. The title is one
    more call, made only for a program that wants it."""
    if not shutil.which("kdotool"):
        return NOTHING
    window = _run(["kdotool", "getactivewindow"]).strip()
    if not window:
        return NOTHING
    name = _run(["kdotool", "getwindowclassname", window]).strip()
    if not name:
        pid = _run(["kdotool", "getwindowpid", window]).strip()
        name = _process_name(pid) if pid.isdigit() else ""
    title = ""
    if _wants(name, title_for):
        title = _run(["kdotool", "getwindowname", window]).strip()
    return name, title


def _unknown(_pid=None, title_for=None):
    return NOTHING


def _json(text):
    try:
        return json.loads(text) if text.strip() else None
    except ValueError:
        return None


def _hyprland(_pid=None, title_for=None):
    """One call: the active window's class (the one it started with when it
    has none now) and its title, in one JSON object."""
    if not shutil.which("hyprctl"):
        return NOTHING
    window = _json(_run(["hyprctl", "activewindow", "-j"]))
    if not isinstance(window, dict):
        return NOTHING
    name = window.get("class") or window.get("initialClass") or ""
    return _answer(name, window.get("title"), title_for)


def _sway_focused(node):
    """The focused node of a sway tree, tiled or floating, or None."""
    stack = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        if current.get("focused") is True:
            return current
        for key in ("nodes", "floating_nodes"):
            children = current.get(key)
            if isinstance(children, list):
                stack.extend(children)
    return None


def _sway(_pid=None, title_for=None):
    """One call: the whole tree, then the focused node in it. A native window
    names itself by app_id; one under Xwayland has none, and its X11 class
    stands in."""
    if not shutil.which("swaymsg"):
        return NOTHING
    focused = _sway_focused(_json(_run(["swaymsg", "-t", "get_tree", "-r"])))
    if focused is None:
        return NOTHING
    name = focused.get("app_id")
    if not name:
        properties = focused.get("window_properties")
        name = properties.get("class") if isinstance(properties, dict) else ""
    return _answer(name or "", focused.get("name"), title_for)


def _niri(_pid=None, title_for=None):
    """One call; "null" when nothing has focus, and either field may be
    missing."""
    if not shutil.which("niri"):
        return NOTHING
    window = _json(_run(["niri", "msg", "--json", "focused-window"]))
    if not isinstance(window, dict):
        return NOTHING
    return _answer(window.get("app_id") or "", window.get("title"), title_for)


_GNOME_WINDOWS = ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                  "--object-path", "/org/gnome/Shell/Extensions/Windows",
                  "--method"]
_GNOME_INTERFACE = "org.gnome.Shell.Extensions.Windows."
# Details asked of at most this many windows, newest first, looking for the
# focused one: one call each, and with List and GetTitle the worst case is
# five calls, 2.5 s, inside the 3 s app.py waits.
GNOME_DETAILS_LIMIT = 3

# What the last GNOME lookup learned about the extension: True when it is
# there, False when D-Bus said the interface or the object does not exist,
# None before anything was asked. The Profiles tab reads it instead of
# guessing at a folder name.
gnome_extension = {"present": None}

_MISSING = ("UnknownObject", "UnknownMethod", "UnknownInterface",
            "ServiceUnknown", "No such interface", "No such object")


def _gvariant_string(output):
    """The one string out of what gdbus prints for a reply: a GVariant tuple,
    ('...',), in a syntax close enough to Python's literals to be read as
    one. Anything else is ""."""
    text = output.strip()
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return ""
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        return value[0]
    return ""


def _gnome_call(method, *arguments):
    res = _call(_GNOME_WINDOWS + [_GNOME_INTERFACE + method]
                + [str(a) for a in arguments])
    if res is None:
        return ""
    if res.returncode != 0:
        if any(sign in (res.stderr or "") for sign in _MISSING):
            gnome_extension["present"] = False
        return ""
    gnome_extension["present"] = True
    return _gvariant_string(res.stdout)


def _gnome(_pid=None, title_for=None):
    """GNOME Shell keeps window focus to itself; the Window Calls extension
    lends it out over D-Bus. List first (which may already say which window
    has focus), Details for a few until the focused one, and the title only
    for a program that wants it. Without the extension every call fails,
    and the answer is unknown."""
    if not shutil.which("gdbus"):
        return NOTHING
    windows = _json(_gnome_call("List"))
    if not isinstance(windows, list):
        return NOTHING
    windows = [w for w in windows if isinstance(w, dict)
               and isinstance(w.get("id"), int) and not isinstance(w.get("id"), bool)]
    focused = next((w for w in windows if w.get("focus") is True), None)
    if focused is None and not any("focus" in w for w in windows):
        for window in list(reversed(windows))[:GNOME_DETAILS_LIMIT]:
            details = _json(_gnome_call("Details", window["id"]))
            if isinstance(details, dict) and details.get("focus") is True:
                focused = dict(window, **details)
                break
    if focused is None:
        return NOTHING
    name = focused.get("wm_class") or focused.get("wm_class_instance") or ""
    if not isinstance(name, str):
        name = ""
    title = _gnome_call("GetTitle", focused["id"]) if _wants(name, title_for) else ""
    return name, title


def probe_gnome():
    """Ask the extension once, for the Profiles tab. Off the main thread."""
    if shutil.which("gdbus"):
        _gnome_call("List")
    return gnome_extension["present"]


def _desktops():
    """XDG_CURRENT_DESKTOP's entries, lower case, most specific first
    ("Budgie:GNOME" is Budgie)."""
    value = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    return [entry for entry in value.split(":") if entry]


# The compositors that announce themselves with a socket variable. One left
# over in the environment of another session (a terminal started under sway,
# then a KDE login) must not send that session to the wrong compositor, so
# the variable counts only when XDG_CURRENT_DESKTOP agrees or says nothing.
_COMPOSITORS = (("HYPRLAND_INSTANCE_SIGNATURE", "hyprland", "_hyprland"),
                ("SWAYSOCK", "sway", "_sway"),
                ("NIRI_SOCKET", "niri", "_niri"))


_NOT_GNOME_SHELL = frozenset(("budgie", "x-cinnamon", "cinnamon", "unity", "mate",
                              "xfce", "lxqt", "pantheon"))


def backend():
    """The function that can answer on this session. Read every time, like
    paste.desktop(), so a test can say where it is standing."""
    if sys.platform == "darwin":
        return _macos
    if sys.platform == "win32":
        return _windows
    if os.environ.get("XDG_SESSION_TYPE") == "x11":
        return _x11
    desktops = _desktops()
    for variable, name, function in _COMPOSITORS:
        if name in desktops or (os.environ.get(variable) and not desktops):
            return globals()[function]
    if os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return _x11
    if "kde" in desktops:
        return _kde_wayland
    # GNOME Shell, whatever the distribution calls its session (ubuntu:GNOME,
    # pop:GNOME, zorin:GNOME), but not a desktop that only builds on GNOME
    # and runs a shell of its own, which loads no Shell extensions.
    if "gnome" in desktops and desktops[0] not in _NOT_GNOME_SHELL:
        return _gnome
    return _unknown


def front(pid=None, title_for=None):
    """(program, title) of the window in front. The title is read only when
    `title_for(program)` says so, and is "" otherwise or when it cannot be.

    `pid` is what macOS has already found out about the front, which saves
    asking twice; elsewhere it is not needed."""
    try:
        name, title = backend()(pid, title_for=title_for)
    except Exception:
        return NOTHING
    return _clean(name), (title or "").strip()


def program_name(pid=None):
    """The program in front, lower case and bare, or "" when it is unknown."""
    return front(pid)[0]
