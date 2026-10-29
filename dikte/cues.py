"""Short, distinguishable accessibility cues for dictation state changes."""

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication


# QApplication.beep() uses the desktop's normal notification sound. Different
# short patterns make the states distinguishable without shipping sound files,
# adding a multimedia dependency, or bypassing the user's system volume.
PATTERNS = {
    "start": (0,),
    "stop": (0, 120),
    "done": (0, 120, 240),
    "failed": (0, 160),
}


class AudioCues:
    """Play one non-blocking pattern at a time when the user opted in."""

    def __init__(self, enabled=False):
        self.enabled = bool(enabled)
        self._generation = 0

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if not self.enabled:
            self._generation += 1

    def play(self, name):
        if not self.enabled or name not in PATTERNS:
            return
        app = QApplication.instance()
        if app is None:
            return
        self._generation += 1
        generation = self._generation
        delays = PATTERNS[name]

        def beep(index):
            if not self.enabled or generation != self._generation:
                return
            QApplication.beep()
            if index + 1 < len(delays):
                QTimer.singleShot(delays[index + 1],
                                   lambda: beep(index + 1))

        beep(0)
