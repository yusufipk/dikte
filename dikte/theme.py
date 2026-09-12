"""Built-in desktop palettes shared by windows and recording indicators."""

from pathlib import Path

from PyQt6.QtGui import QColor, QPalette

DEFAULT = "nord"
NAMES = {"nord": "Nord", "dark": "Classic dark", "light": "Classic light", "dracula": "Dracula"}

# Nord keeps the original blue workspace colors for existing installations.
PALETTES = {
    "nord": dict(base="#172434", surface="#213247", border="#3C5168", text="#F1F5FA",
                 muted="#B2C1D1", accent="#A5C7FA", accent_text="#172434", hover="#2B4059",
                 disabled_text="#8998A9", disabled_bg="#1C2B3D", capture_disabled="#687B93"),
    "dark": dict(base="#101010", surface="#202020", border="#474747", text="#F5F5F5",
                 muted="#BBBBBB", accent="#DDDDDD", accent_text="#101010", hover="#303030",
                 disabled_text="#888888", disabled_bg="#181818", capture_disabled="#606060"),
    "light": dict(base="#FFFFFF", surface="#F2F4F7", border="#B6BEC9", text="#1D2633",
                  muted="#526174", accent="#285DB5", accent_text="#FFFFFF", hover="#E2E7EF",
                  disabled_text="#687385", disabled_bg="#E9EDF2", capture_disabled="#91A8CD"),
    "dracula": dict(base="#282A36", surface="#303341", border="#626787", text="#F8F8F2",
                    muted="#BBC0D9", accent="#BD93F9", accent_text="#282A36", hover="#44475A",
                    disabled_text="#9096B0", disabled_bg="#282A36", capture_disabled="#706483"),
}


def palette(name=DEFAULT):
    return PALETTES.get(name, PALETTES[DEFAULT])


_STYLE = """
QWidget { color: @text; }
QDialog, QWidget#home, QScrollArea, QScrollArea > QWidget > QWidget {
    background: @base;
}
QLabel { background: transparent; }
QLabel#brand { font-size: 21px; font-weight: 700; }
QLabel#heading { font-size: 24px; font-weight: 500; }
QLabel#muted, QLabel#footer { color: @muted; }
QLabel#footer { padding: 4px 0; }
QLabel#models { color: @muted; font-size: 12px; }
QFrame#result { background: @surface; border-radius: 12px; }
QPushButton {
    background: @surface; border: 1px solid @border; border-radius: 7px;
    padding: 6px 10px; min-height: 18px;
}
QPushButton:hover { background: @hover; }
QPushButton:pressed, QPushButton:checked { background: @border; }
QPushButton:focus, QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus,
QListWidget:focus { border: 2px solid @accent; }
QPushButton:disabled { color: @disabled_text; background: @disabled_bg; }
QPushButton#primary { background: @accent; color: @accent_text; font-weight: 600; }
QPushButton#primary:disabled { background: @disabled_bg; color: @disabled_text; border-color: @border; }
QPushButton#capture {
    background: @accent; color: @accent_text; border: 6px solid @surface;
    border-radius: 56px; padding: 0;
    min-width: 100px; max-width: 100px; min-height: 100px; max-height: 100px;
}
QPushButton#capture:focus { border-color: @text; }
QPushButton#capture:disabled { background: @capture_disabled; }
QPushButton#mode { border: 1px solid @border; background: @surface; padding: 6px 4px; }
QPushButton#mode:hover { background: @hover; }
QPushButton#mode:checked { background: @accent; color: @accent_text; border-color: @accent; }
QPushButton#mode:focus { border: 2px solid @accent; }
QPushButton#settings { padding: 0; min-height: 30px; min-width: 32px; }
QToolButton#disclosure {
    background: transparent; border: none; border-radius: 5px;
    padding: 5px 7px; text-align: left; min-height: 20px;
}
QToolButton#disclosure:hover, QToolButton#disclosure:checked { background: @hover; }
QToolButton#disclosure:focus { border: 1px solid @accent; }
QGroupBox { border: 1px solid @border; border-radius: 8px; margin-top: 14px; padding: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QLineEdit, QPlainTextEdit, QListWidget, QComboBox, QSpinBox {
    background: @surface; border: 1px solid @border; border-radius: 5px;
    padding: 5px 7px; selection-background-color: @accent; selection-color: @accent_text;
}
QComboBox { padding-right: 28px; min-height: 18px; }
QComboBox::drop-down {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 26px; border: none; background: transparent;
}
QComboBox::down-arrow { image: url("__ICONS__/chevron-down@arrow_suffix.svg"); width: 12px; height: 12px; }
QSpinBox { padding: 2px 20px 2px 6px; min-height: 18px; }
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
    width: 14px; height: 14px; border: 1px solid @border;
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
QTabWidget::pane { border: none; }
QTabBar::tab { background: @surface; padding: 10px; }
QTabBar::tab:selected { background: @border; }
QMenu { background: @surface; color: @text; border: 1px solid @border; }
QMenu::item:selected { background: @border; }
QToolTip { background: @surface; color: @text; border: 1px solid @accent; }
QScrollBar:vertical { background: @base; width: 12px; }
QScrollBar::handle:vertical { background: @border; min-height: 24px; border-radius: 6px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: @base; }
"""


def stylesheet(name=DEFAULT):
    colors = palette(name)
    result = _STYLE.replace("@arrow_suffix", "-light" if name == "light" else "")
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
    widget.setStyleSheet(stylesheet(name))
