"""The first-run setup: one service and its key, or one model on this computer.

Settings holds every option there is, which is the wrong first screen for
somebody who only wants to talk and see text. This dialog asks for the one
thing transcription cannot run without and leaves the rest at its defaults.
It writes through the Settings window's own form and its Apply, so what it
saves is exactly what Settings would have saved, and the full form stays one
link away.
"""

import re
import threading

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from . import api, ggml, theme, widgets
from . import config as cfg
from .i18n import t

# The services offered here, OpenRouter first: its one key also writes meeting
# minutes and cleans transcripts up, so it is the one that needs nothing else.
# OpenRouter and OpenAI are providers of their own; the rest are presets of
# the Custom server provider and are saved as one.
CLOUD = ("openrouter", "openai", "groq", "gemini", "requesty")
# Where each service hands out its keys.
KEY_PAGES = {
    "openrouter": "https://openrouter.ai/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "groq": "https://console.groq.com/keys",
    "gemini": "https://aistudio.google.com/apikey",
    "requesty": "https://app.requesty.ai/api-keys",
}
# The services whose key can clean the transcript up as well, so choosing one
# of them turns cleanup on with nothing more to set up.
CLEANS = ("openrouter", "gemini", "requesty")


def service_name(name):
    """What a named provider or a Custom server preset is called on screen."""
    if name in cfg.PRESETS:
        return t(cfg.PRESETS[name].service)
    return t(cfg.TRANSCRIBERS[name].service)


def friendly_model(name):
    """A whisper file as people call the model: "ggml-large-v3-turbo-q5_0.bin"
    is "Whisper Large v3 Turbo". The quantisation and the file are left to
    the tooltip and the Advanced part."""
    stem = re.sub(r"\.bin$", "", name or "")
    stem = re.sub(r"^ggml-", "", stem)
    stem = re.sub(r"-(q\d[\w]*|f16|f32)$", "", stem, flags=re.IGNORECASE)
    english = stem.endswith(".en")
    stem = stem.removesuffix(".en")
    words = [word if re.fullmatch(r"v\d+", word) else word.capitalize()
             for word in stem.split("-") if word]
    title = " ".join(["Whisper"] + words) if words else name
    return t("{model} (English)", model=title) if english else title


def rough_size(count):
    """A size the way a person says it: "550 MB", "0.9 GB", "2.9 GB". A
    first-run choice needs the scale, not the fourth digit."""
    megabytes = count / (1024 * 1024)
    if megabytes < 1:
        return "1 MB"
    if megabytes < 900:
        step = 10 if megabytes >= 100 else 1
        return f"{round(megabytes / step) * step:.0f} MB"
    return f"{megabytes / 1024:.1f} GB"


def memory_need(size):
    """About how much memory a whisper model of `size` bytes takes once
    loaded: the weights and the working buffers around them. An estimate
    fitted to whisper.cpp's published figures (75 MB of tiny needs about
    270 MB, 2.9 GB of large about 3.9 GB), not a measurement."""
    return int(size * 1.2 + 250 * 1024 * 1024)


class SetupDialog(QDialog):
    """`route` is "cloud" or "local". `finished_setup` fires once transcription
    is set up and saved."""

    finished_setup = pyqtSignal()
    _checked = pyqtSignal(bool, str)

    def __init__(self, settings, route="cloud", parent=None, open_full=None):
        super().__init__(parent)
        self.settings = settings
        # How the full Settings window is opened, so it comes up the way the
        # application always opens it.
        self._open_settings = open_full
        self.conf = settings.conf
        self.route = route
        self.setWindowTitle(t("Set up transcription"))
        theme.apply(self, self.conf["theme"])
        column = QVBoxLayout(self)
        column.setContentsMargins(22, 18, 22, 16)
        column.setSpacing(12)
        heading = QLabel(t("Connect a transcription service") if route == "cloud"
                         else t("Transcribe on this computer"))
        heading.setObjectName("editortitle")
        column.addWidget(heading)
        if route == "cloud":
            self._cloud(column)
        else:
            self._local(column)
        buttons = QHBoxLayout()
        self.full = QPushButton(t("All model settings"))
        self.full.setObjectName("link")
        self.full.setCursor(Qt.CursorShape.PointingHandCursor)
        self.full.setAutoDefault(False)
        self.full.clicked.connect(self._open_full)
        buttons.addWidget(self.full)
        buttons.addStretch(1)
        cancel = QPushButton(t("Cancel"))
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.go = QPushButton(t("Save and start") if route == "cloud"
                              else t("Download and use"))
        self.go.setObjectName("primary")
        self.go.setAutoDefault(False)
        self.go.clicked.connect(self._save_cloud if route == "cloud" else self._local_next)
        buttons.addWidget(self.go)
        column.addSpacing(6)
        column.addLayout(buttons)
        self._checked.connect(self._on_checked)
        self.setMinimumWidth(520)
        # After every widget exists: filling the key in calls _sync, which
        # reads the button above.
        if route == "cloud":
            self._service_changed()
        self._sync()

    # ---- a service and its key ------------------------------------------

    def _cloud(self, column):
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(14)
        self.service = QComboBox()
        for name in CLOUD:
            self.service.addItem(service_name(name), name)
        self.service.setItemData(0, t("One key also writes meeting minutes and cleans "
                                      "transcripts up."), Qt.ItemDataRole.ToolTipRole)
        self.service.currentIndexChanged.connect(self._service_changed)
        get_key = QPushButton(t("Get a key"))
        get_key.setObjectName("link")
        get_key.setCursor(Qt.CursorShape.PointingHandCursor)
        get_key.setAutoDefault(False)
        get_key.setToolTip(t("Opens the service's key page in your browser."))
        get_key.clicked.connect(self._get_key)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.service, 1)
        row.addWidget(get_key)
        form.addRow(t("Service"), row)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText(t("Paste your key"))
        self.key.setAccessibleName(t("Key"))
        self.key.textChanged.connect(self._sync)
        self.test = QPushButton(t("Test"))
        self.test.setAutoDefault(False)
        self.test.clicked.connect(self._test)
        keys = QHBoxLayout()
        keys.setSpacing(8)
        keys.addWidget(self.key, 1)
        keys.addWidget(self.test)
        form.addRow(t("Key"), keys)
        self.answer = widgets.StatusLine()
        form.addRow("", self.answer)
        column.addLayout(form)

    def provider(self):
        return self.service.currentData() if self.route == "cloud" else "local"

    def _stored_key(self, name):
        """The key Settings already holds for a service or preset, as typed."""
        settings = self.settings
        if name in cfg.PRESETS:
            # A job showing that preset holds the key as typed now.
            for role, shown in settings._preset_shown.items():
                if shown == name:
                    return settings._preset_widgets[role]["key"].text()
            return settings._custom_values.get(cfg.PRESETS[name].key, "")
        field = settings._key_fields.get(name)
        return field.text() if field is not None else ""

    def _service_changed(self, *_):
        # A key already in Settings for this service is offered again rather
        # than asked for twice.
        self.key.setText(self._stored_key(self.provider()))
        self.answer.set()

    def _get_key(self):
        QDesktopServices.openUrl(QUrl(KEY_PAGES[self.provider()]))

    def _test(self):
        provider = self.provider()
        key = self.key.text().strip()
        base = (self.conf.get(f"{provider}_base_url", cfg.PRESETS[provider].base_url)
                if provider in cfg.PRESETS else self.conf[cfg.TRANSCRIBERS[provider].url])
        self.test.setEnabled(False)
        self._testing = True
        self._tested = provider
        self.answer.set(t("Trying…"))
        from .settings_ui import key_check

        def work():
            try:
                self._checked.emit(True, key_check(provider, key, base))
            except api.ApiError as exc:
                self._checked.emit(False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_checked(self, ok, message):
        self._testing = False
        self.test.setEnabled(True)
        if getattr(self, "_tested", None) != self.provider():
            return  # the answer is about a service no longer picked
        if ok:
            self.answer.set(message)
        else:
            self.answer.set("", message.splitlines()[0], message, "danger")

    def _save_cloud(self):
        provider, key = self.provider(), self.key.text().strip()
        settings = self.settings
        if provider in cfg.PRESETS:
            settings.transcribe_provider.setCurrentIndex(
                settings.transcribe_provider.findData("custom"))
            self._use_preset("transcribe", provider, key)
        else:
            settings.transcribe_provider.setCurrentIndex(
                settings.transcribe_provider.findData(provider))
            settings._key_fields[provider].setText(key)
        self._sensible_cleanup(provider)
        self._finish()

    # ---- a model on this computer ---------------------------------------

    def _local(self, column):
        from .settings_ui import LocalModelBox
        # The full box does the work (the engine, the model, the downloads),
        # folded under Advanced: what this dialog shows is the one model it
        # recommends and one button that fetches everything it needs.
        self.box = LocalModelBox(ggml.WHISPER, t("On this machine"),
                                 ggml.whisper_models, ggml.whisper_model_path)
        self.box.custom_binary = self.settings.local_whisper.custom_binary
        self.box.load(self.conf["local_model"] or ggml.SUGGESTED_WHISPER)
        self.box.changed.connect(self._local_progress)
        self.box.program_changed.connect(self._local_progress)
        self.box._progress.connect(self._show_progress)
        self.box._finished.connect(self._download_ended)
        self.box._installed.connect(self._download_ended)

        summary = QHBoxLayout()
        summary.setSpacing(14)
        summary.addWidget(widgets.Badge("chip", "accent", 40), 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.model_name = QLabel()
        self.model_name.setObjectName("tiletitle")
        top.addWidget(self.model_name)
        self.recommended = QLabel(t("Recommended"))
        self.recommended.setObjectName("badgeText")
        self.recommended.setToolTip(t("The model this computer runs best."))
        top.addWidget(self.recommended)
        top.addStretch(1)
        text.addLayout(top)
        self.model_facts = QLabel()
        self.model_facts.setObjectName("muted")
        text.addWidget(self.model_facts)
        text.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        summary.addLayout(text, 1)
        column.addLayout(summary)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        column.addWidget(self.progress)
        self.local_status = widgets.StatusLine()
        column.addWidget(self.local_status)

        # Whether anything still leaves the computer: transcription is local
        # now, but cleanup may go on sending the text to a service.
        self.privacy = QLabel()
        self.privacy.setObjectName("muted")
        self.privacy.setWordWrap(True)
        column.addWidget(self.privacy)
        # Said as what stays on, ticked: unticking it is what keeps the text
        # on this computer as well.
        self.cleanup_keep = QCheckBox()
        self.cleanup_keep.setChecked(True)
        self.cleanup_keep.setToolTip(t(
            "Cleanup can be set up on this computer later, under Models in Settings."))
        self.cleanup_keep.toggled.connect(self._show_privacy)
        column.addWidget(self.cleanup_keep)

        advanced = QWidget()
        inside = QVBoxLayout(advanced)
        inside.setContentsMargins(0, 4, 0, 0)
        inside.addWidget(self.box)
        self.advanced = widgets.Disclosure(t("Advanced"), advanced)
        column.addWidget(self.advanced)
        self._chain = False
        self._step = ""
        self._chose_best = False
        # Folded away, the box is never shown and would never ask for its
        # list; the summary above needs it now.
        if self.box._pending:
            self.box._pending = False
            self.box._fetch_models(self.box.repository())
        self._show_model()
        self._show_privacy()

    def _cleaner(self):
        """The service cleanup would still send text to, or ""."""
        settings = self.settings
        if not settings.cleanup_enabled.isChecked():
            return ""
        cleaner = self._cleanup_service()
        if cleaner == "local" or self._cleanup_keyless():
            # No key: _sensible_cleanup turns it off on the way out.
            return ""
        if settings.cleanup_provider.currentData() == "custom" and cleaner != "other":
            return service_name(cleaner)
        return settings.cleanup_provider.currentText()

    def _cleanup_service(self):
        """Which service cleans up: a named provider, or the Custom server
        preset when that is the provider."""
        settings = self.settings
        cleaner = settings.cleanup_provider.currentData() or ""
        if cleaner == "custom":
            return settings._preset_widgets["cleanup"]["preset"].currentData() or "other"
        return cleaner

    def _cleanup_keyless(self):
        """Whether cleanup points at a service it has no key (or, for a server
        of your own, no address) for."""
        settings = self.settings
        cleaner = settings.cleanup_provider.currentData() or ""
        if cleaner == "custom":
            rows = settings._preset_widgets["cleanup"]
            name = rows["preset"].currentData() or "other"
            if name == "other":
                return not rows["url"].text().strip()
            return not (rows["key"].text().strip()
                        or self.conf.api_key(cfg.PRESETS[name].key))
        field = settings._key_fields.get(cleaner)
        return field is not None and not (field.text().strip()
                                          or self.conf.api_key(f"{cleaner}_api_key"))

    def _use_preset(self, role, name, key=None):
        """Point a job's Custom server rows at preset `name`, with `key` if
        one is given; the other jobs on that preset share it."""
        settings = self.settings
        rows = settings._preset_widgets[role]
        if rows["preset"].findData(name) < 0:
            return
        settings._select_data(rows["preset"], name)
        if key is not None:
            rows["key"].setText(key)
            settings._share_preset_key(role)

    def _show_privacy(self, *_):
        cleaner = self._cleaner()
        self.cleanup_keep.setVisible(bool(cleaner))
        self.cleanup_keep.setText(t("Clean up with {service}", service=cleaner))
        if cleaner and self.cleanup_keep.isChecked():
            self.privacy.setText(t("Cleanup still sends the text to {service}.",
                                   service=cleaner))
        else:
            self.privacy.setText(t("Nothing leaves this computer."))

    def _show_model(self):
        """The chosen model in words: its name, its size and the memory it
        needs. The file behind it is the tooltip."""
        name = self.box.selected()
        item = self.box._current_item()
        self.model_name.setText(friendly_model(name) if name else t("Speech model"))
        self.model_name.setToolTip(name)
        items = self.box._current_items()
        best = ggml.recommended(items, self.box._suggested()) if items else ""
        if best and not self.conf["local_model"] and not self._chose_best:
            # Nobody picked a model yet: the one offered is the one this
            # machine should take, once the list says which that is.
            self._chose_best = True
            index = self.box.model.findData(ggml.whisper_model_path(best).name)
            if index >= 0 and index != self.box.model.currentIndex():
                self.box.model.setCurrentIndex(index)
                return
            name, item = self.box.selected(), self.box._current_item()
        self.recommended.setVisible(
            bool(name and best) and name == ggml.whisper_model_path(best).name)
        if ggml.have_model(ggml.whisper_model_path(name)) if name else False:
            facts = t("Downloaded")
        elif item is not None and item.size:
            facts = t("Download {size}", size=rough_size(item.size))
        else:
            facts = ""
        if item is not None and item.size:
            need = t("needs about {size} of memory", size=rough_size(memory_need(item.size)))
            facts = widgets.meta_line(facts, need)
        self.model_facts.setText(facts)
        self.model_facts.setVisible(bool(facts))
        self.model_facts.setToolTip(t("The memory figure is an estimate."))

    def _show_progress(self, job, done, total):
        self.progress.show()
        self.progress.setRange(0, 1000 if total else 0)
        if total:
            self.progress.setValue(int(done * 1000 / total))
        what = t("Downloading the speech engine…") if job == "program" \
            else t("Downloading the model…")
        share = f" {done * 100 // total}%" if total else ""
        self.local_status.set(what + share)

    def _download_ended(self, _name, error):
        self.progress.hide()
        if error:
            self.local_status.set("", error.splitlines()[0], error, "danger")
        else:
            self.local_status.set()

    def _local_ready(self):
        name = self.box.selected()
        return bool(name and self.box._program_path()
                    and ggml.have_model(ggml.whisper_model_path(name)))

    def _local_next(self):
        """One press for the whole way: the program, then the model, then use
        it. Each download reports back through the box (_local_progress)."""
        box = self.box
        if self._local_ready():
            self._save_local()
            return
        self._chain = True
        if not box._program_path():
            self._step = "program"
            box._install_program()
        elif box._current_item() is not None:
            self._step = "model"
            box._download()
        else:
            self._chain = False
        self._sync()

    def _local_progress(self):
        box = self.box
        self._show_model()
        if self._chain and not box._downloading and box.install_button.isEnabled():
            if self._local_ready():
                self._chain = False
                self._save_local()
                return
            if (self._step == "program" and box._program_path()
                    and box._current_item() is not None):
                self._step = "model"
                box._download()
                self._sync()
                return
            # A step ended without getting any further: it failed or was
            # stopped, and the box says why. Going round again would only
            # fail again.
            self._chain = False
        self._sync()

    def _save_local(self):
        settings = self.settings
        settings.transcribe_provider.setCurrentIndex(
            settings.transcribe_provider.findData("local"))
        settings.local_whisper.custom_binary = self.box.custom_binary
        settings.local_whisper.load(self.box.selected())
        self._sensible_cleanup("local")
        if not self.cleanup_keep.isHidden() and not self.cleanup_keep.isChecked():
            settings.cleanup_enabled.setChecked(False)
        self._finish()

    # ---- both -------------------------------------------------------------

    def _sensible_cleanup(self, provider):
        """Cleanup set up along the way or left alone, never left pointing at
        a service with no key: that would turn every dictation into a warning.
        The full form changes it later."""
        settings = self.settings
        cleaner = self._cleanup_service()
        keyless = self._cleanup_keyless()
        if provider in CLEANS and (keyless or cleaner == provider):
            # A different cleaner that already has its key is the user's
            # choice and stays; otherwise the service just connected cleans
            # up too, since it can.
            if provider in cfg.PRESETS:
                settings.cleanup_provider.setCurrentIndex(
                    settings.cleanup_provider.findData("custom"))
                self._use_preset("cleanup", provider)
            else:
                settings.cleanup_provider.setCurrentIndex(
                    settings.cleanup_provider.findData(provider))
            settings.cleanup_enabled.setChecked(True)
            return
        if keyless:
            settings.cleanup_enabled.setChecked(False)

    def _finish(self):
        self.settings._save(quiet=True)
        if self.conf.transcribe_ready():
            self.finished_setup.emit()
            self.accept()
        else:
            self._sync()

    def _sync(self, *_):
        if self.route == "cloud":
            self.go.setEnabled(bool(self.key.text().strip()))
        else:
            busy = self.box._downloading or not self.box.install_button.isEnabled()
            self.go.setEnabled(not busy)
            self.go.setText(t("Use this model") if self._local_ready()
                            else t("Download and use"))

    def busy(self):
        """A download, an install or a key test still running for this dialog."""
        if getattr(self, "_testing", False):
            return True
        box = getattr(self, "box", None)
        return bool(box is not None and (box._downloading
                                         or not box.install_button.isEnabled()))

    def done(self, result):
        """Every way out (Cancel, Escape, the window's close, All model
        settings) stops a model download rather than leaving it half done."""
        box = getattr(self, "box", None)
        if box is not None and box._downloading:
            box._stop = True
        super().done(result)

    def _open_full(self):
        self.reject()
        self.settings.show_page("api")
        if self._open_settings is not None:
            self._open_settings()
        else:
            self.settings.show()
            self.settings.raise_()
