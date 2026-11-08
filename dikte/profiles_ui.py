"""The two editors behind the dictionary and the writing profiles in Settings.

Kept out of settings_ui.py, which is long enough, and built the way the rest of
that window expects: each editor holds its own working copy, is read into the
form by load() and written back by store() when the window's Apply runs, and
reports what it holds through values() so the window can tell that something
is unsaved. The boxes inside an editor are marked as the editor's own (owned()),
because a search field or the box showing the selected profile changes without
anything having been edited.
"""

import copy
import html
import itertools
import threading

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget,
)

from . import dictionary
from . import frontapp
from . import profiles
from . import widgets
from .i18n import t

_OWNED = "dikte_editor"

# What each preference value is called on screen.
STYLE_LABELS = {
    "formality": ("Formality", {"casual": "Casual", "neutral": "Neutral",
                                "formal": "Formal"}),
    "sentences": ("Sentence length", {"short": "Short", "long": "Longer"}),
    "punctuation": ("Punctuation", {"minimal": "Light",
                                    "expressive": "Expressive"}),
    "brevity": ("Brevity", {"concise": "Concise",
                            "complete": "Keep the full phrasing"}),
}


class _Relay(QObject):
    """Where a program lookup reports back. One for the whole application and
    never deleted, so a thread finishing after its editor has gone emits on
    something that still exists; the editor's own connection goes with it."""

    found = pyqtSignal(object, str)
    # A question's number, the program in front, and its title when it is a
    # browser: what Add site reads after its countdown.
    titled = pyqtSignal(object, str, str)
    # Whether GNOME's Window Calls extension answered, once it has been asked.
    probed = pyqtSignal(object)


_RELAY = []
# Whether the GNOME extension has been asked about in this session.
_PROBE = {"started": False}
# Every question asked, numbered across all editors: an address can be reused
# by a new editor, a number from this never is.
_QUESTIONS = itertools.count(1)


def _relay():
    if not _RELAY:
        _RELAY.append(_Relay())
    return _RELAY[0]


def owned(widget):
    """Whether a box belongs to one of these editors rather than to the form."""
    while widget is not None:
        if widget.property(_OWNED):
            return True
        widget = widget.parentWidget()
    return False


def _label(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _muted(text=""):
    label = _label(text)
    label.setObjectName("muted")
    return label


def _row(*widgets):
    row = QHBoxLayout()
    for widget in widgets:
        row.addWidget(widget)
    row.addStretch()
    return row


def style_summary(style):
    """"Formality: Casual, Sentence length: Short", for a set of preferences."""
    parts = []
    for key, (title, values) in STYLE_LABELS.items():
        if key in style:
            parts.append(t("{name}: {value}", name=t(title),
                           value=t(values[style[key]])))
    return ", ".join(parts)


# --- the dictionary ---------------------------------------------------------

class EntryDialog(QDialog):
    """One dictionary record: the word, and how it should be written."""

    def __init__(self, parent=None, term="", spelling=""):
        super().__init__(parent)
        title = t("Edit word") if term else t("Add a word")
        self.setWindowTitle(title)
        column = QVBoxLayout(self)
        column.setContentsMargins(18, 16, 18, 14)
        column.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("editortitle")
        column.addWidget(heading)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(9)
        form.setHorizontalSpacing(14)
        column.addLayout(form)
        self.term = QLineEdit(term)
        self.term.setPlaceholderText(t("e.g. dikte"))
        self.spelling = QLineEdit(spelling)
        self.spelling.setPlaceholderText(t("Optional, e.g. Dikte"))
        self.spelling.setToolTip(t(
            "Leave the spelling empty when the word is already written the way "
            "you want it. With one, the cleanup model is told to write the "
            "word that way."))
        form.addRow(t("Word or expression"), self.term)
        form.addRow(t("Preferred spelling"), self.spelling)
        # Built by hand rather than with a QDialogButtonBox, whose order
        # follows the desktop: every dialog of the app reads Cancel, then the
        # primary action at the right end.
        buttons = self.button_row = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton(t("Cancel"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton(t("Save"))
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        column.addSpacing(12)
        column.addLayout(buttons)
        self.setMinimumWidth(420)


class DictionaryEditor(QWidget):
    """The personal dictionary as a list of records, with learning's switch."""

    changed = pyqtSignal()

    FILTERS = (("All", ""), ("Auto-added", dictionary.AUTO),
               ("Manually added", dictionary.MANUAL))
    # The rows the list grows to before it scrolls: a few when it is stacked
    # above the instructions, so they stay in view; more when it has a
    # column of its own (set_max_rows).
    MAX_ROWS = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty(_OWNED, True)
        self._entries = []
        self._loaded = []
        self._cleared = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # One sentence on screen; how it is used is in the tooltip.
        intro = _label(t("Names and terms you use; they help transcription and cleanup."))
        intro.setToolTip(t(
            "Names and terms you say often. They go to the transcription model "
            "as a hint, and to the cleanup model as a glossary, so it can "
            "repair the ones that still come out wrong. Every profile uses "
            "the same dictionary, and so do audio files and meetings."))
        layout.addWidget(intro)

        self.search = QLineEdit()
        self.search.setPlaceholderText(t("Search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._show)
        self.filter = QComboBox()
        for label, value in self.FILTERS:
            self.filter.addItem(t(label), value)
        self.filter.currentIndexChanged.connect(self._show)
        # One way in, as on the profile list: a plus beside the search. Each
        # row edits and deletes itself; the one action over all of them waits
        # under More.
        self.add_button = widgets.GlyphButton("plus", t("Add…"))
        self.add_button.clicked.connect(self._add)
        more = self.more_button = widgets.GlyphButton("more", t("More"))
        more.setObjectName("more")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        self.clear_auto_button = menu.addAction(t("Clear auto-learned"))
        self.clear_auto_button.triggered.connect(self.clear_auto)
        more.setMenu(menu)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.search, 1)
        top.addWidget(self.filter)
        top.addWidget(self.add_button)
        top.addWidget(more)
        layout.addLayout(top)

        # Rows like the history's: the word as it is written, and under it,
        # muted, what it is heard as and whether it was learned.
        # One line each and no avatar: a word list, not a list of people.
        self.list = widgets.row_list(QListWidget(), lines=1, actions=(
            ("edit", t("Edit…"), self._edit_row),
            ("trash", t("Delete"), self._delete_row)), badges=False, inline=True)
        self.max_rows = self.MAX_ROWS
        self.list.setProperty("card", True)
        self.list.setAccessibleName(t("Dictionary"))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.itemDoubleClicked.connect(lambda *_: self._edit())
        delete_key = QShortcut(QKeySequence.StandardKey.Delete, self.list)
        delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_key.activated.connect(self.delete_selected)
        layout.addWidget(self.list)
        self.empty = _muted(t("No words yet."))
        layout.addWidget(self.empty)

        self.learning = QCheckBox(t("Learn names and terms from my dictations"))
        self.learning.toggled.connect(lambda *_: self.changed.emit())
        layout.addWidget(self.learning)
        layout.addStretch(1)
        self.learning.setToolTip(t("Learns after three dictations, on this computer.") + " " + t(
            "Only ordinary dictations are read, and only once they have been "
            "cleaned up and finished without a warning: never meetings, files, "
            "the assistant, translations, the clipboard or other programs. A name "
            "written with a capital in the middle of a sentence is added after "
            "it has come through three dictations unchanged, marked Auto. So is a "
            "misheard word the cleanup puts right the same way three times "
            "(Klod Kod to Claude Code), with what it was heard as. Until then "
            "what is being counted is kept in the dictionary file. A learned "
            "entry you delete is not learned again. The learning runs on this "
            "computer; what it adds is sent to the models with the rest of the "
            "dictionary."))

    # ---- the form ---------------------------------------------------------

    def load(self, conf):
        self._entries = dictionary.records(conf)
        self._loaded = [dict(r) for r in self._entries]
        self._cleared = False
        self.learning.setChecked(bool(conf["dictionary_learning"]))
        self._show()

    def values(self):
        return [[(r["term"], r["spelling"], r["source"]) for r in self._entries],
                self.learning.isChecked()]

    def store(self, conf):
        """Put the edits into `conf`. Returns what commit() writes once the
        window has saved config.json, or None when the records are unchanged:
        an Apply that edited nothing leaves the dictionary as it was stored,
        down to the commas."""
        conf["dictionary_learning"] = self.learning.isChecked()
        # The names still being counted are words out of dictations, kept
        # only for learning: they go when learning is off, or when the user
        # cleared what it learned.
        forget = self._cleared or not self.learning.isChecked()
        if self._entries == self._loaded:
            return (None, forget)
        return (dictionary.save(conf, self._entries, self._loaded), forget)

    @staticmethod
    def commit(stored):
        if stored is None:
            return
        pending, forget = stored
        if pending is not None:
            dictionary.write(pending, forget)
        elif forget:
            dictionary.forget_pending()

    # ---- editing ----------------------------------------------------------

    def entries(self):
        return list(self._entries)

    def add_entry(self, term, spelling="", source=dictionary.MANUAL):
        """Add one record per comma-separated piece; False when nothing was
        added because each one is already there."""
        added = False
        for piece in dictionary.split(term) or []:
            record = dictionary.entry(piece, spelling, source)
            if record is None or self._index(record["term"]) is not None:
                continue
            self._entries.append(record)
            added = True
        if added:
            self._show()
            self.changed.emit()
        return added

    def edit_entry(self, index, term, spelling=""):
        """Correct a record. A record the user corrected is theirs now, so an
        Auto mark is taken off it."""
        record = dictionary.entry(term, spelling)
        if record is None or not 0 <= index < len(self._entries):
            return False
        old = self._entries[index]
        if (old["term"], old["spelling"]) == (record["term"], record["spelling"]):
            return True
        # Only a change of word can collide: two equal terms migrated from the
        # old text stay editable, each one on its own.
        renamed = dictionary.fold(record["term"]) != dictionary.fold(old["term"])
        if renamed and self._index(record["term"], skip=index) is not None:
            return False
        record["breaks"] = old.get("breaks", 0)
        self._entries[index] = record
        self._show()
        self.changed.emit()
        return True

    def delete_entries(self, indexes):
        """Remove records. A line break that stood before one moves on to the
        record after it, so deleting a word does not join two lines."""
        doomed = set(indexes)
        kept, carry = [], 0
        for i, record in enumerate(self._entries):
            if i in doomed:
                carry = max(carry, record.get("breaks", 0))
                continue
            if carry and kept:
                record = dict(record, breaks=max(record.get("breaks", 0), carry))
            carry = 0
            kept.append(record)
        if kept:
            kept[0] = dict(kept[0], breaks=0)
        self._entries = kept
        self._show()
        self.changed.emit()

    def delete_selected(self):
        self.delete_entries(self._selected())

    def clear_auto(self):
        if not any(r["source"] == dictionary.AUTO for r in self._entries):
            return
        self._entries = dictionary.clear_auto(self._entries)
        self._cleared = True
        self._show()
        self.changed.emit()

    def _index(self, term, skip=None):
        """The first other record with this term, folded the way the
        dictionary folds it (Turkish's dotted I included)."""
        key = dictionary.fold(term)
        for i, record in enumerate(self._entries):
            if i != skip and dictionary.fold(record["term"]) == key:
                return i
        return None

    def _selected(self):
        return sorted(item.data(Qt.ItemDataRole.UserRole)
                      for item in self.list.selectedItems())

    def _edit_row(self, row):
        """The pencil on a row: edit that one, whatever else is selected."""
        item = self.list.item(row)
        if item is not None:
            self.list.clearSelection()
            item.setSelected(True)
            self._edit()

    def _delete_row(self, row):
        item = self.list.item(row)
        if item is not None:
            self.delete_entries([item.data(Qt.ItemDataRole.UserRole)])

    def _add(self):
        dialog = EntryDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_entry(dialog.term.text(), dialog.spelling.text())

    def _edit(self):
        chosen = self._selected()
        if not chosen:
            return
        record = self._entries[chosen[0]]
        dialog = EntryDialog(self, record["term"], record["spelling"])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if not self.edit_entry(chosen[0], dialog.term.text(),
                                   dialog.spelling.text()):
                QMessageBox.information(self, t("Dictionary"), t(
                    "That word is already in the dictionary."))

    def visible(self):
        """The records the search and the filter let through, as indexes."""
        needle = self.search.text().strip().casefold()
        wanted = self.filter.currentData() or ""
        out = []
        for i, record in enumerate(self._entries):
            if wanted and record["source"] != wanted:
                continue
            if needle and needle not in record["term"].casefold() \
                    and needle not in record["spelling"].casefold():
                continue
            out.append(i)
        return out

    @staticmethod
    def describe(record):
        """What a row says after the word: what is heard and how it is
        written ("klod kod → Claude Code"), and whether it was learned rather
        than added by hand."""
        heard = (f"{record['term']} \u2192 {record['spelling']}"
                 if record["spelling"] and record["spelling"] != record["term"] else "")
        if record["source"] == dictionary.AUTO:
            # Manual is what an entry is unless it says otherwise; only the
            # learned ones carry a mark.
            return widgets.meta_line(t("Auto"), heard) if heard else t("Auto")
        return heard

    def _show(self, *_):
        shown = self.visible()
        self.list.clear()
        for index in shown:
            record = self._entries[index]
            written = record["spelling"] or record["term"]
            meta = self.describe(record)
            item = QListWidgetItem(widgets.meta_line(written, meta))
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setData(widgets.BODY_ROLE, written)
            item.setData(widgets.META_ROLE, meta)
            item.setToolTip(meta or written)
            self.list.addItem(item)
        self.clear_auto_button.setEnabled(
            any(r["source"] == dictionary.AUTO for r in self._entries))
        self.empty.setVisible(not shown)
        self.list.setVisible(bool(shown))
        QTimer.singleShot(0, self._fit)

    def set_max_rows(self, rows):
        """How many rows the list shows before it scrolls."""
        if rows != self.max_rows:
            self.max_rows = rows
            self._fit()

    def _fit(self):
        """As tall as its rows, up to max_rows of them; past that it scrolls."""
        try:
            rows = [self.list.sizeHintForRow(i) for i in range(self.list.count())]
            chrome = self.list.frameWidth() * 2 + 10
            self.list.setFixedHeight(chrome + sum(rows[:self.max_rows]) if rows else chrome)
        except RuntimeError:
            pass  # the editor went while the timer ran


# --- the profiles -----------------------------------------------------------

class _RemovableDelegate(QStyledItemDelegate):
    """List rows with an x at the right end that removes the row.

    The text stays editable in place (double-click); only a press on the x
    removes, so a slip of the mouse on the text does nothing.
    """

    SIZE = 18

    def __init__(self, view, remove):
        super().__init__(view)
        self.view = view
        self.remove = remove

    def _cross(self, rect):
        return QRect(rect.right() - self.SIZE - 6,
                     rect.center().y() - self.SIZE // 2, self.SIZE, self.SIZE)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if not self.view.isEnabled():
            return
        colors = widgets.colors_for(self.view)
        painter.drawPixmap(self._cross(option.rect),
                           widgets.glyph_pixmap("close", colors["muted"], self.SIZE))

    def eventFilter(self, viewport, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            index = self.view.indexAt(event.position().toPoint())
            if index.isValid() and self._cross(self.view.visualRect(index)).contains(
                    event.position().toPoint()):
                self.remove(index.row())
                return True
        return super().eventFilter(viewport, event)



class ProfilesEditor(QWidget):
    """The Default profile, the named ones, and the switches over all of them."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty(_OWNED, True)
        self._default = profiles.clean({}, named=False)
        self._named = []
        self._filling = False
        self._was_unavailable = False
        self._site_question = None
        self._site_target = None
        self._countdown = 0
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick)
        # Fills the pane rather than growing with the editor: the editor
        # scrolls on its own, so the list beside it stays in view.
        layout = widgets.FitColumn(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # The profile dictation uses is marked in the list and chosen beside
        # the editor's title, so editing a profile and using it are two
        # separate things in one place. "" is the Default profile, or picking
        # by the program in front when that is on.
        self._override = ""
        # What this desktop cannot tell about the program in front: one notice
        # for the whole page. Hidden while there is nothing to say.
        self.detection = widgets.Notice()
        self.detection.set(self.detection_text())
        layout.addWidget(self.detection)

        self.use_style = QCheckBox(t("Use profile styles"))
        self.use_style.setToolTip(t(
            "Adds the profile's preferences to the dictation cleanup "
            "instruction. A cleanup instruction of your own replaces the "
            "built-in one, and the style is still added after it; switch this "
            "off to use your own instruction alone. Applies to every profile."))
        self.by_app = QCheckBox(t("Pick by program in front"))
        self.by_app.setToolTip(t(
            "The program in front is looked up when a recording starts. "
            "When it cannot be found out or no profile lists it, the Default "
            "profile is used, and the history names the profile and the program."))
        self.translation = QCheckBox(t("Enable translation"))
        self.translation.setToolTip(t(
            "The translation is made after the recording, by the cleanup "
            "model, before the text is pasted. It is not live. If it fails, "
            "the original transcript is copied and nothing is pasted."))
        self.style_learning = QCheckBox(t("Use learned styles"))
        self.style_learning.setToolTip(t(
            "Paste something you wrote, or a dictation and the way you "
            "corrected it. It is read on this computer and not kept: only the "
            "preferences found in it are, shown above, and a choice you made "
            "yourself always wins over a learned one."))
        holder = QWidget()
        switches = self.switches = QGridLayout(holder)
        switches.setContentsMargins(18, 2, 0, 4)
        switches.setHorizontalSpacing(18)
        switches.setVerticalSpacing(8)
        self.switch_boxes = (self.use_style, self.translation,
                             self.style_learning, self.by_app)
        for box in self.switch_boxes:
            box.toggled.connect(self._switched)
        self._place_switches(2)
        switches.setColumnStretch(2, 1)
        # The switches over every profile, set once and rarely touched again,
        # folded away: above the list and editor when they are stacked, under
        # the list when they sit side by side (_arranged), so the list and
        # the editor start at one top. Opened by itself when one of them
        # being off is what greys out a part of the profile being edited.
        self.everyone = widgets.Disclosure(t("Profile options"), holder)
        layout.addWidget(self.everyone)

        # The list of profiles, with its three actions in one row under it.
        self.list = QListWidget()
        self.list.setAccessibleName(t("Profiles"))
        self.list.setToolTip(t(
            "A profile says how a dictation should read, and can translate it. "
            "The Default profile is used unless a named profile lists the "
            "program in front when the recording starts. Files, meetings and "
            "the assistant are never changed by a profile."))
        self.list.currentRowChanged.connect(self._selected)
        # A name with its programs under it, muted, as the history lists them,
        # its initial in the badge and a check on the one in use.
        widgets.row_list(self.list, lines=1, marks=True)
        self.list.setProperty("card", True)
        self.list.setMinimumHeight(110)
        self.list.setMaximumHeight(320)
        self.add_button = widgets.GlyphButton("plus", t("Add profile…"))
        self.add_button.clicked.connect(self._add)
        self.rename_button = widgets.GlyphButton("edit", t("Rename…"))
        self.rename_button.clicked.connect(self._rename)
        self.remove_button = widgets.GlyphButton("trash", t("Remove"), "danger")
        self.remove_button.clicked.connect(self.remove_current)
        side = self.side = QWidget()
        side_column = self.side_column = QVBoxLayout(side)
        side_column.setContentsMargins(0, 0, 0, 0)
        side_column.setSpacing(6)
        # The list's actions head it, on the line of the editor's title.
        self.list_header = QWidget()
        toolbar = QHBoxLayout(self.list_header)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        for button in (self.add_button, self.rename_button, self.remove_button):
            toolbar.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        toolbar.addStretch(1)
        side_column.addWidget(self.list_header)
        # Side by side, as tall as the editor's section heading, so the list
        # and the first card start at one top (_align_side).
        self.list_gap = QWidget()
        self.list_gap.setFixedHeight(0)
        side_column.addWidget(self.list_gap)
        side_column.addWidget(self.list)
        side_column.addStretch(1)

        self.editor_title = QLabel()
        self.editor_title.setObjectName("editortitle")
        self.editor_title.setTextFormat(Qt.TextFormat.PlainText)
        self.use_button = QPushButton(t("Use this profile"))
        self.use_button.setAutoDefault(False)
        self.use_button.clicked.connect(self.use_current)
        self.title_bar = QWidget()
        title_row = QHBoxLayout(self.title_bar)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(12)
        title_row.addWidget(self.editor_title, 1)
        title_row.addWidget(self.use_button, 0, Qt.AlignmentFlag.AlignVCenter)

        # Matching: when this profile is the one used. Its rules live in a form
        # of their own, so that where nothing can say which program is in
        # front they fold behind one row and can still be looked at.
        self.matching, matching_form = widgets.section(t("Matching"))
        self.rules = QWidget()
        form = QFormLayout(self.rules)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(widgets.ROW_GAP)
        form.setHorizontalSpacing(14)
        self.rules_disclosure = widgets.Disclosure(
            t("Saved rules (not used on this desktop)"), self.rules)
        matching_form.addRow(self.rules_disclosure)
        self.apps = QLineEdit()
        self.apps.setPlaceholderText(t("e.g. thunderbird, outlook"))
        self.apps.setToolTip(t(
            "Program names, separated by commas, the way Find the program name "
            "shows them. A browser counts as one program whatever site is open "
            "in it."))
        self.apps.textEdited.connect(self._edited)
        self.apps_label = QLabel(t("Programs"))
        self.find_button = QToolButton()
        self.find_button.setObjectName("tool")
        self.find_button.setText(t("Find…"))
        self.find_button.setToolTip(t(
            "Switch to the program within three seconds; its name is shown "
            "here, ready to be listed in a profile."))
        self.find_button.setAccessibleName(t("Find the program name"))
        self.find_button.clicked.connect(self._find_program)
        programs = QHBoxLayout()
        programs.setContentsMargins(0, 0, 0, 0)
        programs.addWidget(self.apps, 1)
        programs.addWidget(self.find_button)
        programs_box = self.programs_box = QWidget()
        programs_box.setLayout(programs)
        form.addRow(self.apps_label, programs_box)
        self.found = _muted()
        form.addRow(QLabel(), self.found)

        # Sites: keywords looked for in a browser's window title. A list of
        # their own rather than a line like the programs, because one is
        # usually picked up from the tab rather than typed. One way in: the
        # field, which the pick button can also fill from the tab in front.
        self.sites = QListWidget()
        self.sites.setItemDelegate(_RemovableDelegate(self.sites, self._remove_site_at))
        self.sites.viewport().installEventFilter(self.sites.itemDelegate())
        self.sites.setMouseTracking(True)
        self.sites.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.sites.itemChanged.connect(self._site_renamed)
        remove_key = QShortcut(QKeySequence.StandardKey.Delete, self.sites)
        remove_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        remove_key.activated.connect(self.remove_selected_sites)
        self.site_edit = QLineEdit()
        self.site_edit.setPlaceholderText(t("e.g. WhatsApp, Gmail, YouTube"))
        self.site_edit.returnPressed.connect(self._add_typed_site)
        self.add_site_button = QPushButton(t("Add"))
        self.add_site_button.clicked.connect(self._add_typed_site)
        self.read_site_button = QToolButton()
        self.read_site_button.setObjectName("tool")
        self.read_site_button.setText(t("From tab…"))
        self.read_site_button.setToolTip(t(
            "Counts down three seconds: switch to the browser tab in the "
            "meantime, and the site is read from its title."))
        self.read_site_button.clicked.connect(self.start_site_countdown)
        self.site_status = _muted()
        self.site_status.hide()
        self.sites_note = _muted(self.sites_text())
        self.sites.setToolTip(self.sites_tooltip())
        self.sites_note.setToolTip(self.sites_tooltip())
        sites = QVBoxLayout()
        sites.setSpacing(6)
        typed = QHBoxLayout()
        typed.addWidget(self.site_edit, 1)
        typed.addWidget(self.add_site_button)
        typed.addWidget(self.read_site_button)
        sites.addLayout(typed)
        sites.addWidget(self.sites)
        sites.addWidget(self.site_status)
        sites.addWidget(self.sites_note)
        self.sites_label = QLabel(t("Sites"))
        self.sites_box = QWidget()
        sites.setContentsMargins(0, 0, 0, 0)
        self.sites_box.setLayout(sites)
        form.addRow(self.sites_label, self.sites_box)
        self.editor_form = form

        # Writing: how the text of this profile reads.
        self.writing, form = widgets.section(t("Writing"))
        self.style_boxes = {}
        for key, (title, values) in STYLE_LABELS.items():
            box = QComboBox()
            box.addItem(t("No preference"), "")
            for value in profiles.STYLE[key]:
                box.addItem(t(values[value]), value)
            box.currentIndexChanged.connect(self._edited)
            self.style_boxes[key] = box
            form.addRow(t(title), box)
        self.instruction = QLineEdit()
        self.instruction.setPlaceholderText(t("e.g. Write numbers as digits"))
        self.instruction.textEdited.connect(self._edited)
        form.addRow(t("Extra instructions"), self.instruction)
        self.target = QComboBox()
        self.target.addItem(t("Do not translate"), "")
        for code, english, _turkish in profiles.LANGUAGES:
            self.target.addItem(t(english), code)
        self.target.currentIndexChanged.connect(self._edited)
        form.addRow(t("Translate into"), self.target)
        self.learned = _muted()
        self.reset_learned = QPushButton(t("Reset learned style"))
        self.reset_learned.clicked.connect(self.reset_learned_style)
        form.addRow(QLabel(), self.learned)
        form.addRow(QLabel(), self.reset_learned)
        self.writing_form = form

        # Learning from examples: optional, so collapsed until wanted. Its
        # switch stays outside the part it greys out.
        self.teach = QWidget()
        teach = QVBoxLayout(self.teach)
        teach.setContentsMargins(0, 0, 0, 0)
        self.teach.setToolTip(self.style_learning.toolTip())
        self.examples = QPlainTextEdit()
        self.examples.setPlaceholderText(t("Examples of your own writing"))
        self.examples.setToolTip(t("Paste your own writing; only the preferences "
                                   "found in it are kept."))
        self.examples.setMaximumHeight(90)
        teach.addWidget(self.examples)
        self.before = QPlainTextEdit()
        self.before.setPlaceholderText(t("What Dikte wrote (optional)"))
        self.before.setMaximumHeight(60)
        self.after = QPlainTextEdit()
        self.after.setPlaceholderText(t("What you corrected it to (optional)"))
        self.after.setMaximumHeight(60)
        pair = QHBoxLayout()
        pair.addWidget(self.before)
        pair.addWidget(self.after)
        teach.addLayout(pair)
        self.learn_button = QPushButton(t("Learn from these"))
        self.learn_button.clicked.connect(self.learn_from_examples)
        self.learn_result = _muted()
        teach.addLayout(_row(self.learn_button, self.learn_result))
        learning = QWidget()
        learning_column = QVBoxLayout(learning)
        learning_column.setContentsMargins(14, 2, 0, 0)
        learning_column.addWidget(self.teach)
        self.learning = widgets.Disclosure(t("Learn the style from examples"), learning)

        detail = self.editor = QWidget()
        detail_column = QVBoxLayout(detail)
        detail_column.setContentsMargins(0, 0, 0, 0)
        detail_column.setSpacing(4)
        detail_column.addWidget(self.title_bar)
        detail_column.addWidget(self.writing)
        detail_column.addSpacing(10)
        detail_column.addWidget(self.learning)
        detail_column.addWidget(self.matching)
        detail_column.addStretch(1)
        # One label column for both sections, the unlabelled rows included,
        # so a section whose labelled rows are hidden keeps its field column.
        labels = [form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                  for form in (self.editor_form, self.writing_form)
                  for row in range(form.rowCount())]
        labels = [item.widget() for item in labels if item is not None and item.widget()]
        width = max((label.sizeHint().width() for label in labels), default=0)
        for label in labels:
            label.setMinimumWidth(width)
        # The editor scrolls by itself, so the list beside it stays put.
        scroller = self.editor_scroll = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroller.setWidget(detail)
        scroller.setMinimumHeight(220)
        self.split = widgets.Split(side, scroller, side=260, threshold=660,
                                   first_stacked_max=200)
        # Side by side or stacked changes the room the list has, and where
        # the profile options go.
        self.split.arranged = self._arranged
        layout.addSpacing(4)
        layout.addWidget(self.split, 1)
        # Side by side, the list follows the editor's cards: its top and
        # bottom are measured from them whenever they move.
        self._aligning = False
        for watched in (detail, scroller.viewport(), side, self.title_bar,
                        self.writing.frame, self.matching.frame):
            watched.installEventFilter(self)

        self._find_timer = QTimer(self)
        self._find_timer.setSingleShot(True)
        self._find_timer.timeout.connect(self._show_found)
        _relay().found.connect(self._found)
        _relay().titled.connect(self._titled)
        _relay().probed.connect(self._probed)
        if (frontapp.backend() is frontapp._gnome
                and frontapp.gnome_extension["present"] is None
                and not _PROBE["started"]):
            # Asked once a session, off this thread, so the note can say
            # whether the extension is there rather than guess from a folder
            # name. An answer that never comes is not asked for again by
            # every editor that opens after it.
            _PROBE["started"] = True
            relay = _relay()
            threading.Thread(target=lambda: relay.probed.emit(frontapp.probe_gnome()),
                             daemon=True).start()
        self._asked = 0

    # ---- the form ---------------------------------------------------------

    def load(self, conf):
        self._filling = True
        self._default = profiles.default_profile(conf)
        self._named = profiles.named_profiles(conf)
        self.use_style.setChecked(bool(conf["profile_style"]))
        self.by_app.setChecked(bool(conf["profile_by_app"]))
        self.translation.setChecked(bool(conf["translation_enabled"]))
        self.style_learning.setChecked(bool(conf["style_learning"]))
        chosen = conf["profile_override"] if isinstance(conf["profile_override"], str) else ""
        # Matched without regard to case, as profiles.override() matches it.
        self._override = next((name for name in self.profile_names()
                               if chosen and name.casefold() == chosen.casefold()), "")
        self._fill_list(0)
        self._filling = False
        self._selected(self.list.currentRow())
        self._sync_switches()

    def values(self):
        # Copies: the window keeps the last saved values to compare with, and
        # the profiles are edited in place.
        return copy.deepcopy([self._default, self._named, self.use_style.isChecked(),
                self.by_app.isChecked(), self.translation.isChecked(),
                self.style_learning.isChecked(), self._override])

    def store(self, conf):
        # Only what differs: an Apply that touched no profile leaves them
        # stored the way they were, rather than rewritten in their clean form.
        if self._default != profiles.default_profile(conf):
            conf["profile_default"] = copy.deepcopy(self._default)
        if self._named != profiles.named_profiles(conf):
            conf["profiles"] = copy.deepcopy(self._named)
        conf["profile_style"] = self.use_style.isChecked()
        conf["profile_by_app"] = self.by_app.isChecked()
        conf["translation_enabled"] = self.translation.isChecked()
        conf["style_learning"] = self.style_learning.isChecked()
        conf["profile_override"] = self._override

    @staticmethod
    def detection_text():
        """What this session can tell about the program in front."""
        backend = frontapp.backend()
        if backend is frontapp._unknown:
            return t("Per-application profiles are not available on this desktop.")
        if backend is frontapp._kde_wayland and not frontapp.shutil.which("kdotool"):
            return t("On KDE Plasma under Wayland, per-application profiles need "
                     "kdotool installed.")
        if backend is frontapp._x11 and not frontapp.shutil.which("xprop"):
            return t("On X11, per-application profiles need xprop installed.")
        if backend is frontapp._gnome and frontapp.gnome_extension["present"] is False:
            return t("On GNOME, per-application profiles need the “Window "
                     "Calls” GNOME Shell extension installed.")
        # Nothing to warn about: how the lookup works is the by-app tooltip.
        return ""

    @staticmethod
    def sites_text():
        """The one line shown under the sites."""
        if frontapp.backend() is frontapp._macos:
            return t("Site matching is not available on macOS.")
        return t("Matches the site's name in the browser tab's title.")

    @staticmethod
    def sites_tooltip():
        """What reading a site involves, in full."""
        if frontapp.backend() is frontapp._macos:
            return t("Site matching is not available on macOS: reading another "
                     "program's window title needs a permission Dikte does not "
                     "ask for. Programs still match.")
        return t("When a dictation starts in a browser, its window title is read "
                 "once to match these sites. A site matches the site's name in "
                 "the title, not the text of the page. Titles are never saved: "
                 "the history keeps only the site that matched. A site wins over "
                 "a program, so a browser listed above is used for every other "
                 "site.")

    # ---- sites ------------------------------------------------------------

    def add_site(self, keyword, profile=None):
        """Add a keyword to a named profile (the selected one by default);
        False when it is empty, already there, or the profile is Default."""
        profile = self.current() if profile is None else profile
        if profile is self._default or not any(profile is p for p in self._named):
            return False
        keyword = " ".join((keyword or "").split())[:60]
        if not keyword or profiles.fold(keyword) in (
                profiles.fold(s) for s in profile["sites"]):
            return False
        profile["sites"] = profile["sites"] + [keyword]
        if profile is self.current():
            self._fill_sites(profile)
        self.changed.emit()
        return True

    def remove_selected_sites(self):
        profile = self.current()
        rows = {self.sites.row(item) for item in self.sites.selectedItems()}
        if profile is self._default or not rows:
            return
        profile["sites"] = [s for i, s in enumerate(profile["sites"]) if i not in rows]
        self._fill_sites(profile)
        self.changed.emit()

    def _remove_site_at(self, row):
        """The x on a site's row."""
        profile = self.current()
        if profile is self._default or not 0 <= row < len(profile["sites"]):
            return
        profile["sites"] = [s for i, s in enumerate(profile["sites"]) if i != row]
        self._fill_sites(profile)
        self.changed.emit()

    def _add_typed_site(self):
        if self.add_site(self.site_edit.text()):
            self.site_edit.clear()
            self._status("")

    def _site_renamed(self, item):
        """A keyword edited in place in the list."""
        if self._filling:
            return
        profile = self.current()
        if profile is self._default:
            return
        sites = [self.sites.item(i).text() for i in range(self.sites.count())]
        profile["sites"] = profiles.clean({"sites": sites})["sites"]
        self._fill_sites(profile)
        self.changed.emit()

    def start_site_countdown(self):
        """Give the user three seconds to bring the tab forward, counting them
        out loud, then read the window in front."""
        if self.current() is self._default:
            return
        self._site_target = self.current()
        self._countdown = 3
        self.read_site_button.setEnabled(False)
        self._show_countdown()
        self._countdown_timer.start()

    def _show_countdown(self):
        self._status(t("Switch to the tab… {seconds}",
                       seconds=self._countdown))

    def _tick(self):
        self._countdown -= 1
        if self._countdown > 0:
            self._show_countdown()
            return
        self._countdown_timer.stop()
        self._status(t("Reading the window in front…"))
        self._read_site()

    def _read_site(self):
        """Off the main thread, like Find the program name, and answered
        through the same relay."""
        self._site_question = next(_QUESTIONS)
        asked, relay = self._site_question, _relay()

        def work():
            program, title = frontapp.front(None, profiles.is_browser)
            relay.titled.emit(asked, program, title)

        threading.Thread(target=work, daemon=True).start()

    def _titled(self, asked, program, title):
        if asked != self._site_question:
            return
        self._site_question = None
        # Only back on for a named profile: Default has no sites to add to.
        self.read_site_button.setEnabled(self.current() is not self._default)
        target, self._site_target = self._site_target, None
        if target is None or not any(target is p for p in self._named):
            # Removed, or the settings reloaded, while the countdown ran.
            self._status(t("The profile changed meanwhile; nothing was added."))
            return
        if not program:
            self._status(t("The program in front could not be found out."))
            return
        if not profiles.is_browser(program):
            self._status(t("{name} is not a browser Dikte knows.", name=program),
                         t("List it under Programs instead."))
            return
        keyword = profiles.propose_site(title)
        if not keyword:
            # A blank tab, or a title that is only the browser's own name:
            # never add the browser as a site.
            self._status(t("No site in the title; open the site's tab first."),
                         t("Read from {browser}.", browser=program))
            return
        # Shown so the user can see where the keyword came from. Only on
        # screen: the title is not stored anywhere.
        added = target is not None and self.add_site(keyword, target)
        read = t("Read from {browser}: “{title}”. Double-click a site to change it.",
                 browser=program, title=title)
        self._status(t("Added “{site}”.", site=keyword) if added
                     else t("“{site}” is already listed.", site=keyword), read)

    def _status(self, line, detail=""):
        """The one line under the sites, with what lies behind it in its
        tooltip. A tooltip has no plain-text mode and draws anything that
        looks like markup as markup, and the detail carries a web page's own
        title, so it is escaped and marked as rich text to show literally."""
        self.site_status.setText(line)
        self.site_status.setVisible(bool(line))
        self.site_status.setToolTip(
            "<qt>" + html.escape(detail) + "</qt>" if detail else "")

    def _fill_sites(self, profile):
        filling, self._filling = self._filling, True
        self.sites.clear()
        for keyword in profile.get("sites", []):
            item = QListWidgetItem(keyword)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.sites.addItem(item)
        self._filling = filling
        self._fit_sites()

    def _fit_sites(self):
        """The site list as tall as its rows, one row at least and four at
        most, rather than a fixed box with one site floating at the top."""
        row = self.sites.sizeHintForRow(0) if self.sites.count() else 0
        if row <= 0:
            row = self.sites.fontMetrics().height() + 12
        rows = min(4, max(1, self.sites.count()))
        frame = self.sites.frameWidth() * 2 + 12
        self.sites.setFixedHeight(row * rows + frame)

    # ---- the profiles -----------------------------------------------------

    def profile_names(self):
        return [p["name"] for p in self._named]

    def _taken(self, name, skip=None):
        """Whether a name would be confused with another profile, or with
        Default in either language."""
        key = name.casefold()
        if key in ("default", t("Default").casefold()):
            return True
        return any(n.casefold() == key for i, n in enumerate(self.profile_names())
                   if i != skip)

    def add_profile(self, name):
        name = " ".join((name or "").split())[:60]
        if not name or self._taken(name):
            return False
        self._named.append(profiles.clean({"name": name, "id": profiles.new_id()}))
        self._fill_list(len(self._named))
        self.changed.emit()
        return True

    def rename_current(self, name):
        index = self.list.currentRow() - 1
        name = " ".join((name or "").split())[:60]
        if index < 0 or not name:
            return False
        if self._taken(name, skip=index):
            return False
        old = self._named[index]["name"]
        self._named[index]["name"] = name
        if self._override == old:
            self._override = name
        self._fill_list(index + 1)
        self.changed.emit()
        return True

    def remove_current(self):
        index = self.list.currentRow() - 1
        if index < 0:
            return False
        gone = self._named.pop(index)
        # Removing the profile in use falls back to the Default, or to
        # picking by program where that is on.
        if self._override == gone["name"]:
            self._override = ""
        self._fill_list(min(index + 1, len(self._named)))
        self.changed.emit()
        return True

    def current(self):
        """The profile being edited: the Default one at row 0."""
        index = self.list.currentRow()
        if index <= 0 or index > len(self._named):
            return self._default
        return self._named[index - 1]

    def select(self, row):
        self.list.setCurrentRow(row)

    def reset_learned_style(self):
        profile = self.current()
        if profile["learned"]:
            profile["learned"] = {}
            self._selected(self.list.currentRow())
            self.changed.emit()

    def learn_from_examples(self):
        """Read the pasted text into the selected profile's learned style, then
        empty the boxes: the text was lent for this, not given to keep. Kept
        when nothing was found in it, for the user to add to."""
        before = self.before.toPlainText().strip()
        after = self.after.toPlainText().strip()
        corrections = [(before, after)] if before and after else []
        found = profiles.learn_style([self.examples.toPlainText()], corrections)
        if not found:
            # Left in place, so that a few more sentences can be added to them.
            self.learn_result.setText(t("Not enough to learn from; paste a few "
                                        "more sentences."))
            return {}
        for box in (self.examples, self.before, self.after):
            box.clear()
        profile = self.current()
        profile["learned"] = dict(profile["learned"], **found)
        self.learn_result.setText(t("Learned: {preferences}",
                                    preferences=style_summary(found)))
        self._selected(self.list.currentRow())
        self.changed.emit()
        return found

    def _list_item(self, item, profile):
        """A profile's row: its name, and what it is matched by as muted meta."""
        if profile is self._default:
            name, meta = t("Default"), t("When nothing else matches")
        else:
            name = profile["name"]
            meta = ", ".join(profile.get("apps", []) + profile.get("sites", []))
            meta = meta or t("Chosen by hand")
        active = self.active_profile() is profile
        item.setText(name)
        item.setToolTip(widgets.meta_line(meta, t("In use") if active else ""))
        item.setData(Qt.ItemDataRole.AccessibleTextRole,
                     widgets.meta_line(name, meta, t("In use") if active else ""))
        item.setData(widgets.BODY_ROLE, name)
        item.setData(widgets.META_ROLE, meta)
        item.setData(widgets.KIND_ROLE, "profile")
        item.setData(widgets.LETTER_ROLE, name)
        item.setData(widgets.CHECK_ROLE, active)

    def _fill_list(self, row):
        with_signals = self.list.blockSignals(True)
        self.list.clear()
        for profile in [self._default] + self._named:
            item = QListWidgetItem()
            self._list_item(item, profile)
            self.list.addItem(item)
        self.list.blockSignals(with_signals)
        QTimer.singleShot(0, self._fit_list)
        self.list.setCurrentRow(max(0, min(row, len(self._named))))
        self._selected(self.list.currentRow())

    def _place_switches(self, columns):
        """The profile options in `columns` columns: two across the page,
        one in the narrow column under the list."""
        for box in self.switch_boxes:
            self.switches.removeWidget(box)
        for index, box in enumerate(self.switch_boxes):
            self.switches.addWidget(box, index // columns, index % columns)

    def _arranged(self, wide):
        """Profile options under the list beside the editor, and above both
        when they are stacked; the list sized again for its new room."""
        self.layout().removeWidget(self.everyone)
        self.side_column.removeWidget(self.everyone)
        if wide:
            self.side_column.insertWidget(self.side_column.indexOf(self.list) + 1,
                                          self.everyone)
            self._place_switches(1)
            # On the line of the editor's disclosure under its first card.
            self.everyone.setContentsMargins(0, 12, 0, 0)
            self.list.setMaximumHeight(16777215)
        else:
            self.layout().insertWidget(self.layout().indexOf(self.detection) + 1,
                                       self.everyone)
            self._place_switches(2)
            self.everyone.setContentsMargins(0, 0, 0, 0)
            self.list_gap.setFixedHeight(0)
            self.list_header.setMinimumHeight(0)
            self.list_header.setMaximumHeight(16777215)
            # The wide height came from the editor; _fit_list sizes it again.
            self.list.setMinimumHeight(110)
            self.list.setMaximumHeight(320)
        self.everyone.show()
        QTimer.singleShot(0, self._fit_list)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show,
                            QEvent.Type.Hide, QEvent.Type.LayoutRequest):
            self._queue_align()
        return super().eventFilter(watched, event)

    def _queue_align(self):
        if not self._aligning:
            self._aligning = True
            QTimer.singleShot(0, self._align_side)

    def _align_side(self):
        """Side by side, the list's actions on the line of the editor's
        title, the list's top on the first card's top and its bottom on the
        last card's bottom (as far as the pane has room for it)."""
        self._aligning = False
        try:
            if not self.split.wide:
                return
            self.list_header.setFixedHeight(max(self.title_bar.height(),
                                                self.list_header.sizeHint().height()))
            viewport = self.editor_scroll.viewport()
            # Measured in the editor's own coordinates, so a scrolled editor
            # does not drag the list about.
            origin = viewport.mapTo(self, QPoint(0, 0)).y()
            cards = [box.frame for box in (self.writing, self.matching)
                     if not box.isHidden()]
            if not cards:
                return
            top = origin + cards[0].mapTo(self.editor, QPoint(0, 0)).y()
            last = cards[-1]
            bottom = origin + last.mapTo(self.editor, QPoint(0, last.height())).y()
            bottom = min(bottom, origin + viewport.height())
            # The header, the gap and the list follow one another down the
            # side, a layout spacing apart.
            above = (self.side.mapTo(self, QPoint(0, 0)).y() + self.list_header.height()
                     + 2 * self.side_column.spacing())
            self.list_gap.setFixedHeight(max(0, top - above))
            list_top = above + self.list_gap.height()
            # Room for the options under it, opened or not.
            below = self.everyone.sizeHint().height() + self.side_column.spacing()
            room = self.side.mapTo(self, QPoint(0, self.side.height())).y() - below - list_top
            height = max(110, min(bottom - list_top, room))
            self.list.setMinimumHeight(height)
            self.list.setMaximumHeight(height)
        except RuntimeError:
            pass

    def _fit_list(self):
        """The list as tall as its profiles, up to its cap, so a handful of
        them never hide behind a scroll bar. Stacked above the editor, the
        pane holding it is capped too, and the list then leaves room in it
        for its row of buttons rather than pushing them out of the pane.
        Beside the editor it follows the editor's cards instead."""
        if self.split.wide:
            self._queue_align()
            return
        try:
            rows = sum(self.list.sizeHintForRow(i) for i in range(self.list.count()))
            room = self.list.maximumHeight()
            side = self.list.parentWidget()
            if side is not None and side.maximumHeight() < 16777215:
                below = self.add_button.sizeHint().height() + side.layout().spacing()
                room = min(room, side.maximumHeight() - below)
            self.list.setMinimumHeight(max(60, min(room, max(110, rows + self.list.frameWidth() * 2 + 14))))
        except RuntimeError:
            pass

    @staticmethod
    def matching_available():
        """Whether this desktop can say which program is in front at all."""
        return frontapp.backend() is not frontapp._unknown

    def _automatic(self):
        """Whether the empty choice picks a profile by the program in front,
        or only ever means the Default profile."""
        return self.matching_available() and self.by_app.isChecked()

    def active_profile(self):
        """The profile dictation uses unless the program in front picks one."""
        return next((profile for profile in self._named
                     if profile["name"] == self._override), self._default)

    def use_current(self):
        """Make the profile being edited the one dictation uses."""
        profile = self.current()
        self.use_profile("" if profile is self._default else profile["name"])

    def use_profile(self, name):
        """Make the named profile the one dictation uses, or with "" go back
        to Default (or to picking by the program in front, where that is on).
        What Use this profile does, and the home window's profile chip."""
        if name and name not in self.profile_names():
            name = ""
        if name == self._override:
            return
        self._override = name
        self._mark_active()
        self.changed.emit()

    def automatic(self):
        """Whether no override means picking by the program in front."""
        return self._automatic() and any(
            profile.get("apps") or profile.get("sites") for profile in self._named)

    def _mark_active(self):
        """The check in the list and the button beside the title, again."""
        for row, profile in enumerate([self._default] + self._named):
            item = self.list.item(row)
            if item is not None:
                self._list_item(item, profile)
        self._sync_use_button()

    def _sync_use_button(self):
        current = self.current()
        active = current is self.active_profile()
        self.use_button.setText(t("In use") if active else t("Use this profile"))
        self.use_button.setEnabled(not active)
        if current is self._default and self._automatic():
            tip = t("Dictation picks a profile by the program in front, and "
                    "this one when none of them lists it.")
        elif active:
            tip = t("Dictation uses this profile.")
        else:
            tip = t("Use this profile for every dictation.")
        self.use_button.setToolTip(tip)

    def _open_options_if_needed(self, profile):
        """Open Profile options when a switch in it, being off, is what
        greys out a part of `profile`: the reason is then in view."""
        blocked = ((profile["style"] or profile["instruction"])
                   and not self.use_style.isChecked())
        blocked = blocked or (profile["translate_to"] and not self.translation.isChecked())
        blocked = blocked or (profile["learned"] and not self.style_learning.isChecked())
        blocked = blocked or (profile is not self._default and self.matching_available()
                              and (profile.get("apps") or profile.get("sites"))
                              and not self.by_app.isChecked())
        if blocked:
            self.everyone.set_expanded(True)

    def _selected(self, _row=None):
        profile = self.current()
        named = profile is not self._default
        self._filling = True
        self.editor_title.setText(t("Edit {name}", name=profile["name"] if named
                                    else t("Default")))
        self.apps.setText(", ".join(profile.get("apps", [])))
        self.apps.setCursorPosition(0)
        matching = self.matching_available()
        self.apps.setEnabled(named and matching)
        self.apps_label.setEnabled(named and matching)
        self.find_button.setEnabled(named and matching)
        # Default is what is used when nothing matches: it has nothing to be
        # matched by, so no Matching box at all. Where matching cannot work,
        # the rules fold behind one row and stay there to be looked at.
        self.matching.setVisible(named)
        self.rules_disclosure.button.setVisible(not matching)
        if matching:
            self.rules_disclosure.set_expanded(True)
        elif not self._was_unavailable:
            self.rules_disclosure.set_expanded(False)
        self._was_unavailable = not matching
        self._fill_sites(profile)
        for widget in (self.sites, self.site_edit, self.add_site_button,
                       self.sites_label):
            widget.setEnabled(named and matching)
        self._show_found_row()
        # Not while a countdown or a reading is under way: a second one would
        # start on top of it.
        busy = self._countdown_timer.isActive() or self._site_question is not None
        self.read_site_button.setEnabled(named and matching and not busy)
        for key, box in self.style_boxes.items():
            index = box.findData(profile["style"].get(key, ""))
            box.setCurrentIndex(max(0, index))
        self.instruction.setText(profile["instruction"])
        # From the start: scrolled to its end, a long line hides its first
        # letters behind the frame. Again once it has its width, which it
        # may not have yet.
        self.instruction.setCursorPosition(0)
        # A bound method rather than a lambda: Qt drops the call if the editor
        # is deleted first, and a lambda would reach into a deleted widget.
        QTimer.singleShot(0, self._instruction_from_start)
        self.target.setCurrentIndex(max(0, self.target.findData(profile["translate_to"])))
        learned = style_summary(profile["learned"])
        self.learned.setText(t("Learned from your examples: {preferences}",
                               preferences=learned) if learned else "")
        self.writing_form.setRowVisible(self.learned, bool(learned))
        self.writing_form.setRowVisible(self.reset_learned, bool(learned))
        self.rename_button.setEnabled(named)
        self.remove_button.setEnabled(named)
        self._sync_use_button()
        self._open_options_if_needed(profile)
        self._filling = False

    def _instruction_from_start(self):
        if not self.instruction.hasFocus():
            self.instruction.setCursorPosition(0)

    def _edited(self, *_):
        if self._filling:
            return
        profile = self.current()
        if profile is not self._default:
            profile["apps"] = profiles.clean({"apps": self.apps.text()})["apps"]
            item = self.list.currentItem()
            if item is not None:
                self._list_item(item, profile)
        profile["style"] = {key: box.currentData()
                            for key, box in self.style_boxes.items()
                            if box.currentData()}
        profile["instruction"] = self.instruction.text().strip()[:2000]
        profile["translate_to"] = self.target.currentData() or ""
        self.changed.emit()

    def _switched(self, *_):
        self._sync_switches()
        if not self._filling:
            self._sync_use_button()
            self.changed.emit()

    def _sync_switches(self):
        # Never shown checked but greyed out: where the program in front
        # cannot be told, the switch is left out and the notice says why. Its
        # stored value is kept for a desktop that can.
        self.by_app.setVisible(self.matching_available())
        for box in self.style_boxes.values():
            box.setEnabled(self.use_style.isChecked())
        self.instruction.setEnabled(self.use_style.isChecked())
        self.target.setEnabled(self.translation.isChecked())
        self.teach.setEnabled(self.style_learning.isChecked())

    def _add(self):
        name, ok = QInputDialog.getText(self, t("Add profile"), t("Name"))
        if ok and not self.add_profile(name):
            QMessageBox.information(self, t("Profiles"), t(
                "A profile needs a name of its own."))

    def _rename(self):
        profile = self.current()
        if profile is self._default:
            return
        name, ok = QInputDialog.getText(self, t("Rename profile"), t("Name"),
                                        text=profile["name"])
        if ok and not self.rename_current(name):
            QMessageBox.information(self, t("Profiles"), t(
                "A profile needs a name of its own."))

    def _show_found_row(self):
        """The line under Programs only while it has something to say."""
        named = self.current() is not self._default
        self.editor_form.setRowVisible(self.found, named and bool(self.found.text()))

    def _find_program(self):
        self.found.setText(t("Switch to the program now…"))
        self._show_found_row()
        self._find_timer.start(3000)

    def _show_found(self):
        """Ask on a thread of its own: the asking is up to three subprocesses,
        and this is the main thread. The answer comes back through a signal."""
        self._asked = next(_QUESTIONS)
        asked, relay = self._asked, _relay()

        def work():
            relay.found.emit(asked, frontapp.program_name())

        threading.Thread(target=work, daemon=True).start()

    def _probed(self, _present):
        try:
            # Hidden while it has nothing to say; shown once the answer names
            # something missing, even if the editor was built before it came.
            self.detection.set(self.detection_text())
        except RuntimeError:
            pass  # an editor already deleted on the C++ side

    def _found(self, asked, name):
        # Only this editor's newest question: a slow earlier one must not land
        # on top of it, and another editor's is not this one's to show.
        if asked != self._asked:
            return
        self.found.setText(t("In front: {name}", name=name) if name
                           else t("The program in front could not be found out."))
        self._show_found_row()
