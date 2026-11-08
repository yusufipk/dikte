"""Native workspace navigation, capture boundaries and persisted results."""

import itertools
import os
from types import SimpleNamespace
from unittest import mock

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QAbstractButton, QApplication, QLineEdit, QMessageBox, QPushButton

from dikte import config as cfg, home_ui, i18n
from dikte.app import Dikte
from tests import test_ui
from tests.support import DikteTest


class HomeFixture(DikteTest):
    """A home window over a settings window and a stand-in controller."""

    def setUp(self):
        super().setUp()
        self.fixture = test_ui.Settings("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conf = self.fixture.config(transcribe_provider="openai", openai_api_key="test",
                                        cleanup_enabled=False)
        self.settings = self.fixture.window(self.conf)
        self.controller = SimpleNamespace(
            conf=self.conf, state="idle", ask_state="idle", meeting_state="idle",
            meeting_message="", home_messages={}, recording=False, paused=False,
            paste_override={}, open_settings=mock.Mock(), reset_conversation=mock.Mock(),
            _recorded_seconds=lambda: 65, meeting_elapsed=SimpleNamespace(elapsed=lambda: 90000),
        )
        for name in ("start", "stop", "start_ask", "stop_ask", "_toggle_pause", "_cancel",
                     "cancel_ask", "_toggle_meeting", "cancel_meeting"):
            setattr(self.controller, name, mock.Mock())
        self.window = home_ui.HomeWindow(self.controller, self.settings)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        self.window.show()
        QApplication.processEvents()


class Home(HomeFixture):
    def test_empty_gateway_model_is_not_called_a_custom_server_default(self):
        conf = self.fixture.config(cleanup_provider="groq", cleanup_groq_model="", cleanup_enabled=True)
        location = home_ui.processing_locations(conf)
        self.assertNotIn("Custom server default model", location)
        conf["cleanup_custom_preset"] = "other"
        self.assertIn("Custom server default model", home_ui.processing_locations(conf))

    def test_daily_tasks_are_reachable_outside_configuration(self):
        for mode in ("dictation", "file", "meeting", "ask", "history"):
            self.window.show_mode(mode)
            QApplication.processEvents()
            self.assertEqual(self.window.pages.currentWidget(), self.window.mode_pages[mode])
        self.window.show_mode("meeting")
        self.assertTrue(self.settings.minutes_view.isVisible())
        self.window.show_mode("history")
        self.assertTrue(self.settings.history.isVisible())
        self.assertEqual(self.settings.tabs.count(), 7)
        self.assertFalse(self.settings.tabs.tabBar().isVisible())

    def test_native_chrome_and_capture_geometry(self):
        self.assertFalse(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(self.window.capture_button.width(), self.window.capture_button.height())
        self.assertGreaterEqual(self.window.capture_button.height(), 100)

    def test_theme_preview_discard_and_save_keep_runtime_separate(self):
        self.assertEqual(self.conf["theme"], "nord")
        self.settings.theme_choice.setCurrentIndex(self.settings.theme_choice.findData("light"))
        self.assertEqual(self.conf["theme"], "nord")
        self.assertIn("#FFFFFF", self.settings.styleSheet())
        self.assertNotEqual(self.window.styleSheet(), self.settings.styleSheet())
        self.settings._discard_changes()
        self.assertEqual(self.settings.theme_choice.currentData(), "nord")
        self.settings.theme_choice.setCurrentIndex(self.settings.theme_choice.findData("dracula"))
        self.settings._save()
        self.assertEqual(cfg.Config()["theme"], "dracula")
        self.assertEqual(self.window._theme_name, "dracula")
        self.assertIn("#282A36", self.window.styleSheet())

    def test_theme_colors_are_per_overlay_and_unknown_name_falls_back(self):
        from dikte import overlay, theme
        dark = overlay.Overlay(theme_name="dark")
        light = overlay.Overlay(theme_name="light")
        self.addCleanup(dark.deleteLater)
        self.addCleanup(light.deleteLater)
        self.assertEqual(light.colors["base"].name(), "#ffffff")
        self.assertEqual(dark.colors["base"].name(), "#101010")
        dark.set_theme("dracula")
        self.assertEqual(light.colors["base"].name(), "#ffffff")
        self.assertEqual(theme.stylesheet("unknown"), theme.stylesheet("nord"))

    def test_wide_meeting_page_keeps_actions_compact(self):
        self.window.resize(900, 700)
        self.window.show_mode("meeting")
        QApplication.processEvents()
        self.assertLess(self.window.meeting_button.width(), 300)
        self.assertLessEqual(self.settings.minutes_view.width(), 680)

    def test_wide_windows_center_every_task_and_settings_page(self):
        """Dictation puts its recent entries beside it and the assistant how
        it runs, and the list pages use the width for a list beside a reader,
        centred too. Settings pages are centred in their pane."""
        self.window.resize(1900, 1000)
        sides = {"dictation": self.window.recent_panel, "ask": self.window.ask_side_panel}
        splits = {"file": self.settings.file_split, "meeting": self.settings.minutes_split}
        for mode, area in self.window.mode_pages.items():
            self.window.show_mode(mode)
            QApplication.processEvents()
            with self.subTest(mode=mode):
                if mode in sides:
                    self.assertGreater(area.widget().width(), 1000)
                    self.assertFalse(sides[mode].isHidden())
                elif mode == "history":
                    # One list, as wide as the page.
                    self.assertGreater(self.settings.history.width(), 1000)
                else:
                    self.assertGreater(area.widget().width(), 1000)
                    self.assertTrue(splits[mode].wide)
                self.assertAlmostEqual(area.widget().geometry().center().x(),
                                       area.viewport().rect().center().x(), delta=1)
        self.settings.resize(1900, 1000)
        self.settings.show()
        for index in range(self.settings.tabs.count()):
            self.settings.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            area = self.settings.tabs.widget(index)
            with self.subTest(settings=index):
                # Centred in the pane, so a capped page leaves no empty third.
                self.assertAlmostEqual(area.widget().geometry().center().x(),
                                       area.viewport().rect().center().x(), delta=1)
                self.assertLess(area.widget().width(), area.viewport().width())

    def test_meeting_and_assistant_actions_share_one_row(self):
        self.window.resize(620, 760)
        for mode, labels in (
            # Open the folder and Delete are in the More menu beside Copy.
            ("meeting", ("Copy", "More")),
            ("ask", (self.window.ask_button.text(), "New")),
        ):
            self.window.show_mode(mode)
            QApplication.processEvents()
            page = self.window.mode_pages[mode]
            buttons = {b.text() or b.toolTip(): b for b in page.findChildren(QAbstractButton)
                       if b.isVisible()}
            positions = [buttons[label].mapTo(page, buttons[label].rect().center()).y()
                         for label in labels]
            with self.subTest(mode=mode):
                self.assertLessEqual(max(positions) - min(positions), 1)

    def test_empty_state_does_not_invent_a_transcript(self):
        self.assertEqual(self.window.latest_text.toPlainText(), "")
        self.assertFalse(self.window.copy_button.isEnabled())
        self.assertFalse(self.window.open_button.isEnabled())

    def test_a_translation_says_so_and_offers_the_original(self):
        cfg.append_history({"ts": "2026-10-17 10:00:00", "raw": "book it for Thursday",
                            "text": "Buch es für Donnerstag.", "profile": "Email",
                            "translate_to": "de", "translated": True})
        self.window.refresh_results()
        self.assertIn("Email", self.window.latest_time.text())
        self.assertIn("into German", self.window.latest_time.text())
        self.assertFalse(self.window.original_button.isHidden())
        self.window.original_button.click()
        self.assertEqual(QApplication.clipboard().text(), "book it for Thursday")
        self.window.show_mode("history")
        self.settings.history.item(0).setSelected(True)
        self.assertTrue(self.settings.history_original.isEnabled())

    def test_an_ordinary_result_has_no_original_button(self):
        cfg.append_history({"raw": "uh, hi", "text": "Hi."})
        self.window.refresh_results()
        self.assertTrue(self.window.original_button.isHidden())

    def test_history_is_one_list_the_width_of_the_page(self):
        """No reader beside or under the list: the list takes the page, the
        search runs over its whole width, and nothing is clipped, in English
        and Turkish at the owner's window size and at a narrow one."""
        from PyQt6.QtWidgets import QPlainTextEdit
        cfg.append_history({"ts": "2026-11-08 10:00:00", "text": "One two three"})
        for language in ("en", "tr"):
            for width in (850, 460):
                with self.subTest(language=language, width=width):
                    i18n.set_language(language)
                    settings = self.fixture.window(self.conf)
                    window = home_ui.HomeWindow(self.controller, settings)
                    self.addCleanup(window.deleteLater)
                    self.addCleanup(window.close)
                    window.resize(width, 860)
                    window.show()
                    window.show_mode("history")
                    QApplication.processEvents()
                    page = window.mode_pages["history"]
                    self.assertFalse([box for box in page.findChildren(QPlainTextEdit)
                                      if box.isVisible()])
                    content = page.widget().width()
                    self.assertGreaterEqual(settings.history.width(), content - 2)
                    self.assertGreaterEqual(settings.history_search.width(),
                                            settings.history.width() - 2)
                    self.assertGreater(settings.history.height(), 500 if width == 850 else 250)
                    self.assertFalse(page.horizontalScrollBar().isVisible())

    def test_clearing_history_removes_the_latest_preview(self):
        cfg.append_history({"text": "Remove this preview"})
        self.window.refresh_results()
        self.window.show_mode("history")
        with mock.patch.object(self.settings, "_confirm", return_value=True):
            self.settings._clear_history()
        self.window.show_mode("dictation")
        self.assertEqual(self.window.latest_text.toPlainText(), "")
        self.assertFalse(self.window.copy_button.isEnabled())

    def test_missing_local_model_opens_setup_without_recording(self):
        self.conf["transcribe_provider"] = "local"
        self.window.refresh()
        # The welcome is over the setup tiles; the state title is empty.
        self.assertEqual(self.window.capture_status.text(), "")
        self.assertEqual(self.window.capture_status.accessibleName(), "Welcome to Dikte")
        self.assertEqual(self.window.welcome.text(), "Welcome to Dikte")
        # The ways to set up, and no microphone that cannot work yet, nor
        # anything left over from a setup that is not there.
        self.assertFalse(self.window.setup_tile.isHidden())
        self.assertEqual([tile.title.text() for tile in self.window.setup_tiles],
                         ["Cloud service", "On this computer", "Shortcut"])
        self.assertTrue(self.window.capture_row.isHidden())
        self.assertFalse(self.window.capture_button.isEnabled())
        self.assertTrue(self.window.latest_card.isHidden())
        self.assertTrue(self.window.footer.isHidden())
        self.window._capture()
        self.controller.start.assert_not_called()
        self.controller.open_settings.assert_called_once()
        self.assertEqual(self.settings.tabs.currentIndex(), self.settings.api_tab_index)
        self.assertEqual(self.controller.paste_override, {})

    def test_button_capture_never_automatically_pastes(self):
        def start():
            self.assertIs(self.controller.paste_override["dictation"], False)
            self.controller.state = "recording"
            self.controller.recording = True
        self.controller.start.side_effect = start
        self.window._capture()
        self.controller.start.assert_called_once()
        self.assertFalse(self.controller.paste_override["dictation"])
        self.assertIn("01:05", self.window.capture_status.text())
        self.assertTrue(self.window.pause_button.isVisible())
        self.window._capture()
        self.controller.stop.assert_called_once()

    def test_failed_capture_does_not_leak_a_paste_override(self):
        self.window._capture()
        self.assertEqual(self.controller.paste_override, {})

    def test_busy_capture_can_queue_but_does_not_steal_assistant_microphone(self):
        self.controller.state = "busy"
        self.window.refresh()
        self.assertTrue(self.window.capture_button.isEnabled())
        self.controller.ask_state = "recording"
        self.controller.recording = True
        self.window.refresh()
        self.assertFalse(self.window.capture_button.isEnabled())
        self.window._capture()
        self.controller.start.assert_not_called()

    def test_pause_cancel_and_failures_are_visible(self):
        self.controller.state = "recording"
        self.controller.recording = True
        self.controller.paused = True
        self.window.refresh()
        self.assertIn("Paused", self.window.capture_status.text())
        self.window._pause()
        self.controller._toggle_pause.assert_called_once()
        self.window._cancel_capture()
        self.controller._cancel.assert_called_once()
        self.controller.home_messages["dictation"] = "Microphone permission denied"
        self.window.refresh()
        self.assertIn("permission denied", self.window.capture_error.text())

    def test_real_latest_dictation_is_separate_from_assistant_answer(self):
        cfg.append_history({"ts": "2026-09-09 12:00:00", "text": "Actual transcript"})
        cfg.append_history({"mode": "ask", "text": "Actual answer"})
        self.window.refresh_results()
        self.assertEqual(self.window.latest_text.toPlainText(), "Actual transcript")
        self.assertIn("Actual answer", self.window.ask_thread.text())
        cursor = self.window.latest_text.textCursor()
        cursor.setPosition(3)
        self.window.latest_text.setTextCursor(cursor)
        self.window.refresh()
        self.window.refresh_results()
        self.assertEqual(self.window.latest_text.textCursor().position(), 3)
        cfg.clear_history()
        self.window.refresh_results()
        self.assertEqual(self.window.latest_text.toPlainText(), "")

    def test_processing_summary_uses_full_models_and_actual_acceleration(self):
        self.conf["transcribe_provider"] = "local"
        self.conf["local_model"] = "ggml-large-v3-turbo-q5_0.bin"
        self.conf["cleanup_enabled"] = True
        self.conf["cleanup_provider"] = "local"
        self.conf["local_llm_model"] = "gemma-3-4b-it-Q4_K_M.gguf"
        self.conf["local_gpu"] = True
        with mock.patch.object(home_ui.ggml, "state", return_value={
            "whisper": {"running": True, "backend": "CPU"},
            "llama": {"running": True, "backend": "Vulkan"},
        }):
            text = home_ui.processing_locations(self.conf)
        self.assertIn("ggml-large-v3-turbo-q5_0.bin (Local CPU)", text)
        self.assertIn("gemma-3-4b-it-Q4_K_M.gguf (Local GPU)", text)
        self.assertNotIn("API", text)
        with mock.patch.object(home_ui.ggml, "state", return_value={}):
            text = home_ui.processing_locations(self.conf)
        self.assertNotIn("GPU", text)
        self.assertIn("(Local)", text)
        self.assertIn(self.conf["meeting_model"], home_ui.processing_locations(self.conf, "meeting"))
        self.conf["assistant_cleanup"] = True
        self.conf["cleanup_provider"] = "gemini"
        self.conf["assistant_provider"] = "codex"
        text = home_ui.processing_locations(self.conf, "ask")
        self.assertIn(self.conf["cleanup_gemini_model"] + " (API)", text)
        self.assertIn("Codex default model (CLI)", text)

    def test_timestamped_file_summary_uses_the_timestamp_model(self):
        self.conf["transcribe_provider"] = "openrouter"
        self.conf["openrouter_transcribe_model"] = "google/gemini-audio"
        self.conf["openrouter_file_model"] = "openai/whisper-1"
        text = home_ui.processing_locations(self.conf, "file", file_timestamps=True)
        self.assertIn("openai/whisper-1 (API)", text)
        self.assertNotIn("google/gemini-audio", text)

    def test_meeting_summary_uses_segment_model_even_without_file_timestamps(self):
        self.conf["transcribe_provider"] = "openai"
        self.conf["transcribe_model"] = "gpt-4o-transcribe"
        text = home_ui.processing_locations(self.conf, "meeting", file_timestamps=False)
        self.assertIn("whisper-1 (API)", text)
        self.assertNotIn("gpt-4o-transcribe", text)
        self.conf["transcribe_provider"] = "openrouter"
        self.conf["openrouter_file_model"] = "mistralai/voxtral-small-24b-2507"
        text = home_ui.processing_locations(self.conf, "meeting", file_timestamps=False)
        self.assertIn("mistralai/voxtral-small-24b-2507 (API)", text)

    def test_unsupported_meeting_is_disabled(self):
        self.enterContext(mock.patch.object(home_ui.audio, "sound", return_value=SimpleNamespace(meetings=False)))
        self.window.show_mode("meeting")
        self.assertFalse(self.window.meeting_button.isEnabled())
        self.assertIn("not supported", self.window.meeting_hint.text())
        self.window._meeting()
        self.controller._toggle_meeting.assert_not_called()

    def test_the_requesty_agent_is_available_on_its_own_key(self):
        self.conf["assistant_provider"] = "requesty"
        self.conf["openrouter_api_key"] = "sk-or-test"
        self.conf["requesty_api_key"] = ""
        with mock.patch.dict("os.environ", {"PATH": os.environ.get("PATH", "")}, clear=True):
            self.assertFalse(self.window._assistant_available())
            self.conf["requesty_api_key"] = "rqsty-test"
            self.assertTrue(self.window._assistant_available())

    def test_assistant_scope_uses_actual_shortcut_and_permissions(self):
        self.conf["assistant_provider"] = "codex"
        self.conf["assistant_shortcut"] = "Ctrl+Alt+A"
        self.conf["assistant_dir"] = self.root
        self.conf["assistant_codex_sandbox"] = "danger-full-access"
        self.window.show_mode("ask")
        self.assertIn("Ctrl+Alt+A", self.window.ask_scope.text())
        self.assertIn(self.root, self.window.ask_scope.text())
        self.assertIn("No sandbox at all", self.window.ask_scope.text())
        self.assertEqual(self.window.ask_button.text(), "Set up assistant")
        self.window._ask()
        self.controller.start_ask.assert_not_called()
        self.controller.open_settings.assert_called_once()

    def test_failed_settings_save_keeps_runtime_config_and_form_edits(self):
        before = dict(self.conf.data)
        self.settings.auto_paste.setChecked(not self.conf["auto_paste"])
        self.assertEqual(self.settings.dirty_label.text(), "Unsaved changes")
        with mock.patch.object(self.conf, "save", side_effect=OSError("disk full")), mock.patch.object(QMessageBox, "warning"):
            self.settings._save()
        self.assertEqual(self.conf.data, before)
        self.assertNotEqual(self.settings.auto_paste.isChecked(), self.conf["auto_paste"])
        self.settings.file_path = "/tmp/chosen.wav"
        self.settings._discard_changes()
        self.assertEqual(self.settings.dirty_label.text(), "")
        self.assertEqual(self.settings.file_path, "/tmp/chosen.wav")

    def test_small_window_keeps_navigation_and_footer_accessible(self):
        self.window.resize(460, 460)
        QApplication.processEvents()
        for mode in ("dictation", "file", "meeting", "ask", "history"):
            self.window.show_mode(mode)
            QApplication.processEvents()
            if self.window.footer.isVisible():
                self.assertTrue(self.window.rect().contains(self.window.footer.geometry()))
            self.assertTrue(self.window.rect().contains(self.window.mode_buttons["dictation"].geometry().topLeft()))

    def test_apply_merges_unrelated_cli_changes_and_keeps_user_edits(self):
        self.settings.auto_paste.setChecked(False)
        self.conf["shortcut"] = "Ctrl+Shift+F9"
        self.conf["groq_transcribe_model"] = "external-model"
        self.settings.refresh_configuration()
        self.assertFalse(self.settings.auto_paste.isChecked())
        self.settings._save()
        self.assertFalse(self.conf["auto_paste"])
        self.assertEqual(self.conf["shortcut"], "Ctrl+Shift+F9")
        self.assertEqual(self.conf["groq_transcribe_model"], "external-model")
        self.assertEqual(self.settings._shortcut_rows["toggle"][0].currentText(), "Ctrl+Shift+F9")
        self.assertEqual(self.settings.dirty_label.text(), "")

    def test_clean_form_refreshes_from_cli_without_changing_file_result(self):
        self.settings.file_output.setPlainText("Existing file result")
        self.conf["shortcut"] = "Ctrl+Alt+F9"
        self.settings.refresh_configuration()
        self.assertEqual(self.settings._shortcut_rows["toggle"][0].currentText(), "Ctrl+Alt+F9")
        self.assertEqual(self.settings.file_output.toPlainText(), "Existing file result")
        self.assertEqual(self.settings.dirty_label.text(), "")

    def test_cached_provider_model_edits_remain_dirty_after_switching_back(self):
        self.settings._select_data(self.settings.transcribe_provider, "custom")
        self.settings._select_data(self.settings._preset_widgets["transcribe"]["preset"], "groq")
        self.settings.transcribe_model.setCurrentText("my-groq-model")
        self.settings._select_data(self.settings.transcribe_provider, "openai")
        self.assertEqual(self.settings.dirty_label.text(), "Unsaved changes")
        self.settings._save()
        self.assertEqual(self.conf["groq_transcribe_model"], "my-groq-model")

    def test_cli_selected_new_source_is_resolved_when_settings_reopens(self):
        self.conf["mic_target"] = "new-usb"
        self.settings.refresh_configuration()
        with mock.patch.object(home_ui.audio, "list_sources", return_value=[("new-usb", "New USB microphone")]), mock.patch.object(home_ui.audio, "list_monitors", return_value=[]):
            self.settings.refresh_sources()
        self.assertEqual(self.settings.mic.currentData(), "new-usb")
        self.assertEqual(self.settings.mic.currentText(), "New USB microphone")
        self.settings._save()
        self.assertEqual(self.conf["mic_target"], "new-usb")

    def test_language_rebuild_preserves_file_result_and_active_mode(self):
        from dikte.meeting import MeetingPipeline
        controller = Dikte.__new__(Dikte)
        controller.__dict__.update(vars(self.controller))
        controller.meetings = MeetingPipeline(self.conf)
        controller._make_settings()
        old_settings = controller.settings_window
        old_home = home_ui.HomeWindow(controller, old_settings)
        controller.home_window = old_home
        old_home.show_mode("file")
        old_home.show()
        old_settings.file_path = "/tmp/chosen.wav"
        old_settings.file_label.setText("chosen.wav")
        old_settings.file_output.setPlainText("Retained transcript")
        old_settings.file_segments = [{"text": "Retained transcript", "start": 0, "end": 2}]
        i18n.set_language("tr")
        controller._reopen_settings()
        self.addCleanup(controller.settings_window.deleteLater)
        self.addCleanup(controller.settings_window.close)
        self.addCleanup(controller.home_window.deleteLater)
        self.addCleanup(controller.home_window.close)
        self.assertEqual(controller.home_window.mode, "file")
        self.assertEqual(controller.settings_window.file_path, "/tmp/chosen.wav")
        self.assertEqual(controller.settings_window.file_output.toPlainText(), "Retained transcript")
        self.assertTrue(controller.settings_window.file_save_srt.isEnabled())
        self.assertEqual(controller.home_window.mode_buttons["file"].text(), "Dosya")

    def test_disconnected_configured_source_survives_an_unrelated_apply(self):
        self.conf["mic_target"] = "disconnected-usb"
        self.settings.refresh_configuration()
        self.assertEqual(self.settings.mic.currentData(), "disconnected-usb")
        self.settings.auto_paste.setChecked(False)
        self.settings._save()
        self.assertEqual(self.conf["mic_target"], "disconnected-usb")

    def test_source_refresh_keeps_selection_and_discovers_hotplugged_devices(self):
        self.settings.mic.addItem("Old microphone", "old")
        self.settings.mic.setCurrentIndex(self.settings.mic.findData("old"))
        with mock.patch.object(home_ui.audio, "list_sources", return_value=[("usb", "USB microphone")]), mock.patch.object(home_ui.audio, "list_monitors", return_value=[("loop", "Loopback")]):
            self.settings.refresh_sources()
        self.assertEqual(self.settings.mic.currentData(), "old")
        self.assertGreaterEqual(self.settings.mic.findData("usb"), 0)
        self.assertGreaterEqual(self.settings.meeting_mic.findData("usb"), 0)
        self.assertGreaterEqual(self.settings.meeting_system.findData("loop"), 0)

    def test_missing_assistant_directory_displays_the_actual_fallback(self):
        self.conf["assistant_provider"] = "codex"
        self.conf["assistant_dir"] = "/does/not/exist/dikte-test"
        self.window.show_mode("ask")
        self.assertNotIn(self.conf["assistant_dir"], self.window.ask_scope.text())
        self.assertIn(home_ui.assistant.working_dir(self.conf), self.window.ask_scope.text())

    def test_turkish_task_labels_and_runtime_status(self):
        i18n.set_language("tr")
        self.window.refresh()
        # Idle, no title on screen, but the state still has its name.
        self.assertEqual(self.window.capture_status.text(), "")
        self.assertEqual(self.window.capture_status.accessibleName(), "Konuşmaya hazır")
        self.assertIn("Dikte:", self.window.footer.toolTip())
        self.assertIn("Temizleme:", self.window.footer.toolTip())

    def test_completed_run_refreshes_workspace_without_changing_controller_state(self):
        controller = Dikte.__new__(Dikte)
        controller.home_messages = {}
        controller.home_window = self.window
        controller._waiters = {}
        cfg.append_history({"text": "Finished"})
        controller._settle("dictation", {"ok": True, "text": "Finished"})
        self.assertEqual(self.window.latest_text.toPlainText(), "Finished")
        self.assertEqual(controller.home_messages["dictation"], "Transcript ready")


class Round4(HomeFixture):
    """What the home window says before and after a run, and the first run."""


    def test_the_capture_line_says_where_the_text_goes(self):
        self.conf["shortcut"] = "ctrl+space"
        self.conf["auto_paste"] = True
        self.window.refresh()
        self.assertEqual(self.window.capture_hint.text(),
                         "Ctrl + Space to start, again to stop and paste")
        self.assertEqual(self.window.capture_action.text(), "Record")
        self.conf["auto_paste"] = False
        self.window.refresh()
        self.assertEqual(self.window.capture_hint.text(),
                         "Ctrl + Space to start, again to stop and copy")

    def test_the_invitation_takes_the_shortcut_line(self):
        """Right after setup the invitation is the line under the button,
        not a second line beneath it."""
        self.conf["shortcut"] = "ctrl+space"
        self.window._invite = True
        self.window.refresh()
        self.assertEqual(self.window.capture_hint.text(),
                         "All set. Press Ctrl + Space in any program and start talking.")
        self.assertEqual(self.window.capture_error.text(), "")

    def test_a_button_recording_stops_and_copies(self):
        def start():
            self.controller.state = "recording"
            self.controller.recording = True
        self.controller.start.side_effect = start
        self.window._capture()
        self.assertEqual(self.window.capture_button.accessibleName(), "Stop and copy")
        self.assertEqual(self.window.capture_action.text(), "Stop and copy")

    def test_a_failure_offers_its_own_retry(self):
        kept = cfg.RECORDINGS_DIR / "kept.wav"
        cfg.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(b"RIFF0000WAVE")
        cfg.append_history({"ts": "2026-11-08 10:00:00", "failed": True, "error": "Timed out",
                            "audio": str(kept), "text": "", "raw": ""})
        self.window.refresh_results()
        self.controller.home_messages["dictation"] = "Failed: Timed out\nThe recording was kept: x"
        self.window.refresh()
        self.assertIn("Timed out", self.window.capture_error.notice.text())
        self.assertEqual(self.window.capture_retry.text(), "Try again")
        asked = []
        self.settings.retry_requested.connect(asked.append)
        self.window._retry_failure()
        self.assertEqual(asked[0]["error"], "Timed out")

    def test_a_failed_row_is_titled_by_what_failed(self):
        cfg.append_history({"ts": "2026-11-08 10:00:00", "failed": True, "error": "Timed out"})
        self.window.refresh_results()
        item = self.window.recent_list.item(0)
        from dikte import widgets
        self.assertEqual(item.data(widgets.BODY_ROLE), "Dictation failed")
        self.assertIn("Timed out", item.data(widgets.META_ROLE))

    def test_the_conversation_keeps_its_earlier_turns(self):
        self.conf["assistant_session_minutes"] = 30
        for ts, question, answer in (("2026-11-08 08:00:00", "Old", "Gone"),
                                     ("2026-11-08 10:00:00", "First", "One"),
                                     ("2026-11-08 10:10:00", "Second", "Two")):
            cfg.append_history({"ts": ts, "mode": "ask", "question": question, "text": answer})
        cfg.append_history({"ts": "2026-11-08 10:11:00", "mode": "ask", "assist": "edit",
                            "question": "Selected", "text": "Not a turn"})
        self.window.refresh_results()
        text = self.window.ask_thread.text()
        self.assertEqual(text, "First\n\nOne\n\nSecond\n\nTwo")
        self.window._new_conversation()
        self.controller.reset_conversation.assert_called_once()
        self.assertEqual(self.window.ask_thread.text(), "")

    def test_the_question_shows_while_the_answer_is_on_its_way(self):
        self.controller.ask_state = "busy"
        self.controller.home_messages["ask_question"] = "What is next?"
        self.window.refresh()
        self.assertIn("What is next?", self.window.ask_thread.text())

    def test_meters_and_a_pinned_row_while_a_meeting_records(self):
        self.controller.meeting_state = "recording"
        self.controller.overlay = SimpleNamespace(levels=[0.5] * 22, levels2=[0.0] * 22)
        self.window.show_mode("meeting")
        self.window._feed_meters()
        self.assertEqual(self.settings.minutes_list.item(0).text(), "Recording now")
        # The live row comes in picked, and the reader shows it live.
        self.assertIs(self.settings.minutes_list.currentItem(),
                      self.settings.minutes_list.item(0))
        self.assertIs(self.settings.minutes_reader.currentWidget(), self.window.meeting_live)
        self.window._heard_at["theirs"] -= 60
        self.window.refresh()
        self.assertIn("system audio", self.window.meeting_silence.text())
        self.controller.meeting_state = "idle"
        self.window.refresh()
        self.assertNotEqual(self.settings.minutes_list.count() and
                            self.settings.minutes_list.item(0).text(), "Recording now")

    def test_state_titles_are_named_without_their_clock(self):
        self.controller.state = "recording"
        self.controller.recording = True
        self.window.refresh()
        self.assertEqual(self.window.capture_status.accessibleName(), "Recording")
        self.assertIn("01:05", self.window.capture_status.text())



class Round6(HomeFixture):
    """Live states that say what is true, and the profile the next dictation
    uses."""

    def record(self, from_button):
        self.controller.paste_override = {"dictation": False} if from_button else {}
        self.controller.state = "recording"
        self.controller.recording = True
        self.window.refresh()

    def test_the_hint_says_how_the_shortcut_stops_this_recording(self):
        self.conf["shortcut"] = "ctrl+space"
        self.conf["auto_paste"] = True
        self.record(from_button=False)
        self.assertEqual(self.window.capture_hint.text(), "Ctrl + Space to stop and paste")
        self.record(from_button=True)
        self.assertEqual(self.window.capture_hint.text(), "Ctrl + Space to stop and copy")

    def test_caption_tooltip_and_name_are_the_same_action(self):
        for from_button in (True, False):
            self.record(from_button)
            button = self.window.capture_button
            self.assertEqual(self.window.capture_action.text(), button.accessibleName())
            self.assertEqual(button.toolTip(), button.accessibleName())
            self.assertEqual(button.accessibleName(), "Stop and copy")

    def test_while_transcribing_the_hint_keeps_its_place(self):
        self.controller.state = "busy"
        self.window.refresh()
        hint = self.window.capture_hint
        self.assertTrue(hint.isHidden())
        self.assertTrue(hint.sizePolicy().retainSizeWhenHidden())
        self.assertFalse(self.window.capture_action.isHidden())
        self.assertTrue(self.window.capture_action.text())

    def test_paused_the_meter_holds_still_in_the_paused_colour(self):
        self.controller.overlay = SimpleNamespace(levels=[0.5] * 22, levels2=[])
        self.record(from_button=True)
        self.window._feed_meters()
        self.assertEqual(self.window.capture_meter.role, "danger")
        self.controller.paused = True
        self.window.refresh()
        before = list(self.window.capture_meter.levels)
        self.controller.overlay.levels = [0.9] * 22
        self.window._feed_meters()
        self.assertEqual(self.window.capture_meter.levels, before)
        self.assertEqual(self.window.capture_meter.role, "warn")

    def test_a_failure_makes_the_capture_area_compact(self):
        self.controller.home_messages["dictation"] = "Failed: Timed out"
        self.window.refresh()
        self.assertEqual(self.window.capture_button.width(), 72)
        self.assertTrue(self.window.capture_hint.isHidden())
        self.controller.home_messages["dictation"] = ""
        self.window.refresh()
        self.assertEqual(self.window.capture_button.width(), 112)
        self.assertFalse(self.window.capture_hint.isHidden())
        # A dictation that went well leaves a plain line, not a notice.
        self.controller.home_messages["dictation"] = "Transcript ready"
        self.window.refresh()
        self.assertEqual(self.window.capture_button.width(), 112)

    def test_the_profile_chip_shows_and_switches_the_next_profile(self):
        self.conf["profiles"] = [{"name": "Mail", "translate_to": "de"}]
        self.conf["translation_enabled"] = True
        self.settings.profiles_editor.load(self.conf)
        self.settings._saved_form = self.settings._form_values()
        self.window.refresh()
        chip = self.window.profile_chip
        self.assertFalse(chip.isHidden())
        self.assertEqual(chip.text(), "Default")
        self.window._fill_profile_menu()
        mail = next(action for action in self.window.profile_menu.actions()
                    if action.text() == "Mail")
        mail.trigger()
        self.assertEqual(self.conf["profile_override"], "Mail")
        self.assertEqual(chip.text(), "Mail \u2192 German")
        # Kept, not waiting for Apply.
        self.assertFalse(self.settings.apply_button.isEnabled())
        self.assertEqual(self.settings.profiles_editor.active_profile()["name"], "Mail")
        self.window._use_profile("")
        self.assertEqual(self.conf["profile_override"], "")
        self.assertEqual(chip.text(), "Default")

    def test_no_profiles_no_chip(self):
        self.window.refresh()
        self.assertTrue(self.window.profile_chip.isHidden())

    def test_the_new_conversation_button_says_so(self):
        self.assertEqual(self.window.ask_new.text(), "New")
        self.assertEqual(self.window.ask_new.accessibleName(), "Start a new conversation")

class Round8(HomeFixture):
    def chip(self):
        self.window.refresh()
        return self.window.footer.chips[0].full

    def test_the_footer_chip_names_the_speech_model_not_the_provider(self):
        self.conf["transcribe_model"] = "openai/whisper-1"
        self.assertEqual(self.chip(), "Speech: whisper-1")
        tip = self.window.footer.toolTip()
        self.assertIn("Provider: OpenAI", tip)
        self.assertIn("openai/whisper-1", tip)

    def test_the_footer_chip_names_a_custom_or_local_model(self):
        self.conf["transcribe_provider"] = "custom"
        self.conf["custom_base_url"] = "http://127.0.0.1:9/v1"
        self.conf["custom_transcribe_model"] = "Systran/faster-whisper-large-v3"
        self.assertEqual(self.chip(), "Speech: faster-whisper-large-v3")
        self.conf["custom_transcribe_model"] = ""
        self.assertEqual(self.chip(), "Speech: Server default")
        self.assertEqual(home_ui.model_name("ggml-large-v3-turbo-q5_0.bin", "local"),
                         "large-v3-turbo-q5_0")
        self.assertEqual(home_ui.model_name("", "local"), "")

    def test_the_footer_chip_names_a_gateway_preset_and_where_it_runs(self):
        self.conf["transcribe_provider"] = "custom"
        self.conf["transcribe_custom_preset"] = "groq"
        self.conf["groq_transcribe_model"] = "whisper-large-v3-turbo"
        self.assertEqual(self.chip(), "Speech: whisper-large-v3-turbo")
        self.assertIn("Provider: Groq", self.window.footer.toolTip())
        # A gateway has no server default; an empty box is a model to choose.
        self.conf["groq_transcribe_model"] = ""
        self.assertEqual(self.chip(), "Speech: Model not selected")

    def test_the_assistant_model_of_a_custom_server(self):
        self.conf["assistant_provider"] = "custom"
        self.conf["assistant_custom_preset"] = "other"
        self.conf["assistant_custom_model"] = ""
        self.assertEqual(home_ui.assistant_model(self.conf), "Custom server default model")
        self.conf["assistant_custom_preset"] = "opencode"
        self.conf["assistant_opencode_model"] = "glm-5.3"
        self.assertEqual(home_ui.assistant_model(self.conf), "glm-5.3")
        self.conf["assistant_opencode_model"] = ""
        self.assertEqual(home_ui.assistant_model(self.conf), "")
        self.assertIn("Model not selected", home_ui.processing_locations(self.conf, "ask"))

    def test_history_names_the_gateway_an_older_row_was_asked(self):
        from dikte import settings_ui
        self.assertEqual(settings_ui.agent_name("requesty"), "Requesty")
        self.assertEqual(settings_ui.agent_name("custom"), "Custom server")
        self.assertEqual(settings_ui.agent_name(None), "Claude")
        self.assertEqual(settings_ui.agent_name("", ""), "")

    def test_the_dictation_title_and_meter_share_the_button_axis(self):
        self.window.resize(850, 860)
        axis = lambda widget: widget.mapTo(self.window, widget.rect().center()).x()
        for state in ("idle", "recording", "busy"):
            self.controller.state = state
            self.controller.recording = state != "idle"
            self.window.refresh()
            QApplication.processEvents()
            button = axis(self.window.capture_button)
            title = self.window.capture_status
            if state == "idle":
                # Idle has no title at all, and takes no room for one.
                self.assertTrue(title.isHidden())
                continue
            self.assertEqual(title.alignment() & Qt.AlignmentFlag.AlignHCenter,
                             Qt.AlignmentFlag.AlignHCenter)
            margins = title.contentsMargins()
            self.assertEqual(margins.left(), margins.right())
            self.assertLessEqual(abs(axis(title) - button), 1, state)
            if state == "recording":
                self.assertLessEqual(abs(axis(self.window.capture_meter) - button), 1)

    def test_reading_boxes_wrap_and_never_scroll_sideways(self):
        for editor in (self.window.latest_text, self.settings.minutes_view,
                       self.settings.file_output):
            self.assertEqual(editor.horizontalScrollBarPolicy(),
                             Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


class Setup(HomeFixture):

    def test_the_cloud_route_saves_the_service_and_its_key(self):
        from dikte import setup_ui
        self.conf["openai_api_key"] = ""
        self.conf["cleanup_provider"] = "openrouter"
        self.conf["cleanup_enabled"] = True
        self.settings._discard_changes()
        with mock.patch.dict("os.environ", {"PATH": os.environ.get("PATH", "")}, clear=True):
            dialog = setup_ui.SetupDialog(self.settings, "cloud")
            self.addCleanup(dialog.deleteLater)
            dialog.service.setCurrentIndex(dialog.service.findData("groq"))
            self.assertFalse(dialog.go.isEnabled())
            dialog.key.setText("gsk-test")
            self.assertTrue(dialog.go.isEnabled())
            done = []
            dialog.finished_setup.connect(lambda: done.append(True))
            dialog._save_cloud()
        stored = cfg.Config()
        # Groq is a Custom server preset now, and saved as one.
        self.assertEqual((stored["transcribe_provider"], stored.custom_preset("transcribe")),
                         ("custom", "groq"))
        self.assertEqual(stored["groq_api_key"], "gsk-test")
        self.assertTrue(stored.transcribe_ready())
        # No OpenRouter key to clean up with, so cleanup is left off rather
        # than failing on every dictation.
        self.assertFalse(stored["cleanup_enabled"])
        self.assertEqual(done, [True])

    def test_a_key_that_cleans_up_too_turns_cleanup_on_with_it(self):
        from dikte import setup_ui
        dialog = setup_ui.SetupDialog(self.settings, "cloud")
        self.addCleanup(dialog.deleteLater)
        dialog.key.setText("sk-or-test")
        dialog._save_cloud()
        stored = cfg.Config()
        self.assertEqual((stored["transcribe_provider"], stored["cleanup_provider"],
                          stored["cleanup_enabled"]), ("openrouter", "openrouter", True))

    def test_a_gateway_key_that_cleans_up_too_sets_both_presets(self):
        from dikte import setup_ui
        self.conf["openrouter_api_key"] = ""
        self.settings._discard_changes()
        with mock.patch.dict("os.environ", {"PATH": os.environ.get("PATH", "")}, clear=True):
            dialog = setup_ui.SetupDialog(self.settings, "cloud")
            self.addCleanup(dialog.deleteLater)
            dialog.service.setCurrentIndex(dialog.service.findData("gemini"))
            self.assertEqual(dialog.service.currentText(), "Google AI Studio")
            dialog.key.setText("AIza-test")
            dialog._save_cloud()
        stored = cfg.Config()
        self.assertEqual((stored["transcribe_provider"], stored.custom_preset("transcribe"),
                          stored["cleanup_provider"], stored.custom_preset("cleanup"),
                          stored["cleanup_enabled"]),
                         ("custom", "gemini", "custom", "gemini", True))
        self.assertEqual(stored["gemini_api_key"], "AIza-test")

    def test_a_stored_gateway_key_is_offered_again(self):
        from dikte import setup_ui
        self.conf["requesty_api_key"] = "rq-stored"
        self.settings._discard_changes()
        dialog = setup_ui.SetupDialog(self.settings, "cloud")
        self.addCleanup(dialog.deleteLater)
        dialog.service.setCurrentIndex(dialog.service.findData("requesty"))
        self.assertEqual(dialog.key.text(), "rq-stored")

    def test_every_offered_service_has_a_key_page_and_a_tester(self):
        from dikte import setup_ui, settings_ui
        for name in setup_ui.CLOUD:
            with self.subTest(service=name):
                self.assertIn(name, setup_ui.KEY_PAGES)
                self.assertTrue(name in cfg.TRANSCRIBERS or cfg.PRESETS[name].transcription)
        with mock.patch.object(settings_ui.api, "preset_models", return_value=["a", "b"]) as fetch:
            self.assertIn("2", settings_ui.key_check("groq", "gsk", "https://x"))
        self.assertEqual(fetch.call_args.args[0], "groq")
        self.assertTrue(fetch.call_args.kwargs["test_key"])


class LocalSetup(HomeFixture):
    """The focused local setup: one recommended model in words, and an honest
    line about whether anything still leaves the computer."""

    def dialog(self):
        from dikte import setup_ui
        dialog = setup_ui.SetupDialog(self.settings, "local")
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_a_model_file_is_named_the_way_people_call_it(self):
        from dikte import setup_ui
        self.assertEqual(setup_ui.friendly_model("ggml-large-v3-turbo-q5_0.bin"),
                         "Whisper Large v3 Turbo")
        self.assertEqual(setup_ui.friendly_model("ggml-base.en.bin"), "Whisper Base (English)")
        self.assertEqual(setup_ui.friendly_model("ggml-small.bin"), "Whisper Small")

    def test_sizes_are_rounded_the_way_people_say_them(self):
        from dikte import setup_ui
        self.assertEqual(setup_ui.rough_size(574_041_195), "550 MB")
        self.assertEqual(setup_ui.rough_size(950_950_000), "0.9 GB")
        self.assertEqual(setup_ui.rough_size(3_095_033_483), "2.9 GB")

    def test_the_memory_estimate_is_more_than_the_file(self):
        from dikte import setup_ui
        size = 574_000_000
        self.assertGreater(setup_ui.memory_need(size), size)

    def test_cloud_cleanup_is_said_and_can_be_turned_off(self):
        self.settings.cleanup_enabled.setChecked(True)
        self.settings.cleanup_provider.setCurrentIndex(
            self.settings.cleanup_provider.findData("openrouter"))
        self.settings._key_fields["openrouter"].setText("sk-or-test")
        dialog = self.dialog()
        self.assertIn("OpenRouter", dialog.privacy.text())
        self.assertFalse(dialog.cleanup_keep.isHidden())
        self.assertTrue(dialog.cleanup_keep.isChecked())
        self.assertEqual(dialog.cleanup_keep.text(), "Clean up with OpenRouter")
        dialog.cleanup_keep.setChecked(False)
        self.assertEqual(dialog.privacy.text(), "Nothing leaves this computer.")

    def test_nothing_leaves_when_cleanup_is_off(self):
        self.settings.cleanup_enabled.setChecked(False)
        dialog = self.dialog()
        self.assertEqual(dialog.privacy.text(), "Nothing leaves this computer.")
        self.assertTrue(dialog.cleanup_keep.isHidden())

    def test_the_expert_box_is_folded_away(self):
        dialog = self.dialog()
        self.assertFalse(dialog.advanced.expanded())
        self.assertTrue(dialog.box.isHidden() or not dialog.box.isVisibleTo(dialog))


class Indicator(DikteTest):
    def test_a_failed_cleanup_says_what_happened_to_the_words(self):
        from dikte import app
        self.assertEqual(app._warning_line("hi", "hi", "rate limited", True),
                         "Cleanup failed. Pasted the raw text.")
        self.assertEqual(app._warning_line("hi", "hi", "rate limited", False),
                         "Cleanup failed. Copied the raw text.")
        both = "rate limited\nCopied, but pasting failed: no focus"
        self.assertEqual(app._warning_line("hi", "hi", both, True),
                         "Cleanup failed. Copied the raw text.")
        self.assertIn("Completed with a warning",
                      app._warning_line("hi", "Hi.", "Copied, but pasting failed: x", True))

    def test_the_overlay_names_its_state_on_transitions(self):
        from dikte import overlay
        indicator = overlay.Overlay()
        self.addCleanup(indicator.deleteLater)
        indicator.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        indicator.show_recording()
        self.assertEqual(indicator.accessibleName(), "Recording")
        indicator.set_paused(True)
        self.assertEqual(indicator.accessibleName(), "Paused")
        indicator.show_error("No connection")
        self.assertEqual(indicator.accessibleName(), "Failed. No connection")
        indicator._anim.stop()
        indicator._hide_timer.stop()


class OwnerRound(HomeFixture):
    """No title that only repeats the tab, and History as a list that copies."""

    def test_no_tab_repeats_its_name_as_a_title_while_idle(self):
        self.window.resize(850, 860)
        for mode in ("dictation", "file", "meeting", "ask", "history"):
            self.window.show_mode(mode)
            QApplication.processEvents()
            page = self.window.mode_pages[mode]
            titles = [label.text() for label in page.findChildren(home_ui.QLabel)
                      if label.objectName() == "heading" and label.isVisible()]
            with self.subTest(mode=mode):
                self.assertEqual(titles, [])

    def test_a_state_keeps_its_title_and_the_button_stays_put(self):
        self.window.resize(850, 860)
        QApplication.processEvents()
        button = self.window.capture_button
        before = button.mapTo(self.window, button.rect().topLeft())
        self.controller.state = "recording"
        self.controller.recording = True
        self.window.refresh()
        QApplication.processEvents()
        self.assertTrue(self.window.capture_status.isVisible())
        self.assertIn("01:05", self.window.capture_status.text())
        self.assertEqual(button.mapTo(self.window, button.rect().topLeft()), before)
        self.controller.ask_state = "recording"
        self.window.show_mode("ask")
        self.window.refresh()
        QApplication.processEvents()
        self.assertTrue(self.window.ask_status.isVisible())
        self.assertIn("01:05", self.window.ask_status.text())

    def test_the_assistant_rows_breathe(self):
        from dikte import widgets
        self.window.resize(850, 860)
        self.window.show_mode("ask")
        QApplication.processEvents()
        ask, new = self.window.ask_action, self.window.ask_new
        self.assertGreaterEqual(self.window.ask_button.width(), widgets.PRIMARY_MIN)
        gap = new.mapTo(self.window, new.rect().topLeft()).x() - \
            ask.mapTo(self.window, ask.rect().topRight()).x()
        self.assertGreaterEqual(gap, widgets.BUTTON_GAP)
        chips = self.window.ask_scope
        under = ask.mapTo(self.window, ask.rect().topLeft()).y() - \
            chips.mapTo(self.window, chips.rect().bottomLeft()).y()
        self.assertGreaterEqual(under, 12)

    def rows(self):
        kept = cfg.RECORDINGS_DIR / "kept.wav"
        cfg.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(b"RIFF0000WAVE")
        cfg.append_history({"ts": "2026-11-08 09:00:00", "failed": True, "error": "Gone",
                            "text": "", "raw": ""})
        cfg.append_history({"ts": "2026-11-08 09:30:00", "failed": True, "error": "Timed out",
                            "audio": str(kept), "text": "", "raw": ""})
        cfg.append_history({"ts": "2026-11-08 10:00:00", "text": "Plain words"})
        self.window.refresh_results()
        self.window.show_mode("history")
        QApplication.processEvents()
        return self.settings.history

    def glyphs(self, history, number):
        delegate = history.itemDelegate()
        return [name for name, _tip, _call in
                delegate.actions_for(history.model().index(number, 0))]

    def test_each_row_offers_what_it_is_for(self):
        history = self.rows()
        self.assertEqual(self.glyphs(history, 0), ["copy", "more"])
        self.assertEqual(self.glyphs(history, 1), ["retry", "more"])
        self.assertEqual(self.glyphs(history, 2), ["mic", "more"])

    def test_copying_a_row_copies_it_and_says_so(self):
        from dikte import widgets
        history = self.rows()
        QApplication.clipboard().clear()
        delegate = history.itemDelegate()
        index = history.model().index(0, 0)
        delegate.actions_for(index)[0][2](0)
        self.assertEqual(QApplication.clipboard().text(), "Plain words")
        self.assertTrue(history.item(0).isSelected())
        self.assertEqual(delegate.flash(index), "Copied")
        self.assertEqual(self.glyphs(history, 0)[0], "check")
        self.settings._copied_timer.timeout.emit()
        self.assertEqual(delegate.flash(index), "")
        self.assertIsInstance(delegate, widgets.RowDelegate)

    def test_keys_and_a_double_click_copy_the_selection(self):
        from PyQt6.QtGui import QKeySequence
        from PyQt6.QtTest import QTest
        history = self.rows()
        history.setCurrentRow(0)
        history.setFocus()
        for press in (lambda: QTest.keySequence(history, QKeySequence.StandardKey.Copy),
                      lambda: QTest.keyClick(history, Qt.Key.Key_Return),
                      lambda: history.itemDoubleClicked.emit(history.item(0))):
            QApplication.clipboard().clear()
            press()
            QApplication.processEvents()
            self.assertEqual(QApplication.clipboard().text(), "Plain words")
        # A failure has nothing to copy.
        history.setCurrentRow(1)
        QApplication.clipboard().setText("kept")
        self.settings._copy_history()
        self.assertEqual(QApplication.clipboard().text(), "kept")

    def test_open_and_retry_stay_reachable(self):
        history = self.rows()
        history.setCurrentRow(0)
        with mock.patch.object(self.window, "_open_text") as opened:
            self.settings.history_open.trigger()
        opened.assert_called_once_with("Plain words")
        asked = []
        self.settings.retry_requested.connect(asked.append)
        history.itemDelegate().actions_for(history.model().index(1, 0))[0][2](1)
        self.assertEqual(asked[0]["error"], "Timed out")
        history.setCurrentRow(2)
        self.assertTrue(self.settings.history_record.isVisible())
        self.assertFalse(self.settings.history_copy.isVisible())

    def test_show_in_history_selects_the_row_in_the_list(self):
        history = self.rows()
        self.window.show_mode("dictation")
        self.settings.history_search.setText("nothing like it")
        self.window._show_failure()
        self.assertEqual(self.window.mode, "history")
        self.assertEqual(self.settings.history_search.text(), "")
        self.assertEqual(history.currentRow(), 1)
        self.assertEqual([item.text() for item in history.selectedItems()],
                         [history.item(1).text()])
