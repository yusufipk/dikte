"""What macOS has to be told, and what it will not tell you.

Every default here suits an application with a window and a Dock icon rather
than one that lives in the menu bar and appears while you are typing somewhere
else:

  * an application that has not said otherwise comes to the front when it puts
    a window on screen, taking the keyboard with it — the one thing an
    indicator must never do;
  * a utility panel is hidden while its application is not the active one,
    which is every moment the indicator is for;
  * a window belongs to the desktop its application started on;
  * and showing any window at all leaves the window that had the keyboard no
    longer holding it. Not the focus — the application stays frontmost and
    nothing moves on screen — but the key window, which is where a typed
    character actually arrives. There is no flag that prevents this: not a
    panel that cannot become key, not an accessory application, not
    WA_ShowWithoutActivating, not NSWindowStyleMaskNonactivatingPanel. So the
    keyboard is noted before and handed back after.

And one thing it lies about: told to press a key it has not been allowed to,
macOS reports that it did. Nothing fails, nothing is logged, the key reaches
nobody. Hence trusted_to_type(), asked before rather than after.

Reached through ctypes rather than PyObjC because the whole of Dikte is the
standard library and PyQt6, and this is a handful of messages.
"""

import ctypes
import ctypes.util
import sys
import time

from PyQt6.QtWidgets import QApplication

# NSApplicationActivationPolicy: in the menu bar, not in the Dock, and never
# activated by putting a window up.
NS_ACCESSORY = 1

# NSWindowCollectionBehavior: on every desktop, and beside a full-screen window
# rather than replacing it.
NS_CAN_JOIN_ALL_SPACES = 1 << 0
NS_FULL_SCREEN_AUXILIARY = 1 << 8

# CGEventFlags, which are not the numbers Carbon uses for the same keys.
CG_FLAGS = {
    "cmd": 1 << 20, "command": 1 << 20, "meta": 1 << 20, "super": 1 << 20,
    "shift": 1 << 17,
    "ctrl": 1 << 18, "control": 1 << 18,
    "alt": 1 << 19, "option": 1 << 19,
}
# kCGHIDEventTap: posted where the hardware would have, so every application
# sees it the way it sees a real key.
CG_HID_EVENT_TAP = 0

_objc = None
_appservices = None


def _services():
    """ApplicationServices, which holds both Quartz and the accessibility check."""
    global _appservices
    if _appservices is None:
        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return None
        _appservices = ctypes.cdll.LoadLibrary(path)
    return _appservices


def available():
    """Whether these calls mean anything here.

    The platform is asked for by name and not worked out from the operating
    system: winId() is an NSView under the cocoa plugin only, and offscreen —
    which is what the tests run on — hands back a handle that is not an object
    at all. Sending that a message ends the process.
    """
    return sys.platform == "darwin" and QApplication.platformName() == "cocoa"


def _runtime():
    """libobjc, loaded once, with the functions used below declared.

    Declaring them matters more than it looks: a pointer returned as a C int is
    truncated on a 64-bit build, and the class would arrive at objc_msgSend as
    a different address than the one it was given.
    """
    global _objc
    if _objc is None:
        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        lib.objc_getClass.restype = ctypes.c_void_p
        lib.objc_getClass.argtypes = [ctypes.c_char_p]
        lib.sel_registerName.restype = ctypes.c_void_p
        lib.sel_registerName.argtypes = [ctypes.c_char_p]
        _objc = lib
    return _objc


def _send(receiver, selector, restype=ctypes.c_void_p, argtypes=(), *args):
    """[receiver selector:args].

    objc_msgSend is variadic, and ctypes cannot call a variadic function on
    arm64 without being told the argument types, so every call declares its own.
    """
    objc = _runtime()
    objc.objc_msgSend.restype = restype
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
    return objc.objc_msgSend(receiver, objc.sel_registerName(selector), *args)


def _app():
    # Through _runtime() rather than the module-level name: Python evaluates
    # the arguments before the call, so reaching for the library here while
    # _send() is still the only thing that loads it is an AttributeError — one
    # the callers below would have caught and turned into a silent "no".
    return _send(_runtime().objc_getClass(b"NSApplication"), b"sharedApplication")


def _try(what, call):
    """Run one of these and say whether it worked, out loud when it did not.

    Every failure here is a window behaving as if the call had never been
    written: the indicator hides itself, or takes the keyboard with it. Both
    look like a bug in the recording rather than in three lines of ctypes, and
    a caught exception with nothing said is what makes them look that way.
    """
    try:
        call()
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        print(f"dikte: macOS refused {what} ({exc}); "
              f"the indicator may not behave", file=sys.stderr)
        return False
    return True


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
    return _try("setActivationPolicy:", lambda: _send(
        _app(), b"setActivationPolicy:", ctypes.c_bool, [ctypes.c_long],
        NS_ACCESSORY))


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

    def call():
        # A Qt that hands out something other than an NSView, or a macOS that
        # has stopped answering to these. An indicator that hides too eagerly
        # is worth less than one that stays; neither is worth a crash.
        window = _send(ctypes.c_void_p(int(widget.winId())), b"window")
        if not window:
            raise ValueError("no NSWindow behind this widget")
        _send(window, b"setHidesOnDeactivate:", None, [ctypes.c_bool], False)
        _send(window, b"setCollectionBehavior:", None, [ctypes.c_ulong],
              NS_CAN_JOIN_ALL_SPACES | NS_FULL_SCREEN_AUXILIARY)

    return _try("setHidesOnDeactivate:", call)


def trusted_to_type(ask=False):
    """Whether macOS will let this process press a key on your behalf.

    Worth asking rather than finding out: told to press a key it has not been
    allowed to, macOS answers that it did. osascript exits 0, System Events
    reports no error, and the key reaches nobody — a dictation that ends in a
    transcript nothing was ever pasted from, and no reason given anywhere.

    `ask` opens the system's own dialogue, which is the only way to the pane
    that grants it, and returns the answer as it stands rather than waiting.
    """
    if not available():
        return False
    services = _services()
    if services is None:
        return False
    try:
        if not ask:
            services.AXIsProcessTrusted.restype = ctypes.c_bool
            return bool(services.AXIsProcessTrusted())
        core = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        core.CFStringCreateWithCString.restype = ctypes.c_void_p
        core.CFStringCreateWithCString.argtypes = [ctypes.c_void_p,
                                                   ctypes.c_char_p, ctypes.c_uint32]
        core.CFDictionaryCreate.restype = ctypes.c_void_p
        core.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_long,
                                            ctypes.c_void_p, ctypes.c_void_p]
        key = ctypes.c_void_p(core.CFStringCreateWithCString(
            None, b"AXTrustedCheckOptionPrompt", 0x08000100))   # kCFStringEncodingUTF8
        true_value = ctypes.c_void_p.in_dll(core, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key)
        values = (ctypes.c_void_p * 1)(true_value)
        options = core.CFDictionaryCreate(None, keys, values, 1, None, None)
        services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        return bool(services.AXIsProcessTrustedWithOptions(options))
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        print(f"dikte: could not ask macOS about accessibility ({exc})",
              file=sys.stderr)
        return False


def press_keys(modifiers, key_code):
    """Post one key down and up, with `modifiers` held, as this process.

    Through Quartz rather than by shelling out to osascript: one fewer process
    on every dictation, and — the reason it matters — the permission belongs to
    whoever posts the event. osascript is a program of Apple's that Dikte
    happens to run, and macOS judges it by the application responsible for it,
    which is not always the one somebody has allowed in the settings.
    """
    if not available():
        return False
    services = _services()
    if services is None:
        return False
    try:
        services.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        services.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        services.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        services.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        core = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        core.CFRelease.argtypes = [ctypes.c_void_p]

        for down in (True, False):
            event = services.CGEventCreateKeyboardEvent(None, key_code, down)
            if not event:
                return False
            services.CGEventSetFlags(event, modifiers)
            services.CGEventPost(CG_HID_EVENT_TAP, event)
            core.CFRelease(event)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        print(f"dikte: could not press the key ({exc})", file=sys.stderr)
        return False
    return True


_front_before = 0


def remember_the_front():
    """Note which application has the keyboard, before the indicator goes up.

    Putting any window on screen — a panel that cannot become key, belonging
    to an accessory application, shown without activating — still leaves the
    window that had the keyboard no longer holding it. The application stays
    frontmost, so nothing looks different, but a typed character now arrives
    nowhere. Which is exactly what a dictation ends by doing.

    So who it was is written down here, and give_the_keyboard_back() hands it
    over again before the transcript is typed.
    """
    if not available():
        return 0
    global _front_before
    try:
        workspace = _send(_runtime().objc_getClass(b"NSWorkspace"),
                          b"sharedWorkspace")
        app = _send(workspace, b"frontmostApplication")
        _front_before = _send(app, b"processIdentifier", ctypes.c_int) if app else 0
    except (OSError, AttributeError, TypeError, ValueError):
        _front_before = 0
    return _front_before


def give_the_keyboard_back():
    """Make that application key again, so what is typed reaches it.

    It is already the frontmost one, so nothing moves on screen and nothing
    the user did is undone: this only restores the part macOS quietly took
    when the indicator appeared.
    """
    if not available() or not _front_before:
        return False

    def call():
        cls = _runtime().objc_getClass(b"NSRunningApplication")
        app = _send(cls, b"runningApplicationWithProcessIdentifier:",
                    ctypes.c_void_p, [ctypes.c_int], _front_before)
        if not app:
            raise ValueError("that application is gone")
        # NSApplicationActivateIgnoringOtherApps, so the window that was there
        # before the indicator gets the keyboard rather than whatever Dikte's
        # own activation would hand it to.
        _send(app, b"activateWithOptions:", ctypes.c_bool, [ctypes.c_ulong], 1 << 1)

    return _try("activateWithOptions:", call)


def type_text(text, chunk=16):
    """Send `text` to whatever has the keyboard, as if it had been typed.

    No clipboard and no Cmd+V: the characters travel in the event itself, so
    nothing anybody had copied is overwritten and no key combination has to
    mean paste in the window receiving it.

    A few characters at a time, because a single event carrying a paragraph is
    dropped by some applications. Each pair is a press and a release of the one
    key that carries a string, which is how macOS delivers text that has no key
    of its own.
    """
    if not available() or not text:
        return False
    services = _services()
    if services is None:
        return False
    try:
        services.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        services.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        services.CGEventKeyboardSetUnicodeString.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        services.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        core = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        core.CFRelease.argtypes = [ctypes.c_void_p]

        for start in range(0, len(text), chunk):
            piece = text[start:start + chunk]
            # UTF-16, which is what a UniChar is, and surrogate pairs count as
            # the two units they are rather than the one character they spell.
            units = piece.encode("utf-16-le")
            buffer = ctypes.create_string_buffer(units, len(units))
            for down in (True, False):
                event = services.CGEventCreateKeyboardEvent(None, 0, down)
                if not event:
                    return False
                services.CGEventKeyboardSetUnicodeString(
                    event, len(units) // 2, buffer)
                services.CGEventPost(CG_HID_EVENT_TAP, event)
                core.CFRelease(event)
            # Posted faster than this, a run of events arrives at some
            # applications with pieces missing: the tail of a sentence, usually,
            # which is the half nobody notices is gone.
            time.sleep(0.004)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        print(f"dikte: could not type the text ({exc})", file=sys.stderr)
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
    return _try("activateIgnoringOtherApps:", lambda: _send(
        _app(), b"activateIgnoringOtherApps:", None, [ctypes.c_bool], True))
