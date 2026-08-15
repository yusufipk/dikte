"""The two windows, on Qt's offscreen platform.

Not a look at the pixels: what these hold onto is the round trip through the
settings window. Every tab loads a setting into a widget and writes it back on
save, so a setting added to one half and not the other is silently reset the
next time anybody presses Save. That is the failure this catches.
"""

import unittest
from unittest import mock

from PyQt6.QtWidgets import QApplication, QMessageBox, QScrollArea

import cleanup
import config as cfg
import hotkey
import overlay as overlay_module
import settings_ui
from tests.support import DikteTest, only_these_tools, sandbox_shortcuts

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
    "evdev_hotkey": True,
    "history_limit": 50,
}


class Settings(DikteTest):
    def setUp(self):
        super().setUp()
        # No pactl, no model lists over the network, and no modal dialogue
        # waiting for somebody to press OK.
        self.enterContext(only_these_tools())
        self.enterContext(mock.patch.object(QMessageBox, "information"))
        # A shortcut somebody else already holds asks before it is installed,
        # and a modal question in a test is a run that never ends. Windows
        # reserves a good many combinations, so this is not hypothetical.
        self.enterContext(mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes))
        self.enterContext(mock.patch.object(settings_ui.SettingsWindow,
                                            "_load_models"))
        self.enterContext(mock.patch.object(settings_ui.SettingsWindow,
                                            "_load_transcribe_models"))
        sandbox_shortcuts(self)

    def window(self, conf):
        window = settings_ui.SettingsWindow(conf)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    def test_the_window_opens_with_every_tab_on_it(self):
        window = self.window(cfg.Config())
        tabs = window.findChildren(settings_ui.QTabWidget)[0]
        self.assertEqual(tabs.count(), 9)
        self.assertEqual(window.windowTitle(), "Dikte Settings")

    def test_saving_without_touching_anything_changes_nothing(self):
        """Every widget has to load what is stored, or Save writes its default
        over it. This says so for the whole table at once."""
        conf = cfg.Config()
        before = dict(conf.data)
        self.window(conf)._save()
        self.assertEqual(conf.data, before)

    def test_a_setting_of_your_own_survives_the_round_trip(self):
        self.write_config(CHANGED)
        conf = cfg.Config()
        self.window(conf)._save()
        # Read back through Config rather than out of the file: an API key is
        # not stored as it was typed on every platform, and what matters here
        # is that the window did not drop it on the way through.
        reloaded = cfg.Config()
        for key, value in CHANGED.items():
            with self.subTest(key=key):
                self.assertEqual(reloaded[key], value)

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
            box.setText("")
        window._save()
        self.assertTrue(conf["shortcut"])
        self.assertEqual(conf["shortcut"], hotkey.default_combo("toggle"))
        self.assertEqual(conf["cancel_shortcut"], "")
        self.assertEqual(conf["assistant_shortcut"], "")
        self.assertEqual(conf["meeting_shortcut"], "")

    def test_installing_the_discard_key_writes_its_own_entry(self):
        conf = cfg.Config()
        window = self.window(conf)
        window._shortcut_rows["cancel"][0].setText("Meta+Shift+Space")
        with mock.patch.object(settings_ui.hotkey, "install_shortcut",
                               return_value=(True, "saved")) as install:
            window._install_shortcut("cancel")
        combo, command = install.call_args.args
        self.assertEqual(combo, "Meta+Shift+Space")
        self.assertTrue(command.endswith(" cancel"))
        self.assertEqual(install.call_args.kwargs["desktop_id"],
                         hotkey.CANCEL_DESKTOP_ID)
        self.assertEqual(conf["cancel_shortcut"], "Meta+Shift+Space")

    def test_each_shortcut_has_a_capture_button_instead_of_a_preset_list(self):
        window = self.window(cfg.Config())
        self.assertEqual(set(window._shortcut_catchers), set(hotkey.SHORTCUTS))
        for box, _status, _missing in window._shortcut_rows.values():
            with self.subTest(box=box):
                self.assertIsInstance(box, settings_ui.QLineEdit)

    def test_a_captured_shortcut_is_selected_in_its_own_field(self):
        window = self.window(cfg.Config())
        window._shortcut_captured("meeting", "Ctrl+Shift+M")
        box, status, _missing = window._shortcut_rows["meeting"]
        self.assertEqual(box.text(), "Ctrl+Shift+M")
        self.assertIn("Ctrl+Shift+M", status.text())

    def test_one_combination_cannot_be_captured_for_two_actions(self):
        window = self.window(cfg.Config())
        window._shortcut_rows["toggle"][0].setText("Ctrl+Space")
        window._shortcut_rows["cancel"][0].setText("Ctrl+Alt+Space")
        window._shortcut_captured("cancel", "Ctrl+Space")
        box, status, _missing = window._shortcut_rows["cancel"]
        self.assertEqual(box.text(), "Ctrl+Alt+Space")
        self.assertIn("Ctrl+Space", status.text())

    def test_closing_while_capturing_gives_the_keyboard_back(self):
        window = self.window(cfg.Config())
        catchers = list(window._shortcut_catchers.values())
        for catcher in catchers:
            catcher.cancel = mock.Mock()
        window.close()
        self.assertTrue(all(catcher.cancel.called for catcher in catchers))

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

    def test_a_regular_indicator_is_transparent_to_native_input(self):
        from PyQt6.QtCore import Qt
        flags = self.overlay().windowFlags()
        self.assertTrue(flags & Qt.WindowType.WindowTransparentForInput)

    def test_a_dismissable_indicator_shrinks_when_concealed(self):
        widget = self.overlay(dismissable=True)
        widget.show_busy("Working")
        widget._conceal()
        self.assertEqual((widget.width(), widget.height()), (1, 1))

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


if __name__ == "__main__":
    unittest.main()


class LocalModels(DikteTest):
    """The download boxes, without a network and without either program."""

    def window(self, conf):
        window = settings_ui.SettingsWindow(conf)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    def test_it_opens_where_the_missing_model_is_fixed(self):
        # Nothing can transcribe on a fresh install, which is why this window
        # was opened at all.
        conf = cfg.Config()
        with mock.patch.object(conf, "transcribe_ready", return_value=False):
            window = self.window(conf)
        self.assertEqual(window.tabs.currentIndex(), window.api_tab_index)

    def test_the_model_page_scrolls_without_pushing_save_off_screen(self):
        window = self.window(cfg.Config())
        self.assertIsInstance(window.tabs.widget(window.api_tab_index), QScrollArea)

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
        box._downloading = True
        box._report(1_048_576, 2_489_757_856)
        _app.processEvents()
        self.assertIn("2.3 GB", box.status.text())
        self.assertNotIn("-", box.status.text())

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
