"""Handing a dictation to an agent.

Three providers behind one setting, so most of this is about the command line
each of them is given and about the conversation carried between dictations. The
CLIs are faked at subprocess.Popen: what the tests read is the argument list and
what the stream of JSON events is turned into.
"""

import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from dikte import assistant
from tests.support import (DikteTest, FakeCompleted, fake_urlopen,
                           only_these_tools)


class FakeCli:
    """A CLI that prints the given events and exits.

    Its stderr is not modelled: _stream hands the process a temporary file for
    that, and a mocked Popen leaves the file empty, which is what a quiet CLI
    writes anyway.
    """

    def __init__(self, events=(), code=0, noise=()):
        lines = list(noise) + [json.dumps(event) for event in events]
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.returncode = code
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True


class WedgedCli:
    """A CLI whose stdout produces nothing, the way a hung process's does.

    Iterating its stdout blocks until the kill arrives, because that is what
    reading a silent pipe does: only the process ending closes the stream.
    """

    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.released = threading.Event()
        self.stdout = self

    def __iter__(self):
        return self

    def __next__(self):
        # The 5 second cap is a safety net for the test itself; the kill is
        # what is supposed to end the wait.
        self.released.wait(timeout=5)
        raise StopIteration

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def close(self):
        pass


class Provider(DikteTest):
    def test_the_default(self):
        self.assertEqual(assistant.provider(self.config()), "claude")

    def test_a_provider_this_version_does_not_have(self):
        self.assertEqual(
            assistant.provider(self.config(assistant_provider="ollama")), "claude")

    def test_each_one_is_recognised(self):
        for name in assistant.PROVIDERS:
            with self.subTest(name=name):
                self.assertEqual(
                    assistant.provider(self.config(assistant_provider=name)), name)

    def test_what_each_one_runs(self):
        self.assertEqual(assistant.executable("claude"), "claude")
        self.assertEqual(assistant.executable("codex"), "codex")
        self.assertEqual(assistant.executable("agy"), "agy")
        self.assertEqual(assistant.executable("openrouter"), "")
        self.assertEqual(assistant.executable("opencode"), "")
        self.assertEqual(assistant.executable("requesty"), "")

    def test_the_model_recorded_is_the_one_that_answered(self):
        """The history used to write Claude's setting whoever had answered."""
        self.assertEqual(assistant.model(self.config()), "sonnet")
        self.assertEqual(
            assistant.model(self.config(assistant_provider="codex")), "codex")
        self.assertEqual(
            assistant.model(self.config(assistant_provider="agy")), "agy")
        self.assertEqual(
            assistant.model(self.config(assistant_provider="agy",
                                        assistant_agy_model="gemini-3.1-pro-low")),
            "gemini-3.1-pro-low")
        self.assertEqual(
            assistant.model(self.config(assistant_provider="openrouter")),
            "google/gemini-3.5-flash")

    def test_what_each_one_is_called(self):
        self.assertEqual(assistant.display_name(self.config()), "Claude")
        for name, called in (("codex", "Codex"), ("agy", "Antigravity"),
                             ("openrouter", "OpenRouter"),
                             ("opencode", "OpenCode Go"),
                             ("requesty", "Requesty")):
            with self.subTest(name=name):
                self.assertEqual(
                    assistant.display_name(self.config(assistant_provider=name)),
                    called)

    def test_every_provider_has_a_name_to_be_called_by(self):
        """_conclude writes its errors in it, so a gap here is a bare id."""
        self.assertEqual(set(assistant.SERVICES), set(assistant.PROVIDERS))


class Effort(unittest.TestCase):
    """One scale, offered once; a rung a provider lacks lands on its nearest."""

    def test_the_scales_cover_the_same_settings(self):
        self.assertEqual(set(assistant.CLAUDE_EFFORT), set(assistant.CODEX_EFFORT))
        self.assertEqual(set(assistant.CLAUDE_EFFORT), set(assistant.AGY_EFFORT))

    def test_neither_codex_nor_agy_has_a_rung_above_high(self):
        for scale in (assistant.CODEX_EFFORT, assistant.AGY_EFFORT):
            self.assertEqual(scale["xhigh"], "high")
            self.assertEqual(scale["max"], "high")

    def test_none_of_them_asks_for_a_rung_below_low(self):
        # Claude has none; Codex has one, but calls it "minimal" on the older
        # models and "none" on the newer ones, and refuses the wrong word; agy
        # has three rungs and no word for off at all.
        for scale in (assistant.CLAUDE_EFFORT, assistant.CODEX_EFFORT,
                      assistant.AGY_EFFORT):
            self.assertEqual(scale["none"], "low")
            self.assertEqual(scale["minimal"], "low")

    def test_an_empty_setting_asks_for_nothing(self):
        for scale in (assistant.CLAUDE_EFFORT, assistant.CODEX_EFFORT,
                      assistant.AGY_EFFORT):
            self.assertEqual(scale.get("", ""), "")


class Session(DikteTest):
    def test_nothing_stored_yet(self):
        self.assertEqual(assistant.read_session("claude", 1800), "")
        self.assertEqual(assistant.read_messages("openrouter", 1800), [])
        self.assertEqual(assistant.stored_provider(), "")
        self.assertIsNone(assistant.session_age())

    def test_an_id_is_written_and_read_back(self):
        assistant.write_session("claude", "abc-123")
        self.assertEqual(assistant.read_session("claude", 1800), "abc-123")
        self.assertEqual(assistant.stored_provider(), "claude")

    def test_nobody_picks_up_another_provider_s_thread(self):
        assistant.write_session("claude", "abc-123")
        self.assertEqual(assistant.read_session("codex", 1800), "")

    def test_a_conversation_that_has_sat_unused_is_dropped(self):
        assistant.write_session("claude", "abc-123")
        with mock.patch.object(time, "time", return_value=time.time() + 3600):
            self.assertEqual(assistant.read_session("claude", 1800), "")

    def test_a_session_that_never_expires(self):
        assistant.write_session("claude", "abc-123")
        with mock.patch.object(time, "time", return_value=time.time() + 10 ** 6):
            self.assertEqual(assistant.read_session("claude", 0), "abc-123")

    def test_the_messages_of_the_provider_that_keeps_none(self):
        messages = [{"role": "user", "content": "hi"}]
        assistant.write_session("openrouter", messages=messages)
        self.assertEqual(assistant.read_messages("openrouter", 1800), messages)

    def test_the_history_window_ends_somewhere(self):
        messages = [{"role": "user", "content": str(index)} for index in range(50)]
        assistant.write_session("openrouter", messages=messages)
        stored = assistant.read_messages("openrouter", 1800)
        self.assertEqual(len(stored), assistant.MAX_HISTORY)
        self.assertEqual(stored[-1]["content"], "49")

    def test_the_age_of_the_conversation(self):
        assistant.write_session("claude", "abc-123")
        self.assertLess(assistant.session_age(), 5)

    def test_a_row_with_neither_an_id_nor_messages_has_no_age(self):
        assistant.write_session("claude", "")
        self.assertIsNone(assistant.session_age())

    def test_clearing(self):
        assistant.write_session("claude", "abc-123")
        assistant.clear_session()
        self.assertEqual(assistant.read_session("claude", 1800), "")

    def test_clearing_one_that_is_not_there(self):
        assistant.clear_session()   # must not raise

    def test_a_session_file_that_is_not_json(self):
        assistant.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        assistant.SESSION_FILE.write_text("{oh dear", encoding="utf-8")
        self.assertEqual(assistant.read_session("claude", 1800), "")
        self.assertEqual(assistant.stored_provider(), "")
        self.assertIsNone(assistant.session_age())


class WorkingDir(DikteTest):
    def test_the_home_directory_by_default(self):
        self.assertEqual(assistant.working_dir(self.config()),
                         os.path.expanduser("~"))

    def test_a_directory_of_your_own(self):
        conf = self.config(assistant_dir=self.root)
        self.assertEqual(assistant.working_dir(conf), self.root)

    def test_a_tilde_is_expanded(self):
        conf = self.config(assistant_dir="~")
        self.assertEqual(assistant.working_dir(conf), os.path.expanduser("~"))

    def test_a_directory_that_is_not_there_falls_back(self):
        conf = self.config(assistant_dir="/no/such/place")
        self.assertEqual(assistant.working_dir(conf), os.path.expanduser("~"))


class Labels(DikteTest):
    def test_a_tool_the_table_knows(self):
        self.assertEqual(assistant._claude_label({"name": "Bash"}),
                         "Running a command…")

    def test_a_tool_arriving_from_an_mcp_server_is_named_by_its_server(self):
        self.assertIn("gmail", assistant._claude_label({"name": "mcp__gmail__send"}))

    def test_a_skill_is_named_by_the_skill(self):
        label = assistant._claude_label(
            {"name": "Skill", "input": {"skill": "calendar"}})
        self.assertIn("calendar", label)

    def test_a_tool_nobody_wrote_a_line_for(self):
        self.assertIn("SomeNewTool",
                      assistant._claude_label({"name": "SomeNewTool"}))

    def test_a_tool_with_no_name_at_all(self):
        self.assertTrue(assistant._claude_label({}))

    def test_the_codex_table(self):
        self.assertEqual(assistant._codex_label({"type": "command_execution"}),
                         "Running a command…")

    def test_a_codex_mcp_call(self):
        self.assertIn("gmail", assistant._codex_label(
            {"type": "mcp_tool_call", "server": "gmail"}))

    def test_a_codex_item_nobody_listed(self):
        self.assertIn("something_new",
                      assistant._codex_label({"type": "something_new"}))


class Denials(DikteTest):
    def test_nothing_was_denied(self):
        self.assertEqual(assistant._denial_warning({}), "")
        self.assertEqual(assistant._denial_warning({"permission_denials": []}), "")

    def test_a_denied_tool_is_named(self):
        warning = assistant._denial_warning(
            {"permission_denials": [{"tool_name": "Bash"}]})
        self.assertIn("Bash", warning)

    def test_the_same_tool_denied_twice_is_named_once(self):
        warning = assistant._denial_warning({"permission_denials": [
            {"tool_name": "Bash"}, {"tool_name": "Bash"}, {"tool_name": "Write"}]})
        self.assertEqual(warning.count("Bash"), 1)
        self.assertIn("Write", warning)


class LastLine(unittest.TestCase):
    def test_the_last_line_is_the_one_worth_showing(self):
        self.assertEqual(assistant.last_line("warning\n\nreal error\n"),
                         "real error")
        self.assertEqual(assistant.last_line(""), "")
        self.assertEqual(assistant.last_line(None), "")


class Conclude(DikteTest):
    def found(self, **changes):
        row = {"answer": "", "warning": "", "session": "", "failure": ""}
        row.update(changes)
        return row

    def test_an_answer_and_its_session(self):
        answer, warning = assistant._conclude(
            self.found(answer="done", session="abc"), 0, "", "", "claude")
        self.assertEqual(answer, "done")
        self.assertEqual(warning, "")
        self.assertEqual(assistant.read_session("claude", 1800), "abc")

    def test_each_one_stores_under_its_own_name(self):
        for name, session in (("codex", "t-1"), ("agy", "c-9")):
            with self.subTest(name=name):
                assistant._conclude(self.found(answer="done", session=session),
                                    0, "", "", name)
                self.assertEqual(assistant.read_session(name, 1800), session)

    def test_the_error_is_written_in_the_provider_s_own_name(self):
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant._conclude(self.found(), 1, "", "", "agy")
        self.assertIn("Antigravity", str(caught.exception))

    def test_a_non_zero_exit_with_nothing_to_show_for_it(self):
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant._conclude(self.found(), 1, "it all went wrong\n", "", "claude")
        self.assertIn("it all went wrong", str(caught.exception))

    def test_a_session_that_is_gone_is_raised_apart(self):
        with self.assertRaises(assistant._SessionGone):
            assistant._conclude(self.found(), 1, "session abc not found",
                                "abc", "claude")

    def test_the_recovery_no_longer_hangs_on_the_words_the_cli_chose(self):
        # The complaint used to be matched by substring, which a CLI update or
        # another language broke. A resumed run that died with nothing to show
        # is now enough on its own.
        for stderr in ("Oturum bulunamadı", "something else entirely", ""):
            with self.subTest(stderr=stderr):
                with self.assertRaises(assistant._SessionGone):
                    assistant._conclude(self.found(), 1, stderr, "abc", "Claude")

    def test_api_trouble_on_a_resumed_run_is_not_blamed_on_the_session(self):
        # A fresh session cannot cure a spent quota, a signed-out CLI or a dead
        # network: the retry would fail the same way after a second wait, and
        # the user would lose the conversation thread on top.
        for stderr in ("Rate limit exceeded",
                       "You are not logged in. Please run /login.",
                       "API Error: 401 Unauthorized",
                       "fetch failed: ECONNREFUSED 127.0.0.1"):
            with self.subTest(stderr=stderr):
                with self.assertRaises(assistant.AssistantError) as caught:
                    assistant._conclude(self.found(), 1, stderr, "abc", "Claude")
                self.assertIn(stderr, str(caught.exception))

    def test_a_session_that_is_gone_only_matters_when_one_was_resumed(self):
        with self.assertRaises(assistant.AssistantError):
            assistant._conclude(self.found(), 1, "session abc not found",
                                "", "claude")

    def test_an_answer_survives_a_non_zero_exit(self):
        answer, _ = assistant._conclude(self.found(answer="done"), 1, "noise",
                                        "", "claude")
        self.assertEqual(answer, "done")

    def test_an_answer_on_a_resumed_session_is_kept_rather_than_retried(self):
        answer, _ = assistant._conclude(self.found(answer="done"), 1, "noise",
                                        "abc", "Claude")
        self.assertEqual(answer, "done")

    def test_a_reported_failure_with_no_answer(self):
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant._conclude(self.found(failure="the model refused"), 0, "",
                                "", "claude")
        self.assertIn("refused", str(caught.exception))

    def test_a_run_that_said_nothing_at_all(self):
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant._conclude(self.found(), 0, "", "", "codex")
        self.assertIn("Codex", str(caught.exception))


class Stream(DikteTest):
    def test_a_cli_that_floods_stderr_still_finishes(self):
        # A real subprocess, because the wedge being tested is real plumbing:
        # with stderr on a pipe nobody drains, 200 KB fills the pipe's buffer,
        # the child blocks writing it, and the run hangs until the watchdog
        # timeout. With stderr on a file the run completes at once.
        script = (
            "import sys\n"
            "sys.stderr.write('x' * 200000)\n"
            "sys.stderr.flush()\n"
            "print('{\"type\": \"result\", \"result\": \"done\"}')\n"
        )
        conf = self.config(assistant_timeout=15)
        events = []
        code, stderr = assistant._stream(
            [sys.executable, "-c", script], conf, events.append, None)
        self.assertEqual(code, 0)
        self.assertEqual(len(stderr), 200000)
        self.assertEqual(events[-1]["result"], "done")

    def test_the_watchdog_takes_the_whole_tree_down_on_timeout(self):
        proc = WedgedCli()

        def killed(target):
            # What the real kill does, as far as _stream can see: the process
            # ends, and its closing stream releases the blocked read.
            target.returncode = 1
            target.released.set()

        conf = self.config(assistant_timeout=0)
        with mock.patch.object(subprocess, "Popen", return_value=proc), \
                mock.patch.object(assistant, "kill_tree",
                                  side_effect=killed) as kill:
            with self.assertRaises(assistant.AssistantError) as caught:
                assistant._stream(["claude"], conf, lambda event: None, None)
        kill.assert_called_once_with(proc)
        self.assertIn("did not finish", str(caught.exception))


class AskClaude(DikteTest):
    def run_ask(self, conf=None, events=None, code=0, noise=(),
                session=""):
        conf = conf or self.config()
        proc = FakeCli(events or [
            {"type": "system", "subtype": "init", "session_id": "abc"},
            {"type": "result", "session_id": "abc", "result": "  done  "},
        ], code=code, noise=noise)
        stages = []
        with only_these_tools("claude", "codex"), \
                mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            result = assistant._ask_claude(
                "book it", conf, session, stages.append, None)
        return result, popen.call_args.args[0], stages

    def test_the_answer_comes_back_stripped(self):
        (answer, warning), _, _ = self.run_ask()
        self.assertEqual(answer, "done")
        self.assertEqual(warning, "")

    def test_the_prompt_goes_in_as_one_argument(self):
        _, cmd, _ = self.run_ask()
        self.assertEqual(cmd[:3], ["claude", "-p", "book it"])

    def test_the_stream_is_asked_for_so_progress_can_be_shown(self):
        _, cmd, _ = self.run_ask()
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)

    def test_the_model_and_the_permission_mode_are_passed_on(self):
        conf = self.config(assistant_model="opus",
                           assistant_permission_mode="plan")
        _, cmd, _ = self.run_ask(conf)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")

    def test_the_instruction_rides_along_as_a_system_prompt(self):
        conf = self.config()
        _, cmd, _ = self.run_ask(conf)
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1],
                         conf.assistant_prompt())

    def test_no_effort_asked_for_means_no_flag(self):
        _, cmd, _ = self.run_ask()
        self.assertNotIn("--effort", cmd)

    def test_an_effort_is_translated_to_the_provider_s_vocabulary(self):
        _, cmd, _ = self.run_ask(self.config(assistant_reasoning="minimal"))
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_a_conversation_is_resumed(self):
        _, cmd, _ = self.run_ask(session="abc-123")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc-123")

    def test_a_fresh_conversation_resumes_nothing(self):
        _, cmd, _ = self.run_ask()
        self.assertNotIn("--resume", cmd)

    def test_every_tool_it_picks_up_is_named_in_the_corner(self):
        _, _, stages = self.run_ask(events=[
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "WebSearch"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"}]}},
            {"type": "result", "result": "done"},
        ])
        self.assertEqual(stages, ["Searching the web…", "Running a command…"])

    def test_a_denied_tool_comes_back_as_a_warning_beside_the_answer(self):
        (answer, warning), _, _ = self.run_ask(events=[
            {"type": "result", "result": "I could not do that.",
             "permission_denials": [{"tool_name": "Bash"}]},
        ])
        self.assertEqual(answer, "I could not do that.")
        self.assertIn("Bash", warning)

    def test_a_run_that_ended_in_an_error(self):
        with self.assertRaises(assistant.AssistantError):
            self.run_ask(events=[{"type": "result", "is_error": True,
                                  "result": "rate limited"}])

    def test_the_odd_unstructured_line_among_the_json(self):
        (answer, _), _, _ = self.run_ask(noise=["Loading…", "not json at all"])
        self.assertEqual(answer, "done")

    def test_a_json_line_that_is_not_an_object(self):
        proc = FakeCli(code=0)
        proc.stdout = io.StringIO('{"type": "result", "result": "done"}\n[1,2]\n')
        with only_these_tools("claude"), \
                mock.patch.object(subprocess, "Popen", return_value=proc):
            answer, _ = assistant._ask_claude("hi", self.config(), "", None, None)
        self.assertEqual(answer, "done")


class AskCodex(DikteTest):
    def run_ask(self, conf=None, events=None, session=""):
        conf = conf or self.config(assistant_provider="codex")
        proc = FakeCli(events or [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "done"}},
        ])
        stages = []
        with only_these_tools("codex"), \
                mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            result = assistant._ask_codex("book it", conf, session,
                                          stages.append, None)
        return result, popen.call_args.args[0], stages

    def test_the_answer(self):
        (answer, _), _, _ = self.run_ask()
        self.assertEqual(answer, "done")

    def test_the_instruction_is_kept_apart_from_the_command(self):
        """Codex takes no system prompt, so the two must not read as one."""
        conf = self.config(assistant_provider="codex")
        _, cmd, _ = self.run_ask(conf)
        body = cmd[-1]
        self.assertTrue(body.startswith(conf.assistant_prompt()))
        self.assertIn("\n\n---\n\n", body)
        self.assertTrue(body.endswith("book it"))

    def test_there_is_nobody_here_to_approve_anything(self):
        _, cmd, _ = self.run_ask()
        self.assertIn('approval_policy="never"', cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertIn("--json", cmd)

    def test_the_sandbox_setting_is_passed_on(self):
        _, cmd, _ = self.run_ask(
            self.config(assistant_provider="codex",
                        assistant_codex_sandbox="read-only"))
        self.assertIn('sandbox_mode="read-only"', cmd)

    def test_no_model_named_means_whatever_codex_is_set_to(self):
        _, cmd, _ = self.run_ask()
        self.assertNotIn("-m", cmd)

    def test_a_model_of_your_own(self):
        _, cmd, _ = self.run_ask(
            self.config(assistant_provider="codex", assistant_codex_model=" gpt-5 "))
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5")

    def test_the_effort_lands_on_the_nearest_rung_codex_has(self):
        _, cmd, _ = self.run_ask(
            self.config(assistant_provider="codex", assistant_reasoning="max"))
        self.assertIn('model_reasoning_effort="high"', cmd)

    def test_a_conversation_is_resumed(self):
        _, cmd, _ = self.run_ask(session="t-1")
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "t-1"])

    def test_a_fresh_conversation(self):
        _, cmd, _ = self.run_ask()
        self.assertEqual(cmd[:2], ["codex", "exec"])

    def test_the_closing_message_is_the_answer(self):
        (answer, _), _, _ = self.run_ask(events=[
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "let me look"}},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "it is on Thursday"}},
        ])
        self.assertEqual(answer, "it is on Thursday")

    def test_the_work_is_narrated_as_it_goes(self):
        _, _, stages = self.run_ask(events=[
            {"type": "item.started", "item": {"type": "command_execution"}},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "done"}},
        ])
        self.assertEqual(stages, ["Running a command…"])

    def test_a_turn_that_failed(self):
        with self.assertRaises(assistant.AssistantError) as caught:
            self.run_ask(events=[{"type": "turn.failed",
                                  "error": {"message": "quota exhausted"}}])
        self.assertIn("quota", str(caught.exception))


class AskAgy(DikteTest):
    """agy's stream is shaped nothing like the other two: the key is `event`,
    the answer arrives whole in `result.response`, and the conversation to
    resume is named in the first line rather than the last."""

    def run_ask(self, conf=None, events=None, session=""):
        conf = conf or self.config(assistant_provider="agy")
        proc = FakeCli(events or [
            {"event": "init", "conversation_id": "c-9", "init": {"cwd": "/home"}},
            {"event": "result",
             "result": {"conversation_id": "c-9", "status": "SUCCESS",
                        "response": "  done  "}},
        ])
        stages = []
        with only_these_tools("agy"), \
                mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            result = assistant._ask_agy("book it", conf, session,
                                        stages.append, None)
        return result, popen.call_args.args[0], stages

    def test_the_answer_comes_back_stripped(self):
        (answer, warning), _, _ = self.run_ask()
        self.assertEqual(answer, "done")
        self.assertEqual(warning, "")

    def test_the_instruction_is_kept_apart_from_the_command(self):
        """agy takes no system prompt, so the two must not read as one."""
        conf = self.config(assistant_provider="agy")
        _, cmd, _ = self.run_ask(conf)
        body = cmd[cmd.index("-p") + 1]
        self.assertTrue(body.startswith(conf.assistant_prompt()))
        self.assertIn("\n\n---\n\n", body)
        self.assertTrue(body.endswith("book it"))

    def test_a_first_command_starts_a_project_of_its_own(self):
        """Without it agy works in whichever project it was last in."""
        _, cmd, _ = self.run_ask()
        self.assertIn("--new-project", cmd)
        self.assertNotIn("--conversation", cmd)

    def test_a_second_command_carries_the_conversation_rather_than_starting_one(self):
        _, cmd, _ = self.run_ask(session="c-9")
        self.assertEqual(cmd[cmd.index("--conversation") + 1], "c-9")
        self.assertNotIn("--new-project", cmd)

    def test_the_conversation_is_kept_under_agy_s_own_name(self):
        self.run_ask()
        self.assertEqual(assistant.read_session("agy", 1800), "c-9")

    def test_it_is_not_left_to_give_up_before_the_caller_does(self):
        conf = self.config(assistant_provider="agy", assistant_timeout=90)
        _, cmd, _ = self.run_ask(conf)
        self.assertEqual(cmd[cmd.index("--print-timeout") + 1], "90s")

    def test_no_model_named_means_whatever_agy_is_set_to(self):
        _, cmd, _ = self.run_ask()
        self.assertNotIn("--model", cmd)

    def test_a_model_of_your_own(self):
        _, cmd, _ = self.run_ask(
            self.config(assistant_provider="agy",
                        assistant_agy_model="gemini-3.1-pro-low"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-3.1-pro-low")

    def test_a_tool_is_named_in_the_corner_as_it_starts(self):
        _, _, stages = self.run_ask(events=[
            {"event": "step_update",
             "step_update": {"step_type": "tool", "state": "ACTIVE",
                             "tool_name": "run_command"}},
            {"event": "step_update",
             "step_update": {"step_type": "tool", "state": "DONE",
                             "tool_name": "run_command"}},
            {"event": "result",
             "result": {"status": "SUCCESS", "response": "done"}},
        ])
        self.assertEqual(stages, ["Running a command…"])

    def test_the_two_dozen_browser_tools_are_one_line_between_them(self):
        _, _, stages = self.run_ask(events=[
            {"event": "step_update",
             "step_update": {"step_type": "tool", "state": "ACTIVE",
                             "tool_name": "browser_click_element"}},
            {"event": "result",
             "result": {"status": "SUCCESS", "response": "done"}},
        ])
        self.assertEqual(stages, ["Working in the browser…"])

    def test_a_turn_that_did_not_succeed_is_a_failure_rather_than_an_answer(self):
        with self.assertRaises(assistant.AssistantError) as caught:
            self.run_ask(events=[
                {"event": "result",
                 "result": {"status": "ERROR", "response": "the model refused"}},
            ])
        self.assertIn("refused", str(caught.exception))

    def test_a_failure_with_nothing_to_say_is_still_named(self):
        with self.assertRaises(assistant.AssistantError) as caught:
            self.run_ask(events=[
                {"event": "result", "result": {"status": "ERROR"}},
            ])
        self.assertIn("Antigravity", str(caught.exception))


class AskOmp(DikteTest):
    def test_two_turns_resume_without_exposing_memory_context(self):
        conf = self.config(assistant_provider="omp")
        first = FakeCli([
            {"type": "session", "id": "voice-123"},
            {"type": "message_end", "message": {
                "role": "custom", "content": "private memory"}},
            {"type": "turn_end", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Thursday"}]}},
        ])
        second = FakeCli([
            {"type": "session", "id": "voice-123"},
            {"type": "turn_end", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Friday"}]}},
        ])
        with only_these_tools("omp"), mock.patch.object(
                subprocess, "Popen", side_effect=[first, second]) as popen:
            answer, _ = assistant.ask("Which day?", conf)
            followup, _ = assistant.ask("Move it to the next day", conf)
        self.assertEqual((answer, followup), ("Thursday", "Friday"))
        self.assertEqual(assistant.read_session("omp", 1800), "voice-123")
        second_cmd = popen.call_args_list[1].args[0]
        self.assertEqual(second_cmd[second_cmd.index("--resume") + 1],
                         "voice-123")

    def test_tool_activity_is_reported_while_work_is_running(self):
        conf = self.config(assistant_provider="omp")
        proc = FakeCli([
            {"type": "session", "id": "voice-123"},
            {"type": "tool_execution_start", "toolName": "web_search"},
            {"type": "tool_execution_end", "toolName": "web_search"},
            {"type": "turn_end", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Done"}]}},
        ])
        stages = []
        with only_these_tools("omp"), mock.patch.object(
                subprocess, "Popen", return_value=proc):
            assistant.ask("Look it up", conf, on_stage=stages.append)
        self.assertEqual(stages, ["Using web_search…"])

    def test_no_assistant_answer_is_an_error(self):
        conf = self.config(assistant_provider="omp")
        proc = FakeCli([
            {"type": "session", "id": "voice-123"},
            {"type": "message_end", "message": {
                "role": "custom", "content": "private memory"}},
        ])
        with only_these_tools("omp"), mock.patch.object(
                subprocess, "Popen", return_value=proc):
            with self.assertRaises(assistant.AssistantError):
                assistant.ask("Merhaba", conf)


class AskOpenRouter(DikteTest):
    def test_a_question_and_an_answer(self):
        conf = self.config(assistant_provider="openrouter",
                           openrouter_api_key="sk-or-test")
        with fake_urlopen({"choices": [{"message": {"content": "on Thursday"}}]}):
            answer, warning = assistant.ask("when is it", conf)
        self.assertEqual(answer, "on Thursday")
        self.assertEqual(warning, "")

    def test_the_conversation_is_ours_to_keep(self):
        conf = self.config(assistant_provider="openrouter",
                           openrouter_api_key="sk-or-test")
        with fake_urlopen({"choices": [{"message": {"content": "on Thursday"}}]}):
            assistant.ask("when is it", conf)
        stored = assistant.read_messages("openrouter", 1800)
        self.assertEqual([row["content"] for row in stored],
                         ["when is it", "on Thursday"])

    def test_the_next_command_knows_what_that_means(self):
        conf = self.config(assistant_provider="openrouter",
                           openrouter_api_key="sk-or-test")
        assistant.write_session("openrouter", messages=[
            {"role": "user", "content": "when is it"},
            {"role": "assistant", "content": "on Thursday"}])
        with fake_urlopen({"choices": [{"message": {"content": "moved"}}]}) as calls:
            assistant.ask("move it to Friday", conf)
        sent = json.loads(calls[0].data.decode("utf-8"))["messages"]
        self.assertEqual(len(sent), 4)   # system, the two stored, the new one

    def test_an_api_failure_reads_as_an_assistant_failure(self):
        conf = self.config(assistant_provider="openrouter")
        with self.assertRaises(assistant.AssistantError):
            assistant.ask("when is it", conf)


class AskOpenCode(DikteTest):
    def test_a_question_and_an_answer(self):
        conf = self.config(assistant_provider="opencode",
                           opencode_api_key="opencode-test-key")
        with fake_urlopen({"choices": [{"message": {"content": "on Thursday"}}]}):
            answer, warning = assistant.ask("when is it", conf)
        self.assertEqual(answer, "on Thursday")
        self.assertEqual(warning, "")

    def test_the_conversation_is_ours_to_keep(self):
        conf = self.config(assistant_provider="opencode",
                           opencode_api_key="opencode-test-key")
        with fake_urlopen({"choices": [{"message": {"content": "on Thursday"}}]}):
            assistant.ask("when is it", conf)
        stored = assistant.read_messages("opencode", 1800)
        self.assertEqual([row["content"] for row in stored],
                         ["when is it", "on Thursday"])

    def test_the_model_and_endpoint_are_opencode_s_own(self):
        conf = self.config(assistant_provider="opencode",
                           opencode_api_key="opencode-test-key",
                           assistant_opencode_model="glm-5.3")
        with fake_urlopen({"choices": [{"message": {"content": "on Thursday"}}]}) as calls:
            assistant.ask("when is it", conf)
        sent = json.loads(calls[0].data.decode("utf-8"))
        self.assertEqual(sent["model"], "glm-5.3")
        self.assertIn("https://opencode.ai/zen/go/v1/chat/completions",
                      calls[0].full_url)

    def test_an_api_failure_reads_as_an_assistant_failure(self):
        conf = self.config(assistant_provider="opencode")
        with self.assertRaises(assistant.AssistantError):
            assistant.ask("when is it", conf)


class AskRequesty(DikteTest):
    def test_a_question_and_an_answer(self):
        conf = self.config(assistant_provider="requesty",
                           requesty_api_key="rqsty-test-key")
        with fake_urlopen({"choices": [{"message": {"content": "on Thursday"}}]}):
            answer, warning = assistant.ask("when is it", conf)
        self.assertEqual(answer, "on Thursday")
        self.assertEqual(warning, "")

    def test_the_conversation_is_ours_to_keep(self):
        conf = self.config(assistant_provider="requesty",
                           requesty_api_key="rqsty-test-key")
        with fake_urlopen({"choices": [{"message": {"content": "on Thursday"}}]}):
            assistant.ask("when is it", conf)
        stored = assistant.read_messages("requesty", 1800)
        self.assertEqual([row["content"] for row in stored],
                         ["when is it", "on Thursday"])

    def test_the_model_key_and_endpoint_are_requesty_s_own(self):
        conf = self.config(assistant_provider="requesty",
                           requesty_api_key="rqsty-test-key",
                           requesty_base_url="https://router.eu.requesty.ai/v1",
                           assistant_requesty_model="gpt-5.4-mini")
        with fake_urlopen({"choices": [{"message": {"content": "on Thursday"}}]}) as calls:
            assistant.ask("when is it", conf)
        sent = json.loads(calls[0].data.decode("utf-8"))
        self.assertEqual(sent["model"], "gpt-5.4-mini")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer rqsty-test-key")
        self.assertIn("https://router.eu.requesty.ai/v1/chat/completions",
                      calls[0].full_url)
        self.assertEqual(assistant.model(conf), "gpt-5.4-mini")

    def test_an_api_failure_reads_as_an_assistant_failure(self):
        conf = self.config(assistant_provider="requesty")
        with self.assertRaises(assistant.AssistantError):
            assistant.ask("when is it", conf)


class Ask(DikteTest):
    def test_a_cli_that_is_not_installed_says_where_to_change_it(self):
        with only_these_tools(), \
                self.assertRaises(assistant.AssistantError) as caught:
            assistant.ask("hi", self.config())
        self.assertIn("claude", str(caught.exception))
        self.assertIn("Settings", str(caught.exception))

    def test_a_session_that_is_gone_is_started_over_without_a_word(self):
        conf = self.config()
        assistant.write_session("claude", "stale-id")
        attempts = []

        def run(prompt, conf, session, on_stage, should_stop):
            attempts.append(session)
            if session:
                raise assistant._SessionGone()
            return "done", ""

        with only_these_tools("claude"), \
                mock.patch.object(assistant, "_ask_claude", side_effect=run):
            answer, _ = assistant.ask("hi", conf)
        self.assertEqual(answer, "done")
        self.assertEqual(attempts, ["stale-id", ""])
        self.assertEqual(assistant.stored_provider(), "")

    def test_a_resumed_run_that_dies_is_retried_without_the_session_flag(self):
        # All the way through the stream this time: the first run exits 1 with
        # no answer and whatever stderr it liked, and the recovery must not
        # depend on those words.
        conf = self.config()
        assistant.write_session("claude", "stale-id")
        procs = iter([
            FakeCli(code=1),
            FakeCli(events=[{"type": "result", "result": "done"}]),
        ])
        cmds = []

        def popen(cmd, **kwargs):
            cmds.append(cmd)
            return next(procs)

        with only_these_tools("claude"), \
                mock.patch.object(subprocess, "Popen", side_effect=popen):
            answer, _ = assistant.ask("hi", conf)
        self.assertEqual(answer, "done")
        self.assertEqual(cmds[0][cmds[0].index("--resume") + 1], "stale-id")
        self.assertNotIn("--resume", cmds[1])

    def test_a_run_that_dies_with_an_answer_in_hand_is_not_retried(self):
        conf = self.config()
        assistant.write_session("claude", "stale-id")
        calls = []

        def popen(cmd, **kwargs):
            calls.append(cmd)
            return FakeCli(events=[{"type": "result", "result": "done"}], code=1)

        with only_these_tools("claude"), \
                mock.patch.object(subprocess, "Popen", side_effect=popen):
            answer, _ = assistant.ask("hi", conf)
        self.assertEqual(answer, "done")
        self.assertEqual(len(calls), 1)


class CodexModels(DikteTest):
    """The model list read off `codex debug models`."""

    CATALOG = {"models": [
        {"slug": "gpt-6-mini", "visibility": "list", "priority": 9},
        {"slug": "gpt-6", "visibility": "list", "priority": 1},
        {"slug": "codex-auto-review", "visibility": "hide", "priority": 3},
    ]}

    def models(self, reply, code=0):
        with only_these_tools("codex"), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(
                                      returncode=code, stdout=reply)) as run:
            found = assistant.codex_models()
        self.run_call = run
        return found

    def test_the_catalog_arrives_best_first_without_the_hidden_ones(self):
        found = self.models(json.dumps(self.CATALOG))
        self.assertEqual(found, ["gpt-6", "gpt-6-mini"])
        self.assertEqual(self.run_call.call_args.args[0],
                         ["codex", "debug", "models"])

    def test_a_codex_that_is_not_installed_is_not_run(self):
        with only_these_tools(), \
                mock.patch.object(subprocess, "run") as run:
            self.assertEqual(assistant.codex_models(), [])
        run.assert_not_called()

    def test_a_codex_too_old_to_have_the_command(self):
        self.assertEqual(self.models("error: unknown subcommand", code=2), [])

    def test_a_catalog_that_is_not_what_was_expected(self):
        self.assertEqual(self.models(json.dumps(["gpt-6"])), [])
        self.assertEqual(self.models(""), [])

    def test_a_codex_that_hangs_is_given_up_on(self):
        with only_these_tools("codex"), \
                mock.patch.object(subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(
                                      ["codex"], 30)):
            self.assertEqual(assistant.codex_models(), [])


class AgyModels(DikteTest):
    """The model list read off `agy models`: one id, a tab, a display name."""

    LISTING = ("gemini-4-flash-high\tGemini 4 Flash (High)\n"
               "gemini-4-flash-low\tGemini 4 Flash (Low)\n"
               "a line with no tab is not a model\n"
               "\ta tab with no id in front of it is not one either\n")

    def models(self, reply, code=0):
        with only_these_tools("agy"), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(
                                      returncode=code, stdout=reply)) as run:
            found = assistant.agy_models()
        self.run_call = run
        return found

    def test_the_listing_arrives_in_agy_s_own_order(self):
        found = self.models(self.LISTING)
        self.assertEqual(found, ["gemini-4-flash-high", "gemini-4-flash-low"])
        self.assertEqual(self.run_call.call_args.args[0], ["agy", "models"])

    def test_an_agy_that_is_not_installed_is_not_run(self):
        with only_these_tools(), \
                mock.patch.object(subprocess, "run") as run:
            self.assertEqual(assistant.agy_models(), [])
        run.assert_not_called()

    def test_a_call_that_failed_answers_with_nothing(self):
        self.assertEqual(self.models("error: not logged in", code=1), [])
        self.assertEqual(self.models(""), [])

    def test_an_agy_that_hangs_is_given_up_on(self):
        with only_these_tools("agy"), \
                mock.patch.object(subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(
                                      ["agy"], 30)):
            self.assertEqual(assistant.agy_models(), [])


if __name__ == "__main__":
    unittest.main()
