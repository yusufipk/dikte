"""The tray icons, drawn here for the desktops that have none to hand out.

Linux has an icon theme, and Dikte has always asked it for the three names it
shows: a microphone when it is idle, a record dot while it is recording, a
refresh arrow while it is working. Windows has no such thing, since an
application ships its own, so rather than carrying three bitmaps at three sizes
each, the same three are drawn.

Drawn also means they follow the taskbar. A white microphone is invisible on a
light taskbar and a black one on a dark one, so the outline colour is taken
from the palette Qt reports for the system, while the record dot stays red and
the working arrow amber, which read on either.
"""

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

# Drawn once at a size no tray asks for, so that scaling down is all Qt has to
# do however large the display scaling is.
SIZE = 256

RECORD_RED = QColor(214, 60, 60)
WORKING_AMBER = QColor(224, 152, 46)

_cache = {}


def _foreground():
    """A colour that will be seen against whatever the tray is painted with.

    Qt reports which of the two schemes the system is set to from 6.5 onward;
    older builds land on the window-text colour of the palette, which is the
    same question asked less directly.
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return QColor(230, 230, 230)
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return QColor(236, 236, 236)
        if scheme == Qt.ColorScheme.Light:
            return QColor(40, 40, 40)
    except (AttributeError, TypeError):
        pass
    return app.palette().windowText().color()


def _canvas():
    pixmap = QPixmap(SIZE, SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Everything below is drawn in a 100 x 100 box, whatever SIZE is.
    painter.scale(SIZE / 100.0, SIZE / 100.0)
    return pixmap, painter


def _microphone(painter, colour):
    pen = QPen(colour, 7.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(colour)
    # The capsule somebody speaks into.
    painter.drawRoundedRect(QRectF(38.0, 12.0, 24.0, 46.0), 12.0, 12.0)
    # The cradle under it, drawn as the bottom half of a circle.
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(QRectF(27.0, 25.0, 46.0, 46.0), 180 * 16, 180 * 16)
    # The stem and the foot.
    painter.drawLine(QPointF(50.0, 71.0), QPointF(50.0, 84.0))
    painter.drawLine(QPointF(34.0, 87.0), QPointF(66.0, 87.0))


def _record(painter, colour):
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    painter.drawEllipse(QRectF(20.0, 20.0, 60.0, 60.0))


def _working(painter, colour):
    pen = QPen(colour, 11.0)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # Three quarters of a circle, with the arrowhead where the fourth would be.
    painter.drawArc(QRectF(20.0, 20.0, 60.0, 60.0), 90 * 16, -280 * 16)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    painter.drawPolygon(QPolygonF([
        QPointF(50.0, 4.0), QPointF(50.0, 36.0), QPointF(76.0, 20.0),
    ]))


DRAWINGS = {
    "audio-input-microphone": (_microphone, None),
    "media-record": (_record, RECORD_RED),
    "view-refresh": (_working, WORKING_AMBER),
}


def tray_icon(name):
    """One of the three, drawn. An unknown name gets the microphone."""
    draw, colour = DRAWINGS.get(name, DRAWINGS["audio-input-microphone"])
    foreground = colour or _foreground()
    key = (name, foreground.rgba())
    if key in _cache:
        return _cache[key]
    pixmap, painter = _canvas()
    try:
        draw(painter, foreground)
    finally:
        painter.end()
    icon = QIcon(pixmap)
    _cache[key] = icon
    return icon


def forget():
    """Drop the drawn icons, for when the system switches between light and dark."""
    _cache.clear()
