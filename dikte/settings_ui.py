"""Settings window."""

import copy
import functools
import os
import pathlib
import re
import shutil
import sys
import threading

from PyQt6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer, QUrl, QSignalBlocker, pyqtSignal
from PyQt6.QtGui import (QAction, QDesktopServices, QFont, QGuiApplication, QKeySequence, QShortcut,
                         QTextCharFormat, QTextCursor, QTextDocument, QTextFormat,
                         QTextListFormat)
from PyQt6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QAbstractSpinBox, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QTabWidget, QTextBrowser,
    QToolButton,
    QVBoxLayout, QWidget,
)

from . import __version__
from . import api
from . import assistant
from . import audio
from . import cleanup
from . import config as cfg
from . import filetranscribe
from . import ggml
from . import hardware
from . import hotkey
from . import hub
from . import i18n
from . import ipc
from . import meeting
from . import paste
from . import profiles
from . import profiles_ui
from . import update
from . import widgets
from . import worker
from .filetranscribe import FileTranscriber
from .i18n import t
from . import theme

UI_LANGUAGES = [("Automatic (system)", "auto"), ("Turkish", "tr"), ("English", "en")]
LANGUAGES = [
    ("Detect automatically", "auto"), ("Turkish", "tr"), ("English", "en"),
    ("German", "de"), ("French", "fr"), ("Spanish", "es"), ("Arabic", "ar"),
]
CORNERS = ["bottom-left", "bottom-right", "top-left", "top-right"]
CORNER_LABELS = {"bottom-left": "Bottom left", "bottom-right": "Bottom right",
                 "top-left": "Top left", "top-right": "Top right"}
# The pages of Settings, in order: the name code opens a page by, the title
# above it, the label in the sidebar (the same words, so the two never
# disagree about where you are), and the sidebar's glyph.
PAGES = [
    ("general", "General", "General", "general"),
    ("api", "Models", "Models", "key"),
    ("editing", "Editing", "Editing", "edit"),
    ("profiles", "Profiles", "Profiles", "profile"),
    ("assistant", "Assistant", "Assistant", "sparkle"),
    ("meeting", "Meeting", "Meeting", "meeting"),
    ("shortcuts", "Shortcuts", "Shortcuts", "keyboard"),
]
# Settings content never gets wider than this: a form line longer than that is
# hard to follow from label to field. Wider windows centre it in the pane.
CONTENT_WIDTH = 760
# The band Apply and Discard sit in.
FOOTER_HEIGHT = 64
# The margin between the sidebar and a page, and on the page's other side.
PAGE_GUTTER = 20
# Every page is centred at one width, so a page starts at the same x as the
# one before it whatever is on it; the ones with more to show go to two
# columns before they reach it.
PAGE_WIDTH = 1240


def key_label(combo):
    """"ctrl+shift+v" as a keyboard says it: "Ctrl+Shift+V"."""
    return "+".join(part.strip()[:1].upper() + part.strip()[1:]
                    for part in str(combo).split("+") if part.strip())


def reflow(text):
    """A prompt written with hard line breaks, as one line per paragraph.

    The defaults are kept wrapped at 80 columns in the source; in an editor
    that wraps by itself those breaks only make ragged lines. List items,
    headings and blank lines keep their own lines.
    """
    out, current = [], None
    for line in text.splitlines():
        stripped = line.strip()
        item = re.match(r"([-*\u2022]|\d+[.)])\s", stripped)
        if not stripped or item or stripped.startswith("#"):
            if current is not None:
                out.append(current)
            current = None
            if not stripped or stripped.startswith("#"):
                out.append(stripped)
            else:
                current = line.rstrip()
            continue
        current = stripped if current is None else current + " " + stripped
    if current is not None:
        out.append(current)
    return "\n".join(out).strip()


def _note(text, tooltip=""):
    """One short muted line under a control; the rest goes in its tooltip."""
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(True)
    if tooltip:
        label.setToolTip(tooltip)
    return label


def _with_info(widget, tooltip):
    """A control with an (i) beside it whose tooltip says the rest."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    if isinstance(widget, QLabel):
        # One short line by design; wrapped, it would break beside its (i).
        widget.setWordWrap(False)
    row.addWidget(widget)
    topic = widget.text() if isinstance(widget, (QLabel, QAbstractButton)) else ""
    row.addWidget(widgets.InfoIcon(tooltip, topic=topic))
    row.addStretch(1)
    return holder


def _reveal(widget):
    """Scroll the page `widget` is on so all of it is in view."""
    try:
        area = widget.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is not None and widget.isVisible():
            area.ensureWidgetVisible(widget, 0, 16)
    except RuntimeError:
        pass  # the window went while the timer ran


def _graphics_device_label(device):
    if not device.memory or device.shared is None:
        return device.name
    kind = t("shared") if device.shared else t("dedicated")
    return t("{name} ({size} {kind})",
             name=device.name, size=ggml.human_size(device.memory), kind=kind)


# The provider box offers what config knows how to reach, this machine first.
TRANSCRIBE_PROVIDERS = ([("This machine (whisper.cpp)", "local")]
                        + [(who.service, name)
                           for name, who in cfg.TRANSCRIBERS.items()])
# Starting points for the model box; "Fetch model list" replaces them with
# whatever the provider offers today.
TRANSCRIBE_MODELS = {
    "openai": ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"],
    "openrouter": [
        "openai/gpt-4o-transcribe", "openai/gpt-4o-mini-transcribe",
        "openai/whisper-1", "openai/whisper-large-v3",
        "openai/whisper-large-v3-turbo", "mistralai/voxtral-mini-transcribe",
        "deepgram/nova-3", "google/chirp-3",
    ],
}
CLEANUP_MODELS = [
    "google/gemini-3.5-flash-lite", "google/gemini-3.1-flash-lite",
    "google/gemini-2.5-flash-lite", "anthropic/claude-haiku-4.5",
    "openai/gpt-5-mini", "meta-llama/llama-3.3-70b-instruct",
]
# agy's model ids carry the reasoning effort in their suffix, which is why one
# model appears here at more than one level. The same list seeds two boxes:
# cleanup, which wants the bottom rung, and the agent, which sometimes does not.
AGY_MODELS = [
    "gemini-3.7-flash-low", "gemini-3.7-flash-medium", "gemini-3.7-flash-high",
    "gemini-3.5-flash-low", "gemini-3.1-pro-low",
]
# In the order they answer in. The hosted requests are over in a second, a
# model here takes a little longer and costs nothing, and the three CLIs the
# agent can run on open a whole session to do the smaller job.
CLEANUP_PROVIDERS = [
    ("OpenRouter", "openrouter"),
    ("Custom server", "custom"), ("This machine (llama.cpp)", "local"), ("Claude Code", "claude"),
    ("Codex", "codex"), ("Antigravity", "agy"),
]
# Cleaning up a sentence is the lightest thing either of them will ever be
# asked, so the small model comes first.
CLEANUP_CLAUDE_MODELS = ["haiku", "sonnet", "opus", "fable"]
# Minutes are a harder job than cleanup: an hour of talk has to be read whole
# and turned into decisions, so the starting points are the larger models.
MEETING_MODELS = [
    "google/gemini-3.5-flash", "google/gemini-3.1-pro-preview",
    "anthropic/claude-sonnet-5", "openai/gpt-5.4", "x-ai/grok-4.5",
]
ASSISTANT_PROVIDERS = [
    ("Claude Code", "claude"), ("Codex", "codex"), ("Antigravity", "agy"),
    ("OpenRouter", "openrouter"), ("Custom server", "custom"),
]
# Aliases resolve to the newest model of that name, so they age better than an
# id does; a full id can be typed in when a particular one is wanted.
ASSISTANT_MODELS = ["sonnet", "opus", "haiku", "fable"]
# What the Codex boxes offer before Codex itself has answered, and everything
# they offer when it cannot: the real list comes from `codex debug models` when
# the window opens, so this only has to be roughly right.
CODEX_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]
# Starting points only; the box is editable and OpenRouter has hundreds.
ASSISTANT_OR_MODELS = [
    "google/gemini-3.5-flash", "anthropic/claude-sonnet-5", "openai/gpt-5.4",
    "x-ai/grok-4.5", "google/gemini-3.1-pro-preview",
]
# What Claude Code may do without being able to ask. It cannot ask: there is no
# window to answer in, so a mode that would have prompted denies instead.
PERMISSION_MODES = [
    ("Decide on its own, with the safety checks on", "auto"),
    ("Allow everything", "bypassPermissions"),
    ("Only what needs no permission", "manual"),
]
def _typed_model_note(name):
    """Every model box takes a typed name too; the tooltip that says so."""
    return t("The list is a starting point, not a fence: any model name "
             "{name} accepts can be typed straight in.", name=name)


# Codex confines the commands it runs instead of asking about them.
CODEX_SANDBOXES = [
    ("Read anything, write in the working directory", "workspace-write"),
    ("Read only", "read-only"),
    ("No sandbox at all", "danger-full-access"),
]
MEETING_STATUS = {
    "recorded": "waiting to be written up",
    "transcribed": "transcript ready, minutes missing",
    "failed": "failed",
}
# How hard the cleanup model may think before it answers, in OpenRouter's own
# effort levels. A model that ignores the field simply answers as it always did.
REASONING_LEVELS = [
    ("Model's own default", ""), ("Off", "none"), ("Minimal", "minimal"),
    ("Low", "low"), ("Medium", "medium"), ("High", "high"),
    ("Very high", "xhigh"), ("Maximum", "max"),
]
# Offered for every global shortcut, which keeps them one kind of field rather
# than four. The boxes stay editable: this is a shortlist of combinations that
# are usually free, not the set of ones that work.
SHORTCUTS = [
    "Ctrl+Space", "Ctrl+Alt+Space", "Ctrl+Shift+Space", "Meta+Space",
    "Ctrl+Alt+A", "Ctrl+Alt+D", "Ctrl+Alt+M", "Ctrl+Alt+Q",
    "Meta+A", "Meta+D", "Meta+M",
    "Ctrl+Alt+F1", "Ctrl+Alt+F2", "Ctrl+Alt+F3",
]
# Cmd+Space is Spotlight and Ctrl+Space switches input sources, so a Mac gets
# its own shortlist. Option is what Alt is called on that keyboard.
MAC_SHORTCUTS = [
    "Ctrl+Option+Space", "Cmd+Shift+Space", "Ctrl+Shift+Space",
    "Ctrl+Option+A", "Ctrl+Option+D", "Ctrl+Option+M",
    "Cmd+Option+A", "Cmd+Option+D", "Cmd+Option+M",
]
AUDIO_FILTER = ("*.mp3 *.wav *.m4a *.ogg *.opus *.flac *.aac *.wma "
                "*.mp4 *.mkv *.webm *.mov *.avi")


class WrappedLabel(QLabel):
    """A label that wraps, and keeps the height the wrapping calls for.

    Word wrap on its own only decides where the lines break. The height comes
    from the layout, which asks once, before the width is settled, and a label
    sharing a row with a button is answered as if one line were enough: a long
    program path then has its second line cut off. Claiming the height back as
    a minimum, once the width is known, is what keeps the whole text on screen.
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def setText(self, text):
        super().setText(text)
        self._fit()

    def showEvent(self, event):
        # Text set while the window was still being built was measured against
        # nothing; this is the first moment the width means anything.
        super().showEvent(event)
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        # A label the layout has not placed yet is a handful of pixels wide,
        # and wrapping a sentence against that width invents a hundred lines.
        # The minimum set from it does not stay a minimum either: QLabel folds
        # it into its own cached size hints and clears that cache only when the
        # text changes, so the row stands thousands of pixels tall and carries
        # the model box and everything under it off the bottom of the window
        # until another publisher is picked. Nothing to measure against yet
        # means nothing to claim yet, and the show and resize above come back
        # for it.
        if not self.isVisible() or self.width() <= 0:
            return
        # Measured off the font rather than asked of the label, whose own answer
        # is floored by the minimum set here a moment ago and so only ever grows.
        wrap = Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere
        box = QRect(0, 0, self.width(), 0)
        self.setMinimumHeight(
            self.fontMetrics().boundingRect(box, wrap, self.text()).height())


class WheelGuard(QObject):
    """Keeps a rolled wheel off the box the pointer only passed over.

    A combo box and a spin box both read the wheel as a change of value, and
    every tab scrolls now: rolling down the API tab with the pointer over the
    model box would pick a different model on the way past, and the setting is
    saved without anybody having chosen it. The wheel counts once the box has
    been clicked into; before that it is handed back to the page underneath,
    which is what the roll was for.
    """

    def eventFilter(self, box, event):
        win = box.window()
        focused = box.hasFocus() or (win is not None and win.focusWidget() is box)
        if event.type() == QEvent.Type.Wheel and not focused:
            # Refused rather than swallowed. An unaccepted wheel event carries
            # on up the parents to the scroll area, so the page still moves.
            event.ignore()
            return True
        return super().eventFilter(box, event)


class LocalModelBox(QGroupBox):
    """The program, the model, and the two downloads that put them there.

    One class for whisper.cpp and llama.cpp, because the job is the same one
    twice: say whether the program is here, offer the models somebody publishes,
    fetch the chosen one, and stay usable while a gigabyte arrives. Nothing is
    listed in the source; `repos` and `models` are asked at the moment the box is
    opened, so a model published this morning is in the list this afternoon.
    """

    _listed = pyqtSignal(list, str)
    # qint64 rather than int, which is C++'s 32-bit one: a 2.3 GB model is more
    # than fits in it, and the count comes out the far side negative.
    # The tag says which label the numbers belong to: the program download and
    # a model download can run at once, and routing by a flag read when the
    # queued event lands put one job's bytes in the other's label.
    _progress = pyqtSignal(str, "qint64", "qint64")
    _finished = pyqtSignal(str, str)
    _installed = pyqtSignal(str, str)

    changed = pyqtSignal()
    program_changed = pyqtSignal()

    def __init__(self, program, title, models, model_path, repos=None, parent=None):
        super().__init__(title, parent)
        self.program = program
        self.custom_binary = ""  # UI draft; only Save writes the configuration.
        self._models = models          # () -> [hub.Item], or (repo) -> [hub.Item]
        self._model_path = model_path  # (name) -> Path
        self._repos = repos            # None, or () -> [repo id]
        self._downloading = False
        self._pending = False
        self._stop = False
        self._wanted = ""              # the model to select once a list arrives
        self._chosen_in = ""           # the publisher the selected model is from
        # Whether a list for the publisher on screen has come back. An empty
        # box before one has is a box nobody has asked anything yet, and the
        # two read the same without this.
        self._answered = False
        # What the last publisher listing held, so that the switch beside the
        # box can be flipped without asking for it again.
        self._found_repos = []
        # Typing or arrowing through the publisher box changes its text a
        # character at a time, and each of those would otherwise be a request.
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.setInterval(400)
        self._later.timeout.connect(self._later_fetch)

        # Flat: it sits inside a section that already has a frame and a
        # title, and its rows share that section's label column.
        self.setObjectName("flat")
        self.setTitle("")
        self.setAccessibleName(title)
        form = self.form = QFormLayout(self)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(9)
        form.setHorizontalSpacing(14)

        self.program_label = WrappedLabel()
        self.install_button = QPushButton(t("Download"))
        self.install_button.clicked.connect(self._install_program)
        form.addRow(t("Speech engine") if program is ggml.WHISPER else t("Program"),
                    self._side_by_side(self.program_label, self.install_button))

        self.automatic_button = QPushButton(t("Use automatic selection"))
        self.automatic_button.clicked.connect(self._use_automatic_program)
        form.addRow("", self.automatic_button)

        # What the model rows are judged against, said out loud. Without it,
        # "too big for this machine" and the recommendation above the list are
        # a verdict with no visible reason behind them.
        self.machine_label = WrappedLabel()
        self.machine_label.setToolTip(
            t("A model may take half of this memory, less a gigabyte for the "
              "context around the weights. Anything past that is marked too "
              "big; it may still load, on a machine with nothing else open."))
        form.addRow(t("This machine"), self.machine_label)
        self._show_machine()

        if self._repos is not None:
            self.repo = QComboBox()
            self.repo.setEditable(True)
            self.repo.setToolTip(t("A Hugging Face repository of GGUF files. The "
                                   "list is fetched; any other one can be typed in."))
            self.repo.currentTextChanged.connect(self._repo_changed)
            # Forty repository ids is not a choice anybody can make. The few
            # that were picked for this job are what the box holds until
            # somebody asks for the rest.
            self.every_repo = QCheckBox(t("All"))
            self.every_repo.setToolTip(
                t("Everything ggml-org publishes, including the models that "
                  "are too big to run here and the ones that are not for "
                  "cleaning up text."))
            self.every_repo.toggled.connect(self._every_repo_changed)
            form.addRow(t("Publisher"),
                        self._side_by_side(self.repo, self.every_repo))
            # A repository id names the publisher, the parameter count and the
            # shape of the weights, and says nothing about whether it is the
            # one to click.
            self.repo_note = WrappedLabel()
            form.addRow("", self.repo_note)

        self.model = QComboBox()
        self.model.setToolTip(
            t("large-v3 makes the fewest mistakes and is the slowest of them. "
              "large-v3-turbo is that model with a four layer decoder in place "
              "of a thirty-two layer one: several times faster, at one to two "
              "points of word error in English and about two and a half in "
              "the other languages. Below those, every step down the list "
              "trades accuracy for size, and the .en models are trained on "
              "English alone.")
            if program is ggml.WHISPER else
            t("Cleanup is punctuation, capitals and filler words, so what "
              "these are picked on is following an instruction rather than "
              "knowing anything. Start at a q4 file; the 16-bit ones are "
              "several times the memory for a difference this job cannot "
              "see."))
        self.download_button = QPushButton(t("Download"))
        self.download_button.clicked.connect(self._download)
        self.delete_button = QPushButton(t("Delete"))
        self.delete_button.clicked.connect(self._delete)
        form.addRow(t("Model"), self._side_by_side(self.model,
                                                   self.download_button,
                                                   self.delete_button))
        self.model.currentIndexChanged.connect(self._model_changed)

        self.status = widgets.StatusLine(muted=False)
        form.addRow(self.status)

        self._listed.connect(self._on_listed)
        self._progress.connect(self._on_progress)
        self._finished.connect(self._on_finished)
        self._installed.connect(self._on_installed)

    @staticmethod
    def _fit_popup(combo):
        """Let the list that drops down be as wide as its longest row.

        A combo box hands its own width to the list under it and elides
        whatever does not fit, which lands in the middle of the name:
        `ggml-org/Qwen....7B-Base-GGUF` is not a model anybody can choose
        between. The box itself stays the width the form gave it.
        """
        view = combo.view()
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        metrics = combo.fontMetrics()
        widest = max((metrics.horizontalAdvance(combo.itemText(row))
                      for row in range(combo.count())), default=0)
        # Room for the frame and for a scroll bar, which a long list will have.
        view.setMinimumWidth(widest + view.verticalScrollBar().sizeHint().width() + 24)

    @staticmethod
    def _side_by_side(*widgets):
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        for index, widget in enumerate(widgets):
            layout.addWidget(widget, 1 if index == 0 else 0)
        if widgets and all(isinstance(widget, QPushButton) for widget in widgets):
            layout.addStretch()
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # ---- what is here ----------------------------------------------------

    def selected(self):
        return self.model.currentData() or ""

    def repository(self):
        return self.repo.currentText().strip() if self._repos is not None else ""

    def load(self, model, repo=""):
        """Show what is stored. What else is on offer is asked for on the way up.

        Hidden boxes defer fetching until shown. A catalog already fetched for
        this repository survives Apply; a visible box on a new repository
        refreshes immediately. Installed files are available in either case.
        """
        target_repo = repo or (ggml.suggested_llm()[0] if self._repos is not None else "")
        reuse = self._answered and self._chosen_in == target_repo and self.repository() == target_repo
        items = self._current_items() if reuse else []
        self._later.stop()
        self._wanted = model
        self._pending = not reuse
        self._answered = reuse
        self._show_program()
        self._chosen_in = ""
        if self._repos is not None:
            suggested = ggml.suggested_llm()
            self._chosen_in = repo or suggested[0]
            self.repo.blockSignals(True)
            self.repo.setCurrentText(self._chosen_in)
            self.repo.blockSignals(False)
            self._fill_repos_box(suggested)
        self._fill_models(items)
        if self._pending and self.isVisible():
            self._pending = False
            self._fetch_models(self.repository())

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending:
            self._pending = False
            if self._repos is not None:
                self._fill_repos(self.repository())
            self._fetch_models(self.repository())

    def _use_automatic_program(self):
        self.custom_binary = ""
        self._show_program()
        self._refresh_buttons()
        self.changed.emit()
        self.program_changed.emit()

    def _program_path(self):
        return ggml.program_path(self.program, self.custom_binary)

    def _show_program(self):
        # The row, not just the button: a hidden button in a visible row
        # still leaves a row's worth of gap under the program.
        self.form.setRowVisible(self.automatic_button, bool(self.custom_binary))
        path = self._program_path()
        installed = ggml.installed_program(self.program)
        # Installed and selected are independent: a download never changes an
        # explicit override, and automatic resolution still prefers PATH.
        self.install_button.setText(t("Download again") if installed else t("Download"))
        self.install_button.setVisible(bool(self.custom_binary)
                                       or not ggml.system_program(self.program))
        if not path:
            self.program_label.setText(
                t("Custom program unavailable: {path}", path=self.custom_binary)
                if self.custom_binary else t("Not installed."))
            self.install_button.setVisible(True)
            return
        managed = bool(installed and pathlib.Path(path).resolve()
                       == pathlib.Path(installed).resolve())
        if not managed:
            if self.custom_binary:
                self.program_label.setText(t("Using custom build: {path}", path=path))
            else:
                version = ggml.homebrew_version(self.program, path)
                self.program_label.setText(
                    t("Installed on the system: whisper.cpp {version}\n{path}",
                      version=version, path=path)
                    if version else t("Installed on the system: {path}", path=path)
                )
        elif ggml.vulkan_missing(self.program):
            # The download landed the processor build where the graphics card
            # one belongs, and nothing else on this window would say so.
            self.program_label.setText(
                t("Downloaded: {name}, version {version}. There was no Vulkan build, "
                  "so this one runs on the processor.",
                  name=self.program.repo.rsplit("/", 1)[-1],
                  version=ggml.installed_version(self.program) or "?"))
        else:
            self.program_label.setText(
                t("Downloaded: {name}, version {version}.",
                  name=self.program.repo.rsplit("/", 1)[-1],
                  version=ggml.installed_version(self.program) or "?"))

    def _show_machine(self, selection=None, devices=(), selectable=None):
        where = ggml.accelerator()
        memory = ggml.total_memory()
        parts = []
        memory_parts = []
        selectable = devices if selectable is None else selectable
        chosen = next(
            (device for device in selectable
             if device.identifier and device.identifier == selection),
            None,
        )
        if selection == "cpu":
            parts.append(t("Selected: Processor (CPU)."))
        elif selection == "auto":
            parts.append(t("Selected: Automatic."))
        elif selection:
            if chosen is None:
                parts.append(t("Selected: Graphics card unavailable."))
            else:
                parts.append(t("Selected: {name}.", name=chosen.name))
                if chosen.memory and chosen.shared is not None:
                    kind = t("shared") if chosen.shared else t("dedicated")
                    memory_parts.append(t("{size} {kind} graphics",
                                          size=ggml.human_size(chosen.memory),
                                          kind=kind))
        if selection in ("cpu", "auto"):
            if devices:
                parts.append(t("Graphics: {devices}.", devices="; ".join(
                    _graphics_device_label(device) for device in devices
                )))
            elif where:
                parts.append(t("Graphics: {name}.", name=where))
            elif sys.platform == "darwin":
                parts.append(t(
                    "Graphics capability unknown. The active backend is reported "
                    "after whisper.cpp starts."
                ))
        elif selection is None:
            if where:
                parts.append(t("Graphics: {name}.", name=where))
            elif sys.platform == "darwin":
                parts.append(t(
                    "Graphics capability unknown. The active backend is reported "
                    "after whisper.cpp starts."
                ))
            else:
                parts.append(t("Graphics: Processor only."))
        if memory:
            memory_parts.append(t("{size} system", size=ggml.human_size(memory)))
        if memory_parts:
            parts.append(t("Memory: {memory}.", memory="; ".join(memory_parts)))
        self.machine_label.setText("\n".join(parts))

    # ---- the lists -------------------------------------------------------

    def _fill_repos(self, current):
        def work():
            self._listed.emit([("repos", ggml.llm_repos(), "")], "")

        threading.Thread(target=work, daemon=True).start()

    def _fill_repos_box(self, found):
        """The publishers, with the suggested ones kept apart from the rest.

        Forty repositories in one run is a list nobody reads to the end of, and
        the few worth starting from are lost in it. A separator rather than a
        heading, because this box is typed into as well as chosen from and a
        heading would land in the field as though it were a repository.
        """
        self._found_repos = found
        current = self.repo.currentText()
        # Every suggestion, whether or not it came back in the listing: that
        # listing is the forty repositories touched most recently, and a
        # publisher that has not been updated in a season falls off it while
        # still being the one to point at.
        first = list(ggml.suggested_llm())
        rest = [r for r in found if r not in first]
        if not self.every_repo.isChecked():
            # The one being used stays on offer whatever the switch says, so
            # that a repository somebody typed in is not dropped out from
            # under them by the next fetch.
            rest = [r for r in rest if r == current]
        self.repo.blockSignals(True)
        self.repo.clear()
        self.repo.addItems(first)
        if first and rest:
            self.repo.insertSeparator(self.repo.count())
        self.repo.addItems(rest)
        self.repo.setCurrentText(current)
        self.repo.blockSignals(False)
        self._fit_popup(self.repo)
        self._show_repo_note()

    def _repo_changed(self):
        self._show_repo_note()
        if not self._downloading:
            self._later.start()

    def _show_repo_note(self):
        note = ggml.SUGGESTED_LLM_NOTE.get(self.repository(), "")
        self.repo_note.setText(t(note) if note else "")

    def _every_repo_changed(self):
        self._fill_repos_box(self._found_repos)

    def _later_fetch(self):
        # A download that started inside the wait was not there to be seen when
        # the timer went off, and rebuilding the rows underneath one is exactly
        # what the guard above is for.
        if not self._downloading:
            self._fetch_models(self.repository())

    def _fetch_models(self, repo=""):
        self._answered = False
        self.status.setText(t("Fetching the model list…"))

        def work():
            try:
                found = self._models(repo) if self._repos is not None else self._models()
                self._listed.emit([("models", found, repo)], "")
            except ggml.LocalError as exc:
                self._listed.emit([("models", [], repo)], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_listed(self, payload, error):
        kind, found, repo = payload[0] if payload else ("repos", [], "")
        # A publisher changed while its predecessor's list was still on the way
        # would otherwise be answered with the wrong models, whichever request
        # happened to come back last.
        if kind == "models" and repo != self.repository():
            return
        if error:
            # The list is the publisher's, so a failed one leaves the box no
            # longer showing this publisher's models: emptying it is what keeps
            # the two boxes saying the same thing. The message goes on after,
            # because filling the box writes a status of its own.
            if kind == "models":
                self._fill_models([])
            self._refresh_buttons()
            self.status.setText(error)
            return
        if kind == "repos":
            self._fill_repos_box(found)
            return
        self._answered = True
        self._fill_models(found)

    def _sections(self, items, best):
        """[(heading, [Item])] for the rows to show, in the order to show them.

        The list arrives sorted by size and nothing else, which for whisper
        interleaves the models: `large-v3-turbo-q5_0` lands between the two
        `medium` quantisations, half a screen away from the turbo model it is a
        copy of. Grouping puts the choice of model above the choice of
        quantisation, and the row this machine should take goes on top, where
        somebody who does not want to make either choice can stop reading.
        """
        if not items:
            return []
        groups = (ggml.whisper_groups(items) if self.program is ggml.WHISPER
                  else [("", items)])
        # A publisher with one file on offer is not a choice, and a row of its
        # own above the only row there is would be the same model twice.
        top = [i for i in items if i.name == best] if len(items) > 1 else []
        if not top:
            return groups
        if len(groups) == 1 and not groups[0][0]:
            groups = [(t("Everything this publisher offers"), groups[0][1])]
        return [(t("Recommended for this machine"), top)] + groups

    def _suggested(self):
        """The name to prefer when it is on offer, or "" for whatever fits."""
        if self.program is not ggml.WHISPER:
            return ""
        # A Vulkan loader on the machine is not a card in play when what was
        # installed is the processor build, the Linux fallback or upstream's
        # Windows BLAS one: recommending the accurate model off the loader
        # alone would put a 1 GB model on a processor and the wait for it in
        # front of somebody who asked for a sentence.
        on_processor = (ggml.vulkan_missing(self.program)
                        or ggml.processor_only(self.program, self._program_path()))
        return ggml.suggested_whisper(graphics="" if on_processor else None)

    def _add_heading(self, text):
        """A row that names the group under it and cannot be chosen."""
        self.model.addItem(text)
        row = self.model.count() - 1
        font = self.model.font()
        font.setBold(True)
        self.model.setItemData(row, font, Qt.ItemDataRole.FontRole)
        listing = self.model.model()
        entry = listing.item(row) if hasattr(listing, "item") else None
        if entry is not None:
            entry.setEnabled(False)

    def _add_model(self, name, item, best):
        """One row: the file, what it weighs, and whether it is worth taking."""
        here = ggml.have_model(self._model_path(name))
        if here:
            marks = [t("downloaded")]
        elif item is None:
            # Chosen but neither here nor on offer: the file was deleted from
            # underneath, or the settings came from another machine.
            marks = [t("not downloaded")]
        else:
            marks = [ggml.human_size(item.size)]
        # `q5_1`, `Q4_K_M`, `MXFP4`, `BF16`: four spellings of the same thing
        # in one list, and the number is the whole of what any of them says. A
        # whisper file with no mark at all is the full 16-bit model, which is
        # the one convention here that a name does not carry.
        bits = ggml.bit_depth(name) or (16 if self.program is ggml.WHISPER
                                        else 0)
        if bits:
            marks.append(t("{bits}-bit", bits=bits))
        if ggml.ENGLISH_ONLY in name:
            marks.append(t("English only"))
        # The verdicts last, after everything the row is: what to do about the
        # row rather than what it holds.
        if item is not None and not here and not ggml.fits(item.size):
            marks.append(t("too big for this machine"))
        if name == best:
            marks.append(t("recommended"))
        self.model.addItem(f"{name}  ({', '.join(marks)})", name)
        self.model.setItemData(self.model.count() - 1, item,
                               Qt.ItemDataRole.UserRole + 1)

    def _first_model(self):
        """The first row that is a model rather than a heading."""
        for row in range(self.model.count()):
            if self.model.itemData(row):
                return row
        return -1

    def _fill_models(self, items):
        """One row per model, grouped, saying what it weighs and where it is."""
        # The selection is only worth carrying over within the publisher it was
        # made in. Carried across one, a model this repository does not publish
        # would be added back as "not downloaded" and selected again, and
        # changing the publisher would leave the model box looking untouched.
        same = self._repos is None or self.repository() == self._chosen_in
        wanted = self._wanted or (self.selected() if same else "")
        best = ggml.recommended(items, self._suggested()) if items else ""
        self.model.blockSignals(True)
        self.model.clear()
        listed = set()
        for heading, group in self._sections(items, best):
            if heading:
                self._add_heading(heading)
            for item in group:
                name = self._model_path(item.name).name
                self._add_model(name, item, best)
                listed.add(name)
        # A model that was downloaded and then dropped from the list upstream is
        # still on this disk and still works, so it stays on offer. So does one
        # that is chosen but not here: Save reads this box, and a row missing
        # here would quietly empty the setting rather than showing that the
        # model needs downloading again.
        extras = [(t("Already on this machine"),
                   [name for name in self._on_disk() if name not in listed])]
        if wanted and wanted not in listed \
                and not ggml.have_model(self._model_path(wanted)):
            extras.append((t("Chosen, but not downloaded"), [wanted]))
        for heading, names in extras:
            if names and listed:
                self._add_heading(heading)
            for name in names:
                self._add_model(name, None, best)
        index = self.model.findData(wanted)
        self.model.setCurrentIndex(index if index >= 0 else self._first_model())
        self.model.blockSignals(False)
        self._fit_popup(self.model)
        self._wanted = ""
        self._chosen_in = self.repository()
        self._model_changed()

    def _on_disk(self):
        return (ggml.installed_whisper_models() if self.program is ggml.WHISPER
                else ggml.installed_llm_models())

    # ---- fetching --------------------------------------------------------

    def _install_program(self):
        self.install_button.setEnabled(False)
        self.program_label.setText(t("Downloading…"))

        def work():
            try:
                ggml.install_program(
                    self.program,
                    on_progress=functools.partial(self._report, "program"))
                self._installed.emit("", "")
            except ggml.LocalError as exc:
                self._installed.emit("", str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_installed(self, _, error):
        self.install_button.setEnabled(True)
        self._show_program()
        if error:
            self.program_label.setText(
                self.program_label.text() + "\n" +
                t("Download failed: {error}", error=error))
        # The model line says whether the program is here, so installing one
        # changes what it should read.
        self._refresh_buttons()
        self.changed.emit()
        self.program_changed.emit()

    def _current_item(self):
        return self.model.currentData(Qt.ItemDataRole.UserRole + 1)

    def _download(self):
        if self._downloading:
            self._stop = True
            # The flag is only read between blocks, and the wait for the server
            # to answer is not between blocks: a click during it changes
            # nothing on screen for as long as the connection takes.
            self.status.setText(t("Stopping…"))
            return
        item = self._current_item()
        if item is None:
            return
        self._downloading, self._stop = True, False
        self._refresh_buttons()
        # Opening the connection can take ten or twenty seconds, and the first
        # byte counts are what the line below would otherwise wait for. Left
        # saying "not downloaded yet" beside a button that now reads Stop, a
        # download that started looks like a click that did not register.
        self.status.setText(t("Starting the download…"))

        def work():
            try:
                target_path = self._model_path(item.name)
                # A model the running server holds open cannot be replaced on
                # Windows, and the finished download would be thrown away over
                # it; a re-download lets go of the server first, and the next
                # run starts it again on the fresh file.
                if target_path.exists():
                    (ggml.whisper if self.program is ggml.WHISPER
                     else ggml.llm).stop()
                landed = ggml.download(item, target_path,
                                       on_progress=functools.partial(
                                           self._report, "model"),
                                       should_stop=lambda: self._stop)
                self._finished.emit(item.name if landed else "", "")
            except ggml.LocalError as exc:
                self._finished.emit("", str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _report(self, job, done, total):
        self._progress.emit(job, done, total)

    def _on_progress(self, job, done, total):
        share = f" ({done * 100 // total}%)" if total else ""
        text = t("Downloading: {done} of {total}{share}",
                 done=ggml.human_size(done), total=ggml.human_size(total or done),
                 share=share)
        if job == "model":
            self.status.setText(text)
        else:
            self.program_label.setText(text)

    def _on_finished(self, name, error):
        self._downloading = False
        if error:
            self.status.setText(error)
        elif not name:
            self.status.setText(t("Download stopped."))
        self._fill_models_from_current()
        self.changed.emit()

    def _fill_models_from_current(self):
        """Redraw the rows without asking anybody anything again."""
        self._wanted = self.selected()
        self._fill_models(self._current_items())

    def _current_items(self):
        # By name, because the recommended model has a row of its own at the
        # top as well as one in its group, and reading the rows back twice
        # would double it in the list every time a download finished.
        items, seen = [], set()
        for row in range(self.model.count()):
            item = self.model.itemData(row, Qt.ItemDataRole.UserRole + 1)
            if item is not None and item.name not in seen:
                seen.add(item.name)
                items.append(item)
        return items

    def _delete(self):
        name = self.selected()
        if not name or not ggml.have_model(self._model_path(name)):
            return
        if QMessageBox.question(self, t("Delete model"),
                                t("Delete {name} from this machine?", name=name)) \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            ggml.delete_model(self._model_path(name))
        except ggml.LocalError as exc:
            self.status.setText(str(exc))
        self._fill_models_from_current()
        self.changed.emit()

    def _model_changed(self):
        self._refresh_buttons()
        self.changed.emit()

    def _refresh_buttons(self):
        name = self.selected()
        here = bool(name) and ggml.have_model(self._model_path(name))
        # A row carries what it takes to fetch it. The ones that do not are the
        # models found on this disk and the one the settings name but the list
        # does not offer: there is nothing to press Download for on those, and
        # a button that can only do nothing is worse than one that is out.
        item = self._current_item()
        self.delete_button.setEnabled(here and not self._downloading)
        self.download_button.setText(t("Stop") if self._downloading else t("Download"))
        self.download_button.setEnabled(self._downloading or (item is not None
                                                              and not here))
        if self._downloading:
            return
        if not name and self._repos is not None and self._answered \
                and self._first_model() < 0:
            # An empty box under a publisher that answered perfectly well: what
            # it publishes is split across files, past the size cap, or a
            # projector or draft head rather than a model of its own. Said
            # nowhere, it read as though the click had not registered.
            self.status.warn(
                t("Nothing from this publisher runs here. Choose another one."),
                t("{repo} publishes nothing that can be run here. Its models "
                  "are split across files, larger than {cap}, or pieces of a "
                  "model rather than one. Choose another publisher.",
                  repo=self.repository(), cap=ggml.human_size(ggml.GGUF_MAX_BYTES)))
        elif not name:
            self.status.setText(t("Nothing downloaded yet."))
        elif here and not self._program_path():
            # The model alone runs nothing, and "Ready" over a missing program
            # reads as though it does.
            self.status.warn(t("The program above is missing. Download it first."),
                             t("{name} is here, but the program above is "
                               "not. Download it first.", name=name))
        elif here:
            self.status.setText(t("Ready: {name}.", name=name))
        elif item is None:
            self.status.warn(t("Not available from this publisher. Pick another model."),
                             t("{name} is not on this machine and this "
                               "publisher does not offer it. Choose another "
                               "model, or another publisher.", name=name))
        else:
            self.status.setText(t("{name} has not been downloaded yet.", name=name))


# Kept under its old name for the callers that import it from here.
FlowLayout = widgets.FlowLayout


def history_marks(row, program=True):
    """What a history row says about its profile and translation, so that a
    translated text is never mistaken for what was said. Empty for a row
    written without either. `program=False` leaves out the program and site
    the profile was matched by, for a card that has no room for them."""
    marks = ""
    if "profile" in row:
        marks += t("  ·  profile: {name}", name=row.get("profile") or t("Default"))
        # Only for a row whose program was asked for: one skipped on purpose
        # (an override, the switch off, no profile listing one) says nothing.
        name = row.get("program")
        if program and isinstance(name, str):
            marks += (t(" ({program})", program=name) if name
                      else t(" (program unknown)"))
        site = row.get("site")
        if program and isinstance(site, str) and site:
            marks += t("  ·  site: {site}", site=site)
    code = row.get("translate_to")
    if isinstance(code, str) and code:
        language = t(profiles.language_name(code))
        marks += (t("  ·  translated into {language}", language=language)
                  if row.get("translated")
                  else t("  ·  translation into {language} failed", language=language))
    return marks


def agent_name(name, default="Claude"):
    """What a history row's assistant is called. Rows from before the gateways
    became Custom server presets name the gateway itself; rows from before the
    provider was recorded name nothing, and those were all Claude's."""
    if name in cfg.PRESETS and name != "other":
        return t(cfg.PRESETS[name].service)
    service = assistant.SERVICES.get(name)
    return t(service) if service else default


def history_brief(row):
    """The muted line under a history row: when, in a word or two, and which
    profile or assistant. The full timestamp and marks are in its tooltip."""
    parts = [widgets.short_time(row.get("ts", ""))]
    if row.get("failed"):
        # The row is titled by what failed; why is said here, in a word.
        parts.append((row.get("error") or t("Failed")).splitlines()[0]
                     if (row.get("error") or "").strip() else t("Failed"))
    elif row.get("mode") == "ask":
        parts.append(agent_name(row.get("assistant")))
    elif "profile" in row:
        parts.append(row.get("profile") or t("Default"))
    code = row.get("translate_to")
    if isinstance(code, str) and code and not row.get("failed"):
        language = t(profiles.language_name(code))
        parts.append(t("into {language}", language=language) if row.get("translated")
                     else t("translation failed"))
    return widgets.meta_line(*parts)


def history_context(row):
    """What a run was, past when: the assistant or the profile, and the
    translation, each in a word or two."""
    parts = []
    if row.get("mode") == "ask":
        parts.append(agent_name(row.get("assistant")))
    elif "profile" in row:
        parts.append(row.get("profile") or t("Default"))
    code = row.get("translate_to")
    if isinstance(code, str) and code and not row.get("failed"):
        language = t(profiles.language_name(code))
        parts.append(t("into {language}", language=language) if row.get("translated")
                     else t("translation failed"))
    return parts


def history_title(row):
    """What a history row says first: the text, or for a failure what kind of
    run failed, so two failures with the same error do not read as one."""
    if row.get("failed"):
        return (t("Assistant request failed") if row.get("mode") == "ask"
                else t("Dictation failed"))
    return (row.get("text") or "").replace("\n", " ")


def history_kind(row):
    """The glyph a history row leads with."""
    if row.get("failed"):
        return "failed"
    if row.get("mode") == "ask":
        return "sparkle"
    if row.get("translate_to") and row.get("translated"):
        return "globe"
    return "mic"


def key_check(provider, key, base=""):
    """Ask a hosted service whether `key` works: the line to show, or ApiError
    with the line to show instead. Runs on a worker thread. `provider` is a
    named service or a Custom server preset ("custom" meaning Other)."""
    if provider == "openrouter":
        return api.openrouter_key_status(key)
    if provider == "openai":
        return t("Connection works. {count} audio models visible.",
                 count=len(api.openai_models(key, base)))
    name = "other" if provider == "custom" else provider
    count = len(api.preset_models(name, key, base, test_key=True))
    if cfg.PRESETS[name].catalog == "audio":
        return t("Connection works. {count} audio models visible.", count=count)
    return t("Connection works. {count} models visible.", count=count)


def _retryable(row):
    """A failed dictation whose kept recording is still on disk."""
    return bool(row.get("failed")) and worker.is_kept_recording(row.get("audio"))


class SettingsWindow(QDialog):
    applied = pyqtSignal()
    # A save changed the interface language. Every label below is translated
    # as the window is built, so the change cannot reach this window: the
    # owner replaces it with a fresh one instead.
    language_changed = pyqtSignal()
    # A newer release this window's own check found, so that the tray icon
    # hears about it from here rather than waiting for its own next check.
    update_found = pyqtSignal(object)
    # A failed dictation in the history the user asked to try again, as its
    # row. The owner runs it, since the dictation chain is the owner's.
    retry_requested = pyqtSignal(dict)
    # What a failed entry with no recording left offers instead of a retry:
    # "record" (dictate it again) or "check" (the connection settings). The
    # owner of the home window acts on it.
    history_action = pyqtSignal(str)

    def _sources_once(self):
        """One device listing per window build, shared by every combo.

        Three listings at open were three subprocess runs on the main thread,
        which on a slow ffmpeg was most of the wait for the window."""
        if not hasattr(self, "_sources"):
            self._sources = audio.list_sources()
        return self._sources

    _models_loaded = pyqtSignal(list, str)
    _preset_models_loaded = pyqtSignal(str, str, list, str)
    _transcribe_models_loaded = pyqtSignal(list, str)
    _codex_models_loaded = pyqtSignal(list)
    _agy_models_loaded = pyqtSignal(list)
    # Which hosted provider's list arrived on its own at open, and the list.
    _hosted_models_loaded = pyqtSignal(str, list)
    # Which key was tested, whether it worked, and what to write under it.
    _test_done = pyqtSignal(str, bool, str)
    # The release that was found, or None, and what went wrong instead.
    _update_checked = pyqtSignal(object, str)
    _processing_devices_loaded = pyqtSignal(object, object, int)

    def __init__(self, conf, meetings=None, parent=None):
        super().__init__(parent)
        if parent is None and sys.platform not in ("darwin", "win32"):
            # Under X11 a dialog without a parent is marked transient for the
            # whole application, and KWin keeps such a window on the virtual
            # desktop of the home window. Opening Settings from another
            # desktop then switched the user back there. A plain top-level
            # window is placed on its own, like the home window.
            self.setWindowFlags(
                (self.windowFlags() & ~Qt.WindowType.WindowType_Mask)
                | Qt.WindowType.Window)
        self.conf = conf
        self.meetings = meetings
        self._processing_devices_request = 0
        self._processing_devices_loaded.connect(self._set_processing_devices)
        # Filled in by _shortcut_row as the tabs are built: which combination
        # box, status label and "nothing installed" line belong to each of the
        # global shortcuts. One dictionary is what lets install, remove and the
        # status line be written once instead of once per key.
        self._shortcut_rows = {}
        self._shortcut_forms = {}
        self._shortcut_tips = {}
        # Each provider keeps its own transcription model, so switching the
        # provider back and forth never overwrites the other one's.
        self._models = dict.fromkeys((name for name in cfg.TRANSCRIBERS if name != "custom"), "")
        self._custom_values = dict(conf.data)
        self._preset_shown = {}
        self._preset_widgets = {}
        self._key_fields = {}
        self._testers = {}
        self._shown_provider = ""
        # What _save compares against to know a rebuild is due.
        self._built_language = i18n.language()
        # Where "Open the release page" goes: the release itself once a check
        # has named one, and the page that redirects to the newest until then.
        self._release_url = update.RELEASES_PAGE
        self.transcriber = FileTranscriber(conf, self)
        self.setWindowTitle(t("Dikte Settings"))
        theme.apply(self, conf["theme"])

        # One for the whole window, parented to it so it outlives the boxes it
        # watches and goes when they do.
        self._wheel_guard = WheelGuard(self)

        tabs = self.tabs = QTabWidget(self)
        builders = {
            "general": self._general_tab,
            "api": self._api_tab, "editing": self._prompt_tab,
            "assistant": self._assistant_tab, "meeting": self._meeting_tab,
            "shortcuts": self._shortcut_tab,
        }
        self.profiles_editor = profiles_ui.ProfilesEditor()
        # Pages are opened by name (show_page), never by a number that moves
        # whenever a page is added.
        self.page_index = {}
        for name, title, _short, _glyph in PAGES:
            page = self.profiles_editor if name == "profiles" else builders[name]()
            self.page_index[name] = tabs.addTab(self._scrolled(page, PAGE_WIDTH), t(title))
        self.api_tab_index = self.page_index["api"]
        self.task_pages = {
            "file": self._file_tab(),
            "minutes": self._minutes_tab(),
            "history": self._history_tab(),
        }
        for page in self.task_pages.values():
            page.setParent(self)
            page.hide()
        tabs.tabBar().hide()

        # The sidebar: every page in view at once, rather than behind a
        # dropdown that looks like one more setting.
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setAccessibleName(t("Settings category"))
        # Wider than the glyph: an unsaved page's dot goes beside the glyph
        # rather than on top of it (widgets.glyph_pixmap, aside).
        self.nav.setIconSize(QSize(26, 18))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for _name, _title, short, _glyph in PAGES:
            item = QListWidgetItem(t(short))
            item.setToolTip(t(short))
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(tabs.setCurrentIndex)
        tabs.currentChanged.connect(self._page_changed)

        self.page_title = QLabel("")
        self.page_title.setObjectName("pagetitle")
        # Moved to the left edge of the page under it whenever that moves
        # (_align_title), so the title, the section headings and the boxes
        # share one edge whatever the width and whether a scroll bar shows.
        title_row = self.title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.page_title, 1)

        # Save keeps the window open, so the window is closed with the titlebar
        # cross (or Escape) instead. A "Cancel" next to it would be a lie: the
        # settings are already on disk by then.
        self.apply_button = QPushButton(t("Apply changes"))
        self.apply_button.setObjectName("primary")
        # Always shaped as the primary button: a pale fill while there is
        # nothing to apply, the accent once there is.
        self.apply_button.setProperty("footer", True)
        self.apply_button.setAutoDefault(False)
        self.apply_button.clicked.connect(self._save)
        self.discard_button = QPushButton(t("Discard changes"))
        # An outlined ghost button: beside Apply it must never look like a
        # second primary, enabled or not.
        self.discard_button.setObjectName("ghost")
        self.discard_button.setAutoDefault(False)
        self.discard_button.clicked.connect(self._discard_changes)
        self.dirty_label = QLabel("")
        self.dirty_label.setObjectName("muted")
        footer = QWidget()
        footer.setObjectName("footerbar")
        footer.setFixedHeight(FOOTER_HEIGHT)
        bar = QHBoxLayout(footer)
        bar.setContentsMargins(PAGE_GUTTER, 0, PAGE_GUTTER, 0)
        bar.setSpacing(10)
        bar.addWidget(self.dirty_label, 1)
        bar.addWidget(self.discard_button)
        bar.addWidget(self.apply_button)

        side = QWidget()
        side.setObjectName("side")
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(10, 14, 6, 10)
        side_layout.addWidget(self.nav)
        self.side = side
        rule = QFrame()
        rule.setObjectName("vrule")
        hrule = QFrame()
        hrule.setObjectName("rule")
        right = QVBoxLayout()
        right.setContentsMargins(0, 14, 0, 0)
        right.setSpacing(8)
        right.addLayout(title_row)
        right.addWidget(tabs, 1)
        right.addWidget(hrule)
        right.addWidget(footer)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(side)
        layout.addWidget(rule)
        layout.addLayout(right, 1)
        self._compact = None
        self._set_compact(False)
        self._size_to_screen(940, 700)

        self._preset_models_loaded.connect(self._on_preset_models_loaded)
        self._models_loaded.connect(self._on_models_loaded)
        self._transcribe_models_loaded.connect(self._on_transcribe_models_loaded)
        self._codex_models_loaded.connect(self._on_codex_models_loaded)
        self._agy_models_loaded.connect(self._on_agy_models_loaded)
        self._hosted_models_loaded.connect(self._on_hosted_models_loaded)
        self._test_done.connect(self._on_test_done)
        self._update_checked.connect(self._on_update_checked)
        self.transcriber.progress.connect(self._on_file_progress)
        self.transcriber.finished.connect(self._on_file_finished)
        self.transcriber.failed.connect(self._on_file_failed)
        if self.meetings is not None:
            self.meetings.progress.connect(self._on_minutes_progress)
            self.meetings.finished.connect(self._on_minutes_finished)
            self.meetings.failed.connect(self._on_minutes_failed)
        self._load()
        for button in self.findChildren(QPushButton):
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        compact = self._short_selects()
        # One width for every short select and one for every number box, so
        # the fields of a page end where the fields above them end; only the
        # model names, the devices and free text stretch with the window.
        for box in compact:
            box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            if isinstance(box, QComboBox):
                box.setFixedWidth(widgets.SELECT_WIDTH)
                # A longer entry is elided in the closed box and shown whole
                # in the list that drops down.
                LocalModelBox._fit_popup(box)
            else:
                # The same short width as the selects: two widths per page,
                # full or short, rather than one per kind of control.
                box.setFixedWidth(max(widgets.SELECT_WIDTH, box.minimumWidth()))
        # The stylesheet's padding does not reach the editor inside a spin
        # box, so its text would sit against the frame while every combo and
        # line edit beside it starts 7 px in.
        for box in self.findChildren(QSpinBox):
            box.lineEdit().setTextMargins(7, 0, 0, 0)
        # A long entry (a model file and its "not downloaded" note) would
        # otherwise set the box's minimum width and push the page sideways.
        for box in self.tabs.findChildren(QComboBox):
            if box not in compact:
                box.setSizeAdjustPolicy(
                    QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                box.setMinimumContentsLength(14)
        # Listed once, in a fixed order. findChildren follows the order of the
        # children, and showing a tab raises its page to the end of that, so a
        # list read again after the user moved between tabs would compare its
        # values against the wrong boxes. The editors' own boxes are left out:
        # a search or a selection there is not an edit, and the editors say
        # when something was edited.
        self._form_boxes = self._boxes()
        self._box_pages = [self._page_of(box) for box in self._form_boxes]
        self._saved_form = self._form_values()
        self._show_dirty()  # Apply and Discard start disabled: nothing to do yet
        for box in self._form_boxes:
            if isinstance(box, (QLineEdit, QPlainTextEdit)):
                box.textChanged.connect(self._show_dirty)
            elif isinstance(box, QComboBox):
                box.currentTextChanged.connect(self._show_dirty)
            elif isinstance(box, QCheckBox):
                box.toggled.connect(self._show_dirty)
            else:
                box.valueChanged.connect(self._show_dirty)
        self.local_threads.valueChanged.connect(self._local_threads_was_changed)
        self.dictionary_editor.changed.connect(self._show_dirty)
        self.profiles_editor.changed.connect(self._show_dirty)
        self.local_whisper.program_changed.connect(self._refresh_processing_devices)
        self.local_whisper.program_changed.connect(self._show_dirty)
        self.local_llm.program_changed.connect(self._show_dirty)
        self._refresh_processing_devices()
        self._load_codex_models()
        self._load_agy_models()
        self._load_hosted_models()
        # Connected after the load, so that filling the boxes in is not taken
        # for the user ticking them.
        self.file_timestamps.toggled.connect(self._remember_file_choices)
        self.file_cleanup.toggled.connect(self._remember_file_choices)
        self.file_speakers.toggled.connect(self._remember_file_choices)
        self.file_wrap.toggled.connect(self._remember_file_choices)
        # On a machine where nothing can transcribe yet, this window was opened
        # because of that, so open it on the tab that fixes it.
        if not conf.transcribe_ready():
            self.tabs.setCurrentIndex(self.api_tab_index)
        # A model takes up to ggml.STARTUP_TIMEOUT to load, so a line written
        # once as the window opens would be wrong for most of the wait. Runs
        # only while the window is on screen: there is nobody to read it
        # otherwise, and it costs a lock and a poll() each time.
        self._page_changed(self.tabs.currentIndex())
        self._local_state_timer = QTimer(self)
        self._local_state_timer.setInterval(2000)
        self._local_state_timer.timeout.connect(self._show_local_state)
        self._show_local_state()

    def _short_selects(self):
        """The boxes that hold a short choice or a number, which share one
        width: every fixed list but the ones naming a device or a model file,
        whose entries are as long as somebody else decided."""
        stretch = {self.mic, self.meeting_mic, self.meeting_system, self.local_device}
        boxes = []
        for box in self.tabs.findChildren((QComboBox, QSpinBox)):
            if box in stretch or isinstance(box.parentWidget(), QSpinBox):
                continue
            owner = box.parentWidget()
            inside = False
            while owner is not None:
                if isinstance(owner, (LocalModelBox, profiles_ui.DictionaryEditor)):
                    inside = True
                    break
                owner = owner.parentWidget()
            # The paste key is typed into as well, but holds a key like the
            # other short selects rather than a model name.
            if inside or (isinstance(box, QComboBox) and box.isEditable()
                          and box is not self.paste_shortcut):
                continue
            boxes.append(box)
        return boxes

    def _boxes(self):
        return [box for box in self.tabs.findChildren(
                    (QLineEdit, QComboBox, QCheckBox, QSpinBox, QPlainTextEdit))
                if not profiles_ui.owned(box)]

    def _page_of(self, widget):
        """The name of the page `widget` is on, or ""."""
        areas = {self.tabs.widget(index): name for name, index in self.page_index.items()}
        while widget is not None:
            if widget in areas:
                return areas[widget]
            widget = widget.parentWidget()
        return ""

    def _form_entries(self):
        """Every value the form holds, each with the page it is on."""
        entries = []
        pages = getattr(self, "_box_pages", None) or [""] * len(self._form_boxes)
        # The editors report their own contents, below.
        for box, page in zip(self._form_boxes, pages):
            if isinstance(box, QPlainTextEdit):
                if not box.isReadOnly():
                    entries.append((page, box.toPlainText()))
            elif isinstance(box, QLineEdit):
                entries.append((page, box.text()))
            elif isinstance(box, QComboBox):
                value = box.currentData()
                entries.append((page, box.currentText() if value is None else value))
            elif isinstance(box, QCheckBox):
                entries.append((page, box.isChecked()))
            else:
                entries.append((page, box.value()))
        models = dict(self._models)
        provider = self.transcribe_provider.currentData()
        if provider in models:
            models[provider] = self.transcribe_model.currentText().strip()
        # What a preset switch stashed, and the model each preset box shows
        # now. The agent's models are on the Assistant page, the rest here.
        custom = dict(self._custom_values)
        for role, name in self._preset_shown.items():
            if role != "transcribe" or provider == "custom":
                custom[cfg.model_setting(role, name)] = self._preset_model(role).currentText().strip()
        agent = {key: custom.pop(key) for key in list(custom)
                 if key.startswith("assistant_") and key.endswith("_model")}
        entries.extend((
            ("api", models),
            ("api", custom),
            ("assistant", agent),
            ("api", self.local_whisper.custom_binary),
            ("api", self.local_llm.custom_binary),
            ("editing", self.dictionary_editor.values()),
            ("profiles", self.profiles_editor.values()),
        ))
        return entries

    def _form_values(self):
        return [value for _page, value in self._form_entries()]

    def refresh_configuration(self):
        """Reload clean forms while keeping unsaved edits for a later merge."""
        if self._form_values() == self._saved_form:
            self._load()
            self._saved_form = self._form_values()
            self._show_dirty()

    def refresh_sources(self):
        """Discover new devices without replacing an in-progress selection."""
        self._sources = audio.list_sources()
        monitors = audio.list_monitors()
        for box, title, sources in (
            (self.mic, "Default microphone", self._sources),
            (self.meeting_mic, "Same as dictation", self._sources),
            (self.meeting_system, "Current output", monitors),
        ):
            selected = box.currentData() or ""
            with QSignalBlocker(box):
                box.clear()
                box.addItem(t(title), "")
                for name, description in sources:
                    box.addItem(description, name)
                if selected and box.findData(selected) < 0:
                    box.addItem(t("{name} (not connected)", name=selected), selected)
                self._select_data(box, selected)
        self._show_dirty()

    def _show_dirty(self, *_):
        entries = self._form_entries()
        saved = getattr(self, "_saved_form", [])
        dirty = [value for _page, value in entries] != saved
        self.dirty_label.setText(t("Unsaved changes") if dirty else "")
        self.apply_button.setEnabled(dirty)
        self.discard_button.setEnabled(dirty)
        # Which pages hold the changes, as a dot on their sidebar entry: an
        # edit made three pages ago is otherwise easy to apply unawares.
        if len(saved) == len(entries):
            pages = {page for (page, value), before in zip(entries, saved)
                     if value != before}
        else:
            pages = {page for page, _value in entries} if dirty else set()
        if pages != getattr(self, "_dirty_pages", None):
            self._dirty_pages = pages
            self._paint_nav()

    # ---- navigation ------------------------------------------------------

    def show_page(self, name):
        """Open the page called `name` (see PAGES)."""
        self.tabs.setCurrentIndex(self.page_index[name])

    def current_page(self):
        index = self.tabs.currentIndex()
        return next((name for name, i in self.page_index.items() if i == index), "")

    def _page_changed(self, index):
        if self.nav.currentRow() != index:
            self.nav.setCurrentRow(index)
        if 0 <= index < len(PAGES):
            self.page_title.setText(t(PAGES[index][1]))
            area = self.tabs.widget(index)
            if isinstance(area, QScrollArea) and area.widget() is not None:
                area.widget().installEventFilter(self)
        self._align_title()

    def _align_title(self):
        """Start the title where the page under it starts."""
        area = self.tabs.currentWidget()
        page = area.widget() if isinstance(area, QScrollArea) else None
        if page is None or not page.isVisible():
            return
        left = page.mapTo(self, page.rect().topLeft()).x()
        margins = page.layout().contentsMargins() if page.layout() else None
        inner = margins.left() if margins else 0
        origin = self.tabs.mapTo(self, self.tabs.rect().topLeft()).x()
        self.title_row.setContentsMargins(max(0, left - origin + inner), 0, 0, 0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show):
            area = self.tabs.currentWidget()
            if isinstance(area, QScrollArea) and area.widget() is watched:
                QTimer.singleShot(0, self._align_title)
        return super().eventFilter(watched, event)

    def _apply_theme(self, name):
        theme.apply(self, name)
        self._paint_nav()
        for badge in self.findChildren((widgets.GlyphLabel, widgets.Badge, widgets.GlyphButton)):
            badge.restyle()

    def _paint_nav(self):
        """The sidebar's glyphs, with a dot on every page holding an edit."""
        colors = theme.palette(self.property("dikte_theme") or theme.DEFAULT)
        dirty = getattr(self, "_dirty_pages", set())
        for row, (name, _title, short, glyph) in enumerate(PAGES):
            item = self.nav.item(row)
            changed = name in dirty
            item.setIcon(widgets.glyph(glyph, colors["text"], 18,
                                       dot=colors["accent"] if changed else None,
                                       aside=True))
            item.setToolTip(t("{page} (unsaved changes)", page=t(short)) if changed
                            else t(short))

    def _set_compact(self, compact):
        """Below a comfortable width the sidebar keeps its icons only."""
        if compact == self._compact:
            return
        self._compact = compact
        for row, (_name, _title, short, _glyph) in enumerate(PAGES):
            self.nav.item(row).setText("" if compact else t(short))
        self.side.setFixedWidth(64 if compact else 196)
        QTimer.singleShot(0, self._align_title)

    def resizeEvent(self, event):
        self._set_compact(event.size().width() < 800)
        super().resizeEvent(event)

    def _discard_changes(self):
        self._load()
        self._saved_form = self._form_values()
        self._show_dirty()

    def showEvent(self, event):
        super().showEvent(event)
        self._show_local_state()
        self._local_state_timer.start()

    def hideEvent(self, event):
        self._local_state_timer.stop()
        super().hideEvent(event)

    def _show_local_state(self):
        """What each model on this machine is loaded on, as it is now."""
        local = ggml.state()
        self.local_state.setText(
            self._local_state_text(ggml.WHISPER, local.get("whisper", {})))
        self.local_llm_state.setText(
            self._local_state_text(ggml.LLAMA, local.get("llama", {})))
        self._sync_local_rows(local)

    def _sync_local_rows(self, local=None):
        """The loaded-on line under each local model, only once it is loaded.

        "Not loaded" is the state of every model nobody has used yet, and says
        nothing the Model row above it does not.
        """
        local = ggml.state() if local is None else local
        stt = (self.transcribe_provider.currentData() or "local") == "local"
        cleaner = (self.cleanup_provider.currentData() or "openrouter") == "local"
        self.stt_form.setRowVisible(
            self.local_state, stt and ggml.accel_kind(local.get("whisper", {})) != "off")
        self.cleanup_form.setRowVisible(
            self.local_llm_state,
            cleaner and ggml.accel_kind(local.get("llama", {})) != "off")

    @staticmethod
    def _local_state_text(program, entry):
        """One line: whether the model is loaded, and what it ended up on.

        Four answers rather than two, because "could not tell" is a real one: a
        whisper built by hand on a Mac prints nothing about its backend, and
        answering "the processor" there would be a confident lie about the one
        thing this line exists to be honest about.
        """
        kind = ggml.accel_kind(entry)
        if kind == "off":
            return t("Not loaded.")
        # The backend and the card keep the names the server printed for them.
        detail = ggml.accel_detail(entry)
        if kind == "unknown":
            return t("Loaded; it did not say what it is running on.")
        if kind == "gpu":
            return t("Loaded on the graphics card ({detail}).", detail=detail)
        if not entry.get("gpu_wanted"):
            return t("Loaded on the processor ({detail}).", detail=detail)
        if not ggml.cpu_only_loaded(entry):
            return t("Loaded on the processor: the graphics card is switched "
                     "on, but could not be used.")
        return t("Loaded on the processor: only the CPU backend was loaded. "
                 "Check the server log for graphics backend or driver errors.")

    def _scrolled(self, page, cap=CONTENT_WIDTH):
        """A tab that scrolls instead of growing the window to fit."""
        # Every tab goes through here. A page kept at its full height passes
        # that height on as the window's minimum, and a tall one (the API tab
        # is the tallest, and taller still under a large interface font) then
        # pushes Save off the bottom of the screen with no way to shrink back.
        page.setMaximumWidth(cap)
        if page.layout() is not None:
            page.layout().setContentsMargins(PAGE_GUTTER, 4, PAGE_GUTTER, 16)
        area = QScrollArea()
        area.setWidgetResizable(True)
        # Centred in the pane, title and all: a capped page against the
        # sidebar leaves the right third of a wide window empty. The pages
        # that have more to show go to two columns before they get here.
        area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        # Never sideways: a page wider than the window is a layout bug, and the
        # combo boxes below elide rather than push the page out.
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(page)
        for box in page.findChildren((QComboBox, QAbstractSpinBox)):
            # Focus by click or by tab, not by wheel. Qt hands the focus over
            # before it delivers the wheel, so a box left on the default policy
            # would have it by the time the guard below asked.
            box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            box.installEventFilter(self._wheel_guard)
        return area

    def _size_to_screen(self, width, height):
        """Open at this size, or at whatever the screen has room for."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            room = screen.availableGeometry()
            # Room for the titlebar and a little air, so the window is grabbable
            # and the buttons along the bottom stay on screen.
            width = min(width, room.width() - 40)
            height = min(height, room.height() - 80)
        # Scrolling tabs ask for no height of their own, which leaves nothing to
        # stop the window being dragged down to a tab bar and half a button. The
        # floor is a floor and not a demand: it never asks for more room than
        # the screen has just been found to have.
        self.setMinimumSize(min(520, width), min(380, height))
        self.resize(width, height)

    # ---- tabs ----------------------------------------------------------

    @staticmethod
    def _section(title):
        """A titled block of form rows, the unit every page is built from:
        a bold title above a framed form. Returns the block (to add, show
        and hide) and its form."""
        return widgets.section(title)

    @staticmethod
    def _align_labels(*boxes):
        """One label column for every section of a page: as wide as the
        longest label on it, up to widgets.LABEL_MAX, where a longer label
        wraps rather than pushing every field to the right. Sibling cards
        then start their controls at the same x. A row with no label (a lone
        checkbox, a status line, a row of buttons) spans the whole width
        rather than sitting behind an empty label. Forms nested in a section
        (box.subforms) share the column. Returns its width."""
        forms = []
        for box in boxes:
            form = getattr(box, "form", None)
            if not isinstance(form, QFormLayout):
                continue
            forms += [form] + list(getattr(box, "subforms", []))
        return widgets.align_form(forms) if forms else 0

    @staticmethod
    def _advanced(box, form, *rows):
        """Tuning most people never touch, behind an Advanced disclosure at
        the end of `box`. `rows` are (label, widget) pairs; returns the
        disclosure and the form inside it."""
        holder, inner = widgets.subform(box)
        for label, widget in rows:
            inner.addRow(label, widget)
        disclosure = widgets.Disclosure(t("Advanced"), holder)
        form.addRow(disclosure)
        return disclosure, inner

    def _page(self, *sections):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(4)
        for section in sections:
            layout.addWidget(section)
        layout.addStretch(1)
        self._align_labels(*sections)
        return page

    def _two_columns(self, rows, order=None):
        """A page whose sections pair up in rows on a wide window (see
        widgets.Columns) and stack in one column on a narrow one."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(4)
        page.columns = widgets.Columns(rows, order=order)
        layout.addWidget(page.columns)
        layout.addStretch(1)
        sections = [section for row in rows for cell in row
                    for section in (cell if isinstance(cell, (list, tuple)) else [cell])]
        self._align_labels(*sections)
        return page

    def _general_tab(self):
        recording, form = self._section(t("Recording"))
        self.mic = QComboBox()
        self.mic.addItem(t("Default microphone"), "")
        for name, desc in self._sources_once():
            self.mic.addItem(desc, name)
        form.addRow(t("Microphone"), self.mic)

        self.language = QComboBox()
        for label, code in LANGUAGES:
            self.language.addItem(t(label), code)
        form.addRow(t("Speech language"), self.language)

        self.max_seconds = QSpinBox()
        self.max_seconds.setRange(10, 3600)
        self.max_seconds.setSuffix(t(" s"))
        form.addRow(t("Longest recording"), self.max_seconds)

        self.skip_silent = QCheckBox(t("Skip silent recordings"))
        self.skip_silent.setToolTip(t(
            "A recording with no speech in it is dropped without calling the API."))
        form.addRow("", self.skip_silent)

        self.silence_db = QSpinBox()
        self.silence_db.setRange(-80, -20)
        self.silence_db.setSuffix(" dB")
        self.silence_db.setToolTip(t(
            "Speech also has to rise {margin} dB above the recording's own noise "
            "floor, so this absolute floor rarely needs touching. Lower it if quiet "
            "speech gets dropped; raise it if noise still gets through.",
            margin=10,
        ))

        self.filter_hallucinations = QCheckBox(t("Remove phrases invented during silence"))
        self.filter_hallucinations.setToolTip(
            t("Discard stock phrases models invent for near-silent audio") + ". "
            + t("Whisper answers silence with things like “Thanks for watching”.")
        )
        form.addRow("", self.filter_hallucinations)

        self.audio_cues = QCheckBox(t("Play audio cues when recording changes"))
        self.audio_cues.setToolTip(t(
            "A short pattern marks recording start, stop, completion and failure."
        ))
        form.addRow("", self.audio_cues)

        self.keep_audio = QCheckBox(t("Keep the audio files"))
        self.keep_audio.setToolTip(t("Saved in {path}", path=str(cfg.RECORDINGS_DIR)))
        form.addRow("", self.keep_audio)
        self._advanced(recording, form, (t("Silence threshold"), self.silence_db))

        pasting, form = self._section(t("Pasting"))
        self.auto_paste = QCheckBox(t("Paste the text into the focused window"))
        form.addRow("", self.auto_paste)
        self.pasting_form = form

        self.paste_shortcut = QComboBox()
        # A shortlist of the combinations that usually paste, not the set of
        # them: a stored one this desktop does not offer is kept as it is
        # rather than quietly replaced by the first item on the list.
        self.paste_shortcut.setEditable(True)
        self.paste_shortcut.addItems([key_label(k) for k in paste.desktop().shortcuts])
        self.paste_shortcut.setToolTip(t(
            "macOS asks for Accessibility permission the first time this is sent."
            if paste.desktop() is paste.MACOS else
            "Terminals usually want ctrl+shift+v. Change this if pasting does nothing."
        ))
        # In the label column like every other row. It means nothing while
        # pasting is off, so it is greyed out then rather than hidden: the
        # combination stays in view for whoever turns pasting back on.
        self.paste_key_label = QLabel(t("Paste key"))
        form.addRow(self.paste_key_label, self.paste_shortcut)
        self.auto_paste.toggled.connect(self._sync_paste_key)

        self.restore_clipboard = QCheckBox(t("Restore the previous clipboard after pasting"))
        form.addRow("", self.restore_clipboard)

        application, form = self._section(t("Application"))
        self.ui_language = QComboBox()
        for label, code in UI_LANGUAGES:
            self.ui_language.addItem(t(label), code)
        form.addRow(t("Interface language"), self.ui_language)

        self.start_in_tray = QCheckBox(t("Start in the tray when I log in"))
        self.start_in_tray.setToolTip(
            t("Only the start at login. Opening Dikte from the menu always "
              "shows the window, and so does a first start that still needs "
              "setting up.")
        )
        form.addRow("", self.start_in_tray)

        self.history_limit = QSpinBox()
        self.history_limit.setRange(0, 10000)
        self.history_limit.setSpecialValueText(t("no limit"))
        self.history_limit.setSuffix(t(" entries"))
        self.history_limit.setToolTip(t(
            "Once the history passes this many entries, the oldest one is dropped "
            "every time a new one arrives. Set it to 0 to keep everything."
        ))
        form.addRow(t("History size"), self.history_limit)

        self.update_check = QCheckBox(t("Look for a newer version once a day"))
        self.update_check.setToolTip(
            t("Dikte only looks. What it finds opens the release page in your "
              "browser; it downloads and installs nothing by itself.")
        )
        form.addRow(t("Updates"), self.update_check)

        self.update_status = WrappedLabel("")
        self.update_status.setObjectName("muted")
        self.update_page = QPushButton(t("Open the release page"))
        self.update_page.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self._release_url))
        )
        self.update_now = QPushButton(t("Check now"))
        self.update_now.clicked.connect(self._check_for_update)
        form.addRow("", self._row(self.update_status, self.update_page,
                                  self.update_now))
        appearance = self._appearance_section()
        # Wide: two rows of two, paired by height so that neither card in a
        # row is left with much empty space (Recording is the tall one).
        # Narrow: one column, recording and its look first.
        return self._two_columns([(recording, application), (appearance, pasting)],
                                 order=[recording, appearance, pasting, application])

    def _sync_paste_key(self, *_):
        on = self.auto_paste.isChecked()
        self.paste_shortcut.setEnabled(on)
        self.paste_key_label.setEnabled(on)

    def _appearance_section(self):
        # The theme and the recording indicator in one card: a card holding
        # the theme alone was a frame around a single box.
        appearance, form = self._section(t("Appearance"))
        self.theme_choice = QComboBox()
        for name, label in theme.NAMES.items():
            self.theme_choice.addItem(t(label), name)
        self.theme_choice.currentIndexChanged.connect(
            lambda: self._apply_theme(self.theme_choice.currentData()))
        form.addRow(t("Theme"), self.theme_choice)

        self.indicator_screen = QComboBox()
        # The active screen rather than the pointer, for the reason in
        # overlay._compositor_screen: it is what a compositor will answer for,
        # and on Plasma the two are one screen only where the active screen is
        # set to follow the mouse.
        self.indicator_screen.addItem(t("Follow the active screen"), "")
        for screen in QGuiApplication.screens():
            # The native resolution, so that a scaled 4K screen reads
            # 3840 × 2160 and not the 1920 × 1080 Qt sees through the scale.
            area = screen.geometry()
            ratio = screen.devicePixelRatio()
            self.indicator_screen.addItem(
                t("{name} ({width} × {height})", name=screen.name(),
                  width=round(area.width() * ratio),
                  height=round(area.height() * ratio)),
                screen.name(),
            )
        form.addRow(t("Indicator screen"), self.indicator_screen)

        # Only the screen it appeared on is decided when it appears; this is
        # what makes it keep up with a session that moves to another one
        # mid-recording. The active screen and not the pointer, because that is
        # what a compositor will answer for: on Plasma the two are the same
        # screen only where the active screen is set to follow the mouse, and
        # otherwise it is the focused window that decides. Nothing to offer
        # when a screen is named above, since that name is the whole answer.
        self.follow_pointer = QCheckBox(t("Move it when the active screen changes"))
        self.indicator_screen.currentIndexChanged.connect(self._sync_follow_pointer)
        form.addRow("", self.follow_pointer)

        self.corner = QComboBox()
        for value in CORNERS:
            self.corner.addItem(t(CORNER_LABELS[value]), value)
        form.addRow(t("Indicator corner"), self.corner)
        return appearance

    def _sync_follow_pointer(self):
        self.follow_pointer.setEnabled(not self.indicator_screen.currentData())

    def _preset_panel(self, role):
        """The Custom server rows of one job: which endpoint, its key, its
        address and, for the chat jobs, its model. A frameless block of form
        rows, so it sits in the section's own grid; its form is handed to
        _align_labels as a subform to share the label column."""
        box = QWidget()
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(9)
        form.setHorizontalSpacing(14)
        box.form = form
        preset = QComboBox()
        for name, who in cfg.PRESETS.items():
            if getattr(who, "transcription" if role == "transcribe" else "chat"):
                preset.addItem(t(who.service), name)
        key = QLineEdit()
        key.setEchoMode(QLineEdit.EchoMode.Password)
        # The same tick, Test and answer line as the named services' keys.
        tick = widgets.GlyphLabel("check", "accent", 16)
        tick.setToolTip(t("Key set"))
        tick.setAccessibleName(t("Key set"))
        policy = tick.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        tick.setSizePolicy(policy)
        tick.hide()
        key.textChanged.connect(lambda text: tick.setVisible(bool(text.strip())))
        test = QPushButton(t("Test"))
        test.clicked.connect(lambda: self._test_preset(role))
        answer = QLabel("")
        answer.setObjectName("muted")
        answer.setWordWrap(True)
        answer.hide()
        key_holder = QWidget()
        key_column = QVBoxLayout(key_holder)
        key_column.setContentsMargins(0, 0, 0, 0)
        key_column.setSpacing(4)
        key_column.addWidget(self._row(key, tick, test))
        key_column.addWidget(answer)
        self._testers[f"preset:{role}"] = (test, answer)
        url = QLineEdit()
        url.setPlaceholderText("http://localhost:8000/v1")
        url.setAccessibleName(t("Custom server address"))
        form.addRow(t("Preset"), preset)
        form.addRow(t("Key"), key_holder)
        form.addRow(t("Address"), url)
        rows = {"preset": preset, "key": key, "url": url}
        if role != "transcribe":
            model = QComboBox()
            model.setEditable(True)
            fetch = QPushButton(t("Fetch model list"))
            fetch.clicked.connect(lambda: self._load_preset_models(role))
            form.addRow(t("Model"), self._row(model, fetch))
            rows.update(model=model, fetch=fetch)
        self._preset_widgets[role] = rows
        preset.currentIndexChanged.connect(lambda: self._preset_changed(role))
        key.editingFinished.connect(lambda: self._share_preset_key(role))
        url.editingFinished.connect(lambda: self._share_preset_key(role))
        return box

    def _test_preset(self, role):
        """Whether the key in a job's Custom server rows opens its endpoint."""
        self._share_preset_key(role)
        rows = self._preset_widgets[role]
        name = rows["preset"].currentData() or "other"
        key = rows["key"].text().strip() or self.conf.api_key(cfg.PRESETS[name].key)
        base = rows["url"].text().strip()
        self.__dict__.setdefault("_preset_tested", {})[role] = name
        self._test_key(f"preset:{role}", lambda: key_check(name, key, base))

    def _preset_model_settings(self):
        return {cfg.model_setting(role, name) for role in self._preset_widgets
                for name in cfg.PRESETS}

    def _preset_model(self, role):
        return (self.transcribe_model if role == "transcribe"
                else self._preset_widgets[role]["model"])

    def _stash_preset(self, role):
        name = self._preset_shown.get(role)
        if name is None:
            return
        rows = self._preset_widgets[role]
        self._custom_values[cfg.PRESETS[name].key] = rows["key"].text().strip()
        if role != "transcribe" or self._shown_provider == "custom":
            setting = cfg.model_setting(role, name)
            model = self._preset_model(role).currentText().strip()
            self._custom_values[setting] = model or (cfg.DEFAULTS[setting] if name != "other" else "")
        if name == "other":
            self._custom_values["custom_base_url"] = rows["url"].text().strip()

    def _share_preset_key(self, role):
        self._stash_preset(role)
        name = self._preset_shown.get(role)
        for other, shown in self._preset_shown.items():
            if other != role and shown == name:
                rows = self._preset_widgets[other]
                rows["key"].setText(self._custom_values[cfg.PRESETS[name].key])
                if name == "other":
                    rows["url"].setText(self._custom_values["custom_base_url"])

    def _preset_changed(self, role):
        self._share_preset_key(role)
        rows = self._preset_widgets[role]
        name = rows["preset"].currentData() or "other"
        self._preset_shown[role] = name
        who = cfg.PRESETS[name]
        rows["key"].setText(self._custom_values[who.key])
        rows["key"].setPlaceholderText(t("Paste your key (optional)") if name == "other"
                                          else t("Paste your key"))
        rows["key"].setAccessibleName(t("{service} key", service=t(who.service)
                                           if name != "other" else t("Custom server")))
        rows["url"].setText(self._custom_values.get(
            "custom_base_url" if name == "other" else f"{name}_base_url", who.base_url))
        rows["url"].setReadOnly(name != "other")
        rows["url"].setToolTip(t("Custom server address") if name == "other"
                                  else t("Choose Other to edit the address."))
        rows["key"].setToolTip(t("Left empty, the {variable} environment variable is used.",
                                    variable=who.key.upper()))
        # An answer about the previous endpoint says nothing about this one.
        _button, answer = self._testers[f"preset:{role}"]
        answer.clear()
        answer.hide()
        if role != "transcribe" or self._shown_provider == "custom":
            self._show_preset_model(role)
        if role == "transcribe" and self._shown_provider == "custom":
            self._say_transcribe("")

    def _show_preset_model(self, role):
        name = self._preset_shown.get(role, "other")
        combo = self._preset_model(role)
        combo.clear()
        combo.setCurrentText(self._custom_values[cfg.model_setting(role, name)])
        # Short, since the box shares its row with Fetch in a narrow column;
        # what empty means is the tooltip.
        combo.lineEdit().setPlaceholderText(
            t("Server default") if name == "other"
            else cfg.DEFAULTS[cfg.model_setting(role, name)]
            or t("Pick a model from the list"))
        combo.setToolTip(t("Empty: the server's default model") if name == "other" else "")
        if role == "transcribe":
            self.refresh_transcribe_models.setVisible(name != "requesty")

    def _load_preset_models(self, role):
        self._share_preset_key(role)
        rows = self._preset_widgets[role]
        name = rows["preset"].currentData()
        key = rows["key"].text().strip() or self.conf.api_key(cfg.PRESETS[name].key)
        base = rows["url"].text().strip()
        button = self.refresh_transcribe_models if role == "transcribe" else rows["fetch"]
        button.setEnabled(False)

        def work():
            try:
                models = api.preset_models(name, key, base, transcription=role == "transcribe")
                self._preset_models_loaded.emit(role, name, models, "")
            except api.ApiError as exc:
                self._preset_models_loaded.emit(role, name, [], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_preset_models_loaded(self, role, name, models, error):
        rows = self._preset_widgets[role]
        button = self.refresh_transcribe_models if role == "transcribe" else rows["fetch"]
        button.setEnabled(True)
        if rows["preset"].currentData() != name:
            return
        if role == "transcribe" and self._shown_provider != "custom":
            return
        if error:
            if role == "transcribe":
                self._say_transcribe(t("Could not fetch the list: {error}", error=error))
            else:
                QMessageBox.warning(self, t("Custom server"), error)
            return
        combo = self._preset_model(role)
        current = combo.currentText()
        combo.clear()
        combo.addItems(models)
        combo.setCurrentText(current)
        if role == "transcribe":
            self._say_transcribe(t("{count} models loaded.", count=len(models)))

    def _api_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setSpacing(6)

        # One key per named service, each in a holder that moves: the key of
        # the transcription provider sits under that provider, the cleanup
        # provider's under cleanup, and the rest wait, collapsed, under Other
        # connections. A key no longer belongs to a single job, so it is never
        # in two places and never missing from all of them. Gateway keys live
        # in the Custom server rows of the job that uses them.
        self._key_holders = {}
        self._key_homes = {}
        self._key_names = []
        others = QWidget()
        self.other_keys = QVBoxLayout(others)
        self.other_keys.setContentsMargins(0, 0, 0, 0)
        self.other_keys.setSpacing(8)
        # "Paste key" in every field; the environment variable a key may come
        # from instead is the field's tooltip, not a placeholder to decode.
        self.openai_key = self._key_row("openai", "OPENAI_API_KEY", self._test_openai)
        self.openrouter_key = self._key_row(
            "openrouter", "OPENROUTER_API_KEY", self._test_openrouter)
        self.other_connections = widgets.Disclosure(t("Other connections"), others)

        stt, stt_form = self._section(t("Speech to text"))
        self.transcribe_provider = QComboBox()
        for label, value in TRANSCRIBE_PROVIDERS:
            self.transcribe_provider.addItem(t(label), value)
        stt_form.addRow(t("Provider"), self.transcribe_provider)
        self.stt_key_slot = self._key_slot()
        stt_form.addRow(t("Key"), self.stt_key_slot)
        self.transcribe_custom_box = self._preset_panel("transcribe")
        stt_form.addRow(self.transcribe_custom_box)
        stt.subforms.append(self.transcribe_custom_box.form)

        # A hosted provider takes any model id that is typed at it; the local
        # one offers what has been published. One row each, and only the rows of
        # whoever is chosen are on screen.
        self.stt_form = stt_form
        self.transcribe_model = QComboBox()
        self.transcribe_model.setEditable(True)
        self.refresh_transcribe_models = QPushButton(t("Fetch model list"))
        self.refresh_transcribe_models.clicked.connect(self._load_transcribe_models)
        self.transcribe_model_row = self._row(self.transcribe_model,
                                              self.refresh_transcribe_models)
        stt_form.addRow(t("Model"), self.transcribe_model_row)
        # OpenRouter only: which of its models a timestamped run asks for.
        self.file_model = QComboBox()
        self.file_model.setEditable(True)
        # Empty in the settings means whisper-1; said as a choice of its own
        # rather than a grey placeholder that reads as a disabled box.
        self.file_model_default = t("{model} (default)", model=api.OPENROUTER_FILE_MODEL)
        self.file_model.addItem(self.file_model_default)
        self.file_model.setToolTip(
            t("The model a timestamped audio file (subtitles) is sent to. Not every model "
              "on OpenRouter returns segment times; empty means openai/whisper-1."))
        self.file_model_row = self._row(self.file_model)
        stt_form.addRow(t("Audio file model"), self.file_model_row)
        # A spanning row: in the narrow field column a wrapped label gets a
        # height that fits one line, and the rest of the text is cut off.
        self.transcribe_status = widgets.StatusLine()
        stt_form.addRow(self.transcribe_status)

        self.local_whisper = LocalModelBox(
            ggml.WHISPER, t("On this machine"),
            ggml.whisper_models, ggml.whisper_model_path)
        stt_form.addRow(self.local_whisper)
        stt.subforms.append(self.local_whisper.form)

        self.local_models_dir = QLineEdit()
        self.local_models_dir.setPlaceholderText(str(ggml.DATA_DIR / "models"))
        self.local_models_dir.setToolTip(t(
            "The folder shared by the local whisper and llama models. Choose "
            "an ASCII-only path on Windows if your user folder contains "
            "characters whisper.cpp cannot open. Empty uses the default."
        ))
        choose_models_dir = QPushButton(t("Choose…"))
        choose_models_dir.clicked.connect(self._choose_models_dir)
        self.models_dir_row = self._row(self.local_models_dir, choose_models_dir)

        self.local_device = QComboBox()
        self.local_device.addItem(t("Automatic (whisper.cpp default)"), "auto")
        self.local_device.addItem(t("Processor (CPU)"), "cpu")
        self._graphics_devices = ()
        self._selectable_graphics_devices = ()
        LocalModelBox._fit_popup(self.local_device)
        self.local_device.setToolTip(t(
            "Automatic lets whisper.cpp choose a graphics card through Metal, "
            "CUDA, ROCm or Vulkan when its build supports one. Processor keeps "
            "all speech recognition on the CPU."
        ))
        self.local_device.currentIndexChanged.connect(
            self._processing_device_changed
        )
        self.local_preload = QCheckBox(t("Load the model when Dikte starts"))
        self.local_preload.setToolTip(
            t("A large model takes a second or two to load. Loading it up front "
              "spends that once instead of on the first dictation, at the cost of "
              "the memory it sits in."))
        self.local_threads = QSpinBox()
        self._local_threads_changed = False
        self.local_threads.setRange(0, hardware.cpu_threads())
        self.local_threads.setSpecialValueText(t("Automatic"))
        self.local_threads.setToolTip(t(
            "CPU-thread limit available to this process: {count}. "
            "Automatic lets whisper.cpp choose. More threads are not always faster.",
            count=self.local_threads.maximum(),
        ))
        # A spin box asks for room for its numbers:
        # the word standing in for zero is what actually has to fit, and on
        # macOS, where the stepper sits inside the frame, it does not. Widened
        # to the word rather than to a number picked by eye, so that it still
        # fits once the word is "Otomatik".
        self.local_threads.setMinimumWidth(
            self.local_threads.fontMetrics()
            .horizontalAdvance(t("Automatic")) + 56)
        self.local_options = QWidget()
        options_form = self.local_options_form = QFormLayout(self.local_options)
        options_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        options_form.setContentsMargins(0, 0, 0, 0)
        options_form.setVerticalSpacing(9)
        options_form.setHorizontalSpacing(14)
        stt.subforms.append(options_form)
        options_form.addRow(t("Processing device"), self.local_device)
        options_form.addRow("", self.local_preload)
        # The same disclosure as every other Advanced, holding the threads.
        self.local_advanced, _threads_form = self._advanced(
            stt, options_form, (t("CPU threads"), self.local_threads))
        self.local_advanced_toggle = self.local_advanced.button
        stt_form.addRow(self.local_options)
        # What the model is actually doing, as against what the boxes above
        # ask for. The checkbox can only ask: whether a card was found is
        # decided by the build and by the machine, and is read back off the
        # server's own log once it has loaded.
        self.local_state = WrappedLabel("")
        self.local_state.setObjectName("muted")
        stt_form.addRow(self.local_state)

        self.transcribe_provider.currentIndexChanged.connect(self._provider_changed)

        orr, orr_form = self._section(t("Transcript cleanup"))
        self.cleanup_form = orr_form
        self.cleanup_enabled = QCheckBox(t("Clean the transcript with a model"))
        orr_form.addRow("", self.cleanup_enabled)

        self.cleanup_provider = QComboBox()
        for label, value in CLEANUP_PROVIDERS:
            self.cleanup_provider.addItem(t(label), value)
        self.cleanup_provider.currentIndexChanged.connect(self._cleanup_provider_changed)
        self.cleanup_enabled.toggled.connect(lambda *_: self._place_keys())
        orr_form.addRow(t("Provider"), self.cleanup_provider)
        self.cleanup_key_slot = self._key_slot()
        orr_form.addRow(t("Key"), self.cleanup_key_slot)

        self.cleanup_model = QComboBox()
        self.cleanup_model.setEditable(True)
        self.cleanup_model.addItems(CLEANUP_MODELS)
        self.refresh_models = QPushButton(t("Fetch model list"))
        self.refresh_models.clicked.connect(self._load_models)
        self.cleanup_model_row = self._row(self.cleanup_model, self.refresh_models)
        orr_form.addRow(t("Model"), self.cleanup_model_row)

        # One row per provider rather than one box that means a different thing
        # in each: an OpenRouter id and a Claude alias do not belong in the same
        # field, and only the row of whoever is chosen is on screen.
        self.cleanup_claude_model = QComboBox()
        self.cleanup_claude_model.setEditable(True)
        self.cleanup_claude_model.addItems(CLEANUP_CLAUDE_MODELS)
        self.cleanup_claude_model.setToolTip(_typed_model_note("Claude Code"))
        orr_form.addRow(t("Model"), self.cleanup_claude_model)

        self.cleanup_codex_model = QComboBox()
        self.cleanup_codex_model.setEditable(True)
        self.cleanup_codex_model.addItems([t("Codex's own default")] + CODEX_MODELS)
        self.cleanup_codex_model.setToolTip(_typed_model_note("Codex"))
        orr_form.addRow(t("Model"), self.cleanup_codex_model)

        self.cleanup_custom_box = self._preset_panel("cleanup")
        orr_form.addRow(self.cleanup_custom_box)
        orr.subforms.append(self.cleanup_custom_box.form)

        self.cleanup_agy_model = QComboBox()
        self.cleanup_agy_model.setEditable(True)
        self.cleanup_agy_model.addItems([t("Antigravity's own default")] + AGY_MODELS)
        orr_form.addRow(t("Model"), self.cleanup_agy_model)

        self.cleanup_reasoning = QComboBox()
        for label, value in REASONING_LEVELS:
            self.cleanup_reasoning.addItem(t(label), value)
        self.cleanup_reasoning.setToolTip(
            t("How long a thinking model may reason before it answers. Cleanup is "
              "a light job, so more thinking mostly costs time and tokens. Models "
              "that cannot think ignore this.")
        )

        # Only says something for a provider that is a program on this
        # machine: where it was found, or that it was not.
        self.models_label = widgets.StatusLine()
        orr_form.addRow(self.models_label)

        self.local_llm = LocalModelBox(
            ggml.LLAMA, t("On this machine"),
            ggml.llm_quants, ggml.llm_model_path,
            repos=ggml.llm_repos)
        orr_form.addRow(self.local_llm)
        orr.subforms.append(self.local_llm.form)

        self.local_llm_gpu = QCheckBox(t("Use the graphics card"))
        self.local_llm_preload = QCheckBox(t("Load the model when Dikte starts"))
        self.local_llm_preload.setToolTip(
            t("An LLM is slower to load than a whisper model and sits in more "
              "memory. Off means it is loaded on the first cleanup instead."))
        self.local_llm_reasoning = QComboBox()
        for label, value in REASONING_LEVELS:
            self.local_llm_reasoning.addItem(t(label), value)
        self.local_llm_reasoning.setToolTip(
            t("A model trained to think will think unless it is told not to, and "
              "spending 300 tokens of reasoning on a comma is 300 tokens of "
              "waiting. Off is what cleanup wants."))
        self.local_llm_options = QWidget()
        llm_form = QFormLayout(self.local_llm_options)
        llm_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        llm_form.setContentsMargins(0, 0, 0, 0)
        llm_form.setVerticalSpacing(9)
        llm_form.setHorizontalSpacing(14)
        orr.subforms.append(llm_form)
        llm_form.addRow("", self.local_llm_gpu)
        llm_form.addRow("", self.local_llm_preload)
        orr_form.addRow(self.local_llm_options)
        self.local_llm_state = WrappedLabel("")
        self.local_llm_state.setObjectName("muted")
        orr_form.addRow(self.local_llm_state)
        # How hard either kind of cleanup model thinks: one row each, since
        # the two default to opposite things, and only the chosen one shows.
        self.cleanup_advanced, self.cleanup_advanced_form = self._advanced(
            orr, orr_form, (t("Thinking"), self.cleanup_reasoning),
            (t("Thinking"), self.local_llm_reasoning))


        # One box for both servers rather than a row inside each: what is being
        # decided is whether this machine keeps gigabytes tied up between
        # dictations, and that is not a question anybody wants to answer once
        # per model.
        self.local_box, local_form = self._section(t("Models on this machine"))
        local_form.addRow(t("Model folder"), self.models_dir_row)
        self.local_idle_unload = QCheckBox(t("Unload a model that is sitting unused"))
        self.local_idle_unload.setToolTip(
            t("A loaded model holds its memory whether anything is using it or "
              "not: over a gigabyte for whisper, several for an LLM. Unloading "
              "gives that back to the rest of the desktop, and the next "
              "dictation loads it again at the cost of the seconds that takes."))
        self.local_idle_minutes = QSpinBox()
        self.local_idle_minutes.setRange(1, 720)
        self.local_idle_minutes.valueChanged.connect(self._idle_suffix)
        self._idle_suffix(self.local_idle_minutes.value())
        self.local_idle_unload.toggled.connect(self.local_idle_minutes.setEnabled)
        local_form.addRow("", self.local_idle_unload)
        local_form.addRow(t("After"), self.local_idle_minutes)
        others_box = QWidget()
        others_column = QVBoxLayout(others_box)
        others_column.setContentsMargins(0, widgets.SECTION_GAP, 0, 0)
        others_column.addWidget(self.other_connections)
        # Wide: speech to text on the left, cleanup on the right. Narrow: one
        # column, speech to text first.
        # The models on this machine serve both sides, and the other keys are
        # a list of their own, so each takes a full row under the pair.
        page.columns = widgets.Columns([(stt, orr), (self.local_box,), (others_box,)],
                                       order=[stt, self.local_box, orr, others_box])
        outer.addWidget(page.columns)
        # The keys under Other connections line up with the sections above.
        width = self._align_labels(stt, orr, self.local_box)
        for name in self._key_names:
            name.setMinimumWidth(width)
        self.other_keys.setContentsMargins(15, 6, 15, 0)

        outer.addStretch(1)
        return page

    def _idle_suffix(self, minutes):
        """The spin box's own noun, since its lowest value is one of them.

        Turkish is handed both and translates them the same: a number there is
        followed by the singular however many it counts.
        """
        self.local_idle_minutes.setSuffix(
            t(" minute") if minutes == 1 else t(" minutes"))

    def _refresh_local_box(self):
        """The idle unload is only on screen when something here runs locally."""
        self.local_box.setVisible(
            (self.transcribe_provider.currentData() or "local") == "local"
            or (self.cleanup_provider.currentData() or "openrouter") == "local"
        )

    def _prompt_tab(self):
        # Two jobs, two sets of rules: dictation is rewritten for reading, an
        # audio file becomes subtitles that have to stay in sync with the voice.
        combined = t(
            "Your own instruction replaces the built-in one. The dictionary, "
            "and the style and translation of the profile in use, are still "
            "added after it. Switch the profile style off under Profiles to "
            "use your instruction alone.")
        modes = (
            (t("Dictation"), t("System instruction given to the cleanup model. This is where you "
                               "decide how much it may touch your words.") + "\n\n" + combined,
             cfg.default_cleanup_prompt),
            (t("Audio file"), t("Used instead when an audio or video file is cleaned up. It is "
                                "written for subtitles: lines stay where they are, nothing is "
                                "shortened, and misheard words are repaired from the context."),
             cfg.default_file_cleanup_prompt),
        )
        rules, rules_form = self._section(t("Cleanup instructions"))
        # A segmented switch between the two, each with its status line and
        # its editor collapsed until asked for.
        segments = QHBoxLayout()
        segments.setContentsMargins(0, 0, 0, 0)
        segments.setSpacing(0)
        self.prompt_modes = QButtonGroup(self)
        self.prompt_stack = QStackedWidget()
        self.prompt_info = widgets.InfoIcon(modes[0][1], topic=t("Cleanup instructions"))
        editors = []
        for index, (title, tip, default) in enumerate(modes):
            button = QPushButton(title)
            button.setObjectName("segment")
            button.setProperty("edge", "first" if index == 0 else "last")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setToolTip(tip)
            self.prompt_modes.addButton(button, index)
            segments.addWidget(button)
            box, holder = self._instructions(default)
            self.prompt_stack.addWidget(holder)
            editors.append(box)
        segments.addSpacing(8)
        segments.addWidget(self.prompt_info)
        segments.addStretch(1)

        def show_mode(index):
            self.prompt_stack.setCurrentIndex(index)
            self.prompt_info.set_text(modes[index][1])
        self.prompt_modes.idClicked.connect(show_mode)
        self.prompt_modes.button(0).setChecked(True)
        holder = QWidget()
        holder.setLayout(segments)
        rules_form.addRow(holder)
        rules_form.addRow(self.prompt_stack)
        self.cleanup_prompt, self.file_cleanup_prompt = editors
        # How a custom instruction and the profiles combine, said where the
        # instruction is written; Config.cleanup_prompt is the rule itself.
        self.cleanup_prompt.setToolTip(combined)

        # The dictionary is what this page is mostly used for, so it comes
        # first; it is as tall as its words, up to a few screens' worth of
        # rows, and the cards of both columns start at the same top.
        self.dictionary_editor = profiles_ui.DictionaryEditor()
        dictionary = QWidget()
        column = QVBoxLayout(dictionary)
        column.setContentsMargins(0, widgets.SECTION_GAP, 0, 0)
        column.setSpacing(6)
        heading = QLabel(t("Dictionary"))
        heading.setObjectName("section")
        column.addWidget(heading)
        frame = QFrame()
        frame.setObjectName("sectionbox")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(widgets.CARD_PAD, 12, widgets.CARD_PAD, 12)
        inner.addWidget(self.dictionary_editor)
        column.addWidget(frame, 1)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(4)
        page.columns = widgets.Columns([(dictionary, rules)], threshold=900)
        # Stacked, a few rows keep the instructions below in view; beside
        # them the list has a column of its own to fill.
        page.columns.on_arrange = lambda wide: self.dictionary_editor.set_max_rows(
            12 if wide else profiles_ui.DictionaryEditor.MAX_ROWS)
        layout.addWidget(page.columns)
        layout.addStretch(1)
        return page

    def _assistant_tab(self):
        how, how_form = self._section(t("How it runs"))
        self.assistant_how_form = how_form
        self._shortcut_link(how_form, "ask", t("Shortcut"))

        self.assistant_provider = QComboBox()
        for label, value in ASSISTANT_PROVIDERS:
            self.assistant_provider.addItem(t(label), value)
        self.assistant_provider.currentIndexChanged.connect(
            self._assistant_provider_changed
        )
        how_form.addRow(t("Provider"), self.assistant_provider)
        self.assistant_found = widgets.StatusLine()
        how_form.addRow("", self.assistant_found)

        self.assistant_dir = QLineEdit()
        self.assistant_dir.setPlaceholderText(os.path.expanduser("~"))
        dir_tip = t(
            "The directory the command runs in, which decides which project's "
            "instructions and files it can see. Your own skills and services "
            "are there whichever one it is."
        )
        self.assistant_dir.setToolTip(dir_tip)
        browse = QPushButton(t("Choose…"))
        browse.clicked.connect(self._choose_assistant_dir)
        self.assistant_dir_row = self._row(self.assistant_dir, browse)
        how_form.addRow(t("Working directory"), self.assistant_dir_row)

        # What is sent: the spoken command, cleaned up first or not. It
        # belongs with the provider, not with what comes back.
        self.assistant_cleanup = QCheckBox(t("Clean the transcript up before sending it"))
        self.assistant_cleanup.setToolTip(t(
            "Off by default: Claude reads through “erm” and “you know” without "
            "help, and cleanup costs an API call and a second or two."
        ))
        how_form.addRow("", self.assistant_cleanup)

        # One scale for all four: how hard to think is one thing to want, and
        # each provider is handed the nearest rung it actually has.
        self.assistant_reasoning = QComboBox()
        for label, value in REASONING_LEVELS:
            self.assistant_reasoning.addItem(t(label), value)
        self.assistant_reasoning.setToolTip(t(
            "More thinking is slower, and you are standing in front of the "
            "screen while it happens. Worth it for a job that has to be worked "
            "out rather than looked up."
        ))

        self.assistant_timeout = QSpinBox()
        self.assistant_timeout.setRange(15, 3600)
        self.assistant_timeout.setSuffix(t(" s"))
        self.assistant_timeout.setToolTip(t(
            "A command still running after this is given up on. The tray menu "
            "can stop one earlier."
        ))
        self._advanced(how, how_form, (t("Thinking"), self.assistant_reasoning),
                       (t("Give up after"), self.assistant_timeout))

        # One box per provider, only the chosen one on screen: they have nothing
        # in common past the model, and four sets of half-relevant fields would
        # be worse than none.
        self.claude_box, claude_form = self._section(t("Claude Code"))
        self.assistant_model = QComboBox()
        self.assistant_model.setEditable(True)
        self.assistant_model.addItems(ASSISTANT_MODELS)
        self.assistant_model.setToolTip(t(
            "A name like “sonnet” always means the newest model of that line. "
            "Opus thinks harder and answers slower, which is felt here more "
            "than anywhere else: you are standing in front of the screen."
        ) + " " + _typed_model_note("Claude Code"))
        claude_form.addRow(t("Model"), self.assistant_model)
        self.assistant_permission = QComboBox()
        for label, value in PERMISSION_MODES:
            self.assistant_permission.addItem(t(label), value)
        claude_form.addRow(t("Permissions"), self.assistant_permission)

        self.codex_box, codex_form = self._section(t("Codex"))
        self.assistant_codex_model = QComboBox()
        self.assistant_codex_model.setEditable(True)
        self.assistant_codex_model.addItem(t("Codex's own default"), "")
        for name in CODEX_MODELS:
            self.assistant_codex_model.addItem(name, name)
        self.assistant_codex_model.setToolTip(_typed_model_note("Codex"))
        codex_form.addRow(t("Model"), self.assistant_codex_model)
        self.assistant_codex_sandbox = QComboBox()
        for label, value in CODEX_SANDBOXES:
            self.assistant_codex_sandbox.addItem(t(label), value)
        codex_form.addRow(t("Sandbox"), self.assistant_codex_sandbox)

        # The chat providers answer and do nothing else; one short line says
        # so, and the tooltip what that means.
        chat_line = t("Answers questions; runs no commands.")

        def chat_tip(service):
            return t(
                "A plain question and a plain answer, over the {service} key you "
                "already have. It runs no commands, opens no files and reaches "
                "none of your services, so it can tell you what the capital of "
                "Peru is but not what is in your calendar.", service=service)

        self.openrouter_box, or_form = self._section(t("Model"))
        self.assistant_openrouter_model = QComboBox()
        self.assistant_openrouter_model.setEditable(True)
        self.assistant_openrouter_model.addItems(ASSISTANT_OR_MODELS)
        or_form.addRow(t("Model"), self.assistant_openrouter_model)
        or_form.addRow("", _with_info(_note(chat_line), chat_tip("OpenRouter")))

        self.agy_box, agy_form = self._section(t("Antigravity"))
        self.assistant_agy_model = QComboBox()
        self.assistant_agy_model.setEditable(True)
        self.assistant_agy_model.addItem(t("Antigravity's own default"), "")
        for name in AGY_MODELS:
            self.assistant_agy_model.addItem(name, name)
        agy_form.addRow(t("Model"), self.assistant_agy_model)
        agy_form.addRow("", _with_info(
            _note(t("Uses its own allow-rules for permissions.")),
            t("Antigravity has neither a permission mode nor a sandbox to hand "
              "it, so what it may do without asking is whatever its own "
              "allow-rules say. The working directory still applies.")))

        # Every OpenAI-compatible gateway, and a server of your own.
        self.assistant_custom_box, custom_form = self._section(t("Custom server"))
        self.assistant_custom_panel = self._preset_panel("assistant")
        custom_form.addRow(self.assistant_custom_panel)
        self.assistant_custom_box.subforms.append(self.assistant_custom_panel.form)
        custom_form.addRow("", _with_info(_note(chat_line), chat_tip(t("Custom server"))))

        thread, thread_form = self._section(t("The conversation"))
        self.assistant_session_minutes = QSpinBox()
        self.assistant_session_minutes.setRange(0, 1440)
        self.assistant_session_minutes.setSuffix(t(" min"))
        self.assistant_session_minutes.setSpecialValueText(t("every command on its own"))
        self.assistant_session_minutes.setToolTip(t(
            "Commands within this long of each other are one conversation, so "
            "“and move that to Thursday” knows what “that” is. After it, the "
            "next command starts fresh."
        ))
        thread_form.addRow(t("Reset after inactivity"), self.assistant_session_minutes)
        reset = QPushButton(t("New conversation"))
        reset.setToolTip(t("Start a new conversation now"))
        reset.clicked.connect(self._reset_assistant_session)
        self.assistant_session_status = QLabel("")
        self.assistant_session_status.setObjectName("muted")
        # One line beside its button at every width: the state is a few words.
        self.assistant_session_status.setWordWrap(False)
        thread_form.addRow("", self._row(self.assistant_session_status, reset))

        # What happens to the reply, in the same card: a card of its own held
        # this one checkbox.
        self.assistant_paste = QCheckBox(t("Paste the answer into the focused window"))
        self.assistant_paste.setToolTip(t(
            "It is copied to the clipboard either way."
        ))
        thread_form.addRow("", self.assistant_paste)

        # Its own switch and its own key, so that turning it on changes
        # nothing about the spoken command above.
        selected, selected_form = self._section(t("Selected text"))
        self.selection_assist = QCheckBox(t("Ask about the selected text"))
        self.selection_assist.setToolTip(t(
            "Select text, press the shortcut and say what to do with it. The "
            "answer opens in a window; nothing is pasted until you press "
            "Replace selection. The selection goes to the assistant with no "
            "tools, and Codex and Antigravity are not sent it. The history "
            "keeps your instruction and the reply, not the selection."))
        selected_form.addRow("", self.selection_assist)
        # The link to the Shortcuts page is in How it runs already.
        self._shortcut_link(selected_form, "assist", t("Shortcut"), link=False)

        instructions, instructions_form = self._section(t("Instructions"))
        self.assistant_prompt, editor = self._instructions(cfg.default_assistant_prompt)
        self.assistant_prompt.setToolTip(t(
            "Told to the assistant alongside every command, on top of "
            "whatever your own configuration already says."))
        instructions_form.addRow(editor)
        # Wide: how it runs beside the conversation, the provider's own box
        # (one is shown at a time) beside the selection, and the instructions,
        # an editor once opened, across the whole width. Narrow: one column,
        # how it runs first.
        providers = [self.claude_box, self.codex_box, self.openrouter_box,
                     self.agy_box, self.assistant_custom_box]
        return self._two_columns([(how, thread), (providers, selected), (instructions,)])

    def _meeting_tab(self):
        sources, sources_form = self._section(t("Sound"))
        self.meeting_mic = QComboBox()
        self.meeting_mic.addItem(t("Same as dictation"), "")
        for name, desc in self._sources_once():
            self.meeting_mic.addItem(desc, name)
        sources_form.addRow(t("Microphone"), self.meeting_mic)

        self.meeting_system = QComboBox()
        self.meeting_system.addItem(t("Current output"), "")
        for name, desc in audio.list_monitors():
            self.meeting_system.addItem(desc, name)
        sources_form.addRow(t("The other participants"), self.meeting_system)

        if audio.sound() is audio.COREAUDIO:
            sources_form.addRow("", _with_info(
                _note(t("Needs BlackHole or Loopback on macOS.")),
                t("macOS does not offer what the speakers are playing as something "
                  "to record. Install BlackHole or Loopback, send the meeting's "
                  "sound through it, and pick it above.")))
        elif not audio.sound().meetings:
            # Windows is the system this is written for: it offers nothing that
            # captures what the speakers are playing, and there is no driver to
            # install that would put an entry in the list above. Left unsaid,
            # the box is simply empty and the Record button fails at the press.
            sources_form.addRow("", widgets.Notice(
                t("Meetings cannot be recorded on this system."),
                t("This system offers nothing that records what the speakers are "
                  "playing, so a meeting cannot be recorded on it. Dictation and "
                  "transcribing a file are unaffected.")))

        sources_form.addRow("", _with_info(
            _note(t("Wear headphones if you can.")),
            t("Through speakers your microphone hears the other side as well, "
              "and although a line that lands on both channels at once is "
              "dropped again, the repair is never as clean as not needing it.")))

        people, people_form = self._section(t("Who is talking"))
        self.meeting_self_name = QLineEdit()
        self.meeting_self_name.setPlaceholderText(t("Me"))
        people_form.addRow(t("You"), self.meeting_self_name)
        self.meeting_other_name = QLineEdit()
        self.meeting_other_name.setPlaceholderText(t("Other side"))
        people_form.addRow(t("The other end"), self.meeting_other_name)
        self.meeting_participants = QPlainTextEdit()
        self.meeting_participants.setMaximumHeight(70)
        self.meeting_participants.setPlaceholderText(t("One name per line"))
        people_tip = t(
            "Everyone on the far end shares one label: they reach you as a single "
            "mixed signal. The names go to the transcription model so they come "
            "out spelled right, and to the minutes, which may use one for a line "
            "only when the conversation itself makes clear who was speaking."
        )
        self.meeting_participants.setToolTip(people_tip)
        people_form.addRow(t("Expected"), self.meeting_participants)
        people_form.addRow("", _with_info(
            _note(t("Names help the spelling and the minutes.")), people_tip))

        models, models_form = self._section(t("Minutes"))
        self.meeting_model = QComboBox()
        self.meeting_model.setEditable(True)
        self.meeting_model.addItems(MEETING_MODELS)
        models_form.addRow(t("Model"), self.meeting_model)
        self.meeting_reasoning = QComboBox()
        for label, value in REASONING_LEVELS:
            self.meeting_reasoning.addItem(t(label), value)
        self.meeting_reasoning.setToolTip(t(
            "Unlike cleanup, this one is worth some thinking: it has to hold a "
            "whole meeting in its head and work out what was actually decided."
        ))
        self.meeting_language = QComboBox()
        self.meeting_language.addItem(t("Same as dictation"), "")
        for label, code in LANGUAGES:
            self.meeting_language.addItem(t(label), code)
        models_form.addRow(t("Speech language"), self.meeting_language)
        self.meeting_cleanup = QCheckBox(t("Clean the transcript up first"))
        self.meeting_cleanup.setToolTip(t(
            "Runs the cleanup model over the transcript before the minutes are "
            "written, keeping the timestamps and the speaker labels."
        ))
        models_form.addRow("", self.meeting_cleanup)
        self._advanced(models, models_form, (t("Thinking"), self.meeting_reasoning))
        self.meeting_prompt, editor = self._instructions(cfg.default_meeting_prompt)
        self.meeting_prompt.setToolTip(t("System instruction given to the minutes model."))
        models_form.addRow(editor)

        recording, recording_form = self._section(t("Recording"))
        self.meeting_max_minutes = QSpinBox()
        self.meeting_max_minutes.setRange(5, 600)
        self.meeting_max_minutes.setSuffix(t(" min"))
        recording_form.addRow(t("Longest meeting"), self.meeting_max_minutes)
        self.meeting_keep_audio = QCheckBox(
            t("Keep the recording after the minutes are written")
        )
        self.meeting_keep_audio.setToolTip(t(
            "A run that fails keeps its recording either way, so it can be tried "
            "again from the Minutes tab. This is about the ones that worked."
        ))
        recording_form.addRow("", self.meeting_keep_audio)
        self._shortcut_link(recording_form, "meeting", t("Shortcut"))
        # Wide: paired by height, sound beside the recording limits and the
        # people beside the minutes. Narrow: the order the page always had.
        return self._two_columns([(sources, recording), (people, models)],
                                 order=[sources, people, models, recording])

    @staticmethod
    def _card(*children, spacing=8):
        """A filled card with the one padding every card uses (widgets.card)."""
        return widgets.card(*children, name="card", spacing=spacing)

    def _more_button(self, actions_list):
        """A "More" button holding the actions that are not the main ones."""
        more = QToolButton()
        more.setObjectName("more")
        more.setText("")
        more.setIcon(widgets.glyph("more", theme.palette(self.conf["theme"])["text"], 16))
        more.setToolTip(t("More"))
        more.setAccessibleName(t("More"))
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        actions = {}
        for label, handler in actions_list:
            if label is None:
                menu.addSeparator()
                continue
            action = menu.addAction(label)
            action.triggered.connect(handler)
            actions[label] = action
        more.setMenu(menu)
        more.actions_ = actions
        return more

    def _minutes_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        self.minutes_list = widgets.row_list(QListWidget(), lines=1)
        self.minutes_list.setAccessibleName(t("Minutes"))
        self.minutes_list.currentItemChanged.connect(self._show_minutes)
        self.minutes_list.setMinimumHeight(90)
        self.minutes_list.setProperty("card", True)

        self.minutes_status = QLabel("")
        self.minutes_status.setObjectName("muted")
        self.minutes_status.setWordWrap(True)

        # Rendered: headings and lists read as headings and lists. Copy and
        # the file on disk stay the Markdown that was written.
        self.minutes_view = widgets.guard_links(QTextBrowser())
        self.minutes_view.setPlaceholderText(t("Pick a meeting to read it."))
        self.minutes_view.setMinimumHeight(90)
        self.minutes_view.setAccessibleName(t("Minutes"))
        self._minutes_text = ""

        # Secondary: the one filled button on that screen is Record a meeting.
        copy = self.minutes_copy = QPushButton(t("Copy"))
        copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self._minutes_text)
        )
        copy.setEnabled(False)
        self.minutes_retry = QPushButton(t("Write it up"))
        self.minutes_retry.clicked.connect(self._retry_minutes)
        self.minutes_retry.setEnabled(False)
        self.minutes_retry.hide()
        more = self.minutes_more = self._more_button([
            (t("Open the folder"), lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(cfg.MEETINGS_DIR)))),
            (None, None),
            (t("Delete selected"), self._delete_minutes),
        ])
        # Kept by reference: the label is translated, and the language can
        # change under a window that was built in the other one.
        self.minutes_delete = more.actions_[t("Delete selected")]
        self.minutes_delete.setEnabled(False)
        widgets.roomy(copy, self.minutes_retry)
        row = QHBoxLayout()
        row.setSpacing(widgets.BUTTON_GAP)
        row.addWidget(self.minutes_status, 1)
        row.addWidget(copy)
        row.addWidget(self.minutes_retry)
        row.addWidget(more)
        self.minutes_actions = row
        widgets.cap_reading(self.minutes_view)
        # The reader shows the minutes, or while a meeting records and its
        # row is the one picked, a live panel the home window hands in.
        self.minutes_view.document().setDocumentMargin(0)
        self.minutes_reader = QStackedWidget()
        self.minutes_reader.flush = True
        self.minutes_reader.addWidget(self.minutes_view)
        self.minutes_live_panel = None
        reader = self._card((self.minutes_reader, 1), row)
        # Stacked, the list opens two rows tall (the live recording among
        # them) and the minutes, which are what this page is read for, take
        # the rest. The handle still lets it grow.
        self.minutes_split = widgets.Split(self.minutes_list, reader, side=300,
                                           threshold=820, splitter=True, name="minutes",
                                           share=0.0, min_rows=2)
        layout.addWidget(self.minutes_split, 1)
        return page

    def _file_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        options = QWidget()
        layout = QVBoxLayout(options)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        pick = QPushButton(t("Choose file…"))
        pick.clicked.connect(self._choose_file)
        # The file is dropped here or picked with the button inside; the
        # zone says which one is chosen. The whole pane takes a drop, so the
        # zone itself needs no more height than its name and button.
        self.file_drop = widgets.DropZone(t("Drop an audio or video file here"), pick)
        self.file_drop.setToolTip(t("Transcribe an existing audio or video file with the same models."))
        self.file_drop.dropped.connect(self._use_file)
        self.file_drop.accept_drops(options)
        self.file_label = self.file_drop.title
        layout.addWidget(self.file_drop)
        layout.addSpacing(4)

        self.file_timestamps = QCheckBox(t("Add timestamps"))
        self._timestamps_tip = t(
            "Prefixes every segment with [mm:ss]. Uses whisper-1 on whichever "
            "provider you picked, the only model that returns segment times.")
        self.file_timestamps.setToolTip(self._timestamps_tip)
        layout.addWidget(self.file_timestamps)

        # Hidden rather than greyed out: most models cannot do it, and a box
        # that is always off says nothing about the one that is selected.
        self.file_speakers = QCheckBox(t("Tell the speakers apart"))
        self.file_speakers.setToolTip(
            t("Puts who is talking in front of every line. Only offered for the "
              "models known to label speakers.")
        )
        layout.addWidget(self.file_speakers)

        self.file_cleanup = QCheckBox(t("Run the cleanup model afterwards"))
        self.file_cleanup.setToolTip(
            t("With its own rules, under Cleanup rules: written for subtitles, so "
              "the lines keep their place and nothing is shortened.")
        )
        layout.addWidget(self.file_cleanup)

        self.file_wrap = QCheckBox(t("Break long subtitle lines"))
        self.file_wrap.setToolTip(
            t("Splits a cue longer than 42 characters into balanced lines in "
              "the .srt. The text and timing stay the same.")
        )
        layout.addWidget(self.file_wrap)

        # Transcribe and Stop take turns in one place: only the one that can
        # be pressed is there.
        self.file_run = QPushButton(t("Transcribe"))
        self.file_run.setObjectName("primary")
        self.file_run.clicked.connect(self._run_file)
        self.file_stop = QPushButton(t("Stop"))
        self.file_stop.clicked.connect(self._stop_file)
        self.file_stop.setEnabled(False)
        self.file_stop.hide()
        widgets.roomy(self.file_run, self.file_stop)
        self.file_run.setMinimumWidth(widgets.PRIMARY_MIN)
        self.file_stop.setMinimumWidth(widgets.PRIMARY_MIN)
        run_row = QHBoxLayout()
        run_row.setContentsMargins(0, 6, 0, 0)
        run_row.setSpacing(widgets.BUTTON_GAP)
        run_row.addWidget(self.file_run)
        run_row.addWidget(self.file_stop)
        run_row.addStretch(1)
        layout.addLayout(run_row)

        self.file_status = QLabel("")
        self.file_status.setObjectName("muted")
        self.file_status.setWordWrap(True)
        self.file_status.hide()
        layout.addWidget(self.file_status)
        # The file, its options and Transcribe stay together at the top.
        layout.addStretch(1)

        self.file_output = QPlainTextEdit()
        self.file_output.setPlaceholderText(t("The transcript will appear here."))
        self.file_output.setAccessibleName(t("Transcript"))
        self.file_output.setMinimumHeight(100)
        widgets.cap_reading(self.file_output)

        copy = QPushButton(t("Copy"))
        copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.file_output.toPlainText())
        )
        save = QPushButton(t("Save as .txt"))
        save.clicked.connect(self._save_transcript)
        # There only when the run gave segment times to make subtitles from.
        self.file_save_srt = QPushButton(t("Save as .srt"))
        self.file_save_srt.setToolTip(
            t("Subtitles, timed from the segments. Needs the timestamps option.")
        )
        self.file_save_srt.clicked.connect(self._save_subtitles)
        self._set_srt(False)
        widgets.roomy(copy, save, self.file_save_srt)
        out_row = QHBoxLayout()
        out_row.setSpacing(widgets.BUTTON_GAP)
        out_row.addStretch(1)
        out_row.addWidget(copy)
        out_row.addWidget(save)
        out_row.addWidget(self.file_save_srt)
        transcript = self._card((self.file_output, 1), out_row)
        card = self._card((options, 1))
        controls = QWidget()
        stack = QVBoxLayout(controls)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.addWidget(card, 1)
        self.file_split = widgets.Split(controls, transcript, side=330, threshold=820,
                                        fit_first=True)
        outer.addWidget(self.file_split, 1)
        return page

    def _set_srt(self, available):
        """Offer the subtitles only when the transcript has times to give them."""
        self.file_save_srt.setEnabled(available)
        self.file_save_srt.setVisible(available)

    def _shortcut_tab(self):
        # Every global key on one page, in one section per feature, and each
        # section one form: two forms would give each row a label column of
        # its own, and boxes starting at different places read as unrelated
        # settings rather than the set they are. The feature pages name their
        # key and link here.
        keys, form = self._section(t("Dictation"))
        self._shortcut_row(
            form, "toggle", t("Start and stop"), placeholder="Ctrl+Space",
        )
        # The point of holding a recording is that something else came up, and
        # something else is exactly when a hand is not free for a menu.
        self._shortcut_row(
            form, "pause", t("Pause and resume"),
            tooltip=t("Holds the recording without ending it. Nothing said "
                      "while it is paused is kept, and the clock stops with it."),
            missing_tip=t("No global shortcut installed. The tray menu holds it too."),
        )
        # Stopping is what sends the recording off to be transcribed, and that
        # is the step there is no taking back. By the time the tray menu is
        # open the sentence you did not mean to dictate is already on its way.
        self._shortcut_row(
            form, "cancel", t("Discard"),
            tooltip=t("Throws the recording away without transcribing it. Works "
                      "on a dictation and on a command for the assistant alike, "
                      "whichever is running."),
            missing_tip=t("No global shortcut installed. The tray menu discards it too."),
        )

        asking, asking_form = self._section(t("Assistant"))
        self._shortcut_row(
            asking_form, "ask", t("Spoken command"),
            missing_tip=t("No global shortcut installed. The tray menu asks it too."),
        )
        box = self._shortcut_row(asking_form, "assist", t("Selected text"))
        # The key does nothing while the feature is off, so it is greyed out,
        # all but Remove: a key installed earlier can still be taken back.
        row = box.parentWidget()
        self._assist_shortcut = [box, asking_form.labelForField(row)] + [
            button for button in row.findChildren(QPushButton)
            if button.text() != t("Remove")]
        # Off, the key does nothing: a link to the switch that turns it on,
        # rather than directions to it.
        self.assist_enable_link = QPushButton(t("Enable in Assistant"))
        self.assist_enable_link.setObjectName("link")
        self.assist_enable_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.assist_enable_link.setAutoDefault(False)
        self.assist_enable_link.setToolTip(t("Turns on Ask about the selected text."))
        self.assist_enable_link.clicked.connect(self._go_enable_assist)
        asking_form.addRow("", self.assist_enable_link)
        self._assist_link_form = asking_form
        self.selection_assist.toggled.connect(self._enable_assist_shortcut)
        self._enable_assist_shortcut(False)

        meeting_keys, meeting_form = self._section(t("Meeting"))
        self._shortcut_row(
            meeting_form, "meeting", t("Start and stop"),
            missing_tip=t("No global shortcut installed. The tray menu starts a meeting too."),
        )

        how, how_form = self._section(t("How shortcuts work here"))
        self.evdev_enabled = QCheckBox(t("Use the built-in listener (/dev/input)"))
        self.evdev_enabled.setToolTip(t(
            "For when the {desktop} shortcut is not active yet. Works "
            "immediately, no session restart. The only difference: the key "
            "combination also reaches the focused application.",
            desktop=hotkey.desktop_name()))
        # Nothing to wait for where nothing is installed: there the listener is
        # the mechanism, always on, and not a choice to offer. The row is not
        # added at all then: a hidden form row still left its spacing behind.
        if hotkey.installs_shortcuts():
            how_form.addRow("", self.evdev_enabled)
        else:
            self.evdev_enabled.setParent(how)
            self.evdev_enabled.hide()

        if hotkey.shortcut_needs_restart():
            line = t("Installed shortcuts start working after you log in again.")
            explanation = t(
                "KWin only reads shortcut settings at startup. After 'Install' the "
                "shortcut shows up under System Settings → Shortcuts, but it will "
                "not fire until you log out and back in. Until then, use the "
                "built-in listener."
            )
        elif hotkey.installs_shortcuts():
            line = explanation = t("The shortcut starts working as soon as it is installed.")
        elif hotkey.backend() in (hotkey.MACOS, hotkey.WINDOWS):
            line = t("Dikte holds these combinations while it runs.")
            explanation = t(
                "Dikte asks macOS for these combinations itself, while it is "
                "running. Nothing is installed, and no other application receives "
                "them in the meantime."
            ) if hotkey.backend() == hotkey.MACOS else t(
                "Dikte asks Windows for these combinations itself, while it is "
                "running. Nothing is installed, and no other application receives "
                "them in the meantime."
            )
        else:
            # The desktops nobody writes a backend for. Saying "installed" here
            # would be the old bug in words: there is no registry, the listener
            # is the whole mechanism, and both of its costs are permanent
            # rather than lasting until the next login.
            line = t("Dikte listens for these combinations while it runs.")
            explanation = t(
                "{desktop} keeps no shortcut registry, so Dikte listens for "
                "these combinations itself while it is running. Your user has "
                "to be able to read /dev/input for that, and the focused "
                "application receives the keys as well. To have the desktop own "
                "them instead, bind this command in its own configuration, with "
                "the last word swapped for pause, cancel, ask or meeting:",
                desktop=hotkey.desktop_name(),
            )
        how_form.addRow(_with_info(_note(line), explanation))
        if hotkey.backend() == hotkey.LISTENER:
            command = QLineEdit(ipc.command_for("toggle"))
            command.setReadOnly(True)
            command.setCursorPosition(0)
            command.setToolTip(explanation)
            how_form.addRow(t("Desktop command"), command)
        # Wide: the dictation keys and the meeting's on the left, the
        # assistant's and how they work on the right.
        return self._two_columns([(keys, asking), (meeting_keys, how)])

    def _history_tab(self):
        """The history as one list, the page's whole width and height, with
        the search over it. Each row is the tool: its copy button, its More
        menu, Ctrl+C, a double click or Enter. There is no reader: Open puts
        an entry in a window of its own."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(widgets.BUTTON_GAP)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText(t("Search the history"))
        self.history_search.setClearButtonEnabled(True)
        self.history_search.setAccessibleName(t("Search the history"))
        self.history_search.addAction(
            widgets.glyph("search", theme.palette(self.conf["theme"])["muted"], 16),
            QLineEdit.ActionPosition.LeadingPosition)
        self.history_search.textChanged.connect(self._filter_history)
        layout.addWidget(self.history_search)

        # Rows copied a moment ago, by key, and the timer that lets their
        # confirmation go again.
        self._copied_keys = set()
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(1500)
        self._copied_timer.timeout.connect(self._end_copied)
        self.history = widgets.row_list(QListWidget(), lines=4,
                                        actions=self._history_row_actions, slots=2,
                                        flash=self._history_flash, flash_word=t("Copied"))
        self.history.setAccessibleName(t("History"))
        self.history.setAccessibleDescription(t(
            "Enter or a double click copies the entry; Ctrl+C copies the selection. "
            "The context menu has the other actions."))
        self.history.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self._history_menu)
        for keys, handler in ((QKeySequence.StandardKey.Delete, self._delete_history),
                              (QKeySequence.StandardKey.Copy, self._copy_history),
                              (QKeySequence(Qt.Key.Key_Return), self._copy_history),
                              (QKeySequence(Qt.Key.Key_Enter), self._copy_history)):
            shortcut = QShortcut(keys, self.history)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(handler)
        self.history.itemDoubleClicked.connect(lambda _item: self._copy_history())
        self.history.itemSelectionChanged.connect(self._sync_history_actions)
        # The row just chosen, all of it: a half-visible selection reads as
        # nothing being selected.
        self.history.currentItemChanged.connect(
            lambda item, _old: QTimer.singleShot(0, lambda: self._reveal_history(item)))
        self.history.setMinimumHeight(90)
        # A filled card like every other list, not an outlined frame.
        self.history.setProperty("card", True)
        layout.addWidget(self.history, 1)

        # What a row offers beyond copying, in its More menu and in the
        # context menu alike, enabled for what is selected.
        def action(label, handler, tip=""):
            item = QAction(label, self)
            if tip:
                item.setToolTip(tip)
            item.triggered.connect(handler)
            return item
        self.history_open = action(t("Open"), lambda: self.history_action.emit("open"),
                                   t("Open selected text"))
        self.history_copy = action(t("Copy"), self._copy_history,
                                   t("Copy selected to clipboard"))
        # What was heard, for a row whose text is a translation of it.
        self.history_original = action(
            t("Copy original"), self._copy_original,
            t("Copy the transcript as it was heard, before it was translated."))
        self.history_retry = action(
            t("Try again"), self._retry_history,
            t("Transcribe the kept recording of a failed dictation again. The "
              "text goes to the clipboard."))
        # A failed entry whose recording is gone cannot be retried: what is
        # left is to say it again, or to look at why it failed.
        self.history_record = action(t("Record again"),
                                     lambda: self.history_action.emit("record"))
        self.history_check = action(t("Check connection"),
                                    lambda: self.history_action.emit("check"),
                                    t("Opens the Models page of the settings."))
        self.history_delete = action(t("Delete selected"), self._delete_history)
        self.history_clear = action(t("Clear history"), self._clear_history)
        self._sync_history_actions()
        return page

    def _history_row_actions(self, index):
        """The buttons at the right end of a history row: the one thing the
        row is for (copy it, or for a failure try it again or say it again),
        then More."""
        row = index.data(Qt.ItemDataRole.UserRole) or {}
        number = index.row()
        more = ("more", t("More"), lambda at: self._history_more(at))
        if row.get("failed"):
            if _retryable(row):
                return [("retry", t("Try again"), self._retry_row), more]
            return [("mic", t("Record again"),
                     lambda _at: self.history_action.emit("record")), more]
        copied = cfg._row_key(row) in self._copied_keys
        return [("check" if copied else "copy", t("Copied") if copied else t("Copy"),
                 self._copy_row), more]

    def _history_flash(self, index):
        row = index.data(Qt.ItemDataRole.UserRole) or {}
        return t("Copied") if cfg._row_key(row) in self._copied_keys else ""

    def _pick_history_row(self, number):
        """Make the row at `number` the selection, unless it already is part
        of it: a button on a selected row acts on the whole selection."""
        item = self.history.item(number)
        if item is not None and not item.isSelected():
            self.history.setCurrentRow(number)

    def _copy_row(self, number):
        self._pick_history_row(number)
        self._copy_history()

    def _retry_row(self, number):
        self._pick_history_row(number)
        self._retry_history()

    def _history_more(self, number):
        self._pick_history_row(number)
        item = self.history.item(number)
        rect = self.history.visualRect(self.history.indexFromItem(item))
        spot = self.history.itemDelegate().action_rects(rect, [None, None])[-1]
        self._history_menu(spot.bottomLeft(), on_row=False)

    def _end_copied(self):
        self._copied_keys = set()
        try:
            self.history.viewport().update()
        except RuntimeError:
            pass  # the window went while the timer ran

    def _instructions(self, default):
        """A system prompt behind an "Edit instructions" disclosure: the box,
        and the disclosure holding it with its Reset button. The status line
        says whether the built-in instruction or your own is in use."""
        box = QPlainTextEdit()
        box.setMinimumHeight(220)
        reset = QPushButton(t("Reset to default"))
        reset.clicked.connect(lambda: box.setPlainText(reflow(default())))
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(box, 1)
        column.addWidget(reset, 0, Qt.AlignmentFlag.AlignRight)
        disclosure = widgets.Disclosure(t("Edit instructions"), content)
        status = _note("")

        def show_state():
            own = box.toPlainText().strip() not in (
                "", reflow(default()), default().strip())
            status.setText(t("Using your own instructions.") if own
                           else t("Using the built-in instructions."))
        box.textChanged.connect(show_state)
        holder = QWidget()
        stack = QVBoxLayout(holder)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(10)
        stack.addWidget(status)
        stack.addWidget(disclosure, 0)
        disclosure.toggled.connect(
            lambda expanded: stack.setStretchFactor(disclosure, 1 if expanded else 0))
        # Opened, the editor is brought into view whole once the page has laid
        # itself out again; otherwise it opens below the fold, one line of it
        # showing above the footer.
        disclosure.toggled.connect(
            lambda expanded: expanded and QTimer.singleShot(60, lambda: _reveal(content)))
        holder.disclosure = disclosure
        return box, holder

    @staticmethod
    def _shortcut_box(placeholder=""):
        """The field a global shortcut is typed or picked in."""
        box = QComboBox()
        box.setEditable(True)
        box.addItems(MAC_SHORTCUTS if hotkey.backend() == hotkey.MACOS
                     else SHORTCUTS)
        box.setCurrentText("")
        if placeholder:
            box.lineEdit().setPlaceholderText(placeholder)
        return box

    def _key_row(self, provider, variable, tester, service="", above=None, optional=False):
        """A key field, its Test button and the line the answer lands on.

        The field and the pair the answer needs are filed under the provider's
        name, so saving, loading and the test handler find them by name rather
        than through three attributes each. The three travel together as one
        holder, which _place_keys moves to wherever the key is needed.
        """
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText(t("Paste your key (optional)") if optional else t("Paste your key"))
        field.setToolTip(t("Left empty, the {variable} environment variable is used.",
                           variable=variable))
        label = service or cfg.TRANSCRIBERS[provider].service
        field.setAccessibleName(t("{service} key", service=label))
        # A tick beside a field that holds a key, so the set ones can be told
        # from the empty ones without revealing either.
        tick = widgets.GlyphLabel("check", "accent", 16)
        tick.setToolTip(t("Key set"))
        tick.setAccessibleName(t("Key set"))
        policy = tick.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        tick.setSizePolicy(policy)
        tick.hide()
        field.textChanged.connect(lambda text, tick=tick: tick.setVisible(bool(text.strip())))
        button = QPushButton(t("Test"))
        button.clicked.connect(tester)
        answer = QLabel("")
        answer.setObjectName("muted")
        answer.setWordWrap(True)
        answer.hide()
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        if above is not None:
            column.addWidget(above)
        column.addWidget(self._row(field, tick, button))
        column.addWidget(answer)
        home = QWidget()
        row = QHBoxLayout(home)
        row.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label)
        name.setMinimumWidth(130)
        name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        name.setContentsMargins(0, 6, 0, 0)
        row.addWidget(name)
        row.setSpacing(14)
        self._key_names.append(name)
        slot = QVBoxLayout()
        slot.setContentsMargins(0, 0, 0, 0)
        row.addLayout(slot, 1)
        slot.addWidget(holder)
        self.other_keys.addWidget(home)
        self._key_fields[provider] = field
        self._testers[provider] = (button, answer)
        self._key_holders[provider] = holder
        self._key_homes[provider] = (home, slot)
        return field

    @staticmethod
    def _key_slot():
        slot = QWidget()
        layout = QVBoxLayout(slot)
        layout.setContentsMargins(0, 0, 0, 0)
        return slot

    def _place_keys(self):
        """Put each provider's key where the job that needs it is set up."""
        if not getattr(self, "_key_holders", None) or not hasattr(self, "cleanup_key_slot"):
            return
        stt = self.transcribe_provider.currentData() or "local"
        cleaner = self.cleanup_provider.currentData() or "openrouter"
        if cleaner == stt or not self.cleanup_enabled.isChecked():
            cleaner = None
        for provider, holder in self._key_holders.items():
            home, home_slot = self._key_homes[provider]
            if provider == stt:
                target = self.stt_key_slot.layout()
            elif provider == cleaner:
                target = self.cleanup_key_slot.layout()
            else:
                target = home_slot
            if target.indexOf(holder) < 0:
                for layout in (self.stt_key_slot.layout(),
                               self.cleanup_key_slot.layout(), home_slot):
                    layout.removeWidget(holder)
                target.addWidget(holder)
                holder.show()
            home.setVisible(target is home_slot)
        self.stt_form.setRowVisible(self.stt_key_slot, stt in self._key_holders)
        self.cleanup_form.setRowVisible(self.cleanup_key_slot, cleaner in self._key_holders)
        set_up = sum(1 for provider, field in self._key_fields.items()
                     if self._key_homes[provider][1].indexOf(self._key_holders[provider]) >= 0
                     and field.text().strip())
        self.other_connections.button.setText(
            t("Other connections ({count} set)", count=set_up) if set_up
            else t("Other connections"))

    def _shortcut_row(self, form, which, label, placeholder="",
                      tooltip="", missing_tip=""):
        """One global shortcut: the combination, Install, Remove, and a line
        saying what the desktop has registered. `missing_tip` is the rest of
        that line when the desktop has nothing registered."""
        missing = t("Not assigned.")
        box = self._shortcut_box(placeholder or t("Not assigned"))
        if tooltip:
            box.setToolTip(tooltip)
        form.addRow(label, self._row(box, *self._install_buttons(
            lambda: self._install_shortcut(which),
            lambda: self._remove_shortcut(which),
        )))
        status = QLabel("")
        status.setObjectName("muted")
        status.setWordWrap(True)
        form.addRow("", status)
        form.setRowVisible(status, False)
        self._shortcut_rows[which] = (box, status, missing)
        self._shortcut_forms[which] = form
        self._shortcut_tips[which] = missing_tip
        box.currentTextChanged.connect(lambda *_: self._refresh_shortcut_status(which))
        return box

    def _shortcut_link(self, form, which, label, link=True):
        """A feature page's line about its key: the combination, and a link
        to the Shortcuts page where it is set (once per page)."""
        current = QLabel("")
        if link:
            button = QPushButton(t("Change in Shortcuts"))
            button.setObjectName("link")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
            button.clicked.connect(lambda: self.show_page("shortcuts"))
            form.addRow(label, self._row(current, button))
        else:
            current.setToolTip(t("Change in Shortcuts"))
            form.addRow(label, current)
        if not hasattr(self, "_shortcut_summaries"):
            self._shortcut_summaries = {}
        self._shortcut_summaries[which] = current

    def _go_enable_assist(self):
        self.show_page("assistant")
        self.selection_assist.setFocus(Qt.FocusReason.OtherFocusReason)
        area = self.tabs.currentWidget()
        if isinstance(area, QScrollArea):
            area.ensureWidgetVisible(self.selection_assist)

    def _enable_assist_shortcut(self, on):
        for widget in self._assist_shortcut:
            if widget is not None:
                widget.setEnabled(on)
        if hasattr(self, "assist_enable_link"):
            self._assist_link_form.setRowVisible(self.assist_enable_link, not on)
        if "assist" in self._shortcut_rows:
            self._refresh_shortcut_status("assist")

    @staticmethod
    def _install_buttons(install_handler, remove_handler):
        """Install and Remove, where this system has somewhere to install into.

        macOS has not, and neither has a Linux desktop that keeps no registry:
        Dikte holds the combination itself while it runs, so there is nothing
        to write down and nothing to take back out.
        """
        if not hotkey.installs_shortcuts():
            return []
        install = QPushButton(t("Install as a {desktop} shortcut",
                                desktop=hotkey.desktop_name()))
        install.clicked.connect(install_handler)
        remove = QPushButton(t("Remove"))
        remove.clicked.connect(remove_handler)
        return [install, remove]

    @staticmethod
    def _row(*widgets):
        """Widgets side by side in one form row; the first one takes the space."""
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        for index, widget in enumerate(widgets):
            layout.addWidget(widget, 1 if index == 0 else 0)
        if widgets and all(isinstance(widget, QPushButton) for widget in widgets):
            layout.addStretch()
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # ---- load / save ----------------------------------------------------

    def _load(self):
        conf = self.conf
        self._select_data(self.ui_language, conf["ui_language"])
        self._select_data(self.theme_choice, conf["theme"])
        self._apply_theme(self.theme_choice.currentData())
        self._select_source(self.mic, conf["mic_target"])
        self._select_data(self.language, conf["language"])
        self.auto_paste.setChecked(conf["auto_paste"])
        self._sync_paste_key()
        self.paste_shortcut.setCurrentText(key_label(conf["paste_shortcut"]))
        self.restore_clipboard.setChecked(conf["restore_clipboard"])
        screen_name = conf["overlay_screen"]
        if screen_name and self.indicator_screen.findData(screen_name) < 0:
            self.indicator_screen.addItem(t("{name} (not connected)", name=screen_name), screen_name)
        self._select_data(self.indicator_screen, screen_name)
        self.follow_pointer.setChecked(conf["overlay_follows_pointer"])
        self._sync_follow_pointer()
        self._select_data(self.corner, conf["overlay_corner"])
        self.max_seconds.setValue(conf["max_seconds"])
        self.skip_silent.setChecked(conf["skip_silent"])
        self.silence_db.setValue(int(conf["silence_db"]))
        self.filter_hallucinations.setChecked(conf["filter_hallucinations"])
        self.audio_cues.setChecked(conf["audio_cues"])
        self.keep_audio.setChecked(conf["keep_audio"])
        self.update_check.setChecked(conf["update_check"])
        self.start_in_tray.setChecked(conf["start_in_tray"])
        self._show_update(update.pending())

        for name, who in cfg.TRANSCRIBERS.items():
            if name == "custom":
                continue
            self._key_fields[name].setText(conf[who.key])
            self._models[name] = conf[who.model]
        self._custom_values = dict(conf.data)
        self._preset_shown.clear()
        for role, rows in self._preset_widgets.items():
            with QSignalBlocker(rows["preset"]):
                self._select_data(rows["preset"], conf.custom_preset(role))
            self._preset_changed(role)
        self._shown_provider = ""
        self._select_data(self.transcribe_provider, conf["transcribe_provider"])
        self._provider_changed()  # selecting index 0 fires no signal
        self.file_model.setCurrentText(conf["openrouter_file_model"]
                                       or self.file_model_default)
        binary_changed = self.local_whisper.custom_binary != conf["local_binary"]
        self.local_whisper.custom_binary = conf["local_binary"]
        processing_device = conf["local_device"]
        if processing_device not in ("auto", "cpu") \
                and self.local_device.findData(processing_device) < 0:
            self.local_device.addItem(
                t("Previously selected graphics card (unavailable)"),
                processing_device,
            )
        self._select_data(self.local_device, processing_device)
        self._processing_device_changed()
        self.local_models_dir.setText(conf["local_models_dir"])
        self.local_preload.setChecked(conf["local_preload"])
        saved_threads = cfg._local_thread_count(conf["local_threads"])
        self.local_threads.setValue(min(saved_threads, self.local_threads.maximum()))
        self._local_threads_changed = False
        # A deliberate manual override must not disappear behind a disclosure.
        self.local_advanced_toggle.setChecked(self.local_threads.value() != 0)
        self.local_whisper.load(conf["local_model"])
        # Initial construction probes after signals are connected. Every later
        # reload must invalidate the old draft's probe as soon as it restores a
        # different executable.
        if binary_changed and hasattr(self, "_saved_form"):
            self._refresh_processing_devices()

        self.cleanup_enabled.setChecked(conf["cleanup_enabled"])
        self.cleanup_model.setCurrentText(conf["cleanup_model"])
        self.cleanup_claude_model.setCurrentText(conf["cleanup_claude_model"])
        self.cleanup_codex_model.setCurrentText(
            conf["cleanup_codex_model"] or t("Codex's own default")
        )
        self.cleanup_agy_model.setCurrentText(
            conf["cleanup_agy_model"] or t("Antigravity's own default")
        )
        self._select_data(self.cleanup_provider, conf["cleanup_provider"])
        self._cleanup_provider_changed()  # selecting index 0 fires no signal
        self._select_data(self.cleanup_reasoning, conf["cleanup_reasoning"])
        self.local_llm_gpu.setChecked(conf["local_llm_gpu"])
        self.local_llm_preload.setChecked(conf["local_llm_preload"])
        self._select_data(self.local_llm_reasoning, conf["local_llm_reasoning"])
        self.local_llm.custom_binary = conf["local_llm_binary"]
        self.local_llm.load(conf["local_llm_model"], conf["local_llm_repo"])
        self.local_idle_unload.setChecked(conf["local_idle_unload"])
        self.local_idle_minutes.setValue(int(conf["local_idle_minutes"]))
        self.local_idle_minutes.setEnabled(conf["local_idle_unload"])
        # The defaults as they read NOW, kept for the save comparison: after a
        # language switch the boxes still hold the old language's default, and
        # comparing against the new one would store that text as a custom
        # prompt shadowing every future improvement.
        # Reflowed, as the editors show them: one line per paragraph.
        self._loaded_defaults = {
            "cleanup": reflow(cfg.default_cleanup_prompt()),
            "file": reflow(cfg.default_file_cleanup_prompt()),
            "assistant": reflow(cfg.default_assistant_prompt()),
            "meeting": reflow(cfg.default_meeting_prompt()),
        }
        self.cleanup_prompt.setPlainText(
            conf["cleanup_prompt"] or self._loaded_defaults["cleanup"])
        self.file_cleanup_prompt.setPlainText(
            conf["file_cleanup_prompt"] or self._loaded_defaults["file"]
        )
        self.dictionary_editor.load(conf)
        self.profiles_editor.load(conf)

        self._select_data(self.assistant_provider, conf["assistant_provider"])
        self.assistant_model.setCurrentText(conf["assistant_model"])
        self._select_data(self.assistant_permission, conf["assistant_permission_mode"])
        self.assistant_codex_model.setCurrentText(conf["assistant_codex_model"])
        self._select_data(self.assistant_codex_sandbox, conf["assistant_codex_sandbox"])
        self.assistant_openrouter_model.setCurrentText(conf["assistant_openrouter_model"])
        self.assistant_agy_model.setCurrentText(conf["assistant_agy_model"])
        self._assistant_provider_changed()  # selecting index 0 fires no signal
        self._select_data(self.assistant_reasoning, conf["assistant_reasoning"])
        self.assistant_dir.setText(conf["assistant_dir"])
        self.assistant_timeout.setValue(int(conf["assistant_timeout"]))
        self.assistant_session_minutes.setValue(int(conf["assistant_session_minutes"]))
        self.assistant_paste.setChecked(conf["assistant_paste"])
        self.assistant_cleanup.setChecked(conf["assistant_cleanup"])
        self.selection_assist.setChecked(conf["selection_assist"])
        self._enable_assist_shortcut(conf["selection_assist"])
        self.assistant_prompt.setPlainText(
            conf["assistant_prompt"] or self._loaded_defaults["assistant"]
        )

        self._select_source(self.meeting_mic, conf["meeting_mic_target"])
        self._select_source(self.meeting_system, conf["meeting_system_target"])
        self.meeting_self_name.setText(conf["meeting_self_name"])
        self.meeting_other_name.setText(conf["meeting_other_name"])
        self.meeting_participants.setPlainText(conf["meeting_participants"])
        self.meeting_model.setCurrentText(conf["meeting_model"])
        self._select_data(self.meeting_reasoning, conf["meeting_reasoning"])
        self._select_data(self.meeting_language, conf["meeting_language"])
        self.meeting_cleanup.setChecked(conf["meeting_cleanup"])
        self._meeting_max_loaded = int(conf["meeting_max_seconds"])
        self.meeting_max_minutes.setValue(max(5, self._meeting_max_loaded // 60))
        self.meeting_keep_audio.setChecked(conf["meeting_keep_audio"])
        self.meeting_prompt.setPlainText(
            conf["meeting_prompt"] or self._loaded_defaults["meeting"]
        )

        with QSignalBlocker(self.file_timestamps), QSignalBlocker(self.file_cleanup), \
                QSignalBlocker(self.file_speakers), QSignalBlocker(self.file_wrap):
            self.file_timestamps.setChecked(conf["file_timestamps"])
            self.file_cleanup.setChecked(conf["file_cleanup"])
            self.file_speakers.setChecked(conf["file_speakers"])
            self.file_wrap.setChecked(conf["file_wrap_lines"])
        # The run reads the saved settings, so the saved model is the one asked.
        self.file_speakers.setVisible(bool(api.diarize_model(conf.transcribe_target())))
        # Greyed out rather than hidden, since every other provider can do it,
        # and shown unticked without the setting being touched, so that going
        # back to one of those brings the choice back with it.
        target = conf.transcribe_target()
        timed = api.gives_times(target.provider, target.preset)
        self.file_timestamps.setEnabled(timed)
        self.file_timestamps.setToolTip(self._timestamps_tip if timed else t(
            "{service} returns no timestamps.",
            service=conf.transcribe_target().service))
        if not timed:
            with QSignalBlocker(self.file_timestamps):
                self.file_timestamps.setChecked(False)
        if not hasattr(self, "file_path"):
            self.file_path = ""

        for which, (box, _status, _missing) in self._shortcut_rows.items():
            box.setCurrentText(conf[hotkey.SHORTCUTS[which].setting])
        self.evdev_enabled.setChecked(conf["evdev_hotkey"])

        self.history_limit.setValue(max(0, int(conf["history_limit"])))

        for which in self._shortcut_rows:
            self._refresh_shortcut_status(which)
        self._refresh_assistant_status()
        self._load_history()
        self._load_minutes()
        self._loaded_config = dict(conf.data)

    def _save(self, quiet=False):
        conf = self.conf
        before = dict(conf.data)
        conf["ui_language"] = self.ui_language.currentData() or "auto"
        conf["theme"] = self.theme_choice.currentData() or theme.DEFAULT
        conf["mic_target"] = self.mic.currentData() or ""
        conf["language"] = self.language.currentData() or "auto"
        conf["auto_paste"] = self.auto_paste.isChecked()
        # Shown as "Ctrl+V", stored the way the paste code and the config
        # always had it; the key names are read without regard to case.
        typed = self.paste_shortcut.currentText().strip()
        conf["paste_shortcut"] = (conf["paste_shortcut"]
                                  if key_label(conf["paste_shortcut"]) == typed
                                  else typed.lower())
        conf["restore_clipboard"] = self.restore_clipboard.isChecked()
        conf["overlay_screen"] = self.indicator_screen.currentData() or ""
        # Read even while it is greyed out, so that naming a screen and taking
        # the name back again does not clear a preference nobody touched.
        conf["overlay_follows_pointer"] = self.follow_pointer.isChecked()
        conf["overlay_corner"] = self.corner.currentData() or "bottom-left"
        conf["max_seconds"] = self.max_seconds.value()
        conf["skip_silent"] = self.skip_silent.isChecked()
        conf["silence_db"] = float(self.silence_db.value())
        conf["filter_hallucinations"] = self.filter_hallucinations.isChecked()
        conf["audio_cues"] = self.audio_cues.isChecked()
        conf["keep_audio"] = self.keep_audio.isChecked()
        conf["update_check"] = self.update_check.isChecked()
        conf["start_in_tray"] = self.start_in_tray.isChecked()

        provider = self.transcribe_provider.currentData() or "local"
        if provider in self._models:
            self._models[provider] = self.transcribe_model.currentText().strip()
        conf["transcribe_provider"] = provider
        for name, who in cfg.TRANSCRIBERS.items():
            if name == "custom":
                continue
            conf[who.key] = self._key_fields[name].text().strip()
            conf[who.model] = self._models[name].strip() or cfg.DEFAULTS[who.model]
        file_model = self.file_model.currentText().strip()
        conf["openrouter_file_model"] = ("" if file_model == self.file_model_default
                                         else file_model)
        for role in self._preset_widgets:
            self._stash_preset(role)
        model_settings = self._preset_model_settings()
        for key, value in self._custom_values.items():
            if key.endswith("_api_key") or key == "custom_base_url" or key in model_settings:
                if key not in ("openai_api_key", "openrouter_api_key"):
                    conf[key] = value
        for role, rows in self._preset_widgets.items():
            conf[f"{role}_custom_preset"] = rows["preset"].currentData()
        conf["local_binary"] = self.local_whisper.custom_binary
        conf["local_llm_binary"] = self.local_llm.custom_binary
        conf["local_models_dir"] = self.local_models_dir.text().strip()
        conf["local_model"] = self.local_whisper.selected()
        conf["local_device"] = self.local_device.currentData() or "auto"
        conf["local_gpu"] = conf["local_device"] != "cpu"
        conf["local_preload"] = self.local_preload.isChecked()
        if self._local_threads_changed:
            conf["local_threads"] = self.local_threads.value()

        conf["cleanup_enabled"] = self.cleanup_enabled.isChecked()
        conf["cleanup_provider"] = self.cleanup_provider.currentData() or "openrouter"
        conf["cleanup_model"] = self.cleanup_model.currentText().strip()
        conf["cleanup_claude_model"] = (self.cleanup_claude_model.currentText().strip()
                                        or cfg.DEFAULTS["cleanup_claude_model"])
        codex_cleanup_model = self.cleanup_codex_model.currentText().strip()
        conf["cleanup_codex_model"] = (
            "" if codex_cleanup_model == t("Codex's own default") else codex_cleanup_model
        )
        agy_cleanup_model = self.cleanup_agy_model.currentText().strip()
        conf["cleanup_agy_model"] = (
            "" if agy_cleanup_model == t("Antigravity's own default")
            else agy_cleanup_model
        )
        conf["cleanup_reasoning"] = self.cleanup_reasoning.currentData() or ""
        conf["local_llm_model"] = self.local_llm.selected()
        conf["local_llm_repo"] = self.local_llm.repository()
        conf["local_llm_gpu"] = self.local_llm_gpu.isChecked()
        conf["local_llm_preload"] = self.local_llm_preload.isChecked()
        conf["local_llm_reasoning"] = self.local_llm_reasoning.currentData() or ""
        conf["local_idle_unload"] = self.local_idle_unload.isChecked()
        conf["local_idle_minutes"] = self.local_idle_minutes.value()

        # Store an empty prompt when it matches a default: the one it was
        # loaded with, or today's (a Reset click in a session that switched
        # languages fills in the latter). Both count, so switching the
        # interface language keeps switching the prompt language.
        prompt = self.cleanup_prompt.toPlainText().strip()
        conf["cleanup_prompt"] = ("" if prompt in (
            self._loaded_defaults["cleanup"], cfg.default_cleanup_prompt(),
            reflow(cfg.default_cleanup_prompt()))
            else prompt)
        file_prompt = self.file_cleanup_prompt.toPlainText().strip()
        conf["file_cleanup_prompt"] = ("" if file_prompt in (
            self._loaded_defaults["file"], cfg.default_file_cleanup_prompt(),
            reflow(cfg.default_file_cleanup_prompt()))
            else file_prompt)
        dictionary_write = self.dictionary_editor.store(conf)
        self.profiles_editor.store(conf)

        conf["assistant_provider"] = self.assistant_provider.currentData() or "claude"
        conf["assistant_model"] = (self.assistant_model.currentText().strip()
                                   or cfg.DEFAULTS["assistant_model"])
        conf["assistant_permission_mode"] = (self.assistant_permission.currentData()
                                             or "auto")
        # The editable box shows a label for "no choice", which must not be
        # stored as if it were a model id.
        codex_model = self.assistant_codex_model.currentText().strip()
        conf["assistant_codex_model"] = (
            "" if codex_model == t("Codex's own default") else codex_model
        )
        conf["assistant_codex_sandbox"] = (self.assistant_codex_sandbox.currentData()
                                           or "workspace-write")
        conf["assistant_openrouter_model"] = (
            self.assistant_openrouter_model.currentText().strip()
            or cfg.DEFAULTS["assistant_openrouter_model"]
        )
        agy_model = self.assistant_agy_model.currentText().strip()
        conf["assistant_agy_model"] = (
            "" if agy_model == t("Antigravity's own default") else agy_model
        )
        conf["assistant_reasoning"] = self.assistant_reasoning.currentData() or ""
        conf["assistant_dir"] = self.assistant_dir.text().strip()
        conf["assistant_timeout"] = self.assistant_timeout.value()
        conf["assistant_session_minutes"] = self.assistant_session_minutes.value()
        conf["assistant_paste"] = self.assistant_paste.isChecked()
        conf["assistant_cleanup"] = self.assistant_cleanup.isChecked()
        conf["selection_assist"] = self.selection_assist.isChecked()
        assistant_prompt = self.assistant_prompt.toPlainText().strip()
        conf["assistant_prompt"] = ("" if assistant_prompt in (
            self._loaded_defaults["assistant"], cfg.default_assistant_prompt(),
            reflow(cfg.default_assistant_prompt()))
            else assistant_prompt)

        conf["meeting_mic_target"] = self.meeting_mic.currentData() or ""
        conf["meeting_system_target"] = self.meeting_system.currentData() or ""
        conf["meeting_self_name"] = self.meeting_self_name.text().strip()
        conf["meeting_other_name"] = self.meeting_other_name.text().strip()
        conf["meeting_participants"] = self.meeting_participants.toPlainText().strip()
        conf["meeting_model"] = (self.meeting_model.currentText().strip()
                                 or cfg.DEFAULTS["meeting_model"])
        conf["meeting_reasoning"] = self.meeting_reasoning.currentData() or ""
        conf["meeting_language"] = self.meeting_language.currentData() or ""
        conf["meeting_cleanup"] = self.meeting_cleanup.isChecked()
        # Only when the dial was actually turned: the box speaks whole minutes
        # with a floor, and an unrelated Save must not rewrite a value the
        # command line set in seconds.
        if self.meeting_max_minutes.value() != max(5, self._meeting_max_loaded // 60):
            conf["meeting_max_seconds"] = self.meeting_max_minutes.value() * 60
        conf["meeting_keep_audio"] = self.meeting_keep_audio.isChecked()
        meeting_prompt = self.meeting_prompt.toPlainText().strip()
        conf["meeting_prompt"] = ("" if meeting_prompt in (
            self._loaded_defaults["meeting"], cfg.default_meeting_prompt(),
            reflow(cfg.default_meeting_prompt()))
            else meeting_prompt)

        if self.file_timestamps.isEnabled():
            conf["file_timestamps"] = self.file_timestamps.isChecked()
        conf["file_cleanup"] = self.file_cleanup.isChecked()
        conf["file_speakers"] = self.file_speakers.isChecked()
        conf["file_wrap_lines"] = self.file_wrap.isChecked()

        # Left empty, only the toggle falls back to a default: the application
        # is unusable without it. The rest stay empty, which is what turns
        # them off.
        for which, (box, _status, _missing) in self._shortcut_rows.items():
            spec = hotkey.SHORTCUTS[which]
            conf[spec.setting] = (box.currentText().strip()
                                  or hotkey.default_combo(which))
        conf["evdev_hotkey"] = self.evdev_enabled.isChecked()
        conf["history_limit"] = self.history_limit.value()
        # A retained form may predate a CLI reload. Only its edits take priority;
        # unchanged fields keep the current runtime value.
        device_edited = (
            conf["local_device"] != self._loaded_config.get("local_device")
        )
        for key, value in before.items():
            if device_edited and key in ("local_device", "local_gpu"):
                continue
            if conf.data.get(key) == self._loaded_config.get(key):
                conf.data[key] = value
        try:
            conf.save()
        except OSError as exc:
            # An antivirus or a sync tool holding the file for a beat is a
            # message, not an exit: an exception out of a Qt slot takes the
            # whole application down.
            conf.data = before
            QMessageBox.warning(self, "Dikte",
                                t("Could not save the settings: {error}",
                                  error=exc))
            return
        # After config.json, so that a failed save above leaves the file still
        # agreeing with the setting (see dictionary.save).
        try:
            self.dictionary_editor.commit(dictionary_write)
        except OSError as exc:
            QMessageBox.warning(self, "Dikte",
                                t("Could not save the dictionary: {error}",
                                  error=exc))
        # A lowered limit should bite now, not on the next dictation.
        try:
            cfg.trim_history(conf["history_limit"])
        except OSError as exc:
            print(f"dikte: could not trim the history ({exc})")
        self._load()
        self._saved_form = self._form_values()
        self._show_dirty()
        self.applied.emit()
        # conf.save() has switched the language t() speaks, so the message box
        # already answers in the new one; the labels around it were translated
        # when the window was built and stay behind. Asking for the rebuild
        # waits until the box is dismissed, so the window is not pulled out
        # from under a dialog it is holding up.
        if not quiet:
            QMessageBox.information(self, t("Dikte Settings"), t("Saved successfully."))
        if i18n.language() != self._built_language and not self._work_in_flight():
            self.language_changed.emit()

    def _work_in_flight(self):
        """A daemon thread of this window's is still running.

        Replacing the window now would let it be collected, taking the C++
        side of the model boxes down with it, and the thread's next progress
        report would land on a deleted object. The stale labels stand until a
        later save finds the window quiet; _built_language keeps the old
        language, so that save asks for the rebuild by itself.
        """
        return (self.transcriber.busy
                or self.local_whisper._downloading
                or self.local_llm._downloading)

    @staticmethod
    def _select_source(combo, value):
        if value and combo.findData(value) < 0:
            combo.addItem(t("{name} (not connected)", name=value), value)
        SettingsWindow._select_data(combo, value)

    @staticmethod
    def _select_data(combo, value):
        index = combo.findData(value)
        fallback = combo.findData("other")
        combo.setCurrentIndex(index if index >= 0 else max(fallback, 0))

    # ---- api helpers -----------------------------------------------------

    def _toggle_local_advanced(self, expanded):
        self.local_advanced.set_expanded(expanded)

    def _local_threads_was_changed(self):
        self._local_threads_changed = True

    def _refresh_processing_devices(self):
        custom = self.local_whisper.custom_binary
        self._processing_devices_request += 1
        request = self._processing_devices_request
        # A previous binary's verified mapping is not evidence for the new
        # draft (or replacement download). Keep its UUID, not its verification.
        self._set_processing_devices(self._graphics_devices, ())

        def work():
            devices = hardware.graphics_devices()
            binary = ggml.program_path(ggml.WHISPER, custom)
            managed = ggml.managed_vulkan_devices(binary, devices)
            self._processing_devices_loaded.emit(devices, managed, request)

        threading.Thread(target=work, daemon=True).start()

    def _set_processing_devices(self, devices, managed, request=None):
        if request is not None and request != self._processing_devices_request:
            return
        current = self.local_device.currentData() or "auto"
        self._graphics_devices = tuple(devices)
        self._selectable_graphics_devices = tuple(managed)
        self.local_device.blockSignals(True)
        self.local_device.clear()
        self.local_device.addItem(t("Automatic (whisper.cpp default)"), "auto")
        self.local_device.addItem(t("Processor (CPU)"), "cpu")
        for device in managed:
            if device.identifier:
                self.local_device.addItem(
                    device.name, device.identifier
                )
        if current not in ("auto", "cpu") \
                and self.local_device.findData(current) < 0:
            self.local_device.addItem(
                t("Previously selected graphics card (unavailable)"), current
            )
        self._select_data(self.local_device, current)
        self.local_device.blockSignals(False)
        LocalModelBox._fit_popup(self.local_device)
        self._processing_device_changed()

    def _processing_device_changed(self):
        self.local_whisper._show_machine(
            self.local_device.currentData() or "auto",
            self._graphics_devices,
            self._selectable_graphics_devices,
        )
        binary = ggml.program_path(ggml.WHISPER, self.local_whisper.custom_binary)
        # A downloaded build with no graphics backend ignores every choice but
        # the processor, and a box that can be set to a card it will never
        # reach is a setting that silently does nothing. The saved choice is
        # left as it is, so a later download that can reach the card picks it
        # up again. A specific card saved from an earlier build is the
        # exception: that one fails to start here, so the box stays usable
        # until it is moved off it.
        processor_only = ggml.processor_only(ggml.WHISPER, binary)
        self.local_device.setEnabled(
            not processor_only
            or (self.local_device.currentData() or "auto") not in ("auto", "cpu"))
        if processor_only:
            # After a Vulkan download fell back, the Program line already says
            # this build runs on the processor; once is enough.
            if ggml.vulkan_missing(ggml.WHISPER):
                return
            label = self.local_whisper.machine_label
            label.setText(label.text() + "\n" + t(
                "This whisper.cpp build has no graphics card support, so it "
                "always runs on the processor."))
            return
        selectable = {device.identifier for device in self._selectable_graphics_devices
                      if device.identifier}
        if self._graphics_devices and any(
                not device.identifier or device.identifier not in selectable
                for device in self._graphics_devices):
            if "GGML_VK_VISIBLE_DEVICES" in os.environ:
                reason = t("GGML_VK_VISIBLE_DEVICES overrides graphics visibility.")
            elif not ggml.managed_vulkan_in_use(binary):
                reason = t("The selected custom or unmanaged program does not provide "
                           "verified explicit graphics selection.")
            else:
                reason = t("The program's graphics device mapping could not be verified.")
            hint = t("Detected graphics: {devices}. {reason} Automatic may still use "
                     "a graphics card if the program supports it.",
                     devices="; ".join(_graphics_device_label(device)
                                       for device in self._graphics_devices),
                     reason=reason)
            label = self.local_whisper.machine_label
            label.setText(label.text() + "\n" + hint)

    def _provider_changed(self):
        """Swap the model box over to the newly chosen provider's own model."""
        if self._shown_provider == "custom":
            self._stash_preset("transcribe")
        if self._shown_provider in TRANSCRIBE_MODELS:
            self._models[self._shown_provider] = self.transcribe_model.currentText().strip()
        provider = self.transcribe_provider.currentData() or "local"
        self._shown_provider = provider
        local = provider == "local"
        self.refresh_transcribe_models.setVisible(True)
        self.stt_form.setRowVisible(self.transcribe_custom_box, provider == "custom")
        self.stt_form.setRowVisible(self.transcribe_model_row, not local)
        self.stt_form.setRowVisible(self.file_model_row, provider == "openrouter")
        self.stt_form.setRowVisible(self.transcribe_status, False)
        self.stt_form.setRowVisible(self.local_whisper, local)
        self.stt_form.setRowVisible(self.local_options, local)
        self._sync_local_rows()
        self._refresh_local_box()
        self._place_keys()
        if local:
            return
        if provider == "custom":
            self._show_preset_model("transcribe")
            self._say_transcribe("")
            return
        self.transcribe_model.clear()
        self.transcribe_model.addItems(TRANSCRIBE_MODELS[provider])
        self.transcribe_model.setCurrentText(self._models[provider])
        self.transcribe_model.lineEdit().setPlaceholderText("")
        self.transcribe_model.setToolTip("")
        if provider == "openrouter":
            self._fill_file_models(TRANSCRIBE_MODELS[provider])
        self._say_transcribe("")

    def _say_transcribe(self, line):
        """`line` on the status row, under the warning the provider carries.

        Said where the provider is picked, rather than after a meeting has been
        recorded for an hour with nothing able to write it up, and kept there
        through a fetch of the model list rather than replaced by its count.
        """
        warning = ("" if api.gives_times(self._shown_provider,
                     self._preset_widgets["transcribe"]["preset"].currentData()
                     if self._shown_provider == "custom" else "")
                   else t("Gives no timestamps, so subtitles and meetings need "
                          "another provider."))
        self.transcribe_status.set(line, t("No timestamps from this provider.") if warning else "",
                                   warning)
        self.stt_form.setRowVisible(self.transcribe_status,
                                    bool(self.transcribe_status.text())
                                    and self._shown_provider != "local")

    def _fill_file_models(self, models):
        current = self.file_model.currentText()
        self.file_model.clear()
        self.file_model.addItem(self.file_model_default)
        self.file_model.addItems(models)
        self.file_model.setCurrentText(current)

    def _load_transcribe_models(self):
        """The model list of whichever provider is selected."""
        provider = self.transcribe_provider.currentData() or "openai"
        if provider == "custom":
            self._load_preset_models("transcribe")
            return
        self.refresh_transcribe_models.setEnabled(False)
        self._fetching_provider = provider
        self._say_transcribe(t("Fetching model list…"))
        key, base = self._typed_key(provider)
        service = t(cfg.TRANSCRIBERS[provider].service)

        def work():
            try:
                if provider == "openrouter":
                    models = api.openrouter_models(key, transcription=True)
                else:
                    models = api.openai_models(key, base, service)
                self._transcribe_models_loaded.emit(models, "")
            except api.ApiError as exc:
                self._transcribe_models_loaded.emit([], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_transcribe_models_loaded(self, models, error):
        self.refresh_transcribe_models.setEnabled(True)
        # Another provider was picked while the list was on its way, and these
        # are not its models.
        if getattr(self, "_fetching_provider", self._shown_provider) != self._shown_provider:
            return
        if error:
            self._say_transcribe(t("Could not fetch the list: {error}", error=error))
            return
        current = self.transcribe_model.currentText()
        self.transcribe_model.clear()
        self.transcribe_model.addItems(models)
        self.transcribe_model.setCurrentText(current)
        if self._shown_provider == "openrouter":
            self._fill_file_models(models)
        self._say_transcribe(t("{count} models loaded.", count=len(models)))

    def _load_models(self):
        self.refresh_models.setEnabled(False)
        self.models_label.setText(t("Fetching model list…"))
        key = self.openrouter_key.text().strip() or self.conf.openrouter_key()

        def work():
            try:
                self._models_loaded.emit(api.openrouter_models(key), "")
            except api.ApiError as exc:
                self._models_loaded.emit([], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_models_loaded(self, models, error):
        self.refresh_models.setEnabled(True)
        if error:
            self.models_label.setText(t("Could not fetch the list: {error}", error=error))
            return
        for combo in (self.cleanup_model, self.meeting_model):
            current = combo.currentText()
            combo.clear()
            combo.addItems(models)
            combo.setCurrentText(current)
        self.models_label.setText(t("{count} models loaded.", count=len(models)))

    def _load_codex_models(self):
        """Ask Codex which models it offers, off the interface thread.

        No button and no network of ours: the CLI answers from its own cache in
        well under a second. Skipped when Codex is not installed, which is also
        when the built-in list stays on screen and nobody is running Codex
        anyway.
        """
        if not shutil.which("codex"):
            return

        def work():
            found = assistant.codex_models()
            if found:
                self._codex_models_loaded.emit(found)

        threading.Thread(target=work, daemon=True).start()

    def _on_codex_models_loaded(self, models):
        for combo in (self.cleanup_codex_model, self.assistant_codex_model):
            current = combo.currentText()
            combo.clear()
            combo.addItem(t("Codex's own default"), "")
            for name in models:
                combo.addItem(name, name)
            combo.setCurrentText(current)

    def _load_agy_models(self):
        """Ask Antigravity which models it offers, off the interface thread.

        The same arrangement as Codex, except agy answers over the network
        rather than from a cache, so the couple of seconds it takes are spent
        where nobody is waiting. Skipped when agy is not installed, which is
        also when the built-in list stays on screen and nobody is running
        Antigravity anyway.
        """
        if not shutil.which("agy"):
            return

        def work():
            found = assistant.agy_models()
            if found:
                self._agy_models_loaded.emit(found)

        threading.Thread(target=work, daemon=True).start()

    def _on_agy_models_loaded(self, models):
        for combo in (self.cleanup_agy_model, self.assistant_agy_model):
            current = combo.currentText()
            combo.clear()
            combo.addItem(t("Antigravity's own default"), "")
            for name in models:
                combo.addItem(name, name)
            combo.setCurrentText(current)

    def _load_hosted_models(self):
        """Fetch the hosted model lists at open, without being asked.

        Only selected gateway presets are fetched. Errors leave the current
        suggestions unchanged; Fetch buttons allow an explicit retry.
        """
        jobs = []
        openrouter_key = self.conf.openrouter_key()
        if openrouter_key:
            jobs.append(("openrouter",
                         lambda: api.openrouter_models(openrouter_key)))
        selected = {self.conf.custom_preset(role)
                    for role in ("cleanup", "assistant")
                    if self.conf[f"{role}_provider"] == "custom"}
        for name, who in cfg.PRESETS.items():
            key = self.conf.api_key(who.key)
            if name != "other" and name in selected and key:
                base = self.conf.get(f"{name}_base_url", who.base_url)
                jobs.append((name, lambda name=name, key=key, base=base:
                             api.preset_models(name, key, base)))
        for provider, fetch in jobs:
            def work(provider=provider, fetch=fetch):
                try:
                    found = fetch()
                except api.ApiError:
                    return
                if found:
                    self._hosted_models_loaded.emit(provider, found)

            threading.Thread(target=work, daemon=True).start()

    def _on_hosted_models_loaded(self, provider, models):
        if provider != "openrouter":
            for role, rows in self._preset_widgets.items():
                if role != "transcribe" and rows["preset"].currentData() == provider:
                    self._on_preset_models_loaded(role, provider, models, "")
            return
        combos = (self.cleanup_model, self.meeting_model)
        for combo in combos:
            current = combo.currentText()
            combo.clear()
            combo.addItems(models)
            combo.setCurrentText(current)

    def _test_openai(self):
        key, base = self._typed_key("openai")
        self._test_key("openai", lambda: t(
            "Connection works. {count} audio models visible.",
            count=len(api.openai_models(key, base)),
        ))

    def _test_openrouter(self):
        key, _ = self._typed_key("openrouter")
        self._test_key("openrouter", lambda: api.openrouter_key_status(key))

    def _typed_key(self, provider):
        """(key, base URL) for a provider, preferring what is in the field now."""
        who = cfg.TRANSCRIBERS[provider]
        key_setting, url_setting = who.key, who.url
        typed = self._key_fields[provider].text().strip()
        base = self.conf[url_setting]
        return typed or self.conf.api_key(key_setting), base

    def _test_key(self, provider, ask):
        """Run `ask` off the interface thread and write its answer under the key.

        `ask` returns the line to show, or raises ApiError with the line to show
        instead; either way it is read from a field before the thread starts.
        """
        button, answer = self._testers[provider]
        button.setEnabled(False)
        answer.setText(t("Trying…"))
        answer.show()

        def work():
            try:
                self._test_done.emit(provider, True, ask())
            except api.ApiError as exc:
                self._test_done.emit(provider, False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, provider, ok, message):
        button, answer = self._testers[provider]
        button.setEnabled(True)
        if provider.startswith("preset:"):
            role = provider.split(":", 1)[1]
            asked = getattr(self, "_preset_tested", {}).get(role)
            if asked != self._preset_widgets[role]["preset"].currentData():
                return  # another preset was picked while this one was tried
        answer.setText(("✓ " if ok else "✗ ") + message)
        answer.setObjectName("muted" if ok else "danger")
        answer.style().unpolish(answer)
        answer.style().polish(answer)
        answer.show()

    # ---- updates ---------------------------------------------------------

    def _check_for_update(self):
        """The button, which asks GitHub whatever the daily clock says."""
        self.update_now.setEnabled(False)
        self.update_status.setText(t("Looking…"))

        def work():
            try:
                self._update_checked.emit(update.check(force=True), "")
            except hub.HubError as exc:
                self._update_checked.emit(None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_update_checked(self, release, error):
        self.update_now.setEnabled(True)
        if error:
            self.update_status.setText(error)
            return
        self._show_update(release, asked=True)
        if release is not None:
            self.update_found.emit(release)

    def _show_update(self, release, asked=False):
        """What the line under the checkbox says, and whether the page button
        is on it. `release` is None when this build is the newest one, and
        `asked` is what tells "nothing new" from "nobody has looked yet"."""
        self.update_page.setVisible(release is not None)
        if release is None:
            self.update_status.setText(
                t("Dikte {version} is the newest release.", version=__version__)
                if asked else t("This is Dikte {version}.", version=__version__))
            return
        self._release_url = release.url
        self.update_status.setText(
            t("Dikte {version} is out; this is {current}.",
              version=release.version, current=__version__))

    # ---- audio file ------------------------------------------------------

    def _file_say(self, text):
        """The file tab's status line, taking no room while it is empty."""
        self.file_status.setText(text)
        self.file_status.setVisible(bool(text))

    def _choose_file(self):
        start = self.conf["file_last_dir"] or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, t("Select an audio file"), start,
            f"{t('Audio and video files')} ({AUDIO_FILTER});;{t('All files')} (*)",
        )
        if path:
            self._use_file(path)

    def _use_file(self, path):
        """The file to transcribe next, picked or dropped."""
        if not path or self.transcriber.busy:
            return
        self.file_path = path
        self.file_label.setText(os.path.basename(path))
        self.conf["file_last_dir"] = os.path.dirname(path)
        self._remember_file_choices()

    def _remember_file_choices(self):
        """Keep this tab's choices without waiting for the Save button.

        The two switches and the folder belong to the run rather than to the
        form: what was ticked before Transcribe is what the next file wants
        too, and Save is at the far end of a window opened to transcribe one
        file. Everything else on the tab is a button, so there is nothing here
        an unsaved form could be caught by.
        """
        # A greyed-out box is unticked for this provider only.
        if self.file_timestamps.isEnabled():
            self.conf["file_timestamps"] = self.file_timestamps.isChecked()
        self.conf["file_cleanup"] = self.file_cleanup.isChecked()
        self.conf["file_speakers"] = self.file_speakers.isChecked()
        self.conf["file_wrap_lines"] = self.file_wrap.isChecked()
        try:
            self.conf.save()
        except OSError as exc:
            print(f"dikte: could not save the settings: {exc}", file=sys.stderr)

    def _run_file(self):
        if not getattr(self, "file_path", "") or self.transcriber.busy:
            return
        self.file_output.clear()
        self.file_segments = []
        self._set_srt(False)
        self.file_run.setEnabled(False)
        self.file_run.hide()
        self.file_stop.setEnabled(True)
        self.file_stop.show()
        self.transcriber.start(
            self.file_path,
            self.file_timestamps.isChecked(),
            self.file_cleanup.isChecked(),
            self.file_speakers.isChecked(),
        )

    def _stop_file(self):
        # The button goes dead here rather than when the run comes back, so a
        # second press cannot land while the first one is still travelling.
        self.file_stop.setEnabled(False)
        self._file_say(t("Stopping…"))
        self.transcriber.stop()

    def _on_file_progress(self, message):
        self._file_say(message)
        if message == t("Stopped."):
            self._file_idle()

    def _on_file_finished(self, text, segments):
        self.file_output.setPlainText(text)
        self.file_segments = segments
        self._set_srt(bool(segments))
        self._file_say(t("Done: {chars} characters.", chars=len(text)))
        self._file_idle()

    def _on_file_failed(self, error):
        self._file_say(t("Failed: {error}", error=error))
        self._file_idle()

    def _file_idle(self):
        self.file_run.setEnabled(True)
        self.file_run.show()
        self.file_stop.setEnabled(False)
        self.file_stop.hide()

    def _save_transcript(self):
        self._write_transcript(self.file_output.toPlainText(), ".txt",
                               f"{t('Text files')} (*.txt)")

    def _save_subtitles(self):
        srt = filetranscribe.to_srt(self.file_output.toPlainText(),
                                    getattr(self, "file_segments", []),
                                    self.file_wrap.isChecked())
        if not srt:
            self._file_say(t("No timestamped lines to turn into subtitles."))
            return
        self._write_transcript(srt, ".srt", f"{t('Subtitle files')} (*.srt)")

    def _write_transcript(self, text, suffix, file_filter):
        if not text:
            return
        base = os.path.splitext(os.path.basename(getattr(self, "file_path", "")))[0]
        start = os.path.join(self.conf["file_last_dir"] or os.path.expanduser("~"),
                             f"{base or 'transcript'}{suffix}")
        path, _ = QFileDialog.getSaveFileName(
            self, t("Save transcript"), start, file_filter
        )
        if not path:
            return
        if not path.lower().endswith(suffix):
            path += suffix
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            self._file_say(t("Saved: {path}", path=path))
        except OSError as exc:
            self._file_say(t("Failed: {error}", error=exc))

    # ---- shortcuts -------------------------------------------------------

    def _install_shortcut(self, which):
        spec = hotkey.SHORTCUTS[which]
        box, _status, _missing = self._shortcut_rows[which]
        combo = box.currentText().strip() or hotkey.default_combo(which)
        if not combo:
            QMessageBox.information(self, t("Shortcut"),
                                    t("Type a key combination first."))
            return
        clashes = hotkey.conflicting_shortcuts(combo, spec.desktop_id)
        if clashes:
            answer = QMessageBox.question(
                self, t("Shortcut conflict"),
                t("{shortcut} is also used by:\n\n{list}\n\nInstall anyway?",
                  shortcut=combo, list="\n".join(clashes[:6])),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        ok, message = hotkey.install_shortcut(
            combo, ipc.command_for(spec.verb), name=spec.name,
            desktop_id=spec.desktop_id,
        )
        QMessageBox.information(self, t("Shortcut"), message)
        if ok:
            self.conf[spec.setting] = combo
            try:
                self.conf.save()
            except OSError as exc:
                QMessageBox.warning(self, "Dikte",
                                    t("Could not save the settings: {error}",
                                      error=exc))
        self._refresh_shortcut_status(which)

    def _remove_shortcut(self, which):
        hotkey.remove_shortcut(hotkey.SHORTCUTS[which].desktop_id)
        self._refresh_shortcut_status(which)

    def _refresh_shortcut_status(self, which):
        box, status, missing = self._shortcut_rows[which]
        typed = box.currentText().strip()
        summary = getattr(self, "_shortcut_summaries", {}).get(which)
        if summary is not None:
            shown = typed or (hotkey.default_combo(which) if which == "toggle" else "")
            summary.setText(shown or t("Not assigned"))
            summary.setObjectName("" if shown else "muted")
            summary.style().unpolish(summary)
            summary.style().polish(summary)
        current = hotkey.shortcut_status(hotkey.SHORTCUTS[which].desktop_id)
        status.setToolTip("")
        if which == "assist" and hasattr(self, "selection_assist") \
                and not self.selection_assist.isChecked():
            # The link under the row says how to turn it on; a line repeating
            # it would be the same thing twice.
            status.setText("")
        elif current and hotkey.installs_shortcuts():
            status.setText(t("Registered in {desktop}: {shortcut}",
                             desktop=hotkey.desktop_name(), shortcut=current))
        elif current:
            # Nothing was written anywhere: this is the combination the running
            # process is holding, which is the only sense in which it exists.
            status.setText(t("Held by Dikte while it runs: {shortcut}",
                             shortcut=current))
        elif not typed and which != "toggle":
            # The empty box already says "Not assigned"; a line under it
            # saying it again is noise.
            status.setText("")
        elif not hotkey.installs_shortcuts():
            # Where there is no registry, a combination in the box is the one
            # Dikte takes as it starts. The page says that once, under "How
            # shortcuts work here"; a line under every key repeating it is noise.
            status.setText("")
        else:
            status.setText(t("Not registered with the desktop."))
            status.setToolTip(self._shortcut_tips.get(which, ""))
        form = self._shortcut_forms.get(which)
        if form is not None:
            form.setRowVisible(status, bool(status.text()))
        else:
            status.setVisible(bool(status.text()))

    def _cleanup_provider_changed(self):
        provider = self.cleanup_provider.currentData() or "openrouter"
        self.cleanup_form.setRowVisible(self.cleanup_custom_box, provider == "custom")
        self.cleanup_form.setRowVisible(self.cleanup_model_row,
                                        provider == "openrouter")
        self.cleanup_form.setRowVisible(self.cleanup_claude_model,
                                        provider == "claude")
        self.cleanup_form.setRowVisible(self.cleanup_codex_model,
                                        provider == "codex")
        self.cleanup_form.setRowVisible(self.cleanup_agy_model,
                                        provider == "agy")
        self.cleanup_advanced_form.setRowVisible(self.cleanup_reasoning,
                                                 provider != "local")
        self.cleanup_advanced_form.setRowVisible(self.local_llm_reasoning,
                                                 provider == "local")
        self.cleanup_form.setRowVisible(self.local_llm, provider == "local")
        self.cleanup_form.setRowVisible(self.local_llm_options, provider == "local")
        self._sync_local_rows()
        self._refresh_local_box()
        self._place_keys()
        binary = cleanup.executable(provider)
        found = shutil.which(binary) if binary else ""
        # The provider box already says where cleanup runs; the line under it
        # only speaks for a program that has to be found on this machine.
        if not binary:
            self.models_label.set()
        elif found:
            self.models_label.set(t("Found: {path}", path=found))
        else:
            self.models_label.warn(
                t("{binary} was not found on your PATH.", binary=binary), t(
                    "{binary} is not on your PATH, so cleanup would fail and the raw "
                    "transcript would be pasted. Install it, or pick another one "
                    "above.", binary=binary))
        self.cleanup_form.setRowVisible(self.models_label, bool(self.models_label.text()))

    def _assistant_provider_changed(self):
        provider = self.assistant_provider.currentData() or "claude"
        # A chat provider runs nothing, anywhere: a working directory for it
        # would be a setting that silently does nothing.
        self.assistant_how_form.setRowVisible(
            self.assistant_dir_row, provider in ("claude", "codex", "agy"))
        self.claude_box.setVisible(provider == "claude")
        self.codex_box.setVisible(provider == "codex")
        self.openrouter_box.setVisible(provider == "openrouter")
        self.agy_box.setVisible(provider == "agy")
        self.assistant_custom_box.setVisible(provider == "custom")
        self._refresh_assistant_status()

    def _refresh_assistant_status(self):
        provider = self.assistant_provider.currentData() or "claude"
        binary = assistant.executable(provider)
        found = shutil.which(binary) if binary else ""
        # A chat provider needs nothing on this machine; that is the
        # provider's tooltip, not a line of its own under it.
        needs = ""
        if not binary:
            needs = t("Chat provider; no local command execution")
        self.assistant_provider.setToolTip(needs)
        if not binary:
            self.assistant_found.set()
        elif found:
            self.assistant_found.setText(t("Found: {path}", path=found))
        else:
            self.assistant_found.warn(
                t("{binary} was not found on your PATH.", binary=binary), t(
                    "{binary} is not on your PATH, so this cannot run yet. Install "
                    "it, or pick another one above.", binary=binary))
        self.assistant_how_form.setRowVisible(self.assistant_found,
                                              bool(self.assistant_found.text()))
        age = assistant.session_age()
        if age is None:
            self.assistant_session_status.setText(t("No conversation going."))
        else:
            self.assistant_session_status.setText(
                t("Last used {minutes} min ago.", minutes=int(age // 60))
            )

    def _reset_assistant_session(self):
        assistant.clear_session()
        self._refresh_assistant_status()

    def _choose_assistant_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, t("Working directory"),
            self.assistant_dir.text().strip() or os.path.expanduser("~"),
        )
        if chosen:
            self.assistant_dir.setText(chosen)

    def _choose_models_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, t("Model folder"),
            self.local_models_dir.text().strip() or str(ggml.DATA_DIR / "models"),
        )
        if chosen:
            self.local_models_dir.setText(chosen)

    # ---- minutes ---------------------------------------------------------

    def _load_minutes(self):
        # Read again whenever the page is opened or a meeting moves on: the
        # meeting being read stays the one in the reader.
        chosen = (self._selected_meeting() or {}).get("base")
        live = getattr(self, "_live_meeting", None)
        self.minutes_list.blockSignals(True)
        self.minutes_list.clear()
        self.minutes_list.blockSignals(False)
        self._live_item = None
        if live:
            self.set_live_meeting(*live)
            if not chosen:
                # The live row stays the one picked, as it was.
                self.minutes_list.setCurrentItem(self._live_item)
        for row in reversed(cfg.read_meetings()):
            title = row.get("title") or t("Meeting")
            head = f"{row.get('ts', '')}  ·  {meeting.length_label(row.get('duration', 0))}"
            brief = (f"{widgets.short_time(row.get('ts', ''))}  ·  "
                     f"{meeting.length_label(row.get('duration', 0))}")
            state = MEETING_STATUS.get(row.get("status", ""), "")
            if state:
                head += "  ·  " + t(state)
                brief += "  ·  " + t(state)
            item = QListWidgetItem(f"{head}\n{title}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setData(widgets.META_ROLE, brief)
            item.setData(widgets.BODY_ROLE, title)
            item.setData(widgets.KIND_ROLE,
                         "failed" if row.get("status") == "failed"
                         else "doc" if row.get("status", "done") == "done" else "history")
            self.minutes_list.addItem(item)
            if chosen and row.get("base") == chosen:
                self.minutes_list.setCurrentItem(item)
        self.minutes_split._place()
        if self._selected_meeting() is None:
            self._show_minutes()
        if not self.minutes_list.count():
            self._set_minutes_text("")
            self.minutes_retry.setEnabled(False)
            self.minutes_retry.hide()
        self._sync_minutes_actions()

    def use_profile_now(self, name):
        """Switch the profile dictation uses, from outside this window (the
        home window's chip), and keep it: the same change as Use this profile
        and Apply, touching nothing else that is waiting to be applied."""
        editor = self.profiles_editor
        # Checked against the profiles as stored, which is what the chip
        # offered: an unapplied rename in the editor must not turn the
        # choice into Default.
        stored = [profile["name"] for profile in profiles.named_profiles(self.conf)]
        chosen = name if name in stored else ""
        editor.use_profile(chosen)
        if self.conf["profile_override"] != chosen:
            self.conf["profile_override"] = chosen
            self.conf.save()
        # The saved copy now has it too, so it is not listed as an edit.
        entries = self._form_entries()
        saved = getattr(self, "_saved_form", None)
        if saved is not None and len(saved) == len(entries):
            for index, (page, value) in enumerate(entries):
                if page == "profiles" and isinstance(value, list) and len(value) == 7:
                    stored = copy.deepcopy(saved[index])
                    stored[6] = chosen
                    saved[index] = stored
        self._show_dirty()

    def set_live_meeting(self, title="", meta=""):
        """Pin a row for the meeting being recorded at the top of the list, or
        take it away with no title. It comes in picked, so the reader shows
        the recording live rather than an older meeting; picking another row
        reads that one meanwhile."""
        item = getattr(self, "_live_item", None)
        if not title:
            self._live_meeting = None
            if item is not None:
                try:
                    self.minutes_list.takeItem(self.minutes_list.row(item))
                except RuntimeError:
                    pass
                self._live_item = None
                self.minutes_split._place()
            return
        self._live_meeting = (title, meta)
        if item is None:
            item = QListWidgetItem(title)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setData(widgets.KIND_ROLE, "mic")
            self.minutes_list.insertItem(0, item)
            self._live_item = item
            self.minutes_list.setCurrentItem(item)
            self.minutes_split._place()
        item.setText(title)
        item.setData(widgets.BODY_ROLE, title)
        item.setData(widgets.META_ROLE, meta)

    def _sync_minutes_actions(self):
        """Copy and Delete only with a meeting in the reader to act on."""
        chosen = self._selected_meeting() is not None
        self.minutes_copy.setEnabled(chosen and bool(self._minutes_text))
        self.minutes_delete.setEnabled(chosen)

    def _selected_meeting(self):
        item = self.minutes_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def set_live_panel(self, panel):
        """The widget the reader shows while the live recording's row is
        picked (elapsed time, the meters)."""
        self.minutes_live_panel = panel
        self.minutes_reader.addWidget(panel)
        self._show_minutes()

    def _show_minutes(self, *_):
        row = self._selected_meeting()
        live = (self.minutes_live_panel is not None
                and getattr(self, "_live_item", None) is not None
                and self.minutes_list.currentItem() is self._live_item)
        self.minutes_reader.setCurrentWidget(self.minutes_live_panel if live
                                             else self.minutes_view)
        if not row:
            self._set_minutes_text("")
            self.minutes_retry.setEnabled(False)
            self.minutes_retry.hide()
            self._sync_minutes_actions()
            return
        doc_path, _wav = cfg.meeting_paths(row["base"])
        try:
            self._set_minutes_text(doc_path.read_text(encoding="utf-8"), markdown=True)
        except OSError:
            self._set_minutes_text(
                row.get("error") or t("Nothing has been written yet.")
            )
        busy = self.meetings is not None and self.meetings.busy
        self.minutes_retry.setVisible(row.get("status") != "done")
        self.minutes_retry.setEnabled(
            self.meetings is not None and not busy and row.get("status") != "done"
        )
        self._sync_minutes_actions()

    def _set_minutes_text(self, text, markdown=False):
        """Show minutes rendered, and keep their text for Copy."""
        self._minutes_text = text
        if markdown:
            self.minutes_view.setHtml(self._minutes_html(text, self.minutes_view.font()))
        else:
            self.minutes_view.setPlainText(text)

    @staticmethod
    def _minutes_html(text, font):
        """Markdown as HTML set like a short document.

        Read straight into a styled viewer, Markdown headings all came out at
        the text's own size, and Qt's own sizes are poster sized and run into
        each other. Here the title is one step under a page heading, sections
        a step above the text with room above them, paragraphs sit close, and
        a bullet keeps a space between itself and its line."""
        document = QTextDocument()
        document.setDefaultFont(font)
        # Raw HTML in the model's text stays text; Markdown links still render.
        document.setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub
                             | QTextDocument.MarkdownFeature.MarkdownNoHTML)
        base = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        # Points from pixels at the 96 dpi the stylesheet's px assume.
        sizes = {1: 15.0, 2: 11.75, 3: max(base, 10.5)}
        cursor = QTextCursor(document)
        block = document.begin()
        lists = []
        while block.isValid():
            fmt = block.blockFormat()
            level = fmt.headingLevel()
            listed = block.textList()
            if level:
                fmt.setTopMargin(0 if block == document.begin() else 12)
                fmt.setBottomMargin(4)
                cursor.setPosition(block.position())
                cursor.setPosition(block.position() + block.length() - 1,
                                   QTextCursor.MoveMode.KeepAnchor)
                char = QTextCharFormat()
                # Markdown headings carry a relative size step as well, which
                # Qt adds on top of any size set here.
                char.setProperty(QTextFormat.Property.FontSizeAdjustment, 0)
                char.setFontPointSize(sizes.get(level, base))
                char.setFontWeight(QFont.Weight.DemiBold)
                cursor.mergeCharFormat(char)
            elif listed is not None:
                lists.append((block, listed))
            else:
                fmt.setTopMargin(0)
                fmt.setBottomMargin(6)
            cursor.setPosition(block.position())
            cursor.setBlockFormat(fmt)
            block = block.next()
        # A list item becomes a paragraph with its own marker and a hanging
        # indent: Qt draws a list's bullet tight against the text.
        for block, listed in lists:
            style = listed.format().style()
            if style in (QTextListFormat.Style.ListDecimal, QTextListFormat.Style.ListLowerAlpha,
                         QTextListFormat.Style.ListUpperAlpha):
                marker = listed.itemText(block)
            else:
                marker = "\u2022"
            depth = listed.format().indent()
            listed.remove(block)
            fmt = block.blockFormat()
            fmt.setIndent(0)
            fmt.setLeftMargin(10 + 18 * depth)
            fmt.setTextIndent(-14)
            fmt.setTopMargin(0)
            fmt.setBottomMargin(3)
            cursor.setPosition(block.position())
            cursor.setBlockFormat(fmt)
            cursor.insertText(marker + "\u2002")
        # Read back in, an <h1> adds Qt's own size step on top of the size the
        # span sets; as a paragraph the sizes above are the ones shown.
        return re.sub(r"<(/?)h[1-6]\b", r"<\1p", document.toHtml())

    def _retry_minutes(self):
        row = self._selected_meeting()
        if not row or self.meetings is None or self.meetings.busy:
            return
        # A row that already has its transcript resumes from there; only a run
        # that never got that far goes back to the audio.
        self.meetings.run(row)
        self.minutes_retry.setEnabled(False)
        self.minutes_status.setText(t("Working…"))

    def _delete_minutes(self):
        row = self._selected_meeting()
        if not row:
            return
        if self.meetings is not None and self.meetings.running_base == row["base"]:
            QMessageBox.information(self, t("Minutes"),
                                    t("This one is being written up right now."))
            return
        if not self._confirm(
            t("Delete this meeting, its minutes and its recording?"), t("Minutes")
        ):
            return
        try:
            cfg.delete_meetings([row["base"]])
        except OSError as exc:
            QMessageBox.warning(self, t("Minutes"), t("Failed: {error}", error=exc))
        self._load_minutes()

    def _on_minutes_progress(self, _base, message):
        self.minutes_status.setText(message)

    def _on_minutes_finished(self, _base, title):
        self.minutes_status.setText(t("Done: {title}", title=title))
        self._load_minutes()

    def _on_minutes_failed(self, _base, error):
        self.minutes_status.setText(t("Failed: {error}", error=error))
        self._load_minutes()

    # ---- history ---------------------------------------------------------

    def _load_history(self):
        # A retry's result reloads the list, so what was queued is settled.
        self._queued_retries = set()
        # A reload arrives whenever a dictation settles, so whatever was
        # selected is selected again rather than lost under the user's hand.
        chosen = {cfg._row_key(row) for row in self._selected_rows()}
        self.history.clear()
        for row in reversed(cfg.read_history(self.conf["history_limit"])):
            # The whole text: the row shows what fits in its lines, and the
            # search looks through all of it.
            preview = (row.get("text") or "").replace("\n", " ")
            header = t("{ts}  ({duration} s)",
                       ts=row.get("ts", ""), duration=row.get("duration", 0))
            if row.get("failed"):
                error = (row.get("error") or "").replace("\n", " ")
                preview = t("Failed: {error}", error=error[:110])
                if _retryable(row):
                    preview += "\n" + t("The recording was kept; select it and press Try again.")
            elif row.get("mode") == "ask":
                # The text of an answer says nothing about what was asked, and
                # out of that context half of them read like non sequiturs.
                asked = (row.get("question") or row.get("raw") or "").replace("\n", " ")
                # Rows written before the provider was recorded are all Claude's,
                # because it was the only one the history could name.
                who = agent_name(row.get("assistant"))
                header += t("  ·  asked {who}: {question}",
                            who=i18n.name(who, "dative"),
                            question=asked[:60] + ("…" if len(asked) > 60 else ""))
            header += history_marks(row)
            item = QListWidgetItem(f"{header}\n{preview}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            # The list says when and which profile in a few words.
            item.setData(widgets.META_ROLE, history_brief(row))
            # The full header, program and seconds and all, for whoever hovers.
            item.setToolTip(header)
            # A failure's row says what failed; why is on the line under it.
            item.setData(widgets.BODY_ROLE, history_title(row))
            item.setData(widgets.KIND_ROLE, history_kind(row))
            self.history.addItem(item)
            if cfg._row_key(row) in chosen:
                item.setSelected(True)
        self._filter_history()
        self._sync_history_actions()

    def _filter_history(self, *_):
        needle = self.history_search.text().strip().casefold()
        for index in range(self.history.count()):
            item = self.history.item(index)
            hidden = bool(needle) and needle not in item.text().casefold()
            item.setHidden(hidden)
            if hidden:
                item.setSelected(False)

    def _reveal_history(self, item):
        try:
            if item is not None and item.listWidget() is self.history:
                self.history.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)
        except RuntimeError:
            pass  # the list was reloaded and the item deleted meanwhile

    def _retryable_selection(self):
        """Selected failed dictations whose recording is still there, oldest
        first, the order they were spoken and should be retried in."""
        return [row for row in reversed(self._selected_rows()) if _retryable(row)]

    def _sync_history_actions(self):
        rows = self._selected_rows()
        retryable = bool(self._retryable_selection())
        translated = bool(self._translated_selection())
        # A failure with nothing to retry: the newest selected is one, and no
        # selected row could be tried again.
        stranded = bool(rows and rows[0].get("failed") and not retryable)
        self.history_retry.setEnabled(retryable)
        self.history_retry.setVisible(retryable)
        self.history_record.setVisible(stranded)
        self.history_check.setVisible(stranded)
        self.history_original.setEnabled(translated)
        self.history_original.setVisible(translated)
        readable = any(not row.get("failed") for row in rows)
        # A failure has no text to copy or open: those are left out of its
        # menu rather than shown greyed beside the ones that do something.
        for item in (self.history_copy, self.history_open):
            item.setEnabled(readable)
            item.setVisible(readable or not rows)
        self.history_delete.setEnabled(bool(rows))
        self.history_clear.setEnabled(self.history.count() > 0)

    def _translated_selection(self):
        return [row for row in self._selected_rows()
                if row.get("translate_to") and row.get("raw")]

    def _copy_original(self):
        rows = self._translated_selection()
        if rows:
            QGuiApplication.clipboard().setText(
                "\n\n".join(row["raw"] for row in rows))

    def _retry_history(self):
        queued = self.__dict__.setdefault("_queued_retries", set())
        rows = [row for row in self._retryable_selection()
                if cfg._row_key(row) not in queued]
        for row in rows:
            queued.add(cfg._row_key(row))
            self.retry_requested.emit(row)
        if rows:
            # Until the result reloads the list, a second press would queue
            # what is already queued.
            self.history_retry.setEnabled(False)

    def _selected_rows(self):
        """Selected entries, newest first, the order they are listed in."""
        items = sorted(self.history.selectedItems(), key=self.history.row)
        return [item.data(Qt.ItemDataRole.UserRole) for item in items]

    def _copy_history(self):
        rows = [row for row in self._selected_rows() if not row.get("failed")]
        if not rows:
            return
        QGuiApplication.clipboard().setText("\n\n".join(row.get("text", "") for row in rows))
        # Each copied row says so for a moment, where its copy button was.
        self._copied_keys = {cfg._row_key(row) for row in rows}
        self._copied_timer.start()
        self.history.viewport().update()

    def _delete_history(self):
        rows = self._selected_rows()
        if not rows:
            return
        # One entry goes without asking; a multi-selection is easy to make by
        # accident, and there is no undo.
        if len(rows) > 1 and not self._confirm(
            t("Delete the {count} selected entries?", count=len(rows))
        ):
            return
        self._rewrite_history(lambda: cfg.delete_history(rows))

    def _clear_history(self):
        if not self.history.count():
            return
        if not self._confirm(t("Delete the whole history? This cannot be undone.")):
            return
        self._rewrite_history(cfg.clear_history)

    def _confirm(self, question, title=None):
        answer = QMessageBox.question(
            self, title or t("History"), question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _rewrite_history(self, action):
        try:
            action()
        except OSError as exc:
            QMessageBox.warning(self, t("History"), t("Failed: {error}", error=exc))
        self._load_history()

    def _history_menu(self, pos, on_row=True):
        """The context menu, and the row's More menu: every action on the
        selection, with what does not apply to it left out or greyed."""
        if on_row:
            item = self.history.itemAt(pos)
            if item is not None and not item.isSelected():
                self.history.setCurrentItem(item)
        self._sync_history_actions()
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for item in (self.history_copy, self.history_open, self.history_original,
                     self.history_retry, self.history_record, self.history_check):
            if item.isVisible():
                menu.addAction(item)
        menu.addSeparator()
        menu.addAction(self.history_delete)
        menu.addAction(self.history_clear)
        menu.exec(self.history.viewport().mapToGlobal(pos))
        menu.deleteLater()
