"""Native workspace navigation, capture boundaries and persisted results."""

import itertools
from types import SimpleNamespace
from unittest import mock

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton

from dikte import config as cfg, home_ui, i18n
from dikte.app import Dikte
from tests import test_ui
from tests.support import DikteTest


class Home(DikteTest):
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

    def test_daily_tasks_are_reachable_outside_configuration(self):
        for mode in ("dictation", "file", "meeting", "ask", "history"):
            self.window.show_mode(mode)
            QApplication.processEvents()
            self.assertEqual(self.window.pages.currentWidget(), self.window.mode_pages[mode])
        self.window.show_mode("meeting")
        self.assertTrue(self.settings.minutes_view.isVisible())
        self.window.show_mode("history")
        self.assertTrue(self.settings.history.isVisible())
        self.assertEqual(self.settings.tabs.count(), 8)
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
        self.window.resize(1900, 1000)
        for mode, area in self.window.mode_pages.items():
            self.window.show_mode(mode)
            QApplication.processEvents()
            with self.subTest(mode=mode):
                self.assertLessEqual(area.widget().width(), 680)
                self.assertAlmostEqual(area.widget().geometry().center().x(),
                                       area.viewport().rect().center().x(), delta=1)
        self.settings.resize(1900, 1000)
        self.settings.show()
        for index in range(self.settings.tabs.count()):
            self.settings.tabs.setCurrentIndex(index)
            QApplication.processEvents()
            area = self.settings.tabs.widget(index)
            with self.subTest(settings=index):
                self.assertAlmostEqual(area.widget().geometry().center().x(),
                                       area.viewport().rect().center().x(), delta=1)

    def test_meeting_and_assistant_actions_share_one_row(self):
        self.window.resize(620, 760)
        for mode, labels in (
            ("meeting", ("Copy", "Write it up", "Open the folder", "Delete selected", "Reload")),
            ("ask", (self.window.ask_button.text(), "Start a new conversation")),
        ):
            self.window.show_mode(mode)
            QApplication.processEvents()
            page = self.window.mode_pages[mode]
            buttons = {b.text(): b for b in page.findChildren(QPushButton)}
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
        self.assertIn("profile: Email", self.window.latest_time.text())
        self.assertIn("translated into German", self.window.latest_time.text())
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

    def test_every_history_button_fits_at_the_default_width(self):
        """Seven actions in English and in Turkish, in the column the window's
        default width leaves them: none may be clipped, at the default font or
        at a large one (Windows' UI font draws them nearly twice as wide)."""
        default = QApplication.font()
        self.addCleanup(QApplication.setFont, default)
        # A font set in pixels has no point size, and the large pass would
        # quietly run at the default one.
        self.assertGreater(default.pointSizeF(), 0)
        for scale in (1, 1.8):
            for language in ("en", "tr"):
                with self.subTest(scale=scale, language=language):
                    font = QFont(default)
                    font.setPointSizeF(default.pointSizeF() * scale)
                    QApplication.setFont(font)
                    i18n.set_language(language)
                    settings = self.fixture.window(self.conf)
                    window = home_ui.HomeWindow(self.controller, settings)
                    self.addCleanup(window.deleteLater)
                    self.addCleanup(window.close)
                    window.show()
                    window.show_mode("history")
                    QApplication.processEvents()
                    page = window.mode_pages["history"]
                    buttons = [b for b in page.findChildren(QPushButton)
                               if b.isVisible()]
                    self.assertGreaterEqual(len(buttons), 7)
                    for button in buttons:
                        self.assertGreaterEqual(button.width(),
                                                button.sizeHint().width(), button.text())
                        self.assertGreaterEqual(button.height(),
                                                button.sizeHint().height(), button.text())
                        # At the default font everything is in view; at a
                        # very large one a button wider than the column may
                        # scroll sideways, but it is never cut off.
                        if scale == 1:
                            corner = button.mapTo(page, button.rect().bottomRight())
                            right = page.viewport().width()
                        else:
                            corner = button.mapTo(page.widget(), button.rect().bottomRight())
                            right = page.widget().width()
                        self.assertLess(corner.x(), right, button.text())
                        # A row the layout was given no height for is drawn
                        # below its parent and cut off.
                        host = button.parentWidget()
                        bottom = button.mapTo(host, button.rect().bottomRight()).y()
                        self.assertLess(bottom, host.height(), button.text())
                    for a, b in itertools.combinations(buttons, 2):
                        self.assertFalse(a.geometry().intersects(b.geometry())
                                         and a.parentWidget() is b.parentWidget(),
                                         (a.text(), b.text()))
                    # Open selected text stays with the actions on the selection.
                    actions = settings.history_actions
                    self.assertEqual(
                        actions.itemAt(actions.indexOf(settings.history_original) + 1)
                        .widget().text(), i18n.t("Open selected text"))

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
        self.assertEqual(self.window.capture_status.text(), "Set up transcription")
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
        self.assertEqual(self.window.ask_output.toPlainText(), "Actual answer")
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
        with mock.patch.dict("os.environ", {}, clear=True):
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
        self.settings._select_data(self.settings.transcribe_provider, "groq")
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
        self.assertEqual(self.window.capture_status.text(), "Konuşmaya hazır")
        self.assertIn("Dikte:", self.window.capture_models.text())
        self.assertIn("Temizleme:", self.window.capture_models.text())

    def test_completed_run_refreshes_workspace_without_changing_controller_state(self):
        controller = Dikte.__new__(Dikte)
        controller.home_messages = {}
        controller.home_window = self.window
        controller._waiters = {}
        cfg.append_history({"text": "Finished"})
        controller._settle("dictation", {"ok": True, "text": "Finished"})
        self.assertEqual(self.window.latest_text.toPlainText(), "Finished")
        self.assertEqual(controller.home_messages["dictation"], "Transcript ready")
