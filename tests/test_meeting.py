"""A two-channel recording turned into minutes.

Attribution is settled by the channels rather than guessed, so the tests care
about what happens at the seams: the microphone picking the other side up
through the speakers, two people talking over each other, and a run that died
after the transcription and must not pay for it twice.
"""

import contextlib
import unittest
import wave
from unittest import mock

from dikte import api
from dikte import config as cfg
from dikte import meeting
from tests.support import DikteTest, make_wav, silence, speech, stereo, tone


def seg(start, end, text, speaker):
    return (start, end, text, speaker)


class SplitChannels(DikteTest):
    def stereo_file(self, left, right, name="meeting.wav"):
        return make_wav(self.path(name), stereo(left, right), channels=2)

    def test_the_two_sides_come_out_as_separate_files(self):
        path = self.stereo_file(tone(1.0, amplitude=16000), silence(1.0))
        mine, theirs = meeting.split_channels(path, self.root)
        for side in (mine, theirs):
            with contextlib.closing(wave.open(side, "rb")) as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getnframes(), 16000)

    def test_the_left_channel_is_mine(self):
        path = self.stereo_file(tone(1.0, amplitude=16000), silence(1.0))
        mine, theirs = meeting.split_channels(path, self.root)
        self.assertGreater(max(meeting.rms_series(mine)), 0.1)
        self.assertEqual(max(meeting.rms_series(theirs)), 0.0)

    def test_a_recording_longer_than_the_read_block(self):
        path = self.stereo_file(tone(3.0), silence(3.0))
        mine, _ = meeting.split_channels(path, self.root)
        with contextlib.closing(wave.open(mine, "rb")) as wav:
            self.assertEqual(wav.getnframes(), 3 * 16000)

    def test_a_dictation_is_not_a_meeting(self):
        path = make_wav(self.path("mono.wav"), silence(1.0))
        with self.assertRaises(api.ApiError):
            meeting.split_channels(path, self.root)


class RmsSeries(DikteTest):
    def test_silence_reads_as_nothing(self):
        path = make_wav(self.path("quiet.wav"), silence(1.0))
        self.assertEqual(set(meeting.rms_series(path)), {0.0})

    def test_a_block_per_level_frame(self):
        path = make_wav(self.path("clip.wav"), silence(1.0))
        self.assertEqual(len(meeting.rms_series(path)),
                         -(-16000 // meeting.LEVEL_FRAMES))

    def test_a_loud_recording_reads_above_zero(self):
        path = make_wav(self.path("loud.wav"), tone(1.0, amplitude=16000))
        self.assertGreater(max(meeting.rms_series(path)), 0.1)

    def test_the_rate_is_read_off_the_file(self):
        path = make_wav(self.path("clip.wav"), silence(0.1), rate=8000)
        self.assertEqual(meeting.wav_rate(path), 8000)


class MergeTurns(unittest.TestCase):
    def test_one_timeline_out_of_two_channels(self):
        turns = meeting.merge_turns([
            seg(5.0, 6.0, "and you?", "theirs"),
            seg(0.0, 1.0, "hello", "mine"),
        ])
        self.assertEqual([(start, speaker) for start, speaker, _ in turns],
                         [(0.0, "mine"), (5.0, "theirs")])

    def test_one_person_carrying_on_stays_one_turn(self):
        turns = meeting.merge_turns([
            seg(0.0, 1.0, "hello", "mine"),
            seg(1.2, 2.0, "how are you", "mine"),
        ])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0][2], "hello how are you")

    def test_a_long_pause_starts_a_new_line(self):
        turns = meeting.merge_turns([
            seg(0.0, 1.0, "hello", "mine"),
            seg(20.0, 21.0, "still there?", "mine"),
        ])
        self.assertEqual(len(turns), 2)

    def test_the_gap_is_measured_from_the_end_of_the_last_words(self):
        turns = meeting.merge_turns([
            seg(0.0, 10.0, "a long sentence", "mine"),
            seg(15.0, 16.0, "and another", "mine"),
        ])
        self.assertEqual(len(turns), 1)

    def test_the_speaker_changing_always_starts_a_new_turn(self):
        turns = meeting.merge_turns([
            seg(0.0, 1.0, "hello", "mine"),
            seg(1.1, 2.0, "hi", "theirs"),
        ])
        self.assertEqual(len(turns), 2)

    def test_my_microphone_hearing_them_through_the_speakers_is_dropped(self):
        turns = meeting.merge_turns([
            seg(0.0, 2.0, "we should ship it on Friday", "theirs"),
            seg(0.1, 2.0, "we should ship it on friday", "mine"),
        ])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0][1], "theirs")

    def test_talking_over_each_other_is_not_an_echo(self):
        turns = meeting.merge_turns([
            seg(0.0, 2.0, "we should ship it on Friday", "theirs"),
            seg(0.1, 2.0, "no, next week is better", "mine"),
        ])
        self.assertEqual(len(turns), 2)

    def test_the_same_sentence_said_later_is_not_an_echo(self):
        turns = meeting.merge_turns([
            seg(0.0, 2.0, "ship it on Friday", "theirs"),
            seg(30.0, 32.0, "ship it on Friday", "mine"),
        ])
        self.assertEqual(len(turns), 2)

    def test_a_side_that_transcribed_to_nothing_is_dropped(self):
        turns = meeting.merge_turns([
            seg(0.0, 1.0, "...", "mine"),
            seg(2.0, 3.0, "hello", "theirs"),
        ])
        self.assertEqual(len(turns), 1)

    def test_nothing_was_said_at_all(self):
        self.assertEqual(meeting.merge_turns([]), [])


class RenderTurns(unittest.TestCase):
    def test_a_stamp_and_a_name_per_line(self):
        text = meeting.render_turns(
            [(0.0, "mine", "hello"), (65.0, "theirs", " hi ")], "Yusuf", "Ayşe")
        self.assertEqual(text, "[00:00] Yusuf: hello\n[01:05] Ayşe: hi")

    def test_nothing_to_render(self):
        self.assertEqual(meeting.render_turns([], "Me", "Them"), "")


class Document(DikteTest):
    def test_a_heading_becomes_the_title(self):
        self.assertEqual(meeting.split_title("# Kickoff\n\nWe agreed."),
                         ("Kickoff", "We agreed."))

    def test_minutes_that_open_with_prose_have_no_title(self):
        self.assertEqual(meeting.split_title("We agreed to ship."),
                         ("", "We agreed to ship."))

    def test_nothing_written(self):
        self.assertEqual(meeting.split_title(""), ("", ""))
        self.assertEqual(meeting.split_title(None), ("", ""))

    def test_the_document_carries_the_title_the_date_and_the_length(self):
        text = meeting.build_document("Kickoff", "2026-08-01 10:00", 3900,
                                      "We agreed.", "[00:00] Me: hello")
        self.assertTrue(text.startswith("# Kickoff"))
        self.assertIn("2026-08-01 10:00", text)
        self.assertIn("1 h 5 min", text)

    def test_the_transcript_can_be_read_back_out(self):
        transcript = "[00:00] Me: hello\n[00:05] Other side: hi"
        text = meeting.build_document("Kickoff", "now", 60, "We agreed.", transcript)
        self.assertEqual(meeting.read_transcript(text), transcript)

    def test_a_document_with_no_minutes_yet_still_gives_its_transcript_back(self):
        transcript = "[00:00] Me: hello"
        text = meeting.build_document("Kickoff", "now", 60, "", transcript)
        self.assertEqual(meeting.read_transcript(text), transcript)

    def test_a_document_written_by_something_else(self):
        self.assertEqual(meeting.read_transcript("# Notes\n\nJust prose."), "")

    def test_the_marker_is_a_comment_so_it_never_renders(self):
        self.assertTrue(meeting.TRANSCRIPT_MARKER.startswith("<!--"))

    def test_the_length_reads_as_minutes_under_an_hour(self):
        self.assertEqual(meeting.length_label(0), "0 min")
        self.assertEqual(meeting.length_label(3540), "59 min")

    def test_the_length_reads_as_hours_past_one(self):
        self.assertEqual(meeting.length_label(3600), "1 h 0 min")
        self.assertEqual(meeting.length_label(7325), "2 h 2 min")

    def test_the_length_is_translated(self):
        self.write_config({"ui_language": "tr"})
        cfg.Config()
        self.assertIn("dk", meeting.length_label(600))


class Entry(unittest.TestCase):
    def test_the_stem_is_a_sortable_timestamp(self):
        base = meeting.new_base()
        self.assertEqual(len(base), 15)
        self.assertEqual(base[8], "-")

    def test_a_fresh_row_reads_its_date_back_out_of_the_stem(self):
        entry = meeting.new_entry("20260801-143000", 125.44)
        self.assertEqual(entry["base"], "20260801-143000")
        self.assertEqual(entry["ts"], "2026-08-01 14:30")
        self.assertEqual(entry["duration"], 125.4)
        self.assertEqual(entry["status"], "recorded")


class Pipeline(DikteTest):
    """The chain, with the transcription and the two cleanup calls faked."""

    def setUp(self):
        super().setUp()
        self.conf = self.config(openrouter_api_key="sk-or-test")
        self.base = "20260801-100000"
        self.doc, self.wav = cfg.meeting_paths(self.base)
        self.wav.parent.mkdir(parents=True, exist_ok=True)
        make_wav(self.wav, stereo(speech(2.0), speech(2.0, freq=220.0)), channels=2)
        cfg.save_meeting(meeting.new_entry(self.base, 1.0))

    def run_pipeline(self, entry=None, segments=None, minutes="# Kickoff\n\nAgreed.",
                     cleanup_fails=False):
        worker = meeting.MeetingPipeline(self.conf)
        done, failures = [], []
        worker.finished.connect(lambda *args: done.append(args))
        worker.failed.connect(lambda *args: failures.append(args))

        def cleanup(text, *args, **kwargs):
            if cleanup_fails:
                raise api.ApiError("OpenRouter is rate limiting you")
            return minutes

        with mock.patch.object(api, "transcribe_segments",
                               return_value=segments or [(0.0, 1.0, "hello")]), \
                mock.patch.object(api, "cleanup", side_effect=cleanup):
            # Run in this thread: the signals would otherwise be queued and
            # never delivered without an event loop.
            worker._work(entry or cfg.read_meetings()[0])
        return done, failures

    def test_a_recording_becomes_a_document(self):
        done, failures = self.run_pipeline()
        self.assertEqual(failures, [])
        self.assertEqual(done[0], (self.base, "Kickoff"))
        self.assertIn("Agreed.", self.doc.read_text(encoding="utf-8"))

    def test_the_row_ends_up_done_with_its_title(self):
        self.run_pipeline()
        row = cfg.read_meetings()[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["title"], "Kickoff")

    def test_the_audio_goes_once_the_minutes_are_written(self):
        self.run_pipeline()
        self.assertFalse(self.wav.exists())

    def test_the_audio_is_kept_when_the_setting_says_so(self):
        self.conf["meeting_keep_audio"] = True
        self.run_pipeline()
        self.assertTrue(self.wav.exists())

    def test_a_failed_run_keeps_the_audio_whatever_the_setting_says(self):
        """It is the only copy of the meeting, and a retry starts from it."""
        _, failures = self.run_pipeline(cleanup_fails=True)
        self.assertTrue(self.wav.exists())
        self.assertEqual(failures[0][0], self.base)
        self.assertEqual(cfg.read_meetings()[0]["status"], "failed")

    def test_a_retry_does_not_transcribe_the_hour_again(self):
        self.conf["meeting_cleanup"] = False
        self.run_pipeline(cleanup_fails=True)
        entry = cfg.read_meetings()[0]
        self.assertEqual(entry["status"], "failed")

        with mock.patch.object(api, "transcribe_segments") as transcribe, \
                mock.patch.object(api, "cleanup", return_value="# Kickoff\n\nAgreed."):
            worker = meeting.MeetingPipeline(self.conf)
            worker._work(dict(entry, status="transcribed"))
        transcribe.assert_not_called()
        self.assertEqual(cfg.read_meetings()[0]["status"], "done")

    def test_a_recording_that_is_gone(self):
        self.wav.unlink()
        _, failures = self.run_pipeline()
        self.assertIn("gone", failures[0][1])

    def test_a_meeting_where_nobody_said_anything(self):
        with mock.patch.object(meeting.MeetingPipeline, "_silent",
                               return_value=True):
            _, failures = self.run_pipeline()
        self.assertIn("speech", failures[0][1])

    def test_the_transcript_is_attributed_by_channel(self):
        self.conf["meeting_self_name"] = "Yusuf"
        self.conf["meeting_other_name"] = "Ayşe"
        self.conf["meeting_cleanup"] = False
        sides = iter([[(0.0, 1.0, "shall we ship it")],
                      [(2.0, 3.0, "next week is better")]])
        with mock.patch.object(api, "transcribe_segments",
                               side_effect=lambda *a, **k: next(sides)), \
                mock.patch.object(api, "cleanup", return_value="# Kickoff\n\nAgreed."):
            meeting.MeetingPipeline(self.conf)._work(cfg.read_meetings()[0])
        transcript = meeting.read_transcript(self.doc.read_text(encoding="utf-8"))
        self.assertEqual(transcript.splitlines(),
                         ["[00:00] Yusuf: shall we ship it",
                          "[00:02] Ayşe: next week is better"])

    def test_minutes_with_no_heading_fall_back_to_a_title(self):
        self.run_pipeline(minutes="We agreed to ship.")
        self.assertEqual(cfg.read_meetings()[0]["title"], "Meeting")

    def test_an_unexpected_error_marks_the_meeting_failed(self):
        self.conf["meeting_cleanup"] = False
        worker = meeting.MeetingPipeline(self.conf)
        done, failures = [], []
        worker.finished.connect(lambda *args: done.append(args))
        worker.failed.connect(lambda *args: failures.append(args))
        with mock.patch.object(api, "transcribe_segments",
                               return_value=[(0.0, 1.0, "hello")]), \
                mock.patch.object(api, "cleanup",
                                  side_effect=ValueError("bad payload")):
            worker._work(cfg.read_meetings()[0])
        self.assertEqual(done, [])
        self.assertEqual(failures[0][0], self.base)
        self.assertIn("Unexpected error", failures[0][1])
        row = cfg.read_meetings()[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("Unexpected error", row["error"])
        self.assertTrue(self.wav.exists())

    def test_a_second_run_while_one_is_going_is_refused(self):
        worker = meeting.MeetingPipeline(self.conf)
        worker._thread = mock.Mock(is_alive=lambda: True)
        self.assertFalse(worker.run({"base": self.base}))


if __name__ == "__main__":
    unittest.main()
