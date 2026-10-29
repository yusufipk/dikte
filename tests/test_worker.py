"""The dictation chain end to end, with every outside call faked.

This is the one test that says what a dictation actually does: what gets sent,
what gets pasted, what is written to the history, and what happens to the audio
afterwards. A pull request that reorders any of it shows up here.
"""

import collections
import contextlib
import io
import os
import threading
import unittest
from unittest import mock

from dikte import api
from dikte import assistant
from dikte import config as cfg
from dikte import dictionary
from dikte import paste
from dikte import profiles
from dikte import worker
from tests.support import DikteTest, make_wav, speech


class ChainBase(DikteTest):
    """One dictation run, every outside call faked; the tests below drive it."""

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
                  detected="en", focus=None, retry=None, profile=None):
        pipeline = worker.Pipeline(self.conf)
        done, failures, stages, cancels = [], [], [], []
        pipeline.finished.connect(lambda *args: done.append(args))
        pipeline.failed.connect(failures.append)
        pipeline.stage.connect(stages.append)
        pipeline.cancelled.connect(lambda: cancels.append(True))
        # The waits between retries record themselves instead of running: what
        # the tests pin is the ladder's timing, not the wall clock. The real
        # Event would block on wait() for real, and nothing here cancels.
        sleeps = []
        stop = mock.Mock(spec=threading.Event)
        stop.wait.side_effect = (lambda seconds:
                                 sleeps.append(seconds) or False)
        stop.is_set.return_value = False
        pipeline._stop = stop

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
                           focus, retry, profile)
        return {"done": done, "failures": failures, "stages": stages,
                "cancelled": cancels, "sleeps": sleeps, **calls}



class Chain(ChainBase):
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

    # ---- a failed dictation in the history ----------------------------------

    def test_a_failed_dictation_is_listed_with_its_recording(self):
        """The history shows the failure and where its audio went, so it can
        be tried again from there."""
        self.run_chain(transcribe_error=api.ApiError("server down"))
        row, = cfg.read_history()
        self.assertTrue(row["failed"])
        self.assertEqual(row["error"], "server down")
        self.assertEqual(row["text"], "")
        self.assertEqual(row["duration"], 2.0)
        self.assertEqual(row["audio"],
                         str(next(cfg.RECORDINGS_DIR.glob("*.wav"))))

    def test_a_failed_command_leaves_no_retry_row(self):
        self.run_chain(ask=True, transcribe_error=api.ApiError("server down"))
        self.assertEqual(cfg.read_history(), [])

    def test_a_failure_after_the_row_was_written_adds_no_second_row(self):
        """The clipboard refusing a finished transcript is not a failed
        dictation: the transcript is already in the history."""
        with mock.patch.object(paste, "copy",
                               side_effect=paste.PasteError("no wl-copy")):
            self.conf["language"] = "tr"
            pipeline = worker.Pipeline(self.conf)
            with contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(api, "transcribe", return_value="hello"), \
                    mock.patch.object(api, "cleanup", return_value="Hello."), \
                    mock.patch.object(paste, "read_clipboard", return_value=None):
                pipeline._work(self.wav, 2.0, self.rms, False, None)
        row, = cfg.read_history()
        self.assertEqual(row["text"], "Hello.")
        self.assertNotIn("failed", row)

    def test_a_retry_puts_the_transcript_in_the_failed_rows_place(self):
        self.run_chain(transcribe_error=api.ApiError("server down"))
        failed, = cfg.read_history()
        self.wav = failed["audio"]
        run = self.run_chain(retry=failed, paste_override=False, rms=[])
        self.assertEqual(run["failures"], [])
        row, = cfg.read_history()
        self.assertEqual(row["text"], "Book it for Thursday.")
        self.assertEqual(row["ts"], failed["ts"])
        self.assertNotIn("failed", row)
        run["copy"].assert_called_once_with("Book it for Thursday.")
        run["press"].assert_not_called()
        # Done with, so gone, like any other dictation's recording.
        self.assertEqual(list(cfg.RECORDINGS_DIR.glob("*.wav")), [])

    def test_a_retry_that_fails_again_updates_its_row(self):
        self.run_chain(transcribe_error=api.ApiError("server down"))
        failed, = cfg.read_history()
        self.wav = failed["audio"]
        run = self.run_chain(retry=failed, rms=[],
                             transcribe_error=api.ApiError("still down", 402))
        self.assertIn("still down", run["failures"][0])
        row, = cfg.read_history()
        self.assertEqual(row["error"], "still down")
        self.assertTrue(os.path.isfile(row["audio"]))
        self.assertEqual(len(list(cfg.RECORDINGS_DIR.glob("*.wav"))), 1)

    def test_a_retry_whose_recording_cannot_be_moved_keeps_it(self):
        """The kept file is the only copy of the speech: a retry that fails
        and cannot move it must not delete it on the way out."""
        self.run_chain(transcribe_error=api.ApiError("server down"))
        failed, = cfg.read_history()
        self.wav = failed["audio"]
        with mock.patch.object(worker.shutil, "move", side_effect=OSError("full")):
            self.run_chain(retry=failed, rms=[],
                           transcribe_error=api.ApiError("still down", 402))
        self.assertTrue(os.path.isfile(failed["audio"]))

    def test_a_retried_recording_stays_put_when_audio_is_kept(self):
        self.run_chain(transcribe_error=api.ApiError("server down"))
        failed, = cfg.read_history()
        self.conf["keep_audio"] = True
        self.wav = failed["audio"]
        self.run_chain(retry=failed, rms=[])
        self.assertEqual(list(cfg.RECORDINGS_DIR.glob("*.wav")),
                         [cfg.RECORDINGS_DIR / os.path.basename(failed["audio"])])

    def test_a_retry_whose_recording_is_gone_says_so(self):
        failed = {"ts": "x", "failed": True, "audio": str(self.path("gone.wav"))}
        cfg.append_history(failed)
        self.wav = failed["audio"]
        run = self.run_chain(retry=failed, rms=[])
        self.assertIn("no longer there", run["failures"][0])
        run["transcribe_detected"].assert_not_called()
        self.assertEqual(cfg.read_history(), [failed])

    # ---- the retry ladder ---------------------------------------------------

    def test_a_rate_limited_dictation_is_asked_again_and_survives(self):
        """A 429 is the service having a busy minute, not the run being wrong:
        the request is asked again and the dictation lands as usual."""
        run = self.run_chain(transcribe_error=[
            api.ApiError("rate limited", 429),
            ("uh, book it for Thursday", "en")])
        self.assertEqual(run["failures"], [])
        self.assertEqual(run["transcribe_detected"].call_count, 2)
        self.assertEqual(run["sleeps"], [10])
        self.assertTrue(any("Retrying in 10 s (2/5)" in s for s in run["stages"]))
        self.assertEqual(run["done"][0],
                         ("uh, book it for Thursday", "Book it for Thursday.",
                          "", "en"))

    def test_every_try_shares_what_was_already_heard(self):
        """A dictation sent in pieces keeps the finished ones across the ladder."""
        run = self.run_chain(transcribe_error=[
            api.ApiError("rate limited", 429),
            api.ApiError("rate limited", 429),
            ("uh, book it for Thursday", "en")])
        heards = [c.kwargs["heard"]
                  for c in run["transcribe_detected"].call_args_list]
        self.assertEqual(len(heards), 3)
        self.assertIsInstance(heards[0], api.Heard)
        self.assertTrue(all(h is heards[0] for h in heards))

    def test_a_fixed_language_shares_it_too(self):
        self.conf["language"] = "tr"
        run = self.run_chain(transcribe_error=[
            api.ApiError("rate limited", 429), "uh, book it for Thursday"])
        first, second = run["transcribe"].call_args_list
        self.assertIsInstance(first.kwargs["heard"], api.Heard)
        self.assertIs(first.kwargs["heard"], second.kwargs["heard"])

    def test_the_retry_waits_grow_between_attempts(self):
        """The ladder starts short and grows, and the line the user is watching
        always names the failure and comes back to the stage afterwards."""
        pipeline = worker.Pipeline(self.conf)
        sleeps, stages = [], []
        pipeline.stage.connect(stages.append)
        stop = mock.Mock(spec=threading.Event)
        stop.wait.side_effect = (lambda seconds:
                                 sleeps.append(seconds) or False)
        stop.is_set.return_value = False
        pipeline._stop = stop
        calls = []

        def call():
            calls.append(1)
            if len(calls) < 3:
                raise api.ApiError("rate limited", 429)
            return "transcript"

        self.assertEqual(pipeline._transcribe(call, "Transcribing…"),
                         "transcript")
        self.assertEqual(calls, [1, 1, 1])
        self.assertEqual(sleeps, [10, 30])
        self.assertEqual(stages, [
            "rate limited Retrying in 10 s (2/5)…", "Transcribing…",
            "rate limited Retrying in 30 s (3/5)…", "Transcribing…"])

    def test_the_ladder_gives_up_after_its_last_wait(self):
        """Every rung spent, the run fails as before and the audio is kept."""
        run = self.run_chain(
            transcribe_error=[api.ApiError("rate limited", 429)] * 5)
        self.assertEqual(run["transcribe_detected"].call_count, 5)
        self.assertEqual(run["sleeps"], [10, 30, 60, 120])
        self.assertTrue(any("Retrying in 120 s (5/5)" in s for s in run["stages"]))
        self.assertIn("rate limited", run["failures"][0])
        self.assertIn("kept", run["failures"][0])
        self.assertEqual(len(list(cfg.RECORDINGS_DIR.glob("*.wav"))), 1)

    def test_a_stop_landing_mid_wait_ends_the_run_with_its_reason(self):
        """Cancelling during a wait fails the run with the error it was
        waiting out, audio kept one level up, rather than asking once more."""
        pipeline = worker.Pipeline(self.conf)
        stop = mock.Mock(spec=threading.Event)
        stop.wait.return_value = True  # cancel arrived during the wait
        stop.is_set.return_value = False
        pipeline._stop = stop
        calls = []

        def call():
            calls.append(1)
            raise api.ApiError("rate limited", 429)

        with self.assertRaises(api.ApiError):
            pipeline._transcribe(call, "Transcribing…")
        self.assertEqual(calls, [1])

    def test_a_failure_the_ladder_cannot_fix_is_not_retried(self):
        """A rejected key is just as wrong the second time: no wait, no retry."""
        run = self.run_chain(transcribe_error=api.ApiError("no credit", 402))
        self.assertEqual(run["transcribe_detected"].call_count, 1)
        self.assertEqual(run["sleeps"], [])

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
    def test_one_recording_is_retried_once_at_a_time(self):
        pipeline = worker.Pipeline(self.config())
        gate = threading.Event()
        pipeline._work = lambda *job: gate.wait(5)
        row = {"failed": True, "audio": "/kept.wav"}
        self.assertIsNone(pipeline.run("/kept.wav", 1.0, retry=row))
        self.assertIs(pipeline.run("/kept.wav", 1.0, retry=row), False)
        gate.set()
        pipeline._thread.join(5)
        self.assertIsNone(pipeline.run("/kept.wav", 1.0, retry=row))
        pipeline._thread.join(5)

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


class ProfileChain(ChainBase):
    """A dictation under a writing profile: the prompt it gets, what a
    translation does to the paste and the history, and what learning reads."""

    def profiled(self, **values):
        for key, value in values.items():
            self.conf[key] = value
        return profiles.choose(self.conf, "thunderbird", looked=True)

    def test_the_profile_shapes_the_prompt_and_is_named_in_the_history(self):
        choice = self.profiled(profiles=[{"name": "Email", "apps": ["thunderbird"],
                                          "style": {"formality": "formal"}}])
        run = self.run_chain(profile=choice)
        self.assertIn("formal register", run["cleanup"].call_args.args[3])
        self.assertIn("Cleaning up (Email)…", run["stages"])
        row = cfg.read_history()[0]
        self.assertEqual((row["profile"], row["program"]), ("Email", "thunderbird"))

    def test_the_agent_is_never_shaped_by_a_profile(self):
        self.conf["assistant_cleanup"] = True
        choice = self.profiled(profile_default={"style": {"formality": "formal"},
                                                "translate_to": "de"})
        run = self.run_chain(ask=True, profile=choice)
        self.assertNotIn("WRITING STYLE", run["cleanup"].call_args.args[3])
        self.assertNotIn("translate_to", cfg.read_history()[0])

    def test_a_lookup_is_resolved_on_the_dictation_thread(self):
        """app.py hands over the lookup itself; the main thread never waits."""
        choice = self.profiled(profiles=[{"name": "Email", "apps": ["thunderbird"]}])
        lookup = mock.Mock()
        lookup.result.return_value = choice
        self.run_chain(profile=lookup)
        lookup.result.assert_called_once_with()
        self.assertEqual(cfg.read_history()[0]["profile"], "Email")

    def test_the_lookup_is_awaited_after_transcription(self):
        """Transcription needs no profile, so its round trip covers the wait."""
        choice = self.profiled(profiles=[{"name": "Email", "apps": ["thunderbird"]}])
        order = []
        lookup = mock.Mock()
        lookup.result.side_effect = lambda: order.append("profile") or choice
        with mock.patch.object(api, "transcribe_detected",
                               side_effect=lambda *a, **k: order.append("heard")
                               or ("uh, hi", "en")):
            pipeline = worker.Pipeline(self.conf)
            with mock.patch.object(api, "cleanup", return_value="Hi."), \
                    mock.patch.object(paste, "copy"), mock.patch.object(paste, "press"), \
                    contextlib.redirect_stderr(io.StringIO()):
                pipeline._work(self.wav, 2.0, self.rms, False, None, None, None, profile=lookup)
        self.assertEqual(order, ["heard", "profile"])

    def test_a_failure_before_cleanup_still_resolves_the_profile(self):
        choice = self.profiled(profiles=[{"name": "Email", "apps": ["thunderbird"]}])
        lookup = mock.Mock()
        lookup.result.return_value = choice
        self.run_chain(profile=lookup, transcribe_error=api.ApiError("bad key"))
        self.assertEqual(cfg.read_history()[0]["profile"], "Email")

    def test_a_skipped_lookup_names_no_program(self):
        self.conf["profiles"] = [{"name": "Email", "apps": []}]
        self.run_chain(profile=profiles.choose(self.conf, ""))
        row = cfg.read_history()[0]
        self.assertEqual(row["profile"], "")
        self.assertNotIn("program", row)

    def test_without_profiles_the_row_is_the_row_it_always_was(self):
        self.run_chain(profile=profiles.choose(self.conf, "firefox"))
        row = cfg.read_history()[0]
        for key in ("profile", "program", "translate_to", "translated"):
            self.assertNotIn(key, row)

    def test_a_translation_keeps_the_original_beside_it(self):
        choice = self.profiled(profile_default={"translate_to": "de"})
        run = self.run_chain(profile=choice, cleaned="Buch es für Donnerstag.")
        self.assertIn("Translating into German…", run["stages"])
        self.assertIn("German", run["cleanup"].call_args.args[3])
        run["press"].assert_called_once()
        row = cfg.read_history()[0]
        self.assertEqual(row["raw"], "uh, book it for Thursday")
        self.assertEqual(row["text"], "Buch es für Donnerstag.")
        self.assertEqual((row["translate_to"], row["translated"]), ("de", True))

    def test_a_failed_translation_copies_the_original_and_pastes_nothing(self):
        choice = self.profiled(profile_default={"translate_to": "de"})
        run = self.run_chain(profile=choice,
                             cleanup_error=api.ApiError("The model is down."))
        run["copy"].assert_called_once_with("uh, book it for Thursday")
        run["press"].assert_not_called()
        raw, text, warning, _ = run["done"][0]
        self.assertEqual(text, raw)
        self.assertIn("Translation failed", warning)
        self.assertIn("The model is down.", warning)
        row = cfg.read_history()[0]
        self.assertIs(row["translated"], False)

    def test_translation_runs_even_with_cleanup_off(self):
        self.conf["cleanup_enabled"] = False
        choice = self.profiled(profile_default={"translate_to": "fr"})
        run = self.run_chain(profile=choice, cleaned="Réserve-le pour jeudi.")
        self.assertTrue(run["cleanup"].call_args.args[3].startswith(
            "You translate dictation transcripts"))
        run["copy"].assert_called_once_with("Réserve-le pour jeudi.")

    def test_a_queued_recording_keeps_the_profile_it_was_started_with(self):
        pipeline = worker.Pipeline(self.config())
        seen = []
        choice = self.profiled(profile_default={"translate_to": "de"})
        with mock.patch.object(pipeline, "_work",
                               side_effect=lambda *job: seen.append(job[-2])):
            pipeline.run("first.wav", 1.0, profile=choice)
            pipeline._thread.join(5)
        self.assertEqual(seen, [choice])

    def test_a_failed_row_remembers_its_profile_for_the_retry(self):
        choice = self.profiled(profiles=[{"name": "Email", "apps": ["thunderbird"]}])
        self.run_chain(profile=choice, transcribe_error=api.ApiError("bad key"))
        row = cfg.read_history()[0]
        self.assertTrue(row["failed"])
        self.assertEqual((row["profile"], row["program"]), ("Email", "thunderbird"))
        self.assertTrue(row["profile_id"])
        # The retry runs under the same profile, into the same program.
        again = profiles.again(self.conf, row)
        self.assertEqual((again.name, again.program), ("Email", "thunderbird"))


class DictionaryLearningChain(ChainBase):
    """What the dictionary may learn from, and what it may not."""
    said = "we shipped it to OpenFrame today"
    cleaned = "We shipped it to OpenFrame today."

    def three(self, **kwargs):
        for _ in range(3):
            self.run_chain(transcript=self.said, cleaned=self.cleaned, **kwargs)
        return [r["term"] for r in dictionary.records(self.conf)]

    def test_off_by_default(self):
        self.assertEqual(self.three(), [])

    def test_on_it_learns_from_ordinary_dictations(self):
        self.conf["dictionary_learning"] = True
        self.assertEqual(self.three(), ["OpenFrame"])

    def test_never_from_the_agent_a_warning_or_a_translation(self):
        self.conf["dictionary_learning"] = True
        self.conf["assistant_cleanup"] = True
        self.assertEqual(self.three(ask=True), [])
        self.assertEqual(self.three(paste_error=paste.PasteError("no")), [])
        choice = profiles.snapshot(self.conf, None)._replace(translate_to="de")
        self.assertEqual(self.three(profile=choice), [])

    def test_never_from_an_uncleaned_transcript(self):
        self.conf["dictionary_learning"] = True
        self.conf["cleanup_enabled"] = False
        self.assertEqual(self.three(), [])


class RetriesAnswerNoWaiter(DikteTest):
    """A retry started from the history shares the dictation queue, but a
    terminal waiting on its own dictation must be answered with that one."""

    def setUp(self):
        super().setUp()
        from dikte import app as dikte_module

        class Bare:
            """As much of the application as the two methods touch."""

            state = "idle"
            settings_window = None
            _transcripts_pending = 0
            retry_dictation = dikte_module.Dikte.retry_dictation
            _transcript_settled = dikte_module.Dikte._transcript_settled

            def __init__(self):
                self._queued_retries = collections.deque()
                self.answered, self.homes = [], []
                self.pipeline = mock.Mock()
                self.pipeline.run.return_value = None

            def _set_state(self, state):
                self.state = state

            def _settle(self, _kind, payload):
                self.answered.append(payload)

            def _home_settled(self, _kind, payload):
                self.homes.append(payload)

        self.app = Bare()

    def test_a_dictation_ahead_of_a_retry_is_answered_with_its_own_text(self):
        self.app._transcripts_pending += 1
        self.app._queued_retries.append(False)
        self.app.retry_dictation({"audio": "/kept.wav", "duration": 1})
        self.app._transcript_settled({"text": "mine"})
        self.assertEqual(self.app.answered, [{"text": "mine"}])
        self.app._transcript_settled({"text": "retried"})
        self.assertEqual(self.app.answered, [{"text": "mine"}])
        self.assertEqual(self.app.homes, [{"text": "retried"}])
        self.assertEqual(self.app.state, "idle")

    def test_a_retry_refused_as_a_duplicate_leaves_no_trace(self):
        self.app.pipeline.run.return_value = False
        self.app.retry_dictation({"audio": "/kept.wav", "duration": 1})
        self.assertEqual(self.app._transcripts_pending, 0)
        self.assertEqual(list(self.app._queued_retries), [])


if __name__ == "__main__":
    unittest.main()
