"""The two windows, on Qt's offscreen platform.

Not a look at the pixels: what these hold onto is the round trip through the
settings window. Every tab loads a setting into a widget and writes it back on
save, so a setting added to one half and not the other is silently reset the
next time anybody presses Save. That is the failure this catches.
"""

import os
import sys
import unittest
from typing import ClassVar
from unittest import mock

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from dikte import audio
from dikte import cleanup
from dikte import config as cfg
from dikte import ggml
from dikte import hotkey
from dikte import ipc
from dikte import overlay as overlay_module
from dikte import paste
from dikte import settings_ui
from dikte import update
from tests.support import DikteTest, only_these_tools

# One application for the whole run; Qt allows no second one.
_app = QApplication.instance() or QApplication([])


# A valid non-default value for every setting the window shows. Anything the
# window does not touch is left out: the round trip cannot lose what it never
# reads.
CHANGED = {
    "ui_language": "tr",
    "language": "tr",
    "auto_paste": False,
    "paste_shortcut": "ctrl+shift+v",
    "restore_clipboard": True,
    "overlay_corner": "top-right",
    "max_seconds": 120,
    "skip_silent": False,
    "silence_db": -42.0,
    "filter_hallucinations": False,
    "keep_audio": True,
    "openai_api_key": "sk-test-key",
    "groq_api_key": "gsk-test-key",
    "openrouter_api_key": "sk-or-test-key",
    "transcribe_provider": "openrouter",
    "transcribe_model": "whisper-1",
    "groq_transcribe_model": "whisper-large-v3",
    "openrouter_transcribe_model": "openai/whisper-1",
    "cleanup_enabled": False,
    "cleanup_provider": "local",
    "cleanup_model": "some/other-model",
    "cleanup_claude_model": "opus",
    "cleanup_codex_model": "gpt-5",
    "cleanup_reasoning": "high",
    "local_model": "ggml-small.bin",
    "local_gpu": False,
    "local_preload": False,
    "local_threads": 6,
    "local_llm_model": "gemma-3-4b-it-Q4_K_M.gguf",
    "local_llm_repo": "ggml-org/gemma-4-E2B-it-GGUF",
    "local_llm_gpu": False,
    "local_llm_preload": True,
    "local_llm_reasoning": "low",
    "cleanup_prompt": "Only fix the punctuation.",
    "file_cleanup_prompt": "Keep the stamps where they are.",
    "transcribe_prompt": "Paraşüt, OpenFrame",
    "assistant_provider": "codex",
    "assistant_model": "opus",
    "assistant_permission_mode": "manual",
    "assistant_codex_model": "gpt-5",
    "assistant_codex_sandbox": "read-only",
    "assistant_openrouter_model": "some/agent-model",
    "assistant_reasoning": "high",
    "assistant_dir": "/tmp",
    "assistant_timeout": 600,
    "assistant_session_minutes": 90,
    "assistant_paste": False,
    "assistant_cleanup": True,
    "assistant_prompt": "Answer in one sentence.",
    "assistant_shortcut": "Meta+A",
    "meeting_self_name": "Yusuf",
    "meeting_other_name": "Ayşe",
    "meeting_participants": "Mehmet",
    "meeting_model": "some/meeting-model",
    "meeting_reasoning": "medium",
    "meeting_language": "tr",
    "meeting_cleanup": False,
    "meeting_max_seconds": 7200,
    "meeting_keep_audio": True,
    "meeting_shortcut": "Meta+M",
    "meeting_prompt": "Write it as bullet points.",
    "file_timestamps": True,
    "file_cleanup": False,
    "shortcut": "Ctrl+Alt+Space",
    "cancel_shortcut": "Meta+Shift+Space",
    "pause_shortcut": "Meta+P",
    "evdev_hotkey": True,
    "history_limit": 50,
    "update_check": False,
}


class Settings(DikteTest):
    # What a Mac shows instead, where the combination on offer is a different
    # one. Everything else about the window is the same on both.
    changed = CHANGED
    platform = "linux"
    # A session with no shortcut registry, which is what most Linux desktops
    # are. The subclasses below stand on the other two. Pinned rather than
    # inherited from whatever the machine running the suite is logged into,
    # since half the shortcut tab is built from the answer.
    desktop = "i3"
    tools: ClassVar[tuple] = ()

    def setUp(self):
        super().setUp()
        # No pactl, no model lists over the network, and no modal dialogue
        # waiting for somebody to press OK.
        self.enterContext(mock.patch.object(sys, "platform", self.platform))
        self.enterContext(only_these_tools(*self.tools))
        self.enterContext(mock.patch.dict(
            os.environ, {"XDG_CURRENT_DESKTOP": self.desktop}))
        self.enterContext(mock.patch.object(QMessageBox, "information"))
        self.enterContext(mock.patch.object(settings_ui.SettingsWindow,
                                            "_load_models"))
        self.enterContext(mock.patch.object(settings_ui.SettingsWindow,
                                            "_load_transcribe_models"))
        self.enterContext(mock.patch.object(settings_ui.SettingsWindow,
                                            "_load_codex_models"))
        # The local model boxes fetch their own list the moment they are shown,
        # from a thread, which is nobody's test failing but a real request.
        self.enterContext(mock.patch.object(settings_ui.LocalModelBox,
                                            "_fetch_models"))
        self.enterContext(mock.patch.object(settings_ui.hotkey, "APPLICATIONS_DIR",
                                            self.path("applications")))
        self.enterContext(mock.patch.object(settings_ui.hotkey, "SHORTCUTS_FILE",
                                            self.path("kglobalshortcutsrc")))

    def window(self, conf):
        window = settings_ui.SettingsWindow(conf)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    @staticmethod
    def wheel():
        """One notch of a mouse wheel, rolled downwards."""
        return QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0),
                           QPoint(0, -120), Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier,
                           Qt.ScrollPhase.NoScrollPhase, False)

    def test_the_window_opens_with_every_tab_on_it(self):
        window = self.window(cfg.Config())
        tabs = window.findChildren(settings_ui.QTabWidget)[0]
        self.assertEqual(tabs.count(), 9)
        self.assertEqual(window.windowTitle(), "Dikte Settings")

    def test_no_tab_can_stretch_the_window_past_a_small_screen(self):
        # A tab that keeps its full height hands that height to the window as a
        # minimum, and a tall one then carries Save off the bottom of a laptop
        # screen with no way to drag it back. Each tab scrolls instead.
        window = self.window(cfg.Config())
        for index in range(window.tabs.count()):
            window.tabs.setCurrentIndex(index)
            self.assertLess(window.minimumSizeHint().height(), 500,
                            window.tabs.tabText(index))

    def test_the_window_cannot_be_dragged_down_to_a_stub(self):
        # A tab that scrolls asks for no height of its own, which leaves nothing
        # to stop the window being pulled down to a tab bar and half a button.
        window = self.window(cfg.Config())
        window.resize(1, 1)
        self.assertGreaterEqual(window.width(), 500)
        self.assertGreaterEqual(window.height(), 360)

    def test_the_wheel_passes_over_a_box_it_was_not_aimed_at(self):
        # Every tab scrolls now, and a combo box reads the wheel as a change of
        # value: rolling down the page with the pointer over the language box
        # would pick another language on the way past, and Save would write it
        # down. The box takes the wheel once it has been clicked into.
        window = self.window(cfg.Config())
        # Shown and activated, because a box in a window nobody is looking at
        # can be given the focus but never has it.
        window.show()
        window.activateWindow()
        QApplication.processEvents()
        box = window.ui_language
        # Not the wheel focus a combo box has by default: Qt hands the focus
        # over before it delivers the wheel, which would make "has the focus"
        # true for the very roll being refused.
        self.assertEqual(box.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        before = box.currentIndex()
        rolled = self.wheel()
        QApplication.sendEvent(box, rolled)
        self.assertEqual(box.currentIndex(), before)
        # Refused, not swallowed. An unaccepted wheel event is the one Qt
        # carries on up to the scroll area, so the page moves instead.
        self.assertFalse(rolled.isAccepted())
        box.setFocus()
        QApplication.sendEvent(box, self.wheel())
        self.assertNotEqual(box.currentIndex(), before)

    def test_a_wrapped_label_keeps_the_room_its_lines_need(self):
        # The program path shares a row with a button, and a row is measured
        # before its width is known: the label has to claim the second line back
        # itself, and give it up again when the window is widened.
        label = settings_ui.WrappedLabel()
        # Shown, because a hidden widget is told about its new size only once
        # somebody looks at it, and the height is worked out from that size.
        label.show()
        self.addCleanup(label.deleteLater)
        line = label.fontMetrics().height()
        label.resize(120, line)
        label.setText("Installed on the system: /opt/homebrew/bin/whisper-server")
        self.assertGreater(label.minimumHeight(), line)
        label.resize(2000, line)
        self.assertLessEqual(label.minimumHeight(), line)

    def test_saving_without_touching_anything_changes_nothing(self):
        """Every widget has to load what is stored, or Save writes its default
        over it. This says so for the whole table at once."""
        conf = cfg.Config()
        before = dict(conf.data)
        self.window(conf)._save()
        self.assertEqual(conf.data, before)

    def test_a_setting_of_your_own_survives_the_round_trip(self):
        self.write_config(self.changed)
        conf = cfg.Config()
        self.window(conf)._save()
        stored = self.read_config_file()
        for key, value in self.changed.items():
            with self.subTest(key=key):
                self.assertEqual(stored[key], value)

    def test_only_a_save_that_changed_the_language_says_so(self):
        # The owner answers language_changed by replacing the window, so a
        # save that left the language alone must keep quiet.
        i18n = settings_ui.i18n
        self.addCleanup(i18n.set_language, i18n.language())
        window = self.window(cfg.Config())
        heard = []
        window.language_changed.connect(lambda: heard.append(True))
        window._save()
        self.assertEqual(heard, [])
        other = "en" if i18n.language() == "tr" else "tr"
        window._select_data(window.ui_language, other)
        window._save()
        self.assertEqual(heard, [True])

    def test_the_model_box_on_screen_belongs_to_whoever_cleans_up(self):
        """An OpenRouter id and a Claude alias are not the same field."""
        window = self.window(cfg.Config())
        boxes = {"openrouter": window.cleanup_model_row,
                 "claude": window.cleanup_claude_model,
                 "codex": window.cleanup_codex_model}
        for provider, box in boxes.items():
            with self.subTest(provider=provider):
                window._select_data(window.cleanup_provider, provider)
                shown = [name for name, other in boxes.items()
                         if not other.isHidden()]
                self.assertEqual(shown, [provider])
                self.assertFalse(box.isHidden())

    def test_codex_answering_refills_both_of_its_boxes(self):
        """The list Codex gave replaces the built-in one, in both places, and
        neither loses what was already picked."""
        conf = self.config(cleanup_codex_model="my-own-model")
        window = self.window(conf)
        window._on_codex_models_loaded(["gpt-6", "gpt-6-mini"])
        for combo in (window.cleanup_codex_model, window.assistant_codex_model):
            with self.subTest(combo=combo.objectName() or "combo"):
                offered = [combo.itemText(i) for i in range(combo.count())]
                self.assertEqual(offered[1:], ["gpt-6", "gpt-6-mini"])
        self.assertEqual(window.cleanup_codex_model.currentText(),
                         "my-own-model")

    def test_the_update_line_names_the_version_that_is_running(self):
        window = self.window(cfg.Config())
        self.assertIn(settings_ui.__version__, window.update_status.text())
        # Nothing to open until a check has found something to open.
        self.assertTrue(window.update_page.isHidden())

    def test_a_newer_release_puts_the_page_button_on_screen(self):
        window = self.window(cfg.Config())
        told = []
        window.update_found.connect(told.append)
        release = update.Release("9.9.9", "https://example.invalid/9.9.9", "")
        window._on_update_checked(release, "")
        self.assertIn("9.9.9", window.update_status.text())
        self.assertFalse(window.update_page.isHidden())
        self.assertEqual(window._release_url, release.url)
        # And the tray hears about it from here rather than waiting a day.
        self.assertEqual(told, [release])

    def test_a_check_that_failed_says_why_and_hands_the_button_back(self):
        window = self.window(cfg.Config())
        window.update_now.setEnabled(False)
        window._on_update_checked(None, "api.github.com answered HTTP 403.")
        self.assertIn("403", window.update_status.text())
        self.assertTrue(window.update_now.isEnabled())

    def test_the_settings_the_window_does_not_show_are_left_alone(self):
        """A tab nobody wrote must not reset what the command line set."""
        self.write_config({"silence_db": -42.0, "speech_margin_db": 15.0,
                           "openrouter_base_url": "http://localhost:1234/v1"})
        conf = cfg.Config()
        self.window(conf)._save()
        stored = self.read_config_file()
        self.assertEqual(stored["speech_margin_db"], 15.0)
        self.assertEqual(stored["openrouter_base_url"], "http://localhost:1234/v1")

    def test_every_global_shortcut_has_a_row_of_its_own(self):
        window = self.window(cfg.Config())
        self.assertEqual(set(window._shortcut_rows), set(hotkey.SHORTCUTS))

    def shortcut_tab_text(self, window):
        """Everything the shortcut tab says, as one string."""
        return "\n".join(
            widget.text() for widget in
            window.findChildren(settings_ui.QLabel)
            + window.findChildren(settings_ui.QLineEdit)
            + window.findChildren(settings_ui.QPushButton)
        )

    def test_the_shortcut_tab_talks_about_this_session_and_no_other(self):
        """A desktop with no registry is told the truth: nothing is installed
        anywhere, Dikte is listening, and here is the command to bind if the
        desktop should own the keys instead. It used to be promised a KWin that
        was not running."""
        window = self.window(cfg.Config())
        text = self.shortcut_tab_text(window)
        self.assertIn("i3 keeps no shortcut registry", text)
        self.assertNotIn("KWin", text)
        self.assertIn(ipc.command_for("toggle"), text)
        # Not a choice to offer where it is the only mechanism there is.
        self.assertTrue(window.evdev_enabled.isHidden())
        self.assertFalse([button for button in
                          window.findChildren(settings_ui.QPushButton)
                          if "install" in button.text().lower()])

    def test_emptying_a_shortcut_turns_it_off_but_not_the_toggle(self):
        """The application is unusable without the toggle, so that one box
        falls back. The rest stay empty, which is how they are switched off.

        Which combination it falls back to is the platform's and is pinned in
        test_hotkey; MacSettings runs this too, and there the answer is not
        Ctrl+Space.
        """
        conf = cfg.Config()
        window = self.window(conf)
        for box, _status, _missing in window._shortcut_rows.values():
            box.setCurrentText("")
        window._save()
        self.assertTrue(conf["shortcut"])
        self.assertEqual(conf["shortcut"], hotkey.default_combo("toggle"))
        self.assertEqual(conf["cancel_shortcut"], "")
        self.assertEqual(conf["pause_shortcut"], "")
        self.assertEqual(conf["assistant_shortcut"], "")
        self.assertEqual(conf["meeting_shortcut"], "")

    def test_installing_the_discard_key_writes_its_own_entry(self):
        conf = cfg.Config()
        window = self.window(conf)
        window._shortcut_rows["cancel"][0].setCurrentText("Meta+Shift+Space")
        with mock.patch.object(settings_ui.hotkey, "install_shortcut",
                               return_value=(True, "saved")) as install:
            window._install_shortcut("cancel")
        combo, command = install.call_args.args
        self.assertEqual(combo, "Meta+Shift+Space")
        self.assertTrue(command.endswith(" cancel"))
        self.assertEqual(install.call_args.kwargs["desktop_id"],
                         hotkey.CANCEL_DESKTOP_ID)
        self.assertEqual(conf["cancel_shortcut"], "Meta+Shift+Space")

    def test_a_prompt_left_at_its_default_is_stored_as_empty(self):
        """So that switching the interface language switches the prompt too."""
        conf = cfg.Config()
        self.window(conf)._save()
        self.assertEqual(conf["cleanup_prompt"], "")
        self.assertEqual(conf["meeting_prompt"], "")
        self.assertEqual(conf["assistant_prompt"], "")

    def test_each_provider_keeps_its_own_transcription_model(self):
        self.write_config({"transcribe_provider": "openai",
                           "transcribe_model": "gpt-4o-transcribe",
                           "groq_transcribe_model": "whisper-large-v3",
                           "openrouter_transcribe_model": "openai/whisper-1"})
        conf = cfg.Config()
        window = self.window(conf)
        for provider in ("groq", "openrouter"):
            window.transcribe_provider.setCurrentIndex(
                window.transcribe_provider.findData(provider))
        window._save()
        self.assertEqual(conf["transcribe_provider"], "openrouter")
        self.assertEqual(conf["transcribe_model"], "gpt-4o-transcribe")
        self.assertEqual(conf["groq_transcribe_model"], "whisper-large-v3")

    def test_the_provider_box_offers_every_provider_config_knows(self):
        window = self.window(cfg.Config())
        offered = [window.transcribe_provider.itemData(i)
                   for i in range(window.transcribe_provider.count())]
        self.assertEqual(offered, ["local"] + list(cfg.TRANSCRIBERS))

    def test_the_cleanup_box_offers_everyone_cleanup_py_dispatches_to(self):
        window = self.window(cfg.Config())
        offered = [window.cleanup_provider.itemData(i)
                   for i in range(window.cleanup_provider.count())]
        self.assertEqual(sorted(offered), sorted(cleanup.PROVIDERS))

    def test_the_answer_to_a_test_lands_under_the_key_it_was_asked_about(self):
        """One signal serves all three buttons, so it carries which one asked."""
        window = self.window(cfg.Config())
        window._on_test_done("groq", True, "it works")
        button, answer = window._testers["groq"]
        self.assertEqual(answer.text(), "✓ it works")
        self.assertTrue(button.isEnabled())
        self.assertEqual(window._testers["openai"][1].text(), "")

    def test_a_key_lands_in_the_field_of_its_own_provider(self):
        self.write_config({"groq_api_key": "gsk-mine"})
        window = self.window(cfg.Config())
        self.assertEqual(window.groq_key.text(), "gsk-mine")
        self.assertEqual(window.openai_key.text(), "")

    def test_saving_applies_the_lowered_history_limit_at_once(self):
        for index in range(10):
            cfg.append_history({"ts": "now", "text": str(index)})
        self.write_config({"history_limit": 3})
        self.window(cfg.Config())._save()
        self.assertEqual(len(cfg.read_history()), 3)

    def test_saving_tells_whoever_is_listening(self):
        conf = cfg.Config()
        window = self.window(conf)
        applied = []
        window.applied.connect(lambda: applied.append(True))
        window._save()
        self.assertEqual(applied, [True])

    def test_the_window_is_readable_in_turkish_too(self):
        self.write_config({"ui_language": "tr"})
        window = self.window(cfg.Config())
        self.assertEqual(window.windowTitle(), "Dikte Ayarları")

    def test_the_audio_file_switches_are_kept_without_the_save_button(self):
        """They are ticked to transcribe one file, not to fill in a form."""
        self.write_config({"file_timestamps": False, "file_cleanup": True})
        window = self.window(cfg.Config())
        window.file_timestamps.setChecked(True)
        window.file_cleanup.setChecked(False)
        stored = self.read_config_file()
        self.assertTrue(stored["file_timestamps"])
        self.assertFalse(stored["file_cleanup"])

    def test_loading_the_audio_file_tab_is_not_taken_for_a_change(self):
        self.write_config({"file_timestamps": True, "file_cleanup": False})
        conf = cfg.Config()
        with mock.patch.object(conf, "save") as save:
            window = self.window(conf)
        save.assert_not_called()
        self.assertTrue(window.file_timestamps.isChecked())
        self.assertFalse(window.file_cleanup.isChecked())

    def test_the_run_button_comes_back_when_the_stop_lands(self):
        """In whichever language, since the worker says so through t() too."""
        for language in ("auto", "tr"):
            with self.subTest(language=language):
                self.write_config({"ui_language": language})
                window = self.window(cfg.Config())
                window.file_run.setEnabled(False)
                window._on_file_progress(settings_ui.t("Stopped."))
                self.assertTrue(window.file_run.isEnabled())

    def test_stop_leaves_nothing_to_press_twice(self):
        window = self.window(cfg.Config())
        with mock.patch.object(window.transcriber, "stop") as stop:
            window.file_stop.setEnabled(True)
            window._stop_file()
        stop.assert_called_once_with()
        self.assertFalse(window.file_stop.isEnabled())


class MacSettings(Settings):
    """The same window and the same round trip, standing on a Mac.

    Nothing here is about macOS: it is the rest of the window, checked on the
    platform where three of its widgets are gone and one offers other keys.
    """

    platform = "darwin"
    changed: ClassVar[dict] = {**CHANGED, "paste_shortcut": "cmd+shift+v"}

    def test_there_is_no_install_button_where_nothing_is_installed(self):
        window = self.window(cfg.Config())
        labels = [button.text() for button in
                  window.findChildren(settings_ui.QPushButton)]
        self.assertFalse([text for text in labels if "shortcut" in text.lower()])

    def test_the_listener_is_not_offered_as_a_choice(self):
        """It is the whole mechanism there; turning it off would leave nothing."""
        window = self.window(cfg.Config())
        self.assertTrue(window.evdev_enabled.isHidden())

    def test_the_shortcut_tab_talks_about_this_session_and_no_other(self):
        """Carbon holds the keys here, so there is no command to bind and no
        /dev/input to be let into."""
        window = self.window(cfg.Config())
        text = self.shortcut_tab_text(window)
        self.assertIn("Dikte asks macOS for these combinations", text)
        self.assertNotIn("KWin", text)
        self.assertNotIn(ipc.command_for("toggle"), text)

    def test_the_paste_keys_on_offer_are_the_ones_a_mac_uses(self):
        window = self.window(cfg.Config())
        offered = [window.paste_shortcut.itemText(index)
                   for index in range(window.paste_shortcut.count())]
        self.assertEqual(offered, paste.MACOS.shortcuts)


class KdeSettings(Settings):
    """The same window on the one desktop that keeps a registry and makes you
    wait for it. Nothing here is about KDE: it is the rest of the window,
    checked on the platform where Install, Remove and the listener's own
    checkbox are all on screen."""

    desktop = "KDE"
    tools: ClassVar[tuple] = ("kwriteconfig6",)

    def test_the_shortcut_tab_talks_about_this_session_and_no_other(self):
        window = self.window(cfg.Config())
        text = self.shortcut_tab_text(window)
        self.assertIn("KWin only reads shortcut settings at startup", text)
        self.assertIn("Install as a KDE shortcut", text)
        self.assertNotIn("keeps no shortcut registry", text)
        self.assertNotIn(ipc.command_for("toggle"), text)
        # Here it is a choice: the wait for the next login, or the key press
        # reaching the focused application as well.
        self.assertFalse(window.evdev_enabled.isHidden())




class FakeAppKit:
    """The Objective-C runtime, answering rather than being asked.

    Stands in for what mac_window._appkit() loads, so what the indicator's
    window would have been sent can be read off `told` on any machine. The
    selectors come back as the names they were registered under, which is what
    lets the tests below name the message they mean.
    """

    def __init__(self, window=4242, panel=True, mask=0xe):
        self.objc = self
        self.window, self.panel, self.mask = window, panel, mask
        self.told = {}

    def objc_getClass(self, name):
        return 1 if name == b"NSPanel" else 0

    def selector(self, name):
        return name.decode()

    def shared(self, _class_name, _selector):
        return 1

    def ask(self, _to, selector):
        return self.window if selector == "window" else 0

    def ask_unsigned(self, _to, _selector):
        return self.mask

    def ask_of_class(self, _to, _selector, _klass):
        return self.panel

    def tell_bool(self, _to, selector, value):
        self.told[selector] = value

    tell_unsigned = tell_bool


class MacIndicatorWindow(DikteTest):
    """Which of the three AppKit settings the indicator's window is sent.

    The nonactivating bit is the one worth a test of its own: it is legal on an
    NSPanel and nowhere else, and sending it to a plain NSWindow raises an
    Objective-C exception, which through ctypes is not something Python can
    catch. It takes the whole process down. `_is_panel` is the only thing
    standing between the two, so what it answers has to decide what is sent.
    """

    def setUp(self):
        super().setUp()
        from dikte import mac_window
        self.mac_window = mac_window
        self.patch_attr(mac_window.QGuiApplication, "platformName",
                        staticmethod(lambda: "cocoa"))

    def told(self, **kwargs):
        """What keep_on_screen sends an indicator, against this AppKit."""
        appkit = FakeAppKit(**kwargs)
        self.patch_attr(self.mac_window, "_appkit", lambda: appkit)
        widget = overlay_module.Overlay()
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.close)
        self.answered = self.mac_window.keep_on_screen(widget)
        return appkit.told

    def test_a_window_that_is_not_a_panel_is_never_sent_the_style_mask(self):
        """The message that would kill the process. The other two still go."""
        told = self.told(panel=False)
        self.assertTrue(self.answered)
        self.assertNotIn("setStyleMask:", told)
        self.assertIs(told["setHidesOnDeactivate:"], False)
        self.assertEqual(told["setCollectionBehavior:"],
                         self.mac_window.BEHAVIOUR)

    def test_a_panel_without_the_bit_is_sent_the_mask_with_it_added(self):
        told = self.told(panel=True, mask=0xe)
        self.assertEqual(told["setStyleMask:"],
                         0xe | self.mac_window.NONACTIVATING_PANEL)

    def test_a_panel_that_already_has_the_bit_is_left_alone(self):
        """Every dictation runs this again, and a mask Cocoa did not need is a
        window it rebuilds underneath the indicator."""
        told = self.told(panel=True,
                         mask=0xe | self.mac_window.NONACTIVATING_PANEL)
        self.assertNotIn("setStyleMask:", told)

    def test_a_window_the_view_does_not_have_yet_is_not_messaged(self):
        self.assertEqual(self.told(window=0), {})
        self.assertFalse(self.answered)

    def test_nothing_is_sent_anywhere_but_cocoa(self):
        """Every other platform hands out a winId that means something else
        entirely, and messaging it crashes the run."""
        self.patch_attr(self.mac_window.QGuiApplication, "platformName",
                        staticmethod(lambda: "offscreen"))
        self.assertEqual(self.told(), {})
        self.assertFalse(self.answered)


class GivingTheFrontBack(DikteTest):
    """Opening the microphone brings Dikte to the front, and the window the
    user was dictating into goes inactive with the caret in it. Nothing can be
    asked of the capture session, so the front is put back afterwards.

    The watch is driven by hand here: what matters is what it decides, not how
    long Qt takes to tick.
    """

    def setUp(self):
        super().setUp()
        from dikte import app as dikte_module
        from dikte import mac_window
        self.dikte = dikte_module
        self.activated = []
        self.patch_attr(mac_window, "activate", self.activate)
        self.ticks = []
        outer = self

        class FakeTimer:
            """Records what it was asked to do and hands over the tick."""

            def __init__(self, _parent):
                self.interval = None
                self.running = False
                outer.ticks.append(self)

            def setInterval(self, milliseconds):
                self.interval = milliseconds

            def start(self):
                self.running = True

            def stop(self):
                self.running = False

            @property
            def timeout(self):
                return self

            def connect(self, slot):
                self.tick = slot

        self.patch_attr(dikte_module, "QTimer", FakeTimer)

        class BareDikte:
            """As much of the application as this one method touches."""

            app = None
            _front_watch = None
            _the_front = dikte_module.Dikte._the_front
            _give_the_front_back = dikte_module.Dikte._give_the_front_back
            _stop_watching_the_front = dikte_module.Dikte._stop_watching_the_front

        self.bare = BareDikte

    def activate(self, pid):
        self.activated.append(pid)
        return True

    def watching(self, was_in_front, dikte_in_front, on=None):
        from dikte import mac_window
        self.patch_attr(mac_window, "is_frontmost", lambda: dikte_in_front)
        dikte = on if on is not None else self.bare()
        dikte._give_the_front_back(was_in_front)
        return self.ticks[-1] if self.ticks else None

    def test_the_front_goes_back_to_whoever_had_it(self):
        watch = self.watching(4242, dikte_in_front=True)
        watch.tick()
        self.assertEqual(self.activated, [4242])
        self.assertTrue(watch.running)    # accepted is not the same as landed
        self.patch_attr(self.mac_window_module(), "is_frontmost", lambda: False)
        watch.tick()
        self.assertFalse(watch.running)

    def test_an_accepted_restore_is_not_sent_again_while_it_is_landing(self):
        watch = self.watching(4242, dikte_in_front=True)
        watch.tick()
        watch.tick()
        self.assertEqual(self.activated, [4242])
        self.assertTrue(watch.running)

    def test_an_accepted_restore_that_never_lands_still_times_out(self):
        watch = self.watching(4242, dikte_in_front=True)
        watch.tick()
        with mock.patch.object(self.dikte.time, "monotonic",
                               return_value=self.dikte.time.monotonic() + 60):
            watch.tick()
        self.assertFalse(watch.running)
        self.assertEqual(self.activated, [4242])

    def test_a_restore_the_system_refused_is_retried(self):
        from dikte import mac_window
        self.patch_attr(mac_window, "activate",
                        lambda pid: self.activated.append(pid) or False)
        watch = self.watching(4242, dikte_in_front=True)
        watch.tick()
        watch.tick()
        self.assertEqual(self.activated, [4242, 4242])
        self.assertTrue(watch.running)

    def test_a_front_that_was_never_taken_is_left_where_it_is(self):
        """The microphone does not always take it, and pulling an application
        forward that is already there is one flicker for nothing."""
        watch = self.watching(4242, dikte_in_front=False)
        watch.tick()
        self.assertEqual(self.activated, [])
        self.assertTrue(watch.running)    # still waiting for the moment

    def test_it_gives_up_rather_than_watching_for_ever(self):
        watch = self.watching(4242, dikte_in_front=False)
        with mock.patch.object(self.dikte.time, "monotonic",
                               return_value=self.dikte.time.monotonic() + 60):
            watch.tick()
        self.assertFalse(watch.running)
        self.assertEqual(self.activated, [])

    def test_a_dictation_started_in_dikte_itself_watches_nothing(self):
        """Settings is a window of ours, and the front is already where it
        belongs."""
        self.assertIsNone(self.watching(os.getpid(), dikte_in_front=True))

    def test_a_second_recording_calls_off_the_watch_the_first_one_left(self):
        """The older watch remembers where the older recording started, and by
        now that is the wrong window to be pulling forward."""
        dikte = self.bare()
        first = self.watching(4242, dikte_in_front=False, on=dikte)
        second = self.watching(1111, dikte_in_front=False, on=dikte)
        self.assertFalse(first.running)
        self.assertTrue(second.running)
        second.tick()                      # and the survivor is the new one
        self.patch_attr(self.mac_window_module(), "is_frontmost", lambda: True)
        second.tick()
        self.assertEqual(self.activated, [1111])

    def test_a_recording_nobody_needs_watching_for_still_calls_off_the_old_one(self):
        """Starting the next one from Dikte's own window is not a reason to
        leave the last one's watch running."""
        dikte = self.bare()
        first = self.watching(4242, dikte_in_front=False, on=dikte)
        self.watching(os.getpid(), dikte_in_front=False, on=dikte)
        self.assertFalse(first.running)
        self.assertIsNone(dikte._front_watch)

    def test_nobody_in_front_is_nobody_to_go_back_to(self):
        self.assertIsNone(self.watching(None, dikte_in_front=True))

    def mac_window_module(self):
        from dikte import mac_window
        return mac_window


class EveryRecordingProtectsTheFront(DikteTest):
    """Three ways in, dictation, agent and meeting, and all three open the same
    avfoundation capture, so all three take the front the same way. What is
    checked here is the order: the front has to be noted before the microphone
    is opened, and the watch armed after, or there is nothing to go back to.
    """

    def setUp(self):
        super().setUp()
        from dikte import app as dikte_module
        self.dikte = dikte_module
        self.order = []

    def app(self, **attributes):
        """A Dikte that records the order it does things in, and nothing else.

        The methods under test are called unbound against it, so the stand-ins
        go on the object rather than on the class.
        """
        dikte = mock.Mock(**attributes)
        dikte._run_id = 0
        dikte.conf = {"mic_target": "", "max_seconds": 60,
                      "meeting_mic_target": "", "meeting_system_target": "",
                      "meeting_max_seconds": 60}
        dikte._the_front.side_effect = lambda: self.order.append("noted") or 4242
        dikte._give_the_front_back.side_effect = (
            lambda pid: self.order.append(f"watching {pid}"))
        dikte._begin_recording = (
            lambda owner: self.dikte.Dikte._begin_recording(dikte, owner))
        dikte.recorder.start.side_effect = (
            lambda *_a: self.order.append("microphone"))
        dikte.meeting_recorder.start.side_effect = (
            lambda *_a: self.order.append("microphone"))
        return dikte

    def test_a_dictation_notes_the_front_before_the_indicator_is_even_shown(self):
        dikte = self.app(state=self.dikte.IDLE, recording=False)
        dikte.overlay.show_recording.side_effect = (
            lambda *_a: self.order.append("indicator"))
        self.dikte.Dikte.start(dikte)
        self.assertEqual(self.order,
                         ["noted", "indicator", "microphone", "watching 4242"])

    def test_the_agent_does_the_same(self):
        dikte = self.app(ask_state=self.dikte.IDLE, recording=False)
        self.dikte.Dikte.start_ask(dikte)
        self.assertEqual(self.order, ["noted", "microphone", "watching 4242"])

    def test_a_meeting_does_the_same(self):
        """The one most worth protecting: the user is in a call."""
        dikte = self.app(meeting_state=self.dikte.M_IDLE)
        dikte.meeting_recorder.active = True
        self.dikte.Dikte.start_meeting(dikte)
        self.assertEqual(self.order, ["noted", "microphone", "watching 4242"])

    def test_a_meeting_whose_microphone_never_opened_watches_nothing(self):
        dikte = self.app(meeting_state=self.dikte.M_IDLE)
        dikte.meeting_recorder.active = False
        self.dikte.Dikte.start_meeting(dikte)
        self.assertEqual(self.order, ["noted", "microphone"])

class Overlay(DikteTest):
    def overlay(self, **kwargs):
        widget = overlay_module.Overlay(**kwargs)
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.close)
        return widget

    def test_it_never_takes_focus(self):
        """It appears while you are typing; stealing the keyboard would be rude."""
        from PyQt6.QtCore import Qt
        flags = self.overlay().windowFlags()
        self.assertTrue(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)

    def test_it_lets_a_click_through_to_whatever_is_under_it(self):
        """It stays mapped while idle, so without this its corner of the screen
        would stop taking clicks for good. The widget attribute is not enough:
        on a top-level window it only makes Qt drop the event it already took."""
        from PyQt6.QtCore import Qt
        flags = self.overlay().windowFlags()
        self.assertTrue(flags & Qt.WindowType.WindowTransparentForInput)

    def test_the_one_that_takes_clicks_shrinks_out_of_the_way(self):
        """It has to stay clickable, so it cannot be transparent to input; it
        gets out of the way by leaving nothing there to click instead."""
        widget = self.overlay(dismissable=True)
        widget.show_busy("Asking Claude…")
        self.assertGreater(widget.width(), 1)
        widget.dismiss()
        self.assertEqual((widget.width(), widget.height()), (1, 1))
        widget.show_busy("Asking Claude…")
        self.assertGreater(widget.width(), 1)

    def test_recording_then_working_then_done(self):
        widget = self.overlay()
        widget.show_recording()
        self.assertTrue(widget.showing)
        widget.push_level(0.5)
        widget.set_seconds(3)
        widget.show_busy("Transcribing…")
        widget.show_done("Pasted")
        widget._conceal()
        self.assertFalse(widget.showing)

    def test_a_held_recording_says_so_and_stops_moving(self):
        """Everything about the ribbon says a recording is running; a pause the
        ribbon did not show would leave all of it saying the opposite."""
        widget = self.overlay()
        widget.show_recording()
        widget.push_level(0.8)
        widget.set_paused(True)
        levels = list(widget.levels)
        widget._tick()
        self.assertEqual(widget.levels, levels)
        widget.set_paused(False)
        widget._tick()
        self.assertNotEqual(widget.levels, levels)

    def test_a_new_recording_is_never_the_last_one_still_held(self):
        widget = self.overlay()
        widget.show_recording()
        widget.set_paused(True)
        widget.show_recording()
        self.assertFalse(widget.paused)

    def test_a_meeting_shows_both_sides(self):
        widget = self.overlay()
        widget.show_meeting()
        widget.push_levels(0.4, 0.7)
        self.assertTrue(widget.showing)

    def test_every_corner_is_understood(self):
        for corner in ("top-left", "top-right", "bottom-left", "bottom-right"):
            with self.subTest(corner=corner):
                widget = self.overlay(corner=corner)
                widget.show_recording()
                widget._reposition()

    def test_a_warning_and_an_error_both_show(self):
        widget = self.overlay()
        widget.show_warning("cleanup failed")
        widget.show_error("no microphone")

    def test_one_indicator_can_stack_above_another(self):
        first = self.overlay()
        first.show_recording()
        second = self.overlay(below=first)
        second.show_busy("Asking Claude…")
        self.assertTrue(second.showing)

    def test_a_job_in_progress_can_be_waved_away(self):
        """Ten minutes of work should not have to be watched for ten minutes."""
        widget = self.overlay(dismissable=True)
        widget.show_busy("Asking Claude…")
        widget.dismiss()
        self.assertFalse(widget.showing)

    def test_progress_stays_away_once_it_was_waved_off(self):
        widget = self.overlay(dismissable=True)
        widget.show_busy("Asking Claude…")
        widget.muted = True
        widget.dismiss()
        widget.show_busy("Reading a web page…")
        self.assertFalse(widget.showing)

    def test_the_outcome_shows_even_so(self):
        """Waving it away asks not to be watched, not to be kept in the dark."""
        widget = self.overlay(dismissable=True)
        widget.show_busy("Asking Claude…")
        widget.muted = True
        widget.dismiss()
        widget.show_done("Pasted")
        self.assertTrue(widget.showing)

    def test_a_new_run_starts_visible_whatever_the_last_one_did(self):
        widget = self.overlay(dismissable=True)
        widget.muted = True
        widget.show_recording()
        self.assertTrue(widget.showing)
        self.assertFalse(widget.muted)


class MeetingSources(DikteTest):
    """What the Meeting tab says about the far side, per sound system.

    The box that picks it is empty on a system that cannot record it, and an
    empty box with nothing next to it reads as a list that has not loaded yet.
    """

    def notes(self, meetings):
        with mock.patch.object(audio, "sound",
                               return_value=audio.PULSE._replace(
                                   meetings=meetings)), \
                only_these_tools(), \
                mock.patch.object(settings_ui.SettingsWindow, "_load_models"), \
                mock.patch.object(settings_ui.SettingsWindow,
                                  "_load_transcribe_models"), \
                mock.patch.object(settings_ui.SettingsWindow,
                                  "_load_codex_models"):
            window = settings_ui.SettingsWindow(cfg.Config())
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return " ".join(label.text()
                        for label in window.findChildren(settings_ui.QLabel))

    def test_a_system_that_cannot_record_the_far_side_says_so(self):
        self.assertIn("nothing that records what the speakers",
                      self.notes(meetings=False))

    def test_a_system_that_can_says_nothing_of_the_sort(self):
        self.assertNotIn("nothing that records what the speakers",
                         self.notes(meetings=True))


if __name__ == "__main__":
    unittest.main()


class LocalModels(DikteTest):
    """The download boxes, without a network and without either program."""

    def setUp(self):
        super().setUp()
        # A machine Dikte is actually installed on would otherwise answer the
        # "nothing can transcribe" question from its real binary and model.
        self.patch_attr(ggml, "BIN_DIR", self.path("bin"))
        self.patch_attr(ggml, "MODELS_DIR", self.path("models"))
        # And one with Codex on it would ask it for its model list.
        self.enterContext(mock.patch.object(settings_ui.SettingsWindow,
                                            "_load_codex_models"))

    def window(self, conf):
        window = settings_ui.SettingsWindow(conf)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    def test_it_opens_where_the_missing_model_is_fixed(self):
        # Nothing can transcribe on a fresh install, which is why this window
        # was opened at all.
        window = self.window(cfg.Config())
        self.assertEqual(window.tabs.currentIndex(), window.api_tab_index)

    def test_it_opens_where_it_was_left_when_everything_works(self):
        conf = self.config(transcribe_provider="openai", openai_api_key="sk-test")
        self.assertEqual(self.window(conf).tabs.currentIndex(), 0)

    def test_a_model_that_is_not_here_yet_survives_a_save(self):
        # The box is filled from what is on this disk, so a model that was
        # deleted from underneath is not in the list. Dropping it on save would
        # quietly empty the setting instead of asking for the download again.
        conf = self.config(local_model="ggml-large-v3-turbo-q5_0.bin")
        with mock.patch.object(QMessageBox, "information"):
            self.window(conf)._save()
        self.assertEqual(conf["local_model"], "ggml-large-v3-turbo-q5_0.bin")

    def test_nothing_is_fetched_for_a_window_nobody_opened(self):
        # DikteTest closes the network, so a request would fail the test. The
        # lists are asked for when the box is shown, not when it is built.
        window = self.window(cfg.Config())
        self.assertTrue(window.local_whisper._pending)

    def test_a_model_bigger_than_two_gigabytes_counts_up_rather_than_down(self):
        # Qt's int is C++'s 32-bit one, and a 2.3 GB model is more than fits in
        # it: the count came out the far side negative, at "-1%".
        box = self.window(cfg.Config()).local_llm
        box._report("model", 1_048_576, 2_489_757_856)
        _app.processEvents()
        self.assertIn("2.3 GB", box.status.text())
        self.assertNotIn("-", box.status.text())

    def test_each_download_reports_into_its_own_label(self):
        """The two can run at once; the tag, not a flag read later, says
        which label the bytes belong to."""
        box = self.window(cfg.Config()).local_llm
        box._report("program", 10, 100)
        box._report("model", 20, 100)
        _app.processEvents()
        self.assertIn("10", box.program_label.text())
        self.assertIn("20", box.status.text())

    def test_a_long_model_name_is_not_cut_in_half(self):
        # The list under a combo box takes the box's width and elides what does
        # not fit, in the middle: "ggml-org/Qwen....7B-Base-GGUF".
        box = self.window(cfg.Config()).local_llm
        box.repo.addItem("ggml-org/a-model-with-a-name-that-runs-on-and-on-GGUF")
        box._fit_popup(box.repo)
        view = box.repo.view()
        self.assertEqual(view.textElideMode(), settings_ui.Qt.TextElideMode.ElideNone)
        widest = max(box.repo.fontMetrics().horizontalAdvance(box.repo.itemText(row))
                     for row in range(box.repo.count()))
        self.assertGreaterEqual(view.minimumWidth(), widest)

    def test_only_the_chosen_transcriber_is_on_screen(self):
        window = self.window(self.config(transcribe_provider="openai"))
        self.assertTrue(window.stt_form.isRowVisible(window.transcribe_model_row))
        self.assertFalse(window.stt_form.isRowVisible(window.local_whisper))
        window._select_data(window.transcribe_provider, "local")
        self.assertFalse(window.stt_form.isRowVisible(window.transcribe_model_row))
        self.assertTrue(window.stt_form.isRowVisible(window.local_whisper))

    def test_only_the_chosen_cleaner_is_on_screen(self):
        window = self.window(cfg.Config())
        self.assertTrue(window.cleanup_form.isRowVisible(window.cleanup_model_row))
        self.assertFalse(window.cleanup_form.isRowVisible(window.local_llm))
        window._select_data(window.cleanup_provider, "local")
        self.assertTrue(window.cleanup_form.isRowVisible(window.local_llm))
        self.assertFalse(window.cleanup_form.isRowVisible(window.cleanup_model_row))
        # Its own thinking box, because the two default to opposite things.
        self.assertFalse(window.cleanup_form.isRowVisible(window.cleanup_reasoning))
