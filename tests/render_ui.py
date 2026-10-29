"""Capture native Qt client areas with isolated settings and sample data.

Run with python -m tests.render_ui --output DIRECTORY. CI explicitly selects
xcb, cocoa or windows before importing the test package; the default local
test backend remains offscreen. No audio, API request or input is generated.
Window decorations, native file dialogs and compositor effects are excluded.
"""

import argparse
import html
import json
import platform
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PyQt6.QtCore import QCoreApplication, QEvent, QT_VERSION_STR, Qt
from PyQt6.QtGui import QFontInfo
from PyQt6.QtWidgets import QApplication, QLineEdit, QScrollArea

from tests.test_ui import Settings
from dikte import config as cfg, i18n, overlay, settings_ui, theme
from dikte.home_ui import HomeWindow

SAMPLE = "Bir sonraki sürüm için kayıt kontrollerini tamamlayalım. Ayarları gözden geçirip uygulamayı üç platformda da deneyelim."
CASES = (
    ("dictation", 620, 560), ("dictation-wide", 1900, 1000),
    ("file", 620, 640), ("meeting", 680, 760), ("ask", 680, 640),
    ("settings-general", 720, 760), ("settings-display", 720, 640),
    ("settings-api", 720, 760), ("settings-assistant", 720, 760),
    ("settings-shortcuts", 720, 640), ("overlay", 220, 48),
)
SETTINGS_PAGES = {"general": 0, "display": 1, "api": 2, "assistant": 5, "shortcuts": 7}


def settle():
    for _ in range(5):
        QApplication.processEvents()


def capture(widget, path, width, height):
    # Native styles and fonts still render; monitor size does not constrain
    # wide-window cases, and the runner's other windows cannot cover them.
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.resize(width, height)
    widget.show()
    settle()
    pixmap = widget.grab()
    if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"Could not capture {path.name}")
    font = QFontInfo(widget.font())
    result = {
        "file": path.name, "requested_size": [width, height],
        "logical_size": [widget.width(), widget.height()],
        "pixel_size": [pixmap.width(), pixmap.height()],
        "device_pixel_ratio": pixmap.devicePixelRatio(),
        "font": font.family(), "font_points": font.pointSizeF(),
        "horizontal_overflow": [],
    }
    for area in widget.findChildren(QScrollArea):
        if area.isVisible() and area.horizontalScrollBar().maximum() > 0:
            result["horizontal_overflow"].append(area.horizontalScrollBar().maximum())
    widget.hide()
    print(f"Captured {path.name}: {result['logical_size']}, {font.family()}", flush=True)
    return result


def capture_theme(name, output):
    harness = Settings("runTest")
    # Keep the actual platform's application branches as well as Qt's style.
    harness.platform = sys.platform
    harness.setUp()
    try:
        harness.enterContext(mock.patch.object(settings_ui.SettingsWindow, "_sources_once", return_value=[]))
        conf = harness.config(
            theme=name, ui_language="tr", transcribe_provider="openrouter",
            openrouter_api_key="screenshot-only", openrouter_transcribe_model="openai/whisper-1",
            cleanup_provider="openrouter", assistant_provider="openrouter",
        )
        i18n.set_language("tr")
        cfg.append_history({"ts": "2026-09-09 14:32:00", "duration": 18,
                            "elapsed": 2, "text": SAMPLE, "raw": SAMPLE})
        settings = harness.window(conf)
        settings.keep_audio.setText("Ses kayıtlarını sakla (örnek kayıt klasörü)")
        settings.assistant_dir.setPlaceholderText("Proje klasörü")
        for field in settings.findChildren(QLineEdit):
            if field.isReadOnly() and "__main__.py toggle" in field.text():
                field.setText("dikte toggle")
        settings._saved_form = settings._form_values()
        settings._show_dirty()
        controller = SimpleNamespace(
            conf=conf, state="idle", ask_state="idle", meeting_state="idle",
            recording=False, paused=False, home_messages={}, meeting_message="",
            paste_override={}, meeting_elapsed=SimpleNamespace(elapsed=lambda: 12000),
            _recorded_seconds=lambda: 12, open_settings=settings.show,
        )
        for method in ("reset_conversation", "_toggle_pause", "_cancel", "cancel_ask",
                       "cancel_meeting", "_toggle_meeting", "start", "stop", "start_ask", "stop_ask"):
            setattr(controller, method, mock.Mock())
        home = HomeWindow(controller, settings)
        harness.addCleanup(home.deleteLater)
        harness.addCleanup(home.close)
        home._timer.stop()
        settings.file_label.setText("örnek-kayıt.wav")
        settings.file_output.setPlainText(SAMPLE)
        home.ask_output.setPlainText("Örnek yanıt: Önce arayüzü doğrulayalım, ardından sürümü hazırlayalım.")
        indicator = overlay.Overlay(theme_name=name)
        harness.addCleanup(indicator.deleteLater)
        harness.addCleanup(indicator.close)
        images = []
        for case, width, height in CASES:
            if case.startswith("settings-"):
                settings.tabs.setCurrentIndex(SETTINGS_PAGES[case.removeprefix("settings-")])
                widget = settings
            elif case == "overlay":
                indicator.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                indicator.show_recording()
                indicator._anim.stop()
                indicator.set_seconds(12)
                indicator.levels = [0.15, 0.3, 0.7, 0.4] * (overlay.BARS // 4) + [0.2] * (overlay.BARS % 4)
                widget = indicator
                width, height = indicator.width(), indicator.height()
            else:
                home.show_mode("dictation" if case == "dictation-wide" else case)
                widget = home
            images.append(capture(widget, output / f"{name}-{case}.png", width, height))
        return images
    finally:
        harness.doCleanups()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-platform")
    args = parser.parse_args()
    app = QApplication.instance()
    backend = app.platformName()
    if args.expect_platform and backend != args.expect_platform:
        parser.error(f"Expected {args.expect_platform}, got {backend}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "system": platform.system(), "python": platform.python_version(),
        "qt": QT_VERSION_STR, "qpa_backend": backend,
        "qt_style": app.style().objectName(), "language": "tr",
        "scope": "Native Qt client-area renders with sample data and isolated settings. No real recording, API call, window decorations, native file dialogs or compositor validation.",
        "images": [],
    }
    for name in theme.NAMES:
        manifest["images"].extend(capture_theme(name, output))
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    title = f"Dikte: {manifest['system']} / {backend} / Qt {QT_VERSION_STR}"
    cards = "".join(
        f'<figure><a href="{entry["file"]}"><img loading="lazy" src="{entry["file"]}"></a><figcaption>{html.escape(entry["file"])}</figcaption></figure>'
        for entry in manifest["images"]
    )
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>' + html.escape(title) + '</title>'
        '<style>body{font:16px system-ui;background:#eee;color:#222;margin:24px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}figure{margin:0}img{width:100%;height:340px;object-fit:contain;object-position:top}figcaption{padding:8px}</style>'
        '<h1>' + html.escape(title) + '</h1><p>' + html.escape(manifest["scope"]) + '</p><main>' + cards + '</main>',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
