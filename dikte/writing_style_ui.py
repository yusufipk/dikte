"""An opt-in, local preview with no automatic history or clipboard collection."""
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QCheckBox,
                             QComboBox, QLabel, QPlainTextEdit, QPushButton)
from . import writing_style as style
from .i18n import t


class WritingStyle(QWidget):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox(t("Enable writing style for dictation cleanup"))
        layout.addWidget(self.enabled)
        note = QLabel(t("Explicit choices override learned preferences. A custom dictation cleanup "
                        "instruction disables this profile. Cleanup must be enabled. Facts, meaning "
                        "and spoken language must be preserved. Save settings to apply changes."))
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.choices = {}
        for key, options in style.OPTIONS.items():
            combo = QComboBox()
            combo.addItem(t("Use learned preference, otherwise preserve"), "")
            for value, label in options.items():
                combo.addItem(t(label), value)
            form.addRow(t(key.capitalize()), combo)
            self.choices[key] = combo
        layout.addLayout(form)
        self.learned = {}
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        for key in style.OPTIONS:
            button = QPushButton(t("Remove learned {field}", field=key))
            button.clicked.connect(lambda checked=False, key=key: self.remove(key))
            layout.addWidget(button)
        self.consent = QCheckBox(t("Preview preferences from text I choose to share here"))
        layout.addWidget(self.consent)
        privacy = QLabel(t("Local surface analysis only; no accuracy score. Examples are not saved or "
                           "sent to a model. Only approved preference choices are saved; these choices "
                           "are sent to your cleanup provider during dictation. No history is collected."))
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        self.example = QPlainTextEdit()
        self.example.setPlaceholderText(t("Your writing example or corrected text (up to 10,000 characters)"))
        self.before = QPlainTextEdit()
        self.before.setPlaceholderText(t("Optional original text for a correction you choose to share"))
        for edit in (self.example, self.before):
            edit.setMaximumHeight(85)
            edit.setEnabled(False)
            edit.textChanged.connect(self.invalidate)
            layout.addWidget(edit)
        self.preview_button = QPushButton(t("Preview suggestions"))
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview)
        layout.addWidget(self.preview_button)
        self.preview_text = QLabel()
        self.preview_text.setWordWrap(True)
        layout.addWidget(self.preview_text)
        self.accept_button = QPushButton(t("Accept previewed preferences"))
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self.accept)
        layout.addWidget(self.accept_button)
        self.consent.toggled.connect(self.opt_in)
        reset = QPushButton(t("Reset profile and clear examples"))
        reset.clicked.connect(self.reset)
        layout.addWidget(reset)
        self.pending = {}
        self.refresh()

    def refresh(self):
        self.changed.emit()
        self.summary.setText(t("Learned preferences: ") + ("; ".join(
            f"{t(key.capitalize())}: {t(style.OPTIONS[key][value])}"
            for key, value in self.learned.items()) or t("None")))

    def remove(self, key):
        self.learned.pop(key, None)
        self.refresh()

    def invalidate(self):
        self.pending = {}
        self.accept_button.setEnabled(False)
        self.preview_text.clear()

    def opt_in(self, enabled):
        for edit in (self.example, self.before):
            edit.setEnabled(enabled)
        self.preview_button.setEnabled(enabled)
        self.invalidate()
        if not enabled:
            self.example.clear()
            self.before.clear()

    def preview(self):
        self.invalidate()
        if not self.consent.isChecked():
            return
        if max(len(self.example.toPlainText()), len(self.before.toPlainText())) > 10000:
            self.preview_text.setText(t("Limit each example to 10,000 characters."))
            return
        self.pending, reasons = style.suggest(self.example.toPlainText(), self.before.toPlainText())
        self.preview_text.setText("\n".join(t(reason) for reason in reasons) + "\n" + "\n".join(
            t(style.OPTIONS[key][value]) for key, value in self.pending.items()))
        self.accept_button.setEnabled(bool(self.pending))

    def accept(self):
        if self.consent.isChecked():
            self.learned.update(self.pending)
            self.refresh()
            self.consent.setChecked(False)

    def clear_examples(self):
        self.consent.setChecked(False)
        self.example.clear()
        self.before.clear()
        self.invalidate()

    def reset(self):
        self.enabled.setChecked(False)
        self.learned = {}
        for combo in self.choices.values():
            combo.setCurrentIndex(0)
        self.consent.setChecked(False)
        self.example.clear()
        self.before.clear()
        self.invalidate()
        self.refresh()

    def load(self, conf):
        self.reset()
        self.enabled.setChecked(conf["style_enabled"] is True)
        self.learned = style.normalize(conf["style_learned"])
        for key, value in style.normalize(conf["style_preferences"]).items():
            self.choices[key].setCurrentIndex(self.choices[key].findData(value))
        self.refresh()

    def save(self, conf):
        conf["style_enabled"] = self.enabled.isChecked()
        conf["style_preferences"] = {key: combo.currentData() for key, combo in self.choices.items()
                                     if combo.currentData()}
        conf["style_learned"] = dict(self.learned)
        self.consent.setChecked(False)
