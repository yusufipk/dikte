"""The login start, and where a window opened from the tray comes up."""

import unittest
from unittest import mock

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication, QWidget

from dikte import app

_app = QApplication.instance() or QApplication([])


class LoginStart(unittest.TestCase):
    def test_the_flag_goes_straight_to_the_application(self):
        with mock.patch.object(app.sys, "argv", ["dikte", "--autostart"]), \
                mock.patch.object(app, "run_app", return_value=0) as run, \
                mock.patch.object(app.cli, "run") as cli:
            app.main()
        run.assert_called_once_with(["autostart"])
        cli.assert_not_called()

    def test_a_running_dikte_is_left_alone(self):
        """A second login start must not open the window the first one kept
        closed: it asks nothing of the instance that holds the lock."""
        lock = mock.Mock()
        lock.tryLock.return_value = False
        with mock.patch.object(app.ipc, "instance_lock", return_value=lock), \
                mock.patch.object(app, "_hand_over") as hand_over, \
                mock.patch.object(app.ipc, "send") as send:
            self.assertEqual(app.run_app(["autostart"]), 0)
        hand_over.assert_not_called()
        send.assert_not_called()

    def test_an_instance_without_the_lock_is_left_alone_too(self):
        with mock.patch.object(app.ipc, "instance_lock", return_value=None), \
                mock.patch.object(app.ipc, "already_serving", return_value=True), \
                mock.patch.object(app.ipc, "send") as send:
            self.assertEqual(app.run_app(["autostart"]), 0)
        send.assert_not_called()

    def test_a_click_still_hands_the_running_one_the_attention(self):
        lock = mock.Mock()
        lock.tryLock.return_value = False
        with mock.patch.object(app.ipc, "instance_lock", return_value=lock), \
                mock.patch.object(app, "_hand_over") as hand_over:
            app.run_app(["home"])
        hand_over.assert_called_once_with("home")


class FakeScreen:
    def __init__(self, name, area):
        self._name, self._area = name, area

    def name(self):
        return self._name

    def availableGeometry(self):
        return QRect(self._area)


class Present(unittest.TestCase):
    def setUp(self):
        self.window = QWidget()
        self.window.resize(400, 300)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        self.left = FakeScreen("DP-1", QRect(0, 0, 1920, 1080))
        self.right = FakeScreen("DP-2", QRect(1920, 0, 2560, 1440))

    def present(self, active, current):
        with mock.patch.object(app, "active_screen", return_value=active), \
                mock.patch.object(self.window, "screen", return_value=current):
            app._present(self.window)

    def test_it_comes_up_in_the_middle_of_the_screen_being_worked_on(self):
        self.present(active=self.right, current=self.left)
        self.assertTrue(self.window.isVisible())
        centre = self.window.frameGeometry().center()
        self.assertTrue(QRect(1920, 0, 2560, 1440).contains(centre))
        self.assertLess(abs(centre.x() - (1920 + 1280)), 5)

    def test_a_window_already_on_that_screen_stays_where_it_was_put(self):
        self.window.move(100, 120)
        self.present(active=self.left, current=self.left)
        self.assertEqual(self.window.pos().x(), 100)
        self.assertEqual(self.window.pos().y(), 120)

    def test_an_open_window_left_behind_is_mapped_again(self):
        """Hidden and shown, which is what brings a window left on another
        virtual desktop over to the current one."""
        self.window.show()
        with mock.patch.object(self.window, "isActiveWindow", return_value=False), \
                mock.patch.object(self.window, "hide", wraps=self.window.hide) as hide:
            self.present(active=self.left, current=self.left)
        hide.assert_called_once()
        self.assertTrue(self.window.isVisible())

    def test_a_minimised_window_comes_back_up(self):
        self.window.showMinimized()
        self.present(active=self.left, current=self.left)
        self.assertFalse(self.window.isMinimized())

    def test_mac_space_policy_is_applied_before_restore_and_show(self):
        window = mock.Mock()
        window.isVisible.return_value = False
        window.isMinimized.return_value = True
        window.windowState.return_value = app.Qt.WindowState.WindowMinimized
        calls = []
        window.setWindowState.side_effect = lambda state: calls.append('restore')
        window.show.side_effect = lambda: calls.append('show')
        with mock.patch.object(app.sys, 'platform', 'darwin'), \
                mock.patch.object(app, 'active_screen', return_value=None), \
                mock.patch.object(app.mac_window, 'move_to_active_space',
                                  side_effect=lambda widget: calls.append('policy')):
            app._present(window)
        self.assertEqual(calls, ['policy', 'restore', 'show'])

    def test_the_active_window_is_not_blinked(self):
        self.window.show()
        with mock.patch.object(self.window, "isActiveWindow", return_value=True), \
                mock.patch.object(self.window, "hide") as hide:
            self.present(active=self.left, current=self.left)
        hide.assert_not_called()


if __name__ == "__main__":
    unittest.main()
