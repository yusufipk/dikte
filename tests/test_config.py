"""Settings, the history file and the meeting index.

Every one of these lives on disk and outlives an update, so the tests care most
about what happens to a file written by an older version: an unknown key, a
setting stored under its Turkish name, a prompt that used to be copied into the
config and now shadows the default.
"""

import json
import os
import unittest
from unittest import mock

import api
import cleanup
import config as cfg
import ggml
import i18n
import paste
from tests.support import DikteTest


class Loading(DikteTest):
    def test_nothing_stored_yet(self):
        conf = cfg.Config()
        self.assertEqual(conf["cleanup_model"], cfg.DEFAULTS["cleanup_model"])

    def test_a_stored_value_wins(self):
        self.write_config({"cleanup_model": "some/other-model"})
        self.assertEqual(cfg.Config()["cleanup_model"], "some/other-model")

    def test_a_key_this_version_does_not_have_is_dropped(self):
        """A setting from a fork, or from a version that removed it."""
        self.write_config({"cleanup_model": "kept", "invented_by_a_fork": True})
        conf = cfg.Config()
        self.assertEqual(conf["cleanup_model"], "kept")
        self.assertNotIn("invented_by_a_fork", conf.data)

    def test_a_config_that_is_not_json_falls_back_to_the_defaults(self):
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg.CONFIG_FILE.write_text("{not json", encoding="utf-8")
        with mock.patch("builtins.print"):
            conf = cfg.Config()
        self.assertEqual(conf["cleanup_model"], cfg.DEFAULTS["cleanup_model"])

    def test_a_config_that_is_json_but_not_an_object(self):
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg.CONFIG_FILE.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(cfg.Config()["cleanup_model"], cfg.DEFAULTS["cleanup_model"])

    def test_a_corner_stored_under_its_old_turkish_name(self):
        self.write_config({"overlay_corner": "sağ-üst"})
        self.assertEqual(cfg.Config()["overlay_corner"], "top-right")

    def test_a_corner_that_needs_no_migrating(self):
        self.write_config({"overlay_corner": "bottom-right"})
        self.assertEqual(cfg.Config()["overlay_corner"], "bottom-right")

    def test_a_default_prompt_an_old_version_copied_in_is_dropped(self):
        """Otherwise it shadows every later improvement to that default."""
        old = "the 1.2 default prompt, whatever it said"
        with mock.patch.object(cfg, "LEGACY_PROMPTS", {cfg._fingerprint(old)}):
            self.write_config({"cleanup_prompt": old})
            self.assertEqual(cfg.Config()["cleanup_prompt"], "")

    def test_a_prompt_the_user_wrote_is_left_alone(self):
        self.write_config({"cleanup_prompt": "Only fix the punctuation."})
        self.assertEqual(cfg.Config()["cleanup_prompt"], "Only fix the punctuation.")

    def test_loading_sets_the_interface_language(self):
        self.write_config({"ui_language": "tr"})
        cfg.Config()
        self.assertEqual(i18n.language(), "tr")

    def test_an_unknown_key_reads_as_its_default(self):
        self.assertIsNone(cfg.Config()["no_such_setting"])
        self.assertEqual(cfg.Config().get("no_such_setting", "fallback"), "fallback")


class Saving(DikteTest):
    def test_a_saved_setting_comes_back(self):
        conf = cfg.Config()
        conf["cleanup_model"] = "some/model"
        conf.save()
        self.assertEqual(cfg.Config()["cleanup_model"], "some/model")

    def test_the_directory_is_created(self):
        self.assertFalse(cfg.CONFIG_DIR.exists())
        cfg.Config().save()
        self.assertTrue(cfg.CONFIG_FILE.exists())

    def test_the_file_is_readable_by_nobody_else(self):
        """It holds two API keys."""
        cfg.Config().save()
        self.assertEqual(cfg.CONFIG_FILE.stat().st_mode & 0o777, 0o600)

    def test_nothing_is_left_behind_half_written(self):
        cfg.Config().save()
        self.assertEqual([p.name for p in cfg.CONFIG_DIR.iterdir()], ["config.json"])

    def test_turkish_is_stored_as_turkish(self):
        conf = cfg.Config()
        conf["transcribe_prompt"] = "Paraşüt, öğle"
        conf.save()
        self.assertIn("Paraşüt", cfg.CONFIG_FILE.read_text(encoding="utf-8"))

    def test_saving_applies_the_interface_language(self):
        conf = cfg.Config()
        conf["ui_language"] = "tr"
        conf.save()
        self.assertEqual(i18n.language(), "tr")


class Keys(DikteTest):
    def test_a_stored_key_is_used(self):
        conf = self.config(openai_api_key="  sk-stored  ")
        self.assertEqual(conf.openai_key(), "sk-stored")

    def test_the_environment_is_the_fallback(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env"}):
            self.assertEqual(cfg.Config().openai_key(), "sk-env")

    def test_a_stored_key_beats_the_environment(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-env"}):
            conf = self.config(openrouter_api_key="sk-stored")
            self.assertEqual(conf.openrouter_key(), "sk-stored")

    def test_no_key_anywhere(self):
        self.assertEqual(cfg.Config().openai_key(), "")

    def test_every_provider_falls_back_to_the_variable_of_its_own_name(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk-env"}):
            self.assertEqual(cfg.Config().groq_key(), "gsk-env")


class TranscribeTarget(DikteTest):
    def test_this_machine_by_default(self):
        target = cfg.Config().transcribe_target()
        self.assertEqual(target.provider, "local")
        self.assertEqual(target.api_key, "")
        # Empty on purpose: the server picks a port when it starts, and reading
        # a setting must not be what starts it.
        self.assertEqual(target.base_url, "")

    def test_openai_when_it_is_picked(self):
        target = self.config(transcribe_provider="openai",
                             openai_api_key="sk-test").transcribe_target()
        self.assertEqual(target.provider, "openai")
        self.assertEqual(target.service, "OpenAI")
        self.assertEqual(target.api_key, "sk-test")
        self.assertEqual(target.base_url, api.OPENAI_URL)
        self.assertEqual(target.model, cfg.DEFAULTS["transcribe_model"])

    def test_openrouter_when_it_is_picked(self):
        conf = self.config(transcribe_provider="openrouter",
                           openrouter_api_key="sk-or-test",
                           openrouter_transcribe_model="openai/whisper-1")
        target = conf.transcribe_target()
        self.assertEqual(target.provider, "openrouter")
        self.assertEqual(target.service, "OpenRouter")
        self.assertEqual(target.api_key, "sk-or-test")
        self.assertEqual(target.model, "openai/whisper-1")

    def test_groq_when_it_is_picked(self):
        conf = self.config(transcribe_provider="groq", groq_api_key="gsk-test",
                           groq_transcribe_model="whisper-large-v3")
        target = conf.transcribe_target()
        self.assertEqual(target.provider, "groq")
        self.assertEqual(target.service, "Groq")
        self.assertEqual(target.api_key, "gsk-test")
        self.assertEqual(target.base_url, api.GROQ_URL)
        self.assertEqual(target.model, "whisper-large-v3")

    def test_a_provider_this_version_has_never_heard_of(self):
        """A config written by a fork, or by a version that dropped one."""
        target = self.config(transcribe_provider="deepgram").transcribe_target()
        self.assertEqual(target.provider, "openai")

    def test_a_self_hosted_endpoint(self):
        conf = self.config(transcribe_provider="openai",
                           openai_base_url="http://localhost:8080/v1")
        self.assertEqual(conf.transcribe_target().base_url, "http://localhost:8080/v1")


class CleanupPrompt(DikteTest):
    def test_the_default_follows_the_interface_language(self):
        self.assertEqual(cfg.Config().cleanup_prompt(), cfg.CLEANUP_PROMPT_EN)
        # Building a Config applies the stored language, so it is set there
        # rather than around it.
        self.write_config({"ui_language": "tr"})
        self.assertEqual(cfg.Config().cleanup_prompt(), cfg.CLEANUP_PROMPT_TR)

    def test_a_prompt_of_your_own(self):
        conf = self.config(cleanup_prompt="  Only fix punctuation.  ")
        self.assertEqual(conf.cleanup_prompt(), "Only fix punctuation.")

    def test_the_glossary_is_appended(self):
        conf = self.config(transcribe_prompt="Paraşüt, OpenFrame")
        self.assertIn("Paraşüt, OpenFrame", conf.cleanup_prompt())

    def test_no_glossary_means_no_rule_about_one(self):
        self.assertEqual(cfg.Config().cleanup_prompt(), cfg.CLEANUP_PROMPT_EN)

    def test_subtitles_use_their_own_prompt(self):
        conf = cfg.Config()
        self.assertNotEqual(conf.cleanup_prompt(subtitles=True), conf.cleanup_prompt())
        self.assertEqual(conf.cleanup_prompt(subtitles=True),
                         cfg.FILE_CLEANUP_PROMPT_EN)

    def test_a_subtitle_prompt_of_your_own(self):
        conf = self.config(file_cleanup_prompt="Keep the stamps.")
        self.assertEqual(conf.cleanup_prompt(subtitles=True), "Keep the stamps.")
        self.assertEqual(conf.cleanup_prompt(), cfg.CLEANUP_PROMPT_EN)

    def test_timestamps_add_a_rule_about_them(self):
        conf = cfg.Config()
        self.assertGreater(len(conf.cleanup_prompt(with_timestamps=True)),
                           len(conf.cleanup_prompt()))

    def test_speakers_bring_the_names_in_with_them(self):
        conf = self.config(meeting_self_name="Yusuf", meeting_other_name="Ayşe")
        prompt = conf.cleanup_prompt(with_speakers=True)
        self.assertIn("Yusuf", prompt)
        self.assertIn("Ayşe", prompt)


class Participants(DikteTest):
    def test_nobody_named(self):
        self.assertEqual(cfg.Config().participants(), "")

    def test_the_two_sides_come_first(self):
        conf = self.config(meeting_self_name="Yusuf", meeting_other_name="Ayşe",
                           meeting_participants="Mehmet")
        self.assertEqual(conf.participants(), "Yusuf\nAyşe\nMehmet")

    def test_commas_and_newlines_both_separate(self):
        conf = self.config(meeting_participants="Ayşe, Mehmet\nZeynep")
        self.assertEqual(conf.participants().splitlines(),
                         ["Ayşe", "Mehmet", "Zeynep"])

    def test_a_name_listed_twice_appears_once(self):
        conf = self.config(meeting_self_name="Yusuf",
                           meeting_participants="yusuf, Ayşe")
        self.assertEqual(conf.participants(), "Yusuf\nAyşe")

    def test_blank_entries_are_dropped(self):
        conf = self.config(meeting_participants="Ayşe,,  ,\nMehmet")
        self.assertEqual(conf.participants(), "Ayşe\nMehmet")


class MeetingSettings(DikteTest):
    def test_the_hint_carries_the_glossary_and_the_names(self):
        conf = self.config(transcribe_prompt="OpenFrame", meeting_self_name="Yusuf")
        self.assertEqual(conf.meeting_hint(), "OpenFrame\nYusuf")

    def test_the_hint_with_neither(self):
        self.assertEqual(cfg.Config().meeting_hint(), "")

    def test_the_speaker_labels_fall_back_to_the_language(self):
        self.assertEqual(cfg.Config().speaker_names(), ("Me", "Other side"))
        self.write_config({"ui_language": "tr"})
        self.assertEqual(cfg.Config().speaker_names(), ("Ben", "Karşı taraf"))

    def test_named_speakers_are_used_as_given(self):
        conf = self.config(meeting_self_name="Yusuf", meeting_other_name="Ayşe")
        self.assertEqual(conf.speaker_names(), ("Yusuf", "Ayşe"))

    def test_the_meeting_prompt_lists_who_was_there(self):
        conf = self.config(meeting_self_name="Yusuf", meeting_other_name="Ayşe")
        self.assertIn("Yusuf", conf.meeting_prompt())

    def test_the_meeting_prompt_with_nobody_named(self):
        self.assertEqual(cfg.Config().meeting_prompt(), cfg.MEETING_PROMPT_EN)


class History(DikteTest):
    def entry(self, text):
        return {"ts": "2026-08-01 10:00:00", "text": text, "raw": text}

    def test_nothing_written_yet(self):
        self.assertEqual(cfg.read_history(), [])

    def test_what_goes_in_comes_out_newest_last(self):
        for text in ("first", "second"):
            cfg.append_history(self.entry(text))
        self.assertEqual([row["text"] for row in cfg.read_history()],
                         ["first", "second"])

    def test_a_limit_reads_the_tail(self):
        for index in range(5):
            cfg.append_history(self.entry(str(index)))
        self.assertEqual([row["text"] for row in cfg.read_history(2)], ["3", "4"])

    def test_a_limit_of_zero_reads_everything(self):
        for index in range(3):
            cfg.append_history(self.entry(str(index)))
        self.assertEqual(len(cfg.read_history(0)), 3)

    def test_a_line_that_is_not_json_is_skipped_rather_than_fatal(self):
        cfg.append_history(self.entry("good"))
        with open(cfg.HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write("half a line, no newline at the end of the world\n")
        cfg.append_history(self.entry("also good"))
        self.assertEqual([row["text"] for row in cfg.read_history()],
                         ["good", "also good"])

    def test_turkish_survives_the_round_trip(self):
        cfg.append_history(self.entry("Öğleden sonra görüşürüz."))
        self.assertEqual(cfg.read_history()[0]["text"], "Öğleden sonra görüşürüz.")

    def test_trimming_keeps_the_newest(self):
        for index in range(10):
            cfg.append_history(self.entry(str(index)))
        cfg.trim_history(3)
        self.assertEqual([row["text"] for row in cfg.read_history()],
                         ["7", "8", "9"])

    def test_a_limit_of_zero_keeps_everything(self):
        for index in range(4):
            cfg.append_history(self.entry(str(index)))
        cfg.trim_history(0)
        self.assertEqual(len(cfg.read_history()), 4)

    def test_trimming_a_file_that_is_already_short_enough(self):
        cfg.append_history(self.entry("only one"))
        cfg.trim_history(200)
        self.assertEqual(len(cfg.read_history()), 1)

    def test_trimming_before_anything_was_written(self):
        cfg.trim_history(10)   # must not raise

    def test_deleting_matches_on_content_not_on_position(self):
        """The worker may have appended a row since the list was read."""
        rows = [self.entry("a"), self.entry("b"), self.entry("c")]
        for row in rows:
            cfg.append_history(row)
        cfg.delete_history([rows[1]])
        self.assertEqual([row["text"] for row in cfg.read_history()], ["a", "c"])

    def test_deleting_is_insensitive_to_key_order(self):
        cfg.append_history({"ts": "now", "text": "hello"})
        cfg.delete_history([{"text": "hello", "ts": "now"}])
        self.assertEqual(cfg.read_history(), [])

    def test_deleting_nothing_touches_nothing(self):
        cfg.append_history(self.entry("a"))
        cfg.delete_history([])
        self.assertEqual(len(cfg.read_history()), 1)

    def test_clearing(self):
        cfg.append_history(self.entry("a"))
        cfg.clear_history()
        self.assertEqual(cfg.read_history(), [])

    def test_clearing_a_history_that_is_not_there(self):
        cfg.clear_history()   # must not raise


class Meetings(DikteTest):
    def entry(self, base, **changes):
        row = {"base": base, "ts": "2026-08-01 10:00", "title": "",
               "duration": 60.0, "status": "recorded", "error": "", "model": ""}
        row.update(changes)
        return row

    def test_nothing_recorded_yet(self):
        self.assertEqual(cfg.read_meetings(), [])

    def test_the_document_and_the_recording_share_a_stem(self):
        doc, wav = cfg.meeting_paths("20260801-100000")
        self.assertEqual(doc.name, "20260801-100000.md")
        self.assertEqual(wav.name, "20260801-100000.wav")
        self.assertEqual(doc.parent, cfg.MEETINGS_DIR)

    def test_saving_and_reading_back(self):
        cfg.save_meeting(self.entry("a"))
        cfg.save_meeting(self.entry("b"))
        self.assertEqual([row["base"] for row in cfg.read_meetings()], ["a", "b"])

    def test_saving_the_same_base_replaces_rather_than_appends(self):
        cfg.save_meeting(self.entry("a"))
        cfg.save_meeting(self.entry("a", status="done"))
        rows = cfg.read_meetings()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "done")

    def test_a_row_with_no_base_is_ignored(self):
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg.MEETINGS_FILE.write_text(
            json.dumps({"title": "orphan"}) + "\n" + json.dumps(self.entry("a")) + "\n",
            encoding="utf-8")
        self.assertEqual([row["base"] for row in cfg.read_meetings()], ["a"])

    def test_a_broken_line_is_skipped(self):
        cfg.save_meeting(self.entry("a"))
        with open(cfg.MEETINGS_FILE, "a", encoding="utf-8") as fh:
            fh.write("{oh dear\n")
        self.assertEqual(len(cfg.read_meetings()), 1)

    def test_updating_patches_and_hands_the_row_back(self):
        cfg.save_meeting(self.entry("a"))
        row = cfg.update_meeting("a", status="done", title="Kickoff")
        self.assertEqual(row["status"], "done")
        self.assertEqual(cfg.read_meetings()[0]["title"], "Kickoff")

    def test_updating_one_that_is_gone(self):
        self.assertIsNone(cfg.update_meeting("nope", status="done"))

    def test_deleting_takes_the_files_with_it(self):
        cfg.save_meeting(self.entry("a"))
        doc, wav = cfg.meeting_paths("a")
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# minutes", encoding="utf-8")
        wav.write_bytes(b"RIFF")
        cfg.delete_meetings(["a"])
        self.assertEqual(cfg.read_meetings(), [])
        self.assertFalse(doc.exists())
        self.assertFalse(wav.exists())

    def test_deleting_a_row_whose_files_are_already_gone(self):
        cfg.save_meeting(self.entry("a"))
        cfg.delete_meetings(["a"])   # must not raise
        self.assertEqual(cfg.read_meetings(), [])

    def test_deleting_nothing(self):
        cfg.save_meeting(self.entry("a"))
        cfg.delete_meetings([])
        self.assertEqual(len(cfg.read_meetings()), 1)


class Defaults(unittest.TestCase):
    """The table itself, which every command line and settings tab reads."""

    def test_no_setting_defaults_to_none(self):
        """cli._coerce switches on the type of the default, so there has to be one."""
        for key, value in cfg.DEFAULTS.items():
            with self.subTest(key=key):
                self.assertIsNotNone(value)

    def test_the_prompts_ship_empty_so_the_default_can_improve(self):
        for key in ("cleanup_prompt", "file_cleanup_prompt", "meeting_prompt",
                    "assistant_prompt"):
            with self.subTest(key=key):
                self.assertEqual(cfg.DEFAULTS[key], "")

    def test_the_keys_ship_empty(self):
        self.assertEqual(cfg.DEFAULTS["openai_api_key"], "")
        self.assertEqual(cfg.DEFAULTS["openrouter_api_key"], "")

    def test_every_language_specific_prompt_has_both_languages(self):
        for name in ("CLEANUP_PROMPT", "FILE_CLEANUP_PROMPT", "MEETING_PROMPT",
                     "ASSISTANT_PROMPT"):
            for suffix in ("EN", "TR"):
                with self.subTest(prompt=f"{name}_{suffix}"):
                    self.assertTrue(getattr(cfg, f"{name}_{suffix}").strip())

    def test_the_paste_key_is_the_one_this_desktop_pastes_with(self):
        """cmd+v on a Mac, and it must be one paste.py can actually press."""
        self.assertEqual(cfg.DEFAULTS["paste_shortcut"],
                         paste.desktop().shortcuts[0])


if __name__ == "__main__":
    unittest.main()


class LocalCleanup(DikteTest):
    def test_the_local_model_is_what_the_history_records(self):
        conf = self.config(cleanup_provider="local",
                           local_llm_model="gemma-3-4b-it-Q4_K_M.gguf")
        self.assertEqual(cleanup.provider(conf), "local")
        self.assertEqual(cleanup.model(conf), "gemma-3-4b-it-Q4_K_M.gguf")

    def test_it_needs_no_program_on_the_path(self):
        # whisper.cpp and llama.cpp are fetched rather than installed, so unlike
        # Claude Code and Codex there is no executable to look for.
        self.assertEqual(cleanup.executable("local"), "")

    def test_the_minutes_do_not_follow_the_cleanup_provider(self):
        # A 4B model here will strip the filler words out of a dictation and
        # will not write up an hour long meeting.
        conf = self.config(cleanup_provider="local")
        self.assertEqual(conf["meeting_model"], cfg.DEFAULTS["meeting_model"])

    def test_only_the_cleanup_setting_asks_for_the_local_model(self):
        self.assertFalse(cfg.Config().uses_local_llm())
        self.assertTrue(self.config(cleanup_provider="local").uses_local_llm())


class ReadyToRun(DikteTest):
    def setUp(self):
        super().setUp()
        self.patch_attr(ggml, "MODELS_DIR", self.path("models"))

    def install(self, name):
        path = ggml.whisper_model_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")

    def test_a_missing_program_is_not_ready(self):
        with mock.patch("shutil.which", return_value=None):
            self.install("ggml-base.bin")
            conf = self.config(local_model="ggml-base.bin")
            self.assertFalse(conf.transcribe_ready())

    def test_a_missing_model_is_not_ready_either(self):
        with mock.patch("shutil.which", return_value="/usr/bin/whisper-server"):
            conf = self.config(local_model="ggml-base.bin")
            self.assertFalse(conf.transcribe_ready())

    def test_both_halves_in_place(self):
        with mock.patch("shutil.which", return_value="/usr/bin/whisper-server"):
            self.install("ggml-base.bin")
            conf = self.config(local_model="ggml-base.bin")
            self.assertTrue(conf.transcribe_ready())

    def test_a_hosted_provider_is_ready_when_it_has_a_key(self):
        conf = self.config(transcribe_provider="openai", openai_api_key="sk-test")
        self.assertTrue(conf.transcribe_ready())

    def test_the_settings_reach_the_servers(self):
        conf = self.config(local_model="ggml-base.bin", local_threads=4,
                           local_gpu=False, local_llm_model="gemma.gguf",
                           local_llm_context=4096)
        conf.apply_local()
        self.addCleanup(ggml.whisper.configure, model="", threads=0, gpu=True)
        self.assertEqual(ggml.whisper.settings()["model"], "ggml-base.bin")
        self.assertEqual(ggml.whisper.settings()["threads"], 4)
        self.assertFalse(ggml.whisper.settings()["gpu"])
        self.assertEqual(ggml.llm.settings()["context"], 4096)
