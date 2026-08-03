"""Choosing a global shortcut by pressing it in the settings window."""

import unittest
from unittest import mock

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

import dikte
import keycapture


_app = QApplication.instance() or QApplication([])


class Catcher(keycapture.ShortcutCatcher):
    """The real keyboard grab is replaced; events are handed in directly."""

    def __init__(self):
        self.grabs = 0
        self.releases = 0
        super().__init__()

    def grabKeyboard(self):
        self.grabs += 1

    def releaseKeyboard(self):
        self.releases += 1


def press(key, modifiers=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers)


class Naming(unittest.TestCase):
    def test_modifiers_are_written_in_the_stored_order(self):
        held = (Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(
            keycapture.combination(held, Qt.Key.Key_M),
            "Ctrl+Alt+Shift+M",
        )

    def test_space_and_function_keys_get_their_normal_names(self):
        ctrl = Qt.KeyboardModifier.ControlModifier
        self.assertEqual(
            keycapture.combination(ctrl, Qt.Key.Key_Space), "Ctrl+Space")
        self.assertEqual(
            keycapture.combination(ctrl, Qt.Key.Key_F9), "Ctrl+F9")

    def test_a_modifier_on_its_own_is_not_a_shortcut(self):
        self.assertEqual(keycapture.combination(
            Qt.KeyboardModifier.ControlModifier, Qt.Key.Key_Control), "")

    def test_an_unsupported_key_is_not_invented(self):
        self.assertEqual(keycapture.combination(
            Qt.KeyboardModifier.ControlModifier, Qt.Key.Key_Plus), "")


class Capturing(unittest.TestCase):
    def setUp(self):
        self.catcher = Catcher()
        self.addCleanup(self.catcher.deleteLater)
        self.addCleanup(self.catcher.cancel)

    def test_clicking_starts_listening_and_a_combination_finishes_it(self):
        states, found = [], []
        self.catcher.listening.connect(states.append)
        self.catcher.captured.connect(found.append)

        self.catcher.click()
        self.assertTrue(self.catcher.waiting)
        self.assertEqual(self.catcher.grabs, 1)
        self.catcher.keyPressEvent(press(
            Qt.Key.Key_Space,
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier,
        ))

        self.assertEqual(found, ["Ctrl+Alt+Space"])
        self.assertEqual(states, [True, False])
        self.assertFalse(self.catcher.waiting)
        self.assertEqual(self.catcher.releases, 1)

    def test_bare_modifiers_are_waited_through(self):
        found = []
        self.catcher.captured.connect(found.append)
        self.catcher.click()
        self.catcher.keyPressEvent(press(
            Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier))
        self.assertTrue(self.catcher.waiting)
        self.assertEqual(found, [])

    def test_escape_alone_cancels(self):
        found = []
        self.catcher.captured.connect(found.append)
        self.catcher.click()
        self.catcher.keyPressEvent(press(Qt.Key.Key_Escape))
        self.assertFalse(self.catcher.waiting)
        self.assertEqual(found, [])

    def test_escape_with_a_modifier_can_be_a_shortcut(self):
        found = []
        self.catcher.captured.connect(found.append)
        self.catcher.click()
        self.catcher.keyPressEvent(press(
            Qt.Key.Key_Escape, Qt.KeyboardModifier.ControlModifier))
        self.assertEqual(found, ["Ctrl+Esc"])

    def test_an_unsupported_key_says_why_and_stops(self):
        refused = []
        self.catcher.refused.connect(refused.append)
        self.catcher.click()
        self.catcher.keyPressEvent(press(
            Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier))
        self.assertFalse(self.catcher.waiting)
        self.assertEqual(len(refused), 1)

    def test_the_timeout_gives_the_keyboard_back(self):
        refused = []
        self.catcher.refused.connect(refused.append)
        self.catcher.click()
        self.catcher._timed_out()
        self.assertFalse(self.catcher.waiting)
        self.assertEqual(len(refused), 1)
        self.assertEqual(self.catcher.releases, 1)


class ApplicationCoordination(unittest.TestCase):
    def app(self):
        made = object.__new__(dikte.Dikte)
        made._capturing_shortcut = False
        made._quitting = False
        made.evdev = mock.Mock()
        made._apply_shortcuts = mock.Mock()
        return made

    def test_the_global_listener_stands_down_while_a_key_is_read(self):
        app = self.app()
        app._capture_shortcut(True)
        app._capture_shortcut(True)
        app.evdev.stop.assert_called_once_with()
        self.assertTrue(app._capturing_shortcut)

    def test_the_listener_returns_when_capture_ends(self):
        app = self.app()
        app._capture_shortcut(True)
        app._capture_shortcut(False)
        app._apply_shortcuts.assert_called_once_with()
        self.assertFalse(app._capturing_shortcut)

    def test_the_listener_does_not_return_during_shutdown(self):
        app = self.app()
        app._capture_shortcut(True)
        app._quitting = True
        app._capture_shortcut(False)
        app._apply_shortcuts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
