"""The dictation chain end to end, with every outside call faked.

This is the one test that says what a dictation actually does: what gets sent,
what gets pasted, what is written to the history, and what happens to the audio
afterwards. A pull request that reorders any of it shows up here.
"""

import contextlib
import io
import os
import threading
import unittest
from unittest import mock

from dikte import api
from dikte import assistant
from dikte import config as cfg
from dikte import paste
from dikte import worker
from tests.support import DikteTest, make_wav, speech


class Chain(DikteTest):
    def setUp(self):
        super().setUp()
        self.conf = self.config(openai_api_key="sk-test",
                                openrouter_api_key="sk-or-test")
        self.wav = make_wav(self.path("clip.wav"), speech(2.0))
        # The levels a real recording of that length would have handed over.
        self.rms = [0.0005] * 40 + [0.2] * 20

    def run_chain(self, ask=False, paste_override=None, duration=2.0,
                  transcript="uh, book it for Thursday",
                  transcribe_error=None,
                  cleaned="Book it for Thursday.",
                  cleanup_error=None, answer=("Booked.", ""), rms=None,
                  clipboard=b"what was there before", paste_error=None,
                  detected="en", focus=None):
        pipeline = worker.Pipeline(self.conf)
        done, failures, stages, cancels = [], [], [], []
        pipeline.finished.connect(lambda *args: done.append(args))
        pipeline.failed.connect(failures.append)
        pipeline.stage.connect(stages.append)
        pipeline.cancelled.connect(lambda: cancels.append(True))

        cleanup = (mock.Mock(side_effect=cleanup_error) if cleanup_error
                   else mock.Mock(return_value=cleaned))
        # Auto mode takes the detection path; a fixed language the plain one.
        # Both are mocked so the chain runs either way without a server.
        behavior = {"side_effect": transcribe_error} if transcribe_error \
            else {"return_value": transcript}
        detect_behavior = {"side_effect": transcribe_error} if transcribe_error \
            else {"return_value": (transcript, detected)}
        calls = {}
        # The chain reports its own failures on stderr, which a test run has no
        # use for.
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(api, "transcribe", **behavior) as tr, \
                mock.patch.object(api, "transcribe_detected",
                                  **detect_behavior) as tdet, \
                mock.patch.object(api, "cleanup", cleanup), \
                mock.patch.object(assistant, "ask", return_value=answer) as ask_call, \
                mock.patch.object(paste, "copy") as copy, \
                mock.patch.object(paste, "copy_bytes") as copy_bytes, \
                mock.patch.object(paste, "press") as press, \
                mock.patch.object(paste, "read_clipboard",
                                  return_value=clipboard) as read_clipboard, \
                mock.patch.object(worker.time, "sleep", lambda seconds: None):
            press.side_effect = paste_error
            calls = {"transcribe": tr, "transcribe_detected": tdet,
                     "cleanup": cleanup, "ask": ask_call,
                     "copy": copy, "copy_bytes": copy_bytes, "press": press,
                     "read_clipboard": read_clipboard}
            pipeline._work(self.wav, duration,
                           self.rms if rms is None else rms, ask, paste_override,
                           focus)
        return {"done": done, "failures": failures, "stages": stages,
                "cancelled": cancels, **calls}

    # ---- the ordinary run -------------------------------------------------

    def test_a_dictation_is_transcribed_cleaned_copied_and_pasted(self):
        run = self.run_chain()
        self.assertEqual(run["failures"], [])
        self.assertEqual(run["done"][0],
                         ("uh, book it for Thursday", "Book it for Thursday.",
                          "", "en"))
        run["copy"].assert_called_once_with("Book it for Thursday.")
        run["press"].assert_called_once_with(self.conf["paste_shortcut"],
                                             focus=None)

    def test_the_paste_is_told_where_the_dictation_started(self):
        """Whoever was in front when the recording began is where the keys are
        meant to go, and the press is the only part that can act on it."""
        run = self.run_chain(focus=4242)
        run["press"].assert_called_once_with(self.conf["paste_shortcut"],
                                             focus=4242)

    def test_the_stages_are_named_as_they_happen(self):
        run = self.run_chain()
        self.assertEqual(run["stages"][:2], ["Transcribing…", "Cleaning up…"])

    def test_cleanup_switched_off_pastes_what_was_heard(self):
        self.conf["cleanup_enabled"] = False
        run = self.run_chain()
        run["cleanup"].assert_not_called()
        run["copy"].assert_called_once_with("uh, book it for Thursday")

    def test_auto_paste_switched_off_only_copies(self):
        self.conf["auto_paste"] = False
        self.conf["restore_clipboard"] = True
        run = self.run_chain()
        run["copy"].assert_called_once()
        run["press"].assert_not_called()
        run["read_clipboard"].assert_not_called()

    def test_a_run_asked_for_from_a_terminal_pastes_nowhere(self):
        """The text comes back down the socket; the focused window is nobody's."""
        run = self.run_chain(paste_override=False)
        run["press"].assert_not_called()
        run["copy"].assert_called_once()

    def test_the_clipboard_is_put_back_afterwards(self):
        self.conf["restore_clipboard"] = True
        run = self.run_chain()
        run["copy_bytes"].assert_called_once_with(b"what was there before")

    def test_nothing_is_put_back_when_the_setting_is_off(self):
        self.conf["restore_clipboard"] = False
        run = self.run_chain()
        run["copy_bytes"].assert_not_called()

    def test_a_failed_keypress_leaves_the_transcript_on_the_clipboard(self):
        """The press failing is a warning, not a lost dictation: restoring the
        old clipboard over the text would leave nothing to paste by hand."""
        self.conf["restore_clipboard"] = True
        run = self.run_chain(paste_error=paste.PasteError("not trusted"))
        self.assertEqual(run["failures"], [])
        raw, text, warning, _lang = run["done"][0]
        self.assertIn("not trusted", warning)
        run["copy_bytes"].assert_not_called()

    def test_a_failed_keypress_still_reaches_the_history(self):
        self.run_chain(paste_error=paste.PasteError("not trusted"))
        rows = cfg.read_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Book it for Thursday.")
        # The row goes in before the paste is attempted, so the paste failing
        # has to be written back into it: the record tells the whole truth.
        self.assertIn("not trusted", rows[0]["cleanup_error"])

    def test_a_failed_transcription_keeps_the_audio(self):
        """Speech the user cannot repeat from memory must survive the failure."""
        run = self.run_chain(transcribe_error=api.ApiError("server down"))
        self.assertIn("server down", run["failures"][0])
        self.assertIn("kept", run["failures"][0])
        kept = list(cfg.RECORDINGS_DIR.glob("*.wav"))
        self.assertEqual(len(kept), 1)
        self.assertFalse(os.path.exists(self.wav))

    def test_two_failures_in_one_second_keep_both_recordings(self):
        self.run_chain(transcribe_error=api.ApiError("down"))
        self.wav = make_wav(self.path("clip2.wav"), speech(2.0))
        with mock.patch.object(worker.time, "strftime",
                               return_value="20260820-120000"):
            self.run_chain(transcribe_error=api.ApiError("down"))
            self.wav = make_wav(self.path("clip3.wav"), speech(2.0))
            self.run_chain(transcribe_error=api.ApiError("down"))
        self.assertEqual(len(list(cfg.RECORDINGS_DIR.glob("*.wav"))), 3)

    def test_the_history_row_says_whether_cleanup_actually_ran(self):
        """The ask path cleans under its own setting; the record follows the
        run, not the dictation gate."""
        self.conf["cleanup_enabled"] = False
        self.conf["assistant_cleanup"] = True
        self.run_chain(ask=True)
        row = cfg.read_history()[0]
        self.assertNotEqual(row["cleanup_model"], "")
        cfg.clear_history()
        self.conf["cleanup_enabled"] = True
        self.conf["assistant_cleanup"] = False
        self.run_chain(ask=True)
        self.assertEqual(cfg.read_history()[0]["cleanup_model"], "")

    def test_the_transcription_is_told_the_language_and_the_glossary(self):
        self.conf["language"] = "tr"
        self.conf["transcribe_prompt"] = "Paraşüt"
        run = self.run_chain()
        self.assertEqual(run["transcribe"].call_args.kwargs["language"], "tr")
        self.assertEqual(run["transcribe"].call_args.kwargs["prompt"], "Paraşüt")

    def test_auto_mode_asks_for_the_detected_language_and_records_it(self):
        run = self.run_chain(detected="tr")
        told = run["transcribe_detected"].call_args.kwargs
        self.assertEqual(told["language"], "auto")
        self.assertEqual(cfg.read_history()[0]["speech_language"], "tr")
        self.assertEqual(run["done"][0][3], "tr")
        run["transcribe"].assert_not_called()

    def test_the_detected_language_is_told_to_the_cleanup_prompt(self):
        # The mock stands in for api.cleanup, which the cleanup module calls
        # with (text, key, model, system_prompt, …); the prompt is the fourth.
        self.conf["transcribe_prompt"] = "Paraşüt"
        run = self.run_chain(detected="tr")
        prompt = run["cleanup"].call_args.args[3]
        # Turkish was detected, so the Turkish glossary rule is appended.
        self.assertIn("KONUŞMACININ KULLANDIĞI İSİM VE TERİMLER", prompt)

    def test_a_fixed_language_needs_no_detection(self):
        self.conf["language"] = "en"
        run = self.run_chain()
        run["transcribe"].assert_called_once()
        run["transcribe_detected"].assert_not_called()
        self.assertEqual(cfg.read_history()[0]["speech_language"], "en")

    # ---- silence and stock phrases ----------------------------------------

    def test_room_tone_costs_no_api_call(self):
        run = self.run_chain(rms=[0.00001] * 60)
        run["transcribe_detected"].assert_not_called()
        self.assertIn("No speech", run["failures"][0])

    def test_the_silence_check_can_be_switched_off(self):
        self.conf["skip_silent"] = False
        run = self.run_chain(rms=[0.00001] * 60)
        run["transcribe_detected"].assert_called_once()

    def test_a_stock_phrase_from_a_short_clip_is_thrown_away(self):
        run = self.run_chain(duration=2.0, transcript="Altyazı M.K.")
        self.assertIn("stock phrase", run["failures"][0])
        run["copy"].assert_not_called()

    def test_the_hallucination_filter_can_be_switched_off(self):
        self.conf["filter_hallucinations"] = False
        run = self.run_chain(duration=2.0, transcript="Altyazı M.K.")
        run["copy"].assert_called_once()

    # ---- when something goes wrong ----------------------------------------

    def test_a_failed_cleanup_still_pastes_the_transcript(self):
        run = self.run_chain(cleanup_error=api.ApiError("rate limited"))
        _raw, text, warning, _lang = run["done"][0]
        self.assertEqual(text, "uh, book it for Thursday")
        self.assertIn("rate limited", warning)
        run["copy"].assert_called_once_with("uh, book it for Thursday")

    def test_a_failed_cleanup_is_never_silent(self):
        """A rejected key would otherwise look like dictation that works."""
        run = self.run_chain(cleanup_error=api.ApiError("bad key"))
        self.assertTrue(run["done"][0][2])
        self.assertEqual(cfg.read_history()[0]["cleanup_error"], "bad key")

    def test_a_failed_transcription_ends_the_run(self):
        # This path mocks api.transcribe, so it wants
        # the plain (fixed-language) transcription.
        self.conf["language"] = "tr"
        pipeline = worker.Pipeline(self.conf)
        failures = []
        pipeline.failed.connect(failures.append)
        with mock.patch.object(api, "transcribe",
                               side_effect=api.ApiError("no credit")), \
                mock.patch.object(paste, "copy") as copy:
            pipeline._work(self.wav, 2.0, self.rms, False, None)
        self.assertIn("no credit", failures[0])
        copy.assert_not_called()

    def test_a_clipboard_that_will_not_take_it(self):
        # This path mocks api.transcribe, so it wants
        # the plain (fixed-language) transcription.
        self.conf["language"] = "tr"
        pipeline = worker.Pipeline(self.conf)
        failures = []
        pipeline.failed.connect(failures.append)
        with mock.patch.object(api, "transcribe", return_value="hello"), \
                mock.patch.object(api, "cleanup", return_value="Hello."), \
                mock.patch.object(paste, "read_clipboard", return_value=None), \
                mock.patch.object(paste, "copy",
                                  side_effect=paste.PasteError("no wl-copy")):
            pipeline._work(self.wav, 2.0, self.rms, False, None)
        self.assertIn("wl-copy", failures[0])

    def test_an_unexpected_error_is_reported_rather_than_swallowed(self):
        # This path mocks api.transcribe, so it wants
        # the plain (fixed-language) transcription.
        self.conf["language"] = "tr"
        pipeline = worker.Pipeline(self.conf)
        failures = []
        pipeline.failed.connect(failures.append)
        with mock.patch.object(api, "transcribe", side_effect=ValueError("oh dear")), \
                mock.patch("traceback.print_exc"):
            pipeline._work(self.wav, 2.0, self.rms, False, None)
        self.assertIn("oh dear", failures[0])

    # ---- handing it to an agent -------------------------------------------

    def test_a_command_goes_to_the_agent_and_the_answer_comes_back(self):
        run = self.run_chain(ask=True)
        run["ask"].assert_called_once()
        self.assertEqual(run["ask"].call_args.args[0], "uh, book it for Thursday")
        run["copy"].assert_called_once_with("Booked.")

    def test_a_command_is_not_cleaned_up_first_by_default(self):
        """The agent reads through the filler words without help."""
        run = self.run_chain(ask=True)
        run["cleanup"].assert_not_called()

    def test_a_command_can_be_cleaned_up_if_you_want(self):
        self.conf["assistant_cleanup"] = True
        run = self.run_chain(ask=True)
        run["cleanup"].assert_called_once()
        self.assertEqual(run["ask"].call_args.args[0], "Book it for Thursday.")

    def test_a_denied_tool_arrives_beside_the_answer(self):
        run = self.run_chain(ask=True, answer=("Booked.", "It could not use: Bash"))
        self.assertIn("Bash", run["done"][0][2])

    def test_the_agent_has_its_own_paste_setting(self):
        self.conf["assistant_paste"] = False
        run = self.run_chain(ask=True)
        run["press"].assert_not_called()

    def test_a_command_that_was_cancelled(self):
        # This path mocks api.transcribe, so it wants
        # the plain (fixed-language) transcription.
        self.conf["language"] = "tr"
        pipeline = worker.Pipeline(self.conf)
        cancels = []
        pipeline.cancelled.connect(lambda: cancels.append(True))
        with mock.patch.object(api, "transcribe", return_value="hello"), \
                mock.patch.object(assistant, "ask", side_effect=assistant.Cancelled):
            pipeline._work(self.wav, 2.0, self.rms, True, None)
        self.assertEqual(cancels, [True])

    def test_an_agent_that_is_not_installed(self):
        # This path mocks api.transcribe, so it wants
        # the plain (fixed-language) transcription.
        self.conf["language"] = "tr"
        pipeline = worker.Pipeline(self.conf)
        failures = []
        pipeline.failed.connect(failures.append)
        with mock.patch.object(api, "transcribe", return_value="hello"), \
                mock.patch.object(assistant, "ask",
                                  side_effect=assistant.AssistantError("no claude")):
            pipeline._work(self.wav, 2.0, self.rms, True, None)
        self.assertIn("no claude", failures[0])

    # ---- what is left behind ----------------------------------------------

    def test_the_run_is_written_to_the_history(self):
        self.run_chain()
        row = cfg.read_history()[0]
        self.assertEqual(row["raw"], "uh, book it for Thursday")
        self.assertEqual(row["text"], "Book it for Thursday.")
        self.assertEqual(row["duration"], 2.0)
        self.assertEqual(row["model"], self.conf.transcribe_target().model)
        self.assertEqual(row["mode"], "")

    def test_a_command_is_recorded_as_one(self):
        self.run_chain(ask=True)
        row = cfg.read_history()[0]
        self.assertEqual(row["mode"], "ask")
        self.assertEqual(row["question"], "uh, book it for Thursday")
        self.assertEqual(row["text"], "Booked.")

    def test_the_history_is_kept_to_its_limit(self):
        self.conf["history_limit"] = 2
        for _ in range(4):
            self.wav = make_wav(self.path("clip.wav"), speech(2.0))
            self.run_chain()
        self.assertEqual(len(cfg.read_history()), 2)

    def test_the_recording_is_deleted_when_it_is_done_with(self):
        self.run_chain()
        self.assertFalse(os.path.exists(self.wav))

    def test_the_recording_is_kept_when_the_setting_says_so(self):
        self.conf["keep_audio"] = True
        self.run_chain()
        self.assertFalse(os.path.exists(self.wav))
        self.assertEqual(len(list(cfg.RECORDINGS_DIR.iterdir())), 1)

    def test_the_recording_goes_even_when_the_run_failed(self):
        self.run_chain(rms=[0.00001] * 60)
        self.assertFalse(os.path.exists(self.wav))


class Busy(DikteTest):
    def test_a_second_run_while_one_is_going_waits_its_turn(self):
        """The microphone is free while a transcript is being cleaned up, so
        the next dictation can already have been spoken by then. It has to run
        once the first is done, in the order they were spoken, on one thread."""
        pipeline = worker.Pipeline(self.config())
        order = []
        started, gate = threading.Event(), threading.Event()

        def work(wav_path, *_rest):
            order.append(wav_path)
            started.set()
            if wav_path == "first.wav":
                gate.wait(5)

        with mock.patch.object(pipeline, "_work", side_effect=work):
            pipeline.run("first.wav", 1.0)
            self.assertTrue(started.wait(5))
            pipeline.run("second.wav", 1.0)
            # Held, not dropped and not running beside the first.
            self.assertEqual(order, ["first.wav"])
            gate.set()
            pipeline._thread.join(5)
        self.assertEqual(order, ["first.wav", "second.wav"])

    def test_a_run_arriving_after_the_queue_drained(self):
        """The worker thread ends with the queue; the next run brings one."""
        pipeline = worker.Pipeline(self.config())
        order = []
        with mock.patch.object(pipeline, "_work",
                               side_effect=lambda wav, *rest: order.append(wav)):
            pipeline.run("first.wav", 1.0)
            pipeline._thread.join(5)
            pipeline.run("second.wav", 1.0)
            pipeline._thread.join(5)
        self.assertEqual(order, ["first.wav", "second.wav"])

    def test_the_chunk_length_matches_the_level_meter(self):
        """The silence thresholds are read in seconds, so the two must agree."""
        self.assertAlmostEqual(worker.CHUNK_SECONDS, 1024 / 16000)


if __name__ == "__main__":
    unittest.main()
