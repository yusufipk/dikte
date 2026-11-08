"""Small building blocks shared by the home window and Settings.

Glyphs are drawn here rather than taken from the desktop's icon theme, which
differs on every system and is missing entirely on Windows and macOS. Every
colour comes from the active theme (theme.palette), so the same code serves
Nord, Dracula, Classic dark and Classic light.
"""

import datetime

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen,
                         QPixmap, QTextOption)
from PyQt6.QtWidgets import (
    QBoxLayout, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QPlainTextEdit,
    QScrollArea, QTextEdit,
    QSizePolicy, QSplitter, QStyle, QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget, QWidgetItem,
)

from . import theme
from .i18n import t

# Item data roles the row delegate reads. The display text stays on the item
# as well, for accessibility and for anything that reads item.text().
META_ROLE = Qt.ItemDataRole.UserRole + 11
BODY_ROLE = Qt.ItemDataRole.UserRole + 12
KIND_ROLE = Qt.ItemDataRole.UserRole + 13
# A letter drawn in the badge instead of the KIND_ROLE glyph (a profile's
# initial), and a check mark at the row's right end (the profile in use).
LETTER_ROLE = Qt.ItemDataRole.UserRole + 14
CHECK_ROLE = Qt.ItemDataRole.UserRole + 15

# ---- the layout system ------------------------------------------------------
# One set of measures for every window, so a card, a page and a form start
# their text at the same place wherever they appear.

# The widest a home tab grows; the tab bar above it is capped the same.
PAGE_MAX = 1180
# The longest line of running text (a transcript, minutes, an answer).
READING_MAX = 760
# The inside of every card: its heading, its body text and its buttons all
# start this far from the card's edge.
CARD_PAD = 16
# What a read-only text box inside a card takes before its first letter (its
# focus border and padding). The card's own margin is reduced by it, so the
# letters still start at CARD_PAD.
EDITOR_INSET = 8
# The one short width of a form, for its short selects (language, corner,
# theme, a key) and its number boxes alike; model names, devices and free
# text take the full width.
SELECT_WIDTH = 260
# A form's label column: as wide as its longest label up to this, wrapping
# past it, worked out once per page.
LABEL_MAX = 200
# Between the parts of a meta line ("Today 10:44 · 18 s · Team chat").
META_SEP = "  ·  "
# The home tabs' spacing scale, so every row of controls breathes the same:
# between chips, between buttons, and between a row of chips or lines and
# the buttons under it.
CHIP_GAP = 8
BUTTON_GAP = 10
GROUP_GAP = 14
# A page's primary action is never narrower than this.
PRIMARY_MIN = 140


def roomy(*buttons):
    """Give buttons the home tabs' horizontal padding (theme: roomy)."""
    for button in buttons:
        button.setProperty("roomy", True)
        button.style().unpolish(button)
        button.style().polish(button)


def meta_line(*parts):
    """The parts of a meta line that say something, joined the one way every
    list, card and reader joins them."""
    return META_SEP.join(str(part).strip() for part in parts if part and str(part).strip())


def colors_for(widget):
    """The palette of the theme the widget's window was given."""
    name = None
    while widget is not None and not name:
        name = widget.property("dikte_theme")
        widget = widget.parentWidget()
    return theme.palette(name or theme.DEFAULT)


# ---- glyphs ----------------------------------------------------------------

def _draw(painter, name, color):
    """Draw glyph `name` on a 24 x 24 grid."""
    pen = QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    p = painter
    if name == "general":
        for y, knob in ((6, 15), (12, 8), (18, 13)):
            p.drawLine(QPointF(4, y), QPointF(20, y))
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF(knob, y), 2.2, 2.2)
            p.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "display":
        p.drawRoundedRect(QRectF(3, 4, 18, 12), 2, 2)
        p.drawLine(QPointF(12, 16), QPointF(12, 20))
        p.drawLine(QPointF(8, 20), QPointF(16, 20))
    elif name == "key":
        p.drawEllipse(QPointF(8, 12), 4, 4)
        p.drawLine(QPointF(12, 12), QPointF(21, 12))
        p.drawLine(QPointF(17, 12), QPointF(17, 15.5))
        p.drawLine(QPointF(20, 12), QPointF(20, 15))
    elif name == "edit":
        path = QPainterPath(QPointF(5, 19))
        path.lineTo(5.6, 15.4)
        path.lineTo(15.5, 5.5)
        path.lineTo(18.5, 8.5)
        path.lineTo(8.6, 18.4)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(13.5, 7.5), QPointF(16.5, 10.5))
    elif name == "profile":
        p.drawEllipse(QPointF(12, 8.5), 3.6, 3.6)
        p.drawArc(QRectF(5, 14, 14, 12), 0, 180 * 16)
    elif name == "sparkle":
        path = QPainterPath(QPointF(12, 3))
        path.quadTo(13.2, 10.8, 21, 12)
        path.quadTo(13.2, 13.2, 12, 21)
        path.quadTo(10.8, 13.2, 3, 12)
        path.quadTo(10.8, 10.8, 12, 3)
        p.setBrush(QColor(color))
        p.drawPath(path)
    elif name == "meeting":
        p.drawEllipse(QPointF(8.5, 8.5), 3, 3)
        p.drawEllipse(QPointF(16, 9.5), 2.6, 2.6)
        p.drawArc(QRectF(2.5, 14, 12, 10), 0, 180 * 16)
        p.drawArc(QRectF(11.5, 15, 10, 8), 20 * 16, 160 * 16)
    elif name == "keyboard":
        p.drawRoundedRect(QRectF(2.5, 6, 19, 12), 2, 2)
        p.setBrush(QColor(color))
        for x in (6.5, 10, 13.5, 17):
            p.drawEllipse(QPointF(x, 10), 0.8, 0.8)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(8, 14.2), QPointF(16, 14.2))
    elif name == "mic":
        p.drawRoundedRect(QRectF(9, 3, 6, 11), 3, 3)
        p.drawArc(QRectF(5.5, 6.5, 13, 11), 200 * 16, 140 * 16)
        p.drawLine(QPointF(12, 17.5), QPointF(12, 21))
    elif name == "globe":
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.drawEllipse(QPointF(12, 12), 3.8, 8.5)
        p.drawLine(QPointF(3.5, 12), QPointF(20.5, 12))
    elif name in ("failed", "close"):
        if name == "failed":
            p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
            a, b = 8.6, 15.4
        else:
            a, b = 7, 17
        p.drawLine(QPointF(a, a), QPointF(b, b))
        p.drawLine(QPointF(b, a), QPointF(a, b))
    elif name == "pause":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(7, 5.5, 3.4, 13), 1.2, 1.2)
        p.drawRoundedRect(QRectF(13.6, 5.5, 3.4, 13), 1.2, 1.2)
    elif name == "play":
        path = QPainterPath(QPointF(8, 5.5))
        path.lineTo(18.5, 12)
        path.lineTo(8, 18.5)
        path.closeSubpath()
        p.setBrush(QColor(color))
        p.drawPath(path)
    elif name == "trash":
        p.drawLine(QPointF(4.5, 7), QPointF(19.5, 7))
        p.drawLine(QPointF(9.5, 7), QPointF(10.2, 4.2))
        p.drawLine(QPointF(10.2, 4.2), QPointF(13.8, 4.2))
        p.drawLine(QPointF(13.8, 4.2), QPointF(14.5, 7))
        path = QPainterPath(QPointF(6.5, 7))
        path.lineTo(7.6, 19.5)
        path.lineTo(16.4, 19.5)
        path.lineTo(17.5, 7)
        p.drawPath(path)
    elif name == "history":
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.drawLine(QPointF(12, 7), QPointF(12, 12))
        p.drawLine(QPointF(12, 12), QPointF(15.5, 14))
    elif name == "search":
        p.drawEllipse(QPointF(10.5, 10.5), 6, 6)
        p.drawLine(QPointF(15, 15), QPointF(20, 20))
    elif name == "more":
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        for x in (6, 12, 18):
            p.drawEllipse(QPointF(x, 12), 1.8, 1.8)
    elif name == "info":
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.drawLine(QPointF(12, 11), QPointF(12, 16.5))
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(12, 7.6), 1.2, 1.2)
    elif name == "doc":
        path = QPainterPath(QPointF(6, 3.5))
        path.lineTo(14, 3.5)
        path.lineTo(18.5, 8)
        path.lineTo(18.5, 20.5)
        path.lineTo(6, 20.5)
        path.closeSubpath()
        p.drawPath(path)
        for y in (11, 14.5, 18):
            p.drawLine(QPointF(9, y), QPointF(15.5, y))
    elif name == "pick":
        p.drawEllipse(QPointF(12, 12), 6.5, 6.5)
        for a, b in (((12, 2.5), (12, 6)), ((12, 18), (12, 21.5)),
                     ((2.5, 12), (6, 12)), ((18, 12), (21.5, 12))):
            p.drawLine(QPointF(*a), QPointF(*b))
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(12, 12), 1.4, 1.4)
    elif name == "chip":
        # A processor: what "on this computer" runs on.
        p.drawRoundedRect(QRectF(6.5, 6.5, 11, 11), 2, 2)
        p.drawRoundedRect(QRectF(9.5, 9.5, 5, 5), 1, 1)
        for at in (9.5, 14.5):
            p.drawLine(QPointF(at, 3), QPointF(at, 6.5))
            p.drawLine(QPointF(at, 17.5), QPointF(at, 21))
            p.drawLine(QPointF(3, at), QPointF(6.5, at))
            p.drawLine(QPointF(17.5, at), QPointF(21, at))
    elif name == "chevron":
        p.drawPolyline(QPointF(9.5, 6), QPointF(15.5, 12), QPointF(9.5, 18))
    elif name == "check":
        p.drawPolyline(QPointF(5, 12.5), QPointF(10, 17.5), QPointF(19, 7))
    elif name == "copy":
        p.drawRoundedRect(QRectF(8.5, 8.5, 11.5, 12), 2, 2)
        path = QPainterPath(QPointF(15.5, 8.5))
        path.lineTo(15.5, 5.5)
        path.quadTo(15.5, 4, 14, 4)
        path.lineTo(5.5, 4)
        path.quadTo(4, 4, 4, 5.5)
        path.lineTo(4, 15)
        path.quadTo(4, 16.5, 5.5, 16.5)
        path.lineTo(8.5, 16.5)
        p.drawPath(path)
    elif name == "retry":
        p.drawArc(QRectF(4.5, 4.5, 15, 15), 60 * 16, 280 * 16)
        path = QPainterPath(QPointF(16.2, 2.8))
        path.lineTo(16.2, 6.8)
        path.lineTo(12.2, 6.8)
        p.drawPath(path)
    elif name == "plus":
        p.drawLine(QPointF(12, 5), QPointF(12, 19))
        p.drawLine(QPointF(5, 12), QPointF(19, 12))
    elif name == "warning":
        path = QPainterPath(QPointF(12, 3.5))
        path.lineTo(21, 19.5)
        path.lineTo(3, 19.5)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(12, 9.5), QPointF(12, 14))
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(12, 16.8), 1.1, 1.1)


def glyph_pixmap(name, color, size=20, ratio=2.0, dot=None, aside=False):
    """Glyph `name` as a pixmap; `dot` adds a small filled circle of that
    colour in the top right corner. With `aside` the pixmap is wider than the
    glyph and the dot sits beside it rather than on it (an unsaved page in the
    sidebar), so it never covers a stroke; every icon of a list drawn this way
    has the same width, dot or not, and their labels stay in line."""
    width = size + round(size * 0.45) if aside else size
    pixmap = QPixmap(int(width * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24, size / 24)
    _draw(painter, name, color)
    if dot:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(dot))
        if aside:
            painter.drawEllipse(QPointF(24 * width / size - 4.5, 5), 3.2, 3.2)
        else:
            painter.drawEllipse(QPointF(20.5, 3.5), 3.5, 3.5)
    painter.end()
    return pixmap


def glyph(name, color, size=20, dot=None, aside=False):
    icon = QIcon()
    for ratio in (1.0, 2.0, 3.0):
        icon.addPixmap(glyph_pixmap(name, color, size, ratio, dot, aside))
    return icon


def letter_pixmap(letter, color, size=16, ratio=2.0, font=None):
    """A single letter drawn like a glyph: a profile's initial in its badge."""
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    face = QFont(font) if font is not None else QFont()
    face.setBold(True)
    face.setPixelSize(round(size * 0.82))
    painter.setFont(face)
    painter.setPen(QColor(color))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, letter[:1].upper())
    painter.end()
    return pixmap


def mix(a, b, amount):
    """Colour `a` moved `amount` of the way towards `b`, as #RRGGBB."""
    a, b = QColor(a), QColor(b)
    return QColor(round(a.red() + (b.red() - a.red()) * amount),
                  round(a.green() + (b.green() - a.green()) * amount),
                  round(a.blue() + (b.blue() - a.blue()) * amount)).name()


# ---- small widgets ----------------------------------------------------------

class GlyphLabel(QLabel):
    """A glyph in a label, recoloured whenever the theme changes."""

    def __init__(self, name, role="muted", size=18, parent=None):
        super().__init__(parent)
        self.name, self.role, self.size_ = name, role, size
        self.setFixedSize(size + 2, size + 2)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.restyle()

    def restyle(self):
        self.setPixmap(glyph_pixmap(self.name, colors_for(self)[self.role], self.size_))

    def showEvent(self, event):
        self.restyle()
        super().showEvent(event)


class GlyphButton(QToolButton):
    """An icon-only tool button whose glyph follows the theme. The words are
    its tooltip and its accessible name."""

    def __init__(self, name, text, role="text", parent=None):
        super().__init__(parent)
        self.name, self.role = name, role
        self.setObjectName("tool")
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.setIconSize(QSize(16, 16))
        self.restyle()

    def restyle(self):
        self.setIcon(glyph(self.name, colors_for(self)[self.role], 16))

    def showEvent(self, event):
        self.restyle()
        super().showEvent(event)


class InfoIcon(GlyphButton):
    """An (i) beside a control with the explanation in its tooltip. It takes
    the keyboard focus like any button: hovering shows the text, and so do a
    click, Enter and Space; Escape puts it away. `topic` names what it
    explains, for a screen reader ("More about Cleanup instructions")."""

    def __init__(self, tooltip, parent=None, topic=""):
        super().__init__("info", tooltip, "muted", parent)
        self.setObjectName("info")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setAutoRaise(True)
        self.topic = topic
        self.set_text(tooltip)
        self.clicked.connect(self.reveal)

    def set_text(self, tooltip):
        self.setToolTip(tooltip)
        self.setAccessibleName(t("More about {topic}", topic=self.topic) if self.topic
                               else t("More information"))
        self.setAccessibleDescription(tooltip)

    def reveal(self):
        from PyQt6.QtWidgets import QToolTip
        QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()), self.toolTip(), self)

    def keyPressEvent(self, event):
        from PyQt6.QtWidgets import QToolTip
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.reveal()
            return
        if event.key() == Qt.Key.Key_Escape and QToolTip.isVisible():
            QToolTip.hideText()
            return
        super().keyPressEvent(event)


class Badge(QLabel):
    """An icon in a small tinted circle, for icon tiles and list rows."""

    def __init__(self, name, role="accent", size=36, parent=None):
        super().__init__(parent)
        self.name, self.role = name, role
        self.setFixedSize(size, size)
        self.setObjectName("badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.restyle()

    def restyle(self):
        colors = colors_for(self)
        tone = {"danger": colors["danger"], "warning": colors["warn"]}.get(
            self.role, colors["accent"])
        self.setStyleSheet(
            f"QLabel#badge {{ background: {mix(colors['surface'], tone, 0.22)}; "
            f"border-radius: {self.width() // 2}px; }}")
        self.setPixmap(glyph_pixmap(self.name, tone, round(self.width() * 0.5)))

    def showEvent(self, event):
        self.restyle()
        super().showEvent(event)


class Notice(QFrame):
    """A one-line notice strip: an icon in a tinted badge, a short text, and
    the details in the tooltip. `tone` is "warning" or "danger"."""

    def __init__(self, text="", tooltip="", tone="warning", parent=None):
        super().__init__(parent)
        self.tone = tone
        self.setObjectName("noticeDanger" if tone == "danger" else "notice")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 5, 10, 5)
        row.setSpacing(8)
        self.badge = Badge("failed" if tone == "danger" else "warning", tone, 24)
        row.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.actions = row
        self.set(text, tooltip)

    def set(self, text, tooltip=""):
        self.label.setText(text)
        self.setToolTip(tooltip)
        self.label.setToolTip(tooltip)
        self.setAccessibleName(text)
        self.setVisible(bool(text))

    def text(self):
        return self.label.text()

    def add_action(self, button):
        """A button at the right end of the strip."""
        self.actions.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return button


class StatusLine(QWidget):
    """A status under a control: a plain muted line, a notice strip, or both.

    `text()` is everything it says in full, the details a notice keeps in its
    tooltip included, so a reader of the state (or a test) never has to know
    which half a sentence went to.
    """

    def __init__(self, parent=None, muted=True):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.line = QLabel()
        self.line.setTextFormat(Qt.TextFormat.PlainText)
        self.line.setWordWrap(True)
        if muted:
            self.line.setObjectName("muted")
        column.addWidget(self.line)
        self.notice = Notice()
        column.addWidget(self.notice)
        self._full = ""
        self.set()

    def set(self, line="", warning="", detail="", tone="warning"):
        """`line` as plain text; `warning` in a notice strip, `detail` in its
        tooltip."""
        self.line.setText(line)
        self.line.setVisible(bool(line))
        if self.notice.tone != tone:
            self.notice.tone = tone
            self.notice.setObjectName("noticeDanger" if tone == "danger" else "notice")
            self.notice.badge.name = "failed" if tone == "danger" else "warning"
            self.notice.badge.role = tone
            self.notice.badge.restyle()
            self.notice.style().unpolish(self.notice)
            self.notice.style().polish(self.notice)
        self.notice.set(warning, detail or warning)
        self._full = "\n".join(part for part in (line, detail or warning) if part)
        self.setVisible(bool(line or warning))

    def setText(self, text):
        self.set(text)

    def warn(self, text, detail=""):
        self.set("", text, detail)

    def text(self):
        return self._full


class FlowLayout(QLayout):
    """Widgets side by side that move on to the next line when the width runs
    out, so a large font or a long translation wraps a row of buttons rather
    than cutting their labels short."""

    def __init__(self, parent=None, spacing=6, align_right=False):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        # Result actions sit at the right end of their card, in every card.
        self.align_right = align_right
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def insertWidget(self, index, widget):
        """As QBoxLayout's, for callers that add a button of their own."""
        self.addChildWidget(widget)
        self._items.insert(max(0, min(index, len(self._items))), QWidgetItem(widget))
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        # As wide as the widest button: narrower than that, one would be cut.
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.sizeHint())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _arrange(self, rect, move):
        """Lay the items out in `rect`, or only measure; the height used."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        lines, line, x = [], [], area.x()
        for item in self._items:
            if item.isEmpty():
                continue  # a hidden button takes no room
            size = item.sizeHint()
            if line and x + size.width() > area.right() + 1:
                lines.append(line)
                line, x = [], area.x()
            line.append((item, size))
            x += size.width() + self._gap
        if line:
            lines.append(line)
        y = area.y()
        for index, line in enumerate(lines):
            height = max(size.height() for _item, size in line)
            used = sum(size.width() for _item, size in line) + self._gap * (len(line) - 1)
            x = area.x() + (max(0, area.width() - used) if self.align_right else 0)
            if move:
                for item, size in line:
                    item.setGeometry(QRect(x, y, size.width(), size.height()))
                    x += size.width() + self._gap
            y += height + (self._gap if index < len(lines) - 1 else 0)
        return y - rect.y() + margins.bottom()


# The space above a section's heading. With the 4 px between a page's blocks
# it makes one 24 px gap between every two sections on every page.
SECTION_GAP = 20
# Between the rows of a section's form.
ROW_GAP = 10


def section(title):
    """A titled block of form rows: a bold heading above a framed form.
    Returns the block (to add, show and hide) and its form. Forms nested in
    the block (an Advanced disclosure) are listed on `box.subforms`, so their
    labels can share the block's label column."""
    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, SECTION_GAP, 0, 0)
    column.setSpacing(6)
    heading = QLabel(title)
    heading.setObjectName("section")
    column.addWidget(heading)
    frame = QFrame()
    frame.setObjectName("sectionbox")
    # Beside a taller neighbour (Columns) the card takes the extra height and
    # its rows stay at the top; elsewhere nothing gives it more than it asks.
    column.addWidget(frame, 1)
    # The rows over a stretch, so that height goes below them rather than
    # into the gaps between them. The form is a widget's own layout, not one
    # nested in the card's: a nested layout is not told when a row in it is
    # shown or hidden, and would go on reporting its old height.
    inside = QVBoxLayout(frame)
    inside.setContentsMargins(CARD_PAD, 12, CARD_PAD, 12)
    inside.setSpacing(0)
    rows = QWidget()
    form = QFormLayout(rows)
    inside.addWidget(rows)
    inside.addStretch(1)
    box.form = form
    box.frame = frame
    box.heading = heading
    box.subforms = []
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setContentsMargins(0, 0, 0, 0)
    form.setVerticalSpacing(ROW_GAP)
    form.setHorizontalSpacing(14)
    return box, form


def subform(box):
    """A form to nest inside `box`'s form (in a disclosure), laid out like it."""
    holder = QWidget()
    form = QFormLayout(holder)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setContentsMargins(0, 0, 0, 0)
    form.setVerticalSpacing(ROW_GAP)
    form.setHorizontalSpacing(14)
    box.subforms.append(form)
    return holder, form


def available_width(widget):
    """The width `widget` may take, read from the scroll area it sits in.

    A box that chooses its own layout from its own width locks itself in:
    once two columns make the page wider than the window, the page stays that
    wide and the box keeps measuring two columns' worth. The viewport is what
    the window actually has; the page margins around `widget` come off it."""
    parent = widget.parentWidget()
    area = None
    while parent is not None:
        if isinstance(parent, QScrollArea):
            area = parent
            break
        parent = parent.parentWidget()
    if area is None or area.widget() is None:
        return widget.width()
    page = area.widget()
    room = min(area.viewport().width(), page.maximumWidth())
    return room - max(0, page.width() - widget.width())


class _Responsive(QWidget):
    """A box that picks its arrangement from the room its scroll area has,
    and again whenever that room changes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._viewport = None
        self.wide = None

    def _fits(self, width):
        raise NotImplementedError

    def _arrange(self, wide):
        raise NotImplementedError

    def _update(self):
        self._arrange(self._fits(available_width(self)))

    def _watch(self):
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        viewport = parent.viewport() if parent is not None else None
        if viewport is not self._viewport:
            if self._viewport is not None:
                self._viewport.removeEventFilter(self)
            self._viewport = viewport
            if viewport is not None:
                viewport.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self._viewport and event.type() == QEvent.Type.Resize:
            self._update()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        self._watch()
        self._update()
        super().showEvent(event)

    def resizeEvent(self, event):
        self._watch()
        self._update()
        super().resizeEvent(event)


class Columns(_Responsive):
    """Sections in a two-column grid on a wide page and one column on a
    narrow one.

    `rows` is a list of tuples. A pair `(left, right)` puts two cells side by
    side in one row: their headings and cards start at the same top and both
    cards stretch to the taller one's height, so no column ends ragged. A
    single `(cell,)` spans the whole width. A cell is a widget or a list of
    widgets stacked in it (the ones shown take the cell's height between
    them, for a set of boxes of which one is visible at a time).

    Two columns only when the room is past `threshold` and both columns fit
    at their minimum width; a longer translation stacks rather than running
    off the edge. Narrow, the cells follow `order` when given, and otherwise
    run down the left cells of each run of pairs and then their right cells,
    so the reading order is the one the page had as two plain columns.
    """

    def __init__(self, rows, threshold=860, spacing=22, order=None, parent=None):
        super().__init__(parent)
        self.threshold = threshold
        self.spacing = spacing
        # Called with True or False whenever the arrangement changes, for a
        # part that wants to be taller beside its neighbour than above it.
        self.on_arrange = None
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        # Sections bring their own gap above them, so rows need only a hair.
        self.grid.setVerticalSpacing(4)
        self.rows = [tuple(self._cell(item) for item in row) for row in rows]
        cells = {id(item): cell for row, wrapped in zip(rows, self.rows)
                 for item, cell in zip(row, wrapped)}
        if order is not None:
            self.order = [cells[id(item)] for item in order]
        else:
            self.order, lefts, rights = [], [], []
            for row in self.rows + [()]:
                if len(row) == 2:
                    lefts.append(row[0])
                    rights.append(row[1])
                    continue
                self.order += lefts + rights + list(row)
                lefts, rights = [], []
        self._arrange(False)

    @staticmethod
    def _cell(item):
        if not isinstance(item, (list, tuple)):
            return item
        cell = QWidget()
        column = QVBoxLayout(cell)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        for widget in item:
            column.addWidget(widget, 1)
        return cell

    def _fits(self, width):
        def widest(cells):
            return max((cell.minimumSizeHint().width() for cell in cells
                        if not cell.isHidden()), default=0)
        pairs = [row for row in self.rows if len(row) == 2]
        need = widest([row[0] for row in pairs]) + widest([row[1] for row in pairs])
        need = max(need + self.spacing,
                   widest([row[0] for row in self.rows if len(row) == 1]))
        return width >= self.threshold and width >= need

    def _arrange(self, wide):
        if wide == self.wide:
            return
        self.wide = wide
        while self.grid.count():
            self.grid.takeAt(0)
        if wide:
            for index, row in enumerate(self.rows):
                if len(row) == 2:
                    self.grid.addWidget(row[0], index, 0)
                    self.grid.addWidget(row[1], index, 1)
                else:
                    self.grid.addWidget(row[0], index, 0, 1, 2)
        else:
            for index, cell in enumerate(self.order):
                self.grid.addWidget(cell, index, 0)
        # Equal halves side by side; stacked, the one column takes it all.
        self.grid.setHorizontalSpacing(self.spacing if wide else 0)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1 if wide else 0)
        if self.on_arrange is not None:
            self.on_arrange(wide)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")


def short_time(stamp, now=None):
    """"2026-11-08 14:32:05" the way a list says it: "14:32" today,
    "Yesterday", "3 Nov" this year and "3 Nov 2025" before that. Anything it
    cannot read is returned as it came."""
    try:
        when = datetime.datetime.fromisoformat(str(stamp).strip())
    except ValueError:
        return str(stamp)
    now = now or datetime.datetime.now()
    if when.date() == now.date():
        return when.strftime("%H:%M")
    if when.date() == now.date() - datetime.timedelta(days=1):
        return t("Yesterday")
    day = t("{day} {month}", day=when.day, month=t(_MONTHS[when.month - 1]))
    return day if when.year == now.year else f"{day} {when.year}"


def day_time(stamp, now=None):
    """The day in words and the time to the minute, for a reader that has the
    room: "Today 10:44", "Yesterday 09:12", "3 Nov 18:05"."""
    try:
        when = datetime.datetime.fromisoformat(str(stamp).strip())
    except ValueError:
        return str(stamp)
    now = now or datetime.datetime.now()
    clock = when.strftime("%H:%M")
    if when.date() == now.date():
        return t("Today {time}", time=clock)
    if when.date() == now.date() - datetime.timedelta(days=1):
        return t("Yesterday {time}", time=clock)
    return f"{short_time(stamp, now)} {clock}"


class Chip(QLabel):
    """The text of a chip, elided in the middle past a maximum width."""

    def __init__(self, text="", tooltip="", parent=None, max_width=150):
        super().__init__(parent)
        self.max_width = max_width
        self.full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set(text, tooltip)

    def set(self, text, tooltip=""):
        self.full = text
        self.setToolTip(tooltip or text)
        self.setAccessibleName(text)
        self._render()

    def _render(self):
        self.setText(self.fontMetrics().elidedText(
            self.full, Qt.TextElideMode.ElideMiddle, self.max_width))

    def showEvent(self, event):
        self._render()
        super().showEvent(event)


class ChipRow(QWidget):
    """A centred row of glyph + chip pairs. `set_chips` takes
    [(glyph, text, tooltip)] or [(glyph, text, tooltip, callback)], and the
    row's own tooltip holds the details. A chip with a callback is a small
    text button, not a pill: a status and an action must not look alike."""

    def __init__(self, parent=None, align=Qt.AlignmentFlag.AlignHCenter, max_width=150):
        super().__init__(parent)
        self.max_width = max_width
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(CHIP_GAP)
        self.align = align
        self._spec = None
        self.chips = []

    def set_align(self, align):
        """Line the chips up at the left or centre them, now and from now on."""
        if align == self.align:
            return
        self.align = align
        spec, self._spec = self._spec, None
        if spec is not None:
            self.set_chips(list(spec[0]), spec[1])

    def set_chips(self, chips, tooltip=""):
        self.setToolTip(tooltip)
        spec = (tuple(chips), tooltip)
        if spec == self._spec:
            return
        self._spec = spec
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            if item.widget():
                # Hidden now, deleted later: deleteLater alone leaves the old
                # chips drawn beside the new ones until the event loop runs.
                item.widget().hide()
                item.widget().deleteLater()
        self.chips = []
        if self.align != Qt.AlignmentFlag.AlignLeft:
            self.layout_.addStretch(1)
        for name, text, tip, *callback in chips:
            if callback and callback[0] is not None:
                button = QToolButton()
                button.setObjectName("chiplink")
                button.setText(text)
                button.setToolTip(tip or tooltip)
                button.setAccessibleName(text)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                if name:
                    button.setIcon(glyph(name, colors_for(self)["accent"], 14))
                button.clicked.connect(callback[0])
                button.full = text
                self.layout_.addWidget(button)
                self.chips.append(button)
                continue
            holder = QFrame()
            holder.setObjectName("chipbox")
            row = QHBoxLayout(holder)
            row.setContentsMargins(8, 2, 10, 2)
            row.setSpacing(5)
            if name:
                row.addWidget(GlyphLabel(name, "muted", 14))
            chip = Chip(text, tip or tooltip, max_width=self.max_width)
            chip.setObjectName("chiptext")
            row.addWidget(chip)
            holder.setToolTip(tip or tooltip)
            self.layout_.addWidget(holder)
            self.chips.append(chip)
        self.layout_.addStretch(1)

    def text(self):
        """What the chips say, with their tooltips, for tests and
        accessibility."""
        return "  ".join(f"{chip.full} ({chip.toolTip()})" for chip in self.chips)


def relayout(widget):
    """Make every layout from `widget` up to its window measure again.

    Showing or hiding part of a card changes the card's height, but a parent
    that cached its children's size hints (a QFormLayout row, a column inside
    a scroll area) can go on placing its siblings as before until something
    resizes the window. Invalidating the chain makes the next layout pass
    start from the new heights."""
    while widget is not None:
        layout = widget.layout()
        if layout is not None:
            layout.invalidate()
        widget.updateGeometry()
        if widget.isWindow():
            break
        widget = widget.parentWidget()


class Disclosure(QWidget):
    """A collapsed section: a toggle with an arrow, and what it opens."""

    toggled = pyqtSignal(bool)

    def __init__(self, title, content, expanded=False, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.button = QToolButton()
        self.button.setObjectName("disclosure")
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setAutoRaise(True)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.button.toggled.connect(self.set_expanded)
        layout.addWidget(self.button)
        self.content = content
        layout.addWidget(content)
        self.set_expanded(expanded)

    def set_expanded(self, expanded):
        if self.button.isChecked() != expanded:
            self.button.setChecked(expanded)
            return
        self.button.setArrowType(Qt.ArrowType.DownArrow if expanded
                                 else Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)
        relayout(self)
        self.toggled.emit(expanded)

    def expanded(self):
        return self.button.isChecked()


# How the user left each stacked list and reader, as the list's share of the
# height, for as long as the application runs.
_SHARES = {}


def _chrome(view):
    """What a list's frame and padding take of its height."""
    if view.isVisible() and view.height() > view.viewport().height():
        return view.height() - view.viewport().height() + 2
    return view.frameWidth() * 2 + 12


def whole_rows(view, height):
    """The tallest height up to `height` that shows `view`'s rows from the
    top without cutting one in half, its frame included. A list that is
    empty, or whose rows all fit, keeps the height it was given."""
    frame = _chrome(view)
    used = frame
    for row in range(view.count()):
        if view.isRowHidden(row):
            continue
        step = view.sizeHintForRow(row)
        if used + step > height:
            return used if used > frame else height
        used += step
    return height


def rows_height(view, rows):
    """The height of the first `rows` visible rows, frame included."""
    used, counted = _chrome(view), 0
    for row in range(view.count()):
        if counted == rows:
            break
        if not view.isRowHidden(row):
            used += view.sizeHintForRow(row)
            counted += 1
    if counted < rows:
        # Fewer rows than that: room for the rest at the height of the last.
        step = view.sizeHintForRow(0) if view.count() else 64
        used += step * (rows - counted)
    return used


class Split(_Responsive):
    """Two panes side by side on a wide page, stacked on a narrow one.

    `side` is the width of the first pane when they sit side by side. Stacked,
    `first_stacked_max` caps the first pane (a list) so the second (its
    reader) takes the rest of the height, and `stacked_max` caps the second
    instead where the first is the one that should grow.

    With `splitter` the stacked panes share a handle the user can drag: the
    list starts at `share` of the height, never below `min_rows` whole rows
    while the reader keeps `second_min`, always ends on a whole row, and the
    split is remembered under `name` for the session.
    """

    def __init__(self, first, second, side=300, threshold=860, stacked_max=None,
                 first_stacked_max=None, spacing=14, fit_first=False, parent=None,
                 splitter=False, name="", share=0.38, min_rows=3, second_min=150):
        super().__init__(parent)
        # Stacked, the first pane keeps the height it asks for and no more.
        self.fit_first = fit_first
        self.first, self.second = first, second
        self.side, self.threshold = side, threshold
        self.stacked_max = stacked_max
        self.first_stacked_max = first_stacked_max
        # Called with True or False after the panes are arranged again.
        self.arranged = None
        self.box = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(spacing)
        self.splitter = None
        if splitter:
            self.name, self.share = name, share
            self.min_rows, self.second_min = min_rows, second_min
            self.splitter = QSplitter(Qt.Orientation.Vertical)
            self.splitter.setObjectName("panes")
            self.splitter.setChildrenCollapsible(False)
            self.splitter.setHandleWidth(spacing)
            self.splitter.addWidget(first)
            self.splitter.addWidget(second)
            self.splitter.splitterMoved.connect(self._moved)
            handle = self.splitter.handle(1)
            handle.setAccessibleName(t("Resize the list"))
            # Sized again once the splitter itself has its new height.
            self.splitter.installEventFilter(self)
            # And once the list's width settles: its rows change height with
            # it, and whole rows at the old width are cut rows at the new one.
            first.installEventFilter(self)
            # And once a scroll bar comes or goes: the rows rewrap to the
            # narrower viewport and change height under the split.
            bar = getattr(first, "verticalScrollBar", None)
            if bar is not None:
                bar().rangeChanged.connect(self._rows_changed)
            self.box.addWidget(self.splitter)
        else:
            self.box.addWidget(first)
            self.box.addWidget(second, 1)
        self._arrange(False)

    def _fits(self, width):
        return width >= self.threshold

    def _arrange(self, wide):
        if wide == self.wide:
            return
        self.wide = wide
        big = 16777215
        if self.splitter is not None:
            self.splitter.setOrientation(Qt.Orientation.Horizontal if wide
                                         else Qt.Orientation.Vertical)
            # Side by side the list keeps its width; the handle only moves
            # the stacked split.
            self.splitter.handle(1).setEnabled(not wide)
            if wide:
                self.first.setFixedWidth(self.side)
                self.first.setMinimumHeight(0)
                self.splitter.setStretchFactor(0, 0)
                self.splitter.setStretchFactor(1, 1)
            else:
                self.first.setMinimumWidth(0)
                self.first.setMaximumWidth(big)
                self._place()
            if self.arranged is not None:
                self.arranged(wide)
            return
        if wide:
            self.box.setDirection(QBoxLayout.Direction.LeftToRight)
            self.first.setFixedWidth(self.side)
            self.first.setMaximumHeight(big)
            self.second.setMaximumHeight(big)
            self.box.setStretch(0, 0)
        else:
            self.box.setDirection(QBoxLayout.Direction.TopToBottom)
            self.first.setMinimumWidth(0)
            self.first.setMaximumWidth(big)
            fitted = self.first.sizeHint().height() if self.fit_first else big
            self.first.setMaximumHeight(self.first_stacked_max or fitted)
            self.second.setMaximumHeight(self.stacked_max or big)
            self.box.setStretch(0, 1 if self.first_stacked_max is None and self.stacked_max else 0)
        if self.arranged is not None:
            self.arranged(wide)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and (
                watched is self.splitter
                or (watched is self.first
                    and event.oldSize().width() != event.size().width())):
            self._place()
        return super().eventFilter(watched, event)

    def _snap(self, wanted, total):
        """The list's height for `wanted`: whole rows, at least `min_rows`
        of them while the reader keeps its minimum."""
        room = max(0, total - self.second_min)
        floor = min(rows_height(self.first, self.min_rows), room)
        height = min(max(wanted, floor), room) if room else wanted
        snapped = whole_rows(self.first, height)
        return max(snapped, min(floor, height))

    def _rows_changed(self, *_):
        # A bound method: Qt drops the call if the split is deleted first.
        QTimer.singleShot(0, self._place)

    def _place(self):
        """Size the stacked panes from the remembered share."""
        if self.splitter is None or self.wide:
            return
        total = self.splitter.height() - self.splitter.handleWidth()
        if total <= 0:
            return
        share = _SHARES.get(self.name, self.share)
        first = self._snap(int(total * share), total)
        sizes = [first, max(0, total - first)]
        if self.splitter.sizes() != sizes:
            self.splitter.setSizes(sizes)

    def _moved(self, position, _index):
        total = self.splitter.height() - self.splitter.handleWidth()
        if self.wide or total <= 0:
            return
        first = self._snap(position, total)
        if self.name:
            _SHARES[self.name] = position / total
        self.splitter.setSizes([first, max(0, total - first)])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def showEvent(self, event):
        super().showEvent(event)
        self._place()


# ---- list rows --------------------------------------------------------------

class RowDelegate(QStyledItemDelegate):
    """A list row with a leading badge, a muted meta line and the body.

    Reads META_ROLE, BODY_ROLE and KIND_ROLE (a glyph name; "failed" draws in
    the danger colour). Up to `lines` lines of body, elided after that, then
    the meta line under it: what a row says comes first, when and where second.
    """

    PAD_V = 11
    PAD_H = 12
    BADGE = 30
    ACTION = 26

    def __init__(self, view, lines=2, actions=(), marks=False, badges=True, inline=False,
                 slots=0, flash=None, flash_word=""):
        """`actions` are (glyph, tooltip, callback) drawn at the right end of
        the row under the pointer and on the selected row, each called with
        the row's index when pressed. It may also be a function of a row's
        model index returning that list, for rows whose actions differ; then
        `slots` is the most actions any row has, the room every row keeps.
        `flash` is a function of a row's index returning a short word to
        show in place of the actions (a confirmation), or ""; every row
        keeps room for `flash_word`, the longest it says, so the word never
        lies over the row's text.
        `marks` keeps room at the right end for CHECK_ROLE.
        `badges` False leaves the leading badge out (a plain list of words);
        `inline` puts the meta text after the body on the same line, for
        compact one-line rows."""
        super().__init__(view)
        self.view = view
        self.lines = 1 if inline else lines
        self.actions = actions if callable(actions) else list(actions)
        self.slots = slots if callable(actions) else len(self.actions)
        self.flash = flash
        self.flash_word = flash_word
        self.marks = marks
        self.badges = badges
        self.inline = inline
        if self.slots:
            view.viewport().installEventFilter(self)

    def actions_for(self, index):
        """The actions of the row at `index`."""
        if callable(self.actions):
            return list(self.actions(index)) if index.isValid() else []
        return self.actions

    def _fonts(self, option):
        body = QFont(option.font)
        meta = QFont(body)
        meta.setPointSizeF(max(7.0, body.pointSizeF() - 1.0))
        return body, meta

    def _trailing(self):
        """What the right end of every row keeps free: the actions, or the
        check mark, so the text never runs under either."""
        if self.slots:
            room = self.slots * (self.ACTION + 4) + 4
            if self.flash_word:
                room = max(room, self._pill_size(self.view.font(), self.flash_word)[0] + 4)
            return room
        return 24 if self.marks else 0

    def _lead(self):
        """What the badge takes at the left end of a row, its gap included."""
        return self.BADGE + 12 if self.badges else 0

    def _text_width(self, width):
        return max(80, width - self.PAD_H * 2 - self._lead() - self._trailing())

    def action_rects(self, rect, actions=None):
        """Where each action of a row drawn in `rect` sits."""
        rects = []
        right = rect.right() - self.PAD_H
        for _ in (self.actions if actions is None else actions):
            rects.insert(0, QRect(right - self.ACTION, rect.center().y() - self.ACTION // 2,
                                  self.ACTION, self.ACTION))
            right -= self.ACTION + 4
        return rects

    def action_at(self, point):
        """The (row, action) under `point` in the viewport, or None."""
        index = self.view.indexAt(point)
        if not index.isValid():
            return None
        if self.flash and self.flash(index):
            return None  # the Copied pill covers the buttons for a moment
        actions = self.actions_for(index)
        for rect, action in zip(self.action_rects(self.view.visualRect(index), actions),
                                actions):
            if rect.contains(point):
                return index, action
        return None

    def eventFilter(self, viewport, event):
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
                    QEvent.Type.MouseButtonRelease) and self.slots \
                and event.button() == Qt.MouseButton.LeftButton:
            hit = self.action_at(event.position().toPoint())
            if hit is not None:
                # The press and a double click are the action's, not the
                # row's: they would select the row or activate it as well.
                if kind == QEvent.Type.MouseButtonRelease:
                    hit[1][2](hit[0].row())
                return True
        if kind == QEvent.Type.ToolTip and self.slots:
            from PyQt6.QtWidgets import QToolTip
            hit = self.action_at(event.pos())
            if hit is not None:
                QToolTip.showText(event.globalPos(), hit[1][1], self.view)
                return True
        return super().eventFilter(viewport, event)

    def _body_lines(self, text, metrics, width):
        if not text:
            return []
        words, lines, current = text.split(), [], ""
        for word in words:
            trial = (current + " " + word).strip()
            if metrics.horizontalAdvance(trial) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
                if len(lines) == self.lines:
                    break
        if len(lines) < self.lines and current:
            lines.append(current)
        lines = lines[:self.lines]
        used = sum(len(line.split()) for line in lines)
        if used < len(words):
            lines[-1] = metrics.elidedText(lines[-1] + " " + " ".join(words[used:]),
                                           Qt.TextElideMode.ElideRight, width)
        return lines

    def sizeHint(self, option, index):
        body, meta = self._fonts(option)
        width = self._text_width(self.view.viewport().width())
        if self.inline:
            pad = self.PAD_V - 1
            return QSize(width, pad * 2 + QFontMetrics(body).lineSpacing())
        lines = self._body_lines(index.data(BODY_ROLE) or "", QFontMetrics(body), width)
        # A row with nothing to say under its text has no meta line at all.
        under = QFontMetrics(meta).height() + 3 if index.data(META_ROLE) else 0
        height = (self.PAD_V * 2 + under
                  + QFontMetrics(body).lineSpacing() * max(1, len(lines)))
        return QSize(width, max(height, self.BADGE + self.PAD_V * 2 if self.badges else 0))

    def paint(self, painter, option, index):
        colors = colors_for(self.view)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(mix(colors["surface"], colors["accent"], 0.2)))
            painter.drawRoundedRect(QRectF(rect.adjusted(3, 2, -3, -2)), 7, 7)
            painter.setBrush(QColor(colors["accent"]))
            painter.drawRoundedRect(QRectF(rect.x() + 3, rect.y() + 8, 3, rect.height() - 16),
                                    1.5, 1.5)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors["hover"]))
            painter.drawRoundedRect(QRectF(rect.adjusted(3, 2, -3, -2)), 7, 7)
        if not selected and index.row() < index.model().rowCount() - 1:
            painter.setPen(QPen(QColor(colors["border"]), 1))
            y = rect.bottom()
            painter.drawLine(rect.x() + self.PAD_H, y, rect.right() - self.PAD_H, y)
        kind = index.data(KIND_ROLE) or "mic"
        tone = colors["danger"] if kind == "failed" else colors["accent"]
        badge = QRectF(rect.x() + self.PAD_H, rect.y() + (rect.height() - self.BADGE) / 2,
                       self.BADGE, self.BADGE)
        if self.badges:
            painter.setPen(Qt.PenStyle.NoPen)
            # On a selected row the background is already a wash of the
            # accent, so the badge takes a stronger one to stay a badge.
            painter.setBrush(QColor(mix(colors["surface"], tone, 0.42 if selected else 0.22)))
            painter.drawEllipse(badge)
            size = 16
            place = QRect(int(badge.center().x() - size / 2),
                          int(badge.center().y() - size / 2), size, size)
            letter = index.data(LETTER_ROLE)
            if letter:
                painter.drawPixmap(place, letter_pixmap(letter, tone, size, font=option.font))
            else:
                painter.drawPixmap(place, glyph_pixmap(kind, tone, size))
        if index.data(CHECK_ROLE):
            mark = 16
            painter.drawPixmap(QRect(rect.right() - self.PAD_H - mark,
                                     rect.center().y() - mark // 2, mark, mark),
                               glyph_pixmap("check", colors["accent"], mark))
        flash = self.flash(index) if self.flash else ""
        if flash:
            self._paint_flash(painter, rect, flash, option, colors, selected)
        elif self.slots and (hovered or selected):
            actions = self.actions_for(index)
            for spot, (name, _tip, _callback) in zip(self.action_rects(rect, actions), actions):
                icon = 16
                tone = (colors["danger"] if name == "trash"
                        else colors["accent"] if name == "check"
                        else colors["text"] if selected else colors["muted"])
                painter.drawPixmap(QRect(spot.center().x() - icon // 2 + 1,
                                         spot.center().y() - icon // 2 + 1, icon, icon),
                                   glyph_pixmap(name, tone, icon))
        body, meta = self._fonts(option)
        left = int(badge.right()) + 12 if self.badges else rect.x() + self.PAD_H + 4
        width = self._text_width(rect.width())
        meta_metrics, body_metrics = QFontMetrics(meta), QFontMetrics(body)
        if self.inline:
            self._paint_inline(painter, index, rect, left, width, body, meta, colors, kind)
            painter.restore()
            return
        lines = self._body_lines(index.data(BODY_ROLE) or "", body_metrics, width)
        under = meta_metrics.height() + 3 if index.data(META_ROLE) else 0
        block = under + body_metrics.lineSpacing() * max(1, len(lines))
        top = rect.y() + max(self.PAD_V, (rect.height() - block) // 2)
        painter.setFont(body)
        painter.setPen(QColor(colors["text"]))
        y = top
        for line in lines or [""]:
            painter.drawText(QRect(left, y, width, body_metrics.lineSpacing()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
            y += body_metrics.lineSpacing()
        painter.setFont(meta)
        painter.setPen(QColor(colors["danger"] if kind == "failed" else colors["muted"]))
        painter.drawText(QRect(left, y + 3, width, meta_metrics.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         meta_metrics.elidedText(index.data(META_ROLE) or "",
                                                 Qt.TextElideMode.ElideRight, width))
        painter.restore()

    @staticmethod
    def _pill_font(base):
        font = QFont(base)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def _pill_size(self, base, word):
        """The width and height of the confirmation pill saying `word`."""
        metrics = QFontMetrics(self._pill_font(base))
        return 14 + 6 + metrics.horizontalAdvance(word) + 20, max(14, metrics.height()) + 10

    def _paint_flash(self, painter, rect, word, option, colors, selected):
        """A short confirmation (a check and a word) in a pill at the right
        end of the row, over whatever the row draws there."""
        font = self._pill_font(option.font)
        icon = 14
        width, height = self._pill_size(option.font, word)
        pill = QRectF(rect.right() - self.PAD_H - width, rect.center().y() - height / 2,
                      width, height)
        back = mix(colors["surface"], colors["accent"], 0.2) if selected else colors["surface"]
        painter.setPen(QPen(QColor(colors["accent"]), 1))
        painter.setBrush(QColor(mix(back, colors["accent"], 0.18)))
        painter.drawRoundedRect(pill, height / 2, height / 2)
        x = int(pill.x()) + 10
        painter.drawPixmap(QRect(x, int(pill.center().y() - icon / 2), icon, icon),
                           glyph_pixmap("check", colors["accent"], icon))
        painter.setFont(font)
        painter.setPen(QColor(colors["text"]))
        painter.drawText(QRectF(x + icon + 6, pill.y(), width, pill.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, word)

    def _paint_inline(self, painter, index, rect, left, width, body, meta, colors, kind):
        """One line: the body, then the meta text muted after it, the body
        keeping its whole width first and the meta elided into what is left."""
        body_metrics, meta_metrics = QFontMetrics(body), QFontMetrics(meta)
        text = index.data(BODY_ROLE) or ""
        shown = body_metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)
        used = body_metrics.horizontalAdvance(shown)
        line = QRect(left, rect.y(), width, rect.height())
        painter.setFont(body)
        painter.setPen(QColor(colors["text"]))
        painter.drawText(line, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)
        rest = width - used - 16
        extra = index.data(META_ROLE) or ""
        if extra and rest > 40:
            painter.setFont(meta)
            painter.setPen(QColor(colors["danger"] if kind == "failed" else colors["muted"]))
            painter.drawText(QRect(left + used + 16, rect.y(), rest, rect.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             meta_metrics.elidedText(extra, Qt.TextElideMode.ElideRight, rest))


def row_list(view, lines=2, actions=(), marks=False, badges=True, inline=False,
             slots=0, flash=None, flash_word=""):
    """Give a QListWidget the row delegate and the settings it relies on."""
    from PyQt6.QtWidgets import QListView
    view.setObjectName("rows")
    view.setItemDelegate(RowDelegate(view, lines, actions, marks, badges, inline,
                                     slots, flash, flash_word))
    view.setMouseTracking(True)
    view.setUniformItemSizes(False)
    view.setResizeMode(QListView.ResizeMode.Adjust)
    view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setWordWrap(True)
    return view


# ---- live sound -------------------------------------------------------------

class LevelMeter(QWidget):
    """The recent input levels as a row of bars, the same levels the
    recording indicator draws. `set_levels` takes them as they are now; the
    meter keeps no clock of its own, so it moves only while somebody feeds it.

    A level under QUIET is drawn as a flat stub in the muted colour: what the
    page calls silence never shows as sound on the meter beside it.
    """

    QUIET = 0.05

    def __init__(self, bars=22, role="danger", parent=None, height=26):
        super().__init__(parent)
        self.role = role
        self.levels = [0.0] * bars
        self.setFixedHeight(height)
        self.setMinimumWidth(bars * 3)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # Decoration for sighted users; the state title says the same in words.
        self.setAccessibleName(t("Input level"))

    def set_levels(self, levels):
        levels = list(levels)[-len(self.levels):]
        # Fewer levels than bars: the newest at the right end, as the
        # indicator draws them, with silence before them.
        levels = [0.0] * (len(self.levels) - len(levels)) + levels
        if levels != self.levels:
            self.levels = levels
            self.update()

    def set_role(self, role):
        """The colour role of the bars ("danger" live, "warn" paused)."""
        if role != self.role:
            self.role = role
            self.update()

    def peak(self, last=6):
        return max(self.levels[-last:], default=0.0)

    def paintEvent(self, _event):
        colors = colors_for(self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        count = len(self.levels)
        width = self.width()
        step = width / max(1, count)
        # Bars that keep their proportion however wide the meter is stretched.
        bar = min(6.0, max(3.0, step * 0.4))
        step = (width - bar) / max(1, count - 1)
        mid = self.height() / 2
        tone = QColor(colors[self.role] if self.role in colors else colors["accent"])
        quiet = QColor(colors["muted"])
        for index, level in enumerate(self.levels):
            if level < self.QUIET:
                color = QColor(quiet)
                color.setAlphaF(0.45)
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(index * step, mid - 1.5, bar, 3.0), 1.4, 1.4)
                continue
            shaped = min(1.0, max(0.0, level) ** 0.55)
            height = 3.0 + shaped * (self.height() - 5)
            color = QColor(tone)
            color.setAlphaF(0.35 + 0.65 * min(1.0, shaped * 2.2))
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(index * step, mid - height / 2, bar, height),
                                    1.4, 1.4)
        painter.end()


class FitColumn(QVBoxLayout):
    """A column that fills the scroll area it is in rather than asking for the
    height its wrapped labels would like, so a pane inside it that scrolls on
    its own does the scrolling (one scroller, not two)."""

    def hasHeightForWidth(self):
        return False


# ---- cards ------------------------------------------------------------------

def _is_editor(item):
    from PyQt6.QtWidgets import QPlainTextEdit, QTextEdit
    return isinstance(item, (QPlainTextEdit, QTextEdit))


def card_add(layout, item, stretch=0, index=None):
    """Put `item` into a card's layout so its first letter lands CARD_PAD in
    from the card's edge: a text box as it is (its own inset makes up the
    difference), anything else moved in by that inset."""
    if _is_editor(item):
        item.document().setDocumentMargin(0)
    elif isinstance(item, QLayout):
        margins = item.contentsMargins()
        item.setContentsMargins(EDITOR_INSET, margins.top(), EDITOR_INSET, margins.bottom())
    elif not getattr(item, "flush", False):
        margins = item.contentsMargins()
        item.setContentsMargins(EDITOR_INSET, margins.top(), EDITOR_INSET, margins.bottom())
    if index is None:
        if isinstance(item, QLayout):
            layout.addLayout(item, stretch)
        else:
            layout.addWidget(item, stretch)
    elif isinstance(item, QLayout):
        layout.insertLayout(index, item, stretch)
    else:
        layout.insertWidget(index, item, stretch)


def card(*items, name="result", spacing=8):
    """A filled card holding `items` (widgets, layouts or (item, stretch)),
    laid out with the one padding every card uses."""
    frame = QFrame()
    frame.setObjectName(name)
    layout = QVBoxLayout(frame)
    inner = CARD_PAD - EDITOR_INSET
    layout.setContentsMargins(inner, CARD_PAD - 4, inner, CARD_PAD - 4)
    layout.setSpacing(spacing)
    for item in items:
        item, stretch = item if isinstance(item, tuple) else (item, 0)
        card_add(layout, item, stretch)
    return frame


class _ReadingWidth(QWidget):
    """Keeps a text box's lines to READING_MAX by narrowing its viewport;
    the box itself, its frame and its scroll bar keep the full width."""

    def __init__(self, editor, width):
        super().__init__(editor)
        self.hide()
        self.editor, self.width_ = editor, width
        editor.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() == QEvent.Type.Resize:
            spare = event.size().width() - self.width_ - EDITOR_INSET * 2
            try:
                self.editor.setViewportMargins(0, 0, max(0, spare), 0)
            except RuntimeError:
                pass
        return False


def wrap_text(editor):
    """Running text that wraps at the box's width, anywhere if a word is
    longer than a line: a reading box never scrolls sideways."""
    if isinstance(editor, QPlainTextEdit):
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
    else:
        editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
    editor.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return editor


def cap_reading(editor, width=READING_MAX):
    """Never let a line of running text in `editor` run past `width`, and
    never let it scroll sideways (wrap_text)."""
    wrap_text(editor)
    editor._reading = _ReadingWidth(editor, width)
    return editor


# ---- small pieces of the home window ---------------------------------------

class StateHeader(QWidget):
    """The head of a home tab: its state title, the lines under it (a meter,
    a hint, chips) and its actions.

    Centred, for a page that is a single column, the title and every line
    under it sit on the page's axis, the one the capture button is on, and
    the actions form a centred row under them: nothing moves sideways when
    the state changes. A `corner` widget (a small chip) sits at the top right
    without pulling the title off that axis, because the title keeps an equal
    margin on both sides; when the row has no room for that, the corner joins
    the row under the title instead.

    As a row, for a page whose panes sit side by side, the title and its
    lines start at the left and the actions end at the right, as a toolbar
    over both panes.

    A title is there only to say a state (recording, working): an empty
    title takes no room, and a header with nothing to show hides itself."""

    GAP = 12

    def __init__(self, title, below=(), actions=(), corner=None, centred=True, parent=None):
        super().__init__(parent)
        self.title, self.below = title, list(below)
        self.actions, self.corner = list(actions), corner
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(4)
        self.action_row = QWidget()
        self._row = QHBoxLayout(self.action_row)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(BUTTON_GAP)
        for action in self.actions:
            self._row.addWidget(action, 0, Qt.AlignmentFlag.AlignVCenter)
        if corner is not None:
            corner.installEventFilter(self)
        # The widest the title has been: the corner's place is chosen for
        # that, so a longer state title does not send the chip back and forth.
        self._title_room = 0
        self.centred = None
        self._corner_up = None
        self.set_centred(centred)

    def set_centred(self, centred):
        if centred == self.centred:
            return
        self.centred = centred
        self._corner_up = None
        self._build()

    def _clear(self):
        while self.grid.count():
            self.grid.takeAt(0)

    def _build(self):
        self._clear()
        grid, centre = self.grid, Qt.AlignmentFlag.AlignHCenter
        left = Qt.AlignmentFlag.AlignLeft
        self.title.setAlignment((Qt.AlignmentFlag.AlignCenter if self.centred
                                 else left | Qt.AlignmentFlag.AlignVCenter))
        self.title.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.title, 0, 0)
        for index, item in enumerate(self.below, 1):
            if isinstance(item, QLabel):
                item.setAlignment(centre if self.centred else left)
                grid.addWidget(item, index, 0)
            elif item.maximumWidth() < 16777215:
                # A capped item (a meter) goes where the title's text is.
                grid.addWidget(item, index, 0, centre if self.centred else left)
            else:
                if hasattr(item, "set_align"):
                    item.set_align(centre if self.centred else left)
                grid.addWidget(item, index, 0)
        grid.setColumnStretch(0, 1)
        if self.centred:
            grid.setColumnStretch(1, 0)
            grid.addWidget(self.action_row, len(self.below) + 1, 0, centre)
        else:
            grid.addWidget(self.action_row, 0, 1, len(self.below) + 1, 1,
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.fit()

    def fit(self):
        """Put the corner where it fits and the action row away when empty;
        cheap enough to call on every refresh."""
        corner = self.corner
        shown = corner is not None and not corner.isHidden()
        up = False
        if shown and self.centred:
            metrics = self.title.fontMetrics()
            self._title_room = max(self._title_room, metrics.horizontalAdvance(self.title.text()))
            reserve = corner.sizeHint().width() + self.GAP
            up = self.width() - 2 * reserve >= self._title_room + self.GAP
        if corner is not None and up != self._corner_up:
            self._corner_up = up
            self.grid.removeWidget(corner)
            self._row.removeWidget(corner)
            if up:
                self.grid.addWidget(corner, 0, 0, Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
            else:
                self._row.addWidget(corner, 0, Qt.AlignmentFlag.AlignVCenter)
            if shown:
                corner.show()
        reserve = corner.sizeHint().width() + self.GAP if (shown and up) else 0
        margins = self.title.contentsMargins()
        if margins.left() != reserve:
            self.title.setContentsMargins(reserve, 0, reserve, 0)
        in_row = list(self.actions) + ([corner] if corner is not None and not up else [])
        acting = any(not item.isHidden() for item in in_row)
        self.action_row.setVisible(acting)
        titled = bool(self.title.text())
        self.title.setVisible(titled)
        above = titled or any(not item.isHidden() for item in self.below)
        # Centred, the buttons sit a group gap under the lines above them;
        # with nothing above, they start at the top.
        gap = GROUP_GAP - self.grid.verticalSpacing() if self.centred and above else 0
        if self._row.contentsMargins().top() != gap:
            self._row.setContentsMargins(0, gap, 0, 0)
        self.setVisible(above or acting or (shown and up))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.fit()

    def eventFilter(self, watched, event):
        if watched is self.corner and event.type() in (
                QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.LayoutRequest):
            QTimer.singleShot(0, self._refit)
        return False

    def _refit(self):
        try:
            self.fit()
        except RuntimeError:
            pass  # the window went while the timer ran


class KeyHint(QWidget):
    """A shortcut as keycaps followed by a few words: [Ctrl] [Space] to start,
    again to stop and paste. Its accessible name says the same in a sentence."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(4)
        self._spec = None
        self._plain = ""

    def set(self, keys, words, before=""):
        """`before` is said ahead of the keys ("All set. Press"), `words`
        after them."""
        spec = (tuple(keys), words, before)
        if spec == self._spec:
            return
        self._spec = spec
        while self.row.count():
            item = self.row.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.row.addStretch(1)
        if before:
            lead = QLabel(before)
            lead.setObjectName("muted")
            lead.setTextFormat(Qt.TextFormat.PlainText)
            self.row.addWidget(lead, 0, Qt.AlignmentFlag.AlignVCenter)
            self.row.addSpacing(4)
        for key in keys:
            cap = QLabel(key)
            cap.setObjectName("keycap")
            cap.setTextFormat(Qt.TextFormat.PlainText)
            self.row.addWidget(cap, 0, Qt.AlignmentFlag.AlignVCenter)
        line = QLabel(words)
        line.setObjectName("muted")
        line.setTextFormat(Qt.TextFormat.PlainText)
        self.row.addSpacing(4 if keys else 0)
        self.row.addWidget(line, 0, Qt.AlignmentFlag.AlignVCenter)
        self.row.addStretch(1)
        self._plain = " ".join(part for part in (before, " + ".join(keys), words) if part)
        self.setAccessibleName(self._plain)

    def text(self):
        return self._plain


class FileName(QLabel):
    """A file name that fits its width by losing the middle of its stem, so
    the end of the name and its extension stay readable
    ("workshop-inter…-03.m4a"). `text()` is always the whole name, which is
    also the tooltip."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(60)
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self.setAccessibleName(self._full)
        self._render()

    def text(self):
        return self._full

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, hint.height())

    def _render(self):
        room = max(20, self.width())
        metrics = self.fontMetrics()
        full = self._full
        if metrics.horizontalAdvance(full) <= room:
            shown = full
        else:
            stem, dot, ext = full.rpartition(".")
            if not dot or not stem or len(ext) > 6:
                shown = metrics.elidedText(full, Qt.TextElideMode.ElideMiddle, room)
            else:
                ext = "." + ext
                shown = metrics.elidedText(stem, Qt.TextElideMode.ElideMiddle,
                                           max(10, room - metrics.horizontalAdvance(ext))) + ext
        QLabel.setText(self, shown)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()


class DropZone(QFrame):
    """Where a file is chosen: dropped on it from a file manager, or picked
    with the button inside it. Emits `dropped` with the path.

    Wide, the badge, the name and the button share one row; narrower than
    STACK_BELOW the button goes under the name, so the name keeps the width.
    The zone is as tall as what it holds: the pane around it can take drops
    too (accept_drops), without a large dashed rectangle to say so."""

    dropped = pyqtSignal(str)
    STACK_BELOW = 400

    def __init__(self, title, button, parent=None):
        super().__init__(parent)
        self.setObjectName("dropzone")
        self.setAcceptDrops(True)
        self.setProperty("over", False)
        self.flush = True
        self.button = button
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(CARD_PAD, 12, CARD_PAD, 12)
        self.column.setSpacing(10)
        self.top = QHBoxLayout()
        self.top.setSpacing(12)
        self.badge = Badge("doc", "accent", 36)
        self.top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = FileName(title)
        self.title.setObjectName("tiletitle")
        self.top.addWidget(self.title, 1, Qt.AlignmentFlag.AlignVCenter)
        self.top.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.column.addLayout(self.top)
        self.stacked = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setMaximumHeight(180)

    def _arrange(self, stacked):
        if stacked == self.stacked:
            return
        self.stacked = stacked
        self.top.removeWidget(self.button)
        self.column.removeWidget(self.button)
        if stacked:
            self.column.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)
        else:
            self.top.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if (event.size().width() < self.STACK_BELOW) != self.stacked:
            # After this resize has been handled, and from the width the
            # zone has by then: moving the button changes the zone's height,
            # which asks the parent to lay out again.
            QTimer.singleShot(0, lambda: self._arrange(self.width() < self.STACK_BELOW))

    def _paths(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def _over(self, on):
        self.setProperty("over", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):
        if self._paths(event):
            event.acceptProposedAction()
            self._over(True)

    def dragLeaveEvent(self, event):
        self._over(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._over(False)
        paths = self._paths(event)
        if paths:
            event.acceptProposedAction()
            self.dropped.emit(paths[0])

    def accept_drops(self, widget):
        """Let `widget` (the pane around the zone) take a dropped file as
        well, lighting the zone up while one is held over it."""
        widget.setAcceptDrops(True)
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.DragEnter:
            self.dragEnterEvent(event)
            return event.isAccepted()
        if kind == QEvent.Type.DragMove:
            if self._paths(event):
                event.acceptProposedAction()
                return True
        if kind == QEvent.Type.DragLeave:
            self._over(False)
        if kind == QEvent.Type.Drop:
            self.dropEvent(event)
            return True
        return super().eventFilter(watched, event)


# ---- forms ------------------------------------------------------------------

def _span_unlabelled(form):
    """Turn every row of `form` that has no label text into a spanning row."""
    row = 0
    while row < form.rowCount():
        label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
        text = label.widget().text() if label is not None and isinstance(
            label.widget(), QLabel) else None
        if field is not None and (label is None or text == ""):
            visible = form.isRowVisible(row)
            taken = form.takeRow(row)
            if taken.labelItem is not None and taken.labelItem.widget() is not None:
                taken.labelItem.widget().hide()
                taken.labelItem.widget().deleteLater()
            item = taken.fieldItem
            if item.widget() is not None:
                # A widget keeps its own visibility through the move. Hiding
                # the row instead would pin it hidden, and a widget that later
                # shows itself (a status line with something to say) would then
                # float outside the layout at a stale position.
                form.insertRow(row, item.widget())
            elif item.layout() is not None:
                form.insertRow(row, item.layout())
                form.setRowVisible(row, visible)
        row += 1


def align_form(forms):
    """One label column for `forms` (a section's form and those nested in
    it): unlabelled rows span, and every label gets one width, wrapping past
    LABEL_MAX. Returns that width."""
    labels = []
    for form in forms:
        _span_unlabelled(form)
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and isinstance(item.widget(), QLabel):
                labels.append(item.widget())
    if not labels:
        return 0
    width = min(LABEL_MAX, max(label.sizeHint().width() for label in labels))
    for label in labels:
        label.setWordWrap(True)
        label.setFixedWidth(width)
    return width


WEB_SCHEMES = ("http", "https")


def guard_links(browser):
    """Let a rendered document open only web links.

    The minutes are written by a model and can carry any link it was talked
    into producing; Qt would hand any scheme to the desktop (smb:, search-ms:,
    custom protocol handlers). Links are opened here instead, and only when
    they point at the web."""
    from PyQt6.QtGui import QDesktopServices

    browser.setOpenLinks(False)
    browser.setOpenExternalLinks(False)

    def follow(url):
        if url.scheme().lower() in WEB_SCHEMES:
            QDesktopServices.openUrl(url)

    browser.anchorClicked.connect(follow)
    return browser
