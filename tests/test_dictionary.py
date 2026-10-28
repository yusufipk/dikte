"""The personal dictionary: records, the setting they are kept in step with,
and what learning may add to them."""

import json
import os
import tempfile
from unittest import mock

from PyQt6.QtCore import QLockFile

from dikte import config as cfg
from dikte import dictionary
from tests.support import DikteTest, posix_only


class Migration(DikteTest):
    def test_untouched_text_is_passed_on_exactly_as_it_was(self):
        """No file yet: the hint and the glossary are the stored text itself,
        line breaks and all, so a user who never opens the dictionary sees
        nothing change."""
        conf = self.config(transcribe_prompt="Paraşüt\nOpenFrame, Dikte  ")
        self.assertEqual(conf.dictionary_hint(), "Paraşüt\nOpenFrame, Dikte  ")
        self.assertEqual(conf.dictionary_glossary(), "Paraşüt\nOpenFrame, Dikte")
        self.assertFalse(dictionary.path().exists())

    def test_the_text_becomes_one_record_per_line_or_comma(self):
        conf = self.config(transcribe_prompt="Paraşüt, OpenFrame\n\nYusuf İpek")
        found = dictionary.records(conf)
        self.assertEqual([r["term"] for r in found],
                         ["Paraşüt", "OpenFrame", "Yusuf İpek"])
        self.assertEqual({r["source"] for r in found}, {dictionary.MANUAL})

    def test_rendered_back_it_reads_as_the_same_list(self):
        text = "Paraşüt, OpenFrame, Yusuf İpek"
        conf = self.config(transcribe_prompt=text)
        self.assertEqual(dictionary.render_hint(dictionary.records(conf)), text)

    def test_every_piece_is_kept_even_a_repeated_one(self):
        conf = self.config(transcribe_prompt="Dikte, dikte, DIKTE")
        self.assertEqual([r["term"] for r in dictionary.records(conf)],
                         ["Dikte", "dikte", "DIKTE"])

    def test_an_edit_keeps_the_lines_the_text_was_typed_in(self):
        conf = self.config(transcribe_prompt="Paraşüt, OpenFrame\n\nYusuf İpek\nDikte")
        found = dictionary.records(conf)
        found.append(dictionary.entry("Grafana"))
        dictionary.store(conf, found, dictionary.records(conf))
        self.assertEqual(conf["transcribe_prompt"],
                         "Paraşüt, OpenFrame\n\nYusuf İpek\nDikte, Grafana")
        self.assertEqual(self.read_config_file()["transcribe_prompt"],
                         "Paraşüt, OpenFrame\n\nYusuf İpek\nDikte, Grafana")


class Records(DikteTest):
    def stored(self, conf, entries, loaded=()):
        return dictionary.store(conf, [dictionary.entry(*e) for e in entries],
                                list(loaded))

    def test_a_spelling_reaches_the_hint_and_the_glossary_differently(self):
        conf = self.config()
        self.stored(conf, [("kube control", "kubectl"), ("Paraşüt",)])
        self.assertEqual(conf.dictionary_hint(), "kubectl, Paraşüt")
        self.assertEqual(conf.dictionary_glossary(), "kube control -> kubectl, Paraşüt")
        self.assertIn("kube control -> kubectl", conf.cleanup_prompt())
        # The setting carries the hint, for an older version to read.
        self.assertEqual(self.read_config_file()["transcribe_prompt"],
                         "kubectl, Paraşüt")

    def test_two_records_with_one_spelling_stay_two_after_an_outside_edit(self):
        conf = self.config()
        self.stored(conf, [("kube control", "kubectl"), ("kube cuddle", "kubectl")])
        conf["transcribe_prompt"] = "kubectl, kubectl, Grafana"
        self.assertEqual([(r["term"], r["spelling"]) for r in dictionary.records(conf)],
                         [("kube control", "kubectl"), ("kube cuddle", "kubectl"),
                          ("Grafana", "")])

    def test_an_edit_from_the_command_line_wins_over_the_file(self):
        conf = self.config()
        self.stored(conf, [("kube control", "kubectl"), ("Paraşüt",)])
        conf["transcribe_prompt"] = "kubectl, OpenFrame"
        found = dictionary.records(conf)
        # The piece that did not change keeps what it knew about itself.
        self.assertEqual([(r["term"], r["spelling"]) for r in found],
                         [("kube control", "kubectl"), ("OpenFrame", "")])

    def test_a_save_keeps_what_was_learned_while_the_window_was_open(self):
        conf = self.config()
        loaded = self.stored(conf, [("Paraşüt",)])
        dictionary._write(loaded + [dictionary.entry("Claude Code", source="auto")],
                          {}, conf["transcribe_prompt"])
        merged = self.stored(conf, [("Paraşüt",), ("OpenFrame",)], loaded)
        self.assertEqual([r["term"] for r in merged],
                         ["Paraşüt", "OpenFrame", "Claude Code"])

    def test_what_is_learned_while_config_json_is_written_is_kept_too(self):
        conf = self.config()
        loaded = self.stored(conf, [("Paraşüt",)])
        pending = dictionary.save(conf, loaded + [dictionary.entry("OpenFrame")], loaded)
        dictionary._write(loaded + [dictionary.entry("Claude", source="auto")],
                          {"Grafana": 2}, "Paraşüt")
        merged = dictionary.write(pending)
        self.assertEqual([r["term"] for r in merged], ["Paraşüt", "OpenFrame", "Claude"])
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertEqual(stored["pending"], {"grafana": [2, 0]})

    def test_a_spelling_learned_while_the_user_typed_it_is_listed_once(self):
        """The window saved "kubectl" typed in, and learning, in the moment
        before the file was written, kept "kube cuttle -> kubectl": the hint
        must not say kubectl twice."""
        conf = self.config()
        loaded = self.stored(conf, [("Paraşüt",)])
        pending = dictionary.save(conf, loaded + [dictionary.entry("kubectl")], loaded)
        dictionary._write(loaded + [dictionary.entry("kube cuttle", "kubectl", "auto")],
                          {}, "Paraşüt")
        merged = dictionary.write(pending)
        self.assertEqual([(r["term"], r["spelling"]) for r in merged],
                         [("Paraşüt", ""), ("kubectl", "")])
        self.assertEqual(conf.dictionary_hint(), "Paraşüt, kubectl")

    def test_one_learned_before_the_save_is_left_out_the_same_way(self):
        conf = self.config()
        loaded = self.stored(conf, [("Paraşüt",)])
        dictionary._write(loaded + [dictionary.entry("kube cuttle", "kubectl", "auto")],
                          {}, "Paraşüt")
        merged = self.stored(conf, [("Paraşüt",), ("Kubectl",)], loaded)
        self.assertEqual([r["term"] for r in merged], ["Paraşüt", "Kubectl"])

    def test_a_deletion_in_the_window_is_a_deletion(self):
        conf = self.config()
        loaded = self.stored(conf, [("Paraşüt",), ("OpenFrame",)])
        merged = self.stored(conf, [("OpenFrame",)], loaded)
        self.assertEqual([r["term"] for r in merged], ["OpenFrame"])

    def test_a_broken_file_falls_back_to_the_setting(self):
        conf = self.config(transcribe_prompt="Paraşüt")
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        dictionary.path().write_text("{not json", encoding="utf-8")
        self.assertEqual(conf.dictionary_hint(), "Paraşüt")

    def test_malformed_records_are_dropped_not_fatal(self):
        conf = self.config()
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        dictionary.path().write_text(json.dumps({
            "entries": [{"term": "Dikte"}, "junk", {"term": ""}, {"no": 1}],
            "pending": {"x": "many"}, "synced": "",
        }), encoding="utf-8")
        self.assertEqual([r["term"] for r in dictionary.records(conf)], ["Dikte"])

    def test_clearing_the_learned_ones_keeps_the_typed_ones(self):
        kept = dictionary.clear_auto([dictionary.entry("A b"),
                                      dictionary.entry("Claude", source="auto")])
        self.assertEqual([r["term"] for r in kept], ["A b"])


class Learning(DikteTest):
    def learn(self, conf, text, times=1, raw=None, language="en"):
        added = []
        for _ in range(times):
            added += dictionary.learn(conf, raw if raw is not None else text,
                                      text, language)
        return added

    def test_a_name_mid_sentence_is_found_as_a_run(self):
        self.assertEqual(
            dictionary.candidates("We moved it to Claude Code yesterday. "
                                  "Then we broke OpenFrame."),
            ["Claude Code", "OpenFrame"])

    def test_a_name_at_a_sentence_start_is_not_cut_short(self):
        """Not "Code" from "Claude Code is great": the run is kept whole and
        only counts once it has been seen in the middle of a sentence."""
        self.assertEqual(dictionary.candidates("Claude Code is great."), [])
        conf = self.config()
        self.assertEqual(self.learn(conf, "Claude Code is great.", 5), [])
        self.assertEqual(self.learn(conf, "Visual Studio Code is open.", 5), [])
        self.learn(conf, "I opened Claude Code again.")
        added = self.learn(conf, "Claude Code is great.", 2)
        self.assertEqual([r["term"] for r in added], ["Claude Code"])

    def test_languages_and_nationalities_are_not_names(self):
        self.assertEqual(dictionary.candidates(
            "I write it in English and the Turkish team reads it, "
            "bunu da Türkçe ve İngilizce yazdım."), [])

    def test_sentence_starts_days_and_acronyms_are_not_names(self):
        self.assertEqual(
            dictionary.candidates("Today I call the API on Monday. Hello there."),
            [])

    def test_a_turkish_suffix_is_left_off(self):
        self.assertEqual(dictionary.candidates("Bunu dün Yusuf'un bilgisayarında yaptım."),
                         ["Yusuf"])

    def test_three_dictations_make_an_entry(self):
        conf = self.config()
        text = "I pushed the fix to OpenFrame this morning."
        self.assertEqual(self.learn(conf, text, times=2), [])
        added = self.learn(conf, text)
        self.assertEqual([(r["term"], r["source"]) for r in added],
                         [("OpenFrame", "auto")])
        self.assertIn("OpenFrame", conf.dictionary_hint())
        # Seen again, it is already known.
        self.assertEqual(self.learn(conf, text, times=3), [])

    def test_an_everyday_word_put_right_is_not_learned(self):
        """"cloud" made "Claude" is a fix for that sentence; learned, it would
        push every cloud towards Claude. Neither the name nor the correction."""
        conf = self.config()
        added = self.learn(conf, "I asked Claude about it.", times=3,
                           raw="I asked cloud about it.")
        self.assertEqual(added, [])
        self.assertEqual(dictionary.corrections("bulut sistemine yükledim",
                                                "Bulut sistemine yükledim."), [])
        # A heard word that is not everyday still counts, widened to the
        # whole name it is part of.
        self.assertEqual(dictionary.corrections("we tried klod code", "We tried Claude Code."),
                         [("klod code", "Claude Code")])

    def test_german_capitals_say_nothing(self):
        conf = self.config()
        self.assertEqual(self.learn(conf, "Ich habe den Hund gesehen.", 3,
                                    language="de"), [])

    def test_german_is_told_by_its_words_when_the_language_is_auto(self):
        """A hosted transcriber on auto never says what it heard, so the
        language arrives as "auto", and the German has to be read."""
        conf = self.config()
        for language in ("auto", ""):
            with self.subTest(language=language):
                self.assertEqual(self.learn(
                    conf, "Wir haben das mit dem Kunden und der Abteilung besprochen.",
                    3, language=language), [])
        added = self.learn(conf, "I pushed the fix to OpenFrame this morning.", 3,
                           language="auto")
        self.assertEqual([r["term"] for r in added], ["OpenFrame"])
        self.assertFalse(dictionary.capitalises_nouns(
            "auto", "Bugün Ahmet ile toplantı yaptık ve plan hazır."))
        self.assertTrue(dictionary.capitalises_nouns("de", "We met Ahmet."))

    def test_a_dictation_with_nothing_new_leaves_the_file_alone(self):
        conf = self.config()
        text = "I pushed the fix to OpenFrame this morning."
        self.learn(conf, text, 3)
        with mock.patch.object(dictionary, "_write") as written:
            self.assertEqual(self.learn(conf, text, 2), [])
        written.assert_not_called()

    def test_a_term_already_in_the_dictionary_is_not_added_again(self):
        conf = self.config(transcribe_prompt="openframe")
        self.assertEqual(self.learn(conf, "It runs on OpenFrame now.", 3), [])

    def test_a_new_line_or_a_list_item_starts_a_sentence(self):
        self.assertEqual(dictionary.candidates("Notes\nCall the doctor\n- Fix it"), [])
        self.assertEqual(dictionary.candidates("Notes:\nask Grafana"), ["Grafana"])

    def test_a_deleted_row_forgets_whole_words_only(self):
        conf = self.config()
        dictionary._write([], {"Ali": [1, 1], "Claude Code": [1, 1], "Grafana": [1, 1]}, "")
        cfg.delete_history([{"raw": "kalibrasyon Alican ile", "text": "I asked Claude  Code\tnow."}])
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertEqual(sorted(stored["pending"]), ["ali", "grafana"])

    def test_a_term_without_words_is_never_matched_by_a_wordless_row(self):
        self.config()
        dictionary._write([], {"123": [1, 1], "Grafana": [1, 1]}, "")
        cfg.delete_history([{"raw": "42", "text": "Grafana"}])
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertEqual(sorted(stored["pending"]), ["123"])

    def test_fold_is_public_and_folds_turkish_capitals(self):
        self.assertEqual(dictionary.fold("İstanbul"), dictionary.fold("istanbul"))

    def test_a_line_break_ends_a_name(self):
        self.assertEqual(dictionary.candidates("I talked to Ahmet\nand Mehmet is here"),
                         ["Ahmet", "Mehmet"])
        self.assertEqual(dictionary.candidates("I talked to Ahmet\nMehmet is here"),
                         ["Ahmet"])
        self.assertEqual(dictionary.runs("I met Ahmet. Mehmet came too."),
                         [("Ahmet", False), ("Mehmet", True)])

    def test_typed_entries_always_go_in_whole(self):
        conf = self.config()
        typed = [dictionary.entry("x" * 400), dictionary.entry("y" * 400)]
        learned = [dictionary.entry("Learned", source="auto")]
        dictionary.store(conf, typed + learned, [])
        self.assertEqual(conf.dictionary_hint(), "x" * 400 + ", " + "y" * 400)

    def test_the_meeting_names_count_against_the_budget(self):
        conf = self.config(meeting_participants="P" * 300)
        learned = [dictionary.entry(f"Learned{i:03d}", source="auto") for i in range(100)]
        dictionary.store(conf, learned, [])
        self.assertLessEqual(len(conf.meeting_hint()), dictionary.HINT_BUDGET + 10)
        self.assertIn("P" * 300, conf.meeting_hint())

    def test_a_reader_never_pairs_a_new_stamp_with_old_data(self):
        conf = self.config()
        dictionary.store(conf, [dictionary.entry("Paraşüt")], [])
        dictionary.records(conf)
        stamp, data = dictionary._cache["entry"]
        self.assertIsNotNone(stamp)
        self.assertEqual([r["term"] for r in data["entries"]], ["Paraşüt"])

    def test_a_full_count_still_counts_a_new_word(self):
        """Even when every word waiting has been seen twice: the ones not seen
        for longest go, not the newcomer."""
        conf = self.config()
        dictionary._write([], {f"Word{i}": [2, i] for i in range(dictionary.PENDING_LIMIT)},
                          "", clock=dictionary.PENDING_LIMIT)
        self.learn(conf, "It runs on OpenFrame now.")
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertIn("openframe", stored["pending"])
        self.assertNotIn("word0", stored["pending"])
        self.assertEqual(len(stored["pending"]), dictionary.PENDING_LIMIT)
        self.learn(conf, "It runs on OpenFrame now.", 2)
        self.assertIn("OpenFrame", conf.dictionary_hint())

    def test_an_older_file_with_bare_counts_still_reads(self):
        conf = self.config()
        dictionary._write([], {"OpenFrame": 2}, "")
        added = self.learn(conf, "It runs on OpenFrame now.")
        self.assertEqual([r["term"] for r in added], ["OpenFrame"])

    def test_the_hint_puts_typed_words_first_and_stays_in_budget(self):
        conf = self.config()
        typed = [dictionary.entry(f"Typed{i}") for i in range(3)]
        learned = [dictionary.entry(f"Learned{i:03d}", source="auto") for i in range(200)]
        dictionary.store(conf, learned[:1] + typed + learned[1:], [])
        hint = conf.dictionary_hint()
        self.assertTrue(hint.startswith("Typed0, Typed1, Typed2, Learned000"))
        self.assertLessEqual(len(hint), dictionary.HINT_BUDGET)
        self.assertIn("Learned199", conf.dictionary_glossary())

    def test_pending_names_go_with_learning_and_the_history(self):
        conf = self.config()
        self.learn(conf, "It runs on OpenFrame now.")
        cfg.clear_history()
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertEqual(stored["pending"], {})
        self.learn(conf, "It runs on OpenFrame now.")
        self.learn(conf, "We asked Grafana about it.")
        # Deleting one row forgets only the names that row held.
        cfg.delete_history([{"ts": "x", "raw": "it runs on openframe",
                             "text": "It runs on OpenFrame now."}])
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertEqual(list(stored["pending"]), ["grafana"])

    def test_a_command_line_edit_keeps_what_was_learned(self):
        conf = self.config(transcribe_prompt="Paraşüt")
        self.learn(conf, "It runs on OpenFrame now.", 3)
        conf["transcribe_prompt"] = "Paraşüt, Grafana"
        self.assertEqual([r["term"] for r in dictionary.records(conf)],
                         ["Paraşüt", "Grafana", "OpenFrame"])

    def test_a_deleted_learned_word_is_not_learned_again(self):
        conf = self.config()
        self.learn(conf, "It runs on OpenFrame now.", 3)
        loaded = dictionary.records(conf)
        dictionary.store(conf, dictionary.clear_auto(loaded), loaded)
        self.assertEqual(self.learn(conf, "It runs on OpenFrame now.", 3), [])
        self.assertEqual(dictionary.records(conf), [])

    def test_learning_leaves_config_json_alone(self):
        """It runs on the dictation thread; the settings file is not its to
        write, and the dictionary file keeps the two in step."""
        conf = self.config(transcribe_prompt="Paraşüt")
        self.learn(conf, "It runs on OpenFrame now.", 3)
        self.assertFalse(cfg.CONFIG_FILE.exists())
        self.assertEqual(conf.dictionary_hint(), "Paraşüt, OpenFrame")


class Files(DikteTest):
    """The file is rewritten by the application and by the command line, each
    from a process of its own."""

    def test_each_write_has_a_temporary_file_of_its_own(self):
        made, real = [], tempfile.mkstemp

        def mkstemp(**kwargs):
            made.append(kwargs)
            return real(**kwargs)

        with mock.patch.object(dictionary.tempfile, "mkstemp", side_effect=mkstemp):
            dictionary._write([dictionary.entry("Dikte")], {}, "Dikte")
        self.assertEqual(made[0]["dir"], cfg.CONFIG_DIR)
        self.assertEqual([p.name for p in cfg.CONFIG_DIR.iterdir()], ["dictionary.json"])

    @posix_only
    def test_the_words_are_never_readable_by_others(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        opened = []
        real = os.fdopen

        def fdopen(handle, *args, **kwargs):
            # Already private when the first word goes in.
            opened.append(os.fstat(handle).st_mode & 0o777)
            return real(handle, *args, **kwargs)

        with mock.patch.object(dictionary.os, "fdopen", side_effect=fdopen):
            dictionary._write([dictionary.entry("Dikte")], {}, "Dikte")
        self.assertEqual(opened, [0o600])
        self.assertEqual(dictionary.path().stat().st_mode & 0o777, 0o600)

    def test_an_unreadable_file_is_set_aside_not_written_over(self):
        conf = self.config()
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        dictionary.path().write_text("{not json", encoding="utf-8")
        broken = cfg.CONFIG_DIR / "dictionary.json.broken"
        # `dikte history clear`, which used to take it for an empty one.
        with mock.patch("builtins.print"):
            cfg.clear_history()
        self.assertEqual(broken.read_text(encoding="utf-8"), "{not json")
        self.assertFalse(dictionary.path().exists())
        dictionary.learn(conf, "It runs on OpenFrame now.", "It runs on OpenFrame now.")
        self.assertEqual(broken.read_text(encoding="utf-8"), "{not json")
        self.assertIn("openframe", dictionary._read_file()["pending"])

    def test_a_file_that_cannot_be_opened_is_not_written_over(self):
        """Held by a virus scanner for a moment, say: an error, not an empty
        dictionary to write the learning into."""
        conf = self.config()
        dictionary.learn(conf, "It runs on OpenFrame now.", "It runs on OpenFrame now.")
        before = dictionary.path().read_text(encoding="utf-8")
        real = open

        def held(file, *args, **kwargs):
            if str(file) == str(dictionary.path()):
                raise PermissionError(13, "held", str(file))
            return real(file, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=held), \
                self.assertRaises(OSError):
            dictionary.learn(conf, "We asked Grafana about it.",
                             "We asked Grafana about it.")
        self.assertEqual(dictionary.path().read_text(encoding="utf-8"), before)
        self.assertFalse((cfg.CONFIG_DIR / "dictionary.json.broken").exists())

    def test_a_file_held_by_another_process_is_waited_for(self):
        """`dikte history delete` in a terminal while the application learns:
        each takes the file's lock, so neither writes over the other."""
        conf = self.config()
        dictionary.learn(conf, "It runs on OpenFrame now.", "It runs on OpenFrame now.")
        other = QLockFile(str(cfg.CONFIG_DIR / "dictionary.json.lock"))
        self.assertTrue(other.tryLock(0))
        self.addCleanup(other.unlock)
        with mock.patch.object(dictionary, "FILE_LOCK_MS", 20):
            with self.assertRaises(OSError):
                dictionary.forget_pending()
            # Said, not raised, by the history it came with.
            with mock.patch("builtins.print") as said:
                cfg.clear_history()
            self.assertIn("could not forget", said.call_args.args[0])
        self.assertIn("openframe", dictionary._read_file()["pending"])
        other.unlock()
        dictionary.forget_pending()
        self.assertEqual(dictionary._read_file()["pending"], {})


class Corrections(DikteTest):
    """Spellings learned from what the cleanup keeps putting right."""

    def learn(self, conf, raw, text, times=1):
        added = []
        for _ in range(times):
            added += dictionary.learn(conf, raw, text, "en")
        return added

    def test_a_misheard_name_and_its_spelling_are_found(self):
        self.assertEqual(dictionary.corrections("we tried klod kod yesterday",
                                                "We tried Claude Code yesterday."),
                         [("klod kod", "Claude Code")])
        self.assertEqual(dictionary.corrections("deploy it on cubernetis now",
                                                "Deploy it on Kubernetes now."),
                         [("cubernetis", "Kubernetes")])

    def test_what_is_not_a_correction(self):
        for raw, text in (
            ("i gonna send it", "I am going to send it."),        # grammar
            ("send it to ahmet", "Send it to Ahmet."),             # case only
            ("we use node js", "We use node.js."),                 # punctuation only
            ("send it to ahmete", "Send it to Ahmet'e."),          # suffix only
            ("ask ahmeti about it", "Ask Ahmet about it."),        # suffix dropped
            ("so we did it uh", "We did it."),                     # words dropped
            ("we did it", "We did it with OpenFrame."),            # words added
            ("the thing we talked", "OpenFrame we talked."),       # a rewrite
            ("we are in istanbul", "We are in İstanbul."),         # diacritic only
        ):
            with self.subTest(raw=raw):
                self.assertEqual(dictionary.corrections(raw, text), [])

    def test_three_sightings_make_an_entry_with_what_it_was_heard_as(self):
        conf = self.config()
        raw, text = "we tried klod kod yesterday", "We tried Claude Code yesterday."
        self.assertEqual(self.learn(conf, raw, text, 2), [])
        added = self.learn(conf, raw, text)
        self.assertEqual([(r["term"], r["spelling"], r["source"]) for r in added],
                         [("klod kod", "Claude Code", "auto")])
        self.assertIn("Claude Code", conf.dictionary_hint())
        self.assertNotIn("klod", conf.dictionary_hint())
        self.assertIn("klod kod -> Claude Code", conf.dictionary_glossary())
        self.assertEqual(self.learn(conf, raw, text, 3), [])

    def test_a_listed_or_refused_spelling_is_not_learned_again(self):
        conf = self.config(transcribe_prompt="Claude Code")
        raw, text = "we tried klod kod yesterday", "We tried Claude Code yesterday."
        self.assertEqual(self.learn(conf, raw, text, 3), [])
        conf = self.config()
        dictionary._write([], {}, "", rejected=["klod kod"])
        self.assertEqual(self.learn(conf, raw, text, 3), [])

    def test_counting_is_forgotten_like_names(self):
        conf = self.config()
        self.learn(conf, "we tried klod kod", "We tried Claude Code.")
        self.learn(conf, "it runs on cubernetis", "It runs on Kubernetes.")
        cfg.delete_history([{"raw": "we tried klod kod", "text": "We tried Claude Code."}])
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertEqual(list(stored["pending"]), ["cubernetis -> kubernetes"])
        cfg.clear_history()
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertEqual(stored["pending"], {})


class CorrectionShapes(DikteTest):
    def test_a_half_misheard_name_is_learned_whole(self):
        self.assertEqual(dictionary.corrections("we use cloud code daily",
                                                "We use Claude Code daily."),
                         [("cloud code", "Claude Code")])

    def test_part_of_a_listed_name_is_not_a_term_of_its_own(self):
        conf = self.config(transcribe_prompt="Visual Studio Code")
        for _ in range(3):
            added = dictionary.learn(conf, "open studio kode now",
                                     "Open Studio Code now.", "en")
        self.assertEqual(added, [])

    def test_swapped_names_are_moved_not_misheard(self):
        self.assertEqual(dictionary.corrections("anna called hannah",
                                                "Hannah called Anna."), [])

    def test_a_short_word_grown_into_a_longer_one_is_not_a_correction(self):
        self.assertEqual(dictionary.corrections("ask mark about it",
                                                "Ask Marketing about it."), [])

    def test_the_same_correction_counts_once_whatever_its_case(self):
        conf = self.config()
        dictionary.learn(conf, "we tried Klod Kod", "We tried Claude Code.", "en")
        dictionary.learn(conf, "we tried klod kod", "We tried Claude Code.", "en")
        added = dictionary.learn(conf, "we tried KLOD KOD", "We tried Claude Code.", "en")
        self.assertEqual([r["spelling"] for r in added], ["Claude Code"])

    def test_filler_is_not_part_of_what_was_heard(self):
        self.assertEqual(dictionary.corrections("we tried uh klod kod",
                                                "We tried Claude Code."),
                         [("klod kod", "Claude Code")])
        self.assertEqual(dictionary.corrections("şey klod kod hani dedim",
                                                "Claude Code dedim."),
                         [("klod kod", "Claude Code")])

    def test_a_deleted_correction_refuses_its_spelling_too(self):
        conf = self.config()
        for _ in range(3):
            dictionary.learn(conf, "we tried klod kod", "We tried Claude Code.", "en")
        loaded = dictionary.records(conf)
        self.assertEqual([r["spelling"] for r in loaded], ["Claude Code"])
        dictionary.store(conf, [], loaded)
        for _ in range(3):
            added = dictionary.learn(conf, "we tried klot kod", "We tried Claude Code.", "en")
        self.assertEqual(added, [])
        self.assertEqual(dictionary.records(conf), [])

    def test_turkish_i_folds_every_way(self):
        self.assertEqual(dictionary.fold("Işık"), dictionary.fold("IŞIK"))
        self.assertEqual(dictionary.fold("ılık"), dictionary.fold("Ilık"))
        self.assertEqual(dictionary.fold("İstanbul"), "istanbul")


class OlderFiles(DikteTest):
    def test_rejected_and_pending_keys_are_folded_on_read(self):
        """A file written before fold() learned the dotless i still matches."""
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        dictionary.path().write_text(json.dumps({
            "entries": [], "synced": "", "clock": 3,
            "rejected": ["ışık", "IŞIK", "Kod"],
            "pending": {"Işık": [1, 1], "ışık": [2, 3], "Klod Kod -> Claude Code": [1, 2]},
        }), encoding="utf-8")
        stored = dictionary._read_file()
        self.assertEqual(stored["rejected"], ["işik", "kod"])
        self.assertEqual(stored["pending"], {"işik": [2, 3],
                                             "klod kod -> claude code": [1, 2]})


class PartialNames(DikteTest):
    def test_a_name_cut_off_at_the_sentence_start_is_not_learned(self):
        self.assertEqual(dictionary.corrections("claude kod is great",
                                                "Claude Code is great."), [])

    def test_a_whole_name_at_the_sentence_start_still_is(self):
        self.assertEqual(dictionary.corrections("klod kod is great",
                                                "Claude Code is great."),
                         [("klod kod", "Claude Code")])


class RefusingSpellings(DikteTest):
    def test_a_spelling_a_typed_entry_uses_is_not_refused(self):
        conf = self.config()
        typed = dictionary.entry("Claude Code")
        learned = dictionary.entry("klod kod", "Claude Code", dictionary.AUTO)
        loaded = dictionary.store(conf, [typed, learned], [])
        dictionary.store(conf, dictionary.clear_auto(loaded), loaded)
        rejected = dictionary._read_file()["rejected"]
        self.assertIn("klod kod", rejected)
        self.assertNotIn("claude code", rejected)


class SentenceStartWords(DikteTest):
    def test_an_everyday_first_word_does_not_stop_the_correction(self):
        self.assertEqual(dictionary.corrections("try cloud code", "Try Claude Code."),
                         [("cloud code", "Claude Code")])
        self.assertEqual(dictionary.corrections("open klod kod now", "Open Claude Code now."),
                         [("klod kod", "Claude Code")])

    def test_a_name_cut_off_at_the_start_still_drops(self):
        self.assertEqual(dictionary.corrections("claude kod is great",
                                                "Claude Code is great."), [])

    def test_one_term_counts_once_a_dictation_whatever_its_case(self):
        conf = self.config()
        dictionary.learn(conf, "we use github and github daily",
                         "We use GitHub and Github daily.", "en")
        stored = dictionary._read_file()
        self.assertEqual(stored["pending"], {"github": [1, 1]})

    def test_words_rewritten_around_a_name_heard_right_are_no_correction(self):
        self.assertEqual(dictionary.corrections("on github", "We use GitHub."), [])
