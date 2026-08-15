"""The three tray icons, drawn here for systems that have no icon theme.

Linux hands out `audio-input-microphone`, `media-record` and `view-refresh`
from whatever icon theme is installed, and Qt finds them through
QIcon.fromTheme. macOS has no such registry: fromTheme returns a null icon
there, and a null icon in the menu bar is an item you cannot see, which is the
whole of Dikte's interface gone. So the same three shapes are drawn here, and
used whenever the theme has nothing to offer.

They are drawn as template images: one colour, transparent everywhere else,
with isMask set. That is what lets macOS invert them for a dark menu bar and
grey them while the menu is open, and it is why the shapes are outlines rather
than the coloured glyphs a Linux theme would give.
"""

import pathlib
import sys

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QColor, QIcon, QLinearGradient, QPainter, QPainterPath,
                         QPen, QPixmap)

# What a Mac menu bar asks for: 22 points, at 1x and at 2x. Both are put in the
# icon rather than one being scaled, because a scaled stroke goes soft.
SIZES = (22, 44)
# Drawn in black; the mask throws the colour away and keeps the coverage, and
# on a system that does not do masks black is still the right ink for a light
# panel and readable on a dark one.
INK = QColor(0, 0, 0)


def _canvas(size):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pixmap, painter


def _paint_microphone(painter, size, ink):
    """A capsule on a stand: idle, and the application's own mark.

    The colour is a parameter because the same glyph is the tray stencil, where
    it is black and then masked, and the white one on the application icon.
    """
    unit = size / 22.0
    pen = QPen(ink, 1.6 * unit)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(ink)
    # The capsule, held away from the edges so the stroke below has room.
    painter.drawRoundedRect(
        QRectF(8.2 * unit, 3.4 * unit, 5.6 * unit, 10.4 * unit),
        2.8 * unit, 2.8 * unit,
    )
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # The arc that cradles it, and the post and foot under that.
    painter.drawArc(
        QRectF(5.4 * unit, 6.6 * unit, 11.2 * unit, 10.4 * unit),
        180 * 16, 180 * 16,
    )
    painter.drawLine(QPointF(11 * unit, 16.8 * unit), QPointF(11 * unit, 19 * unit))
    painter.drawLine(QPointF(7.6 * unit, 19 * unit), QPointF(14.4 * unit, 19 * unit))


def _microphone(painter, size):
    _paint_microphone(painter, size, INK)


def _record(painter, size):
    """A filled dot: recording, and the same red dot the overlay shows."""
    unit = size / 22.0
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(INK)
    painter.drawEllipse(QPointF(11 * unit, 11 * unit), 6.4 * unit, 6.4 * unit)


def _working(painter, size):
    """An arrow chasing its own circle: transcribing, cleaning up, thinking."""
    unit = size / 22.0
    pen = QPen(INK, 2.0 * unit)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    ring = QRectF(4.4 * unit, 4.4 * unit, 13.2 * unit, 13.2 * unit)
    # Three quarters of the way round, leaving the gap the head sits in.
    painter.drawArc(ring, 90 * 16, -280 * 16)

    # The head, as a filled triangle at the open end rather than two more
    # strokes: at 22 points a drawn arrowhead closes up into a blob.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(INK)
    head = QPainterPath()
    head.moveTo(QPointF(11.0 * unit, 1.6 * unit))
    head.lineTo(QPointF(11.0 * unit, 7.2 * unit))
    head.lineTo(QPointF(15.8 * unit, 4.4 * unit))
    head.closeSubpath()
    painter.drawPath(head)


# The names Linux themes use, which are what dikte.py asks for either way.
SHAPES = {
    "audio-input-microphone": _microphone,
    "media-record": _record,
    "view-refresh": _working,
}

_cache = {}


def icon(name):
    """The named icon drawn here, or a null QIcon when it is not one of ours.

    Cached because the tray is refreshed on every state change and every one of
    those would otherwise redraw three pixmaps. A QIcon is cheap to copy and the
    pixmaps inside it are shared, so handing the same object out is safe.
    """
    shape = SHAPES.get(name)
    if shape is None:
        return QIcon()
    if name in _cache:
        return _cache[name]

    result = QIcon()
    for size in SIZES:
        pixmap, painter = _canvas(size)
        try:
            shape(painter, size)
        finally:
            painter.end()
        result.addPixmap(pixmap)
    # The line that makes it a template image: macOS then owns the colour, and
    # the icon follows the menu bar into dark mode instead of staying black.
    result.setIsMask(True)
    _cache[name] = result
    return result


# --- the application icon --------------------------------------------------
#
# The menu bar wants a flat stencil; the Finder, the Dock and the permission
# dialogs want a picture. Same microphone, on a ground of its own, and drawn
# here as well so that `install-mac.sh` has an .icns to build without a binary
# blob living in the repository.

# What iconutil expects to find in an .iconset: each of these at 1x and 2x.
APP_ICON_SIZES = (16, 32, 128, 256, 512)


def app_pixmap(size):
    """The application icon at one size: a white microphone on a blue tile."""
    pixmap, painter = _canvas(size)
    try:
        unit = size / 22.0
        # macOS rounds and shadows the tile itself for some icon styles but not
        # for a plain .icns, so the shape is drawn: the squircle radius Apple
        # uses is close enough to 22% of the side.
        ground = QLinearGradient(0, 0, 0, size)
        ground.setColorAt(0.0, QColor(0x3B, 0x82, 0xF6))
        ground.setColorAt(1.0, QColor(0x1D, 0x4E, 0xD8))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ground)
        inset = 1.0 * unit
        painter.drawRoundedRect(
            QRectF(inset, inset, size - 2 * inset, size - 2 * inset),
            4.4 * unit, 4.4 * unit,
        )
        # The same glyph as the tray, in white and a little smaller so it sits
        # inside the tile rather than against its edges.
        painter.save()
        painter.translate(size / 2.0, size / 2.0)
        painter.scale(0.64, 0.64)
        painter.translate(-size / 2.0, -size / 2.0)
        _paint_microphone(painter, size, QColor(0xFF, 0xFF, 0xFF))
        painter.restore()
    finally:
        painter.end()
    return pixmap


def write_iconset(directory):
    """Write the PNGs `iconutil -c icns` reads. The directory it wrote to."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for size in APP_ICON_SIZES:
        for scale in (1, 2):
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            app_pixmap(size * scale).save(str(directory / name), "PNG")
    return directory


def _main(argv):
    """`python3 trayicon.py <path>.iconset`, which install-mac.sh calls.

    A QGuiApplication has to exist before a QPixmap can, and offscreen because
    this runs from a shell script with no window to open.
    """
    if len(argv) != 2:
        print("usage: trayicon.py <directory>.iconset", file=sys.stderr)
        return 2
    from PyQt6.QtGui import QGuiApplication
    QGuiApplication.setAttribute(
        Qt.ApplicationAttribute.AA_UseSoftwareOpenGL, True)
    app = QGuiApplication(["dikte-icon", "-platform", "offscreen"])
    try:
        print(write_iconset(argv[1]))
    finally:
        del app
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
