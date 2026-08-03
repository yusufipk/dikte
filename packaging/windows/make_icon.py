"""Render the application icon into a .ico for the .exe and the installer.

Drawn from the same microphone the tray icon uses, so the taskbar, the Start
menu, the installer and the tray are one picture rather than four. Written as
an icon file here rather than carried in the repository as a binary, because
the drawing is the source and a checked-in .ico is a copy of it that nobody
would notice going stale.

The colour is fixed rather than taken from the system: this one goes on a
window frame, a Start menu tile and a setup dialogue, none of which follow the
tray's light-or-dark question.

    python packaging/windows/make_icon.py [path/to/dikte.ico]
"""

import os
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QIODevice  # noqa: E402
from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import icons  # noqa: E402

# What Windows asks for: the small ones for a list, 32 and 48 for the desktop
# and Alt-Tab, 256 for the large icon view and the installer.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# Dikte's own blue, dark enough to read on a light background and light enough
# to read on a dark one.
COLOUR = QColor(64, 132, 214)


def png_for(size):
    icon = icons.tray_icon("audio-input-microphone")
    pixmap = icon.pixmap(size, size)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


def write_ico(path, images):
    """An .ico holding PNG entries, which Windows has taken since Vista.

    Written by hand because Qt's icon writer is a plugin that may or may not
    have been shipped, and a build that silently produces no icon is worse than
    forty lines of struct.
    """
    header = struct.pack("<HHH", 0, 1, len(images))
    directory = b""
    body = b""
    offset = len(header) + 16 * len(images)
    for size, payload in images:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256
            0 if size >= 256 else size,
            0,          # no colour palette
            0,          # reserved
            1,          # colour planes
            32,         # bits per pixel
            len(payload),
            offset,
        )
        body += payload
        offset += len(payload)
    pathlib.Path(path).write_bytes(header + directory + body)


def main(argv):
    target = pathlib.Path(argv[0]) if argv else (
        pathlib.Path(__file__).with_name("dikte.ico"))
    app = QApplication(sys.argv[:1])       # noqa: F841 - QPixmap needs one
    icons.forget()
    # The tray icon asks the palette what colour to be; this one does not.
    icons.DRAWINGS["audio-input-microphone"] = (
        icons.DRAWINGS["audio-input-microphone"][0], COLOUR)
    images = [(size, png_for(size)) for size in SIZES]
    target.parent.mkdir(parents=True, exist_ok=True)
    write_ico(target, images)
    print(f"{target}: {len(images)} sizes, {target.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
