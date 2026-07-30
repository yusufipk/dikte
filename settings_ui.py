"""Settings window."""

import os
import shutil
import sys
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
import config as cfg
import filetranscribe
import hotkey
import local_whisper
import meeting
from app_version import APP_VERSION
from filetranscribe import FileTranscriber
from i18n import t

IS_MACOS = sys.platform == "darwin"
UI_LANGUAGES = [("Automatic (system)", "auto"), ("Turkish", "tr"), ("English", "en")]
MENUBAR_ICONS = [
    ("System microphone (monochrome)", cfg.SYSTEM_MENUBAR_ICON),
    ("Analog clock (current time)", cfg.ANALOG_CLOCK_MENUBAR_ICON),
    *[(emoji, emoji) for emoji in
      ["🎙️", "✍️", "🗣️", "🎤", "📝", "🪄", "🧠", "💬"]],
]
LANGUAGES = [
    ("Detect automatically", "auto"), ("Turkish", "tr"), ("English", "en"),
    ("German", "de"), ("French", "fr"), ("Spanish", "es"), ("Arabic", "ar"),
]
CORNERS = ["bottom-left", "bottom-right", "top-left", "top-right"]
TRANSCRIBE_PROVIDERS = [
    ("Local Whisper — no API key", "local"),
    ("OpenAI", "openai"),
    ("OpenRouter", "openrouter"),
]
# Starting points for the model box; "Fetch model list" replaces them with
# whatever the provider offers today.
TRANSCRIBE_MODELS = {
    "local": [str(local_whisper.default_model_path())],
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
CLEANUP_PROVIDERS = [
    ("OpenRouter", "openrouter"),
    ("Codex CLI — no API key", "codex"),
]
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
PASTE_SHORTCUTS = (["cmd+v"] if IS_MACOS
                   else ["ctrl+v", "ctrl+shift+v", "shift+insert"])
# Offered for all three global shortcuts, which keeps them one kind of field
# rather than three. The boxes stay editable: this is a shortlist of
# combinations that are usually free, not the set of ones that work.
SHORTCUTS = (
    [
        "Ctrl+Space", "Ctrl+Option+Space", "Ctrl+Shift+Space",
        "Cmd+Option+Space", "Cmd+Shift+Space",
        "Ctrl+Option+A", "Ctrl+Option+D", "Ctrl+Option+M",
        "Cmd+Option+A", "Cmd+Option+D", "Cmd+Option+M",
    ]
    if IS_MACOS else
    [
        "Ctrl+Space", "Ctrl+Alt+Space", "Ctrl+Shift+Space", "Meta+Space",
        "Ctrl+Alt+A", "Ctrl+Alt+D", "Ctrl+Alt+M", "Ctrl+Alt+Q",
        "Meta+A", "Meta+D", "Meta+M",
        "Ctrl+Alt+F1", "Ctrl+Alt+F2", "Ctrl+Alt+F3",
    ]
)
AUDIO_FILTER = ("*.mp3 *.wav *.m4a *.ogg *.opus *.flac *.aac *.wma "
                "*.mp4 *.mkv *.webm *.mov *.avi")


class SettingsWindow(QDialog):
    applied = pyqtSignal()

    _models_loaded = pyqtSignal(list, str)
    _transcribe_models_loaded = pyqtSignal(list, str)
    _test_done = pyqtSignal(bool, str)
    _or_test_done = pyqtSignal(bool, str)
    _local_install_progress = pyqtSignal(int, int)
    _local_install_done = pyqtSignal(bool, str, str)

    def __init__(self, conf, launch_command, meeting_command=None,
                 meetings=None, ask_command=None, update_manager=None,
                 update_check=None, parent=None):
        super().__init__(parent)
        self.conf = conf
        self.launch_command = launch_command
        self.meeting_command = meeting_command or launch_command
        self.ask_command = ask_command or launch_command
        self.meetings = meetings
        self.update_manager = update_manager
        self.update_check = update_check
        # Each provider keeps its own transcription model, so switching the
        # provider back and forth never overwrites the other one's.
        self._models = {"local": "", "openai": "", "openrouter": ""}
        self._shown_provider = ""
        self.transcriber = FileTranscriber(conf, self)
        self.setWindowTitle(t("Dikte Settings"))
        self.resize(680, 640)

        tabs = QTabWidget(self)
        tabs.addTab(self._general_tab(), t("General"))
        tabs.addTab(self._api_tab(), t("API and models"))
        tabs.addTab(self._prompt_tab(), t("Cleanup rules"))
        tabs.addTab(self._assistant_tab(), t("Agent"))
        tabs.addTab(self._meeting_tab(), t("Meeting"))
        tabs.addTab(self._minutes_tab(), t("Minutes"))
        tabs.addTab(self._file_tab(), t("Audio file"))
        tabs.addTab(self._shortcut_tab(), t("Shortcut"))
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
        self._or_test_done.connect(self._on_or_test_done)
        self._local_install_progress.connect(self._on_local_install_progress)
        self._local_install_done.connect(self._on_local_install_done)
        if self.update_manager is not None:
            self.update_manager.status_changed.connect(self._on_update_status)
        self.transcriber.progress.connect(self._on_file_progress)
        self.transcriber.finished.connect(self._on_file_finished)
        self.transcriber.failed.connect(self._on_file_failed)
        if self.meetings is not None:
            self.meetings.progress.connect(self._on_minutes_progress)
            self.meetings.finished.connect(self._on_minutes_finished)
            self.meetings.failed.connect(self._on_minutes_failed)
        self._load()

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

        self.menubar_emoji = QComboBox()
        self.menubar_emoji.setEditable(True)
        for label, value in MENUBAR_ICONS:
            self.menubar_emoji.addItem(t(label), value)
        self.menubar_emoji.setToolTip(t(
            "Choose the system icon, an emoji, or type any emoji. "
            "It changes as soon as Settings is saved."
        ))
        if IS_MACOS:
            form.addRow(t("Menu bar emoji"), self.menubar_emoji)

        self.launch_at_login = QCheckBox(t("Start Dikte when I log in"))
        if IS_MACOS:
            form.addRow("", self.launch_at_login)

        self.auto_update = QCheckBox(t("Automatically install macOS updates"))
        if IS_MACOS:
            form.addRow("", self.auto_update)

        self.update_status = QLabel(
            self.update_manager.status if self.update_manager is not None
            else t("Current version: v{version}", version=APP_VERSION)
        )
        self.update_status.setWordWrap(True)
        self.check_update = QPushButton(t("Check for updates now"))
        self.check_update.clicked.connect(self._check_for_updates)
        update_row = QHBoxLayout()
        update_row.addWidget(self.update_status, 1)
        update_row.addWidget(self.check_update)
        if IS_MACOS:
            form.addRow(t("Updates"), update_row)

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
        self.paste_shortcut.addItems(PASTE_SHORTCUTS)
        self.paste_shortcut.setToolTip(t(
            "macOS needs Accessibility permission to send Cmd+V."
            if IS_MACOS else
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

        self.keep_audio = QCheckBox(t(
            "Keep audio files ({path})", path=str(cfg.RECORDINGS_DIR)
        ))
        form.addRow("", self.keep_audio)
        return page

    def _api_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)

        # Keys first, then the two jobs, because OpenRouter can now do both of
        # them and a key no longer belongs to a single job.
        keys = QGroupBox(t("Keys"))
        keys_form = QFormLayout(keys)
        self.openai_key = QLineEdit()
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setPlaceholderText(t("sk-… (falls back to OPENAI_API_KEY)"))
        self.test_button = QPushButton(t("Test"))
        self.test_button.clicked.connect(self._test_openai)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)
        keys_form.addRow("OpenAI", self._row(self.openai_key, self.test_button))
        keys_form.addRow("", self.test_label)

        self.openrouter_key = QLineEdit()
        self.openrouter_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openrouter_key.setPlaceholderText(t("sk-or-… (falls back to OPENROUTER_API_KEY)"))
        self.or_test_button = QPushButton(t("Test"))
        self.or_test_button.clicked.connect(self._test_openrouter)
        self.or_test_label = QLabel("")
        self.or_test_label.setWordWrap(True)
        keys_form.addRow("OpenRouter", self._row(self.openrouter_key, self.or_test_button))
        keys_form.addRow("", self.or_test_label)
        outer.addWidget(keys)

        stt = QGroupBox(t("Speech to text"))
        stt_form = QFormLayout(stt)
        self.transcribe_provider = QComboBox()
        for label, value in TRANSCRIBE_PROVIDERS:
            self.transcribe_provider.addItem(t(label), value)
        stt_form.addRow(t("Provider"), self.transcribe_provider)

        self.transcribe_model = QComboBox()
        self.transcribe_model.setEditable(True)
        self.refresh_transcribe_models = QPushButton(t("Fetch model list"))
        self.refresh_transcribe_models.clicked.connect(self._load_transcribe_models)
        stt_form.addRow(t("Model"),
                        self._row(self.transcribe_model, self.refresh_transcribe_models))
        # A spanning row: in the narrow field column a wrapped label gets a
        # height that fits one line, and the rest of the text is cut off.
        self.transcribe_status = QLabel("")
        self.transcribe_status.setWordWrap(True)
        stt_form.addRow(self.transcribe_status)
        self.transcribe_provider.currentIndexChanged.connect(self._provider_changed)
        outer.addWidget(stt)

        orr = QGroupBox(t("Transcript cleanup"))
        orr_form = QFormLayout(orr)
        self.cleanup_enabled = QCheckBox(t("Clean the transcript with a model"))
        orr_form.addRow("", self.cleanup_enabled)

        self.cleanup_provider = QComboBox()
        for label, value in CLEANUP_PROVIDERS:
            self.cleanup_provider.addItem(t(label), value)
        self.cleanup_provider.currentIndexChanged.connect(
            self._cleanup_provider_changed
        )
        orr_form.addRow(t("Provider"), self.cleanup_provider)

        self.cleanup_model = QComboBox()
        self.cleanup_model.setEditable(True)
        self.cleanup_model.addItems(CLEANUP_MODELS)
        self.refresh_models = QPushButton(t("Fetch model list"))
        self.refresh_models.clicked.connect(self._load_models)
        self.cleanup_openrouter_label = QLabel(t("OpenRouter model"))
        self.cleanup_openrouter_row = self._row(
            self.cleanup_model, self.refresh_models
        )
        orr_form.addRow(
            self.cleanup_openrouter_label, self.cleanup_openrouter_row
        )

        self.cleanup_codex_model = QComboBox()
        self.cleanup_codex_model.setEditable(True)
        self.cleanup_codex_model.addItem(t("Codex's own default"), "")
        for name in CODEX_MODELS:
            self.cleanup_codex_model.addItem(name, name)
        self.cleanup_codex_label = QLabel(t("Codex model"))
        orr_form.addRow(self.cleanup_codex_label, self.cleanup_codex_model)

        self.cleanup_reasoning = QComboBox()
        for label, value in REASONING_LEVELS:
            self.cleanup_reasoning.addItem(t(label), value)
        self.cleanup_reasoning.setToolTip(
            t("How long a thinking model may reason before it answers. Cleanup is "
              "a light job, so more thinking mostly costs time and tokens. Models "
              "that cannot think ignore this.")
        )
        orr_form.addRow(t("Thinking"), self.cleanup_reasoning)

        self.models_label = QLabel("")
        self.models_label.setWordWrap(True)
        orr_form.addRow(self.models_label)
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
        self.assistant_shortcut = self._shortcut_box(t("none"))
        install = QPushButton(t(
            "Activate macOS shortcut" if IS_MACOS else "Install as a KDE shortcut"
        ))
        install.clicked.connect(self._install_ask_shortcut)
        remove = QPushButton(t("Remove"))
        remove.clicked.connect(self._remove_ask_shortcut)
        how_form.addRow(t("Shortcut"),
                        self._row(self.assistant_shortcut, install, remove))
        self.assistant_shortcut_status = QLabel("")
        self.assistant_shortcut_status.setWordWrap(True)
        how_form.addRow(self.assistant_shortcut_status)

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

        if IS_MACOS:
            mac_note = QLabel(t(
                "macOS meeting capture needs a virtual audio input such as "
                "BlackHole or Loopback. Select it here after installation."
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

        self.meeting_shortcut = self._shortcut_box(t("none"))
        install = QPushButton(t(
            "Activate macOS shortcut" if IS_MACOS else "Install as a KDE shortcut"
        ))
        install.clicked.connect(self._install_meeting_shortcut)
        remove = QPushButton(t("Remove"))
        remove.clicked.connect(self._remove_meeting_shortcut)
        recording_form.addRow(t("Shortcut"),
                              self._row(self.meeting_shortcut, install, remove))
        self.meeting_shortcut_status = QLabel("")
        self.meeting_shortcut_status.setWordWrap(True)
        recording_form.addRow(self.meeting_shortcut_status)
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
        self.file_stop.clicked.connect(self.transcriber.stop)
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
        form = QFormLayout()
        self.shortcut = self._shortcut_box("Ctrl+Space")
        form.addRow(t("Shortcut"), self.shortcut)
        layout.addLayout(form)

        install = QPushButton(t(
            "Activate macOS shortcut" if IS_MACOS else "Install as a KDE shortcut"
        ))
        install.clicked.connect(self._install_shortcut)
        remove = QPushButton(t("Remove"))
        remove.clicked.connect(self._remove_shortcut)
        row = QHBoxLayout()
        row.addWidget(install)
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)

        self.shortcut_status = QLabel("")
        self.shortcut_status.setWordWrap(True)
        layout.addWidget(self.shortcut_status)

        self.evdev_enabled = QCheckBox(t(
            "Enable global shortcuts on this Mac" if IS_MACOS else
            "Use the built-in listener (/dev/input), for when the KDE shortcut is "
            "not active yet"
        ))
        self.evdev_enabled.setToolTip(t(
            "Uses macOS's native global hotkey service."
            if IS_MACOS else
            "Works immediately, no session restart. The only difference: the key "
            "combination also reaches the focused application."
        ))
        layout.addWidget(self.evdev_enabled)

        note = QLabel(t(
            "Automatic paste asks for Accessibility permission the first time. "
            "Enable Dikte under System Settings → Privacy & Security → Accessibility."
            if IS_MACOS else
            "KWin only reads shortcut settings at startup. After 'Install' the "
            "shortcut shows up under System Settings → Shortcuts, but it will not "
            "fire until you log out and back in. Until then, use the built-in listener."
        ))
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
        box.addItems(SHORTCUTS)
        box.setCurrentText("")
        if placeholder:
            box.lineEdit().setPlaceholderText(placeholder)
        return box

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
        icon_value = conf["menubar_emoji"] or cfg.DEFAULTS["menubar_emoji"]
        icon_index = self.menubar_emoji.findData(icon_value)
        if icon_index >= 0:
            self.menubar_emoji.setCurrentIndex(icon_index)
        else:
            self.menubar_emoji.setCurrentText(icon_value)
        self.launch_at_login.setChecked(conf["launch_at_login"])
        self.auto_update.setChecked(conf["auto_update"])
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

        self.openai_key.setText(conf["openai_api_key"])
        self.openrouter_key.setText(conf["openrouter_api_key"])
        self._models = {
            "local": conf["local_whisper_model"],
            "openai": conf["transcribe_model"],
            "openrouter": conf["openrouter_transcribe_model"],
        }
        self._shown_provider = ""
        self._select_data(self.transcribe_provider, conf["transcribe_provider"])
        self._provider_changed()  # selecting index 0 fires no signal
        self.cleanup_enabled.setChecked(conf["cleanup_enabled"])
        self._select_data(self.cleanup_provider, conf["cleanup_provider"])
        self.cleanup_model.setCurrentText(conf["cleanup_model"])
        self.cleanup_codex_model.setCurrentText(
            conf["cleanup_codex_model"] or t("Codex's own default")
        )
        self._select_data(self.cleanup_reasoning, conf["cleanup_reasoning"])
        self._cleanup_provider_changed()
        self.cleanup_prompt.setPlainText(conf["cleanup_prompt"] or cfg.default_cleanup_prompt())
        self.file_cleanup_prompt.setPlainText(
            conf["file_cleanup_prompt"] or cfg.default_file_cleanup_prompt()
        )
        self.transcribe_prompt.setPlainText(conf["transcribe_prompt"])

        self.assistant_shortcut.setCurrentText(conf["assistant_shortcut"])
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
        self.meeting_shortcut.setCurrentText(conf["meeting_shortcut"])
        self.meeting_prompt.setPlainText(
            conf["meeting_prompt"] or cfg.default_meeting_prompt()
        )

        self.file_timestamps.setChecked(conf["file_timestamps"])
        self.file_cleanup.setChecked(conf["file_cleanup"])
        self.file_path = ""

        self.shortcut.setCurrentText(conf["shortcut"])
        self.evdev_enabled.setChecked(conf["evdev_hotkey"])

        self.history_limit.setValue(max(0, int(conf["history_limit"])))

        self._refresh_shortcut_status()
        self._refresh_meeting_shortcut_status()
        self._refresh_ask_shortcut_status()
        self._refresh_assistant_status()
        self._load_history()
        self._load_minutes()

    def _save(self):
        conf = self.conf
        conf["ui_language"] = self.ui_language.currentData() or "auto"
        icon_text = self.menubar_emoji.currentText().strip()
        icon_index = self.menubar_emoji.findText(icon_text)
        conf["menubar_emoji"] = (
            self.menubar_emoji.itemData(icon_index)
            if icon_index >= 0
            else icon_text or cfg.DEFAULTS["menubar_emoji"]
        )
        conf["launch_at_login"] = self.launch_at_login.isChecked()
        conf["auto_update"] = self.auto_update.isChecked()
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

        conf["openai_api_key"] = self.openai_key.text().strip()
        conf["openrouter_api_key"] = self.openrouter_key.text().strip()

        provider = self.transcribe_provider.currentData() or "openai"
        self._models[provider] = self.transcribe_model.currentText().strip()
        conf["transcribe_provider"] = provider
        for key, name in (("local", "local_whisper_model"),
                          ("openai", "transcribe_model"),
                          ("openrouter", "openrouter_transcribe_model")):
            conf[name] = self._models[key].strip() or cfg.DEFAULTS[name]

        conf["cleanup_enabled"] = self.cleanup_enabled.isChecked()
        conf["cleanup_provider"] = (
            self.cleanup_provider.currentData() or "openrouter"
        )
        conf["cleanup_model"] = self.cleanup_model.currentText().strip()
        codex_cleanup_model = self.cleanup_codex_model.currentText().strip()
        conf["cleanup_codex_model"] = (
            "" if codex_cleanup_model == t("Codex's own default")
            else codex_cleanup_model
        )
        conf["cleanup_reasoning"] = self.cleanup_reasoning.currentData() or ""

        # Store an empty prompt when it matches the default, so switching the
        # interface language also switches the prompt language.
        prompt = self.cleanup_prompt.toPlainText().strip()
        conf["cleanup_prompt"] = "" if prompt == cfg.default_cleanup_prompt() else prompt
        file_prompt = self.file_cleanup_prompt.toPlainText().strip()
        conf["file_cleanup_prompt"] = ("" if file_prompt == cfg.default_file_cleanup_prompt()
                                       else file_prompt)
        conf["transcribe_prompt"] = self.transcribe_prompt.toPlainText().strip()

        conf["assistant_shortcut"] = self.assistant_shortcut.currentText().strip()
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
        conf["meeting_shortcut"] = self.meeting_shortcut.currentText().strip()
        meeting_prompt = self.meeting_prompt.toPlainText().strip()
        conf["meeting_prompt"] = ("" if meeting_prompt == cfg.default_meeting_prompt()
                                  else meeting_prompt)

        conf["file_timestamps"] = self.file_timestamps.isChecked()
        conf["file_cleanup"] = self.file_cleanup.isChecked()

        conf["shortcut"] = self.shortcut.currentText().strip() or "Ctrl+Space"
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

    def _check_for_updates(self):
        if self.update_check is not None:
            self.update_check(True)

    def _on_update_status(self, message):
        self.update_status.setText(message)

    @staticmethod
    def _select_data(combo, value):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    # ---- api helpers -----------------------------------------------------

    def _provider_changed(self):
        """Swap the model box over to the newly chosen provider's own model."""
        if self._shown_provider:
            self._models[self._shown_provider] = self.transcribe_model.currentText().strip()
        provider = self.transcribe_provider.currentData() or "openai"
        self._shown_provider = provider
        self.transcribe_model.clear()
        self.transcribe_model.addItems(TRANSCRIBE_MODELS[provider])
        self.transcribe_model.setCurrentText(self._models[provider])
        if provider == "local":
            self.refresh_transcribe_models.setText(t("Install local Whisper"))
            self.transcribe_status.setText(
                local_whisper.status(self.transcribe_model.currentText())
            )
        else:
            self.refresh_transcribe_models.setText(t("Fetch model list"))
            self.transcribe_status.setText("")

    def _cleanup_provider_changed(self):
        codex = (self.cleanup_provider.currentData() == "codex")
        self.cleanup_openrouter_label.setVisible(not codex)
        self.cleanup_openrouter_row.setVisible(not codex)
        self.cleanup_codex_label.setVisible(codex)
        self.cleanup_codex_model.setVisible(codex)
        self.models_label.setText(
            t("Uses your signed-in Codex CLI session; no API key is needed. "
              "The transcript is sent to Codex.")
            if codex else
            t("Runs on OpenRouter.")
        )

    def _load_transcribe_models(self):
        """The model list of whichever provider is selected."""
        provider = self.transcribe_provider.currentData() or "openai"
        if provider == "local":
            self._install_local_whisper()
            return
        self.refresh_transcribe_models.setEnabled(False)
        self.transcribe_status.setText(t("Fetching model list…"))
        openai_key = self.openai_key.text().strip() or self.conf.openai_key()
        openrouter_key = self.openrouter_key.text().strip() or self.conf.openrouter_key()
        base = self.conf["openai_base_url"]

        def work():
            try:
                models = (api.openrouter_models(openrouter_key, transcription=True)
                          if provider == "openrouter"
                          else api.openai_models(openai_key, base))
                self._transcribe_models_loaded.emit(models, "")
            except api.ApiError as exc:
                self._transcribe_models_loaded.emit([], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _install_local_whisper(self):
        self.refresh_transcribe_models.setEnabled(False)
        self.transcribe_status.setText(
            t("Installing Local Whisper and downloading the recommended "
              "Turkish model (574 MB)…")
        )

        def progress(done, total):
            self._local_install_progress.emit(done, total)

        def work():
            try:
                path = local_whisper.install_recommended(progress)
                self._local_install_done.emit(True, str(path), "")
            except local_whisper.LocalWhisperError as exc:
                self._local_install_done.emit(False, "", str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_local_install_progress(self, done, total):
        if total:
            self.transcribe_status.setText(
                t("Downloading Local Whisper model: {percent}%",
                  percent=min(100, int(done * 100 / total)))
            )
        else:
            self.transcribe_status.setText(
                t("Downloading Local Whisper model: {size} MB",
                  size=round(done / 1024 / 1024))
            )

    def _on_local_install_done(self, ok, path, error):
        self.refresh_transcribe_models.setEnabled(True)
        if not ok:
            self.transcribe_status.setText("✗ " + error)
            return
        self.transcribe_model.setCurrentText(path)
        self._models["local"] = path
        self.transcribe_status.setText(
            "✓ " + t("Local Whisper is ready (no API key).")
        )

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
        self.test_button.setEnabled(False)
        self.test_label.setText(t("Trying…"))
        key = self.openai_key.text().strip() or self.conf.openai_key()
        base = self.conf["openai_base_url"]

        def work():
            try:
                models = api.openai_models(key, base)
                self._test_done.emit(
                    True, t("Connection works. {count} audio models visible.", count=len(models))
                )
            except api.ApiError as exc:
                self._test_done.emit(False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _test_openrouter(self):
        self.or_test_button.setEnabled(False)
        self.or_test_label.setText(t("Trying…"))
        key = self.openrouter_key.text().strip() or self.conf.openrouter_key()

        def work():
            try:
                self._or_test_done.emit(True, api.openrouter_key_status(key))
            except api.ApiError as exc:
                self._or_test_done.emit(False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_or_test_done(self, ok, message):
        self.or_test_button.setEnabled(True)
        self.or_test_label.setText(("✓ " if ok else "✗ ") + message)

    def _on_test_done(self, ok, message):
        self.test_button.setEnabled(True)
        self.test_label.setText(("✓ " if ok else "✗ ") + message)

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

    # ---- shortcut --------------------------------------------------------

    def _install_shortcut(self):
        combo = self.shortcut.currentText().strip() or "Ctrl+Space"
        clashes = hotkey.conflicting_shortcuts(combo)
        if clashes:
            answer = QMessageBox.question(
                self, t("Shortcut conflict"),
                t("{shortcut} is also used by:\n\n{list}\n\nInstall anyway?",
                  shortcut=combo, list="\n".join(clashes[:6])),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        ok, message = hotkey.install_kde_shortcut(combo, self.launch_command)
        QMessageBox.information(self, t("Shortcut"), message)
        if ok:
            self.conf["shortcut"] = combo
            if IS_MACOS:
                self.conf["evdev_hotkey"] = True
                self.evdev_enabled.setChecked(True)
            self.conf.save()
            if IS_MACOS:
                self.applied.emit()
        self._refresh_shortcut_status()

    def _remove_shortcut(self):
        hotkey.remove_kde_shortcut()
        if IS_MACOS:
            self.conf["evdev_hotkey"] = False
            self.evdev_enabled.setChecked(False)
            self.conf.save()
            self.applied.emit()
        self._refresh_shortcut_status()

    def _refresh_shortcut_status(self):
        current = hotkey.kde_shortcut_status()
        self.shortcut_status.setText(
            (t("Active on macOS: {shortcut}", shortcut=current)
             if IS_MACOS and current else
             t("No macOS shortcut active.") if IS_MACOS else
             t("Registered in KDE: {shortcut}", shortcut=current) if current
             else t("No KDE shortcut installed."))
        )

    def _install_meeting_shortcut(self):
        combo = self.meeting_shortcut.currentText().strip()
        if not combo:
            QMessageBox.information(self, t("Shortcut"),
                                    t("Type a key combination first."))
            return
        clashes = hotkey.conflicting_shortcuts(combo, hotkey.MEETING_DESKTOP_ID)
        if clashes:
            answer = QMessageBox.question(
                self, t("Shortcut conflict"),
                t("{shortcut} is also used by:\n\n{list}\n\nInstall anyway?",
                  shortcut=combo, list="\n".join(clashes[:6])),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        ok, message = hotkey.install_kde_shortcut(
            combo, self.meeting_command, name="Dikte: start/end a meeting recording",
            desktop_id=hotkey.MEETING_DESKTOP_ID,
        )
        QMessageBox.information(self, t("Shortcut"), message)
        if ok:
            self.conf["meeting_shortcut"] = combo
            if IS_MACOS:
                self.conf["evdev_hotkey"] = True
            self.conf.save()
            if IS_MACOS:
                self.applied.emit()
        self._refresh_meeting_shortcut_status()

    def _remove_meeting_shortcut(self):
        hotkey.remove_kde_shortcut(hotkey.MEETING_DESKTOP_ID)
        if IS_MACOS:
            self.conf["meeting_shortcut"] = ""
            self.conf.save()
            self.applied.emit()
        self._refresh_meeting_shortcut_status()

    def _refresh_meeting_shortcut_status(self):
        current = hotkey.kde_shortcut_status(hotkey.MEETING_DESKTOP_ID)
        self.meeting_shortcut_status.setText(
            (t("Active on macOS: {shortcut}", shortcut=current)
             if IS_MACOS and current else
             t("No macOS meeting shortcut active. The menu can start one too.")
             if IS_MACOS else
             t("Registered in KDE: {shortcut}", shortcut=current) if current
             else t("No KDE shortcut installed. The tray menu starts a meeting too."))
        )

    # ---- Claude ----------------------------------------------------------

    def _install_ask_shortcut(self):
        combo = self.assistant_shortcut.currentText().strip()
        if not combo:
            QMessageBox.information(self, t("Shortcut"),
                                    t("Type a key combination first."))
            return
        clashes = hotkey.conflicting_shortcuts(combo, hotkey.ASK_DESKTOP_ID)
        if clashes:
            answer = QMessageBox.question(
                self, t("Shortcut conflict"),
                t("{shortcut} is also used by:\n\n{list}\n\nInstall anyway?",
                  shortcut=combo, list="\n".join(clashes[:6])),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        ok, message = hotkey.install_kde_shortcut(
            combo, self.ask_command, name="Dikte: ask Claude Code",
            desktop_id=hotkey.ASK_DESKTOP_ID,
        )
        QMessageBox.information(self, t("Shortcut"), message)
        if ok:
            self.conf["assistant_shortcut"] = combo
            if IS_MACOS:
                self.conf["evdev_hotkey"] = True
            self.conf.save()
            if IS_MACOS:
                self.applied.emit()
        self._refresh_ask_shortcut_status()

    def _remove_ask_shortcut(self):
        hotkey.remove_kde_shortcut(hotkey.ASK_DESKTOP_ID)
        if IS_MACOS:
            self.conf["assistant_shortcut"] = ""
            self.conf.save()
            self.applied.emit()
        self._refresh_ask_shortcut_status()

    def _refresh_ask_shortcut_status(self):
        current = hotkey.kde_shortcut_status(hotkey.ASK_DESKTOP_ID)
        self.assistant_shortcut_status.setText(
            (t("Active on macOS: {shortcut}", shortcut=current)
             if IS_MACOS and current else
             t("No macOS agent shortcut active. The menu can start one too.")
             if IS_MACOS else
             t("Registered in KDE: {shortcut}", shortcut=current) if current
             else t("No KDE shortcut installed. The tray menu asks it too."))
        )

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
