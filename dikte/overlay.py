"""The small recording indicator that appears in a screen corner without taking focus."""

import math
import sys

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import QColor, QCursor, QFont, QPainter, QPainterPath, QPen, QFontMetrics
from PyQt6.QtWidgets import QWidget, QApplication

from . import mac_window

BARS = 22
HEIGHT = 56
MIN_WIDTH = 210
MAX_WIDTH = 460
MARGIN = 28
GAP = 10        # between two indicators sharing a corner

BG = QColor(22, 24, 29, 238)
BORDER = QColor(255, 255, 255, 28)
TEXT = QColor(235, 237, 242)
MUTED = QColor(150, 156, 168)
REC = QColor(240, 78, 82)
BUSY = QColor(120, 170, 255)
OK = QColor(80, 205, 140)
ERR = QColor(240, 100, 90)
WARN = QColor(240, 180, 80)
THEM = QColor(110, 190, 255)   # the other side of a meeting

ASK = QColor(150, 140, 255)    # recording a command rather than a dictation
# Recording, but nothing is going in. The same amber a warning gets, and for
# the same reason: it is the colour that stops you walking away from it.
HELD = WARN

STATE_COLORS = {"recording": REC, "asking": ASK, "meeting": REC, "busy": BUSY,
                "done": OK, "warning": WARN, "error": ERR}
LIVE = ("recording", "asking", "meeting")


class Overlay(QWidget):
    """One indicator. Give it `below` and it stacks on top of that one instead
    of covering it, which is what lets a dictation and a command to the agent be
    under way at the same time and still both be visible."""

    def __init__(self, corner="bottom-left", below=None, dismissable=False,
                 screen_name=""):
        super().__init__(None)
        self.corner = corner
        self.screen_name = screen_name
        self.below = below
        # A job that can run for ten minutes should not have to be watched for
        # ten minutes. Clicking such an indicator puts the progress away; the
        # work carries on and its result still shows up.
        self.dismissable = dismissable
        self.muted = False
        self._stacked = False
        # A pause is not a state of its own: what is on screen is still the
        # recording, held. Keeping it beside the state is what lets the ribbon
        # stay where the pause found it instead of being cleared and rebuilt.
        self.paused = False
        self.state = "idle"
        self.message = ""
        self.levels = [0.0] * BARS
        self.levels2 = [0.0] * BARS   # the other side, while a meeting records
        self.seconds = 0.0
        self._phase = 0.0
        self._concealed = True

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if sys.platform not in ("darwin", "win32"):
            # It is the window manager that would otherwise move this out of
            # the corner. macOS has no such hint, and Qt warns about it;
            # Windows places tool windows where they ask to be anyway.
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        # One that can be clicked away has to receive the click, which means it
        # also swallows one aimed at whatever is underneath it. The rest stay
        # transparent to the mouse, as an indicator should be. It has to be this
        # flag and not WA_TransparentForMouseEvents: on a top-level window the
        # attribute only makes Qt drop the event it already took, so the click
        # never reaches the window below. The flag is the one that tells the
        # display server the window has no input region at all. It is read when
        # the window is created and cannot be turned off later without the
        # window being torn down and built again, which is why the dismissable
        # one has to shrink itself instead (see _conceal).
        if dismissable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            flags |= Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.resize(MIN_WIDTH, HEIGHT)

        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._tick)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._conceal)

    # ---- public API --------------------------------------------------

    def show_recording(self, asking=False):
        """The same ribbon either way, in a different colour when what is being
        recorded is a command for Claude rather than something to paste."""
        self.state = "asking" if asking else "recording"
        self.message = ""
        self.seconds = 0.0
        self.levels = [0.0] * BARS
        self.paused = False
        self.muted = False   # a new run starts visible, whatever the last one did
        self._hide_timer.stop()
        self._appear()

    def show_meeting(self):
        """Both channels at once: your voice up, the other side down."""
        self.state = "meeting"
        self.message = ""
        self.seconds = 0.0
        self.levels = [0.0] * BARS
        self.levels2 = [0.0] * BARS
        self.paused = False
        self._hide_timer.stop()
        self._appear()

    def show_busy(self, message):
        # Muted: the stages keep arriving and are simply not drawn. The state is
        # left alone as well, so nothing repaints the box back onto the screen.
        if self.muted:
            self.message = message
            return
        self.state = "busy"
        self.message = message
        self._hide_timer.stop()
        self._appear()

    def show_done(self, message="", msec=2000):
        self._finish("done", message, msec)

    def show_warning(self, message, msec=9000):
        """Finished, but something the user should know about went wrong."""
        self._finish("warning", message, msec)

    def show_error(self, message, msec=6000):
        self._finish("error", message, msec)

    def _finish(self, state, message, msec):
        """An outcome always shows, even one that was told to be quiet: waving
        the progress away asks not to be watched, not to be kept in the dark."""
        self.muted = False
        self.state = state
        self.message = message
        self._appear()
        self._hide_timer.start(msec)

    def dismiss(self):
        self._hide_timer.stop()
        self._conceal()

    def mousePressEvent(self, event):
        # Only a job in progress can be waved away. A recording is short and
        # ending it by accident would cost the words; an outcome goes on its own.
        if self.dismissable and self.state == "busy":
            self.muted = True
            self.dismiss()
        event.accept()

    @property
    def showing(self):
        """Mapped and actually painting something. The window stays mapped while
        idle, so isVisible() alone would always say yes."""
        return self.isVisible() and not self._concealed

    def push_level(self, level):
        self.levels = self.levels[1:] + [level]

    def push_levels(self, mine, theirs):
        self.levels = self.levels[1:] + [mine]
        self.levels2 = self.levels2[1:] + [theirs]

    def set_seconds(self, seconds):
        self.seconds = seconds

    def set_paused(self, paused):
        """Held, or taking sound in again.

        Everything about the ribbon says a recording is running: a dot that
        pulses, bars that move, a clock that counts. A pause that only stopped
        the sound would leave all three saying the words are still going in, so
        it is the ribbon that has to say otherwise.
        """
        self.paused = bool(paused)
        self.update()

    # ---- internals -----------------------------------------------------

    def _appear(self):
        self._resize_to_content()
        self._reposition()
        if not self.isVisible():
            self.show()
        if sys.platform == "darwin":
            # After show(), because the window it works on does not exist until
            # then, and every time, because a window Qt rebuilt has the setting
            # again at its default.
            mac_window.keep_on_screen(self)
        if self._concealed:
            self.raise_()
            self._concealed = False
        if not self._anim.isActive():
            self._anim.start()

    def _conceal(self):
        """Empty the window out instead of unmapping it.

        Unmapping tears the window down and the next dictation builds a new one,
        which makes the compositor repaint whatever sits underneath: on a tiled
        desktop the terminal behind visibly flinches every time the indicator
        goes away. So the window stays mapped and simply paints nothing. It has
        to be a real repaint, not just zero opacity: with the animation stopped
        nothing else damages the surface, and the stale frame would sit on the
        screen until some other event made the compositor redraw it.

        A window that stays mapped also stays clickable, though, and the one
        that can be dismissed is the one that takes clicks. Left at full size it
        would turn its corner of the screen into a dead zone long after there
        was anything to see there, so it shrinks to a point. Resizing keeps the
        surface alive, unlike hiding it.
        """
        self._anim.stop()
        self.state = "hidden"
        self._concealed = True
        self.repaint()
        if self.dismissable:
            self.resize(1, 1)

    def _resize_to_content(self):
        if self.state in LIVE:
            # A meeting runs long enough to need an hours field.
            width = MIN_WIDTH + (24 if self.state == "meeting" else 0)
        else:
            metrics = QFontMetrics(self._label_font())
            extra = 76 + (18 if self._can_dismiss else 0)
            width = max(MIN_WIDTH,
                        min(MAX_WIDTH, metrics.horizontalAdvance(self.message) + extra))
        self.resize(width, HEIGHT)

    def _reposition(self):
        # The screen the settings name, or, when none is named or it is not
        # plugged in right now, where the user actually is. Names are connector
        # names on X11 and model names on macOS, where two identical monitors
        # can share one; the first then wins.
        screen = next(
            (item for item in QApplication.screens() if item.name() == self.screen_name),
            None,
        )
        screen = screen or QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        left = "left" in self.corner
        top = "top" in self.corner
        self._stacked = self.below is not None and self.below.showing
        # Stack away from the edge the corner sits on, so the pair grows into the
        # screen rather than off it.
        step = (self.below.height() + GAP) if self._stacked else 0
        x = area.left() + MARGIN if left else area.right() - self.width() - MARGIN
        y = (area.top() + MARGIN + step if top
             else area.bottom() - self.height() - MARGIN - step)
        self.move(int(x), int(y))

    def _tick(self):
        self._phase += 0.12
        # The one underneath can come and go while this one is up; drop back to
        # the corner when it does rather than leaving a gap where it was.
        if self.below is not None and self.below.showing != self._stacked:
            self._reposition()
        if self.state in LIVE and not self.paused:
            # keep the ribbon moving even through a pause in speech
            self.levels = self.levels[1:] + [self.levels[-1] * 0.72]
            if self.state == "meeting":
                self.levels2 = self.levels2[1:] + [self.levels2[-1] * 0.72]
        self.update()

    def _label_font(self):
        font = QFont(self.font())
        font.setPointSizeF(10.5)
        return font

    # ---- painting --------------------------------------------------

    def paintEvent(self, _event):
        if self.state == "hidden":
            return  # translucent window, nothing drawn means nothing shown

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)

        path = QPainterPath()
        path.addRoundedRect(rect, 15, 15)
        painter.fillPath(path, BG)
        painter.setPen(QPen(BORDER, 1))
        painter.drawPath(path)

        accent = STATE_COLORS.get(self.state, MUTED)
        if self._held:
            accent = HELD
        self._draw_indicator(painter, accent)

        if self.state in LIVE:
            self._draw_waveform(painter, accent)
            self._draw_time(painter)
        else:
            self._draw_message(painter)
            if self._can_dismiss:
                self._draw_dismiss(painter)

    @property
    def _held(self):
        """A recording that is paused. Nothing else can be."""
        return self.paused and self.state in LIVE

    def _draw_indicator(self, painter, accent):
        cx, cy = 26.0, self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        if self._held:
            # The two bars everything that plays sound uses, and no glow: a
            # pulse is what says a recording is live.
            painter.setBrush(accent)
            for offset in (-4.4, 1.4):
                painter.drawRoundedRect(
                    QRectF(cx + offset, cy - 6.5, 3.0, 13.0), 1.2, 1.2
                )
        elif self.state in LIVE:
            pulse = 0.62 + 0.38 * (0.5 + 0.5 * math.sin(self._phase * 1.6))
            glow = QColor(accent)
            glow.setAlphaF(0.22 * pulse)
            painter.setBrush(glow)
            painter.drawEllipse(QPointF(cx, cy), 13 * pulse, 13 * pulse)
            painter.setBrush(accent)
            painter.drawEllipse(QPointF(cx, cy), 5.5, 5.5)
        elif self.state == "busy":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pen = QPen(QColor(accent), 2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            span = 100 * 16
            start = int(-self._phase * 320) % (360 * 16)
            painter.drawArc(QRectF(cx - 8, cy - 8, 16, 16), start, span)
        elif self.state == "done":
            pen = QPen(accent, 2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(
                QPointF(cx - 7, cy), QPointF(cx - 2, cy + 5.5), QPointF(cx + 7.5, cy - 6)
            )
        elif self.state == "warning":
            pen = QPen(accent, 2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(cx, cy - 7), QPointF(cx, cy + 1.5))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(QPointF(cx, cy + 6), 1.5, 1.5)
        else:  # error
            pen = QPen(accent, 2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(cx - 6, cy - 6), QPointF(cx + 6, cy + 6))
            painter.drawLine(QPointF(cx + 6, cy - 6), QPointF(cx - 6, cy + 6))

    def _bars(self):
        """(x of the first bar, bar width, distance between two bars)."""
        # A meeting's clock carries an hours field, so it needs more room and
        # the ribbon has to stop earlier.
        left = 46.0
        right = self.width() - (74.0 if self.state == "meeting" else 58.0)
        bar_w = 2.6
        gap = (right - left - BARS * bar_w) / max(1, BARS - 1)
        return left, bar_w, bar_w + gap

    @staticmethod
    def _bar_colour(shaped, accent):
        color = QColor(accent if shaped > 0.04 else MUTED)
        color.setAlphaF(0.35 + 0.65 * min(1.0, shaped * 2.2))
        return color

    def _draw_waveform(self, painter, accent=REC):
        if self.state == "meeting":
            self._draw_dual_waveform(painter)
            return
        left, bar_w, step = self._bars()
        mid = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        for i, level in enumerate(self.levels):
            shaped = min(1.0, level ** 0.55)
            h = 3.0 + shaped * 26.0
            painter.setBrush(self._bar_colour(shaped, accent))
            painter.drawRoundedRect(
                QRectF(left + i * step, mid - h / 2, bar_w, h), 1.3, 1.3
            )

    def _draw_dual_waveform(self, painter):
        """Your microphone above the line, what the speakers play below it.

        Seeing both move is the whole check that a meeting is being captured
        properly: one silent half means that side is not reaching the recording.
        """
        left, bar_w, step = self._bars()
        mid = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        for i, (mine, theirs) in enumerate(zip(self.levels, self.levels2)):
            x = left + i * step
            for level, accent, up in ((mine, REC, True), (theirs, THEM, False)):
                shaped = min(1.0, level ** 0.55)
                h = 2.0 + shaped * 12.0
                y = mid - 1.5 - h if up else mid + 1.5
                painter.setBrush(self._bar_colour(shaped, accent))
                painter.drawRoundedRect(QRectF(x, y, bar_w, h), 1.3, 1.3)

    def _draw_time(self, painter):
        font = QFont(self.font())
        font.setPointSizeF(10.0)
        font.setFamilies(["monospace"])
        painter.setFont(font)
        painter.setPen(MUTED)
        mins, secs = divmod(int(self.seconds), 60)
        hours, mins = divmod(mins, 60)
        text = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"
        width = 62 if hours else 44
        painter.drawText(
            QRectF(self.width() - width - 12, 0, width, self.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
            text,
        )

    @property
    def _can_dismiss(self):
        return self.dismissable and self.state == "busy"

    def _draw_dismiss(self, painter):
        """A faint cross on the right: without it there is nothing to say the
        box can be clicked away, and a feature nobody can see is not one."""
        cx, cy = self.width() - 18.0, self.height() / 2
        pen = QPen(QColor(MUTED), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(cx - 4, cy - 4), QPointF(cx + 4, cy + 4))
        painter.drawLine(QPointF(cx + 4, cy - 4), QPointF(cx - 4, cy + 4))

    def _draw_message(self, painter):
        painter.setFont(self._label_font())
        painter.setPen({"error": ERR, "warning": WARN}.get(self.state, TEXT))
        # Leave the cross its corner rather than running the text under it.
        box = QRectF(46, 0, self.width() - 60 - (18 if self._can_dismiss else 0),
                     self.height())
        metrics = QFontMetrics(self._label_font())
        text = metrics.elidedText(self.message, Qt.TextElideMode.ElideRight, int(box.width()))
        painter.drawText(
            box, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text
        )
