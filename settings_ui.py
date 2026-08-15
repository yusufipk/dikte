"""Settings window."""

import os
import shutil
import threading

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

import api
import assistant
import audio
import cleanup
import config as cfg
import filetranscribe
import ggml
import hotkey
import ipc
import meeting
import paste
from filetranscribe import FileTranscriber
from i18n import t

UI_LANGUAGES = [("Automatic (system)", "auto"), ("Turkish", "tr"), ("English", "en")]
LANGUAGES = [
    ("Detect automatically", "auto"), ("Turkish", "tr"), ("English", "en"),
    ("German", "de"), ("French", "fr"), ("Spanish", "es"), ("Arabic", "ar"),
]
CORNERS = ["bottom-left", "bottom-right", "top-left", "top-right"]
# The provider box offers what config knows how to reach, this machine first.
TRANSCRIBE_PROVIDERS = ([("This machine (whisper.cpp)", "local")]
                        + [(who.service, name)
                           for name, who in cfg.TRANSCRIBERS.items()])
# Starting points for the model box; "Fetch model list" replaces them with
# whatever the provider offers today.
TRANSCRIBE_MODELS = {
    "openai": ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"],
    "groq": ["whisper-large-v3-turbo", "whisper-large-v3"],
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
# In the order they answer in. A request to OpenRouter is over in a second, a
# model here takes a little longer and costs nothing, and the two CLIs the agent
# can run on open a whole session to do the smaller job.
CLEANUP_PROVIDERS = [
    ("OpenRouter", "openrouter"), ("This machine (llama.cpp)", "local"),
    ("Claude Code", "claude"), ("Codex", "codex"),
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
    ("Claude Code", "claude"), ("Codex", "codex"), ("OpenRouter", "openrouter"),
]
# Aliases resolve to the newest model of that name, so they age better than an
# id does; a full id can be typed in when a particular one is wanted.
ASSISTANT_MODELS = ["sonnet", "opus", "haiku", "fable"]
CODEX_MODELS = ["gpt-5.4-codex", "gpt-5.4", "o4-mini"]
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


class LocalModelBox(QGroupBox):
    """The program, the model, and the two downloads that put them there.

    One class for whisper.cpp and llama.cpp, because the job is the same one
    twice: say whether the program is here, offer the models somebody publishes,
    fetch the chosen one, and stay usable while a gigabyte arrives. Nothing is
    listed in the source; `repos` and `models` are asked at the moment the box is
    opened, so a model published this morning is in the list this afternoon.
    """

    _listed = pyqtSignal(list, str)
    _quants = pyqtSignal(list, str)
    # qint64 rather than int, which is C++'s 32-bit one: a 2.3 GB model is more
    # than fits in it, and the count comes out the far side negative.
    _progress = pyqtSignal("qint64", "qint64")
    _finished = pyqtSignal(str, str)
    _installed = pyqtSignal(str, str)

    changed = pyqtSignal()

    def __init__(self, program, title, models, model_path, repos=None, parent=None):
        super().__init__(title, parent)
        self.program = program
        self._models = models          # () -> [hub.Item], or (repo) -> [hub.Item]
        self._model_path = model_path  # (name) -> Path
        self._repos = repos            # None, or () -> [repo id]
        self._downloading = False
        self._pending = False
        self._stop = False
        self._wanted = ""              # the model to select once a list arrives

        form = QFormLayout(self)

        self.program_label = QLabel("")
        self.program_label.setWordWrap(True)
        self.install_button = QPushButton(t("Download"))
        self.install_button.clicked.connect(self._install_program)
        form.addRow(t("Program"), self._side_by_side(self.program_label,
                                                     self.install_button))

        if self._repos is not None:
            self.repo = QComboBox()
            self.repo.setEditable(True)
            self.repo.setToolTip(t("A Hugging Face repository of GGUF files. The "
                                   "list is fetched; any other one can be typed in."))
            self.repo.currentTextChanged.connect(self._repo_changed)
            form.addRow(t("Publisher"), self.repo)

        self.model = QComboBox()
        self.download_button = QPushButton(t("Download"))
        self.download_button.clicked.connect(self._download)
        self.delete_button = QPushButton(t("Delete"))
        self.delete_button.clicked.connect(self._delete)
        form.addRow(t("Model"), self._side_by_side(self.model,
                                                   self.download_button,
                                                   self.delete_button))
        self.model.currentIndexChanged.connect(self._model_changed)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        form.addRow(self.status)

        self._listed.connect(self._on_listed)
        self._quants.connect(self._on_listed)
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

        Nothing is fetched here: building the settings window is not the same as
        opening it, and a list nobody is looking at is not worth a request. What
        is already on this disk is shown straight away either way.
        """
        self._wanted = model
        self._pending = True
        self._show_program()
        if self._repos is not None:
            self.repo.blockSignals(True)
            self.repo.clear()
            self.repo.addItems(list(ggml.SUGGESTED_LLM))
            self.repo.setCurrentText(repo or ggml.SUGGESTED_LLM[0])
            self.repo.blockSignals(False)
            self._fit_popup(self.repo)
        self._fill_models([])

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending:
            self._pending = False
            if self._repos is not None:
                self._fill_repos(self.repository())
            self._fetch_models(self.repository())

    def _show_program(self):
        path = ggml.program_path(self.program)
        if not path:
            self.program_label.setText(t("Not installed."))
            self.install_button.setVisible(True)
            return
        self.install_button.setVisible(not ggml.installed_program(self.program)
                                       and not ggml.system_program(self.program))
        if ggml.system_program(self.program):
            # Worth saying which one is running: a distribution package is built
            # for this machine and may reach the graphics card, while the
            # released binaries carry processor backends only.
            self.program_label.setText(t("Installed on the system: {path}", path=path))
        else:
            self.program_label.setText(
                t("Downloaded, version {version}.",
                  version=ggml.installed_version(self.program) or "?"))

    # ---- the lists -------------------------------------------------------

    def _fill_repos(self, current):
        def work():
            self._listed.emit([("repos", ggml.llm_repos())], "")

        threading.Thread(target=work, daemon=True).start()

    def _repo_changed(self):
        if not self._downloading:
            self._fetch_models(self.repository())

    def _fetch_models(self, repo=""):
        self.status.setText(t("Fetching the model list…"))

        def work():
            try:
                found = self._models(repo) if self._repos is not None else self._models()
                self._quants.emit([("models", found)], "")
            except ggml.LocalError as exc:
                self._quants.emit([], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_listed(self, payload, error):
        if error:
            self.status.setText(error)
            self._refresh_buttons()
            return
        kind, found = payload[0]
        if kind == "repos":
            current = self.repo.currentText()
            self.repo.blockSignals(True)
            self.repo.clear()
            self.repo.addItems(found)
            self.repo.setCurrentText(current)
            self.repo.blockSignals(False)
            self._fit_popup(self.repo)
            return
        self._fill_models(found)

    def _fill_models(self, items):
        """One row per model, saying what it weighs and whether it is here."""
        wanted = self._wanted or self.selected()
        here = [name for name in (self._model_path(i.name).name for i in items)]
        self.model.blockSignals(True)
        self.model.clear()
        for item, name in zip(items, here):
            mark = (t("downloaded") if ggml.have_model(self._model_path(item.name))
                    else ggml.human_size(item.size))
            self.model.addItem(f"{name}  ({mark})", name)
            self.model.setItemData(self.model.count() - 1, item, Qt.ItemDataRole.UserRole + 1)
        # A model that was downloaded and then dropped from the list upstream is
        # still on this disk and still works, so it stays on offer.
        for name in self._on_disk():
            if self.model.findData(name) < 0:
                self.model.addItem(f"{name}  ({t('downloaded')})", name)
        # And one that is chosen but not here, because the file was deleted from
        # underneath or the settings came from another machine, stays chosen:
        # Save reads this box, and a row missing here would quietly empty the
        # setting rather than showing that the model needs downloading again.
        if wanted and self.model.findData(wanted) < 0:
            self.model.addItem(f"{wanted}  ({t('not downloaded')})", wanted)
        index = self.model.findData(wanted)
        self.model.setCurrentIndex(max(index, 0))
        self.model.blockSignals(False)
        self._fit_popup(self.model)
        self._wanted = ""
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
                ggml.install_program(self.program, on_progress=self._report)
                self._installed.emit("", "")
            except ggml.LocalError as exc:
                self._installed.emit("", str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_installed(self, _, error):
        self.install_button.setEnabled(True)
        self._show_program()
        if error:
            self.program_label.setText(error)
        self.changed.emit()

    def _current_item(self):
        return self.model.currentData(Qt.ItemDataRole.UserRole + 1)

    def _download(self):
        if self._downloading:
            self._stop = True
            return
        item = self._current_item()
        if item is None:
            return
        self._downloading, self._stop = True, False
        self._refresh_buttons()

        def work():
            try:
                landed = ggml.download(item, self._model_path(item.name),
                                       on_progress=self._report,
                                       should_stop=lambda: self._stop)
                self._finished.emit(item.name if landed else "", "")
            except ggml.LocalError as exc:
                self._finished.emit("", str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _report(self, done, total):
        self._progress.emit(done, total)

    def _on_progress(self, done, total):
        share = f" ({done * 100 // total}%)" if total else ""
        text = t("Downloading: {done} of {total}{share}",
                 done=ggml.human_size(done), total=ggml.human_size(total or done),
                 share=share)
        if self._downloading:
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
        items = [self.model.itemData(i, Qt.ItemDataRole.UserRole + 1)
                 for i in range(self.model.count())]
        self._wanted = self.selected()
        self._fill_models([i for i in items if i is not None])

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
        self.delete_button.setEnabled(here and not self._downloading)
        self.download_button.setText(t("Stop") if self._downloading else t("Download"))
        self.download_button.setEnabled(self._downloading or (bool(name) and not here))
        if self._downloading:
            return
        if not name:
            self.status.setText(t("Nothing downloaded yet."))
        elif here:
            self.status.setText(t("Ready: {name}.", name=name))
        else:
            self.status.setText(t("{name} has not been downloaded yet.", name=name))


class SettingsWindow(QDialog):
    applied = pyqtSignal()

    _models_loaded = pyqtSignal(list, str)
    _transcribe_models_loaded = pyqtSignal(list, str)
    # Which key was tested, whether it worked, and what to write under it.
    _test_done = pyqtSignal(str, bool, str)

    def __init__(self, conf, meetings=None, parent=None):
        super().__init__(parent)
        self.conf = conf
        self.meetings = meetings
        # Filled in by _shortcut_row as the tabs are built: which combination
        # box, status label and "nothing installed" line belong to each of the
        # global shortcuts. One dictionary is what lets install, remove and the
        # status line be written once instead of once per key.
        self._shortcut_rows = {}
        # Each provider keeps its own transcription model, so switching the
        # provider back and forth never overwrites the other one's.
        self._models = dict.fromkeys(cfg.TRANSCRIBERS, "")
        self._key_fields = {}
        self._testers = {}
        self._shown_provider = ""
        self.transcriber = FileTranscriber(conf, self)
        self.setWindowTitle(t("Dikte Settings"))
        self.resize(680, 640)

        tabs = self.tabs = QTabWidget(self)
        tabs.addTab(self._general_tab(), t("General"))
        self.api_tab_index = tabs.addTab(self._api_tab(), t("API and models"))
        tabs.addTab(self._prompt_tab(), t("Cleanup rules"))
        tabs.addTab(self._assistant_tab(), t("Agent"))
        tabs.addTab(self._meeting_tab(), t("Meeting"))
        tabs.addTab(self._minutes_tab(), t("Minutes"))
        tabs.addTab(self._file_tab(), t("Audio file"))
        tabs.addTab(self._shortcut_tab(), t("Shortcuts"))
        tabs.addTab(self._history_tab(), t("History"))

        # Save keeps the window open, so the window is closed with the titlebar
        # cross (or Escape) instead. A "Cancel" next to it would be a lie: the
        # settings are already on disk by then.
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(t("Save"))
        buttons.accepted.connect(self._save)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

        self._models_loaded.connect(self._on_models_loaded)
        self._transcribe_models_loaded.connect(self._on_transcribe_models_loaded)
        self._test_done.connect(self._on_test_done)
        self.transcriber.progress.connect(self._on_file_progress)
        self.transcriber.finished.connect(self._on_file_finished)
        self.transcriber.failed.connect(self._on_file_failed)
        if self.meetings is not None:
            self.meetings.progress.connect(self._on_minutes_progress)
            self.meetings.finished.connect(self._on_minutes_finished)
            self.meetings.failed.connect(self._on_minutes_failed)
        self._load()
        # Connected after the load, so that filling the boxes in is not taken
        # for the user ticking them.
        self.file_timestamps.toggled.connect(self._remember_file_choices)
        self.file_cleanup.toggled.connect(self._remember_file_choices)
        # On a machine where nothing can transcribe yet, this window was opened
        # because of that, so open it on the tab that fixes it.
        if not conf.transcribe_ready():
            self.tabs.setCurrentIndex(self.api_tab_index)

    # ---- tabs ----------------------------------------------------------

    def _general_tab(self):
        page = QWidget()
        form = QFormLayout(page)

        self.ui_language = QComboBox()
        for label, code in UI_LANGUAGES:
            self.ui_language.addItem(t(label), code)
        self.ui_language.setToolTip(
            t("Restart Dikte for the language change to reach every window.")
        )
        form.addRow(t("Interface language"), self.ui_language)

        self.mic = QComboBox()
        self.mic.addItem(t("Default microphone"), "")
        for name, desc in audio.list_sources():
            self.mic.addItem(desc, name)
        form.addRow(t("Microphone"), self.mic)

        self.language = QComboBox()
        for label, code in LANGUAGES:
            self.language.addItem(t(label), code)
        form.addRow(t("Speech language"), self.language)

        self.auto_paste = QCheckBox(t("Paste the text into the focused window"))
        form.addRow("", self.auto_paste)

        self.paste_shortcut = QComboBox()
        # A shortlist of the combinations that usually paste, not the set of
        # them: a stored one this desktop does not offer is kept as it is
        # rather than quietly replaced by the first item on the list.
        self.paste_shortcut.setEditable(True)
        self.paste_shortcut.addItems(paste.desktop().shortcuts)
        self.paste_shortcut.setToolTip(t(
            "macOS asks for Accessibility permission the first time this is sent."
            if paste.desktop() is paste.MACOS else
            "Terminals usually want ctrl+shift+v. Change this if pasting does nothing."
        ))
        form.addRow(t("Paste key"), self.paste_shortcut)

        self.restore_clipboard = QCheckBox(t("Restore the previous clipboard after pasting"))
        form.addRow("", self.restore_clipboard)

        self.corner = QComboBox()
        for value in CORNERS:
            self.corner.addItem(t(value), value)
        form.addRow(t("Indicator corner"), self.corner)

        self.max_seconds = QSpinBox()
        self.max_seconds.setRange(10, 3600)
        self.max_seconds.setSuffix(t(" s"))
        form.addRow(t("Longest recording"), self.max_seconds)

        self.skip_silent = QCheckBox(t("Skip silent recordings (don't call the API)"))
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
        form.addRow(t("Silence threshold"), self.silence_db)

        self.filter_hallucinations = QCheckBox(
            t("Discard stock phrases models invent for near-silent audio")
        )
        self.filter_hallucinations.setToolTip(
            t("Whisper answers silence with things like “Thanks for watching”.")
        )
        form.addRow("", self.filter_hallucinations)

        self.keep_audio = QCheckBox(
            t("Keep audio files ({path})", path=str(cfg.RECORDINGS_DIR))
        )
        form.addRow("", self.keep_audio)
        return page

    def _api_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)

        # Keys first, then the two jobs, because OpenRouter can now do both of
        # them and a key no longer belongs to a single job.
        keys = QGroupBox(t("Keys"))
        keys_form = QFormLayout(keys)
        self.openai_key = self._key_row(
            keys_form, "openai", t("sk-… (falls back to OPENAI_API_KEY)"),
            self._test_openai)
        self.groq_key = self._key_row(
            keys_form, "groq", t("gsk_… (falls back to GROQ_API_KEY)"),
            self._test_groq)
        self.openrouter_key = self._key_row(
            keys_form, "openrouter", t("sk-or-… (falls back to OPENROUTER_API_KEY)"),
            self._test_openrouter)
        outer.addWidget(keys)

        stt = QGroupBox(t("Speech to text"))
        stt_form = QFormLayout(stt)
        self.transcribe_provider = QComboBox()
        for label, value in TRANSCRIBE_PROVIDERS:
            self.transcribe_provider.addItem(t(label), value)
        stt_form.addRow(t("Provider"), self.transcribe_provider)

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
        # A spanning row: in the narrow field column a wrapped label gets a
        # height that fits one line, and the rest of the text is cut off.
        self.transcribe_status = QLabel("")
        self.transcribe_status.setWordWrap(True)
        stt_form.addRow(self.transcribe_status)

        self.local_whisper = LocalModelBox(
            ggml.WHISPER, t("On this machine"),
            ggml.whisper_models, ggml.whisper_model_path)
        stt_form.addRow(self.local_whisper)

        self.local_gpu = QCheckBox(t("Use the graphics card"))
        self.local_gpu.setToolTip(
            t("whisper.cpp reaches the card through CUDA, ROCm or Vulkan when the "
              "build it is running was made with one. A build without any of them "
              "runs on the processor whatever this says."))
        self.local_preload = QCheckBox(t("Load the model when Dikte starts"))
        self.local_preload.setToolTip(
            t("A large model takes a second or two to load. Loading it up front "
              "spends that once instead of on the first dictation, at the cost of "
              "the memory it sits in."))
        self.local_threads = QSpinBox()
        self.local_threads.setRange(0, 64)
        self.local_threads.setSpecialValueText(t("Automatic"))
        # A spin box asks for room for its numbers, and 64 is two characters:
        # the word standing in for zero is what actually has to fit, and on
        # macOS, where the stepper sits inside the frame, it does not. Widened
        # to the word rather than to a number picked by eye, so that it still
        # fits once the word is "Otomatik".
        self.local_threads.setMinimumWidth(
            self.local_threads.fontMetrics()
            .horizontalAdvance(t("Automatic")) + 56)
        self.local_options = QWidget()
        options_form = QFormLayout(self.local_options)
        options_form.setContentsMargins(0, 0, 0, 0)
        options_form.addRow("", self.local_gpu)
        options_form.addRow("", self.local_preload)
        options_form.addRow(t("Threads"), self.local_threads)
        stt_form.addRow(self.local_options)

        self.transcribe_provider.currentIndexChanged.connect(self._provider_changed)
        outer.addWidget(stt)

        orr = QGroupBox(t("Transcript cleanup"))
        orr_form = self.cleanup_form = QFormLayout(orr)
        self.cleanup_enabled = QCheckBox(t("Clean the transcript with a model"))
        orr_form.addRow("", self.cleanup_enabled)

        self.cleanup_provider = QComboBox()
        for label, value in CLEANUP_PROVIDERS:
            self.cleanup_provider.addItem(t(label), value)
        self.cleanup_provider.setToolTip(t(
            "OpenRouter is the quickest and the only one that needs nothing "
            "installed. llama.cpp runs here, on a model downloaded below. Claude "
            "Code and Codex clean up on the subscription you already have, "
            "without a second key, and take a few seconds longer because each "
            "one opens a session to do it."
        ))
        self.cleanup_provider.currentIndexChanged.connect(self._cleanup_provider_changed)
        orr_form.addRow(t("Runs on"), self.cleanup_provider)

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
        orr_form.addRow(t("Model"), self.cleanup_claude_model)

        self.cleanup_codex_model = QComboBox()
        self.cleanup_codex_model.setEditable(True)
        self.cleanup_codex_model.addItems([t("Codex's own default")] + CODEX_MODELS)
        orr_form.addRow(t("Model"), self.cleanup_codex_model)

        self.cleanup_reasoning = QComboBox()
        for label, value in REASONING_LEVELS:
            self.cleanup_reasoning.addItem(t(label), value)
        self.cleanup_reasoning.setToolTip(
            t("How long a thinking model may reason before it answers. Cleanup is "
              "a light job, so more thinking mostly costs time and tokens. Models "
              "that cannot think ignore this.")
        )
        orr_form.addRow(t("Thinking"), self.cleanup_reasoning)

        self.models_label = QLabel(t("Runs on OpenRouter."))
        self.models_label.setWordWrap(True)
        orr_form.addRow(self.models_label)

        self.local_llm = LocalModelBox(
            ggml.LLAMA, t("On this machine"),
            ggml.llm_quants, ggml.llm_model_path, repos=ggml.llm_repos)
        orr_form.addRow(self.local_llm)

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

        llm_form.setContentsMargins(0, 0, 0, 0)
        llm_form.addRow("", self.local_llm_gpu)
        llm_form.addRow("", self.local_llm_preload)
        llm_form.addRow(t("Thinking"), self.local_llm_reasoning)
        orr_form.addRow(self.local_llm_options)

        outer.addWidget(orr)
        outer.addStretch(1)
        return page

    def _prompt_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        # Two jobs, two sets of rules: dictation is rewritten for reading, an
        # audio file becomes subtitles that have to stay in sync with the voice.
        inner = QTabWidget()
        self.cleanup_prompt = self._prompt_page(
            inner, t("Dictation"),
            t("System instruction given to the cleanup model. This is where you "
              "decide how much it may touch your words."),
            cfg.default_cleanup_prompt,
        )
        self.file_cleanup_prompt = self._prompt_page(
            inner, t("Audio file"),
            t("Used instead when an audio or video file is cleaned up. It is "
              "written for subtitles: lines stay where they are, nothing is "
              "shortened, and misheard words are repaired from the context."),
            cfg.default_file_cleanup_prompt,
        )
        layout.addWidget(inner, 1)

        hint = QLabel(t("Names and terms you say often (optional). They go to the "
                        "transcription model as a hint, and to the cleanup model as a "
                        "glossary, so it can repair the ones that still come out wrong."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.transcribe_prompt = QPlainTextEdit()
        self.transcribe_prompt.setMaximumHeight(90)
        layout.addWidget(self.transcribe_prompt)
        return page

    def _assistant_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(t(
            "This shortcut records the same way dictation does, but the "
            "transcript is not what gets pasted. It goes to an agent as a "
            "command, and what comes back is pasted instead: the answer to a "
            "question, or a sentence saying what was done. Claude Code and "
            "Codex run as the session you would have opened yourself, with your "
            "skills, your connected services and your account."
        ))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.assistant_found = QLabel("")
        self.assistant_found.setWordWrap(True)
        layout.addWidget(self.assistant_found)

        how = QGroupBox(t("How it runs"))
        how_form = QFormLayout(how)
        self._shortcut_row(
            how_form, "ask", t("Shortcut"),
            t("No global shortcut installed. The tray menu asks it too."),
        )

        self.assistant_provider = QComboBox()
        for label, value in ASSISTANT_PROVIDERS:
            self.assistant_provider.addItem(t(label), value)
        self.assistant_provider.currentIndexChanged.connect(
            self._assistant_provider_changed
        )
        how_form.addRow(t("Runs on"), self.assistant_provider)

        self.assistant_dir = QLineEdit()
        self.assistant_dir.setPlaceholderText(os.path.expanduser("~"))
        browse = QPushButton(t("Choose…"))
        browse.clicked.connect(self._choose_assistant_dir)
        how_form.addRow(t("Working directory"),
                        self._row(self.assistant_dir, browse))
        dir_note = QLabel(t(
            "The directory the command runs in, which decides which project's "
            "instructions and files it can see. Your own skills and services "
            "are there whichever one it is."
        ))
        dir_note.setWordWrap(True)
        how_form.addRow(dir_note)

        # One scale for all three: how hard to think is one thing to want, and
        # each provider is handed the nearest rung it actually has.
        self.assistant_reasoning = QComboBox()
        for label, value in REASONING_LEVELS:
            self.assistant_reasoning.addItem(t(label), value)
        self.assistant_reasoning.setToolTip(t(
            "More thinking is slower, and you are standing in front of the "
            "screen while it happens. Worth it for a job that has to be worked "
            "out rather than looked up."
        ))
        how_form.addRow(t("Thinking"), self.assistant_reasoning)

        self.assistant_timeout = QSpinBox()
        self.assistant_timeout.setRange(15, 3600)
        self.assistant_timeout.setSuffix(t(" s"))
        self.assistant_timeout.setToolTip(t(
            "A command still running after this is given up on. The tray menu "
            "can stop one earlier."
        ))
        how_form.addRow(t("Give up after"), self.assistant_timeout)
        layout.addWidget(how)

        # One box per provider, only the chosen one on screen: they have nothing
        # in common past the model, and three sets of half-relevant fields would
        # be worse than none.
        self.claude_box = QGroupBox(t("Claude Code"))
        claude_form = QFormLayout(self.claude_box)
        self.assistant_model = QComboBox()
        self.assistant_model.setEditable(True)
        self.assistant_model.addItems(ASSISTANT_MODELS)
        self.assistant_model.setToolTip(t(
            "A name like “sonnet” always means the newest model of that line. "
            "Opus thinks harder and answers slower, which is felt here more "
            "than anywhere else: you are standing in front of the screen."
        ))
        claude_form.addRow(t("Model"), self.assistant_model)
        self.assistant_permission = QComboBox()
        for label, value in PERMISSION_MODES:
            self.assistant_permission.addItem(t(label), value)
        claude_form.addRow(t("Permissions"), self.assistant_permission)
        layout.addWidget(self.claude_box)

        self.codex_box = QGroupBox(t("Codex"))
        codex_form = QFormLayout(self.codex_box)
        self.assistant_codex_model = QComboBox()
        self.assistant_codex_model.setEditable(True)
        self.assistant_codex_model.addItem(t("Codex's own default"), "")
        for name in CODEX_MODELS:
            self.assistant_codex_model.addItem(name, name)
        codex_form.addRow(t("Model"), self.assistant_codex_model)
        self.assistant_codex_sandbox = QComboBox()
        for label, value in CODEX_SANDBOXES:
            self.assistant_codex_sandbox.addItem(t(label), value)
        codex_form.addRow(t("Sandbox"), self.assistant_codex_sandbox)
        layout.addWidget(self.codex_box)

        self.openrouter_box = QGroupBox("OpenRouter")
        or_form = QFormLayout(self.openrouter_box)
        self.assistant_openrouter_model = QComboBox()
        self.assistant_openrouter_model.setEditable(True)
        self.assistant_openrouter_model.addItems(ASSISTANT_OR_MODELS)
        or_form.addRow(t("Model"), self.assistant_openrouter_model)
        or_note = QLabel(t(
            "A plain question and a plain answer, over the OpenRouter key you "
            "already have. It runs no commands, opens no files and reaches none "
            "of your services, so it can tell you what the capital of Peru is "
            "but not what is in your calendar. Working directory and permissions "
            "above mean nothing here."
        ))
        or_note.setWordWrap(True)
        or_form.addRow(or_note)
        layout.addWidget(self.openrouter_box)

        thread = QGroupBox(t("The conversation"))
        thread_form = QFormLayout(thread)
        self.assistant_session_minutes = QSpinBox()
        self.assistant_session_minutes.setRange(0, 1440)
        self.assistant_session_minutes.setSuffix(t(" min"))
        self.assistant_session_minutes.setSpecialValueText(t("every command on its own"))
        thread_form.addRow(t("Carry on for"), self.assistant_session_minutes)
        thread_note = QLabel(t(
            "Commands within this long of each other are one conversation, so "
            "“and move that to Thursday” knows what “that” is. After it, the "
            "next command starts fresh."
        ))
        thread_note.setWordWrap(True)
        thread_form.addRow(thread_note)
        reset = QPushButton(t("Start a new conversation now"))
        reset.clicked.connect(self._reset_assistant_session)
        self.assistant_session_status = QLabel("")
        self.assistant_session_status.setWordWrap(True)
        thread_form.addRow(self._row(reset), self.assistant_session_status)
        layout.addWidget(thread)

        answer = QGroupBox(t("The answer"))
        answer_form = QFormLayout(answer)
        self.assistant_paste = QCheckBox(t("Paste it into the focused window"))
        self.assistant_paste.setToolTip(t(
            "It is copied to the clipboard either way."
        ))
        answer_form.addRow("", self.assistant_paste)
        self.assistant_cleanup = QCheckBox(t("Clean the transcript up before sending it"))
        self.assistant_cleanup.setToolTip(t(
            "Off by default: Claude reads through “erm” and “you know” without "
            "help, and cleanup costs an API call and a second or two."
        ))
        answer_form.addRow("", self.assistant_cleanup)
        layout.addWidget(answer)

        prompt_label = QLabel(t(
            "Told to the agent alongside every command, on top of whatever your "
            "own configuration already says."
        ))
        prompt_label.setWordWrap(True)
        layout.addWidget(prompt_label)
        self.assistant_prompt = QPlainTextEdit()
        self.assistant_prompt.setMinimumHeight(180)
        layout.addWidget(self.assistant_prompt, 1)
        reset_prompt = QPushButton(t("Reset to default"))
        reset_prompt.clicked.connect(
            lambda: self.assistant_prompt.setPlainText(cfg.default_assistant_prompt())
        )
        layout.addWidget(reset_prompt, 0, Qt.AlignmentFlag.AlignRight)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(page)
        return area

    def _meeting_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(t(
            "A meeting is recorded from two devices at once: your microphone and "
            "whatever comes out of your speakers. Nothing has to guess who was "
            "speaking, because the two never share a channel."
        ))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        sources = QGroupBox(t("Sound"))
        sources_form = QFormLayout(sources)
        self.meeting_mic = QComboBox()
        self.meeting_mic.addItem(t("Same as dictation"), "")
        for name, desc in audio.list_sources():
            self.meeting_mic.addItem(desc, name)
        sources_form.addRow(t("Microphone"), self.meeting_mic)

        self.meeting_system = QComboBox()
        self.meeting_system.addItem(t("Current output"), "")
        for name, desc in audio.list_monitors():
            self.meeting_system.addItem(desc, name)
        sources_form.addRow(t("The other participants"), self.meeting_system)

        if audio.sound() is audio.COREAUDIO:
            mac_note = QLabel(t(
                "macOS does not offer what the speakers are playing as something "
                "to record. Install BlackHole or Loopback, send the meeting's "
                "sound through it, and pick it above."
            ))
            mac_note.setWordWrap(True)
            sources_form.addRow(mac_note)

        note = QLabel(t(
            "Wear headphones if you can. Through speakers your microphone hears "
            "the other side as well, and although a line that lands on both "
            "channels at once is dropped again, the repair is never as clean as "
            "not needing it."
        ))
        note.setWordWrap(True)
        sources_form.addRow(note)
        layout.addWidget(sources)

        people = QGroupBox(t("Who is talking"))
        people_form = QFormLayout(people)
        self.meeting_self_name = QLineEdit()
        self.meeting_self_name.setPlaceholderText(t("Me"))
        people_form.addRow(t("You"), self.meeting_self_name)
        self.meeting_other_name = QLineEdit()
        self.meeting_other_name.setPlaceholderText(t("Other side"))
        people_form.addRow(t("The other end"), self.meeting_other_name)
        self.meeting_participants = QPlainTextEdit()
        self.meeting_participants.setMaximumHeight(70)
        self.meeting_participants.setPlaceholderText(t("One name per line"))
        people_form.addRow(t("Expected"), self.meeting_participants)
        people_note = QLabel(t(
            "Everyone on the far end shares one label: they reach you as a single "
            "mixed signal. The names go to the transcription model so they come "
            "out spelled right, and to the minutes, which may use one for a line "
            "only when the conversation itself makes clear who was speaking."
        ))
        people_note.setWordWrap(True)
        people_form.addRow(people_note)
        layout.addWidget(people)

        models = QGroupBox(t("Minutes"))
        models_form = QFormLayout(models)
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
        models_form.addRow(t("Thinking"), self.meeting_reasoning)
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
        layout.addWidget(models)

        recording = QGroupBox(t("Recording"))
        recording_form = QFormLayout(recording)
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

        self._shortcut_row(
            recording_form, "meeting", t("Shortcut"),
            t("No global shortcut installed. The tray menu starts a meeting too."),
        )
        layout.addWidget(recording)

        prompt_label = QLabel(t("System instruction given to the minutes model."))
        prompt_label.setWordWrap(True)
        layout.addWidget(prompt_label)
        self.meeting_prompt = QPlainTextEdit()
        self.meeting_prompt.setMinimumHeight(200)
        layout.addWidget(self.meeting_prompt, 1)
        reset = QPushButton(t("Reset to default"))
        reset.clicked.connect(
            lambda: self.meeting_prompt.setPlainText(cfg.default_meeting_prompt())
        )
        layout.addWidget(reset, 0, Qt.AlignmentFlag.AlignRight)

        # Everything above is more than one screenful; let it scroll rather than
        # squeezing the prompt box down to nothing.
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(page)
        return area

    def _minutes_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        self.minutes_list = QListWidget()
        self.minutes_list.setWordWrap(True)
        self.minutes_list.setMaximumHeight(170)
        self.minutes_list.currentItemChanged.connect(self._show_minutes)
        layout.addWidget(self.minutes_list)

        self.minutes_status = QLabel("")
        self.minutes_status.setWordWrap(True)
        layout.addWidget(self.minutes_status)

        self.minutes_view = QPlainTextEdit()
        self.minutes_view.setReadOnly(True)
        self.minutes_view.setPlaceholderText(t("Pick a meeting to read it."))
        layout.addWidget(self.minutes_view, 1)

        copy = QPushButton(t("Copy"))
        copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.minutes_view.toPlainText())
        )
        self.minutes_retry = QPushButton(t("Write it up"))
        self.minutes_retry.clicked.connect(self._retry_minutes)
        self.minutes_retry.setEnabled(False)
        folder = QPushButton(t("Open the folder"))
        folder.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(cfg.MEETINGS_DIR))
            )
        )
        delete = QPushButton(t("Delete selected"))
        delete.clicked.connect(self._delete_minutes)
        reload_ = QPushButton(t("Reload"))
        reload_.clicked.connect(self._load_minutes)
        row = QHBoxLayout()
        row.addWidget(copy)
        row.addWidget(self.minutes_retry)
        row.addStretch(1)
        row.addWidget(folder)
        row.addWidget(delete)
        row.addWidget(reload_)
        layout.addLayout(row)
        return page

    def _file_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(t("Transcribe an existing audio or video file with the same models."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        pick = QPushButton(t("Choose file…"))
        pick.clicked.connect(self._choose_file)
        self.file_label = QLabel(t("No file selected"))
        self.file_label.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(pick)
        row.addWidget(self.file_label, 1)
        layout.addLayout(row)

        self.file_timestamps = QCheckBox(t("Add timestamps"))
        self.file_timestamps.setToolTip(
            t("Prefixes every segment with [mm:ss]. Uses whisper-1 on whichever "
              "provider you picked, the only model that returns segment times.")
        )
        layout.addWidget(self.file_timestamps)

        self.file_cleanup = QCheckBox(t("Run the cleanup model afterwards"))
        self.file_cleanup.setToolTip(
            t("With its own rules, under Cleanup rules: written for subtitles, so "
              "the lines keep their place and nothing is shortened.")
        )
        layout.addWidget(self.file_cleanup)

        self.file_run = QPushButton(t("Transcribe"))
        self.file_run.clicked.connect(self._run_file)
        self.file_stop = QPushButton(t("Stop"))
        self.file_stop.clicked.connect(self._stop_file)
        self.file_stop.setEnabled(False)
        run_row = QHBoxLayout()
        run_row.addWidget(self.file_run)
        run_row.addWidget(self.file_stop)
        run_row.addStretch(1)
        layout.addLayout(run_row)

        self.file_status = QLabel("")
        self.file_status.setWordWrap(True)
        layout.addWidget(self.file_status)

        self.file_output = QPlainTextEdit()
        self.file_output.setPlaceholderText("…")
        layout.addWidget(self.file_output, 1)

        copy = QPushButton(t("Copy"))
        copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.file_output.toPlainText())
        )
        save = QPushButton(t("Save as .txt"))
        save.clicked.connect(self._save_transcript)
        self.file_save_srt = QPushButton(t("Save as .srt"))
        self.file_save_srt.setToolTip(
            t("Subtitles, timed from the segments. Needs the timestamps option.")
        )
        self.file_save_srt.setEnabled(False)
        self.file_save_srt.clicked.connect(self._save_subtitles)
        out_row = QHBoxLayout()
        out_row.addWidget(copy)
        out_row.addWidget(save)
        out_row.addWidget(self.file_save_srt)
        out_row.addStretch(1)
        layout.addLayout(out_row)
        return page

    def _shortcut_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        # Both keys in one form, the way the Meeting and Agent tabs already lay
        # theirs out. Two forms would give each row a label column of its own,
        # and two combination boxes starting at different places read as two
        # unrelated settings rather than the pair they are.
        form = QFormLayout()
        self._shortcut_row(
            form, "toggle", t("Start and stop"),
            t("No global shortcut installed."), placeholder="Ctrl+Space",
        )
        # Stopping is what sends the recording off to be transcribed, and that
        # is the step there is no taking back. By the time the tray menu is
        # open the sentence you did not mean to dictate is already on its way.
        self._shortcut_row(
            form, "cancel", t("Discard the recording"),
            t("No global shortcut installed. The tray menu discards it too."),
            tooltip=t("Throws the recording away without transcribing it. Works "
                      "on a dictation and on a command for the agent alike, "
                      "whichever is running."),
        )
        layout.addLayout(form)

        self.evdev_enabled = QCheckBox(t(
            "Use the built-in listener (/dev/input), for when the {desktop} "
            "shortcut is not active yet", desktop=hotkey.desktop_name()
        ))
        self.evdev_enabled.setToolTip(t(
            "Works immediately, no session restart. The only difference: the key "
            "combination also reaches the focused application."
        ))
        layout.addWidget(self.evdev_enabled)
        # Nothing to wait for where nothing is installed: there the listener is
        # the mechanism, always on, and not a choice to offer.
        self.evdev_enabled.setVisible(hotkey.installs_shortcuts())

        if hotkey.shortcut_needs_restart():
            explanation = t(
                "KWin only reads shortcut settings at startup. After 'Install' the "
                "shortcut shows up under System Settings → Shortcuts, but it will "
                "not fire until you log out and back in. Until then, use the "
                "built-in listener."
            )
        elif hotkey.installs_shortcuts():
            explanation = t("The shortcut starts working as soon as it is installed.")
        else:
            explanation = t(
                "Dikte asks macOS for these combinations itself, while it is "
                "running. Nothing is installed, and no other application receives "
                "them in the meantime."
            )
        note = QLabel(explanation)
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _history_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.history = QListWidget()
        self.history.setWordWrap(True)
        self.history.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self._history_menu)
        delete_key = QShortcut(QKeySequence.StandardKey.Delete, self.history)
        delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_key.activated.connect(self._delete_history)
        layout.addWidget(self.history, 1)

        self.history_limit = QSpinBox()
        self.history_limit.setRange(0, 10000)
        self.history_limit.setSpecialValueText(t("no limit"))
        self.history_limit.setSuffix(t(" entries"))
        self.history_limit.setToolTip(t(
            "Once the history passes this many entries, the oldest one is dropped "
            "every time a new one arrives. Set it to 0 to keep everything."
        ))
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel(t("Keep at most")))
        limit_row.addWidget(self.history_limit)
        limit_row.addStretch(1)
        layout.addLayout(limit_row)

        copy = QPushButton(t("Copy selected to clipboard"))
        copy.clicked.connect(self._copy_history)
        delete = QPushButton(t("Delete selected"))
        delete.clicked.connect(self._delete_history)
        clear = QPushButton(t("Clear history"))
        clear.clicked.connect(self._clear_history)
        reload_ = QPushButton(t("Reload"))
        reload_.clicked.connect(self._load_history)
        row = QHBoxLayout()
        row.addWidget(copy)
        row.addWidget(delete)
        row.addStretch(1)
        row.addWidget(clear)
        row.addWidget(reload_)
        layout.addLayout(row)
        return page

    @staticmethod
    def _prompt_page(tabs, title, intro, default):
        """A tab holding one editable prompt, and returns its box."""
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel(intro)
        label.setWordWrap(True)
        layout.addWidget(label)
        box = QPlainTextEdit()
        layout.addWidget(box, 1)
        reset = QPushButton(t("Reset to default"))
        reset.clicked.connect(lambda: box.setPlainText(default()))
        layout.addWidget(reset, 0, Qt.AlignmentFlag.AlignRight)
        tabs.addTab(page, title)
        return box

    @staticmethod
    def _shortcut_box(placeholder=""):
        """The field a global shortcut is typed or picked in."""
        box = QComboBox()
        box.setEditable(True)
        box.addItems(MAC_SHORTCUTS if hotkey.desktop_name() == "macOS"
                     else SHORTCUTS)
        box.setCurrentText("")
        if placeholder:
            box.lineEdit().setPlaceholderText(placeholder)
        return box

    def _key_row(self, form, provider, placeholder, tester):
        """A key field, its Test button and the line the answer lands on.

        The field and the pair the answer needs are filed under the provider's
        name, so saving, loading and the test handler find them by name rather
        than through three attributes each.
        """
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText(placeholder)
        button = QPushButton(t("Test"))
        button.clicked.connect(tester)
        answer = QLabel("")
        answer.setWordWrap(True)
        form.addRow(cfg.TRANSCRIBERS[provider].service, self._row(field, button))
        form.addRow("", answer)
        self._key_fields[provider] = field
        self._testers[provider] = (button, answer)
        return field

    def _shortcut_row(self, form, which, label, missing, placeholder="",
                      tooltip=""):
        """One global shortcut: the combination, Install, Remove, and a line
        saying what the desktop has registered. `missing` is what that line
        says when nothing is."""
        box = self._shortcut_box(placeholder or t("none"))
        if tooltip:
            box.setToolTip(tooltip)
        form.addRow(label, self._row(box, *self._install_buttons(
            lambda: self._install_shortcut(which),
            lambda: self._remove_shortcut(which),
        )))
        status = QLabel("")
        status.setWordWrap(True)
        form.addRow(status)
        self._shortcut_rows[which] = (box, status, missing)
        return box

    @staticmethod
    def _install_buttons(install_handler, remove_handler):
        """Install and Remove, where this system has somewhere to install into.

        macOS has not: Dikte asks for the combination itself while it runs, so
        there is nothing to write down and nothing to take back out.
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
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # ---- load / save ----------------------------------------------------

    def _load(self):
        conf = self.conf
        self._select_data(self.ui_language, conf["ui_language"])
        self._select_data(self.mic, conf["mic_target"])
        self._select_data(self.language, conf["language"])
        self.auto_paste.setChecked(conf["auto_paste"])
        self.paste_shortcut.setCurrentText(conf["paste_shortcut"])
        self.restore_clipboard.setChecked(conf["restore_clipboard"])
        self._select_data(self.corner, conf["overlay_corner"])
        self.max_seconds.setValue(conf["max_seconds"])
        self.skip_silent.setChecked(conf["skip_silent"])
        self.silence_db.setValue(int(conf["silence_db"]))
        self.filter_hallucinations.setChecked(conf["filter_hallucinations"])
        self.keep_audio.setChecked(conf["keep_audio"])

        for name, who in cfg.TRANSCRIBERS.items():
            self._key_fields[name].setText(conf[who.key])
            self._models[name] = conf[who.model]
        self._shown_provider = ""
        self._select_data(self.transcribe_provider, conf["transcribe_provider"])
        self._provider_changed()  # selecting index 0 fires no signal
        self.local_gpu.setChecked(conf["local_gpu"])
        self.local_preload.setChecked(conf["local_preload"])
        self.local_threads.setValue(int(conf["local_threads"]))
        self.local_whisper.load(conf["local_model"])

        self.cleanup_enabled.setChecked(conf["cleanup_enabled"])
        self.cleanup_model.setCurrentText(conf["cleanup_model"])
        self.cleanup_claude_model.setCurrentText(conf["cleanup_claude_model"])
        self.cleanup_codex_model.setCurrentText(
            conf["cleanup_codex_model"] or t("Codex's own default")
        )
        self._select_data(self.cleanup_provider, conf["cleanup_provider"])
        self._cleanup_provider_changed()  # selecting index 0 fires no signal
        self._select_data(self.cleanup_reasoning, conf["cleanup_reasoning"])
        self.local_llm_gpu.setChecked(conf["local_llm_gpu"])
        self.local_llm_preload.setChecked(conf["local_llm_preload"])
        self._select_data(self.local_llm_reasoning, conf["local_llm_reasoning"])
        self.local_llm.load(conf["local_llm_model"], conf["local_llm_repo"])
        self.cleanup_prompt.setPlainText(conf["cleanup_prompt"] or cfg.default_cleanup_prompt())
        self.file_cleanup_prompt.setPlainText(
            conf["file_cleanup_prompt"] or cfg.default_file_cleanup_prompt()
        )
        self.transcribe_prompt.setPlainText(conf["transcribe_prompt"])

        self._select_data(self.assistant_provider, conf["assistant_provider"])
        self.assistant_model.setCurrentText(conf["assistant_model"])
        self._select_data(self.assistant_permission, conf["assistant_permission_mode"])
        self.assistant_codex_model.setCurrentText(conf["assistant_codex_model"])
        self._select_data(self.assistant_codex_sandbox, conf["assistant_codex_sandbox"])
        self.assistant_openrouter_model.setCurrentText(conf["assistant_openrouter_model"])
        self._assistant_provider_changed()  # selecting index 0 fires no signal
        self._select_data(self.assistant_reasoning, conf["assistant_reasoning"])
        self.assistant_dir.setText(conf["assistant_dir"])
        self.assistant_timeout.setValue(int(conf["assistant_timeout"]))
        self.assistant_session_minutes.setValue(int(conf["assistant_session_minutes"]))
        self.assistant_paste.setChecked(conf["assistant_paste"])
        self.assistant_cleanup.setChecked(conf["assistant_cleanup"])
        self.assistant_prompt.setPlainText(
            conf["assistant_prompt"] or cfg.default_assistant_prompt()
        )

        self._select_data(self.meeting_mic, conf["meeting_mic_target"])
        self._select_data(self.meeting_system, conf["meeting_system_target"])
        self.meeting_self_name.setText(conf["meeting_self_name"])
        self.meeting_other_name.setText(conf["meeting_other_name"])
        self.meeting_participants.setPlainText(conf["meeting_participants"])
        self.meeting_model.setCurrentText(conf["meeting_model"])
        self._select_data(self.meeting_reasoning, conf["meeting_reasoning"])
        self._select_data(self.meeting_language, conf["meeting_language"])
        self.meeting_cleanup.setChecked(conf["meeting_cleanup"])
        self.meeting_max_minutes.setValue(max(5, int(conf["meeting_max_seconds"]) // 60))
        self.meeting_keep_audio.setChecked(conf["meeting_keep_audio"])
        self.meeting_prompt.setPlainText(
            conf["meeting_prompt"] or cfg.default_meeting_prompt()
        )

        self.file_timestamps.setChecked(conf["file_timestamps"])
        self.file_cleanup.setChecked(conf["file_cleanup"])
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

    def _save(self):
        conf = self.conf
        conf["ui_language"] = self.ui_language.currentData() or "auto"
        conf["mic_target"] = self.mic.currentData() or ""
        conf["language"] = self.language.currentData() or "auto"
        conf["auto_paste"] = self.auto_paste.isChecked()
        conf["paste_shortcut"] = self.paste_shortcut.currentText().strip()
        conf["restore_clipboard"] = self.restore_clipboard.isChecked()
        conf["overlay_corner"] = self.corner.currentData() or "bottom-left"
        conf["max_seconds"] = self.max_seconds.value()
        conf["skip_silent"] = self.skip_silent.isChecked()
        conf["silence_db"] = float(self.silence_db.value())
        conf["filter_hallucinations"] = self.filter_hallucinations.isChecked()
        conf["keep_audio"] = self.keep_audio.isChecked()

        provider = self.transcribe_provider.currentData() or "local"
        if provider in self._models:
            self._models[provider] = self.transcribe_model.currentText().strip()
        conf["transcribe_provider"] = provider
        for name, who in cfg.TRANSCRIBERS.items():
            conf[who.key] = self._key_fields[name].text().strip()
            conf[who.model] = self._models[name].strip() or cfg.DEFAULTS[who.model]
        conf["local_model"] = self.local_whisper.selected()
        conf["local_gpu"] = self.local_gpu.isChecked()
        conf["local_preload"] = self.local_preload.isChecked()
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
        conf["cleanup_reasoning"] = self.cleanup_reasoning.currentData() or ""
        conf["local_llm_model"] = self.local_llm.selected()
        conf["local_llm_repo"] = self.local_llm.repository()
        conf["local_llm_gpu"] = self.local_llm_gpu.isChecked()
        conf["local_llm_preload"] = self.local_llm_preload.isChecked()
        conf["local_llm_reasoning"] = self.local_llm_reasoning.currentData() or ""

        # Store an empty prompt when it matches the default, so switching the
        # interface language also switches the prompt language.
        prompt = self.cleanup_prompt.toPlainText().strip()
        conf["cleanup_prompt"] = "" if prompt == cfg.default_cleanup_prompt() else prompt
        file_prompt = self.file_cleanup_prompt.toPlainText().strip()
        conf["file_cleanup_prompt"] = ("" if file_prompt == cfg.default_file_cleanup_prompt()
                                       else file_prompt)
        conf["transcribe_prompt"] = self.transcribe_prompt.toPlainText().strip()

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
        conf["assistant_reasoning"] = self.assistant_reasoning.currentData() or ""
        conf["assistant_dir"] = self.assistant_dir.text().strip()
        conf["assistant_timeout"] = self.assistant_timeout.value()
        conf["assistant_session_minutes"] = self.assistant_session_minutes.value()
        conf["assistant_paste"] = self.assistant_paste.isChecked()
        conf["assistant_cleanup"] = self.assistant_cleanup.isChecked()
        assistant_prompt = self.assistant_prompt.toPlainText().strip()
        conf["assistant_prompt"] = ("" if assistant_prompt == cfg.default_assistant_prompt()
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
        conf["meeting_max_seconds"] = self.meeting_max_minutes.value() * 60
        conf["meeting_keep_audio"] = self.meeting_keep_audio.isChecked()
        meeting_prompt = self.meeting_prompt.toPlainText().strip()
        conf["meeting_prompt"] = ("" if meeting_prompt == cfg.default_meeting_prompt()
                                  else meeting_prompt)

        conf["file_timestamps"] = self.file_timestamps.isChecked()
        conf["file_cleanup"] = self.file_cleanup.isChecked()

        # Left empty, only the toggle falls back to a default: the application
        # is unusable without it. The other three stay empty, which is what
        # turns them off.
        for which, (box, _status, _missing) in self._shortcut_rows.items():
            spec = hotkey.SHORTCUTS[which]
            conf[spec.setting] = (box.currentText().strip()
                                  or hotkey.default_combo(which))
        conf["evdev_hotkey"] = self.evdev_enabled.isChecked()
        conf["history_limit"] = self.history_limit.value()
        conf.save()
        # A lowered limit should bite now, not on the next dictation.
        try:
            cfg.trim_history(conf["history_limit"])
        except OSError as exc:
            print(f"dikte: could not trim the history ({exc})")
        self._load_history()  # the trim may just have dropped rows from the list
        self.applied.emit()
        QMessageBox.information(self, t("Dikte Settings"), t("Saved successfully."))

    @staticmethod
    def _select_data(combo, value):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    # ---- api helpers -----------------------------------------------------

    def _provider_changed(self):
        """Swap the model box over to the newly chosen provider's own model."""
        if self._shown_provider in TRANSCRIBE_MODELS:
            self._models[self._shown_provider] = self.transcribe_model.currentText().strip()
        provider = self.transcribe_provider.currentData() or "local"
        self._shown_provider = provider
        local = provider == "local"
        self.stt_form.setRowVisible(self.transcribe_model_row, not local)
        self.stt_form.setRowVisible(self.transcribe_status, not local)
        self.stt_form.setRowVisible(self.local_whisper, local)
        self.stt_form.setRowVisible(self.local_options, local)
        if local:
            return
        self.transcribe_model.clear()
        self.transcribe_model.addItems(TRANSCRIBE_MODELS[provider])
        self.transcribe_model.setCurrentText(self._models[provider])
        self.transcribe_status.setText("")

    def _load_transcribe_models(self):
        """The model list of whichever provider is selected."""
        provider = self.transcribe_provider.currentData() or "openai"
        self.refresh_transcribe_models.setEnabled(False)
        self.transcribe_status.setText(t("Fetching model list…"))
        key, base = self._typed_key(provider)
        service = cfg.TRANSCRIBERS[provider].service

        def work():
            try:
                models = (api.openrouter_models(key, transcription=True)
                          if provider == "openrouter"
                          else api.openai_models(key, base, service))
                self._transcribe_models_loaded.emit(models, "")
            except api.ApiError as exc:
                self._transcribe_models_loaded.emit([], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_transcribe_models_loaded(self, models, error):
        self.refresh_transcribe_models.setEnabled(True)
        if error:
            self.transcribe_status.setText(t("Could not fetch the list: {error}", error=error))
            return
        current = self.transcribe_model.currentText()
        self.transcribe_model.clear()
        self.transcribe_model.addItems(models)
        self.transcribe_model.setCurrentText(current)
        self.transcribe_status.setText(t("{count} models loaded.", count=len(models)))

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

    def _test_openai(self):
        key, base = self._typed_key("openai")
        self._test_key("openai", lambda: t(
            "Connection works. {count} audio models visible.",
            count=len(api.openai_models(key, base)),
        ))

    def _test_groq(self):
        key, base = self._typed_key("groq")
        self._test_key("groq", lambda: t(
            "Connection works. {count} audio models visible.",
            count=len(api.openai_models(key, base, cfg.TRANSCRIBERS["groq"].service)),
        ))

    def _test_openrouter(self):
        key, _ = self._typed_key("openrouter")
        self._test_key("openrouter", lambda: api.openrouter_key_status(key))

    def _typed_key(self, provider):
        """(key, base URL) for a provider, preferring what is in the field now."""
        who = cfg.TRANSCRIBERS[provider]
        typed = self._key_fields[provider].text().strip()
        return typed or self.conf.api_key(who.key), self.conf[who.url]

    def _test_key(self, provider, ask):
        """Run `ask` off the interface thread and write its answer under the key.

        `ask` returns the line to show, or raises ApiError with the line to show
        instead; either way it is read from a field before the thread starts.
        """
        button, answer = self._testers[provider]
        button.setEnabled(False)
        answer.setText(t("Trying…"))

        def work():
            try:
                self._test_done.emit(provider, True, ask())
            except api.ApiError as exc:
                self._test_done.emit(provider, False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, provider, ok, message):
        button, answer = self._testers[provider]
        button.setEnabled(True)
        answer.setText(("✓ " if ok else "✗ ") + message)

    # ---- audio file ------------------------------------------------------

    def _choose_file(self):
        start = self.conf["file_last_dir"] or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, t("Select an audio file"), start,
            f"{t('Audio and video files')} ({AUDIO_FILTER});;{t('All files')} (*)",
        )
        if not path:
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
        self.conf["file_timestamps"] = self.file_timestamps.isChecked()
        self.conf["file_cleanup"] = self.file_cleanup.isChecked()
        self.conf.save()

    def _run_file(self):
        if not getattr(self, "file_path", "") or self.transcriber.busy:
            return
        self.file_output.clear()
        self.file_segments = []
        self.file_save_srt.setEnabled(False)
        self.file_run.setEnabled(False)
        self.file_stop.setEnabled(True)
        self.transcriber.start(
            self.file_path,
            self.file_timestamps.isChecked(),
            self.file_cleanup.isChecked(),
        )

    def _stop_file(self):
        # The button goes dead here rather than when the run comes back, so a
        # second press cannot land while the first one is still travelling.
        self.file_stop.setEnabled(False)
        self.file_status.setText(t("Stopping…"))
        self.transcriber.stop()

    def _on_file_progress(self, message):
        self.file_status.setText(message)
        if message == t("Stopped."):
            self._file_idle()

    def _on_file_finished(self, text, segments):
        self.file_output.setPlainText(text)
        self.file_segments = segments
        self.file_save_srt.setEnabled(bool(segments))
        self.file_status.setText(t("Done: {chars} characters.", chars=len(text)))
        self._file_idle()

    def _on_file_failed(self, error):
        self.file_status.setText(t("Failed: {error}", error=error))
        self._file_idle()

    def _file_idle(self):
        self.file_run.setEnabled(True)
        self.file_stop.setEnabled(False)

    def _save_transcript(self):
        self._write_transcript(self.file_output.toPlainText(), ".txt",
                               f"{t('Text files')} (*.txt)")

    def _save_subtitles(self):
        srt = filetranscribe.to_srt(self.file_output.toPlainText(),
                                    getattr(self, "file_segments", []))
        if not srt:
            self.file_status.setText(t("No timestamped lines to turn into subtitles."))
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
            self.file_status.setText(t("Saved: {path}", path=path))
        except OSError as exc:
            self.file_status.setText(t("Failed: {error}", error=exc))

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
            self.conf.save()
        self._refresh_shortcut_status(which)

    def _remove_shortcut(self, which):
        hotkey.remove_shortcut(hotkey.SHORTCUTS[which].desktop_id)
        self._refresh_shortcut_status(which)

    def _refresh_shortcut_status(self, which):
        _box, status, missing = self._shortcut_rows[which]
        current = hotkey.shortcut_status(hotkey.SHORTCUTS[which].desktop_id)
        status.setText(
            t("Registered in {desktop}: {shortcut}",
              desktop=hotkey.desktop_name(), shortcut=current) if current
            else missing
        )

    def _cleanup_provider_changed(self):
        provider = self.cleanup_provider.currentData() or "openrouter"
        self.cleanup_form.setRowVisible(self.cleanup_model_row,
                                        provider == "openrouter")
        self.cleanup_form.setRowVisible(self.cleanup_claude_model,
                                        provider == "claude")
        self.cleanup_form.setRowVisible(self.cleanup_codex_model,
                                        provider == "codex")
        self.cleanup_form.setRowVisible(self.cleanup_reasoning,
                                        provider != "local")
        self.cleanup_form.setRowVisible(self.local_llm, provider == "local")
        self.cleanup_form.setRowVisible(self.local_llm_options, provider == "local")
        binary = cleanup.executable(provider)
        found = shutil.which(binary) if binary else ""
        if provider == "local":
            self.models_label.setText(t("Runs on this machine, on llama.cpp."))
        elif not binary:
            self.models_label.setText(t("Runs on OpenRouter."))
        elif found:
            self.models_label.setText(t("Found: {path}", path=found))
        else:
            self.models_label.setText(t(
                "{binary} is not on your PATH, so cleanup would fail and the raw "
                "transcript would be pasted. Install it, or pick another one "
                "above.", binary=binary,
            ))

    def _assistant_provider_changed(self):
        provider = self.assistant_provider.currentData() or "claude"
        self.claude_box.setVisible(provider == "claude")
        self.codex_box.setVisible(provider == "codex")
        self.openrouter_box.setVisible(provider == "openrouter")
        self._refresh_assistant_status()

    def _refresh_assistant_status(self):
        provider = self.assistant_provider.currentData() or "claude"
        binary = assistant.executable(provider)
        found = shutil.which(binary) if binary else ""
        if not binary:
            self.assistant_found.setText(
                t("Needs no program installed, only the OpenRouter key.")
            )
        elif found:
            self.assistant_found.setText(t("Found: {path}", path=found))
        else:
            self.assistant_found.setText(t(
                "{binary} is not on your PATH, so this cannot run yet. Install "
                "it, or pick another one above.", binary=binary,
            ))
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

    # ---- minutes ---------------------------------------------------------

    def _load_minutes(self):
        self.minutes_list.clear()
        for row in reversed(cfg.read_meetings()):
            title = row.get("title") or t("Meeting")
            head = f"{row.get('ts', '')}  ·  {meeting.length_label(row.get('duration', 0))}"
            state = MEETING_STATUS.get(row.get("status", ""), "")
            if state:
                head += "  ·  " + t(state)
            item = QListWidgetItem(f"{head}\n{title}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.minutes_list.addItem(item)
        if not self.minutes_list.count():
            self.minutes_view.clear()
            self.minutes_retry.setEnabled(False)

    def _selected_meeting(self):
        item = self.minutes_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _show_minutes(self, *_):
        row = self._selected_meeting()
        if not row:
            self.minutes_view.clear()
            self.minutes_retry.setEnabled(False)
            return
        doc_path, _wav = cfg.meeting_paths(row["base"])
        try:
            self.minutes_view.setPlainText(doc_path.read_text(encoding="utf-8"))
        except OSError:
            self.minutes_view.setPlainText(
                row.get("error") or t("Nothing has been written yet.")
            )
        busy = self.meetings is not None and self.meetings.busy
        self.minutes_retry.setEnabled(
            self.meetings is not None and not busy and row.get("status") != "done"
        )

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
        self.history.clear()
        for row in reversed(cfg.read_history(self.conf["history_limit"])):
            text = (row.get("text") or "").replace("\n", " ")
            preview = text[:110] + ("…" if len(text) > 110 else "")
            header = t("{ts}  ({duration} s)",
                       ts=row.get("ts", ""), duration=row.get("duration", 0))
            if row.get("mode") == "ask":
                # The text of an answer says nothing about what was asked, and
                # out of that context half of them read like non sequiturs.
                asked = (row.get("question") or row.get("raw") or "").replace("\n", " ")
                header += t("  ·  asked Claude: {question}",
                            question=asked[:60] + ("…" if len(asked) > 60 else ""))
            item = QListWidgetItem(f"{header}\n{preview}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.history.addItem(item)

    def _selected_rows(self):
        """Selected entries, newest first, the order they are listed in."""
        items = sorted(self.history.selectedItems(), key=self.history.row)
        return [item.data(Qt.ItemDataRole.UserRole) for item in items]

    def _copy_history(self):
        rows = self._selected_rows()
        if rows:
            QGuiApplication.clipboard().setText(
                "\n\n".join(row.get("text", "") for row in rows)
            )

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

    def _history_menu(self, pos):
        item = self.history.itemAt(pos)
        if item is not None and not item.isSelected():
            self.history.setCurrentItem(item)
        menu = QMenu(self)
        copy = menu.addAction(t("Copy selected to clipboard"))
        delete = menu.addAction(t("Delete selected"))
        menu.addSeparator()
        clear = menu.addAction(t("Clear history"))
        has_selection = bool(self.history.selectedItems())
        copy.setEnabled(has_selection)
        delete.setEnabled(has_selection)
        clear.setEnabled(self.history.count() > 0)
        chosen = menu.exec(self.history.viewport().mapToGlobal(pos))
        if chosen is copy:
            self._copy_history()
        elif chosen is delete:
            self._delete_history()
        elif chosen is clear:
            self._clear_history()
