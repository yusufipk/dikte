"""The terminal interface, which is the part a script depends on.

Output is a contract as much as an interface: --json prints one object on
stdout, progress goes to stderr so it never lands in a pipe, and the exit code
says which of the four things happened. Nothing here starts an instance; the
socket is faked, and everything that runs locally runs for real.
"""

import contextlib
import io
import json
import unittest
from unittest import mock

import cli
import config as cfg
import hotkey
import ipc
from tests.support import DikteTest, fake_urlopen


class Options:
    """The parsed command line, as much of it as the printers read."""

    def __init__(self, **values):
        self.json = False
        self.quiet = False
        for key, value in values.items():
            setattr(self, key, value)


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class Printing(unittest.TestCase):
    def test_plain_output_is_the_thing_a_person_wanted(self):
        with captured() as (out, err):
            code = cli.out(Options(), {"ok": True, "text": "hello"}, "hello")
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "hello")
        self.assertEqual(err.getvalue(), "")

    def test_json_output_is_one_object(self):
        with captured() as (out, _):
            cli.out(Options(json=True), {"ok": True, "text": "hello"}, "hello")
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "text": "hello"})

    def test_nothing_to_say_prints_nothing(self):
        with captured() as (out, _):
            cli.out(Options(), {"ok": True})
        self.assertEqual(out.getvalue(), "")

    def test_progress_never_reaches_stdout(self):
        with captured() as (out, err):
            cli.note(Options(), "Transcribing…")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Transcribing…", err.getvalue())

    def test_quiet_keeps_progress_off_stderr_too(self):
        with captured() as (_, err):
            cli.note(Options(quiet=True), "Transcribing…")
        self.assertEqual(err.getvalue(), "")

    def test_a_failure_goes_to_stderr_and_returns_one(self):
        with captured() as (out, err):
            code = cli.fail(Options(), "no microphone")
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("no microphone", err.getvalue())

    def test_a_failure_as_json_stays_on_stdout(self):
        with captured() as (out, err):
            code = cli.fail(Options(json=True), "no microphone", 3, running=False)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue()),
                         {"ok": False, "error": "no microphone", "running": False})
        self.assertEqual(err.getvalue(), "")


class Coerce(unittest.TestCase):
    """A value off the command line, in the type the setting is stored as."""

    def test_a_string_stays_a_string(self):
        self.assertEqual(cli._coerce("cleanup_model", "some/model"), "some/model")

    def test_the_words_that_mean_true(self):
        for raw in ("1", "true", "TRUE", "yes", "on", " True "):
            with self.subTest(raw=raw):
                self.assertIs(cli._coerce("cleanup_enabled", raw), True)

    def test_the_words_that_mean_false(self):
        for raw in ("0", "false", "no", "off"):
            with self.subTest(raw=raw):
                self.assertIs(cli._coerce("cleanup_enabled", raw), False)

    def test_anything_else_is_not_a_boolean(self):
        with self.assertRaises(ValueError):
            cli._coerce("cleanup_enabled", "maybe")

    def test_a_whole_number(self):
        self.assertEqual(cli._coerce("history_limit", "50"), 50)
        self.assertEqual(cli._coerce("history_limit", "50.9"), 50)

    def test_a_number_with_a_fraction(self):
        self.assertEqual(cli._coerce("silence_db", "-42.5"), -42.5)

    def test_something_that_is_not_a_number(self):
        with self.assertRaises(ValueError):
            cli._coerce("history_limit", "lots")

    def test_a_boolean_is_settled_before_it_is_read_as_a_number(self):
        """bool is a subclass of int, so the order of the checks matters."""
        self.assertIs(cli._coerce("cleanup_enabled", "1"), True)


class Masking(unittest.TestCase):
    def test_a_key_is_shown_by_its_last_four(self):
        self.assertEqual(cli._mask("openai_api_key", "sk-abcdefgh1234"), "…1234")

    def test_an_empty_key_is_not_masked_into_something(self):
        self.assertEqual(cli._mask("openai_api_key", ""), "")

    def test_anything_that_is_not_a_key_is_shown(self):
        self.assertEqual(cli._mask("cleanup_model", "some/model"), "some/model")

    def test_both_keys_are_covered(self):
        for key in cli.SECRET_KEYS:
            with self.subTest(key=key):
                self.assertTrue(cli._mask(key, "sk-secret").startswith("…"))


class Parser(unittest.TestCase):
    """Every verb has to parse, and keep the flag that was typed before it."""

    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def test_no_verb_at_all_is_the_settings_window(self):
        # argparse leaves the dest as None; run() is what turns it into "".
        opts = self.parse()
        self.assertIsNone(opts.verb)
        self.assertEqual(opts.func, cli.cmd_plain)

    def test_every_global_shortcut_runs_a_verb_that_exists(self):
        """A shortcut registers a command line; a verb the parser never heard of
        is a key that does nothing at all when it is pressed."""
        for name, spec in hotkey.SHORTCUTS.items():
            with self.subTest(name=name):
                opts = self.parse(spec.verb)
                self.assertTrue(callable(opts.func))

    def test_every_shortcut_can_be_installed_and_removed_by_name(self):
        for name in hotkey.SHORTCUTS:
            with self.subTest(name=name):
                self.assertEqual(self.parse("shortcut", "install", name).which,
                                 name)
                self.assertEqual(self.parse("shortcut", "remove", name).which,
                                 name)

    def test_every_verb_is_wired_to_something(self):
        for verb in ("record", "toggle", "start", "stop", "cancel", "ask",
                     "session", "transcribe", "meeting", "meetings", "history",
                     "config", "prompt", "devices", "models", "test-key",
                     "doctor", "shortcut", "status", "settings", "restart",
                     "quit", "help"):
            with self.subTest(verb=verb):
                argv = [verb]
                if verb == "transcribe":
                    argv.append("clip.mp3")
                opts = self.parse(*argv)
                self.assertEqual(opts.verb, verb)
                self.assertTrue(callable(opts.func))

    def test_the_old_spellings_still_parse(self):
        for verb in ("ask-cancel", "ask-reset", "meeting-cancel"):
            with self.subTest(verb=verb):
                self.assertEqual(self.parse(verb).verb, verb)

    def test_a_flag_typed_before_the_verb_survives(self):
        self.assertTrue(self.parse("--json", "status").json)

    def test_a_flag_typed_after_the_verb_works_too(self):
        self.assertTrue(self.parse("status", "--json").json)

    def test_a_flag_before_the_verb_is_not_overwritten_by_the_default(self):
        opts = self.parse("--quiet", "record")
        self.assertTrue(opts.quiet)

    def test_a_command_to_the_agent_is_taken_as_written(self):
        opts = self.parse("ask", "book", "it", "for", "Thursday")
        self.assertEqual(opts.text, ["book", "it", "for", "Thursday"])

    def test_a_command_with_no_text_is_a_recording(self):
        self.assertEqual(self.parse("ask").text, [])

    def test_the_subcommands_of_a_group(self):
        self.assertEqual(self.parse("config", "get", "cleanup_model").key,
                         "cleanup_model")
        self.assertEqual(self.parse("history", "list", "--limit", "5").limit, 5)
        self.assertEqual(self.parse("meetings", "show", "3").which, "3")

    def test_a_group_with_no_subcommand_asks_for_one(self):
        with captured():
            self.assertEqual(self.parse("config").func(Options()), 2)

    def test_the_three_way_flags_start_out_undecided(self):
        """--cleanup and --no-cleanup both given as nothing means the setting."""
        opts = self.parse("transcribe", "clip.mp3")
        self.assertIsNone(opts.cleanup)
        self.assertIsNone(opts.timestamps)
        self.assertFalse(self.parse("transcribe", "clip.mp3", "--no-cleanup").cleanup)
        self.assertTrue(self.parse("transcribe", "clip.mp3", "--cleanup").cleanup)

    def test_a_setting_that_is_not_a_choice_is_refused(self):
        with self.assertRaises(SystemExit), captured():
            self.parse("models", "--provider", "ollama")

    def test_a_flag_that_falls_back_to_the_setting(self):
        self.assertIs(cli._pick(None, True), True)
        self.assertIs(cli._pick(False, True), False)


class ConfigCommands(DikteTest):
    def run_cmd(self, func, **values):
        with captured() as (out, err):
            code = func(Options(**values))
        return code, out.getvalue(), err.getvalue()

    def test_reading_a_setting(self):
        code, out, _ = self.run_cmd(cli.cmd_config_get, key="cleanup_model")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), cfg.DEFAULTS["cleanup_model"])

    def test_reading_a_setting_that_is_not_a_string(self):
        _, out, _ = self.run_cmd(cli.cmd_config_get, key="history_limit")
        self.assertEqual(out.strip(), str(cfg.DEFAULTS["history_limit"]))

    def test_a_setting_nobody_has(self):
        code, _, err = self.run_cmd(cli.cmd_config_get, key="no_such_setting")
        self.assertEqual(code, 2)
        self.assertIn("unknown setting", err)

    def test_writing_a_setting_reaches_the_file(self):
        with mock.patch.object(ipc, "send"):
            code, _, _ = self.run_cmd(cli.cmd_config_set, key="cleanup_model",
                                      value="some/model")
        self.assertEqual(code, 0)
        self.assertEqual(cfg.Config()["cleanup_model"], "some/model")

    def test_a_running_instance_is_told_to_read_it_back(self):
        """It would otherwise write its own copy back over the change."""
        with mock.patch.object(ipc, "send") as send:
            self.run_cmd(cli.cmd_config_set, key="cleanup_model", value="some/model")
        send.assert_called_once_with("reload")

    def test_writing_a_boolean(self):
        with mock.patch.object(ipc, "send"):
            self.run_cmd(cli.cmd_config_set, key="cleanup_enabled", value="off")
        self.assertIs(cfg.Config()["cleanup_enabled"], False)

    def test_a_value_of_the_wrong_type(self):
        with mock.patch.object(ipc, "send"):
            code, _, err = self.run_cmd(cli.cmd_config_set,
                                        key="history_limit", value="lots")
        self.assertEqual(code, 2)
        # A number says only what could not be converted; a boolean names the
        # setting as well, because "true or false" needs the context.
        self.assertIn("lots", err)

    def test_a_key_is_masked_when_it_is_written_back(self):
        with mock.patch.object(ipc, "send"):
            _, out, _ = self.run_cmd(cli.cmd_config_set, key="openai_api_key",
                                     value="sk-abcdefgh1234")
        self.assertNotIn("sk-abcdefgh", out)
        self.assertIn("1234", out)

    def test_the_listing_masks_the_keys(self):
        with mock.patch.object(ipc, "send"):
            self.run_cmd(cli.cmd_config_set, key="openai_api_key",
                         value="sk-abcdefgh1234")
        _, out, _ = self.run_cmd(cli.cmd_config_list, reveal=False)
        self.assertNotIn("sk-abcdefgh", out)

    def test_a_key_belongs_to_whoever_asked_for_it_by_name(self):
        with mock.patch.object(ipc, "send"):
            self.run_cmd(cli.cmd_config_set, key="openai_api_key",
                         value="sk-abcdefgh1234")
        _, out, _ = self.run_cmd(cli.cmd_config_list, reveal=True)
        self.assertIn("sk-abcdefgh1234", out)

    def test_the_listing_shortens_a_long_value(self):
        with mock.patch.object(ipc, "send"):
            self.run_cmd(cli.cmd_config_set, key="cleanup_prompt", value="x" * 200)
        _, out, _ = self.run_cmd(cli.cmd_config_list, reveal=False)
        self.assertNotIn("x" * 100, out)

    def test_resetting_one_setting(self):
        with mock.patch.object(ipc, "send"):
            self.run_cmd(cli.cmd_config_set, key="cleanup_model", value="some/model")
            code, _, _ = self.run_cmd(cli.cmd_config_reset, key=["cleanup_model"],
                                      all=False)
        self.assertEqual(code, 0)
        self.assertEqual(cfg.Config()["cleanup_model"], cfg.DEFAULTS["cleanup_model"])

    def test_resetting_nothing_asks_what_to_reset(self):
        code, _, err = self.run_cmd(cli.cmd_config_reset, key=[], all=False)
        self.assertEqual(code, 2)
        self.assertIn("--all", err)

    def test_resetting_everything(self):
        with mock.patch.object(ipc, "send"):
            self.run_cmd(cli.cmd_config_set, key="cleanup_model", value="some/model")
            self.run_cmd(cli.cmd_config_reset, key=[], all=True)
        self.assertEqual(cfg.Config()["cleanup_model"], cfg.DEFAULTS["cleanup_model"])

    def test_where_things_are_stored(self):
        _, out, _ = self.run_cmd(cli.cmd_config_path, json=True)
        paths = json.loads(out)
        self.assertEqual(paths["config"], str(cfg.CONFIG_FILE))
        self.assertEqual(paths["history"], str(cfg.HISTORY_FILE))

    def test_the_prompt_a_run_would_really_send(self):
        _, out, _ = self.run_cmd(cli.cmd_prompt, which="cleanup")
        self.assertEqual(out.strip(), cfg.CLEANUP_PROMPT_EN.strip())

    def test_all_four_prompts_at_once(self):
        _, out, _ = self.run_cmd(cli.cmd_prompt, which=None, json=True)
        self.assertEqual(set(json.loads(out)["prompts"]),
                         {"cleanup", "subtitles", "meeting", "agent"})


class ShortcutStatus(DikteTest):
    """Where the answer comes from, which is not one place on every system.

    macOS keeps no shortcut registry: a combination is held by the running
    process and by nothing else, so a command line that reads its own idea of
    "installed" reports every shortcut as missing while all of them work.
    """

    def run_cmd(self, func, **values):
        with captured() as (out, err):
            code = func(Options(**values))
        return code, out.getvalue(), err.getvalue()

    def status(self, reply):
        with mock.patch.object(ipc, "send", return_value=reply) as send:
            code, out, _ = self.run_cmd(cli.cmd_shortcut, shortcut="status",
                                        json=True)
        return code, json.loads(out), send

    def test_what_the_running_instance_holds_is_what_is_reported(self):
        code, answer, _ = self.status({
            "shortcuts": {"toggle": "Ctrl+Option+Space", "cancel": None,
                          "ask": None, "meeting": None},
            "listener": True,
        })
        self.assertEqual(code, 0)
        self.assertEqual(answer["shortcuts"]["toggle"]["registered"],
                         "Ctrl+Option+Space")
        self.assertIsNone(answer["shortcuts"]["cancel"]["registered"])
        self.assertIs(answer["listener"], True)

    def test_the_instance_is_the_one_asked(self):
        _, _, send = self.status({"shortcuts": {}, "listener": False})
        send.assert_called_once_with("status")

    def test_nothing_running_falls_back_to_what_this_process_can_read(self):
        """Which on Linux is the registry, and on macOS is nothing, correctly
        so, because there the keys really are gone with the process."""
        code, answer, _ = self.status(None)
        self.assertEqual(code, 0)
        for name, spec in hotkey.SHORTCUTS.items():
            with self.subTest(name=name):
                self.assertEqual(answer["shortcuts"][name]["registered"],
                                 hotkey.shortcut_status(spec.desktop_id))

    def test_the_configured_combination_is_reported_either_way(self):
        code, answer, _ = self.status(None)
        self.assertEqual(answer["shortcuts"]["toggle"]["configured"],
                         cfg.Config()["shortcut"])


class Providers(DikteTest):
    """The terminal reaches every provider the settings window does."""

    def run_cmd(self, func, **values):
        with captured() as (out, err):
            code = func(Options(**values))
        return code, out.getvalue(), err.getvalue()

    def test_a_provider_the_settings_window_offers_is_a_choice_here_too(self):
        parser = cli.build_parser()
        for provider in cfg.TRANSCRIBERS:
            with self.subTest(provider=provider):
                opts = parser.parse_args(["models", "--provider", provider])
                self.assertEqual(opts.provider, provider)
                self.assertEqual(parser.parse_args(["test-key", provider]).which,
                                 provider)

    def test_the_model_list_is_read_from_the_chosen_provider(self):
        self.write_config({"groq_api_key": "gsk-test"})
        with fake_urlopen({"data": [{"id": "whisper-large-v3"}]}) as calls:
            code, out, _ = self.run_cmd(cli.cmd_models, provider="groq",
                                        transcription=False)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0].full_url, "https://api.groq.com/openai/v1/models")
        self.assertEqual(out.strip(), "whisper-large-v3")

    def test_a_key_that_is_not_there_is_reported_under_its_own_name(self):
        code, out, _ = self.run_cmd(cli.cmd_test_key, which="groq")
        self.assertEqual(code, 1)
        self.assertIn("groq", out)
        self.assertIn("Groq", out)


class Doctor(DikteTest):
    """One pass over everything the settings window checks behind its buttons."""

    def run_doctor(self, as_json=True, **settings):
        self.write_config(settings)
        with mock.patch.object(ipc, "send", return_value=None), \
                captured() as (out, _err):
            cli.cmd_doctor(Options(json=as_json))
        return json.loads(out.getvalue()) if as_json else out.getvalue()

    def test_cleanup_on_openrouter_is_a_question_about_the_key(self):
        reply = self.run_doctor(cleanup_model="some/model")
        self.assertEqual(reply["cleanup"]["provider"], "openrouter")
        self.assertEqual(reply["cleanup"]["model"], "some/model")
        self.assertIn("OpenRouter key, cleaning up on some/model",
                      self.run_doctor(as_json=False, cleanup_model="some/model"))

    def test_cleanup_on_a_cli_is_a_question_about_the_program(self):
        reply = self.run_doctor(cleanup_provider="codex",
                                cleanup_codex_model="gpt-5.4")
        self.assertEqual(reply["cleanup"]["provider"], "codex")
        self.assertEqual(reply["cleanup"]["model"], "gpt-5.4")
        self.assertIn("codex", reply["programs"])
        self.assertIn("codex, cleaning up on gpt-5.4",
                      self.run_doctor(as_json=False, cleanup_provider="codex",
                                      cleanup_codex_model="gpt-5.4"))


class Finding(DikteTest):
    def test_no_history_at_all(self):
        self.assertIsNone(cli._find_history("last"))

    def test_the_newest_entry(self):
        for text in ("first", "second"):
            cfg.append_history({"ts": "now", "text": text})
        self.assertEqual(cli._find_history("last")["text"], "second")
        self.assertEqual(cli._find_history("1")["text"], "second")
        self.assertEqual(cli._find_history("2")["text"], "first")

    def test_counting_past_the_end(self):
        cfg.append_history({"ts": "now", "text": "only one"})
        self.assertIsNone(cli._find_history("2"))
        self.assertIsNone(cli._find_history("0"))

    def test_something_that_is_not_a_number(self):
        cfg.append_history({"ts": "now", "text": "only one"})
        self.assertIsNone(cli._find_history("yesterday"))

    def test_a_meeting_by_its_stem(self):
        for base in ("20260801-100000", "20260802-110000"):
            cfg.save_meeting({"base": base, "status": "done"})
        self.assertEqual(cli._find_meeting("20260801-100000")["base"],
                         "20260801-100000")

    def test_a_meeting_by_the_start_of_its_stem(self):
        for base in ("20260801-100000", "20260801-110000"):
            cfg.save_meeting({"base": base, "status": "done"})
        self.assertEqual(cli._find_meeting("20260801-1")["base"], "20260801-110000")

    def test_a_meeting_by_the_date_it_was_recorded(self):
        """A stem is all digits too, so a date must not be read as a position."""
        for base in ("20260801-100000", "20260801-140000", "20260802-110000"):
            cfg.save_meeting({"base": base, "status": "done"})
        self.assertEqual(cli._find_meeting("20260801")["base"], "20260801-140000")

    def test_a_meeting_by_position(self):
        for base in ("20260801-100000", "20260802-110000"):
            cfg.save_meeting({"base": base, "status": "done"})
        self.assertEqual(cli._find_meeting("1")["base"], "20260802-110000")
        self.assertEqual(cli._find_meeting("2")["base"], "20260801-100000")
        self.assertEqual(cli._find_meeting("last")["base"], "20260802-110000")

    def test_a_position_wins_while_there_are_that_many_meetings(self):
        """Counting back is what a small number has always meant, and a stem
        never starts with one: it starts with the year."""
        for base in ("20260801-100000", "20260802-110000"):
            cfg.save_meeting({"base": base, "status": "done"})
        self.assertEqual(cli._find_meeting("2")["base"], "20260801-100000")

    def test_counting_past_the_end_finds_nothing_rather_than_the_wrong_one(self):
        cfg.save_meeting({"base": "20260801-100000", "status": "done"})
        self.assertIsNone(cli._find_meeting("9"))
        self.assertIsNone(cli._find_meeting("0"))

    def test_a_meeting_nobody_recorded(self):
        cfg.save_meeting({"base": "20260801-100000", "status": "done"})
        self.assertIsNone(cli._find_meeting("20261231"))


class WithoutAnInstance(DikteTest):
    """Nothing is listening on the socket, which is three different things.

    A verb that can start the application does; one that asks for a state the
    application is already in succeeds; anything else fails with code 3.
    """

    def run_verb(self, argv):
        # launch_gui replaces this process with the application, so it never
        # comes back in real use and must not be allowed to here.
        with mock.patch.object(ipc, "send", return_value=None), \
                mock.patch.object(cli, "launch_gui") as launch, \
                captured() as (out, err):
            code = cli.run(argv)
        return code, out.getvalue(), err.getvalue(), launch

    def test_pressing_the_key_on_a_fresh_login_starts_it_recording(self):
        """What the KDE shortcut has always relied on."""
        _, _, _, launch = self.run_verb(["toggle"])
        launch.assert_called_once_with("toggle")

    def test_every_verb_that_opens_a_window_can_start_it(self):
        for verb in ("settings", "toggle", "ask", "meeting"):
            with self.subTest(verb=verb):
                self.assertTrue(self.run_verb([verb])[3].called)

    def test_a_verb_asked_to_wait_starts_nothing(self):
        """There would be no run to wait for; the process would just be replaced."""
        _, _, _, launch = self.run_verb(["toggle", "--wait"])
        launch.assert_not_called()

    def test_asking_it_to_stop_when_it_is_not_going_is_not_a_failure(self):
        for verb in ("cancel", "quit", "restart", "ask-reset"):
            with self.subTest(verb=verb):
                code, _, _, _ = self.run_verb([verb])
                self.assertEqual(code, 0)

    def test_anything_else_says_nothing_is_running(self):
        for argv in (["record"], ["start"]):
            with self.subTest(argv=argv):
                code, _, err, _ = self.run_verb(argv)
                self.assertEqual(code, cli.NOT_RUNNING)
                self.assertIn("not running", err)

    def test_status_answers_the_question_rather_than_failing_it(self):
        """"Is it running" has an answer when it is not, and it goes to stdout."""
        code, out, _, _ = self.run_verb(["status"])
        self.assertEqual(code, cli.NOT_RUNNING)
        self.assertIn("not running", out)

    def test_status_as_json_says_so_in_a_field(self):
        _, out, _, _ = self.run_verb(["--json", "status"])
        self.assertFalse(json.loads(out)["running"])

    def test_the_answer_says_so_in_json_too(self):
        code, out, _, _ = self.run_verb(["--json", "record"])
        self.assertEqual(code, cli.NOT_RUNNING)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["running"])

    def test_a_verb_that_needs_nothing_running_still_works(self):
        with captured() as (out, _):
            code = cli.run(["config", "get", "cleanup_model"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), cfg.DEFAULTS["cleanup_model"])


class Replies(DikteTest):
    """What the instance said, turned into output and an exit code."""

    def run_verb(self, argv, reply):
        with mock.patch.object(ipc, "send", return_value=reply), \
                captured() as (out, err):
            code = cli.run(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_dictation_prints_its_transcript(self):
        code, out, _ = self.run_verb(["stop", "--wait"],
                                     {"ok": True, "text": "Book it for Thursday."})
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "Book it for Thursday.")

    def test_a_dictation_that_failed(self):
        code, out, err = self.run_verb(["stop", "--wait"],
                                       {"ok": False, "error": "No speech detected"})
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("No speech", err)

    def test_a_warning_goes_to_stderr_beside_the_answer(self):
        code, out, err = self.run_verb(
            ["stop", "--wait"],
            {"ok": True, "text": "hello", "warning": "cleanup failed"})
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "hello")
        self.assertIn("cleanup failed", err)

    def test_a_verb_with_nothing_to_say_prints_nothing(self):
        code, out, _ = self.run_verb(["restart"], {"ok": True})
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_cancelling_something_that_is_not_running_is_not_a_failure(self):
        with mock.patch.object(ipc, "send", return_value={"ok": True, "legacy": True}), \
                captured():
            self.assertEqual(cli.run(["cancel"]), 0)


if __name__ == "__main__":
    unittest.main()
