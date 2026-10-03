"""A staged dictionary editor; Save applies changes, Cancel discards them."""
from PyQt6.QtCore import Qt, QSignalBlocker
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox, QListWidget,
    QListWidgetItem, QPushButton, QCheckBox, QLabel, QPlainTextEdit,
)
from . import dictionary
from .i18n import t

SCOPE = ("Learning is off by default. When enabled, only successful dictation "
         "without warnings is eligible: a title-case word of 3–40 letters must "
         "occur twice away from sentence starts in the raw transcript and remain "
         "in the final text. No meetings, files, assistant commands, other apps, "
         "clipboard reading or extra requests. Learned terms join the hints sent "
         "to your chosen transcription and cleanup providers. Review them below; "
         "learning can still make mistakes.")


class DictionaryEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.baseline = []
        self.reset_auto = False
        self.replace_all = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.learning = QCheckBox(t("Learn repeated names from successful dictation"))
        layout.addWidget(self.learning)
        scope = QLabel(t(SCOPE))
        scope.setWordWrap(True)
        layout.addWidget(scope)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(t("Search dictionary"))
        self.source = QComboBox()
        for label, value in (("All", ""), ("Auto-added", "auto"),
                             ("Manually-added", "manual")):
            self.source.addItem(t(label), value)
        bar.addWidget(self.search)
        bar.addWidget(self.source)
        layout.addLayout(bar)
        self.list = QListWidget()
        self.list.setMaximumHeight(160)
        layout.addWidget(self.list)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(t("Word or multiword expression"))
        self.text.setMaximumHeight(65)
        layout.addWidget(self.text)
        buttons = QHBoxLayout()
        for label, action in (("Add entry", self.add), ("Update entry", self.edit),
                              ("Delete entry", self.delete),
                              ("Reset learned entries", self.reset)):
            button = QPushButton(t(label))
            button.clicked.connect(action)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        layout.addWidget(QLabel(t("Freeform dictionary (editing replaces the records)")))
        self.legacy = QPlainTextEdit()
        self.legacy.setMaximumHeight(90)
        layout.addWidget(self.legacy)
        self.legacy.textChanged.connect(self._legacy_changed)
        self.search.textChanged.connect(self.refresh)
        self.source.currentIndexChanged.connect(self.refresh)
        self.list.currentItemChanged.connect(self.select)

    def load(self, conf):
        self.rows = conf.dictionary_entries()
        self.baseline = [dict(row) for row in self.rows]
        self.reset_auto = False
        self.replace_all = False
        self.learning.setChecked(bool(conf["dictionary_learning"]))
        self.sync()

    def save(self, conf):
        # A successful dictation may have learned while Settings was open.
        # Only apply the user's edits; retain newly learned records unless reset.
        with conf._dictionary_lock:
            current = conf.dictionary_entries()
            baseline = {r["id"]: r for r in self.baseline}
            edited = {r["id"]: r for r in self.rows}
            if self.replace_all:
                merged = self.rows
            else:
                merged = []
                for row in current:
                    ident = row["id"]
                    if ident in baseline and ident not in edited:
                        continue
                    if ident in edited and edited[ident] != baseline.get(ident):
                        row = edited[ident]
                    if not (self.reset_auto and row["source"] == "auto"):
                        merged.append(row)
                ids = {r["id"] for r in merged}
                for row in self.rows:
                    if row != baseline.get(row["id"]) and row["id"] not in ids:
                        merged.append(row)
                unique = {}
                for row in merged:
                    key = dictionary.key(row["text"])
                    previous = unique.get(key)
                    if (previous is None or
                            (row["source"] == "manual" and previous["source"] == "auto")):
                        unique[key] = row
                merged = list(unique.values())
            conf.set_dictionary(merged)
            conf["dictionary_learning"] = self.learning.isChecked()
        # SettingsWindow reloads only after its disk save succeeds. Keeping the
        # original baseline here also makes a failed save safe to retry.

    def _legacy_changed(self):
        self.replace_all = True
        self.rows = dictionary.reconcile(None, self.legacy.toPlainText())
        self.reset_auto = True
        self.refresh()

    def sync(self):
        with QSignalBlocker(self.legacy):
            self.legacy.setPlainText(dictionary.render(self.rows))
        self.refresh()

    def refresh(self, *args):
        self.list.clear()
        query, source = dictionary.key(self.search.text()), self.source.currentData()
        for row in self.rows:
            if query not in dictionary.key(row["text"]) or (source and row["source"] != source):
                continue
            item = QListWidgetItem(t("Auto-added" if row["source"] == "auto"
                                     else "Manually-added") + ": " + row["text"])
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.list.addItem(item)

    def selected_id(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def select(self, *args):
        selected = self.selected_id()
        row = next((r for r in self.rows if r["id"] == selected), None)
        if row:
            self.text.setPlainText(row["text"])

    def add(self):
        self.change()

    def edit(self):
        if self.selected_id():
            self.change(self.selected_id())

    def change(self, selected=None):
        try:
            self.rows = dictionary.add(self.rows, self.text.toPlainText(),
                                       replace_id=selected)
        except ValueError as exc:
            self.error.setText(t(str(exc)))
            return
        self.error.clear()
        self.text.clear()
        self.sync()

    def delete(self):
        selected = self.selected_id()
        self.rows = [r for r in self.rows if r["id"] != selected]
        self.sync()

    def reset(self):
        self.reset_auto = True
        self.rows = [r for r in self.rows if r["source"] != "auto"]
        self.sync()
