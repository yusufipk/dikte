"""Built-in desktop palettes shared by windows and recording indicators."""

import re
from pathlib import Path

from PyQt6.QtGui import QColor, QPalette

DEFAULT = "nord"
NAMES = {"nord": "Nord", "dark": "Classic dark", "light": "Classic light", "dracula": "Dracula"}

# Nord keeps the original blue workspace colors for existing installations.
PALETTES = {
    "nord": dict(base="#172434", surface="#213247", border="#3C5168", text="#F1F5FA",
                 muted="#B2C1D1", accent="#A5C7FA", accent_text="#172434", hover="#2B4059",
                 disabled_text="#8998A9", disabled_bg="#1C2B3D", capture_disabled="#687B93",
                 outline="#6F86A2", danger="#F08A8F"),
    "dark": dict(base="#101010", surface="#202020", border="#474747", text="#F5F5F5",
                 muted="#BBBBBB", accent="#DDDDDD", accent_text="#101010", hover="#303030",
                 disabled_text="#888888", disabled_bg="#181818", capture_disabled="#606060",
                 outline="#777777", danger="#FF8A8A"),
    "light": dict(base="#FFFFFF", surface="#F2F4F7", border="#B6BEC9", text="#1D2633",
                  muted="#526174", accent="#285DB5", accent_text="#FFFFFF", hover="#E2E7EF",
                  disabled_text="#687385", disabled_bg="#E9EDF2", capture_disabled="#91A8CD",
                  outline="#7A8597", danger="#B42318"),
    "dracula": dict(base="#282A36", surface="#303341", border="#626787", text="#F8F8F2",
                    muted="#BBC0D9", accent="#BD93F9", accent_text="#282A36", hover="#44475A",
                    disabled_text="#9096B0", disabled_bg="#282A36", capture_disabled="#706483",
                    outline="#8288AD", danger="#FF6E6E"),
}


WARN = {"nord": "#EBCB8B", "dark": "#E8C170", "light": "#9A6700", "dracula": "#F1FA8C"}

# How far a card's edge leans from its fill towards the text colour. On the
# dark themes a filled card already stands out from the background; on white
# the light grey fill alone barely does, so the light theme draws a visible
# edge and the others a faint one.
CARD_EDGE = {"nord": 0.06, "dark": 0.08, "light": 0.16, "dracula": 0.06}


def _mix(a, b, amount):
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(
        f"{round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * amount):02X}"
        for i in (0, 2, 4))


def palette(name=DEFAULT):
    """The theme's colours, plus the tints derived from them: `tint` is the
    accent washed into the surface (a selected item), `danger_tint` the same
    for the danger colour."""
    colors = dict(PALETTES.get(name, PALETTES[DEFAULT]))
    colors["tint"] = _mix(colors["surface"], colors["accent"], 0.22)
    colors["danger_tint"] = _mix(colors["surface"], colors["danger"], 0.18)
    # The selected home tab: the accent washed into the background, so the
    # solid accent stays for the one primary action on a screen.
    colors["tab"] = _mix(colors["base"], colors["accent"], 0.17)
    # A warning is amber in every theme; the danger colour stays for failures.
    colors["warn"] = WARN.get(name, WARN[DEFAULT])
    colors["warn_tint"] = _mix(colors["surface"], colors["warn"], 0.18)
    # The edge of a filled card, so it separates from the window in every theme.
    colors["card_edge"] = _mix(colors["surface"], colors["text"],
                               CARD_EDGE.get(name, CARD_EDGE[DEFAULT]))
    # The track behind the home tabs: one segmented control, not five boxes.
    colors["track"] = _mix(colors["base"], colors["surface"], 0.7)
    # Apply with nothing to apply: still shaped and placed as the primary
    # button, so the footer does not change its layout when an edit arrives.
    colors["primary_idle"] = _mix(colors["surface"], colors["accent"], 0.12)
    return colors


_STYLE = """
QWidget { color: @text; }
QDialog, QWidget#home, QScrollArea, QScrollArea > QWidget > QWidget {
    background: @base;
}
QLabel { background: transparent; }
QLabel#brand { font-size: {+6}; font-weight: 700; }
QLabel#heading { font-size: {+8}; font-weight: 500; }
QLabel#pagetitle { font-size: {+4}; font-weight: 600; }
QLabel#muted, QLabel#footer { color: @muted; }
QLabel#footer { padding: 4px 0; }
QLabel#models { color: @muted; font-size: {-1}; }
QFrame#result { background: @surface; border: 1px solid @card_edge; border-radius: 12px; }
QFrame#bubble { background: @tint; border-radius: 12px; }
QPushButton#tile {
    background: @surface; border: 1px solid @card_edge; border-radius: 12px;
    padding: 0; text-align: left;
}
QPushButton#tile:hover { background: @hover; border-color: @outline; }
QPushButton#tile:focus { border: 2px solid @accent; }
QLabel#tiletitle { font-weight: 600; }
QLabel#warning { color: @warn; }
QFrame#segments { background: @track; border: 1px solid @card_edge; border-radius: 10px; }
QPushButton {
    background: @surface; border: 1px solid @border; border-radius: 7px;
    padding: 6px 10px; min-height: 18px;
}
QPushButton:hover { background: @hover; }
QPushButton:pressed, QPushButton:checked { background: @border; }
QPushButton:focus, QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus,
QTextBrowser:focus, QListWidget:focus { border: 2px solid @accent; }
QPushButton:disabled { color: @disabled_text; background: @disabled_bg; }
QPushButton#primary { background: @accent; color: @accent_text; font-weight: 600; }
QPushButton#primary:hover { background: @accent; border-color: @text; }
QPushButton#primary:disabled { background: transparent; color: @disabled_text; border: 1px solid @border; }
QPushButton#primary[footer="true"] { padding: 7px 16px; }
QPushButton#primary[footer="true"]:disabled {
    background: @primary_idle; color: @disabled_text; border: 1px solid @primary_idle;
}
QPushButton[roomy="true"] { padding: 6px 16px; }
QToolButton#tool[roomy="true"] { padding: 5px 14px 5px 11px; }
QPushButton#ghost {
    background: transparent; border: 1px solid @border; padding: 7px 14px;
}
QPushButton#ghost:hover { background: @hover; }
QPushButton#ghost:disabled { background: transparent; border-color: @border; color: @disabled_text; }
QPushButton#segment {
    background: @surface; border: 1px solid @outline; border-radius: 0; padding: 5px 14px;
}
QPushButton#segment[edge="first"] { border-top-left-radius: 7px; border-bottom-left-radius: 7px; }
QPushButton#segment[edge="last"] {
    border-top-right-radius: 7px; border-bottom-right-radius: 7px; border-left: none;
}
QPushButton#segment:hover { background: @hover; }
QPushButton#segment:checked { background: @tab; color: @accent; font-weight: 600; }
QPushButton#quiet { background: transparent; border: 1px solid transparent; }
QPushButton#quiet:hover { background: @hover; }
QPushButton#quiet:disabled { background: transparent; border-color: transparent; color: @disabled_text; }
QPushButton#link {
    background: transparent; border: none; color: @accent; padding: 2px 0; text-align: left;
}
QPushButton#link:hover { text-decoration: underline; }
QPushButton#link:focus { border: none; text-decoration: underline; }
QPushButton#capture {
    background: @accent; color: @accent_text; border: 6px solid @surface;
    border-radius: 56px; padding: 0;
    min-width: 100px; max-width: 100px; min-height: 100px; max-height: 100px;
}
QPushButton#capture[compact="true"] {
    border-width: 4px; border-radius: 36px;
    min-width: 64px; max-width: 64px; min-height: 64px; max-height: 64px;
}
QPushButton#capture:focus { border-color: @text; }
QPushButton#capture:disabled { background: @capture_disabled; }
QPushButton#mode {
    border: 1px solid transparent; background: transparent; border-radius: 7px;
    padding: 5px 4px; color: @muted;
}
QPushButton#mode:hover { background: @hover; color: @text; }
QPushButton#mode:checked {
    background: @surface; color: @accent; border-color: @card_edge; font-weight: 600;
}
QPushButton#mode:focus { border: 2px solid @accent; }
QPushButton#settings { padding: 0; min-height: 30px; min-width: 32px; }
QToolButton#disclosure {
    background: transparent; border: none; border-radius: 5px;
    padding: 5px 7px 5px 0; text-align: left; min-height: 20px;
}
QToolButton#disclosure:hover { background: @hover; }
QToolButton#disclosure:focus { border: 1px solid @accent; }
QGroupBox {
    border: 1px solid @border; border-radius: 10px;
    margin-top: 22px; padding: 6px 4px 2px 4px;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 2px; top: 2px; padding: 0; font-weight: 600;
}
QLineEdit, QPlainTextEdit, QTextBrowser, QListWidget, QComboBox, QSpinBox {
    background: @surface; border: 1px solid @outline; border-radius: 6px;
    padding: 5px 7px; selection-background-color: @accent; selection-color: @accent_text;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QListWidget:disabled,
QComboBox:disabled, QSpinBox:disabled {
    color: @disabled_text; background: @disabled_bg;
}
QComboBox { padding-right: 28px; min-height: 18px; }
QComboBox::drop-down {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 26px; border: none; background: transparent;
}
QComboBox::down-arrow { image: url("__ICONS__/chevron-down@arrow_suffix.svg"); width: 12px; height: 12px; }
QSpinBox { padding: 5px 20px 5px 0; min-height: 18px; }
QSpinBox > QLineEdit { border: none; background: transparent; padding: 0; }
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width: 20px; border: none; background: transparent;
}
QSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 5px; }
QSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 5px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: @border; }
QSpinBox::up-arrow { image: url("__ICONS__/chevron-up@arrow_suffix.svg"); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url("__ICONS__/chevron-down@arrow_suffix.svg"); width: 10px; height: 10px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 14px; height: 14px; border: 1px solid @outline;
    border-radius: 4px; background: @surface;
}
QCheckBox::indicator:hover { border-color: @accent; }
QCheckBox::indicator:checked {
    background: @accent; border-color: @accent;
    image: url("__ICONS__/check@arrow_suffix.svg");
}
QCheckBox:focus::indicator { border-color: @text; }
QCheckBox:disabled { color: @disabled_text; }
QCheckBox::indicator:disabled { background: @disabled_bg; border-color: @disabled_text; }
QCheckBox::indicator:checked:disabled { background: @disabled_text; }
QComboBox QAbstractItemView { background: @surface; color: @text; selection-background-color: @border; }
QTableWidget {
    background: @surface; color: @text; border: 1px solid @border; border-radius: 5px;
    gridline-color: @border; alternate-background-color: @hover;
    selection-background-color: @accent; selection-color: @accent_text;
}
QTableWidget:focus { border: 2px solid @accent; }
QTableWidget::item { padding: 2px 4px; }
QTableWidget::item:selected { background: @accent; color: @accent_text; }
QHeaderView { background: @surface; }
QHeaderView::section {
    background: @base; color: @muted; border: none;
    border-right: 1px solid @border; border-bottom: 1px solid @border;
    padding: 5px 7px; font-weight: 600;
}
QTableCornerButton::section { background: @base; border: none; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: @surface; padding: 10px; }
QTabBar::tab:selected { background: @border; }
QMenu { background: @surface; color: @text; border: 1px solid @border; }
QMenu::item:selected { background: @border; }
QToolTip { background: @surface; color: @text; border: 1px solid @accent; }
QListWidget#nav {
    background: transparent; border: none; padding: 0; outline: 0;
}
QListWidget#nav:focus { border: none; }
QListWidget#nav::item {
    padding: 8px 10px; margin: 1px 0; border-radius: 8px; color: @muted;
}
QListWidget#nav::item:hover { background: @hover; color: @text; }
QListWidget#nav::item:selected { background: @tint; color: @text; }
QListWidget { outline: 0; }
QListWidget::item { padding: 5px 8px; border-radius: 6px; }
QListWidget::item:hover { background: @hover; }
QListWidget::item:selected { background: @tint; color: @text; }
QListWidget#rows { padding: 2px; outline: 0; }
QListWidget#rows::item { border: none; }
QFrame#result QListWidget#rows { background: transparent; border: none; padding: 0; }
QListWidget#rows[card="true"] {
    background: @surface; border: 1px solid @card_edge; border-radius: 12px; padding: 4px;
}
QListWidget#rows[card="true"]:focus { border: 2px solid @accent; }
QFrame#card { background: @surface; border: 1px solid @card_edge; border-radius: 12px; }
QSplitter#panes::handle { background: transparent; }
QSplitter#panes::handle:vertical:hover { background: @hover; border-radius: 3px; }
QFrame#sectionbox { border: 1px solid @border; border-radius: 10px; }
QFrame#card QLineEdit { background: @base; }
QFrame#card QPlainTextEdit, QFrame#card QTextBrowser {
    background: transparent; border: 2px solid transparent; border-radius: 8px; padding: 6px;
}
QFrame#result QPlainTextEdit[readOnly="true"], QFrame#result QTextBrowser {
    background: transparent; border: 2px solid transparent; border-radius: 8px; padding: 6px;
}
QFrame#card QPlainTextEdit:focus, QFrame#card QTextBrowser:focus,
QFrame#result QPlainTextEdit[readOnly="true"]:focus {
    border: 2px solid @accent; border-radius: 8px;
}
QPlainTextEdit[dim="true"] { color: @muted; }
QPlainTextEdit#original { background: @base; color: @muted; border-color: @border; }
QFrame#notice { background: @warn_tint; border: 1px solid @warn; border-radius: 8px; }
QFrame#noticeDanger { background: @danger_tint; border: 1px solid @danger; border-radius: 8px; }
QGroupBox#flat { border: none; margin-top: 0; padding: 0; }
QFrame#rule { background: @border; max-height: 1px; min-height: 1px; border: none; }
QFrame#vrule { background: @border; max-width: 1px; min-width: 1px; border: none; }
QLabel#section { font-weight: 600; font-size: {+2}; }
QLabel#meta { color: @muted; font-size: {-1}; }
QLabel#danger { color: @danger; }
QLabel#errorhead { color: @danger; font-weight: 600; }
QLabel#editortitle { font-size: {+3}; font-weight: 600; }
QLabel#keycap {
    background: @surface; border: 1px solid @outline; border-bottom-width: 2px;
    border-radius: 5px; padding: 1px 7px; font-size: {-1}; font-weight: 600;
}
QLabel#action { font-weight: 600; }
QLabel#badgeText {
    background: @tint; color: @accent; border-radius: 9px; padding: 1px 8px;
    font-size: {-1}; font-weight: 600;
}
QFrame#dropzone {
    background: @surface; border: 2px dashed @outline; border-radius: 12px;
}
QFrame#dropzone[over="true"] { border-color: @accent; background: @tint; }
QFrame#chipbox { background: @surface; border: 1px solid @border; border-radius: 11px; }
QLabel#chiptext { color: @muted; font-size: {-1}; }
QToolButton#chiplink {
    background: transparent; border: 1px solid transparent; border-radius: 6px;
    color: @accent; font-size: {-1}; padding: 1px 6px;
}
QToolButton#chiplink:hover { background: @hover; }
QToolButton#profilechip {
    background: @surface; border: 1px solid @border; border-radius: 13px;
    color: @text; font-size: {-1}; padding: 3px 10px 3px 8px;
}
QToolButton#profilechip:hover { background: @hover; border-color: @outline; }
QToolButton#profilechip:focus { border: 1px solid @accent; }
QToolButton#profilechip::menu-indicator { image: none; width: 0; }
QToolButton#chiplink:focus { border: 1px solid @accent; }
QPushButton#destructive { color: @danger; border-color: @danger; background: transparent; }
QPushButton#destructive:hover { background: @danger_tint; }
QToolButton#round {
    background: @surface; border: 1px solid @outline; border-radius: 21px;
    min-width: 40px; max-width: 40px; min-height: 40px; max-height: 40px; padding: 0;
}
QToolButton#round:hover { background: @hover; }
QToolButton#round:focus { border: 2px solid @accent; }
QToolButton#roundDanger {
    background: @surface; border: 1px solid @danger; border-radius: 21px;
    min-width: 40px; max-width: 40px; min-height: 40px; max-height: 40px; padding: 0;
}
QToolButton#roundDanger:hover { background: @danger_tint; }
QToolButton#roundDanger:focus { border: 2px solid @danger; }
QToolButton#more, QToolButton#tool {
    background: @surface; border: 1px solid @border; border-radius: 7px;
    padding: 5px 8px; min-height: 18px;
}
QToolButton#more:hover, QToolButton#tool:hover { background: @hover; }
QToolButton#more::menu-indicator { image: none; width: 0; }
QToolButton#more:focus, QToolButton#tool:focus { border: 2px solid @accent; }
QToolButton#more:disabled, QToolButton#tool:disabled { color: @disabled_text; background: @disabled_bg; }
QToolButton#info {
    background: transparent; border: 1px solid transparent; border-radius: 10px; padding: 1px;
}
QToolButton#info:hover { background: @hover; }
QToolButton#info:focus { border: 1px solid @accent; }
QLabel:disabled { color: @disabled_text; }
/* Scroll bars take the colour of whatever they sit on (the window, a card),
   never a band of their own; the corner between two bars does the same. */
QScrollBar:vertical { background: transparent; width: 12px; }
QScrollBar::handle:vertical { background: @border; min-height: 24px; border-radius: 6px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 12px; }
QScrollBar::handle:horizontal { background: @border; min-width: 24px; border-radius: 6px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QAbstractScrollArea::corner { background: transparent; border: none; }
"""


# The body text size the type scale is built on when nothing else is known.
BODY_POINTS = 10.0


def _sizes(style, body):
    """Every "{+2}" in `style` as a point size that far from `body`: headings
    and captions keep their place above and below the text around them at
    whatever size the desktop has chosen, rather than a fixed pixel size that
    a large font overtakes."""
    body = body if body and body > 0 else BODY_POINTS
    return re.sub(r"\{([+-]\d+(?:\.\d+)?)\}",
                  lambda match: f"{max(6.0, body + float(match.group(1))):g}pt", style)


def stylesheet(name=DEFAULT, body=None):
    """The stylesheet for theme `name`, its type scale built on `body` (the
    point size of the text it is applied to)."""
    colors = palette(name)
    result = _sizes(_STYLE, body).replace("@arrow_suffix", "-light" if name == "light" else "")
    for role, color in sorted(colors.items(), key=lambda item: -len(item[0])):
        result = result.replace("@" + role, color)
    return result.replace("__ICONS__", (Path(__file__).parent / "icons").as_posix())


def apply(widget, name=DEFAULT):
    colors = palette(name)
    native = QPalette(widget.palette())
    for role, color in (
        (QPalette.ColorRole.Window, "base"), (QPalette.ColorRole.Base, "surface"),
        (QPalette.ColorRole.AlternateBase, "hover"), (QPalette.ColorRole.Button, "surface"),
        (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Text, "text"),
        (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.PlaceholderText, "muted"),
        (QPalette.ColorRole.Highlight, "accent"), (QPalette.ColorRole.HighlightedText, "accent_text"),
    ):
        native.setColor(role, QColor(colors[color]))
    widget.setPalette(native)
    # Read by the painted widgets (list rows, glyphs) that draw their own
    # colours rather than going through the stylesheet.
    widget.setProperty("dikte_theme", name if name in PALETTES else DEFAULT)
    widget.setStyleSheet(stylesheet(name, widget.font().pointSizeF()))
