"""The parts of AppKit a dictation needs on macOS and Qt does not reach.

Two jobs, both about staying out of the user's way.

The first is keeping the indicator on screen. Qt draws it as a tool window,
which on macOS is an NSPanel, and an NSPanel is hidden by the system the moment
its application stops being the active one. For a dictation indicator that is
exactly backwards: you press the shortcut inside some other program, so Dikte is
never the active application, and the one window that has something to say
disappears as soon as you look away from it. Three settings AppKit has and Qt
does not expose:

  hidesOnDeactivate = NO      stay put when another application comes forward
  collectionBehavior          show on whichever desktop is in front, including
                              over a full screen window, and stay out of Cmd+Tab
  nonactivating panel         come to the front without bringing Dikte with it

The second is putting the front back. Opening the microphone activates Dikte
whatever the indicator does, so app.py watches for that and calls activate()
here. See _give_the_front_back() there for the measurement.

Done through the Objective-C runtime rather than a binding, because Dikte has no
third party Python packages and this is a handful of messages to three objects.
The runtime is loaded in _appkit() rather than at import, the way paste.py loads
its frameworks in _macos_api(): that is the one function a test fakes, and it is
what lets the tests below run on a machine that has no AppKit at all.
"""

import ctypes
import ctypes.util
import os

from PyQt6.QtGui import QGuiApplication

# NSWindowCollectionBehavior, as of the macOS these names come from:
CAN_JOIN_ALL_SPACES = 1 << 0
IGNORES_CYCLE = 1 << 6        # not a window Cmd+Tab should ever land on
FULL_SCREEN_AUXILIARY = 1 << 8
BEHAVIOUR = CAN_JOIN_ALL_SPACES | IGNORES_CYCLE | FULL_SCREEN_AUXILIARY

# NSWindowStyleMaskNonactivatingPanel. Without it, ordering the indicator to
# the front brings Dikte to the front with it, and the application the user was
# typing in loses focus the moment they start dictating: the Cmd+V at the end
# then lands on the indicator instead of their document. Qt has no flag for
# this; WA_ShowWithoutActivating governs the show, not what the panel does to
# the application afterwards.
NONACTIVATING_PANEL = 1 << 7

_appkit_runtime = None


class _AppKit:
    """objc_msgSend under the signatures this file sends it through.

    It has no fixed signature of its own, and calling it through the wrong
    argument or return types is how a Mac crashes rather than raises, so each
    one is spelled out once here and used by name below.
    """

    def __init__(self, objc):
        self.objc = objc
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.ask = self._as(ctypes.c_void_p)
        self.ask_bool = self._as(ctypes.c_bool)
        self.ask_pid = self._as(ctypes.c_int)          # pid_t is an int32
        self.ask_unsigned = self._as(ctypes.c_ulong)   # NSUInteger
        self.tell_bool = self._as(None, ctypes.c_bool)
        self.tell_unsigned = self._as(None, ctypes.c_ulong)
        self.ask_of_class = self._as(ctypes.c_bool, ctypes.c_void_p)
        self.ask_of_pid = self._as(ctypes.c_void_p, ctypes.c_int)
        self.ask_with_options = self._as(ctypes.c_bool, ctypes.c_ulong)

    def _as(self, returns, *arguments):
        return ctypes.cast(self.objc.objc_msgSend, ctypes.CFUNCTYPE(
            returns, ctypes.c_void_p, ctypes.c_void_p, *arguments))

    def selector(self, name):
        return self.objc.sel_registerName(name)

    def shared(self, class_name, selector):
        """A class's singleton, e.g. +[NSWorkspace sharedWorkspace]."""
        return ctypes.c_void_p(self.ask(
            ctypes.c_void_p(self.objc.objc_getClass(class_name)),
            self.selector(selector)))


def _appkit():
    """The Objective-C runtime, loaded the first time something needs it.

    Loaded here rather than at import so that this module can be imported on a
    machine that has no AppKit: the tests stand on macOS from a Linux machine
    and back, and this is the one function they replace to do it.
    """
    global _appkit_runtime
    if _appkit_runtime is None:
        _appkit_runtime = _AppKit(
            ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc")))
    return _appkit_runtime


def frontmost_pid():
    """Which application is in front, by process id, or None when unasked.

    A process id rather than the object itself: the object would have to be
    retained to survive the trip, and a number needs nothing looking after it.
    """
    try:
        api = _appkit()
        workspace = api.shared(b"NSWorkspace", b"sharedWorkspace")
        running = ctypes.c_void_p(api.ask(
            workspace, api.selector(b"frontmostApplication")))
        if not running:
            return None
        return int(api.ask_pid(running, api.selector(b"processIdentifier")))
    except Exception:
        return None


def activate(pid):
    """Put the application with that process id back in front.

    False when it has gone away in the meantime, or when the message could not
    be sent at all: a dictation is not worth failing over the window behind it.
    """
    if not pid:
        return False
    try:
        api = _appkit()
        running = ctypes.c_void_p(api.ask_of_pid(
            ctypes.c_void_p(api.objc.objc_getClass(b"NSRunningApplication")),
            api.selector(b"runningApplicationWithProcessIdentifier:"),
            int(pid)))
        if not running:
            return False
        # activateWithOptions: rather than the deprecated activate, and with no
        # options: bringing every one of its windows forward is not asked for,
        # only the application it was before Dikte took the front from it.
        return bool(api.ask_with_options(
            running, api.selector(b"activateWithOptions:"), 0))
    except Exception:
        return False


def is_frontmost():
    """Whether Dikte itself is the application in front.

    By process id rather than -[NSRunningApplication isActive] on our own
    process, which stays true once the application has ever been activated:
    measured True with another application plainly in front.
    """
    pid = frontmost_pid()
    return pid is not None and pid == os.getpid()


def _is_panel(api, window):
    """Whether this window is an NSPanel, which is the only kind the
    nonactivating bit is legal on: setting it on a plain NSWindow raises an
    Objective-C exception, and an exception through ctypes takes the process
    down with it."""
    panel = api.objc.objc_getClass(b"NSPanel")
    if not panel:
        return False
    return bool(api.ask_of_class(window, api.selector(b"isKindOfClass:"),
                                 ctypes.c_void_p(panel)))


def keep_on_screen(widget):
    """Ask the window behind `widget` to stay while other programs are used.

    Silent when anything is not as expected: an indicator that cannot be made
    to linger is still an indicator, and a dictation should not fail over the
    window it is drawn in.
    """
    # Only the Cocoa backend hands out a real NSView. Under the offscreen
    # platform the tests run on, winId() is a number that means something else
    # entirely, and sending an Objective-C message to it is how a test run
    # turns into a crash.
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        api = _appkit()
        view = ctypes.c_void_p(int(widget.winId()))
        window = api.ask(view, api.selector(b"window"))
        if not window:
            return False
        window = ctypes.c_void_p(window)
        api.tell_bool(window, api.selector(b"setHidesOnDeactivate:"), False)
        api.tell_unsigned(window, api.selector(b"setCollectionBehavior:"),
                          BEHAVIOUR)
        # Only a panel may carry the nonactivating bit, and only a panel is
        # asked to: on anything else the message raises, and an Objective-C
        # exception through ctypes takes the process with it.
        if _is_panel(api, window):
            mask = api.ask_unsigned(window, api.selector(b"styleMask"))
            if not mask & NONACTIVATING_PANEL:
                api.tell_unsigned(window, api.selector(b"setStyleMask:"),
                                  mask | NONACTIVATING_PANEL)
        return True
    except (AttributeError, OSError, RuntimeError, ValueError):
        return False
