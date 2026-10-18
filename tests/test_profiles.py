"""Writing profiles: which one a recording gets, and what it does to the prompt.

The regression class at the top is the one to keep green. With every profile
feature off, or simply never set up, the dictation prompt must be the one the
version before profiles sent, to the byte; the fingerprints below were taken
from that version.
"""

import hashlib
from unittest import mock

from dikte import config as cfg
from dikte import profiles
from tests.support import DikteTest


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# The cleanup prompt before profiles existed, for the default settings.
BEFORE = {
    "en": "f9d7f39fb1a4d8065b394844f62826a7283a8f9b",
    "tr_speech": "3c82d10ded9ecce2db620ddd11bee5595dc4f2b2",
    "en_glossary": "c136e9ffef86c26c94a0ca408eb7d23cf05470a7",
    "tr_glossary": "62cfd49cc6540e2d9c2324547c9366146be882cc",
    "custom_glossary": "4c0149dd992003ad6867c7babb24705ba5041a9d",
}


class Unchanged(DikteTest):
    def prompts(self, conf, profile):
        out = {"en": conf.cleanup_prompt(profile=profile),
               "tr_speech": conf.cleanup_prompt(speech="tr", profile=profile)}
        conf["transcribe_prompt"] = "Paraşüt, OpenFrame"
        out["en_glossary"] = conf.cleanup_prompt(profile=profile)
        out["tr_glossary"] = conf.cleanup_prompt(speech="tr", profile=profile)
        conf["cleanup_prompt"] = "Only fix punctuation."
        out["custom_glossary"] = conf.cleanup_prompt(profile=profile)
        return {key: sha1(value) for key, value in out.items()}

    def test_the_default_settings_send_the_prompt_they_always_sent(self):
        conf = cfg.Config()
        self.assertEqual(self.prompts(conf, profiles.choose(conf, "firefox")), BEFORE)

    def test_without_a_profile_too(self):
        self.assertEqual(self.prompts(cfg.Config(), None), BEFORE)

    def test_with_every_switch_off_a_profile_full_of_settings_changes_nothing(self):
        conf = self.config(
            profile_style=False, profile_by_app=False, translation_enabled=False,
            style_learning=False,
            profile_default={"style": {"formality": "formal"},
                             "instruction": "Be brief.", "translate_to": "de"},
            profiles=[{"name": "Mail", "apps": ["thunderbird"],
                       "style": {"sentences": "short"}, "translate_to": "fr"}],
        )
        self.assertEqual(self.prompts(conf, profiles.choose(conf, "thunderbird")),
                         BEFORE)


class Choosing(DikteTest):
    def setUp(self):
        super().setUp()
        self.conf = self.config(profiles=[
            {"name": "Email", "apps": ["Thunderbird", "OUTLOOK.EXE"],
             "style": {"formality": "formal"}},
            {"name": "Chat", "apps": "slack, discord",
             "style": {"formality": "casual"}},
        ])

    def test_the_program_picks_the_profile_whatever_its_case(self):
        self.assertEqual(profiles.choose(self.conf, "thunderbird").name, "Email")
        self.assertEqual(profiles.choose(self.conf, "Outlook.exe").name, "Email")
        self.assertEqual(profiles.choose(self.conf, "SLACK").name, "Chat")

    def test_an_unknown_or_unlisted_program_gets_the_default_and_says_so(self):
        for program in ("", "firefox"):
            choice = profiles.choose(self.conf, program)
            self.assertEqual(choice.name, "")
            self.assertTrue(choice.shown)
            self.assertEqual(profiles.display_name(choice), "Default")

    def test_a_manual_choice_overrides_the_program(self):
        self.conf["profile_override"] = "chat"
        self.assertEqual(profiles.choose(self.conf, "thunderbird").name, "Chat")
        self.assertFalse(profiles.wants_detection(self.conf))

    def test_an_override_naming_a_removed_profile_is_ignored(self):
        self.conf["profile_override"] = "Gone"
        self.assertEqual(profiles.choose(self.conf, "thunderbird").name, "Email")

    def test_switched_off_the_program_is_not_even_asked_for(self):
        self.assertTrue(profiles.wants_detection(self.conf))
        self.conf["profile_by_app"] = False
        self.assertFalse(profiles.wants_detection(self.conf))
        self.assertEqual(profiles.choose(self.conf, "thunderbird").name, "")

    def test_nothing_to_detect_when_no_profile_lists_a_program(self):
        self.assertFalse(profiles.wants_detection(self.config()))

    def test_without_named_profiles_nothing_is_shown(self):
        self.assertFalse(profiles.choose(self.config(), "slack").shown)

    def test_a_retry_finds_its_profile_by_name(self):
        self.assertEqual(profiles.again(self.conf, {"profile": "email"}).name, "Email")
        self.assertEqual(profiles.again(self.conf, {"profile": "Gone"}).name, "")

    def test_a_retry_finds_a_renamed_profile_by_its_id(self):
        conf = self.config(profiles=[{"name": "Mail", "id": "abc123", "apps": ["x"]}])
        row = {"profile": "Mail", "profile_id": "abc123", "program": "thunderbird"}
        conf["profiles"] = [{"name": "Email", "id": "abc123", "apps": ["x"]}]
        choice = profiles.again(conf, row)
        self.assertEqual((choice.name, choice.program, choice.looked),
                         ("Email", "thunderbird", True))

    def test_a_profile_stored_without_an_id_gets_a_stable_one(self):
        first = profiles.named_profiles(self.conf)[0]["id"]
        self.assertTrue(first)
        self.assertEqual(profiles.named_profiles(self.conf)[0]["id"], first)

    def test_a_short_name_matches_an_application_id(self):
        conf = self.config(profiles=[{"name": "Term", "apps": ["konsole"]}])
        self.assertEqual(profiles.choose(conf, "org.kde.konsole").name, "Term")

    def test_a_two_part_name_is_not_cut_down(self):
        conf = self.config(profiles=[{"name": "Calc", "apps": ["calc", "12"]}])
        self.assertEqual(profiles.choose(conf, "libreoffice.calc").name, "")
        self.assertEqual(profiles.choose(conf, "python3.12").name, "")

    def test_malformed_profiles_do_not_break_anything(self):
        conf = self.config(profiles=["junk", {"name": ""}, {"name": "A", "apps": 3,
                                                            "style": "x"},
                                     {"name": "a"}],
                           profile_default=[1, 2])
        self.assertEqual([p["name"] for p in profiles.named_profiles(conf)], ["A"])
        self.assertEqual(profiles.default_profile(conf)["style"], {})

    def test_an_unhashable_target_language_is_no_target(self):
        for bad in ([], {}, ["de"], 3):
            conf = self.config(profile_default={"translate_to": bad},
                               profiles=[{"name": "A", "translate_to": bad}])
            self.assertEqual(profiles.default_profile(conf)["translate_to"], "")
            self.assertEqual(profiles.named_profiles(conf)[0]["translate_to"], "")
            self.assertNotIn("TRANSLATION",
                             conf.cleanup_prompt(profile=profiles.choose(conf)))
            self.assertEqual(profiles.language_name(bad), "")

    def test_a_malformed_stored_value_falls_back_on_load(self):
        self.write_config({"profiles": "not a list", "profile_style": "yes",
                           "profile_default": None, "profile_override": 3})
        conf = cfg.Config()
        self.assertEqual(conf["profiles"], [])
        self.assertIs(conf["profile_style"], True)
        self.assertEqual(conf["profile_default"], {})
        self.assertEqual(conf["profile_override"], "")
        self.assertEqual(profiles.choose(conf, "x").name, "")


class Prompt(DikteTest):
    def choice(self, conf, program=""):
        return profiles.choose(conf, program)

    def test_the_parts_come_in_their_documented_order(self):
        conf = self.config(
            transcribe_prompt="Paraşüt",
            profile_default={"style": {"formality": "formal", "sentences": "short"},
                             "instruction": "Sign off as Yusuf.",
                             "translate_to": "de"})
        prompt = conf.cleanup_prompt(profile=self.choice(conf))
        base = prompt.index("You tidy up dictation transcripts")
        glossary = prompt.index("NAMES AND TERMS THE SPEAKER USES")
        style = prompt.index("WRITING STYLE")
        translation = prompt.index("TRANSLATION")
        self.assertLess(base, glossary)
        self.assertLess(glossary, style)
        self.assertLess(style, translation)
        self.assertIn("formal register", prompt)
        self.assertIn("short sentences", prompt)
        self.assertIn("- Sign off as Yusuf.", prompt)
        self.assertIn("translate it into German", prompt)

    def test_turkish_speech_gets_the_turkish_sections(self):
        conf = self.config(profile_default={"style": {"formality": "casual"},
                                            "translate_to": "en"})
        prompt = conf.cleanup_prompt(speech="tr", profile=self.choice(conf))
        self.assertIn("YAZIM ÜSLUBU", prompt)
        self.assertIn("Hedef dil: İngilizce", prompt)

    def test_a_custom_instruction_replaces_the_base_and_keeps_the_rest(self):
        conf = self.config(cleanup_prompt="Only fix punctuation.",
                           transcribe_prompt="Paraşüt",
                           profile_default={"style": {"brevity": "concise"},
                                            "translate_to": "fr"})
        prompt = conf.cleanup_prompt(profile=self.choice(conf))
        self.assertTrue(prompt.startswith("Only fix punctuation."))
        self.assertNotIn("You tidy up dictation transcripts", prompt)
        self.assertIn("NAMES AND TERMS", prompt)
        self.assertIn("WRITING STYLE", prompt)
        self.assertIn("French", prompt)
        # Switching the style off leaves the instruction to stand alone, but
        # for the glossary and a translation that is still asked for.
        conf["profile_style"] = False
        prompt = conf.cleanup_prompt(profile=self.choice(conf))
        self.assertNotIn("WRITING STYLE", prompt)
        self.assertIn("French", prompt)

    def test_translation_with_cleanup_off_only_translates(self):
        conf = self.config(cleanup_enabled=False, transcribe_prompt="Paraşüt",
                           profile_default={"style": {"formality": "formal"},
                                            "translate_to": "de"})
        prompt = conf.cleanup_prompt(profile=self.choice(conf))
        self.assertTrue(prompt.startswith("You translate dictation transcripts"))
        self.assertIn("German", prompt)
        self.assertIn("Paraşüt", prompt)
        self.assertNotIn("WRITING STYLE", prompt)
        self.assertNotIn("You tidy up", prompt)

    def test_learned_style_counts_only_while_learning_is_on(self):
        conf = self.config(profile_default={"learned": {"sentences": "short"},
                                            "style": {"formality": "formal"}})
        self.assertNotIn("short sentences", conf.cleanup_prompt(profile=self.choice(conf)))
        conf["style_learning"] = True
        self.assertIn("short sentences", conf.cleanup_prompt(profile=self.choice(conf)))

    def test_an_explicit_choice_beats_a_learned_one(self):
        conf = self.config(style_learning=True, profile_default={
            "learned": {"formality": "casual"}, "style": {"formality": "formal"}})
        prompt = conf.cleanup_prompt(profile=self.choice(conf))
        self.assertIn("formal register", prompt)
        self.assertNotIn("conversational", prompt)

    def test_files_and_meetings_never_see_a_profile(self):
        conf = self.config(profile_default={"style": {"formality": "formal"},
                                            "translate_to": "de"})
        for prompt in (conf.cleanup_prompt(subtitles=True),
                       conf.cleanup_prompt(with_timestamps=True, with_speakers=True),
                       conf.meeting_prompt(), conf.assistant_prompt()):
            self.assertNotIn("WRITING STYLE", prompt)
            self.assertNotIn("TRANSLATION", prompt)

    def test_the_named_profile_for_the_program_is_the_one_applied(self):
        conf = self.config(profiles=[{"name": "Chat", "apps": ["slack"],
                                      "style": {"formality": "casual"}}],
                           profile_default={"style": {"formality": "formal"}})
        self.assertIn("conversational",
                      conf.cleanup_prompt(profile=self.choice(conf, "slack")))
        self.assertIn("formal register",
                      conf.cleanup_prompt(profile=self.choice(conf, "code")))


class LearningStyle(DikteTest):
    def test_short_casual_sentences(self):
        found = profiles.learn_style([
            "Hey, thanks for the fix. I'm gonna test it now. Looks good to me. "
            "Ping me later!"])
        self.assertEqual(found.get("sentences"), "short")
        self.assertEqual(found.get("formality"), "casual")
        self.assertEqual(found.get("punctuation"), "expressive")

    def test_a_correction_that_cut_words_reads_as_concise(self):
        found = profiles.learn_style([], [(
            "So what I wanted to say is that the meeting is basically moved to "
            "Thursday afternoon, I think.",
            "The meeting moved to Thursday afternoon.")])
        self.assertEqual(found.get("brevity"), "concise")

    def test_too_little_text_learns_nothing(self):
        self.assertEqual(profiles.learn_style(["Fine."]), {})

    def test_formal_turkish(self):
        found = profiles.learn_style([
            "Sayın Ahmet Bey, toplantı notlarını ekte bilgilerinize sunarım. "
            "Uygun olduğunuz bir zamanda dönüş yapmanızı rica ederim. "
            "Sorularınız olursa iletişime geçebilirsiniz. Saygılarımla."])
        self.assertEqual(found.get("formality"), "formal")


class Lookup(DikteTest):
    """The program is asked for beside the recording, never in front of it."""

    def test_nothing_to_detect_answers_at_once(self):
        from dikte import app
        with mock.patch.object(app.frontapp, "program_name") as asked:
            lookup = app._ProfileLookup(self.config(), None)
            self.assertEqual(lookup.result().name, "")
        asked.assert_not_called()

    def test_the_lookup_runs_on_its_own_thread(self):
        import threading
        from dikte import app
        conf = self.config(profiles=[{"name": "Chat", "apps": ["slack"]}])
        gate, threads = threading.Event(), []

        def slow(_front):
            threads.append(threading.current_thread())
            gate.wait(5)
            return "slack"

        with mock.patch.object(app.frontapp, "program_name", side_effect=slow):
            lookup = app._ProfileLookup(conf, None)
            gate.set()
            choice = lookup.result()
            self.assertEqual((choice.name, choice.looked), ("Chat", True))
        self.assertIsNot(threads[0], threading.main_thread())

    def test_a_lookup_that_never_answers_falls_back_to_default(self):
        import threading
        from dikte import app
        conf = self.config(profiles=[{"name": "Chat", "apps": ["slack"]}])
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(app.frontapp, "program_name",
                               side_effect=lambda _f: gate.wait(5) and "slack"), \
                mock.patch.object(app._ProfileLookup, "WAIT", 0.05):
            choice = app._ProfileLookup(conf, None).result()
        self.assertEqual((choice.name, choice.program), ("", ""))
        # An answer landing just after the wait gave up is still the answer.
        lookup = app._ProfileLookup(conf, None)
        real = profiles.choose(conf, "slack", True)
        thread = mock.Mock()
        thread.is_alive.return_value = True
        thread.join.side_effect = lambda _t: setattr(lookup, "_choice", real)
        lookup._thread = thread
        self.assertIs(lookup.result(), real)
        # Answering late changes nothing already handed out.
        lookup = app._ProfileLookup(conf, None)
        lookup._settled = True
        lookup._choice = choice
        lookup._choose("slack")
        self.assertIs(lookup._choice, choice)
