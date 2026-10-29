"""The window a question about selected text answers in.

Three parts kept apart on screen, the way they were kept apart in the prompt:
what was said, what it was said about, and what came back. Apply is there only
for an edit of a selection that can be pasted back; Copy is always there; and
Cancel, Escape or closing the window leave the original text exactly as it was.
Nothing is pasted until Apply is pressed.
"""

import html
import itertools
import threading

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                             QPushButton, QVBoxLayout)

from . import paste
from . import selection
from . import theme
from .i18n import t

# How much of the selection the tooltip shows. The whole of it went to the
# agent; this is only there to say which text that was.
PREVIEW_CHARS = 1500


def _tip(text):
    """A tooltip that shows `text` as typed: Qt reads tooltips as rich text,
    so it is escaped, and pre-wrap keeps its line breaks."""
    return ("<qt><div style='white-space:pre-wrap'>" + html.escape(text)
            + "</div></qt>")


def one_line(text, limit=90):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class AnswerBridge(QObject):
    """Where Apply and Copy threads report, by token, never by window.

    One for the life of the application, made on the interface thread. A
    thread holds this and a number, not the window: a window can then be
    closed and deleted the moment a newer answer replaces it, always on the
    interface thread, and a result arriving for it afterwards finds nobody
    listening under that token and goes to whoever reports errors instead.
    """

    apply_finished = pyqtSignal(int, str)   # token, error or ''
    copy_finished = pyqtSignal(int, str)


_shared = None


def shared_bridge():
    """The application's bridge, made on first use (on the interface thread)."""
    global _shared
    if _shared is None:
        _shared = AnswerBridge()
    return _shared


_tokens = itertools.count(1)


def _apply_in_background(bridge, token, apply_fn, text):
    try:
        apply_fn(text)
    except paste.PasteError as exc:
        bridge.apply_finished.emit(token, str(exc))
    except Exception as exc:  # the window is hidden; it has to be said somewhere
        bridge.apply_finished.emit(token, t("Unexpected error: {error}", error=exc))
    else:
        bridge.apply_finished.emit(token, "")


def _copy_in_background(bridge, token, text):
    # Under the paste lock, so a dictation restoring its clipboard at this
    # moment cannot put the old contents back over the answer.
    try:
        with selection._paste_lock():
            paste.copy(text)
    except Exception as exc:  # the button must always come back
        bridge.copy_finished.emit(token, str(exc) or t("Copy failed"))
    else:
        bridge.copy_finished.emit(token, "")


class AnswerWindow(QDialog):
    def __init__(self, instruction, answer, selected="", can_apply=False,
                 apply_fn=None, agent="", bridge=None, note="",
                 theme_name=theme.DEFAULT, parent=None):
        super().__init__(parent)
        theme.apply(self, theme_name)
        self._apply_fn = apply_fn
        self._thread = None
        self._copy_thread = None
        self.token = next(_tokens)
        self.bridge = bridge or shared_bridge()
        # Bound methods of this window: Qt drops the connections when the
        # window is deleted, so a late result for it reaches nothing here.
        self.bridge.apply_finished.connect(self._applied)
        self.bridge.copy_finished.connect(self._copied)
        self.setWindowTitle(t("Dikte: {name}", name=agent) if agent else "Dikte")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        # The answer can take half a minute, and the user may be typing
        # somewhere by then: shown without the focus, an Enter meant for
        # that window cannot press Apply or Copy here.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(520, 360)

        layout = QVBoxLayout(self)
        self.instruction = QLabel(t("You said: {text}", text=one_line(instruction)))
        # Plain text, always: a transcript or a selection that happens to look
        # like markup must not be rendered as such.
        self.instruction.setTextFormat(Qt.TextFormat.PlainText)
        self.instruction.setWordWrap(True)
        self.instruction.setToolTip(_tip(instruction))
        layout.addWidget(self.instruction)

        # Which text went along, at a glance: a selection left over from
        # earlier is easier to spot here than in a tooltip.
        self.selection = QLabel(t("About the selection ({count} characters): {preview}",
                                  count=len(selected), preview=one_line(selected, 60)))
        self.selection.setTextFormat(Qt.TextFormat.PlainText)
        self.selection.setWordWrap(True)
        preview = selected[:PREVIEW_CHARS] + ("…" if len(selected) > PREVIEW_CHARS else "")
        self.selection.setToolTip(_tip(preview))
        if note:
            self.selection.setText(note)
            self.selection.setToolTip("")
        self.selection.setVisible(bool(selected or note))
        layout.addWidget(self.selection)

        self.answer = QPlainTextEdit()
        self.answer.setPlainText(answer)
        # An edit can be touched up before it goes back; an answer is read.
        self.answer.setReadOnly(not can_apply)
        layout.addWidget(self.answer, 1)

        # Why an Apply came back unpasted, in full: the indicator only has
        # room for its first line.
        self.problem = QLabel("")
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        layout.addWidget(self.problem)

        row = QHBoxLayout()
        row.addStretch(1)
        self.apply_button = QPushButton(t("Apply"))
        self.apply_button.setToolTip(t(
            "Replaces the selected text in the window it came from. "
            "Nothing is pasted until you press this."))
        self.apply_button.clicked.connect(self.apply)
        self.apply_button.setVisible(bool(can_apply and apply_fn))
        self.copy_button = QPushButton(t("Copy"))
        self.copy_button.clicked.connect(self.copy)
        self.cancel_button = QPushButton(t("Cancel"))
        self.cancel_button.setToolTip(t("Closes this and leaves the original text alone."))
        self.cancel_button.clicked.connect(self.reject)
        for button in (self.apply_button, self.copy_button, self.cancel_button):
            # No default button, and none that Enter presses for having the
            # focus: where the window is activated anyway (KWin on Wayland),
            # an Enter meant for another window must not Copy over the
            # clipboard or Apply.
            button.setAutoDefault(False)
            button.setDefault(False)
            row.addWidget(button)
        layout.addLayout(row)
        # The window itself holds the focus when it opens, so stray typing
        # reaches neither the rewrite nor a button. A click goes anywhere,
        # and Escape still cancels, which leaves everything as it was.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def showEvent(self, event):
        super().showEvent(event)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    @property
    def can_apply(self):
        return self.apply_button.isVisibleTo(self)

    def text(self):
        return self.answer.toPlainText()

    def apply(self):
        """Hide, so the selection's window has the focus back, then paste."""
        if not self.can_apply:
            return
        self.hide()
        self._thread = threading.Thread(
            target=_apply_in_background,
            args=(self.bridge, self.token, self._apply_fn, self.text()),
            daemon=True)
        self._thread.start()
        self.accept()

    def copy(self):
        """Put the text on the clipboard, on a thread of its own, so waiting
        for the paste lock never freezes the window. A second click while one
        is running is ignored."""
        if self._copy_thread is not None and self._copy_thread.is_alive():
            return
        self._copy_thread = threading.Thread(
            target=_copy_in_background,
            args=(self.bridge, self.token, self.text()), daemon=True)
        self._copy_thread.start()

    def _applied(self, token, error):
        # Nothing was pasted, so the answer comes back to be copied instead.
        # The error itself is reported by the application.
        if token == self.token and error:
            self.problem.setText(error)
            self.problem.setVisible(True)
            self.show()
            self.raise_()

    def _copied(self, token, error):
        if token != self.token:
            return
        if error:
            self.copy_button.setToolTip(_tip(error))
            self.copy_button.setText(t("Copy failed"))
        else:
            self.copy_button.setText(t("Copied"))
