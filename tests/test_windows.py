"""The Windows half: WASAPI arithmetic, the Win32 clipboard, hotkeys, DPAPI.

Two kinds of test in here. The arithmetic runs anywhere, because it is plain
Python and the whole point of it is that it does not drift or lose anything:
turning a sound card's blocks into the 16 kHz mono whisper wants, packing a
clipboard snapshot, working out which keys a combination is. The rest talks to
Windows itself and is marked accordingly.

The drift tests are the ones worth reading. A meeting is two devices with two
clocks, and a converter that rounded a fraction of a sample away per block
would slide the two channels apart over an hour, which is exactly the thing
that makes a channel-split transcript useless.
"""

import array
import ctypes
import os
import sys
import threading
import time
import unittest
import wave
from unittest import mock

from tests.support import DikteTest, windows_only

if sys.platform.startswith("win"):
    from PyQt6.QtWidgets import QApplication

    from platforms.windows import clipboard as win_clipboard
    from platforms.windows import hotkeys as win_hotkeys
    from platforms.windows import resample as win_resample
    from platforms.windows import runtime as win_runtime

    # A signal emitted on a capture thread is queued for the thread its
    # receiver belongs to, and a queued signal needs an event loop to be
    # delivered. There is no window here, only the loop the levels arrive over.
    _app = QApplication.instance() or QApplication([])
else:      # imported so the module loads; nothing below it runs
    _app = None
    win_clipboard = win_hotkeys = win_resample = win_runtime = None

RATE = 16000


# --- turning a device's blocks into whisper's format ------------------------


@windows_only
class Resampling(unittest.TestCase):
    """The rate conversion, which is where a meeting would drift apart."""

    def feed_in_blocks(self, resampler, total, block, value=1000):
        out = 0
        left = total
        while left > 0:
            count = min(block, left)
            out += len(resampler.feed(array.array("h", [value] * count)))
            left -= count
        return out

    def test_forty_eight_to_sixteen_is_exactly_a_third(self):
        resampler = win_resample.Resampler(48000, RATE)
        self.assertEqual(self.feed_in_blocks(resampler, 48000, 1024), RATE)

    def test_a_rate_that_does_not_divide_still_lands_on_the_right_count(self):
        """44100 is 2.75625 source samples per output one, and the fraction is
        what a converter that started each block from zero would throw away."""
        resampler = win_resample.Resampler(44100, RATE)
        self.assertEqual(self.feed_in_blocks(resampler, 44100, 441), RATE)

    def test_ten_seconds_of_it_is_still_exact(self):
        """A tenth of a sample lost per block is 36 seconds lost per hour.

        The accumulator carries its remainder between blocks, so the error does
        not accumulate at all: the count after ten seconds is the count, not
        the count give or take.
        """
        resampler = win_resample.Resampler(44100, RATE)
        self.assertEqual(self.feed_in_blocks(resampler, 44100 * 10, 480),
                         RATE * 10)

    def test_the_block_size_does_not_change_the_answer(self):
        counts = set()
        for block in (64, 441, 1024, 4096):
            resampler = win_resample.Resampler(44100, RATE)
            counts.add(self.feed_in_blocks(resampler, 44100 * 3, block))
        self.assertEqual(counts, {RATE * 3})

    def test_a_device_already_at_sixteen_is_left_alone(self):
        resampler = win_resample.Resampler(RATE, RATE)
        self.assertTrue(resampler.passthrough)
        samples = array.array("h", [1, -2, 3])
        self.assertEqual(list(resampler.feed(samples)), [1, -2, 3])

    def test_downsampling_averages_rather_than_picking_one(self):
        """Point-sampling folds everything above 8 kHz back into the speech
        band, which a transcription model hears as noise."""
        resampler = win_resample.Resampler(48000, RATE)
        out = resampler.feed(array.array("h", [3000, 0, 0] * 4))
        self.assertEqual(list(out), [1000, 1000, 1000, 1000])

    def test_a_device_below_sixteen_is_stretched_rather_than_refused(self):
        resampler = win_resample.Resampler(8000, RATE)
        self.assertEqual(len(resampler.feed(array.array("h", [5] * 800))), 1600)


@windows_only
class Downmixing(unittest.TestCase):
    def test_a_stereo_microphone_becomes_one_channel(self):
        raw = array.array("h", [1000, 2000, -1000, -2000]).tobytes()
        self.assertEqual(list(win_resample.to_mono(raw, 2)), [1500, -1500])

    def test_a_mono_one_is_passed_through(self):
        raw = array.array("h", [7, -7]).tobytes()
        self.assertEqual(list(win_resample.to_mono(raw, 1)), [7, -7])

    def test_a_partial_frame_is_ignored_rather_than_fatal(self):
        raw = array.array("h", [100, 200]).tobytes() + b"\x01"
        self.assertEqual(list(win_resample.to_mono(raw, 2)), [150])

    def test_a_surround_device_is_averaged_across_all_of_it(self):
        raw = array.array("h", [400, 800, 1200, 1600]).tobytes()
        self.assertEqual(list(win_resample.to_mono(raw, 4)), [1000])

    def test_the_converter_does_both_and_hands_back_bytes(self):
        converter = win_resample.Converter(48000, 2, RATE)
        raw = array.array("h", [1000, 2000] * 3).tobytes()   # 3 stereo frames
        out = array.array("h")
        out.frombytes(converter.feed(raw))
        self.assertEqual(list(out), [1500])


# --- the meeting mixer ------------------------------------------------------


class FakeStream:
    """A WASAPI stream handing over blocks, and ending the way a real one does.

    `blocks` bounds how many it gives; after that it either keeps quiet, which
    is what a loopback does while the speakers are idle, or raises, which is
    what a device being unplugged looks like from inside a read.
    """

    def __init__(self, value, block=160, gap=0.005, blocks=None, unplugged=False):
        self.value = value
        self.block = block
        self.gap = gap
        self.left = blocks
        self.unplugged = unplugged
        self.latency = 0.0
        self.closed = False

    def read(self):
        time.sleep(self.gap)
        if self.left is not None:
            if self.left <= 0:
                if self.unplugged:
                    raise OSError("the device went away")
                return b""
            self.left -= 1
        return array.array("h", [self.value] * self.block).tobytes()

    def close(self):
        self.closed = True


def settle(seconds=0.15):
    """Let the queued signals from the capture threads arrive."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        _app.processEvents()
        time.sleep(0.01)
    _app.processEvents()


@windows_only
class MeetingMixing(DikteTest):
    """Two devices, one timeline, and which voice ends up on which channel."""

    def record(self, mic, system, seconds=0.6):
        from platforms.windows import audio as win_audio

        path = str(self.path("meeting.wav"))
        streams = iter([mic, system])
        with mock.patch.object(win_audio, "_microphone",
                               return_value={"name": "mic", "index": 0}), \
                mock.patch.object(win_audio, "_loopbacks",
                                  return_value=[{"name": "out", "index": 1}]), \
                mock.patch.object(win_audio, "_Stream",
                                  side_effect=lambda device: next(streams)):
            recorder = win_audio.MeetingRecorder()
            done, failed = [], []
            recorder.stopped.connect(lambda *args: done.append(args))
            recorder.failed.connect(failed.append)
            recorder.start(path, max_seconds=60)
            time.sleep(seconds)
            recorder.stop()
        settle()
        return path, done, failed

    def channels(self, path):
        with wave.open(path, "rb") as wav:
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getframerate(), RATE)
            samples = array.array("h")
            samples.frombytes(wav.readframes(wav.getnframes()))
        return samples[0::2], samples[1::2]

    def test_the_microphone_is_the_left_channel_and_nothing_of_it_leaks_right(self):
        path, done, failed = self.record(FakeStream(1000), FakeStream(-2000))
        self.assertEqual(failed, [])
        self.assertTrue(done)
        left, right = self.channels(path)
        self.assertEqual(set(left) - {0}, {1000})
        self.assertEqual(set(right) - {0}, {-2000})

    def test_both_channels_hold_the_same_stretch_of_time(self):
        """They are written together or not at all, so neither can slide."""
        path, _, _ = self.record(FakeStream(1000), FakeStream(-2000))
        left, right = self.channels(path)
        self.assertEqual(len(left), len(right))
        self.assertGreater(len(left), RATE // 10)

    def test_a_silent_loopback_does_not_hold_up_the_microphone(self):
        """WASAPI hands over nothing at all while the speakers are idle on some
        machines, and an hour of meeting must not wait on it."""
        path, done, failed = self.record(FakeStream(1000),
                                         FakeStream(0, blocks=0))
        self.assertEqual(failed, [])
        self.assertTrue(done)
        left, right = self.channels(path)
        self.assertEqual(set(left) - {0}, {1000})
        self.assertEqual(set(right), {0})

    def test_the_duration_written_is_the_time_that_passed(self):
        path, done, _ = self.record(FakeStream(1000), FakeStream(-2000),
                                    seconds=0.8)
        _path, seconds = done[0]
        left, _right = self.channels(path)
        self.assertAlmostEqual(len(left) / RATE, seconds, places=3)
        # Written against the wall clock, so what is on disk is how long the
        # meeting ran rather than how many samples a card felt like giving.
        self.assertGreater(seconds, 0.4)
        self.assertLess(seconds, 1.2)

    def test_a_cancelled_meeting_leaves_no_file(self):
        from platforms.windows import audio as win_audio

        path = str(self.path("meeting.wav"))
        streams = iter([FakeStream(1000), FakeStream(-2000)])
        with mock.patch.object(win_audio, "_microphone",
                               return_value={"name": "mic", "index": 0}), \
                mock.patch.object(win_audio, "_loopbacks",
                                  return_value=[{"name": "out", "index": 1}]), \
                mock.patch.object(win_audio, "_Stream",
                                  side_effect=lambda device: next(streams)):
            recorder = win_audio.MeetingRecorder()
            recorder.start(path, max_seconds=60)
            time.sleep(0.2)
            recorder.cancel()
        self.assertFalse(os.path.exists(path))


@windows_only
class Dictation(DikteTest):
    """The microphone on its own, which is what a dictation is."""

    def record(self, stream, seconds=0.5):
        from platforms.windows import audio as win_audio

        with mock.patch.object(win_audio, "_microphone",
                               return_value={"name": "mic", "index": 0}), \
                mock.patch.object(win_audio, "_Stream", return_value=stream):
            recorder = win_audio.Recorder()
            done, failed, levels = [], [], []
            recorder.stopped.connect(lambda *args: done.append(args))
            recorder.failed.connect(failed.append)
            recorder.level.connect(levels.append)
            recorder.start(max_seconds=60)
            time.sleep(seconds)
            recorder.stop()
        settle()
        return done, failed, levels

    def test_a_recording_ends_as_a_wav_at_the_rate_whisper_wants(self):
        done, failed, levels = self.record(FakeStream(8000))
        self.assertEqual(failed, [])
        path, seconds, rms = done[0]
        self.addCleanup(os.unlink, path)
        with wave.open(path, "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getframerate(), RATE)
            self.assertEqual(wav.getsampwidth(), 2)
        self.assertGreater(seconds, 0.2)
        self.assertTrue(rms)
        self.assertGreater(max(levels), 0.0)

    def test_a_stray_keypress_is_not_a_recording(self):
        done, failed, _ = self.record(FakeStream(8000, blocks=1), seconds=0.2)
        self.assertEqual(done, [])
        self.assertIn("0.3", failed[0])

    def test_a_microphone_that_gave_nothing_says_so_rather_than_looking_short(self):
        """Reported as the device going away, not as a recording too short:
        the second one sends the user looking in the wrong place."""
        done, failed, _ = self.record(
            FakeStream(0, blocks=0, unplugged=True), seconds=0.3)
        self.assertEqual(done, [])
        self.assertTrue(any("microphone stopped" in line for line in failed),
                        failed)

    def test_a_cancelled_recording_produces_nothing(self):
        from platforms.windows import audio as win_audio

        with mock.patch.object(win_audio, "_microphone",
                               return_value={"name": "mic", "index": 0}), \
                mock.patch.object(win_audio, "_Stream",
                                  return_value=FakeStream(8000)):
            recorder = win_audio.Recorder()
            done = []
            recorder.stopped.connect(lambda *args: done.append(args))
            recorder.start()
            time.sleep(0.2)
            recorder.cancel()
            recorder.stop()
        self.assertEqual(done, [])

    def test_no_microphone_at_all_names_where_to_pick_one(self):
        from platforms.windows import audio as win_audio

        with mock.patch.object(win_audio, "_microphone", return_value=None):
            recorder = win_audio.Recorder()
            failed = []
            recorder.failed.connect(failed.append)
            recorder.start()
        settle()
        self.assertIn("Settings", failed[0])

    def test_a_device_windows_refuses_points_at_the_privacy_setting(self):
        """A microphone switched off under Privacy fails to open with an access
        error, and the number PortAudio reports for it means nothing to anybody."""
        from platforms.windows import audio as win_audio

        with mock.patch.object(win_audio, "_microphone",
                               return_value={"name": "Mic", "index": 0}), \
                mock.patch.object(win_audio, "_Stream",
                                  side_effect=OSError("[Errno -9996] access denied")):
            recorder = win_audio.Recorder()
            failed = []
            recorder.failed.connect(failed.append)
            recorder.start()
        settle()
        self.assertIn("Privacy", failed[0])


# --- the clipboard ----------------------------------------------------------


@windows_only
class KeyPresses(unittest.TestCase):
    def codes(self, shortcut):
        return win_clipboard.key_events(shortcut)

    def test_down_in_order_then_up_in_reverse(self):
        """Releasing Ctrl before V leaves the application with a plain V."""
        self.assertEqual(self.codes("ctrl+v"),
                         [(0x11, False), (0x56, False),
                          (0x56, True), (0x11, True)])

    def test_three_keys(self):
        self.assertEqual(self.codes("ctrl+shift+v"),
                         [(0x11, False), (0x10, False), (0x56, False),
                          (0x56, True), (0x10, True), (0x11, True)])

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(self.codes(" Ctrl + V "), self.codes("ctrl+v"))

    def test_the_synonyms_land_on_the_same_codes(self):
        self.assertEqual(self.codes("control+insert"), self.codes("ctrl+insert"))
        self.assertEqual(self.codes("super+enter"), self.codes("meta+return"))

    def test_a_key_nobody_mapped_is_refused_before_anything_is_sent(self):
        with self.assertRaises(win_clipboard.PasteError) as caught:
            self.codes("ctrl+f13")
        self.assertIn("f13", str(caught.exception))

    def test_insert_is_sent_as_the_extended_key_it_is(self):
        """Without the flag it arrives as the numeric keypad's 0."""
        self.assertIn(0x2D, win_clipboard.EXTENDED)
        self.assertIn(0x5B, win_clipboard.EXTENDED)


@windows_only
class ClipboardSnapshot(unittest.TestCase):
    """Putting back what was on the clipboard before a dictation replaced it."""

    def test_plain_text_goes_out_and_comes_back_as_itself(self):
        self.assertIsNone(win_clipboard._unpack("günaydın".encode("utf-8")))

    def test_the_formats_survive_the_round_trip(self):
        parts = [("text", "günaydın".encode("utf-8")),
                 ("HTML Format", b"<b>g\xc3\xbcnayd\xc4\xb1n</b>")]
        self.assertEqual(win_clipboard._unpack(win_clipboard._pack(parts)), parts)

    def test_something_that_is_not_a_snapshot_is_treated_as_text(self):
        self.assertIsNone(win_clipboard._unpack(b"\x89PNG\r\n"))

    def test_a_snapshot_that_was_cut_short_is_refused_whole(self):
        """Half a clipboard put back is an invented clipboard."""
        packed = win_clipboard._pack([("text", b"hello")])
        self.assertIsNone(win_clipboard._unpack(packed[:-3]))


@windows_only
class RealClipboard(unittest.TestCase):
    """Against the clipboard this machine actually has."""

    def setUp(self):
        self.before = win_clipboard.read_clipboard()
        self.addCleanup(win_clipboard.copy_bytes, self.before)

    def test_turkish_survives_the_trip_through_utf16(self):
        # A dotted capital I and a dash the Turkish console codepage has never
        # heard of. Both go through UTF-16 on the way in and UTF-8 on the way
        # out, and either conversion is where a dictation would be lost.
        text = "g\u00fcnayd\u0131n, \u0130stanbul \u2014 a\u011f\u0131r"
        win_clipboard.copy(text)
        self.assertEqual(win_clipboard.read_clipboard().decode("utf-8"), text)

    def test_an_empty_string_is_empty_text_rather_than_no_text(self):
        """The two are not the same thing to copy_bytes: nothing to put back
        leaves the clipboard alone, and empty text clears it."""
        win_clipboard.copy("")
        self.assertEqual(win_clipboard.read_clipboard(), b"")

    def test_a_snapshot_restores_the_text_that_was_there(self):
        win_clipboard.copy("what was there before")
        snapshot = win_clipboard.read_clipboard()
        win_clipboard.copy("a dictation")
        win_clipboard.copy_bytes(snapshot)
        self.assertEqual(win_clipboard.read_clipboard().decode("utf-8"),
                         "what was there before")

    def test_a_clipboard_nobody_will_hand_over_is_a_sentence_not_a_hang(self):
        with mock.patch.object(win_clipboard, "_open", return_value=False):
            with self.assertRaises(win_clipboard.PasteError) as caught:
                win_clipboard.copy("hello")
        self.assertIn("clipboard", str(caught.exception))

    def test_restoring_never_raises(self):
        """It runs after the paste went in; failing here must not undo that."""
        with mock.patch.object(win_clipboard, "_open", return_value=False):
            win_clipboard.copy_bytes(b"whatever was there before")


# --- the shortcuts ----------------------------------------------------------


@windows_only
class ParseShortcut(unittest.TestCase):
    def test_the_default(self):
        self.assertEqual(win_hotkeys.parse_shortcut("Ctrl+Space"), ({"ctrl"}, 0x20))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(win_hotkeys.parse_shortcut(" ctrl + SPACE "),
                         ({"ctrl"}, 0x20))

    def test_several_modifiers(self):
        mods, key = win_hotkeys.parse_shortcut("Ctrl+Alt+Shift+D")
        self.assertEqual(mods, {"ctrl", "alt", "shift"})
        self.assertEqual(key, 0x44)

    def test_the_synonyms_land_on_one_name(self):
        self.assertEqual(win_hotkeys.parse_shortcut("Control+Space"),
                         win_hotkeys.parse_shortcut("Ctrl+Space"))
        self.assertEqual(win_hotkeys.parse_shortcut("Meta+Space"),
                         win_hotkeys.parse_shortcut("Super+Space"))

    def test_modifiers_with_no_key(self):
        self.assertEqual(win_hotkeys.parse_shortcut("Ctrl+Alt"), (None, None))

    def test_a_key_nobody_mapped(self):
        self.assertEqual(win_hotkeys.parse_shortcut("Ctrl+F13"), (None, None))

    def test_nothing(self):
        self.assertEqual(win_hotkeys.parse_shortcut(""), (None, None))
        self.assertEqual(win_hotkeys.parse_shortcut(None), (None, None))

    def test_the_flags_are_the_ones_registerhotkey_takes(self):
        mods, _key = win_hotkeys.parse_shortcut("Ctrl+Shift+Space")
        self.assertEqual(win_hotkeys._modifier_flags(mods),
                         win_hotkeys.MOD_CONTROL | win_hotkeys.MOD_SHIFT)

    def test_every_shortcut_the_settings_can_store_is_understood_here(self):
        """A combination typed on Linux has to mean the same key on Windows."""
        import platforms.linux.hotkeys as linux_hotkeys

        for name in linux_hotkeys.KEYS:
            with self.subTest(key=name):
                self.assertIn(name, win_hotkeys.KEYS)


@windows_only
class Registering(DikteTest):
    """What was installed, remembered for the process that did not install it."""

    def setUp(self):
        super().setUp()
        registry = self.path("shortcuts.json")
        self.patch_attr(win_hotkeys, "_registry_path", lambda: registry)

    def test_nothing_is_installed_to_begin_with(self):
        self.assertIsNone(win_hotkeys.shortcut_status(win_hotkeys.DESKTOP_ID))

    def test_installing_one_is_remembered_and_removing_it_forgets_it(self):
        with mock.patch.object(win_hotkeys, "_free", return_value=True):
            ok, _message = win_hotkeys.install_shortcut(
                "Ctrl+Alt+F9", desktop_id=win_hotkeys.CANCEL_DESKTOP_ID)
        self.assertTrue(ok)
        self.assertEqual(
            win_hotkeys.shortcut_status(win_hotkeys.CANCEL_DESKTOP_ID),
            "Ctrl+Alt+F9")
        win_hotkeys.remove_shortcut(win_hotkeys.CANCEL_DESKTOP_ID)
        self.assertIsNone(
            win_hotkeys.shortcut_status(win_hotkeys.CANCEL_DESKTOP_ID))

    def test_each_verb_keeps_its_own(self):
        with mock.patch.object(win_hotkeys, "_free", return_value=True):
            win_hotkeys.install_shortcut("Ctrl+Space",
                                         desktop_id=win_hotkeys.DESKTOP_ID)
            win_hotkeys.install_shortcut("Ctrl+Alt+Space",
                                         desktop_id=win_hotkeys.CANCEL_DESKTOP_ID)
        self.assertEqual(win_hotkeys.shortcut_status(win_hotkeys.DESKTOP_ID),
                         "Ctrl+Space")
        self.assertEqual(
            win_hotkeys.shortcut_status(win_hotkeys.CANCEL_DESKTOP_ID),
            "Ctrl+Alt+Space")

    def test_a_combination_nobody_can_parse_is_refused(self):
        ok, message = win_hotkeys.install_shortcut("Ctrl+F13")
        self.assertFalse(ok)
        self.assertIn("F13", message)

    def test_one_somebody_else_holds_is_refused_and_says_what_to_do(self):
        with mock.patch.object(win_hotkeys, "_free", return_value=False):
            ok, message = win_hotkeys.install_shortcut("Ctrl+Alt+F9")
        self.assertFalse(ok)
        self.assertIn("another application", message)
        self.assertIsNone(win_hotkeys.shortcut_status(win_hotkeys.DESKTOP_ID))

    def test_dikte_s_own_registration_is_not_mistaken_for_a_conflict(self):
        with mock.patch.object(win_hotkeys, "_free", return_value=True):
            win_hotkeys.install_shortcut("Ctrl+Space",
                                         desktop_id=win_hotkeys.DESKTOP_ID)
        with mock.patch.object(win_hotkeys, "_free", return_value=False):
            self.assertEqual(
                win_hotkeys.conflicting_shortcuts("Ctrl+Space",
                                                  win_hotkeys.DESKTOP_ID), [])

    def test_a_conflict_says_windows_will_not_name_the_other_program(self):
        with mock.patch.object(win_hotkeys, "_free", return_value=False):
            clashes = win_hotkeys.conflicting_shortcuts("Ctrl+Alt+F9")
        self.assertEqual(len(clashes), 1)
        self.assertIn("Windows", clashes[0])

    def test_the_desktop_is_called_windows(self):
        self.assertEqual(win_hotkeys.desktop_name(), "Windows")


@windows_only
class LiveHotkey(unittest.TestCase):
    """Against RegisterHotKey itself, on a combination nothing else wants."""

    def test_a_combination_is_taken_and_given_back(self):
        listener = win_hotkeys.WindowsHotkeys()
        self.addCleanup(listener.stop)
        self.assertTrue(listener.start({"toggle": "Ctrl+Alt+Shift+F9"}))
        self.assertEqual(listener.registered(), {"toggle": "Ctrl+Alt+Shift+F9"})
        listener.stop()
        self.assertFalse(listener.running)
        # Given back rather than held: taking it again has to work.
        again = win_hotkeys.WindowsHotkeys()
        self.addCleanup(again.stop)
        self.assertTrue(again.start({"toggle": "Ctrl+Alt+Shift+F9"}))

    def test_an_unparsable_one_is_reported_and_the_rest_go_on(self):
        listener = win_hotkeys.WindowsHotkeys()
        self.addCleanup(listener.stop)
        failures = []
        listener.failed.connect(failures.append)
        self.assertTrue(listener.start({"toggle": "Ctrl+F13",
                                        "ask": "Ctrl+Alt+Shift+F10"}))
        settle()
        self.assertEqual(len(failures), 1)
        self.assertIn("Ctrl+F13", failures[0])
        self.assertEqual(list(listener.registered()), ["ask"])

    def test_nothing_to_register_is_not_a_listener(self):
        listener = win_hotkeys.WindowsHotkeys()
        self.addCleanup(listener.stop)
        self.assertFalse(listener.start({"toggle": "", "ask": ""}))

    def test_a_second_listener_cannot_take_what_the_first_one_holds(self):
        """Which is the whole conflict report Windows offers."""
        first = win_hotkeys.WindowsHotkeys()
        self.addCleanup(first.stop)
        self.assertTrue(first.start({"toggle": "Ctrl+Alt+Shift+F8"}))
        second = win_hotkeys.WindowsHotkeys()
        self.addCleanup(second.stop)
        failures = []
        second.failed.connect(failures.append)
        self.assertFalse(second.start({"toggle": "Ctrl+Alt+Shift+F8"}))
        settle()
        self.assertTrue(failures)
        self.assertIn("already taken", failures[0])


# --- directories, secrets, processes ----------------------------------------


@windows_only
class Directories(unittest.TestCase):
    def test_settings_roam_and_everything_large_does_not(self):
        self.assertEqual(win_runtime.config_dir().name, "Dikte")
        self.assertEqual(win_runtime.data_dir().name, "Dikte")
        self.assertNotEqual(win_runtime.config_home(), win_runtime.data_home())

    def test_the_cache_sits_under_the_data_directory(self):
        self.assertEqual(win_runtime.cache_dir().parent, win_runtime.data_dir())

    def test_the_directories_are_named_the_way_explorer_shows_them(self):
        self.assertEqual(win_runtime.RECORDINGS_NAME, "Recordings")
        self.assertEqual(win_runtime.MEETINGS_NAME, "Meetings")

    def test_a_packaged_app_can_find_programs_shipped_beside_it(self):
        executable = str(win_runtime.data_dir() / "Dikte" / "dikte.exe")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", executable), \
                mock.patch.dict(os.environ, {"PATH": r"C:\Windows"}):
            win_runtime._expose_bundled_programs()
            first = os.environ["PATH"].split(os.pathsep)[0]
        self.assertEqual(os.path.normcase(first),
                         os.path.normcase(os.path.dirname(executable)))


@windows_only
@unittest.skipUnless(win_runtime and win_runtime.dpapi_available(),
                     "this Windows login has no loaded DPAPI profile")
class Secrets(unittest.TestCase):
    """DPAPI, which is what keeps an API key on a filesystem with no modes."""

    def test_a_key_comes_back_out_of_what_was_stored(self):
        stored = win_runtime.protect("sk-secret-key")
        self.assertEqual(win_runtime.unprotect(stored), "sk-secret-key")

    def test_what_is_stored_is_not_the_key(self):
        stored = win_runtime.protect("sk-secret-key")
        self.assertNotIn("sk-secret-key", stored)
        self.assertTrue(stored.startswith(win_runtime.SECRET_PREFIX))

    def test_turkish_and_the_long_ones_survive(self):
        for secret in ("ağırbaşlı-anahtar", "sk-" + "x" * 400, "a"):
            with self.subTest(secret=secret[:12]):
                self.assertEqual(
                    win_runtime.unprotect(win_runtime.protect(secret)), secret)

    def test_an_empty_key_stays_empty_rather_than_becoming_ciphertext(self):
        self.assertEqual(win_runtime.protect(""), "")
        self.assertEqual(win_runtime.unprotect(""), "")

    def test_a_key_written_in_plain_text_is_read_as_it_stands(self):
        self.assertEqual(win_runtime.unprotect("sk-written-by-hand"),
                         "sk-written-by-hand")

    def test_ciphertext_from_another_account_reads_as_missing(self):
        self.assertEqual(win_runtime.unprotect("dpapi:1:bm90IG1pbmU="), "")

    def test_and_so_does_something_that_is_not_even_base64(self):
        self.assertEqual(win_runtime.unprotect("dpapi:1:not base64 at all"), "")

    def test_two_encryptions_of_one_key_are_not_the_same_bytes(self):
        """Or the file would tell anyone reading it when a key was reused."""
        self.assertNotEqual(win_runtime.protect("sk-secret-key"),
                            win_runtime.protect("sk-secret-key"))


@windows_only
class Identity(unittest.TestCase):
    def test_the_user_is_the_same_user_every_time(self):
        self.assertEqual(win_runtime.user_id(), win_runtime.user_id())

    def test_it_is_a_name_a_pipe_can_carry(self):
        found = win_runtime.user_id()
        self.assertTrue(found)
        self.assertTrue(all(char.isalnum() for char in found))

    def test_a_second_instance_finds_the_first_one_holding_the_mutex(self):
        """Two servers can bind one pipe name on Windows, so the pipe cannot
        answer this and the mutex has to."""
        name = "dikte-test-" + win_runtime.user_id()
        first = win_runtime.single_instance(name)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(win_runtime.single_instance(name))
        finally:
            first.release()
        # Released, so the next one gets it: this is the restart path.
        second = win_runtime.single_instance(name)
        self.assertIsNotNone(second)
        second.release()


@windows_only
class Processes(unittest.TestCase):
    def test_this_process_is_found_by_what_is_on_its_command_line(self):
        self.assertTrue(win_runtime.is_our_process(os.getpid(), ["python"])
                        or win_runtime.is_our_process(os.getpid(), ["Python"]))

    def test_something_that_is_not_there_is_not_ours(self):
        self.assertFalse(win_runtime.is_our_process(os.getpid(), ["not-a-server"]))

    def test_a_pid_nothing_is_using_is_not_ours_either(self):
        self.assertFalse(win_runtime.is_our_process(0x7FFFFFF0, ["python"]))

    def test_subprocesses_are_asked_for_without_a_console_window(self):
        self.assertIn("creationflags", win_runtime.NO_WINDOW)

    def test_a_child_can_be_tied_to_this_process(self):
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                **win_runtime.NO_WINDOW)
        try:
            self.assertTrue(win_runtime.adopt(proc))
            self.assertTrue(win_runtime.terminate(proc.pid))
            self.assertIsNotNone(proc.wait(timeout=10))
        finally:
            if proc.poll() is None:
                proc.kill()


@windows_only
class Sockets(unittest.TestCase):
    def test_a_blocked_read_is_ended_rather_than_left_waiting(self):
        """Winsock does not promise a shutdown reaches a recv already in flight,
        so the handle underneath it is closed."""
        import socket

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        client = socket.create_connection(server.getsockname())
        accepted, _ = server.accept()
        self.addCleanup(accepted.close)

        result = []

        def read():
            try:
                result.append(client.recv(16))
            except OSError as exc:
                result.append(exc)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        time.sleep(0.2)
        win_runtime.abort_socket(client)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
