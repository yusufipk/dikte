"""Reading a global shortcut off the keyboard instead of out of a list.

Picking `Ctrl+Alt+M` from a menu of fourteen suggestions is a strange way to
choose a key press. Pressing it is the obvious one, and it is also the only way
to find out what a keyboard actually sends: a laptop with no Insert key, a
Turkish layout where the key beside L is not a semicolon, a keyboard whose Alt
is where you expected Meta.

So the settings window has a button that listens. Press it, press the
combination, and that is the shortcut. What comes back is written in the same
form the settings have always stored, `Ctrl+Alt+Space`, so nothing downstream
learns that it was typed rather than picked, and both platforms' shortcut
tables read it the way they always have.

Two things this has to be careful about.

A combination Dikte is already holding never arrives. RegisterHotKey takes the
key away from whoever has focus, and KDE's global shortcut does the same, so
pressing Ctrl+Space to record it would start a dictation and the window would
sit there waiting. The catcher says when it is listening, and the application
stands its own shortcuts down for exactly that long.

And a key press is not a shortcut until it has a key in it. Holding Ctrl sends
a key press of its own, then another for every key after it, so a modifier on
its own is waited through rather than taken.
"""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QPushButton

from i18n import t

# The order the modifiers are written in. Not alphabetical and not Qt's: this
# is the order that reproduces every combination Dikte already ships, from
# `Ctrl+Alt+Space` to `Meta+Shift+Space`, so a captured shortcut and a stored
# one that mean the same thing look the same in the box.
MODIFIERS = (
    ("Ctrl", Qt.KeyboardModifier.ControlModifier),
    ("Meta", Qt.KeyboardModifier.MetaModifier),
    ("Alt", Qt.KeyboardModifier.AltModifier),
    ("Shift", Qt.KeyboardModifier.ShiftModifier),
)

# A modifier pressed on its own arrives as a key of its own. There is no
# shortcut in it yet, so it is waited through.
BARE_MODIFIERS = {
    Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_AltGr,
    Qt.Key.Key_Meta, Qt.Key.Key_Super_L, Qt.Key.Key_Super_R,
    Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock, Qt.Key.Key_ScrollLock,
}

# Qt's key codes, in the names both platforms' shortcut tables are keyed by.
# Only the keys a global shortcut may be built from: what is not in here is
# refused out loud rather than stored as something that will never fire.
KEY_NAMES = {
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Return: "return",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "pgup",
    Qt.Key.Key_PageDown: "pgdown",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
}
KEY_NAMES.update({getattr(Qt.Key, f"Key_F{number}"): f"f{number}"
                  for number in range(1, 13)})
KEY_NAMES.update({getattr(Qt.Key, f"Key_{letter.upper()}"): letter
                  for letter in "abcdefghijklmnopqrstuvwxyz"})
KEY_NAMES.update({getattr(Qt.Key, f"Key_{digit}"): str(digit)
                  for digit in range(10)})

# How long the button listens before giving up. A capture nobody finished holds
# the keyboard, and a window that has stopped answering the keyboard with no
# sign of why is worse than one that quietly stopped waiting.
TIMEOUT_MS = 10000


def key_name(key):
    """The name a Qt key code goes under in the shortcut tables, or ''."""
    try:
        return KEY_NAMES.get(Qt.Key(key), "")
    except ValueError:
        return ""


def combination(modifiers, key):
    """'Ctrl+Alt+Space' for a key press, or '' when it is not a shortcut.

    Empty for a modifier held on its own, and for a key nothing here can name:
    both are things somebody may press on the way to what they meant, and
    neither is worth interrupting them for.
    """
    if key in BARE_MODIFIERS:
        return ""
    name = key_name(key)
    if not name:
        return ""
    held = [label for label, flag in MODIFIERS if modifiers & flag]
    return "+".join(held + [name.capitalize() if len(name) > 1 else name.upper()])


class ShortcutCatcher(QPushButton):
    """A button that, once pressed, answers with the next key combination.

    `captured` carries the combination. `listening` says when the keyboard is
    being watched, so that the application can stand its own global shortcuts
    down: the whole point is to be able to press the one Dikte already holds.
    """

    captured = pyqtSignal(str)
    listening = pyqtSignal(bool)
    refused = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(t("Capture shortcut"), parent)
        self._idle_text = t("Capture shortcut")
        self._waiting = False
        # Checkable so that "listening" is visible in the button itself rather
        # than only in its label.
        self.setCheckable(True)
        self.setToolTip(t(
            "Click here, then press the shortcut you want. Dikte temporarily "
            "releases its own shortcuts while listening, so you can capture "
            "the shortcut that is already active too."
        ))
        self.clicked.connect(self._start)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(TIMEOUT_MS)
        self._timeout.timeout.connect(self._timed_out)

    @property
    def waiting(self):
        return self._waiting

    # ---- listening -------------------------------------------------------

    def _start(self, _checked=False):
        if self._waiting:
            self._stop()
            return
        self._waiting = True
        self.setText(t("Press shortcut… (Esc cancels)"))
        self.setChecked(True)
        # Grabbed rather than only focused: a combination that is a shortcut of
        # something else in this window would otherwise be eaten by it before
        # this ever saw the key.
        self.grabKeyboard()
        self._timeout.start()
        self.listening.emit(True)

    def _stop(self):
        if not self._waiting:
            return
        self._waiting = False
        self._timeout.stop()
        self.releaseKeyboard()
        self.setText(self._idle_text)
        self.setChecked(False)
        self.listening.emit(False)

    def cancel(self):
        """Stop listening without having taken anything."""
        self._stop()

    def _timed_out(self):
        self._stop()
        self.refused.emit(t("Shortcut capture timed out. Try again."))

    # ---- the keyboard ----------------------------------------------------

    def keyPressEvent(self, event):
        if not self._waiting:
            super().keyPressEvent(event)
            return
        # Everything is consumed while listening. Space and Enter would press
        # this button, Escape would close the settings window, and Tab would
        # move the focus out from under the grab.
        event.accept()
        key = event.key()
        if (key == Qt.Key.Key_Escape
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            self._stop()
            return
        if key in BARE_MODIFIERS:
            return
        found = combination(event.modifiers(), key)
        if not found:
            # Named out loud rather than ignored: a key that does nothing twice
            # in a row looks like a button that is broken.
            self._stop()
            self.refused.emit(t(
                "That key is not supported. Use a letter, number, function "
                "key, Space or a navigation key, optionally with Ctrl, Alt, "
                "Shift or Meta."
            ))
            return
        self._stop()
        self.captured.emit(found)

    def keyReleaseEvent(self, event):
        if self._waiting:
            event.accept()
            return
        super().keyReleaseEvent(event)

    # ---- giving the keyboard back ----------------------------------------

    def focusOutEvent(self, event):
        # Clicking elsewhere is as good as saying never mind, and a grab that
        # outlived the button's focus would swallow the next thing typed.
        self._stop()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self._stop()
        super().hideEvent(event)
