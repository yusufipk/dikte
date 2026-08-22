"""Who cleans the transcript up, and what they are asked.

The CLIs are faked at subprocess.Popen: what the tests read is the argument
list each one is given, where the answer is picked up from, and what happens to
the chain when the program is missing, slow or unhappy. The OpenRouter path is
the one that was always there and is checked here only for still being taken.
"""

import os
import subprocess
import unittest
from unittest import mock

from dikte import api
from dikte import cleanup
from dikte import ggml
from tests.support import DikteTest, fake_urlopen, sent_json, url_error
from tests.test_api import FakeServer, chat_reply


def fake_cli(stdout="", code=0, stderr="", last_message=""):
    """Stand in for subprocess.Popen.

    _output hands the process a temporary file for each stream, so the fake
    writes into those, plus the file Codex would have written on its way out.
    """
    calls = []

    def popen(cmd, **kwargs):
        calls.append(cmd)
        kwargs["stdout"].write(stdout.encode("utf-8"))
        kwargs["stderr"].write(stderr.encode("utf-8"))
        if last_message and "-o" in cmd:
            with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as fh:
                fh.write(last_message)
        proc = mock.Mock()
        proc.returncode = code
        return proc

    return mock.patch.object(subprocess, "Popen", side_effect=popen), calls


class Provider(DikteTest):
    def test_the_default_is_still_openrouter(self):
        self.assertEqual(cleanup.provider(self.config()), "openrouter")

    def test_a_provider_this_version_does_not_have(self):
        self.assertEqual(
            cleanup.provider(self.config(cleanup_provider="ollama")), "openrouter")

    def test_each_one_is_recognised(self):
        for name in cleanup.PROVIDERS:
            with self.subTest(name=name):
                self.assertEqual(
                    cleanup.provider(self.config(cleanup_provider=name)), name)

    def test_what_each_one_runs(self):
        self.assertEqual(cleanup.executable("claude"), "claude")
        self.assertEqual(cleanup.executable("codex"), "codex")
        self.assertEqual(cleanup.executable("openrouter"), "")

    def test_the_model_named_in_the_history_is_the_one_that_did_it(self):
        self.assertEqual(cleanup.model(self.config(cleanup_model="some/model")),
                         "some/model")
        self.assertEqual(
            cleanup.model(self.config(cleanup_provider="claude")), "haiku")
        self.assertEqual(
            cleanup.model(self.config(cleanup_provider="claude",
                                      cleanup_claude_model="opus")), "opus")
        # Codex on its own default has no model id to report, only a name.
        self.assertEqual(
            cleanup.model(self.config(cleanup_provider="codex")), "codex")
        self.assertEqual(
            cleanup.model(self.config(cleanup_provider="codex",
                                      cleanup_codex_model="gpt-5.4")), "gpt-5.4")


class OpenRouter(DikteTest):
    def test_it_is_still_one_request_with_the_settings_as_they_were(self):
        conf = self.config(openrouter_api_key="sk-or-test",
                           cleanup_model="some/model", cleanup_reasoning="low")
        with mock.patch.object(api, "cleanup", return_value="Done.") as call:
            self.assertEqual(cleanup.run("uh, done", conf, "the rules"), "Done.")
        text, key, model, prompt = call.call_args.args
        self.assertEqual((text, key, model, prompt),
                         ("uh, done", "sk-or-test", "some/model", "the rules"))
        self.assertEqual(call.call_args.kwargs["reasoning"], "low")

    def test_no_cli_is_started_for_it(self):
        conf = self.config(openrouter_api_key="sk-or-test")
        patcher, calls = fake_cli(stdout="never")
        with patcher, mock.patch.object(api, "cleanup", return_value="Done."):
            cleanup.run("uh, done", conf, "the rules")
        self.assertEqual(calls, [])


class ClaudeCode(DikteTest):
    def setUp(self):
        super().setUp()
        self.conf = self.config(cleanup_provider="claude")
        self.patch_attr(cleanup.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run_cleanup(self, text="uh, book it", **kwargs):
        patcher, calls = fake_cli(**kwargs)
        with patcher:
            answer = cleanup.run(text, self.conf, "the rules")
        return answer, calls[0]

    def test_the_transcript_goes_in_fenced_and_the_rules_go_in_as_the_prompt(self):
        answer, cmd = self.run_cleanup(stdout="Book it.\n")
        self.assertEqual(answer, "Book it.")
        self.assertEqual(cmd[0], "claude")
        self.assertIn("<transcript>\nuh, book it\n</transcript>", cmd)
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "the rules")
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")

    def test_it_is_given_nothing_to_run_and_nothing_to_remember(self):
        _, cmd = self.run_cleanup(stdout="Book it.")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--no-session-persistence", cmd)

    def test_the_thinking_setting_is_carried_over_in_its_own_words(self):
        self.conf["cleanup_reasoning"] = "none"
        _, cmd = self.run_cleanup(stdout="Book it.")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_no_thinking_setting_means_no_flag(self):
        _, cmd = self.run_cleanup(stdout="Book it.")
        self.assertNotIn("--effort", cmd)

    def test_a_model_of_your_own(self):
        self.conf["cleanup_claude_model"] = "claude-sonnet-5"
        _, cmd = self.run_cleanup(stdout="Book it.")
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5")

    def test_an_answer_of_nothing_is_a_failure_rather_than_an_empty_paste(self):
        with self.assertRaises(cleanup.CleanupError):
            self.run_cleanup(stdout="   \n")

    def test_the_last_line_of_the_complaint_is_what_gets_shown(self):
        with self.assertRaises(cleanup.CleanupError) as caught:
            self.run_cleanup(code=1, stderr="a warning\nout of credit\n")
        self.assertEqual(str(caught.exception), "out of credit")

    def test_a_failure_is_the_same_kind_the_chain_already_catches(self):
        # worker, the file transcriber and the meeting all keep the raw
        # transcript when an ApiError comes out of here.
        self.assertTrue(issubclass(cleanup.CleanupError, api.ApiError))

    def test_a_program_that_is_not_installed_says_so_before_running_anything(self):
        self.patch_attr(cleanup.shutil, "which", lambda name: "")
        with self.assertRaises(cleanup.CleanupError) as caught:
            self.run_cleanup(stdout="Book it.")
        self.assertIn("claude", str(caught.exception))

    def test_a_run_that_never_ends_is_killed_with_its_whole_tree(self):
        def popen(cmd, **kwargs):
            proc = mock.Mock()
            proc.wait.side_effect = subprocess.TimeoutExpired(cmd, 180)
            return proc

        with mock.patch.object(subprocess, "Popen", side_effect=popen), \
                mock.patch.object(cleanup.assistant, "kill_tree") as kill:
            with self.assertRaises(cleanup.CleanupError) as caught:
                cleanup.run("uh, book it", self.conf, "the rules")
        self.assertIn("180", str(caught.exception))
        kill.assert_called_once()


class Codex(DikteTest):
    def setUp(self):
        super().setUp()
        self.conf = self.config(cleanup_provider="codex")
        self.patch_attr(cleanup.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run_cleanup(self, text="uh, book it", **kwargs):
        patcher, calls = fake_cli(**kwargs)
        with patcher:
            answer = cleanup.run(text, self.conf, "the rules")
        return answer, calls[0]

    def test_the_rules_ride_in_front_of_the_transcript(self):
        answer, cmd = self.run_cleanup(last_message="Book it.\n")
        self.assertEqual(answer, "Book it.")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[-1],
                         "the rules\n\n---\n\n<transcript>\nuh, book it\n</transcript>")

    def test_the_answer_is_read_from_the_file_rather_than_the_noise_on_stdout(self):
        answer, _ = self.run_cleanup(
            stdout="workdir: /home\nmodel: gpt-5.4\ntokens used 400\n",
            last_message="Book it.",
        )
        self.assertEqual(answer, "Book it.")

    def test_that_file_does_not_stay_behind(self):
        _, cmd = self.run_cleanup(last_message="Book it.")
        self.assertFalse(os.path.exists(cmd[cmd.index("-o") + 1]))

    def test_it_may_read_but_not_write_and_has_nobody_to_ask(self):
        _, cmd = self.run_cleanup(last_message="Book it.")
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn('approval_policy="never"', cmd)
        self.assertIn("--ephemeral", cmd)

    def test_the_model_is_left_alone_until_one_is_typed_in(self):
        _, cmd = self.run_cleanup(last_message="Book it.")
        self.assertNotIn("-m", cmd)
        self.conf["cleanup_codex_model"] = "gpt-5.4"
        _, cmd = self.run_cleanup(last_message="Book it.")
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.4")

    def test_the_thinking_setting_lands_on_the_nearest_rung_codex_has(self):
        self.conf["cleanup_reasoning"] = "xhigh"
        _, cmd = self.run_cleanup(last_message="Book it.")
        self.assertIn('model_reasoning_effort="high"', cmd)

    def test_an_answer_of_nothing(self):
        with self.assertRaises(cleanup.CleanupError):
            self.run_cleanup(stdout="tokens used 400", last_message="")


if __name__ == "__main__":
    unittest.main()


class Here(DikteTest):
    """llama.cpp, answering the request OpenRouter answers."""

    def setUp(self):
        super().setUp()
        self.conf = self.config(cleanup_provider="local",
                                local_llm_model="gemma.gguf")
        self.server = FakeServer()
        self.patch_attr(ggml, "llm", self.server)

    def test_the_address_comes_from_the_server_it_starts(self):
        with fake_urlopen(chat_reply("Done.")) as calls:
            self.assertEqual(cleanup.run("uh, done", self.conf, "the rules"),
                             "Done.")
        self.assertEqual(self.server.starts, 1)
        self.assertEqual(calls[0].full_url,
                         "http://127.0.0.1:9999/v1/chat/completions")

    def test_no_key_is_wanted_and_none_is_sent(self):
        with fake_urlopen(chat_reply("Done.")) as calls:
            cleanup.run("uh, done", self.conf, "the rules")
        self.assertNotIn("Authorization", calls[0].headers)

    def test_thinking_is_turned_off_in_the_words_llama_cpp_uses(self):
        with fake_urlopen(chat_reply("Done.")) as calls:
            cleanup.run("uh, done", self.conf, "the rules")
        self.assertEqual(sent_json(calls[0])["chat_template_kwargs"],
                         {"enable_thinking": False})

    def test_the_models_own_default_asks_for_nothing(self):
        self.conf["local_llm_reasoning"] = ""
        with fake_urlopen(chat_reply("Done.")) as calls:
            cleanup.run("uh, done", self.conf, "the rules")
        self.assertNotIn("chat_template_kwargs", sent_json(calls[0]))

    def test_a_reply_longer_than_the_transcript_is_cut_off(self):
        # A small model will repeat the transcript until the context is full,
        # and every one of those tokens is a second of somebody waiting.
        with fake_urlopen(chat_reply("Done.")) as calls:
            cleanup.run("x" * 4000, self.conf, "the rules")
        self.assertEqual(sent_json(calls[0])["max_tokens"], 4000)

    def test_a_short_dictation_still_gets_room_to_answer(self):
        with fake_urlopen(chat_reply("Done.")) as calls:
            cleanup.run("uh, done", self.conf, "the rules")
        self.assertEqual(sent_json(calls[0])["max_tokens"], 512)

    def test_a_reply_that_was_all_thinking_names_the_setting_that_fixes_it(self):
        reply = {"choices": [{"message": {"content": "", "reasoning": "hmm"}}]}
        with fake_urlopen(reply), self.assertRaises(api.ApiError) as caught:
            cleanup.run("uh, done", self.conf, "the rules")
        self.assertIn("Thinking", str(caught.exception))

    def test_a_server_that_will_not_start_is_the_error_shown(self):
        self.patch_attr(ggml, "llm", FakeServer(fails="llama.cpp is not installed"))
        with self.assertRaises(api.ApiError) as caught:
            cleanup.run("uh, done", self.conf, "the rules")
        self.assertIn("llama.cpp", str(caught.exception))

    def test_a_server_that_dies_mid_request_says_what_it_printed(self):
        self.patch_attr(ggml, "llm", FakeServer(log="out of memory"))
        with fake_urlopen(url_error("connection reset")):
            with self.assertRaises(api.ApiError) as caught:
                cleanup.run("uh, done", self.conf, "the rules")
        self.assertIn("out of memory", str(caught.exception))

    def test_no_cli_is_started_for_it(self):
        patcher, calls = fake_cli(stdout="never")
        with patcher, fake_urlopen(chat_reply("Done.")):
            cleanup.run("uh, done", self.conf, "the rules")
        self.assertEqual(calls, [])

