"""Transcribing a file: splitting it, stamping it, and writing subtitles.

to_srt is the awkward one. The text is the authority on wording and the segments
on timing, and they meet at a whole-second stamp that a cleanup model was asked
to leave alone. It has to survive a model that wrapped a line, dropped one, or
made up a stamp nobody recorded.
"""

import contextlib
import os
import time
import unittest
import wave
from unittest import mock

from dikte import api
from dikte import filetranscribe as ft
from tests.support import DikteTest, make_wav, silence, tone


class Timestamps(unittest.TestCase):
    def test_under_an_hour(self):
        self.assertEqual(ft.format_timestamp(0), "00:00")
        self.assertEqual(ft.format_timestamp(65.9), "01:05")
        self.assertEqual(ft.format_timestamp(599), "09:59")

    def test_past_an_hour_the_hours_show(self):
        self.assertEqual(ft.format_timestamp(3600), "1:00:00")
        self.assertEqual(ft.format_timestamp(3725), "1:02:05")

    def test_srt_wants_milliseconds_and_a_comma(self):
        self.assertEqual(ft.srt_timestamp(0), "00:00:00,000")
        self.assertEqual(ft.srt_timestamp(1.5), "00:00:01,500")
        self.assertEqual(ft.srt_timestamp(3725.25), "01:02:05,250")

    def test_a_negative_start_is_pulled_up_to_zero(self):
        self.assertEqual(ft.srt_timestamp(-3), "00:00:00,000")


class ToSrt(unittest.TestCase):
    def test_nothing_to_do(self):
        self.assertEqual(ft.to_srt("", []), "")
        self.assertEqual(ft.to_srt("no stamps here", []), "")

    def test_a_cue_takes_its_timing_from_the_segment_it_came_from(self):
        srt = ft.to_srt("[00:01] Hello there.", [(1.25, 2.75, "hello there")])
        self.assertIn("00:00:01,250 --> 00:00:02,750", srt)
        self.assertIn("Hello there.", srt)

    def test_the_text_wins_on_wording(self):
        """Cleanup edits survive; only the timing comes from the segments."""
        srt = ft.to_srt("[00:01] Hello there.", [(1.0, 2.0, "uh hello uh there")])
        self.assertIn("Hello there.", srt)
        self.assertNotIn("uh", srt)

    def test_cues_are_numbered_from_one(self):
        srt = ft.to_srt("[00:00] One\n[00:02] Two",
                        [(0.0, 1.0, "One"), (2.0, 3.0, "Two")])
        self.assertTrue(srt.startswith("1\n"))
        self.assertIn("\n2\n", srt)

    def test_a_wrapped_line_joins_the_cue_above_it(self):
        srt = ft.to_srt("[00:01] Hello there,\nand welcome.",
                        [(1.0, 4.0, "hello there and welcome")])
        self.assertIn("Hello there, and welcome.", srt)
        self.assertEqual(srt.count(" --> "), 1)

    def test_a_stamp_nobody_recorded_still_gets_timing(self):
        srt = ft.to_srt("[00:05] Invented.", [])
        self.assertIn("00:00:05,000 --> ", srt)

    def test_a_cue_with_no_end_runs_until_the_next_one(self):
        srt = ft.to_srt("[00:00] One\n[00:04] Two", [])
        self.assertIn("00:00:00,000 --> 00:00:04,000", srt)

    def test_the_last_cue_gets_a_minimum_length(self):
        srt = ft.to_srt("[00:10] Last words.", [])
        self.assertIn("00:00:10,000 --> 00:00:11,500", srt)

    def test_a_cue_is_cut_short_when_the_next_one_starts_first(self):
        """Whisper's end times overlap now and then; subtitles must not."""
        srt = ft.to_srt("[00:00] One\n[00:02] Two",
                        [(0.0, 9.0, "One"), (2.0, 3.0, "Two")])
        self.assertIn("00:00:00,000 --> 00:00:02,000", srt)

    def test_blank_lines_and_empty_cues_are_dropped(self):
        srt = ft.to_srt("[00:00] One\n\n[00:02]\n[00:03] Three", [])
        self.assertEqual(srt.count(" --> "), 2)

    def test_the_hour_form_of_a_stamp_is_understood(self):
        srt = ft.to_srt("[1:02:05] Late.", [])
        self.assertIn("01:02:05,000", srt)

    def test_the_file_ends_with_a_newline(self):
        self.assertTrue(ft.to_srt("[00:00] One", []).endswith("\n"))


class SplitText(unittest.TestCase):
    def test_short_text_stays_whole(self):
        self.assertEqual(ft.split_text("hello", False), ["hello"])

    def test_a_long_transcript_is_broken_up(self):
        text = " ".join(["word"] * 8000)
        blocks = ft.split_text(text, False)
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            self.assertLessEqual(len(block), ft.CLEANUP_CHUNK_CHARS)

    def test_nothing_is_lost_in_the_splitting(self):
        text = " ".join(f"word{index}" for index in range(4000))
        self.assertEqual(" ".join(ft.split_text(text, False)), text)

    def test_a_timestamped_transcript_is_never_broken_mid_line(self):
        text = "\n".join(f"[00:{index:02d}] a line of some length here"
                         for index in range(600))
        blocks = ft.split_text(text, True)
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            for line in block.splitlines():
                self.assertTrue(line.startswith("["))

    def test_a_single_line_longer_than_the_limit_is_kept_whole(self):
        text = "x" * (ft.CLEANUP_CHUNK_CHARS + 100)
        self.assertEqual(ft.split_text(text, True), [text])


class SplitWav(DikteTest):
    def wav(self, seconds, name="in.wav"):
        return make_wav(self.path(name), silence(seconds))

    def test_a_short_file_is_handed_back_as_it_is(self):
        path = self.wav(2)
        self.assertEqual(ft.split_wav(path, self.root), [(path, 0.0)])

    def test_a_long_file_is_cut_at_the_chunk_length(self):
        path = self.wav(5)
        chunks = ft.split_wav(path, self.root, 2, overlap=0)
        self.assertEqual([offset for _, offset in chunks], [0, 2, 4])

    def test_the_chunks_add_up_to_the_original(self):
        path = self.wav(5)
        chunks = ft.split_wav(path, self.root, 2, overlap=0)
        total = 0
        for chunk_path, _ in chunks:
            with contextlib.closing(wave.open(chunk_path, "rb")) as wav:
                total += wav.getnframes()
                self.assertEqual(wav.getframerate(), 16000)
        self.assertEqual(total, 5 * 16000)

    def test_the_chunks_do_not_write_over_each_other(self):
        path = self.wav(5)
        chunks = ft.split_wav(path, self.root, 2, overlap=0)
        self.assertEqual(len({chunk for chunk, _ in chunks}), len(chunks))

    def test_a_chunk_starts_inside_the_one_before_it(self):
        """The cut is what makes whisper lose the thread, so nobody hears only
        one side of it."""
        path = self.wav(10)
        chunks = ft.split_wav(path, self.root, 4, overlap=1)
        self.assertEqual([offset for _, offset in chunks], [0, 3, 6])
        with contextlib.closing(wave.open(chunks[1][0], "rb")) as wav:
            self.assertEqual(wav.getnframes(), 4 * 16000)

    def test_an_overlap_is_never_more_than_half_a_chunk(self):
        path = self.wav(10)
        chunks = ft.split_wav(path, self.root, 4, overlap=60)
        self.assertEqual([offset for _, offset in chunks], [0, 2, 4, 6])

    def test_a_tail_the_chunk_before_already_holds_is_not_cut_again(self):
        path = self.wav(9)
        chunks = ft.split_wav(path, self.root, 4, overlap=1)
        # 0-4, 3-7, 6-9: a fourth starting at 9 would be the last second again.
        self.assertEqual([offset for _, offset in chunks], [0, 3, 6])


class Stitch(unittest.TestCase):
    def test_the_first_chunk_is_taken_as_it_is(self):
        segments = [(0.0, 1.0, "one"), (1.0, 2.0, "two")]
        self.assertEqual(ft.stitch([], segments), segments)

    def test_the_cue_the_cut_ran_through_is_replaced(self):
        collected = [(0.0, 4.0, "a whole sentence"), (4.0, 5.0, "cut in ha")]
        incoming = [(3.0, 4.0, "sentence"), (4.0, 6.0, "cut in half")]
        self.assertEqual(ft.stitch(collected, incoming),
                         [(0.0, 4.0, "a whole sentence"), (4.0, 6.0, "cut in half")])

    def test_the_chunk_before_gives_way_where_the_new_telling_starts(self):
        """The two chunks put the sentence in different cues; whichever way they
        fall, nothing is said twice and the cues run forwards."""
        collected = [(0.0, 3.0, "one"), (3.0, 5.0, "two"), (5.0, 6.0, "three cut")]
        incoming = [(2.0, 4.5, "one and two"), (4.5, 7.0, "two and three whole")]
        stitched = ft.stitch(collected, incoming)
        self.assertEqual(stitched, [(0.0, 3.0, "one"), (4.5, 7.0, "two and three whole")])
        for before, after in zip(stitched, stitched[1:]):
            self.assertLessEqual(before[1], after[0])

    def test_a_chunk_with_nothing_in_it_takes_nothing_away(self):
        collected = [(0.0, 4.0, "one")]
        self.assertEqual(ft.stitch(collected, []), collected)

    def test_a_chunk_that_heard_only_what_was_already_heard_adds_nothing(self):
        collected = [(0.0, 4.0, "one"), (4.0, 5.0, "two")]
        self.assertEqual(ft.stitch(collected, [(1.0, 2.0, "one")]), collected)


class ChunkSeconds(DikteTest):
    def file(self, size):
        path = self.path("audio.mp3")
        with open(path, "wb") as fh:
            fh.write(b"\x00" * size)
        return path

    def test_a_file_that_fits_is_not_cut_at_all(self):
        self.assertEqual(ft.chunk_seconds(self.file(1024), 600), 0.0)

    def test_a_file_over_the_limit_is_cut_by_what_it_measured(self):
        # Twice the limit over twenty minutes, so a little under ten fits.
        seconds = ft.chunk_seconds(self.file(ft.UPLOAD_LIMIT * 2), 1200)
        self.assertGreater(seconds, 500)
        self.assertLess(seconds, 600)

    def test_a_chunk_is_never_more_audio_than_a_request_can_outlive(self):
        """An hour in one request is a 502 from the gateway, whatever it weighs."""
        self.assertEqual(ft.chunk_seconds(self.file(ft.UPLOAD_LIMIT * 2), 3600),
                         ft.MAX_CHUNK_SECONDS)

    def test_a_small_file_that_is_still_hours_long_is_cut_on_the_clock(self):
        self.assertEqual(ft.chunk_seconds(self.file(1024), 7200),
                         ft.MAX_CHUNK_SECONDS)

    def test_a_file_short_enough_on_both_counts_is_not_cut(self):
        self.assertEqual(ft.chunk_seconds(self.file(1024), ft.MAX_CHUNK_SECONDS), 0.0)

    def test_a_file_with_no_length_is_left_whole(self):
        self.assertEqual(ft.chunk_seconds(self.file(ft.UPLOAD_LIMIT * 2), 0), 0.0)


class Ffmpeg(DikteTest):
    """How the converter process is started."""

    def test_its_output_is_read_as_utf8_whatever_the_locale_says(self):
        """ffmpeg writes UTF-8; read as the locale codepage its messages
        mojibake, and a byte the codepage cannot place raises from inside
        communicate itself."""
        out = str(self.path("out.wav"))
        with open(out, "wb") as fh:
            fh.write(b"\x00")
        proc = mock.Mock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        proc.poll.return_value = 0
        with mock.patch.object(ft.subprocess, "Popen", return_value=proc) as popen:
            ft._ffmpeg(["-i", "in.mp4", out], out)
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")


class Chunks(DikteTest):
    """What each provider is handed, and in how many pieces."""

    def setUp(self):
        super().setUp()
        self.wav = make_wav(self.path("audio.wav"), silence(3))
        self.worker = ft.FileTranscriber(self.config())

    def target(self, provider):
        return api.Target(provider, provider, "key", "https://example.test", "whisper-1")

    def test_a_model_on_this_machine_is_handed_the_wav(self):
        """Nothing is uploaded, so the encoder would cost quality for nothing."""
        with mock.patch.object(ft, "_to_mp3") as encode:
            chunks = self.worker._chunks(self.wav, self.root, self.target("local"), True)
        self.assertEqual(chunks, [(self.wav, 0.0)])
        encode.assert_not_called()

    def test_a_hosted_model_is_handed_one_mp3(self):
        with mock.patch.object(ft, "_to_mp3", side_effect=lambda p, d, name, *a:
                               make_wav(os.path.join(d, name), silence(1))):
            chunks = self.worker._chunks(self.wav, self.root,
                                         self.target("openrouter"), True)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0][0].endswith("audio.mp3"))

    def test_a_file_too_big_to_upload_is_cut_and_encoded_in_pieces(self):
        wav = make_wav(self.path("long.wav"), silence(120))

        def encode(path, workdir, name, *args):
            # The whole file is over the limit; the pieces are not.
            size = ft.UPLOAD_LIMIT * 2 if name == "audio.mp3" else 1024
            out = os.path.join(workdir, name)
            with open(out, "wb") as fh:
                fh.write(b"\x00" * size)
            return out

        with mock.patch.object(ft, "_to_mp3", side_effect=encode):
            chunks = self.worker._chunks(wav, self.root,
                                         self.target("openrouter"), True)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0][1], 0.0)
        for path, _ in chunks:
            self.assertTrue(path.endswith(".mp3"))


class Transcriber(DikteTest):
    """The chain, with ffmpeg and both API calls faked."""

    def setUp(self):
        super().setUp()
        self.source = make_wav(self.path("input.wav"), tone(1.0))
        self.conf = self.config(openrouter_api_key="sk-or-test")

    def run_chain(self, timestamps=False, cleanup=False, transcript="raw text",
                  segments=None, cleaned="clean text", fail=None):
        worker = ft.FileTranscriber(self.conf)
        done, failures, progress = [], [], []
        worker.finished.connect(lambda *args: done.append(args))
        worker.failed.connect(failures.append)
        worker.progress.connect(progress.append)

        def to_wav(path, workdir, aborter=None):
            return make_wav(self.path("converted.wav"), tone(1.0))

        with mock.patch.object(ft, "_to_wav", side_effect=to_wav), \
                mock.patch.object(ft, "_to_mp3",
                                  side_effect=lambda path, *a, **k: path), \
                mock.patch.object(ft.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(api, "transcribe",
                                  side_effect=fail or (lambda *a, **k: transcript)), \
                mock.patch.object(api, "transcribe_segments",
                                  return_value=segments or [(0.0, 1.0, "raw text")]), \
                mock.patch.object(api, "cleanup", return_value=cleaned) as cleanup_call:
            # The chain is run here rather than through start(): its signals are
            # emitted from the worker thread, and a queued connection would need
            # an event loop to deliver them. This is the same code, one frame down.
            worker._work(self.source, timestamps, cleanup)
        return done, failures, progress, cleanup_call

    def test_plain_text_out(self):
        done, failures, _, _ = self.run_chain()
        self.assertEqual(failures, [])
        self.assertEqual(done[0][0], "raw text")

    def test_cleanup_replaces_the_text(self):
        done, _, _, _ = self.run_chain(cleanup=True)
        self.assertEqual(done[0][0], "clean text")

    def test_cleanup_is_told_it_is_writing_subtitles(self):
        _, _, _, cleanup_call = self.run_chain(cleanup=True)
        prompt = cleanup_call.call_args.args[3]
        self.assertEqual(prompt, self.conf.cleanup_prompt(subtitles=True))

    def test_timestamps_come_back_as_segments_and_as_stamped_lines(self):
        done, _, _, _ = self.run_chain(
            timestamps=True, segments=[(0.0, 1.0, "one"), (2.0, 3.0, "two")])
        text, segments = done[0]
        self.assertEqual(text, "[00:00] one\n[00:02] two")
        self.assertEqual(len(segments), 2)

    def test_no_ffmpeg_installed(self):
        worker = ft.FileTranscriber(self.conf)
        failures = []
        worker.failed.connect(failures.append)
        with mock.patch.object(ft.shutil, "which", return_value=None):
            worker._work(self.source, False, False)
        self.assertIn("ffmpeg", failures[0])

    def test_an_api_failure_is_reported_rather_than_raised(self):
        def boom(*args, **kwargs):
            raise api.ApiError("OpenAI rejected the API key")
        _, failures, _, _ = self.run_chain(fail=boom)
        self.assertIn("rejected", failures[0])

    def test_empty_text_is_not_sent_to_cleanup(self):
        _, _, _, cleanup_call = self.run_chain(cleanup=True, transcript="")
        cleanup_call.assert_not_called()

    def test_a_stopped_run_is_not_a_failure(self):
        def stopped(*args, **kwargs):
            raise api.Aborted
        done, failures, progress, _ = self.run_chain(fail=stopped)
        self.assertEqual(failures, [])
        self.assertEqual(done, [])
        self.assertEqual(progress[-1], "Stopped.")

    def test_the_request_is_handed_the_stop_to_watch(self):
        worker = ft.FileTranscriber(self.conf)
        with mock.patch.object(ft, "_to_wav", side_effect=lambda *a: self.source), \
                mock.patch.object(ft, "_to_mp3",
                                  side_effect=lambda path, *a, **k: path), \
                mock.patch.object(ft.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(api, "transcribe", return_value="text") as call:
            worker._work(self.source, False, False)
        self.assertIs(call.call_args.kwargs["aborter"], worker._abort)

    def test_stopping_a_local_run_stops_the_model_with_it(self):
        """Closing the socket is nothing to a process of ours: it would grind on
        to the end of the chunk with nobody left to hand the answer to."""
        worker = ft.FileTranscriber(self.conf)
        worker._local = mock.Mock()
        worker.stop()
        self.assertTrue(worker._abort.aborted)
        for _ in range(100):
            if worker._local.stop.called:
                break
            time.sleep(0.01)
        worker._local.stop.assert_called_once_with()

    def test_a_run_that_is_over_leaves_the_model_alone(self):
        worker = ft.FileTranscriber(self.conf)
        worker.stop()
        self.assertTrue(worker._abort.aborted)

    def test_a_chunk_is_given_longer_to_answer_than_a_dictation(self):
        """A quarter hour of audio is not a sentence: the default would cut it off."""
        worker = ft.FileTranscriber(self.conf)
        with mock.patch.object(ft, "_to_wav", side_effect=lambda *a: self.source), \
                mock.patch.object(ft, "_to_mp3",
                                  side_effect=lambda path, *a, **k: path), \
                mock.patch.object(ft.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(api, "transcribe", return_value="text") as call:
            worker._work(self.source, False, False)
        self.assertEqual(call.call_args.kwargs["timeout"], ft.HOSTED_TIMEOUT)

    def test_a_gateway_having_a_bad_moment_is_asked_again(self):
        with mock.patch.object(ft.FileTranscriber, "_wait"):
            done, failures, progress, _ = self.run_chain(
                fail=[api.ApiError("HTTP 502: timeout", 502), "raw text"])
        self.assertEqual(failures, [])
        self.assertEqual(done[0][0], "raw text")
        self.assertTrue(any("Trying again" in message for message in progress))

    def test_a_rejected_key_is_not_asked_again(self):
        """Trying again with the same key is only a slower way to fail."""
        call = mock.Mock(side_effect=api.ApiError("rejected the API key", 401))
        with mock.patch.object(ft.FileTranscriber, "_wait"):
            _, failures, _, _ = self.run_chain(fail=call)
        self.assertEqual(call.call_count, 1)
        self.assertIn("rejected", failures[0])

    def test_a_chunk_is_given_up_on_after_the_last_try(self):
        call = mock.Mock(side_effect=api.ApiError("HTTP 502: timeout", 502))
        with mock.patch.object(ft.FileTranscriber, "_wait"):
            _, failures, _, _ = self.run_chain(fail=call)
        self.assertEqual(call.call_count, ft.RETRIES)
        self.assertIn("502", failures[0])

    def test_what_was_heard_before_the_failure_is_still_handed_over(self):
        """An hour already transcribed is not thrown away over the chunk after it."""
        boom = api.ApiError("HTTP 502: timeout", 502)
        with mock.patch.object(ft.FileTranscriber, "_wait"), \
                mock.patch.object(ft.FileTranscriber, "_chunks",
                                  side_effect=lambda wav, *a: [(wav, 0.0), (wav, 10.0)]):
            done, failures, _, _ = self.run_chain(
                fail=["first half"] + [boom] * ft.RETRIES)
        self.assertEqual(done[0][0], "first half")
        self.assertIn("502", failures[0])

    def test_nothing_heard_at_all_is_a_plain_failure(self):
        call = mock.Mock(side_effect=api.ApiError("rejected the API key", 401))
        with mock.patch.object(ft.FileTranscriber, "_wait"):
            done, failures, _, _ = self.run_chain(fail=call)
        self.assertEqual(done, [])
        self.assertEqual(failures[0], "rejected the API key")

    def test_a_second_start_while_one_is_running_is_ignored(self):
        worker = ft.FileTranscriber(self.conf)
        worker._thread = mock.Mock(is_alive=lambda: True)
        self.assertTrue(worker.busy)
        worker.start(self.source, False, False)
        self.assertTrue(worker.busy)


if __name__ == "__main__":
    unittest.main()
