"""The silence check, which decides whether a recording is worth an API call."""

import unittest

from dikte import vad
from tests.support import DikteTest

CHUNK = 1024 / 16000  # what worker.py feeds it: one chunk of the level meter


def levels(*, quiet=0.0005, loud=0.2, quiet_chunks=40, loud_chunks=20):
    """A recording as its per-chunk RMS values: a noise floor, then speech."""
    return [quiet] * quiet_chunks + [loud] * loud_chunks


class ToDb(unittest.TestCase):
    def test_silence_does_not_take_the_logarithm_of_zero(self):
        self.assertEqual(vad.to_db(0.0), -120.0)
        self.assertEqual(vad.to_db(-1.0), -120.0)

    def test_full_scale_is_zero_db(self):
        self.assertEqual(vad.to_db(1.0), 0.0)

    def test_half_scale_is_about_minus_six(self):
        self.assertAlmostEqual(vad.to_db(0.5), -6.02, places=2)


class Percentile(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(vad._percentile([], 0.5), 0.0)

    def test_does_not_run_off_the_end(self):
        self.assertEqual(vad._percentile([1, 2, 3], 1.0), 3)

    def test_picks_by_position(self):
        self.assertEqual(vad._percentile([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], 0.1), 1)


class Analyse(unittest.TestCase):
    def test_no_chunks_at_all(self):
        stats = vad.analyse([], CHUNK)
        self.assertEqual(stats["voiced_seconds"], 0.0)
        self.assertEqual(stats["dynamic_db"], 0.0)
        self.assertEqual(stats["speech_db"], -120.0)

    def test_speech_rises_above_the_floor(self):
        stats = vad.analyse(levels(), CHUNK)
        self.assertGreater(stats["speech_db"], stats["noise_db"])
        self.assertGreater(stats["dynamic_db"], 10.0)
        self.assertGreater(stats["voiced_seconds"], 0.3)

    def test_a_flat_recording_has_no_dynamics_and_no_voice(self):
        stats = vad.analyse([0.02] * 60, CHUNK)
        self.assertEqual(stats["dynamic_db"], 0.0)
        self.assertEqual(stats["voiced_seconds"], 0.0)

    def test_voiced_seconds_follow_the_chunk_length(self):
        short = vad.analyse(levels(), CHUNK)
        long = vad.analyse(levels(), CHUNK * 2)
        self.assertAlmostEqual(long["voiced_seconds"], short["voiced_seconds"] * 2)

    def test_a_wider_margin_counts_fewer_chunks_as_voice(self):
        wide = vad.analyse(levels(loud=0.005), CHUNK, margin_db=40.0)
        narrow = vad.analyse(levels(loud=0.005), CHUNK, margin_db=3.0)
        self.assertLess(wide["voiced_seconds"], narrow["voiced_seconds"])


class IsSilent(unittest.TestCase):
    def test_speech_passes(self):
        self.assertFalse(vad.is_silent(vad.analyse(levels(), CHUNK)))

    def test_everything_below_the_absolute_floor(self):
        stats = vad.analyse([0.00001] * 60, CHUNK)
        self.assertTrue(vad.is_silent(stats))

    def test_a_single_loud_chunk_is_not_long_enough_to_be_a_word(self):
        stats = vad.analyse(levels(loud_chunks=1), CHUNK)
        self.assertTrue(vad.is_silent(stats))
        self.assertLess(stats["voiced_seconds"], 0.3)

    def test_steady_hiss_near_the_floor(self):
        # Loud enough to clear the absolute floor, but the level never moves,
        # which is a fan rather than a voice.
        stats = vad.analyse([0.0025] * 60, CHUNK)
        self.assertGreater(stats["speech_db"], -55.0)
        self.assertTrue(vad.is_silent(stats))

    def test_a_level_that_never_moves_is_never_speech_however_loud(self):
        """The floor is the whole recording, so nothing can rise above it.

        This is what settles a flat recording, at any volume: the margin rule
        gets there before the dynamics rule ever does.
        """
        stats = vad.analyse([0.25] * 60, CHUNK)
        self.assertEqual(stats["voiced_seconds"], 0.0)
        self.assertTrue(vad.is_silent(stats))

    def test_flat_dynamics_alone_do_not_reject_a_loud_recording(self):
        stats = {"speech_db": -20.0, "noise_db": -24.0,
                 "dynamic_db": 4.0, "voiced_seconds": 1.0}
        self.assertFalse(vad.is_silent(stats))

    def test_flat_dynamics_do_reject_one_sitting_near_the_floor(self):
        stats = {"speech_db": -50.0, "noise_db": -54.0,
                 "dynamic_db": 4.0, "voiced_seconds": 1.0}
        self.assertTrue(vad.is_silent(stats))

    def test_the_thresholds_are_honoured(self):
        stats = vad.analyse(levels(), CHUNK)
        self.assertTrue(vad.is_silent(stats, silence_db=-1.0))
        self.assertTrue(vad.is_silent(stats, min_voiced_seconds=999.0))


class Hallucinations(DikteTest):
    def test_a_stock_phrase_from_a_short_clip(self):
        self.assertTrue(vad.looks_like_hallucination("Altyazı M.K.", 2.0))
        self.assertTrue(vad.looks_like_hallucination("Thanks for watching!", 2.0))

    def test_the_same_phrase_repeated(self):
        self.assertTrue(vad.looks_like_hallucination(
            "Altyazı M.K. Altyazı M.K. Altyazı M.K.", 3.0))

    def test_a_long_clip_is_believed(self):
        # Somebody who talks for half a minute and lands on the phrase meant it.
        self.assertFalse(vad.looks_like_hallucination("Thanks for watching", 30.0))

    def test_real_speech_is_kept(self):
        self.assertFalse(vad.looks_like_hallucination("Bugün toplantı var.", 2.0))
        self.assertFalse(vad.looks_like_hallucination("Send it on Thursday.", 2.0))

    def test_a_one_word_answer_is_believed(self):
        # Whisper invents both over silence, but people dictate both as whole
        # answers, and losing a real answer costs more than passing a fake one.
        self.assertFalse(vad.looks_like_hallucination("You.", 1.5))
        self.assertFalse(vad.looks_like_hallucination("Bye.", 1.5))

    def test_an_empty_transcript_counts_as_invented(self):
        self.assertTrue(vad.looks_like_hallucination("   ", 2.0))
        self.assertTrue(vad.looks_like_hallucination("...", 2.0))

    def test_matching_ignores_case_punctuation_and_turkish_letters(self):
        for text in ("altyazi mk", "ALTYAZI M.K.", "Altyazı  M.K.!"):
            with self.subTest(text=text):
                self.assertTrue(vad.looks_like_hallucination(text, 2.0))

    def test_the_boundary_is_the_max_duration(self):
        self.assertTrue(vad.looks_like_hallucination("thanks for watching", 6.0))
        self.assertFalse(vad.looks_like_hallucination("thanks for watching", 6.1))


if __name__ == "__main__":
    unittest.main()
