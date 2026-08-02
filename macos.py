"""The few things macOS has to be told, through the Objective-C runtime.

Three of them, and each is a default that suits an application with a window
and a Dock icon rather than one that lives in the menu bar:

  * an application that has not said otherwise comes to the front when it puts
    a window on screen, which for the indicator means taking the keyboard away
    from whatever was being typed into — the one thing it must never do;
  * a utility panel is hidden while its application is not the active one,
    which is every moment the indicator is for;
  * and a window belongs to the desktop its application started on.

Reached through ctypes rather than PyObjC because the whole of Dikte is the
standard library and PyQt6, and this is four messages.
"""

import ctypes
import ctypes.util
import sys

from PyQt6.QtWidgets import QApplication

# NSApplicationActivationPolicy: in the menu bar, not in the Dock, and never
# activated by putting a window up.
NS_ACCESSORY = 1

# NSWindowCollectionBehavior: on every desktop, and beside a full-screen window
# rather than replacing it.
NS_CAN_JOIN_ALL_SPACES = 1 << 0
NS_FULL_SCREEN_AUXILIARY = 1 << 8

_objc = None


def available():
    """Whether these calls mean anything here.

    The platform is asked for by name and not worked out from the operating
    system: winId() is an NSView under the cocoa plugin only, and offscreen —
    which is what the tests run on — hands back a handle that is not an object
    at all. Sending that a message ends the process.
    """
    return sys.platform == "darwin" and QApplication.platformName() == "cocoa"


def _send(receiver, selector, restype=ctypes.c_void_p, argtypes=(), *args):
    """[receiver selector:args].

    objc_msgSend is variadic, and ctypes cannot call a variadic function on
    arm64 without being told the argument types, so every call declares its own.
    """
    global _objc
    if _objc is None:
        _objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        _objc.objc_getClass.restype = ctypes.c_void_p
        _objc.objc_getClass.argtypes = [ctypes.c_char_p]
        _objc.sel_registerName.restype = ctypes.c_void_p
        _objc.sel_registerName.argtypes = [ctypes.c_char_p]
    _objc.objc_msgSend.restype = restype
    _objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
    return _objc.objc_msgSend(receiver, _objc.sel_registerName(selector), *args)


def _app():
    return _send(_objc.objc_getClass(b"NSApplication"), b"sharedApplication")


def live_in_the_menu_bar():
    """Stop the application coming to the front when it shows a window.

    Dikte is its tray icon: the indicator appears while you are typing
    somewhere else, and an application that activates to show it takes the
    keyboard with it — the recording starts, the window you were in loses
    focus, and the paste at the end goes wherever the focus went instead.

    The .app bundle says the same thing with LSUIElement, but a checkout run
    straight from the terminal has no bundle to say it in.
    """
    if not available():
        return False
    try:
        _send(_app(), b"setActivationPolicy:", ctypes.c_bool, [ctypes.c_long],
              NS_ACCESSORY)
    except (OSError, AttributeError, TypeError, ValueError):
        return False
    return True


def keep_visible_without_focus(widget):
    """Leave a window on screen while Dikte is in the background.

    Given the Tool flag, Qt asks Cocoa for a utility panel, and a utility panel
    is hidden the moment its application stops being the active one. The
    indicator is only ever wanted in that state, so it is told to stay, and to
    join whichever desktop you are on rather than the one Dikte started on.

    Nothing to do anywhere else: X11 and Wayland hand a window's visibility to
    the window manager, not to whether its application has focus.
    """
    if not available():
        return False
    try:
        window = _send(ctypes.c_void_p(int(widget.winId())), b"window")
        if not window:
            return False
        _send(window, b"setHidesOnDeactivate:", None, [ctypes.c_bool], False)
        _send(window, b"setCollectionBehavior:", None, [ctypes.c_ulong],
              NS_CAN_JOIN_ALL_SPACES | NS_FULL_SCREEN_AUXILIARY)
    except (OSError, AttributeError, TypeError, ValueError):
        # A Qt that hands out something other than an NSView, or a macOS that
        # has stopped answering to these. An indicator that hides too eagerly
        # is worth less than one that stays; neither is worth a crash.
        return False
    return True


def come_to_the_front():
    """Ask for the keyboard, for the one window that wants it.

    An accessory application is not activated by showing a window, which is the
    point of the policy above and wrong for exactly one thing: the settings
    window is opened deliberately and would otherwise come up behind whatever
    is in front of it.
    """
    if not available():
        return False
    try:
        _send(_app(), b"activateIgnoringOtherApps:", None, [ctypes.c_bool], True)
    except (OSError, AttributeError, TypeError, ValueError):
        return False
    return True
