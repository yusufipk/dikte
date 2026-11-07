"""Breaking long subtitle lines into balanced ones."""

import unittest

from dikte import filetranscribe as ft
from dikte import srtwrap


class SplitLine(unittest.TestCase):
    def test_a_short_line_stays_whole(self):
        self.assertEqual(srtwrap.split_line("Hello there."), ["Hello there."])

    def test_a_long_line_becomes_two_balanced_lines(self):
        lines = srtwrap.split_line(
            "Bugün sizlere bilgisayarınızı nasıl hızlandırabileceğinizi anlatacağım")
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 42 for line in lines))
        self.assertLess(abs(len(lines[0]) - len(lines[1])), 15)

    def test_a_break_after_punctuation_is_preferred(self):
        lines = srtwrap.split_line(
            "We waited for hours in the cold rain, and then the doors finally opened")
        self.assertEqual(lines[0], "We waited for hours in the cold rain,")

    def test_very_long_text_takes_three_lines(self):
        lines = srtwrap.split_line(" ".join(["word"] * 20))
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(len(line) <= 42 for line in lines))

    def test_no_line_starts_with_a_lone_dash(self):
        text = "This is a fairly long sentence - and it goes on for quite a while"
        for line in srtwrap.split_line(text):
            self.assertFalse(line.startswith("-"))

    def test_text_without_spaces_is_left_alone(self):
        text = "これは非常に長い日本語の文章でスペースがありませんので分割されません"
        self.assertEqual(srtwrap.split_line(text), [text])

    def test_an_unplaceable_digit_word_does_not_stall_a_long_cue(self):
        """A digit run too long for any line used to send the search through
        every line count, which on a long cue took minutes."""
        import time
        start = time.monotonic()
        lines = srtwrap.split_line("9" * 43 + " " + "word " * 400)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(lines[0], "9" * 43)

    def test_tags_do_not_count_toward_the_length(self):
        self.assertEqual(srtwrap.visible_length("<i>abc</i>"), 3)


class Wrap(unittest.TestCase):
    def test_short_text_comes_back_unchanged(self):
        self.assertEqual(srtwrap.wrap("Short\nlines"), "Short\nlines")

    def test_a_long_line_and_a_stub_are_rebalanced(self):
        text = ("This line is quite a bit longer than forty two characters\nok")
        lines = srtwrap.wrap(text).split("\n")
        self.assertEqual(len(lines), 2)
        self.assertNotEqual(lines[1], "ok")


class ToSrtWrap(unittest.TestCase):
    LONG = "[00:01] Bugün sizlere bilgisayarınızı nasıl hızlandırabileceğinizi anlatacağım"
    SEGMENTS = [(1.0, 5.0, "x")]

    def test_off_by_default(self):
        srt = ft.to_srt(self.LONG, self.SEGMENTS)
        self.assertEqual(len(srt.strip().split("\n")), 3)

    def test_on_breaks_the_cue_but_keeps_its_timing(self):
        srt = ft.to_srt(self.LONG, self.SEGMENTS, wrap=True)
        lines = srt.strip().split("\n")
        self.assertEqual(lines[1], "00:00:01,000 --> 00:00:05,000")
        self.assertEqual(len(lines), 4)
        self.assertEqual(" ".join(lines[2:]), self.LONG[8:])
