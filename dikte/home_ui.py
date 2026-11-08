"""Task-first native desktop window backed by the application controllers."""

import datetime
import re
import shutil
import time

from PyQt6.QtCore import QEvent, QRectF, Qt, QSize, QTimer
from PyQt6.QtGui import QActionGroup, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QMenu,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from . import api, assistant, audio, cleanup, config as cfg, ggml, overlay, profiles
from .i18n import t
from .settings_ui import (CLEANUP_PROVIDERS, _retryable, agent_name, history_brief, history_context,
                          history_kind, history_title)
from .setup_ui import SetupDialog
from . import theme
from . import widgets


def _label(text="", name="", centered=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setObjectName(name)
    if centered:
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


def _button(text, callback, name=""):
    button = QPushButton(text)
    button.setObjectName(name)
    button.setAutoDefault(False)
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    button.clicked.connect(callback)
    return button


def microphone_icon(recording=False, color="#172434"):
    """A scalable microphone outline, independent of the desktop icon theme."""
    pixmap = QPixmap(80, 80)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 4, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    if recording:
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(26, 26, 28, 28, 3, 3)
    else:
        painter.drawRoundedRect(32, 12, 16, 36, 8, 8)
        painter.drawArc(22, 28, 36, 30, 180 * 16, 180 * 16)
        painter.drawLine(40, 58, 40, 68)
    painter.end()
    return QIcon(pixmap)


def busy_icon(color):
    """The microphone drawn the same whether the button is enabled or not:
    while a recording is transcribed the button is disabled, and Qt's greyed
    version of the glyph vanished into the disc."""
    pixmap = microphone_icon(color=color).pixmap(80, 80)
    icon = QIcon()
    icon.addPixmap(pixmap, QIcon.Mode.Normal)
    icon.addPixmap(pixmap, QIcon.Mode.Disabled)
    return icon


def relative_time(stamp, now=None):
    """"Just now" for the last minute, then the way the history list says it."""
    now = now or datetime.datetime.now()
    try:
        when = datetime.datetime.fromisoformat(str(stamp).strip())
    except ValueError:
        return widgets.short_time(stamp, now)
    if datetime.timedelta(0) <= now - when < datetime.timedelta(minutes=1):
        return t("Just now")
    return widgets.short_time(stamp, now)


# The window width from which Dictation puts the recent entries beside it.
WIDE_DICTATION = 1100
# How many of them are read; the column shows as many as it has room for.
RECENT_ROWS = 30


def settings_icon(color="#B2C1D1"):
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 3, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    painter.translate(24, 24)
    painter.drawEllipse(-12, -12, 24, 24)
    painter.drawEllipse(-4, -4, 8, 8)
    for _ in range(8):
        painter.drawLine(0, -12, 0, -17)
        painter.rotate(45)
    painter.end()
    return QIcon(pixmap)


def _model_location(model, provider, local_state=None):
    model = model or t("Model not selected")
    if provider == "local":
        kind = ggml.accel_kind(local_state or {})
        location = t("Local GPU") if kind == "gpu" else t("Local CPU") if kind == "cpu" else t("Local")
    else:
        location = "CLI" if provider in ("claude", "codex", "agy") else "API"
    return f"{model} ({location})"


def processing_locations(conf, mode="dictation", file_cleanup=None, file_timestamps=None):
    """Display configured model IDs and observed local acceleration, never keys."""
    parts = processing_parts(conf, mode, file_cleanup, file_timestamps)
    location = t("Dictation: {sound} / Cleanup: {text}", sound=parts["sound"], text=parts["text"])
    if "minutes" in parts:
        location += " / " + t("Minutes: {model}", model=parts["minutes"])
    elif "assistant" in parts:
        location += " / " + t("Assistant: {model}", model=parts["assistant"])
    return location


def short_model(full):
    """"openai/whisper-1 (API)" as a chip says it: "whisper-1 · API"."""
    match = re.match(r"^(.*) \(([^()]*)\)$", full)
    if not match:
        return full
    return f"{match.group(1).rsplit('/', 1)[-1]} · {match.group(2)}"


def where_label(provider, service=""):
    """Where a job runs, in a word or two: the service, the program, or this
    computer. The model itself is for the tooltip."""
    if provider == "local":
        return t("This computer")
    names = {value: label for label, value in CLEANUP_PROVIDERS}
    names.update({"claude": "Claude Code", "codex": "Codex", "agy": "Antigravity"})
    return service or t(names.get(provider, provider))


def speech_model(conf, mode="dictation", file_timestamps=None):
    """(target, model id) for the speech model `mode` will run: the
    timestamped one for a meeting or a timestamped file. The id may be
    empty (a custom server left to pick, or nothing chosen yet)."""
    target = conf.transcribe_target()
    model = target.model
    timestamps = conf["file_timestamps"] if file_timestamps is None else file_timestamps
    if mode == "meeting" or (mode == "file" and timestamps):
        model = api.timestamp_model(target.provider, target.model, target.file_model, target.preset)
    return target, (model or "").strip()


def model_name(model, provider=""):
    """A model id the way a chip says it: "openai/whisper-1" is "whisper-1",
    the local "ggml-large-v3-turbo-q5_0.bin" is "large-v3-turbo-q5_0"."""
    name = (model or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if provider == "local":
        if name.startswith(ggml.WHISPER_PREFIX):
            name = name[len(ggml.WHISPER_PREFIX):]
        stem, dot, _suffix = name.rpartition(".")
        if dot and stem and _suffix.lower() in ("bin", "gguf"):
            name = stem
    return name


def processing_parts(conf, mode="dictation", file_cleanup=None, file_timestamps=None):
    """Each model a mode uses, as "model (where)"; keyed by its job."""
    target, sound_model = speech_model(conf, mode, file_timestamps)
    local = ggml.state()
    if target.provider == "custom" and target.preset == "other" and not sound_model:
        sound_model = t("{name} default model", name=target.service)
    sound = _model_location(sound_model, target.provider, local.get("whisper"))
    enabled = conf["cleanup_enabled"]
    if mode == "file":
        enabled = conf["file_cleanup"] if file_cleanup is None else file_cleanup
    elif mode == "meeting":
        enabled = conf["meeting_cleanup"]
    elif mode == "ask":
        enabled = conf["assistant_cleanup"]
    provider = cleanup.provider(conf)
    model = cleanup.model(conf)
    if provider in ("codex", "agy") and not conf[f"cleanup_{provider}_model"].strip():
        model = t("{name} default model", name="Codex" if provider == "codex" else "Antigravity")
    elif provider == "custom" and conf.custom_preset("cleanup") == "other" and not conf.custom_target("cleanup").model:
        model = t("{name} default model", name=t("Custom server"))
    elif provider == "custom" and not conf.custom_target("cleanup").model:
        model = t("Model not selected")
    text = _model_location(model, provider, local.get("llama")) if enabled else t("Editing off")
    parts = {"sound": sound, "text": text}
    if mode == "meeting":
        parts["minutes"] = _model_location(conf["meeting_model"], "openrouter")
    elif mode == "ask":
        provider = assistant.provider(conf)
        parts["assistant"] = _model_location(assistant_model(conf), provider)
    return parts


def assistant_model(conf):
    """The agent's model as a person reads it: a CLI or a server of your own
    left to choose is its default model, and a gateway preset with none chosen
    is empty (the caller says "not selected")."""
    provider = assistant.provider(conf)
    if provider in ("codex", "agy") and not conf[f"assistant_{provider}_model"].strip():
        return t("{name} default model", name=assistant.display_name(conf))
    if provider == "custom":
        target = conf.custom_target("assistant")
        if target.model:
            return target.model
        if target.preset == "other":
            return t("{name} default model", name=t("Custom server"))
        return ""
    return assistant.model(conf)


class CaptureButton(QPushButton):
    """The round record button, which draws a turning ring while the
    recording is being transcribed: something moves, so waiting reads as
    working rather than stuck."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._ring = QColor("#A5C7FA")
        self._spin = QTimer(self)
        self._spin.setInterval(40)
        self._spin.timeout.connect(self._turn)

    def set_ring_color(self, color):
        self._ring = QColor(color)

    def set_busy(self, busy):
        if busy and not self._spin.isActive():
            self._spin.start()
        elif not busy and self._spin.isActive():
            self._spin.stop()
            self.update()

    @property
    def busy(self):
        return self._spin.isActive()

    def _turn(self):
        self._angle = (self._angle - 12) % 360
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._spin.isActive():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._ring, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        rect = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        painter.drawArc(rect, self._angle * 16, 100 * 16)
        painter.end()


def _round(name, tooltip, callback, danger=False):
    """A round icon button beside the main one, with its words as tooltip."""
    button = QToolButton()
    button.setObjectName("roundDanger" if danger else "round")
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setIconSize(QSize(18, 18))
    button.setAutoRaise(False)
    button.clicked.connect(callback)
    button.glyph = name
    policy = button.sizePolicy()
    policy.setRetainSizeWhenHidden(True)
    button.setSizePolicy(policy)
    return button


class _FitLayout(QVBoxLayout):
    """A page layout that fills the window rather than asking for the height
    its wrapped labels would like. Those pages hold a list and a reader that
    scroll on their own; a page that scrolled as well would put the reader's
    buttons below the fold (one scroller, not two). The page still scrolls
    once the window is smaller than its minimum."""

    def hasHeightForWidth(self):
        return False


class SetupTile(QPushButton):
    """One way to get started: an icon in a tinted badge, a title and one
    line, the whole tile one button."""

    def __init__(self, glyph, title, line, callback, parent=None):
        super().__init__(parent)
        self.setObjectName("tile")
        self.setAutoDefault(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        self.setAccessibleDescription(line)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 12, 14, 12)
        row.setSpacing(14)
        row.addWidget(widgets.Badge(glyph, "accent", 40), 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = _label(title, "tiletitle")
        self.line = _label(line, "muted")
        for label in (self.title, self.line):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            text.addWidget(label)
        row.addLayout(text, 1)
        row.addWidget(widgets.GlyphLabel("chevron", "muted", 16), 0,
                      Qt.AlignmentFlag.AlignVCenter)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.clicked.connect(callback)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return max(self.layout().heightForWidth(width), self.layout().minimumSize().height())

    def sizeHint(self):
        return self.layout().sizeHint()

    def minimumSizeHint(self):
        return self.layout().minimumSize()


def conversation(rows, minutes, since=None, limit=6):
    """The assistant exchanges of the conversation still running: the newest
    command, and the ones before it that came within `minutes` of each other,
    the way the assistant itself carries a conversation on. Questions about
    a selection are answered on their own and are left out, as are commands
    from before `since` (a new conversation started here)."""
    def when(row):
        try:
            return datetime.datetime.fromisoformat(str(row.get("ts", "")).strip())
        except ValueError:
            return None
    asks = [row for row in rows if row.get("mode") == "ask" and "assist" not in row
            and not row.get("failed")]
    if since is not None:
        asks = [row for row in asks if (when(row) or since) > since]
    if not asks:
        return []
    thread = [asks[-1]]
    if minutes > 0:
        for row in reversed(asks[:-1]):
            older, newer = when(row), when(thread[0])
            if (older is None or newer is None or len(thread) >= limit
                    or (newer - older).total_seconds() > minutes * 60):
                break
            thread.insert(0, row)
    return thread


class ChatThread(QScrollArea):
    """The running conversation with the assistant: each question as a tinted
    block, each reply as a card under it, newest at the bottom."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAccessibleName(t("Conversation"))
        body = QWidget()
        body.setObjectName("thread")
        self.column = QVBoxLayout(body)
        self.column.setContentsMargins(0, 0, 4, 0)
        self.column.setSpacing(10)
        self.setWidget(body)
        self._spec = None
        self._plain = ""
        self._bubbles = []
        # Kept at the newest exchange while the cards settle their heights,
        # until the user scrolls back on purpose.
        self._stick = True
        bar = self.verticalScrollBar()
        bar.rangeChanged.connect(lambda *_: self._stick and self._to_end())
        bar.actionTriggered.connect(self._scrolled_by_hand)

    def _fit_bubbles(self):
        """Each question as wide as its text, up to three quarters of the
        thread; wrapped labels otherwise settle on a narrow guess."""
        room = int(self.viewport().width() * 0.75) - 28
        for label in self._bubbles:
            try:
                width = label.fontMetrics().horizontalAdvance(label.text()) + 4
                label.setMinimumWidth(max(40, min(width, room)))
            except RuntimeError:
                pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_bubbles()

    def _clear(self):
        self._bubbles = []
        while self.column.count():
            item = self.column.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    inner = item.layout().takeAt(0)
                    if inner.widget():
                        inner.widget().hide()
                        inner.widget().deleteLater()

    def _question(self, text):
        bubble = QFrame()
        bubble.setObjectName("bubble")
        inside = QVBoxLayout(bubble)
        inside.setContentsMargins(14, 8, 14, 8)
        label = _label(text)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setAccessibleName(t("You asked: {question}", question=text))
        inside.addWidget(label)
        bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._bubbles.append(label)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addSpacing(56)
        row.addStretch(1)
        row.addWidget(bubble)
        self.column.addLayout(row)

    def _answer(self, text, meta, dim):
        card = QFrame()
        card.setObjectName("result")
        inside = QVBoxLayout(card)
        inside.setContentsMargins(widgets.CARD_PAD, widgets.CARD_PAD - 4,
                                  widgets.CARD_PAD, widgets.CARD_PAD - 4)
        inside.setSpacing(6)
        label = _label(text, "muted" if dim else "")
        # A long answer reads at the line length every transcript does.
        label.setMaximumWidth(widgets.READING_MAX)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                      | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        label.setAccessibleName(t("Assistant reply"))
        label.setAccessibleDescription(text)
        inside.addWidget(label)
        actions = QHBoxLayout()
        actions.addWidget(_label(meta, "meta"), 1)
        copy = _button(t("Copy"), lambda: QApplication.clipboard().setText(text))
        copy.setToolTip(t("Copy this reply"))
        actions.addWidget(copy)
        inside.addLayout(actions)
        self.column.addWidget(card)

    def set_thread(self, exchanges, pending=None, busy=False, stage="", empty=""):
        """`exchanges` are (question, answer, meta). While `busy`, the earlier
        ones dim, and `pending` (the question on its way) and `stage` follow
        them."""
        spec = (tuple(exchanges), pending, busy, stage, empty)
        if spec == self._spec:
            return
        self._spec = spec
        self._clear()
        plain = []
        for question, answer, meta in exchanges:
            if question:
                self._question(question)
                plain.append(question)
            self._answer(answer, meta, busy)
            plain.append(answer)
        if busy:
            if pending:
                self._question(pending)
                plain.append(pending)
            working = _label(stage or t("Thinking…"), "muted")
            working.setContentsMargins(4, 0, 0, 0)
            self.column.addWidget(working)
        if not exchanges and not busy:
            placeholder = _label(empty, "muted", True)
            placeholder.setContentsMargins(0, 24, 0, 0)
            self.column.addWidget(placeholder)
        self.column.addStretch(1)
        self._plain = "\n\n".join(plain)
        self._fit_bubbles()
        # Newest at the bottom, and the bottom in view.
        self._stick = True
        QTimer.singleShot(0, self._to_end)

    def _scrolled_by_hand(self, *_):
        # After the scroll has moved: whether it ended at the bottom.
        QTimer.singleShot(0, self._settle_stick)

    def _settle_stick(self):
        bar = self.verticalScrollBar()
        self._stick = bar.value() >= bar.maximum()

    def _to_end(self):
        try:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())
        except RuntimeError:
            pass

    def text(self):
        return self._plain


# How long a meeting source may stay silent before the page says so.
SILENT_SECONDS = 5
# A level under this is no sound at all, as far as the meters are concerned.
QUIET = 0.05


class HomeWindow(QWidget):
    """Own navigation and presentation; recording and processing stay in Dikte."""

    MODES = (("dictation", "Dictation", "mic"), ("file", "File", "doc"),
             ("meeting", "Meeting", "meeting"), ("ask", "Assistant", "sparkle"),
             ("history", "History", "history"))

    def __init__(self, controller, settings):
        super().__init__()
        self.controller = controller
        self.conf = controller.conf
        self.settings = settings
        self.mode = "dictation"
        self._last_result = None
        self._last_answer = None
        self._busy_since = None
        self._ask_since = None
        self._failure_row = None
        self._exchanges = []
        self._reset_at = None
        self._invite = False
        self._heard_at = {}
        self._meeting_was = "idle"
        font = self.font()
        font.setPointSizeF(max(10.5, font.pointSizeF()))
        self.setFont(font)
        self.setObjectName("home")
        self.setWindowTitle("Dikte")
        theme.apply(self, self.conf["theme"])
        self._theme_name = None
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 10)
        root.setSpacing(12)
        navigation = self.navigation = QHBoxLayout()
        navigation.setSpacing(8)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        # One segmented control rather than five boxed buttons: the row says
        # where you are without weighing more than the page under it. History
        # is a segment like the others.
        self.segments = QFrame()
        self.segments.setObjectName("segments")
        segments = QHBoxLayout(self.segments)
        segments.setContentsMargins(3, 3, 3, 3)
        segments.setSpacing(2)
        for name, title, _glyph in self.MODES:
            button = _button(t(title), lambda checked=False, name=name: self.show_mode(name), "mode")
            button.setCheckable(True)
            button.setIconSize(QSize(16, 16))
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.mode_group.addButton(button)
            segments.addWidget(button, 1)
            self.mode_buttons[name] = button
        navigation.addWidget(self.segments, 1)
        self.settings_button = _button("", controller.open_settings, "settings")
        self.settings_button.setIcon(settings_icon())
        self.settings_button.setIconSize(QSize(20, 20))
        self.settings_button.setFixedSize(34, 34)
        self.settings_button.setToolTip(t("Settings"))
        self.settings_button.setAccessibleName(t("Settings"))
        navigation.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(navigation)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.mode_pages = {}
        # One width for every tab, and the tab bar above capped to the same
        # (_center_navigation), so nothing moves sideways between tabs.
        self.mode_pages["dictation"] = self._scrolled(self._capture_page())
        self._dictation_page = self.mode_pages["dictation"].widget()
        self.mode_pages["file"] = self._scrolled(self._file_page())
        self.mode_pages["meeting"] = self._scrolled(self._meeting_page())
        self.mode_pages["ask"] = self._scrolled(self._assistant_page())
        self._ask_page = self.mode_pages["ask"].widget()
        self.mode_pages["history"] = self._scrolled(self._history_page())
        for page in self.mode_pages.values():
            self.pages.addWidget(page)
        # The models in use, as chips; the full names and where each runs are
        # in the tooltip rather than in a sentence that wraps.
        self.footer = widgets.ChipRow(max_width=260)
        root.addWidget(self.footer)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        # The meters move at the pace of the sound, not of the status line;
        # this one runs only while something records.
        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(50)
        self._meter_timer.timeout.connect(self._feed_meters)
        settings.applied.connect(self.refresh)
        settings.file_cleanup.toggled.connect(self.refresh)
        settings.file_timestamps.toggled.connect(self.refresh)
        settings.transcriber.finished.connect(self.refresh_results)
        settings.history.model().rowsRemoved.connect(self.refresh_results)
        settings.history.model().modelReset.connect(self.refresh_results)
        self.resize(640, 580)
        screen = self.screen()
        if screen:
            room = screen.availableGeometry()
            self.resize(min(640, room.width() - 40), min(580, room.height() - 80))
        self.setMinimumSize(440, 380)
        self.show_mode("dictation")
        self.refresh_results()

    @staticmethod
    def _scrolled(page, cap=widgets.PAGE_MAX):
        page.setMaximumWidth(cap)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(page)
        return area

    @staticmethod
    def _follow_split(split, header):
        """One rule for every tab's head: centred over a single column, a
        toolbar row once the page's panes sit side by side."""
        header.set_centred(not split.wide)
        earlier = split.arranged

        def arranged(wide):
            if earlier is not None:
                earlier(wide)
            header.set_centred(not wide)
        split.arranged = arranged

    @staticmethod
    def _template(headline, *rows, fit=False):
        """The page every task shares: a headline (or None), then its rows."""
        page = QWidget()
        layout = _FitLayout(page) if fit else QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)
        if headline is not None:
            layout.addWidget(headline)
        for row in rows:
            if isinstance(row, tuple):
                layout.addWidget(row[0], row[1])
            elif isinstance(row, QHBoxLayout):
                layout.addLayout(row)
            elif row.maximumWidth() < 16777215:
                # Against the left edge with the title, not centred, and as
                # wide as its cap allows.
                line = QHBoxLayout()
                line.setContentsMargins(0, 0, 0, 0)
                line.addWidget(row, 1)
                line.addStretch(0)
                layout.addLayout(line)
            else:
                layout.addWidget(row)
        return page, layout

    @staticmethod
    def _centered(*items):
        row = QHBoxLayout()
        row.setSpacing(14)
        row.addStretch(1)
        for item in items:
            row.addWidget(item, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        return row

    @staticmethod
    def _card(*items):
        """A result card with the padding every card uses (widgets.card)."""
        return widgets.card(*items)

    @staticmethod
    def _meter_row(label, meter):
        row = QHBoxLayout()
        row.setSpacing(8)
        name = _label(label, "meta")
        name.setWordWrap(False)
        row.addWidget(name)
        row.addWidget(meter, 1)
        return row

    def _capture_page(self):
        self.capture_status = _label("", "heading")
        # The level of what the microphone hears, under the clock, while it
        # records: the same levels the corner indicator draws.
        self.capture_meter = widgets.LevelMeter(role="danger")
        self.capture_meter.setFixedWidth(320)
        self.capture_meter.hide()
        # What the next dictation uses: picking by the program in front, the
        # profile chosen by hand, and a translation if it makes one. A menu
        # switches it, the way Use this profile does under Settings.
        # Parented from the start: shown before it is placed in a layout,
        # a parentless chip would come up as a window of its own.
        self.profile_chip = QToolButton(self)
        self.profile_chip.setObjectName("profilechip")
        self.profile_chip.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.profile_chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.profile_chip.setIconSize(QSize(14, 14))
        self.profile_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.profile_menu = QMenu(self.profile_chip)
        self.profile_menu.aboutToShow.connect(self._fill_profile_menu)
        self.profile_chip.setMenu(self.profile_menu)
        self.profile_chip.hide()
        # The state and its meter on the capture button's axis, under the
        # button rather than over it: the title is there only while something
        # records or transcribes, and appearing over the button it would push
        # the button out from under the pointer that just pressed it.
        self.capture_header = widgets.StateHeader(self.capture_status, [self.capture_meter])
        self.capture_button = CaptureButton()
        self.capture_button.setObjectName("capture")
        self.capture_button.setAutoDefault(False)
        self.capture_button.clicked.connect(self._capture)
        self._mic_icon = microphone_icon()
        self._stop_icon = microphone_icon(recording=True)
        self.capture_button.setIcon(self._mic_icon)
        self.capture_button.setIconSize(QSize(44, 44))
        self.capture_button.setFixedSize(112, 112)
        self._compact = False
        # Pause and Discard flank the button while there is a recording to
        # pause or discard; hidden, they keep their room so it never jumps.
        self.pause_button = _round("pause", t("Pause"), self._pause)
        self.cancel_button = _round("trash", t("Discard"), self._cancel_capture, danger=True)
        self.capture_row = QWidget()
        capture_column = self._capture_column = QVBoxLayout(self.capture_row)
        capture_column.setContentsMargins(0, 8, 0, 0)
        capture_column.setSpacing(6)
        # The button row, with the profile chip at its top right corner when
        # the row has room on both sides of the button, or on a line of its
        # own above it when not (_place_profile_chip).
        self._capture_buttons = QWidget()
        self._capture_buttons.setLayout(self._centered(self.pause_button, self.capture_button,
                                                       self.cancel_button))
        self._capture_top = QGridLayout()
        self._capture_top.setContentsMargins(0, 0, 0, 0)
        self._capture_top.setVerticalSpacing(widgets.GROUP_GAP)
        self._capture_top.addWidget(self._capture_buttons, 1, 0)
        # In the layout from the start; _place_profile_chip moves it once the
        # row's width is known.
        self._capture_top.addWidget(self.profile_chip, 0, 0, Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignTop)
        self._chip_up = None
        capture_column.addLayout(self._capture_top)
        # What the button does, on it rather than in a sentence under it:
        # Record, then Stop and copy. The button only copies; the shortcut
        # is what pastes, and the line under this one says so.
        self.capture_action = _label("", "action", True)
        self.capture_action.setWordWrap(False)
        policy = self.capture_action.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.capture_action.setSizePolicy(policy)
        capture_column.addWidget(self.capture_action)
        capture_column.addWidget(self.capture_header)
        # The shortcut as keycaps and where its text goes, one line; right
        # after setup the same line carries the invitation instead.
        self.capture_hint = widgets.KeyHint()
        self.capture_shortcut = self.capture_hint
        # How the last dictation ended: a plain line when it went well, a
        # notice strip when it did not, which stays until the next one so a
        # failure is not gone with the indicator that announced it. A failure
        # offers what can be done about it right there.
        self.capture_error = widgets.StatusLine()
        self.capture_error.line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.capture_retry = _button(t("Try again"), self._retry_failure, "link")
        self.capture_error.notice.add_action(self.capture_retry)
        self.capture_details = _button(t("Show in history"), self._show_failure, "link")
        self.capture_error.notice.add_action(self.capture_details)

        # First run: nothing can transcribe yet, so the page offers the ways
        # to set that up instead of a microphone that cannot work.
        self.setup_tile = QWidget()
        tiles = QVBoxLayout(self.setup_tile)
        tiles.setContentsMargins(0, 4, 0, 0)
        tiles.setSpacing(10)
        self.welcome = _label(t("Welcome to Dikte"), "heading", True)
        tiles.addWidget(self.welcome)
        self.setup_tiles = [
            SetupTile("key", t("Cloud service"),
                      t("Paste a key from OpenRouter, OpenAI, Groq or Google."),
                      lambda: self._setup("cloud")),
            SetupTile("chip", t("On this computer"),
                      t("Transcribe audio on this computer."),
                      lambda: self._setup("local")),
            SetupTile("keyboard", t("Shortcut"), "", self._assign_shortcut),
        ]
        for tile in self.setup_tiles:
            tiles.addWidget(tile)
        # Kept under its old name: the first tile is the setup action.
        self.setup_button = self.setup_tiles[0]

        top = QHBoxLayout()
        top.addWidget(_label(t("Latest text"), "section"))
        top.addStretch(1)
        self.latest_text = QPlainTextEdit()
        widgets.cap_reading(self.latest_text)
        self.latest_text.setReadOnly(True)
        self.latest_text.setAccessibleName(t("Latest text"))
        self.latest_text.setPlaceholderText(t("Your first transcript will appear here."))
        # As tall as its text, up to a cap (_fit_latest): no reserved empty
        # lines above the actions.
        self.latest_text.setMinimumHeight(40)
        self.latest_text.setMaximumHeight(110)
        self.latest_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.latest_text.installEventFilter(self)
        self.latest_warning = _label()
        actions = QHBoxLayout()
        self.latest_time = _label("", "meta")
        actions.addWidget(self.latest_time, 1)
        self.copy_button = _button(t("Copy"), lambda: QApplication.clipboard().setText(self.latest_text.toPlainText()))
        self.open_button = _button(t("Open"), lambda: self._open_text(self.latest_text.toPlainText()))
        self.open_button.setToolTip(t("Open text"))
        # Shown only under a translation: the text above is not what was said.
        self.original_button = _button(t("Copy original"), self._copy_original)
        self.original_button.setToolTip(t(
            "Copy the transcript as it was heard, before it was translated."))
        self.original_button.hide()
        # The order every result card uses: Copy, then the rest.
        actions.addWidget(self.copy_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.original_button)
        # The stretch only counts once the card is taller than its rows: wide,
        # the card reaches down to the Recent column's bottom
        # (_stretch_latest) and its actions stay at its foot.
        self.latest_card = self._card(top, self.latest_text, self.latest_warning, actions)
        self.latest_card.layout().insertStretch(3, 1)
        widgets.roomy(self.copy_button, self.open_button, self.original_button)
        actions.setSpacing(widgets.BUTTON_GAP)
        main, layout = self._template(None, self.setup_tile, self.capture_row,
                                      self.capture_hint, self.capture_error, self.latest_card)
        layout.addStretch(1)
        self._dictation_layout = layout

        # A wide window: the latest entries beside the capture area, each one
        # a way into the history.
        self.recent_list = widgets.row_list(QListWidget(), lines=2)
        self.recent_list.setAccessibleName(t("Recent"))
        self.recent_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # As tall as the column, with as many whole rows as fit in it.
        self.recent_list.installEventFilter(self)
        self.recent_list.itemClicked.connect(self._open_recent)
        self.recent_list.itemActivated.connect(self._open_recent)
        recent_top = QHBoxLayout()
        recent_top.addWidget(_label(t("Recent"), "section"))
        recent_top.addStretch(1)
        self.recent_all = _button(t("Show all"), lambda: self.show_mode("history"), "link")
        recent_top.addWidget(self.recent_all)
        self.recent_list.flush = True
        self.recent_card = self._card(recent_top, (self.recent_list, 1))
        self.recent_panel = QWidget()
        recent_column = QVBoxLayout(self.recent_panel)
        recent_column.setContentsMargins(0, 6, 0, 0)
        recent_column.addWidget(self.recent_card, 1)
        self.recent_panel.setFixedWidth(380)
        self.recent_panel.hide()
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(28)
        row.addWidget(main, 1)
        row.addWidget(self.recent_panel, 0)
        return page

    def _file_page(self):
        # No title: the tab says what the page is, and the page's own state
        # (a file transcribing) is in its card.
        files = self.settings.task_pages["file"]
        files.show()
        page, _layout = self._template(None, (files, 1), fit=True)
        return page

    def _meeting_page(self):
        # The state and its action on one row, and one short line under it:
        # what this page is read for is the minutes, which get the height.
        self.meeting_status = _label("", "heading")
        self.meeting_hint = _label("", "muted")
        self.meeting_button = _button(t("Record a meeting"), self._meeting, "primary")
        self.meeting_cancel = _button(t("Discard"), self._cancel_meeting, "destructive")
        # While it records: both sides, each with its own meter, so a source
        # that is not arriving shows before an hour of it is lost.
        # As many bars as the indicator keeps levels, so the row is full.
        self.mic_meter = widgets.LevelMeter(bars=overlay.BARS, role="danger", height=40)
        self.system_meter = widgets.LevelMeter(bars=overlay.BARS, role="accent", height=40)
        self.mic_meter.setAccessibleName(t("Microphone"))
        self.system_meter.setAccessibleName(t("System audio"))
        # The live panel, shown in the reader while the recording's row is
        # the one picked: how long it has run, and each side as wide as the
        # reader, so a side that is not arriving shows before an hour of it
        # is lost.
        self.meeting_live = QWidget()
        live = QVBoxLayout(self.meeting_live)
        live.setContentsMargins(widgets.EDITOR_INSET, 4, widgets.EDITOR_INSET, 0)
        live.setSpacing(8)
        self.meeting_live_clock = _label("", "heading")
        live.addWidget(self.meeting_live_clock)
        for name, meter in ((t("Microphone"), self.mic_meter),
                            (t("System audio"), self.system_meter)):
            live.addSpacing(8)
            live.addWidget(_label(name, "section"))
            live.addWidget(meter)
        self.meeting_silence = _label("", "warning")
        self.meeting_silence.hide()
        live.addSpacing(4)
        live.addWidget(self.meeting_silence)
        live.addStretch(1)
        # It shares the reader with the minutes: its height must not become
        # the reader's minimum, or the list above loses its rows while
        # nothing records.
        self.meeting_live.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self.meeting_meters = self.meeting_live
        widgets.roomy(self.meeting_button, self.meeting_cancel)
        self.meeting_button.setMinimumWidth(widgets.PRIMARY_MIN)
        header = self.meeting_header = widgets.StateHeader(
            self.meeting_status, [self.meeting_hint], [self.meeting_cancel, self.meeting_button])
        self._follow_split(self.settings.minutes_split, header)
        self.meeting_error = widgets.StatusLine()
        minutes = self.settings.task_pages["minutes"]
        minutes.setMinimumHeight(230)
        minutes.show()
        self.settings.set_live_panel(self.meeting_live)
        # Open, as in History: the minutes in a window of their own, for
        # reading at length.
        self.minutes_open = _button(t("Open"), self._open_minutes)
        widgets.roomy(self.minutes_open)
        self.minutes_open.setToolTip(t("Open the minutes in a window"))
        actions = self.settings.minutes_actions
        actions.insertWidget(actions.indexOf(self.settings.minutes_copy) + 1, self.minutes_open)
        self.settings.minutes_list.currentItemChanged.connect(
            lambda *_: QTimer.singleShot(0, self._sync_minutes_open))
        self._sync_minutes_open()
        page, _layout = self._template(header, self.meeting_error, (minutes, 1), fit=True)
        return page

    def _assistant_page(self):
        self.ask_status = _label("", "heading")
        self.ask_meter = widgets.LevelMeter(role="accent")
        self.ask_meter.setFixedWidth(320)
        self.ask_meter.hide()
        # Provider, shortcut, permissions and directory as chips, one row.
        self.ask_scope = widgets.ChipRow(align=Qt.AlignmentFlag.AlignLeft)
        self.ask_button = _button("", self._ask, "primary")
        self.ask_pause = _round("pause", t("Pause"), self._pause)
        self.ask_cancel = _round("trash", t("Discard"), self._cancel_ask, danger=True)
        # A running request is stopped, not thrown away. Its button takes the
        # place and the size of the one it replaces, so nothing in the row
        # moves while the answer is on its way.
        self.ask_stop = _button(t("Cancel"), self._cancel_ask)
        self.ask_stop.setToolTip(t("Cancel request"))
        self.ask_stop.setAccessibleName(t("Cancel request"))
        self.ask_action = QStackedWidget()
        for button in (self.ask_button, self.ask_stop):
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            self.ask_action.addWidget(button)
        self.ask_action.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # In the header, hidden ones take no room: the row ends at its last
        # visible button.
        for button in (self.ask_pause, self.ask_cancel):
            policy = button.sizePolicy()
            policy.setRetainSizeWhenHidden(False)
            button.setSizePolicy(policy)
        self.ask_new = widgets.GlyphButton("plus", t("Start a new conversation"))
        # The plus says it in a word too: a lone + beside Ask by voice read
        # as "ask more".
        self.ask_new.setText(t("New"))
        self.ask_new.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.ask_new.clicked.connect(self._new_conversation)
        widgets.roomy(self.ask_button, self.ask_stop, self.ask_new)
        # The primary action of the page, wide enough to read as one.
        for button in (self.ask_button, self.ask_stop):
            button.setMinimumWidth(widgets.PRIMARY_MIN)
        header = self.ask_header = widgets.StateHeader(
            self.ask_status, [self.ask_meter, self.ask_scope],
            [self.ask_pause, self.ask_action, self.ask_cancel, self.ask_new])
        self.ask_error = widgets.StatusLine()
        self.ask_thread = ChatThread()
        main, _layout = self._template(header, self.ask_error, (self.ask_thread, 1), fit=True)

        # A wide window: how the assistant runs, beside the conversation
        # rather than squeezed into chips above it.
        self.ask_side = QFrame()
        self.ask_side.setObjectName("result")
        side = QVBoxLayout(self.ask_side)
        side.setContentsMargins(widgets.CARD_PAD, widgets.CARD_PAD - 2,
                                widgets.CARD_PAD, widgets.CARD_PAD - 2)
        side.setSpacing(10)
        side.addWidget(_label(t("How it runs"), "section"))
        self.ask_facts = QFormLayout()
        self.ask_facts.setVerticalSpacing(8)
        self.ask_facts.setHorizontalSpacing(12)
        self.ask_fact_labels = {}
        for key, title in (("provider", t("Provider")), ("model", t("Model")),
                           ("permissions", t("Permissions")), ("shortcut", t("Shortcut")),
                           ("directory", t("Working directory"))):
            value = widgets.Chip(max_width=170)
            value.setObjectName("")
            name = _label(title, "meta")
            name.setWordWrap(False)
            self.ask_facts.addRow(name, value)
            self.ask_fact_labels[key] = value
        side.addLayout(self.ask_facts)
        self.ask_side_new = _button(t("New conversation"), self._new_conversation)
        widgets.roomy(self.ask_side_new)
        side.addWidget(self.ask_side_new, 0, Qt.AlignmentFlag.AlignLeft)
        side.addStretch(1)
        self.ask_side_panel = QWidget()
        side_column = QVBoxLayout(self.ask_side_panel)
        side_column.setContentsMargins(0, 6, 0, 0)
        side_column.addWidget(self.ask_side)
        side_column.addStretch(1)
        self.ask_side_panel.setFixedWidth(300)
        self.ask_side_panel.hide()
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(28)
        row.addWidget(main, 1)
        row.addWidget(self.ask_side_panel, 0)
        return page

    def _history_page(self):
        # No title and no reader: the list is the page, its search over its
        # whole width, and each row copies itself.
        history = self.settings.task_pages["history"]
        history.show()
        self.settings.history_action.connect(self._history_action)
        page, _layout = self._template(None, (history, 1), fit=True)
        return page

    def _history_action(self, action):
        """What a history row asks of the window: its text in a window of its
        own, or for a failed entry with no recording left, say it again or
        look at the connection."""
        if action == "open":
            self._open_selected()
        elif action == "check":
            self._open_setup("api")
        elif action == "record":
            self.show_mode("dictation")
            self._capture()

    def _show_failure(self):
        """The newest failed dictation, selected in the history."""
        self._select_in_history(lambda row: bool(row.get("failed")))

    def _select_in_history(self, wanted):
        """History, with the newest row `wanted` accepts selected and
        scrolled into view; the search is cleared so the row is there."""
        self.show_mode("history")
        self.settings.history_search.clear()
        history = self.settings.history
        for number in range(history.count()):
            item = history.item(number)
            if wanted(item.data(Qt.ItemDataRole.UserRole) or {}):
                history.clearSelection()
                history.setCurrentRow(number)
                history.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
                history.setFocus()
                break

    def _retry_failure(self):
        """The banner's own action: the kept recording again, or a new one."""
        row = self._failure_row
        if row and _retryable(row):
            messages = getattr(self.controller, "home_messages", None)
            if isinstance(messages, dict):
                messages["dictation"] = ""
            self.settings.retry_requested.emit(row)
        else:
            self._capture()
        self.refresh()

    def _copy_original(self):
        QApplication.clipboard().setText(self._last_result.get("raw", ""))

    def _open_selected(self, *_):
        rows = self.settings._selected_rows()
        if rows:
            self._open_text("\n\n".join(row.get("text", "") for row in rows
                                         if not row.get("failed")))

    def _sync_minutes_open(self):
        try:
            self.minutes_open.setEnabled(self.settings.minutes_copy.isEnabled())
        except RuntimeError:
            pass

    def _open_minutes(self):
        self._open_text(self.settings._minutes_text, markdown=True, title=t("Minutes"))

    def _open_text(self, text, markdown=False, title=""):
        if not text:
            return
        document = QDialog(self)
        document.setWindowTitle(title or t("Transcript"))
        document.resize(680, 560)
        theme.apply(document, self.conf["theme"])
        layout = QVBoxLayout(document)
        if markdown:
            editor = widgets.guard_links(QTextBrowser())
            editor.setHtml(self.settings._minutes_html(text, editor.font()))
        else:
            editor = QPlainTextEdit()
            editor.setReadOnly(True)
            editor.setPlainText(text)
        widgets.cap_reading(editor)
        layout.addWidget(editor)
        layout.addWidget(_button(t("Copy"), lambda: QApplication.clipboard().setText(text)),
                         0, Qt.AlignmentFlag.AlignRight)
        document.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        document.show()

    def show_mode(self, mode):
        if mode not in self.mode_pages:
            return
        self.mode = mode
        self.pages.setCurrentWidget(self.mode_pages[mode])
        self.mode_buttons[mode].setChecked(True)
        # Read again on the way in, so the lists need no Reload button.
        if mode == "history":
            self.settings._load_history()
        elif mode == "meeting":
            self.settings._load_minutes()
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        self.refresh_results()
        self.refresh()

    def hideEvent(self, event):
        self._timer.stop()
        self._meter_timer.stop()
        super().hideEvent(event)

    def refresh_results(self, *_):
        rows = cfg.read_history(self.conf["history_limit"])
        # A failed dictation has no text to show; the failure itself is on the
        # status line, and its retry lives in the history.
        result = next((row for row in reversed(rows)
                       if row.get("mode") != "ask" and not row.get("failed")), {})
        answer = next((row for row in reversed(rows) if row.get("mode") == "ask"), {})
        # The newest dictation, if it failed: what the banner's action acts on.
        newest = next((row for row in reversed(rows) if row.get("mode") != "ask"), {})
        self._failure_row = newest if newest.get("failed") else None
        self._exchanges = [
            (row.get("question") or row.get("raw") or "", row.get("text") or "",
             widgets.meta_line(relative_time(row["ts"]) if row.get("ts") else "",
                               agent_name(row.get("assistant"), "")))
            for row in conversation(rows, self.conf["assistant_session_minutes"],
                                    self._reset_at)]
        self._set_result(result, answer)
        self._fill_recent(list(reversed(rows))[:RECENT_ROWS])
        self._show_thread()

    def _fill_recent(self, rows):
        keys = [cfg._row_key(row) for row in rows]
        if keys == getattr(self, "_recent_keys", None):
            return
        self._recent_keys = keys
        self.recent_list.clear()
        for row in rows:
            text = history_title(row)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setData(widgets.META_ROLE, history_brief(row))
            item.setData(widgets.BODY_ROLE, text)
            item.setData(widgets.KIND_ROLE, history_kind(row))
            item.setToolTip(t("Open in History"))
            self.recent_list.addItem(item)
        self.recent_card.setVisible(bool(rows))
        self._fit_recent()

    def _fit_recent(self):
        """As many whole rows as the column has room for: no scroll bar, no
        row cut in half."""
        try:
            room = self.recent_list.viewport().height()
            used = 0
            for row in range(self.recent_list.count()):
                used += self.recent_list.sizeHintForRow(row)
                self.recent_list.setRowHidden(row, used > room)
        except RuntimeError:
            pass  # the window went while the timer ran

    def _open_recent(self, item):
        """The entry clicked in Recent, selected in the history."""
        key = cfg._row_key(item.data(Qt.ItemDataRole.UserRole) or {})
        self._select_in_history(lambda row: cfg._row_key(row) == key)

    def _arrange_dictation(self):
        """Recent entries beside the capture area once the window is wide, and
        how the assistant runs beside its conversation."""
        wide = self.width() >= WIDE_DICTATION
        if wide != self.recent_panel.isVisibleTo(self._dictation_page):
            self.recent_panel.setVisible(wide)
            # Wide, the latest text may be taller before it scrolls, but it
            # stays as tall as its text: the Recent column beside it is what
            # takes the height of the window.
            self.latest_text.setMaximumHeight(360 if wide else 110)
            self._stretch_latest(wide)
        QTimer.singleShot(0, self._fit_latest)
        QTimer.singleShot(0, self._place_profile_chip)
        if wide != self.ask_side_panel.isVisibleTo(self._ask_page):
            self.ask_side_panel.setVisible(wide)
            self.ask_header.set_centred(not wide)
            # The side column says it all; the chips and the small button
            # above the conversation would only repeat it.
            self.ask_scope.setVisible(not wide)
            self.refresh()
        if wide:
            QTimer.singleShot(0, self._fit_recent)

    def _stretch_latest(self, wide):
        """Wide, the Latest text card ends where the Recent card beside it
        does; narrow, it is as tall as its rows and the page ends under it."""
        layout = self._dictation_layout
        for index in range(layout.count()):
            item = layout.itemAt(index)
            holds = item.widget() is self.latest_card or (
                item.layout() is not None and item.layout().indexOf(self.latest_card) >= 0)
            if holds:
                layout.setStretch(index, 1 if wide else 0)
        layout.setStretch(layout.count() - 1, 0 if wide else 1)

    def _center_navigation(self):
        """The tab bar as wide as the pages under it, centred over them."""
        margins = self.layout().contentsMargins()
        room = self.width() - margins.left() - margins.right()
        side = max(0, (room - widgets.PAGE_MAX) // 2)
        self.navigation.setContentsMargins(side, 0, side, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._center_navigation()
        self._arrange_dictation()

    def eventFilter(self, watched, event):
        if watched is getattr(self, "latest_text", None) and event.type() == QEvent.Type.Resize:
            if event.size().width() != event.oldSize().width():
                QTimer.singleShot(0, self._fit_latest)
        elif watched is getattr(self, "recent_list", None) and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._fit_recent)
        return super().eventFilter(watched, event)

    def _set_result(self, result, answer):
        if result != self._last_result:
            self._last_result = dict(result)
            self.latest_text.setPlainText(result.get("text", ""))
            QTimer.singleShot(0, self._fit_latest)
            # When, in a word, and the marks that change how the text reads:
            # the profile and a translation. The exact time and the program
            # are the history's.
            self.latest_time.setText(widgets.meta_line(
                relative_time(result["ts"]) if result.get("ts") else "",
                *history_context(result)))
            self.original_button.setVisible(bool(result.get("translate_to")
                                                 and result.get("raw")))
            self.latest_warning.setText(result.get("cleanup_error", ""))
            self.latest_warning.setVisible(bool(self.latest_warning.text()))
            self.copy_button.setEnabled(bool(result.get("text")))
            self.open_button.setEnabled(bool(result.get("text")))
        if answer != self._last_answer:
            self._last_answer = dict(answer)

    def _restyle(self):
        """Theme-coloured glyphs, drawn again for the theme now in use."""
        theme.apply(self, self._theme_name)
        colors = theme.palette(self._theme_name)
        self._mic_icon = microphone_icon(color=colors["accent_text"])
        self._stop_icon = microphone_icon(recording=True, color=colors["accent_text"])
        self.settings_button.setIcon(settings_icon(colors["muted"]))
        self.capture_button.set_ring_color(colors["text"])
        for name, _title, glyph in self.MODES:
            icon = QIcon()
            for ratio in (1.0, 2.0):
                icon.addPixmap(widgets.glyph_pixmap(glyph, colors["muted"], 16, ratio),
                               QIcon.Mode.Normal, QIcon.State.Off)
                icon.addPixmap(widgets.glyph_pixmap(glyph, colors["accent"], 16, ratio),
                               QIcon.Mode.Normal, QIcon.State.On)
            self.mode_buttons[name].setIcon(icon)
        self._busy_icon = busy_icon(colors["text"])
        self.profile_chip.setIcon(widgets.glyph("profile", colors["muted"], 14))
        for button in (self.pause_button, self.cancel_button, self.ask_pause, self.ask_cancel):
            role = "danger" if button.objectName() == "roundDanger" else "text"
            button.setIcon(widgets.glyph(button.glyph, colors[role], 18))
        for painted in self.findChildren((widgets.GlyphLabel, widgets.Badge, widgets.GlyphButton)):
            painted.restyle()
        for meter in self.findChildren(widgets.LevelMeter):
            meter.update()

    @staticmethod
    def _clock(seconds):
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    @staticmethod
    def _announce(label, words):
        """What a state title says to a screen reader: the state in words,
        changed only when the state changes. A label with a name of its own
        is not re-announced as its clock ticks."""
        if label.accessibleName() != words:
            label.setAccessibleName(words)

    def _shortcut_words(self):
        # As a keyboard says it, whatever case it was stored in.
        return " + ".join(part.strip()[:1].upper() + part.strip()[1:]
                          for part in self.conf["shortcut"].split("+") if part.strip())

    def refresh(self, *_):
        app, conf = self.controller, self.conf
        if self._theme_name != conf["theme"]:
            self._theme_name = conf["theme"]
            self._restyle()
        messages = getattr(app, "home_messages", {})
        ready = conf.transcribe_ready()
        recording = app.state == "recording"
        busy = app.state == "busy"
        if busy and self._busy_since is None:
            self._busy_since = time.monotonic()
        elif not busy:
            self._busy_since = None
        # Idle, the page has no title: the tab already says where you are.
        # A recording or a transcription says so, with its clock; first run
        # has its welcome over the setup tiles.
        title = ""
        words = t("Ready to speak") if ready else t("Welcome to Dikte")
        if recording:
            seconds = int(app._recorded_seconds())
            title = words = t("Paused") if app.paused else t("Recording")
            title += "  " + self._clock(seconds)
        elif busy:
            title = words = messages.get("dictation_stage") or t("Transcribing…")
            title += "  " + t("{seconds} s", seconds=int(time.monotonic() - self._busy_since))
        self.capture_status.setText(title)
        self._announce(self.capture_status, words)
        # One action, said the same on the button, in its tooltip and to a
        # screen reader: what pressing the button does now. Pressing it puts
        # this window in front of whatever the text was meant for, so the
        # button always stops and copies (_capture); pasting is the
        # shortcut's, and the line under the button says what it does.
        if recording:
            action = t("Stop and copy")
        else:
            action = t("Record") if ready else t("Set up transcription")
        self.capture_button.setAccessibleName(action)
        self.capture_button.setIcon(self._stop_icon if recording else
                                    self._busy_icon if busy else self._mic_icon)
        self.capture_button.setToolTip(action)
        first_run = not ready and not recording and not busy
        self.capture_button.setEnabled((recording or not app.recording) and not first_run)
        self.capture_button.set_busy(busy)
        self.capture_action.setText(action)
        self.capture_action.setVisible(True)
        # First run: the ways to set up, and none of what they unlock yet.
        self.capture_row.setVisible(not first_run)
        self.setup_tile.setVisible(first_run)
        self.latest_card.setVisible(not first_run)
        self._first_run = first_run
        shortcut = self._shortcut_words()
        self.setup_tiles[2].line.setText(
            t("{shortcut} starts and stops dictation in any program.", shortcut=shortcut))
        self.pause_button.setVisible(recording)
        self.cancel_button.setVisible(recording)
        self.capture_meter.setVisible(recording)
        # Paused: the meter holds still in the paused colour, as the corner
        # indicator does, rather than looking like a live red capture.
        self.capture_meter.set_role("warn" if recording and app.paused else "danger")
        pause = t("Resume") if app.paused else t("Pause")
        self.pause_button.setToolTip(pause)
        self.pause_button.setAccessibleName(pause)
        colors = theme.palette(self._theme_name)
        glyph = "play" if app.paused else "pause"
        if self.pause_button.glyph != glyph:
            self.pause_button.glyph = glyph
            self.pause_button.setIcon(widgets.glyph(glyph, colors["text"], 18))
        warning = ""
        if conf["cleanup_enabled"] and conf["cleanup_provider"] == "local" and not conf.local_llm_ready():
            warning = t("The local editing model is missing. Set it up in Settings; the original transcript is kept if editing fails.")
        message = messages.get("dictation", "")
        if message or recording or busy:
            self._invite = False
        # Right after setup the shortcut line is the invitation, in its own
        # place rather than as a second line under it.
        from_button = getattr(app, "paste_override", {}).get("dictation") is False
        self._show_hint(invite=self._invite and not message and not warning,
                        recording=recording, pastes=conf["auto_paste"] and not from_button)
        failed = self._say(self.capture_error, message, warning,
                           t("The local editing model is missing."))
        # A notice strip under the button (a failure or a warning, not the
        # plain line a finished dictation leaves): the capture area gives up
        # the room it takes, so the latest text and its actions stay in view.
        self._compact_capture(not self.capture_error.notice.isHidden()
                              and not self.capture_error.isHidden()
                              and not recording and not busy)
        # While the text is on its way the shortcut has nothing to do; its
        # line goes, keeping its height so nothing below it jumps.
        self._set_hint_shown(ready and not busy and not self._compact, keep=busy)
        self._refresh_profile_chip(first_run)
        kept = bool(failed and self._failure_row and _retryable(self._failure_row))
        self.capture_retry.setText(t("Try again") if kept else t("Record again"))
        self.capture_retry.setToolTip(
            t("Transcribe the kept recording again.") if kept
            else t("The recording is gone; dictate it again."))
        self.capture_retry.setVisible(failed and ready)
        self.capture_details.setVisible(failed)
        self._refresh_models()
        self._refresh_meeting(messages, ready)
        self._refresh_ask(messages, ready)
        # Titles and the buttons beside them changed: the corner chip and the
        # action rows find their place again.
        for header in (self.capture_header, self.meeting_header, self.ask_header):
            header.fit()
        live = recording or app.meeting_state == "recording" or app.ask_state == "recording"
        if live and not self._meter_timer.isActive() and self.isVisible():
            self._meter_timer.start()
        elif not live and self._meter_timer.isActive():
            self._meter_timer.stop()

    def _keys(self):
        """The dictation shortcut as the keys printed on a keyboard."""
        return [part for part in self._shortcut_words().split(" + ") if part]

    def _destination(self, shortcut, recording=False, pastes=None):
        """What the shortcut does, in the words after its keycaps: how it
        starts and stops, or while a recording runs, how it stops this one."""
        if not shortcut:
            return t("Assign a shortcut to dictate into any program.")
        pastes = self.conf["auto_paste"] if pastes is None else pastes
        if recording:
            return t("to stop and paste") if pastes else t("to stop and copy")
        if self.conf["auto_paste"]:
            return t("to start, again to stop and paste")
        return t("to start, again to stop and copy")

    def _show_hint(self, invite=False, recording=False, pastes=None):
        """The line under the record button: the shortcut and where its text
        goes, or, just after setup, the invitation to try it."""
        keys = self._keys()
        if invite and keys:
            self.capture_hint.set(keys, t("in any program and start talking."),
                                  before=t("All set. Press"))
        else:
            self.capture_hint.set(keys, self._destination(" + ".join(keys), recording, pastes))

    def _set_hint_shown(self, shown, keep=False):
        """Show or hide the shortcut line; with `keep` it holds its height."""
        policy = self.capture_hint.sizePolicy()
        if policy.retainSizeWhenHidden() != keep:
            policy.setRetainSizeWhenHidden(keep)
            self.capture_hint.setSizePolicy(policy)
        self.capture_hint.setVisible(shown)

    def _compact_capture(self, compact):
        """A smaller record button and tighter spacing while a notice is
        shown under it, so the page fits its default size without a scroll
        bar; the full size again once the notice has gone."""
        if compact == self._compact:
            return
        self._compact = compact
        size, icon = (72, 30) if compact else (112, 44)
        self.capture_button.setFixedSize(size, size)
        self.capture_button.setIconSize(QSize(icon, icon))
        # The stylesheet draws the disc; its radius goes with the size.
        self.capture_button.setProperty("compact", compact)
        self.capture_button.style().unpolish(self.capture_button)
        self.capture_button.style().polish(self.capture_button)
        self._capture_column.setContentsMargins(0, 0 if compact else 8, 0, 0)
        self._capture_column.setSpacing(4 if compact else 6)

    def _fit_latest(self):
        """The latest text as tall as its lines, up to a cap: a short
        dictation leaves no empty lines above its actions."""
        try:
            editor = self.latest_text
            text = editor.toPlainText() or editor.placeholderText()
            room = editor.viewport().width() - 2 * editor.document().documentMargin()
            metrics = editor.fontMetrics()
            height = metrics.boundingRect(0, 0, max(40, int(room)), 100000,
                                          int(Qt.TextFlag.TextWordWrap), text).height()
            chrome = editor.frameWidth() * 2 + editor.contentsMargins().top() \
                + editor.contentsMargins().bottom() + 2 * editor.document().documentMargin() + 2
            wide = self.recent_panel.isVisibleTo(self._dictation_page)
            cap = 360 if wide else 110
            editor.setFixedHeight(int(max(metrics.lineSpacing() + chrome,
                                          min(cap, height + chrome))))
        except RuntimeError:
            pass  # the window went while the timer ran

    def _place_profile_chip(self):
        """The profile chip at the top right of the capture button's row when
        the row has room for it on both sides of the button, so the button
        stays on the page's axis; on a line of its own above it when not."""
        try:
            chip = self.profile_chip
            need = (self._capture_buttons.sizeHint().width()
                    + 2 * (chip.sizeHint().width() + widgets.GROUP_GAP))
            up = self.capture_row.width() >= need
            if up == self._chip_up:
                return
            self._chip_up = up
            self._capture_top.removeWidget(chip)
            self._capture_top.addWidget(chip, 1 if up else 0, 0,
                                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        except RuntimeError:
            pass  # the window went while the timer ran

    def _profile_choice(self):
        """What the next dictation uses: (words on the chip, tooltip), from
        the settings as they are stored, never from the last result."""
        conf = self.conf
        named = profiles.named_profiles(conf)
        chosen = profiles.override(conf)
        from .profiles_ui import ProfilesEditor
        automatic = (not chosen and conf["profile_by_app"]
                     and ProfilesEditor.matching_available()
                     and any(p["apps"] or p["sites"] for p in named))
        if chosen:
            name, profile = chosen["name"], chosen
        elif automatic:
            name, profile = t("Automatic"), None
        else:
            name, profile = t("Default"), profiles.default_profile(conf)
        words = name
        code = profile.get("translate_to") if profile else ""
        if code and conf["translation_enabled"]:
            words = f"{name} \u2192 {t(profiles.language_name(code))}"
        tip = (t("Picks a profile by the program in front.") if automatic
               else t("Next dictation uses: {profile}", profile=words))
        return words, tip, bool(named or code)

    def _refresh_profile_chip(self, first_run=False):
        words, tip, useful = self._profile_choice()
        self.profile_chip.setVisible(useful and not first_run)
        if self.profile_chip.text() != words:
            self.profile_chip.setText(words)
            self._chip_up = None
        self._place_profile_chip()
        self.profile_chip.setToolTip(tip)
        self.profile_chip.setAccessibleName(t("Profile for the next dictation: {profile}",
                                              profile=words))

    def _fill_profile_menu(self):
        """The menu under the chip: back to Automatic or Default, each
        named profile, and the way to the profiles themselves."""
        menu = self.profile_menu
        menu.clear()
        # One group per opening; the last one goes with it, or they pile up.
        if getattr(self, "_profile_group", None) is not None:
            self._profile_group.deleteLater()
        group = self._profile_group = QActionGroup(menu)
        group.setExclusive(True)
        conf = self.conf
        chosen = profiles.override(conf)
        from .profiles_ui import ProfilesEditor
        automatic = (conf["profile_by_app"] and ProfilesEditor.matching_available()
                     and any(p["apps"] or p["sites"] for p in profiles.named_profiles(conf)))
        entries = [(t("Automatic") if automatic else t("Default"), "")]
        entries += [(p["name"], p["name"]) for p in profiles.named_profiles(conf)]
        for label, name in entries:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked((chosen["name"] if chosen else "") == name)
            group.addAction(action)
            action.triggered.connect(lambda _=False, name=name: self._use_profile(name))
        menu.addSeparator()
        menu.addAction(t("Edit profiles…"), lambda: self._open_setup("profiles"))

    def _use_profile(self, name):
        self.settings.use_profile_now(name)
        self.refresh()

    def _feed_meters(self):
        """Hand the meters the levels the indicators are drawing right now."""
        app = self.controller
        overlay = getattr(app, "overlay", None)
        # Paused, the meter holds the last levels it had (refresh greys it).
        if (app.state == "recording" and getattr(app, "recorder_owner", "") != "ask"
                and not app.paused):
            self.capture_meter.set_levels(getattr(overlay, "levels", ()))
        if app.ask_state == "recording":
            self.ask_meter.set_levels(getattr(getattr(app, "ask_overlay", None), "levels", ()))
        if app.meeting_state == "recording":
            self.mic_meter.set_levels(getattr(overlay, "levels", ()))
            self.system_meter.set_levels(getattr(overlay, "levels2", ()))
            now = time.monotonic()
            for key, meter in (("mine", self.mic_meter), ("theirs", self.system_meter)):
                if meter.peak() > QUIET:
                    self._heard_at[key] = now

    @staticmethod
    def _say(status, message, warning="", brief=""):
        """How a run ended, on `status`: a failure or a warning as a notice
        strip (the whole message in its tooltip), anything else as a plain
        line. True when it was a failure."""
        failure = t("Failed: {error}", error="")
        caution = t("Completed with a warning: {error}", error="")
        if message.startswith(failure):
            detail = message[len(failure):].strip()
            reason = detail.splitlines()[0].strip() if detail else ""
            status.set("", t("Transcription failed. {reason}", reason=reason).strip()
                       if reason else t("Transcription failed"), detail or message, "danger")
            status._full = message
            return True
        if message.startswith(caution):
            status.set("", message[len(caution):].strip().split("\n")[0], message)
            return False
        if message:
            status.set(message)
        elif warning:
            status.set("", brief or warning, warning)
        else:
            status.set()
        return False

    def _refresh_models(self):
        conf = self.conf
        mode = self.mode if self.mode in ("file", "meeting", "ask") else "dictation"
        file_cleanup = self.settings.file_cleanup.isChecked()
        file_timestamps = self.settings.file_timestamps.isChecked()
        details = processing_locations(conf, mode, file_cleanup, file_timestamps)
        target, model = speech_model(conf, mode, file_timestamps)
        # One chip on every tab: the speech model that will run, by its
        # short name. Where it runs, the full ids and the rest of the chain
        # (cleanup, minutes, the assistant) are in its tooltip.
        if model:
            name = model_name(model, target.provider)
        elif target.provider == "custom" and target.preset in ("", "other"):
            # Only a server of your own picks for itself; a gateway preset
            # with no model is one still to be chosen.
            name = t("Server default")
        else:
            name = t("Model not selected")
        where = where_label(target.provider, "" if target.provider == "local"
                            else t(target.service))
        tooltip = "\n".join([t("Provider: {service}", service=where)]
                            + [part.strip() for part in details.split(" / ")])
        self.footer.set_chips([("mic", t("Speech: {model}", model=name), tooltip)], tooltip)
        self.footer.setVisible(self.mode != "history" and not getattr(self, "_first_run", False))

    def _refresh_meeting(self, messages, ready):
        app = self.controller
        state = app.meeting_state
        supported = audio.sound().meetings
        hint = t("Records your microphone and the other side.")
        tip = t("Record your microphone and the other participants. Use headphones.")
        if not supported:
            hint = tip = t("Meeting recording is not supported on this system. You can still transcribe a file.")
        elif audio.sound() is audio.COREAUDIO:
            hint = t("Needs BlackHole or Loopback on macOS.")
            tip = t("On macOS, set up BlackHole or Loopback and select the system audio source in Settings first.")
        # Idle, no title: the tab names the page. Recording and writing up
        # are states, and say so.
        title, words = "", t("Meeting")
        recording = state == "recording"
        if recording:
            seconds = int(app.meeting_elapsed.elapsed() / 1000)
            title = words = t("Recording")
            title += "  " + self._clock(seconds)
            if self._meeting_was != "recording":
                # Both sides start out as heard, so the warning waits the
                # full stretch before it speaks up.
                now = time.monotonic()
                self._heard_at = {"mine": now, "theirs": now}
            self.settings.set_live_meeting(t("Recording now"), self._clock(seconds))
        else:
            self.settings.set_live_meeting("")
        self._meeting_was = state
        if state == "working":
            title = words = app.meeting_message or t("Writing the meeting up…")
        if supported and not self.conf.openrouter_key():
            hint = t("Connect OpenRouter in Settings to write minutes.")
            tip = t("Connect OpenRouter in Settings to write minutes. The recording is kept if writing fails.")
        self.meeting_status.setText(title)
        self._announce(self.meeting_status, words)
        self.meeting_hint.setText(hint)
        self.meeting_hint.setToolTip(tip)
        self.meeting_hint.setVisible(not recording)
        if recording:
            self.meeting_live_clock.setText(self._clock(seconds))
        silent = self._silent_sources() if recording else ""
        self.meeting_silence.setText(silent)
        self.meeting_silence.setVisible(bool(silent))
        self.meeting_button.setText(t("Stop and write up") if recording else t("Record a meeting") if ready else t("Set up transcription"))
        self.meeting_button.setEnabled(supported and state != "working")
        self.meeting_cancel.setVisible(recording)
        self._say(self.meeting_error, messages.get("meeting", ""))

    def _silent_sources(self):
        now = time.monotonic()
        quiet = {key for key, when in self._heard_at.items() if now - when >= SILENT_SECONDS}
        if quiet == {"mine", "theirs"}:
            return t("No sound from either side. Check the sources in Settings.")
        if "mine" in quiet:
            return t("No sound from the microphone.")
        if "theirs" in quiet:
            return t("No system audio yet. Is the other side playing?")
        return ""

    def _refresh_ask(self, messages, ready):
        app, conf = self.controller, self.conf
        provider = conf["assistant_provider"]
        if provider == "claude":
            permission = {"auto": t("Automatic permission decisions"),
                          "manual": t("Only actions that need no permission"),
                          "bypassPermissions": t("All permissions allowed")}.get(conf["assistant_permission_mode"], conf["assistant_permission_mode"])
        elif provider == "codex":
            permission = {"workspace-write": t("Read files; write in the working directory"),
                          "read-only": t("Read only"),
                          "danger-full-access": t("No sandbox at all")}.get(conf["assistant_codex_sandbox"], conf["assistant_codex_sandbox"])
        elif provider == "agy":
            permission = t("Uses the CLI's configured permissions")
        else:
            permission = t("Chat provider; no local command execution")
        # The chip says it in a word or two; its tooltip in full.
        brief = {"auto": t("Auto"), "manual": t("Safe only"),
                 "bypassPermissions": t("All allowed"),
                 "workspace-write": t("Workspace"), "read-only": t("Read only"),
                 "danger-full-access": t("No sandbox")}
        if provider == "claude":
            short = brief.get(conf["assistant_permission_mode"], permission)
        elif provider == "codex":
            short = brief.get(conf["assistant_codex_sandbox"], permission)
        elif provider == "agy":
            short = t("CLI rules")
        else:
            short = t("Chat only")
        shortcut = conf["assistant_shortcut"]
        if shortcut:
            key_chip = ("keyboard", shortcut, t("Shortcut: {shortcut}", shortcut=shortcut))
        else:
            # Nothing to show, so the chip is the way to set one.
            key_chip = ("keyboard", t("Assign shortcut"),
                        t("No shortcut yet. Opens the Shortcuts settings."),
                        # A bound method, not a lambda: the row compares its
                        # chips to skip rebuilding them on every refresh.
                        self._assign_shortcut)
        # The provider is the footer's; here only what this tab adds.
        chips = [("key", short, t("Permissions: {permission}", permission=permission)),
                 key_chip]
        directory = ""
        if provider in ("claude", "codex", "agy"):
            directory = assistant.working_dir(conf)
            chips.append(("doc", directory, t("Working directory: {directory}", directory=directory)))
        self.ask_scope.set_chips(chips, t(
            "A spoken command goes to the assistant. Its reply is copied without automatic pasting."))
        model = assistant_model(conf) or t("Model not selected")
        facts = {"provider": assistant.display_name(conf), "model": model,
                 "permissions": short, "shortcut": shortcut or t("Not assigned"),
                 "directory": directory}
        tips = {"permissions": permission}
        for key, value in facts.items():
            self.ask_fact_labels[key].set(value, tips.get(key, value))
        self.ask_facts.setRowVisible(self.ask_fact_labels["directory"], bool(directory))
        state = app.ask_state
        recording = state == "recording"
        busy = state == "busy"
        if busy and self._ask_since is None:
            self._ask_since = time.monotonic()
        elif not busy:
            self._ask_since = None
        # Idle, no title: the tab names the page.
        title, words = "", t("Assistant")
        if recording:
            title = words = t("Paused") if app.paused else t("Recording")
            title += "  " + self._clock(int(app._recorded_seconds()))
        elif busy:
            title = words = messages.get("ask_stage", t("Working…"))
            title += "  " + t("{seconds} s", seconds=int(time.monotonic() - self._ask_since))
        self.ask_status.setText(title)
        self._announce(self.ask_status, words)
        self.ask_meter.setVisible(recording)
        available = self._assistant_available()
        self.ask_button.setText(t("Stop and send") if recording else t("Set up transcription") if not ready else t("Ask by voice") if available else t("Set up assistant"))
        self.ask_button.setEnabled(recording or (state == "idle" and not app.recording))
        self.ask_action.setCurrentWidget(self.ask_stop if busy else self.ask_button)
        self.ask_pause.setVisible(recording)
        pause = t("Resume") if app.paused else t("Pause")
        self.ask_pause.setToolTip(pause)
        self.ask_pause.setAccessibleName(pause)
        glyph = "play" if app.paused else "pause"
        if self.ask_pause.glyph != glyph:
            self.ask_pause.glyph = glyph
            self.ask_pause.setIcon(widgets.glyph(glyph, theme.palette(self._theme_name)["text"], 18))
        # The bin throws a recording away; the X stops a request.
        self.ask_cancel.setVisible(recording)
        self.ask_stop.setVisible(busy)
        wide = self.ask_side_panel.isVisibleTo(self._ask_page)
        self.ask_new.setVisible(not recording and not wide)
        self.ask_new.setEnabled(not busy)
        self.ask_side_new.setEnabled(not busy and not recording)
        self._show_thread()
        self._say(self.ask_error, messages.get("ask", ""),
                  "" if available else t("Install the selected assistant CLI or configure its connection in Settings."),
                  t("The assistant is not set up yet."))

    def _show_thread(self):
        """The conversation: the exchanges so far, and while the next answer
        is on its way, its question and where it has got to."""
        messages = getattr(self.controller, "home_messages", {})
        busy = self.controller.ask_state == "busy"
        self.ask_thread.set_thread(
            self._exchanges, pending=messages.get("ask_question") if busy else None,
            busy=busy, stage=messages.get("ask_stage", "") if busy else "",
            empty=t("Ask by voice; the conversation shows here."))

    def _new_conversation(self):
        self.controller.reset_conversation()
        # What was said before belongs to the conversation that just ended.
        self._reset_at = datetime.datetime.now()
        self.refresh_results()
        self.refresh()

    def _assistant_available(self):
        provider = self.conf["assistant_provider"]
        binary = assistant.executable(provider)
        if binary:
            return bool(shutil.which(binary))
        if provider == "custom":
            target = self.conf.custom_target("assistant")
            return bool(target.base_url) and (target.preset == "other" or bool(target.api_key))
        return bool(self.conf.openrouter_key())

    def _assign_shortcut(self):
        self._open_setup("shortcuts")

    def _open_setup(self, page="api"):
        """Settings, opened on the page that fixes what is missing."""
        self.settings.show_page(page)
        self.controller.open_settings()

    def _setup(self, route):
        """The focused setup for one way of transcribing."""
        # Not deleted on close: a download it started may still be winding
        # down and reporting to it. The previous one goes once it is idle.
        previous = getattr(self, "_setup_dialog", None)
        if previous is not None and not previous.busy():
            previous.deleteLater()
        dialog = SetupDialog(self.settings, route, self, open_full=self.controller.open_settings)
        dialog.finished_setup.connect(self._set_up)
        self._setup_dialog = dialog
        dialog.open()
        return dialog

    def _set_up(self):
        """Back from a setup that worked: the microphone, and an invitation."""
        self._invite = True
        self.show_mode("dictation")
        self.refresh()

    def _capture(self):
        app = self.controller
        if app.state == "recording":
            # Clicking Stop puts this window in front of the original target.
            app.paste_override["dictation"] = False
            app.stop()
        elif not app.recording:
            if not self.conf.transcribe_ready():
                self._open_setup("api")
                return
            app.paste_override["dictation"] = False
            app.start()
            if app.state != "recording":
                app.paste_override.pop("dictation", None)
        self.refresh()

    def _ask(self):
        app = self.controller
        if app.ask_state == "recording":
            app.paste_override["ask"] = False
            app.stop_ask()
        elif app.ask_state == "idle" and not app.recording:
            if not self.conf.transcribe_ready():
                self._open_setup("api")
                return
            if not self._assistant_available():
                self._open_setup("assistant")
                return
            app.paste_override["ask"] = False
            app.start_ask()
            if app.ask_state != "recording":
                app.paste_override.pop("ask", None)
        self.refresh()

    def _pause(self):
        self.controller._toggle_pause()
        self.refresh()

    def _cancel_capture(self):
        if self.controller.state == "recording":
            self.controller._cancel()
        self.refresh()

    def _cancel_ask(self):
        self.controller.cancel_ask()
        self.refresh()

    def _meeting(self):
        if not audio.sound().meetings:
            return
        if not self.conf.transcribe_ready() and self.controller.meeting_state == "idle":
            self._open_setup("api")
        else:
            self.controller._toggle_meeting()
        self.refresh()

    def _cancel_meeting(self):
        self.controller.cancel_meeting()
        self.refresh()
