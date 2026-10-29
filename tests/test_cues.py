"""Accessibility audio cue patterns."""

import unittest
from unittest import mock

from PyQt6.QtWidgets import QApplication

from dikte import cues


_app = QApplication.instance() or QApplication([])


class AudioCues(unittest.TestCase):
    def test_each_state_has_a_distinguishable_pattern(self):
        self.assertEqual(cues.PATTERNS["start"], (0,))
        self.assertNotEqual(cues.PATTERNS["start"], cues.PATTERNS["stop"])
        self.assertNotEqual(cues.PATTERNS["done"], cues.PATTERNS["failed"])

    def test_disabled_cues_do_not_beep(self):
        player = cues.AudioCues()
        with mock.patch.object(QApplication, "beep") as beep:
            player.play("start")
        beep.assert_not_called()

    def test_enabled_cue_beeps_immediately(self):
        player = cues.AudioCues(True)
        with mock.patch.object(QApplication, "beep") as beep:
            player.play("start")
        beep.assert_called_once_with()

    def test_disabling_cancels_a_pending_pattern(self):
        player = cues.AudioCues(True)
        with mock.patch.object(QApplication, "beep") as beep, \
                mock.patch.object(cues.QTimer, "singleShot") as later:
            player.play("stop")
            player.set_enabled(False)
            later.call_args.args[1]()
        beep.assert_called_once_with()

