"""The dictionary and profile editors, on Qt's offscreen platform, and what the
history shows about a row made under a profile."""

from unittest import mock

from PyQt6.QtWidgets import QApplication

from dikte import config as cfg
from dikte import dictionary
from dikte import frontapp
from dikte import profiles_ui
from dikte import settings_ui
from tests.support import DikteTest

_app = QApplication.instance() or QApplication([])


class Dictionary(DikteTest):
    def editor(self, conf):
        editor = profiles_ui.DictionaryEditor()
        self.addCleanup(editor.deleteLater)
        editor.load(conf)
        return editor

    def terms(self, editor):
        return [r["term"] for r in editor.entries()]

    def test_the_old_text_arrives_as_records(self):
        editor = self.editor(self.config(transcribe_prompt="Paraşüt, OpenFrame"))
        self.assertEqual(self.terms(editor), ["Paraşüt", "OpenFrame"])
        self.assertEqual(editor.table.rowCount(), 2)

    def test_add_edit_delete(self):
        editor = self.editor(self.config())
        self.assertTrue(editor.add_entry("kube control", "kubectl"))
        self.assertTrue(editor.add_entry("Dikte, OpenFrame"))
        self.assertFalse(editor.add_entry("dikte"))
        self.assertEqual(self.terms(editor), ["kube control", "Dikte", "OpenFrame"])
        self.assertTrue(editor.edit_entry(1, "Dikte", "DIKTE"))
        self.assertEqual(editor.entries()[1]["spelling"], "DIKTE")
        self.assertFalse(editor.edit_entry(1, "OpenFrame"))
        editor.delete_entries([0])
        self.assertEqual(self.terms(editor), ["Dikte", "OpenFrame"])

    def test_search_and_the_source_filter(self):
        editor = self.editor(self.config())
        editor.add_entry("Paraşüt")
        editor.add_entry("Claude Code", source=dictionary.AUTO)
        editor.filter.setCurrentIndex(editor.filter.findData(dictionary.AUTO))
        self.assertEqual(editor.visible(), [1])
        editor.filter.setCurrentIndex(editor.filter.findData(dictionary.MANUAL))
        self.assertEqual(editor.visible(), [0])
        editor.filter.setCurrentIndex(0)
        editor.search.setText("claude")
        self.assertEqual(editor.visible(), [1])

    def test_turkish_dotted_capitals_count_as_the_same_word(self):
        editor = self.editor(self.config())
        editor.add_entry("İstanbul")
        self.assertFalse(editor.add_entry("istanbul"))

    def test_the_second_of_two_equal_terms_can_be_edited(self):
        editor = self.editor(self.config(transcribe_prompt="Dikte, dikte"))
        self.assertTrue(editor.edit_entry(1, "dikte", "DIKTE"))
        self.assertEqual(editor.entries()[1]["spelling"], "DIKTE")

    def test_an_auto_entry_corrected_by_hand_is_manual(self):
        editor = self.editor(self.config())
        editor.add_entry("Claud", source=dictionary.AUTO)
        editor.edit_entry(0, "Claude")
        self.assertEqual(editor.entries()[0]["source"], dictionary.MANUAL)

    def test_clear_auto_learned(self):
        editor = self.editor(self.config())
        editor.add_entry("Paraşüt")
        editor.add_entry("Claude Code", source=dictionary.AUTO)
        editor.clear_auto()
        self.assertEqual(self.terms(editor), ["Paraşüt"])
        self.assertFalse(editor.clear_auto_button.isEnabled())

    def test_an_untouched_store_leaves_the_text_alone(self):
        conf = self.config(transcribe_prompt="Paraşüt\nOpenFrame")
        conf["dictionary_learning"] = True
        editor = self.editor(conf)
        self.assertEqual(editor.store(conf), (None, False))
        self.assertEqual(conf["transcribe_prompt"], "Paraşüt\nOpenFrame")

    def test_deleting_a_word_does_not_join_two_lines(self):
        editor = self.editor(self.config(transcribe_prompt="A\nB, C"))
        editor.delete_entries([1])
        self.assertEqual(dictionary.render_hint(editor.entries()), "A\nC")

    def test_learning_off_or_cleared_forgets_the_names_being_counted(self):
        conf = self.config(dictionary_learning=True)
        dictionary.learn(conf, "It runs on OpenFrame now.", "It runs on OpenFrame now.")
        editor = self.editor(conf)
        editor.learning.setChecked(False)
        editor.commit(editor.store(conf))
        self.assertEqual(dictionary._read_file()["pending"], {})
        dictionary.learn(conf, "It runs on OpenFrame now.", "It runs on OpenFrame now.")
        conf["dictionary_learning"] = True
        editor = self.editor(conf)
        editor.add_entry("Claude", source=dictionary.AUTO)
        editor.clear_auto()
        editor.commit(editor.store(conf))
        self.assertEqual(dictionary._read_file()["pending"], {})


class Profiles(DikteTest):
    def editor(self, conf):
        editor = profiles_ui.ProfilesEditor()
        self.addCleanup(editor.deleteLater)
        editor.load(conf)
        return editor

    def test_a_named_profile_with_programs_style_and_a_language(self):
        conf = self.config()
        editor = self.editor(conf)
        self.assertTrue(editor.add_profile("Email"))
        self.assertFalse(editor.add_profile("email"))
        self.assertFalse(editor.add_profile("Default"))
        editor.apps.setText("Thunderbird, outlook.exe")
        editor.apps.textEdited.emit(editor.apps.text())
        box = editor.style_boxes["formality"]
        box.setCurrentIndex(box.findData("formal"))
        editor.target.setCurrentIndex(editor.target.findData("de"))
        editor.override.setCurrentIndex(editor.override.findData("Email"))
        editor.store(conf)
        stored = conf["profiles"][0]
        self.assertEqual(stored["name"], "Email")
        self.assertEqual(stored["apps"], ["thunderbird", "outlook"])
        self.assertEqual(stored["style"], {"formality": "formal"})
        self.assertEqual(stored["translate_to"], "de")
        self.assertEqual(conf["profile_override"], "Email")
        self.assertEqual(conf["profile_default"], {})

    def test_the_default_profile_lists_no_programs(self):
        editor = self.editor(self.config())
        editor.select(0)
        self.assertFalse(editor.apps.isEnabled())
        self.assertFalse(editor.remove_button.isEnabled())

    def test_rename_keeps_the_override_pointing_at_it(self):
        conf = self.config(profiles=[{"name": "Mail"}], profile_override="Mail")
        editor = self.editor(conf)
        editor.select(1)
        self.assertTrue(editor.rename_current("Email"))
        editor.store(conf)
        self.assertEqual(conf["profile_override"], "Email")

    def test_removing_the_overridden_profile_falls_back_to_automatic(self):
        conf = self.config(profiles=[{"name": "Mail"}], profile_override="Mail")
        editor = self.editor(conf)
        editor.select(1)
        editor.remove_current()
        editor.store(conf)
        self.assertEqual((conf["profiles"], conf["profile_override"]), ([], ""))

    def test_examples_are_read_then_forgotten(self):
        conf = self.config(style_learning=True)
        editor = self.editor(conf)
        editor.examples.setPlainText(
            "Hey, thanks for the fix. I'm gonna test it now. Looks good to me. "
            "Ping me later!")
        found = editor.learn_from_examples()
        self.assertEqual(found["formality"], "casual")
        self.assertEqual(editor.examples.toPlainText(), "")
        self.assertIn("Casual", editor.learned.text())
        editor.store(conf)
        self.assertEqual(conf["profile_default"]["learned"]["formality"], "casual")
        # An explicit choice is kept beside it, and wins in the prompt.
        editor.reset_learned_style()
        editor.store(conf)
        self.assertEqual(conf["profile_default"]["learned"], {})

    def test_too_little_to_learn_from_is_left_to_add_to(self):
        editor = self.editor(self.config(style_learning=True))
        editor.examples.setPlainText("Ok.")
        self.assertEqual(editor.learn_from_examples(), {})
        self.assertEqual(editor.examples.toPlainText(), "Ok.")

    def test_the_override_is_found_whatever_its_case(self):
        """profiles.override() matches without regard to case, so the box
        must show the same profile a dictation would use."""
        conf = self.config(profiles=[{"name": "Chat"}], profile_override="chat")
        editor = self.editor(conf)
        self.assertEqual(editor.override.currentData(), "Chat")

    def test_switches_round_trip(self):
        conf = self.config(profile_style=False, profile_by_app=False,
                           translation_enabled=False, style_learning=True)
        editor = self.editor(conf)
        self.assertFalse(editor.style_boxes["formality"].isEnabled())
        self.assertFalse(editor.target.isEnabled())
        self.assertTrue(editor.teach.isEnabled())
        conf2 = self.config()
        editor.store(conf2)
        self.assertEqual((conf2["profile_style"], conf2["profile_by_app"],
                          conf2["translation_enabled"], conf2["style_learning"]),
                         (False, False, False, True))

    def test_the_detection_note_names_what_this_desktop_can_do(self):
        with mock.patch.object(frontapp, "backend", return_value=frontapp._unknown):
            self.assertIn("not available on this desktop",
                          profiles_ui.ProfilesEditor.detection_text())
        with mock.patch.object(frontapp, "backend",
                               return_value=frontapp._kde_wayland), \
                mock.patch.object(frontapp.shutil, "which", return_value=None):
            self.assertEqual(
                profiles_ui.ProfilesEditor.detection_text(),
                "On KDE Plasma under Wayland, per-application profiles need "
                "kdotool installed.")
        with mock.patch.object(frontapp, "backend",
                               return_value=frontapp._kde_wayland), \
                mock.patch.object(frontapp.shutil, "which",
                                  return_value="/usr/bin/kdotool"):
            self.assertNotIn("kdotool", profiles_ui.ProfilesEditor.detection_text())

    def test_x11_without_xprop_says_so(self):
        with mock.patch.object(frontapp, "backend", return_value=frontapp._x11), \
                mock.patch.object(frontapp.shutil, "which", return_value=None):
            self.assertEqual(profiles_ui.ProfilesEditor.detection_text(),
                             "On X11, per-application profiles need xprop installed.")
        with mock.patch.object(frontapp, "backend", return_value=frontapp._x11), \
                mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"):
            self.assertEqual(profiles_ui.ProfilesEditor.detection_text(), "")

    def test_the_kdotool_note_is_translated(self):
        from dikte import i18n
        i18n.set_language("tr")
        with mock.patch.object(frontapp, "backend",
                               return_value=frontapp._kde_wayland), \
                mock.patch.object(frontapp.shutil, "which", return_value=None):
            self.assertIn("kdotool kurulu olmalı",
                          profiles_ui.ProfilesEditor.detection_text())

    def test_find_the_program_name(self):
        editor = self.editor(self.config())
        with mock.patch.object(profiles_ui.threading, "Thread") as thread, \
                mock.patch.object(frontapp, "program_name", return_value="slack"):
            editor._show_found()
            # Off the main thread: the asking is handed to a thread.
            thread.return_value.start.assert_called_once_with()
            thread.call_args.kwargs["target"]()
        QApplication.processEvents()
        self.assertEqual(editor.found.text(), "In front: slack")

    def test_a_late_answer_to_an_older_question_is_dropped(self):
        editor = self.editor(self.config())
        with mock.patch.object(profiles_ui.threading, "Thread"):
            editor._show_found()
            first = editor._asked
            editor._show_found()
        self.assertNotEqual(first, editor._asked)
        editor._found(first, "firefox")
        self.assertEqual(editor.found.text(), "")
        other = self.editor(self.config())
        with mock.patch.object(profiles_ui.threading, "Thread"):
            other._show_found()
        # Numbers are never shared, whichever editor asked.
        self.assertNotEqual(other._asked, editor._asked)
        editor._found(editor._asked, "slack")
        self.assertEqual(editor.found.text(), "In front: slack")

    def test_the_answer_goes_through_a_relay_that_outlives_the_editor(self):
        editor = self.editor(self.config())
        relay = profiles_ui._relay()
        with mock.patch.object(profiles_ui.threading, "Thread") as thread, \
                mock.patch.object(frontapp, "program_name", return_value="slack"):
            editor._show_found()
            work = thread.call_args.kwargs["target"]
        editor.deleteLater()
        QApplication.processEvents()
        with mock.patch.object(frontapp, "program_name", return_value="slack"):
            work()  # the editor is gone; the relay is not
        QApplication.processEvents()
        self.assertIs(profiles_ui._relay(), relay)


class HistoryMarks(DikteTest):
    def test_a_row_without_profiles_says_nothing_more(self):
        self.assertEqual(settings_ui.history_marks({"text": "x"}), "")

    def test_profile_and_translation_are_named(self):
        marks = settings_ui.history_marks({"profile": "", "translate_to": "de",
                                           "translated": True})
        self.assertIn("profile: Default", marks)
        self.assertIn("translated into German", marks)
        self.assertNotIn("program", marks)
        self.assertIn("(program unknown)",
                      settings_ui.history_marks({"profile": "", "program": ""}))
        self.assertIn("(slack)",
                      settings_ui.history_marks({"profile": "", "program": "slack"}))
        self.assertEqual(settings_ui.history_marks({"translate_to": ["de"]}), "")
        failed = settings_ui.history_marks({"profile": "Email", "translate_to": "de",
                                            "translated": False})
        self.assertIn("profile: Email", failed)
        self.assertIn("failed", failed)


class Owned(DikteTest):
    def test_boxes_inside_an_editor_are_its_own(self):
        editor = profiles_ui.DictionaryEditor()
        self.addCleanup(editor.deleteLater)
        self.assertTrue(profiles_ui.owned(editor.search))
        other = profiles_ui.QLineEdit()
        self.addCleanup(other.deleteLater)
        self.assertFalse(profiles_ui.owned(other))

    def test_nothing_is_written_when_nothing_was_stored(self):
        conf = self.config(profile_default={"style": {"formality": "formal"},
                                            "junk": 1})
        editor = profiles_ui.ProfilesEditor()
        self.addCleanup(editor.deleteLater)
        editor.load(conf)
        editor.store(conf)
        self.assertEqual(conf["profile_default"],
                         {"style": {"formality": "formal"}, "junk": 1})
        self.assertFalse(cfg.CONFIG_FILE.exists())


class CorrectionEntries(DikteTest):
    def test_an_entry_from_a_correction_says_what_it_was_heard_as(self):
        editor = profiles_ui.DictionaryEditor()
        self.addCleanup(editor.deleteLater)
        editor.load(self.config())
        editor._entries.append(dictionary.entry("klod kod", "Claude Code", dictionary.AUTO))
        editor._show()
        self.assertEqual(editor.table.item(0, 2).text(), "Auto, heard as: klod kod")
        editor.edit_entry(0, "klod kod", "Claude-Code")
        self.assertEqual(editor.entries()[0]["spelling"], "Claude-Code")
        editor.delete_entries([0])
        self.assertEqual(editor.entries(), [])


class NoWallsOfText(DikteTest):
    """At most one sentence of text on screen per control; the rest belongs
    in tooltips."""

    def sentences(self, text):
        import re
        return len(re.findall(r"[.!?](?:\s|$)", text.strip()))

    def test_every_label_in_the_editors_is_one_sentence(self):
        from PyQt6.QtWidgets import QCheckBox, QLabel
        from dikte import i18n
        for language in ("en", "tr"):
            i18n.set_language(language)
            for editor in (profiles_ui.DictionaryEditor(), profiles_ui.ProfilesEditor()):
                self.addCleanup(editor.deleteLater)
                editor.load(self.config())
                for widget in editor.findChildren((QLabel, QCheckBox)):
                    with self.subTest(language=language, text=widget.text()[:40]):
                        self.assertLessEqual(self.sentences(widget.text()), 1)

    def test_the_long_explanations_are_tooltips(self):
        editor = profiles_ui.DictionaryEditor()
        self.addCleanup(editor.deleteLater)
        self.assertIn("three dictations unchanged", editor.learning.toolTip())
        # Pin a backend that reads titles: on macOS the sites tooltip says
        # site matching is unavailable instead.
        with mock.patch.object(frontapp, "backend", return_value=frontapp._unknown):
            profile_editor = profiles_ui.ProfilesEditor()
        self.addCleanup(profile_editor.deleteLater)
        self.assertIn("looked up when a recording starts", profile_editor.by_app.toolTip())
        self.assertIn("not kept", profile_editor.style_learning.toolTip())
        self.assertIn("Titles are never saved", profile_editor.sites.toolTip())

    def test_macos_explains_why_sites_do_not_match(self):
        with mock.patch.object(frontapp, "backend", return_value=frontapp._macos):
            self.assertIn("not available on macOS", profiles_ui.ProfilesEditor.sites_tooltip())
            self.assertIn("not available on macOS", profiles_ui.ProfilesEditor.sites_text())
