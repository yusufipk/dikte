"""Task-first native desktop window backed by the application controllers."""

import shutil

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import api, assistant, audio, cleanup, config as cfg, ggml
from .i18n import t
from . import theme


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
    target = conf.transcribe_target()
    local = ggml.state()
    sound_model = target.model
    timestamps = conf["file_timestamps"] if file_timestamps is None else file_timestamps
    if mode == "meeting" or (mode == "file" and timestamps):
        sound_model = api.timestamp_model(target.provider, target.model, target.file_model)
    sound = _model_location(sound_model, target.provider, local.get("whisper"))
    enabled = conf["cleanup_enabled"] or conf["translation_enabled"]
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
    text = _model_location(model, provider, local.get("llama")) if enabled else t("Editing off")
    location = t("Dictation: {sound} / Cleanup: {text}", sound=sound, text=text)
    if mode not in ("file", "meeting", "ask") and conf["translation_enabled"]:
        location = t("Dictation: {sound} / Translation: {text}", sound=sound, text=text)
    if mode == "meeting":
        location += " / " + t("Minutes: {model}", model=_model_location(conf["meeting_model"], "openrouter"))
    elif mode == "ask":
        provider = assistant.provider(conf)
        model = assistant.model(conf)
        if provider in ("codex", "agy") and not conf[f"assistant_{provider}_model"].strip():
            model = t("{name} default model", name=assistant.display_name(conf))
        location += " / " + t("Assistant: {model}", model=_model_location(model, provider))
    return location


class HomeWindow(QWidget):
    """Own navigation and presentation; recording and processing stay in Dikte."""

    def __init__(self, controller, settings):
        super().__init__()
        self.controller = controller
        self.conf = controller.conf
        self.settings = settings
        self.mode = "dictation"
        self._last_result = None
        self._last_answer = None
        font = self.font()
        font.setPointSizeF(max(10.5, font.pointSizeF()))
        self.setFont(font)
        self.setObjectName("home")
        self.setWindowTitle("Dikte")
        theme.apply(self, self.conf["theme"])
        self._theme_name = None
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(12)
        navigation = QHBoxLayout()
        navigation.setSpacing(6)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for name, title in (("dictation", "Dictation"), ("file", "File"),
                            ("meeting", "Meeting"), ("ask", "Assistant")):
            button = _button(t(title), lambda checked=False, name=name: self.show_mode(name), "mode")
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.mode_group.addButton(button)
            navigation.addWidget(button, 1)
            self.mode_buttons[name] = button
        self.settings_button = _button("", controller.open_settings, "settings")
        self.settings_button.setIcon(settings_icon())
        self.settings_button.setIconSize(QSize(20, 20))
        self.settings_button.setFixedSize(34, 34)
        self.settings_button.setToolTip(t("Settings"))
        self.settings_button.setAccessibleName(t("Settings"))
        navigation.addWidget(self.settings_button)
        root.addLayout(navigation)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.mode_pages = {}
        self.mode_pages["dictation"] = self._scrolled(self._capture_page())
        self.mode_pages["file"] = settings.task_pages["file"]
        self.mode_pages["meeting"] = self._scrolled(self._meeting_page())
        self.mode_pages["ask"] = self._scrolled(self._assistant_page())
        self.mode_pages["history"] = self._scrolled(self._history_page())
        for page in self.mode_pages.values():
            self.pages.addWidget(page)
        self.footer = _label("", "footer", True)
        root.addWidget(self.footer)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        settings.applied.connect(self.refresh)
        settings.file_cleanup.toggled.connect(self.refresh)
        settings.file_timestamps.toggled.connect(self.refresh)
        settings.transcriber.finished.connect(self.refresh_results)
        settings.history.model().rowsRemoved.connect(self.refresh_results)
        settings.history.model().modelReset.connect(self.refresh_results)
        self.resize(620, 560)
        screen = self.screen()
        if screen:
            room = screen.availableGeometry()
            self.resize(min(620, room.width() - 40), min(560, room.height() - 80))
        self.setMinimumSize(420, 360)
        self.show_mode("dictation")
        self.refresh_results()

    @staticmethod
    def _scrolled(page):
        page.setMaximumWidth(680)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(page)
        return area

    def _capture_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)
        self.capture_status = _label("", "heading", True)
        layout.addWidget(self.capture_status)
        self.capture_button = _button("", self._capture, "capture")
        self._mic_icon = microphone_icon()
        self._stop_icon = microphone_icon(recording=True)
        self.capture_button.setIcon(self._mic_icon)
        self.capture_button.setIconSize(QSize(44, 44))
        self.capture_button.setFixedSize(112, 112)
        layout.addWidget(self.capture_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.capture_shortcut = _label("", "muted", True)
        layout.addWidget(self.capture_shortcut)
        self.capture_models = _label("", "models", True)
        layout.addWidget(self.capture_models)
        controls = QHBoxLayout()
        controls.addStretch()
        self.pause_button = _button(t("Pause the recording"), self._pause)
        self.cancel_button = _button(t("Discard the recording"), self._cancel_capture)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.cancel_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.capture_error = _label()
        layout.addWidget(self.capture_error)
        card = QFrame()
        card.setObjectName("result")
        content = QVBoxLayout(card)
        content.setContentsMargins(18, 12, 18, 12)
        top = QHBoxLayout()
        top.addWidget(_label(t("Latest text")))
        top.addStretch(1)
        top.addWidget(_button(t("History"), lambda: self.show_mode("history")))
        content.addLayout(top)
        self.latest_text = QPlainTextEdit()
        self.latest_text.setReadOnly(True)
        self.latest_text.setAccessibleName(t("Latest text"))
        self.latest_text.setPlaceholderText(t("Your first transcript will appear here."))
        self.latest_text.setMinimumHeight(84)
        self.latest_text.setMaximumHeight(100)
        content.addWidget(self.latest_text)
        self.latest_warning = _label()
        content.addWidget(self.latest_warning)
        actions = QHBoxLayout()
        self.latest_time = _label("", "muted")
        actions.addWidget(self.latest_time, 1)
        self.copy_button = _button(t("Copy"), lambda: QApplication.clipboard().setText(self.latest_text.toPlainText()))
        self.open_button = _button(t("Open text"), lambda: self._open_text(self.latest_text.toPlainText()))
        actions.addWidget(self.copy_button)
        actions.addWidget(self.open_button)
        content.addLayout(actions)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _meeting_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.meeting_status = _label("", "heading")
        layout.addWidget(self.meeting_status)
        self.meeting_hint = _label("", "muted")
        layout.addWidget(self.meeting_hint)
        actions = QVBoxLayout()
        self.meeting_button = _button(t("Record a meeting"), self._meeting, "primary")
        self.meeting_cancel = _button(t("Discard the meeting"), self._cancel_meeting)
        actions.addWidget(self.meeting_button)
        actions.addWidget(self.meeting_cancel)
        layout.addLayout(actions)
        self.meeting_error = _label()
        layout.addWidget(self.meeting_error)
        layout.addWidget(_label(t("Minutes")))
        minutes = self.settings.task_pages["minutes"]
        minutes.setMinimumHeight(300)
        layout.addWidget(minutes, 1)
        minutes.show()
        return page

    def _assistant_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.ask_status = _label("", "heading")
        layout.addWidget(self.ask_status)
        self.ask_scope = _label("", "muted")
        layout.addWidget(self.ask_scope)
        actions = QHBoxLayout()
        self.ask_button = _button("", self._ask, "primary")
        actions.addWidget(self.ask_button)
        actions.addWidget(_button(t("Start a new conversation"), self.controller.reset_conversation))
        actions.addStretch()
        layout.addLayout(actions)
        self.ask_pause = _button(t("Pause the recording"), self._pause)
        layout.addWidget(self.ask_pause)
        self.ask_cancel = _button(t("Stop"), self._cancel_ask)
        layout.addWidget(self.ask_cancel)
        self.ask_error = _label()
        layout.addWidget(self.ask_error)
        self.ask_output = QPlainTextEdit()
        self.ask_output.setReadOnly(True)
        self.ask_output.setAccessibleName(t("Assistant reply"))
        self.ask_output.setPlaceholderText(t("The assistant's reply will appear here."))
        self.ask_output.setMinimumHeight(160)
        layout.addWidget(self.ask_output, 1)
        layout.addWidget(_button(t("Copy"), lambda: QApplication.clipboard().setText(self.ask_output.toPlainText())))
        return page

    def _history_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_button(t("Back to dictation"), lambda: self.show_mode("dictation")))
        layout.addWidget(self.settings.task_pages["history"], 1)
        self.settings.task_pages["history"].show()
        actions = self.settings.history_actions
        actions.insertWidget(actions.count() - 1, _button(t("Open selected text"), self._open_selected))
        self.settings.history.itemDoubleClicked.connect(self._open_selected)
        return page

    def _open_selected(self, *_):
        rows = self.settings._selected_rows()
        if rows:
            self._open_text("\n\n".join(row.get("text", "") for row in rows))

    def _open_text(self, text):
        if not text:
            return
        document = QDialog(self)
        document.setWindowTitle(t("Transcript"))
        document.resize(600, 500)
        layout = QVBoxLayout(document)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        layout.addWidget(editor)
        layout.addWidget(_button(t("Copy"), lambda: QApplication.clipboard().setText(text)))
        document.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        document.show()

    def show_mode(self, mode):
        if mode not in self.mode_pages:
            return
        self.mode = mode
        self.pages.setCurrentWidget(self.mode_pages[mode])
        self.mode_buttons["dictation" if mode == "history" else mode].setChecked(True)
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
        super().hideEvent(event)

    def refresh_results(self, *_):
        rows = cfg.read_history(self.conf["history_limit"])
        result = next((row for row in reversed(rows) if row.get("mode") != "ask"), {})
        answer = next((row for row in reversed(rows) if row.get("mode") == "ask"), {})
        self._set_result(result, answer)

    def _set_result(self, result, answer):
        if result != self._last_result:
            self._last_result = dict(result)
            self.latest_text.setPlainText(result.get("text", ""))
            stamp = result.get("ts", "")
            if result.get("mode") == "translate":
                stamp += "  ·  " + t("Translation") + " → " + result.get("translation_target", "")
            self.latest_time.setText(stamp)
            self.latest_warning.setText(result.get("cleanup_error", ""))
            self.latest_warning.setVisible(bool(self.latest_warning.text()))
            self.copy_button.setEnabled(bool(result.get("text")))
            self.open_button.setEnabled(bool(result.get("text")))
        if answer != self._last_answer:
            self._last_answer = dict(answer)
            self.ask_output.setPlainText(answer.get("text", ""))

    def refresh(self, *_):
        app, conf = self.controller, self.conf
        if self._theme_name != conf["theme"]:
            self._theme_name = conf["theme"]
            theme.apply(self, self._theme_name)
            colors = theme.palette(self._theme_name)
            self._mic_icon = microphone_icon(color=colors["accent_text"])
            self._stop_icon = microphone_icon(recording=True, color=colors["accent_text"])
            self.settings_button.setIcon(settings_icon(colors["muted"]))
        messages = getattr(app, "home_messages", {})
        ready = conf.transcribe_ready()
        recording = app.state == "recording"
        busy = app.state == "busy"
        title = t("Ready to speak") if ready else t("Set up transcription")
        if recording:
            seconds = int(app._recorded_seconds())
            title = t("Paused") if app.paused else t("Recording")
            title += f"  {seconds // 60:02d}:{seconds % 60:02d}"
        elif busy:
            title = messages.get("dictation_stage") or t("Transcribing…")
        self.capture_status.setText(title)
        action = t("Stop and transcribe") if recording else t("Start recording") if ready else t("Set up transcription")
        self.capture_button.setAccessibleName(action)
        self.capture_button.setIcon(self._stop_icon if recording else self._mic_icon)
        self.capture_button.setToolTip(action)
        self.capture_button.setEnabled(recording or not app.recording)
        self.capture_shortcut.setText(" + ".join(part.strip() for part in conf["shortcut"].split("+")))
        self.pause_button.setVisible(recording)
        self.cancel_button.setVisible(recording)
        self.pause_button.setText(t("Resume the recording") if app.paused else t("Pause the recording"))
        warning = ""
        if (conf["cleanup_enabled"] or conf["translation_enabled"]) and conf["cleanup_provider"] == "local" and not conf.local_llm_ready():
            warning = t("The local editing model is missing. Set it up in Settings; the original transcript is kept if editing fails.")
        self.capture_error.setText(messages.get("dictation", "") or warning)
        self.capture_error.setVisible(bool(self.capture_error.text()))
        details = processing_locations(conf, self.mode, self.settings.file_cleanup.isChecked(),
                                       self.settings.file_timestamps.isChecked())
        self.capture_models.setText(details if self.mode == "dictation" else "")
        self.footer.setText(details)
        self.footer.setVisible(self.mode != "dictation")
        self._refresh_meeting(messages, ready)
        self._refresh_ask(messages, ready)

    def _refresh_meeting(self, messages, ready):
        app = self.controller
        state = app.meeting_state
        supported = audio.sound().meetings
        hint = t("Record your microphone and the other participants. Use headphones.")
        if not supported:
            hint = t("Meeting recording is not supported on this system. You can still transcribe a file.")
        elif audio.sound() is audio.COREAUDIO:
            hint = t("On macOS, set up BlackHole or Loopback and select the system audio source in Settings first.")
        title = t("Meeting")
        if state == "recording":
            seconds = int(app.meeting_elapsed.elapsed() / 1000)
            title = t("Recording") + f"  {seconds // 60:02d}:{seconds % 60:02d}"
        elif state == "working":
            title = app.meeting_message or t("Writing the meeting up…")
        if supported and not self.conf.openrouter_key():
            hint += "\n" + t("Connect OpenRouter in Settings to write minutes. The recording is kept if writing fails.")
        self.meeting_status.setText(title)
        self.meeting_hint.setText(hint)
        self.meeting_button.setText(t("End the meeting and write it up") if state == "recording" else t("Record a meeting") if ready else t("Set up transcription"))
        self.meeting_button.setEnabled(supported and state != "working")
        self.meeting_cancel.setVisible(state == "recording")
        self.meeting_error.setText(messages.get("meeting", ""))
        self.meeting_error.setVisible(bool(self.meeting_error.text()))

    def _refresh_ask(self, messages, ready):
        app, conf = self.controller, self.conf
        name = assistant.display_name(conf)
        provider = conf["assistant_provider"]
        directory = assistant.working_dir(conf)
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
        self.ask_scope.setText(t("{name}\nPermissions: {permission}\nWorking directory: {directory}\nShortcut: {shortcut}\nThis button sends a spoken command to the assistant. Its reply is copied without automatic pasting.", name=name, permission=permission, directory=directory if provider in ("claude", "codex", "agy") else t("Not used"), shortcut=conf["assistant_shortcut"] or t("Not assigned")))
        state = app.ask_state
        self.ask_status.setText(t("Paused") if state == "recording" and app.paused else t("Recording") if state == "recording" else messages.get("ask_stage", t("Working…")) if state == "busy" else t("Assistant"))
        available = self._assistant_available()
        self.ask_button.setText(t("Stop and send command") if state == "recording" else t("Set up transcription") if not ready else t("Record a command") if available else t("Set up assistant"))
        self.ask_button.setEnabled(state == "recording" or (state == "idle" and not app.recording))
        self.ask_pause.setVisible(state == "recording")
        self.ask_pause.setText(t("Resume the recording") if app.paused else t("Pause the recording"))
        self.ask_cancel.setVisible(state != "idle")
        self.ask_error.setText(messages.get("ask", "") or ("" if available else t("Install the selected assistant CLI or configure its connection in Settings.")))
        self.ask_error.setVisible(bool(self.ask_error.text()))

    def _assistant_available(self):
        provider = self.conf["assistant_provider"]
        binary = assistant.executable(provider)
        if binary:
            return bool(shutil.which(binary))
        if provider == "requesty":
            return bool(self.conf.requesty_key())
        return bool(self.conf.opencode_key() if provider == "opencode" else self.conf.openrouter_key())

    def _capture(self):
        app = self.controller
        if app.state == "recording":
            # Clicking Stop puts this window in front of the original target.
            app.paste_override["dictation"] = False
            app.stop()
        elif not app.recording:
            if not self.conf.transcribe_ready():
                self.settings.tabs.setCurrentIndex(self.settings.api_tab_index)
                app.open_settings()
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
                app.open_settings()
                return
            if not self._assistant_available():
                self.settings.tabs.setCurrentIndex(4)
                app.open_settings()
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
            self.controller.open_settings()
        else:
            self.controller._toggle_meeting()
        self.refresh()

    def _cancel_meeting(self):
        self.controller.cancel_meeting()
        self.refresh()
