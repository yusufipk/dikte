"""Level metering, the WAV writer, and what pactl is asked for.

The device list is where a platform port lands first, so the parsing is pinned
here: a source that is not a monitor is an input, one that is belongs to the
speakers, and neither list may go missing when pactl is absent.
"""

import array
import contextlib
import io
import json
import os
import subprocess
import unittest
import wave
from unittest import mock

import sys

import audio
from tests.support import (
    DikteTest,
    FakeCompleted,
    linux_only,
    macos_only,
    only_these_tools,
    pcm,
    silence,
    stereo,
    tone,
)


class ChunkLevels(unittest.TestCase):
    def test_silence(self):
        self.assertEqual(audio.chunk_levels(silence(0.1)), (0.0, 0.0))

    def test_nothing_at_all(self):
        self.assertEqual(audio.chunk_levels(b""), (0.0, 0.0))

    def test_half_a_sample_is_not_a_sample(self):
        self.assertEqual(audio.chunk_levels(b"\x00"), (0.0, 0.0))

    def test_an_odd_trailing_byte_is_ignored_rather_than_fatal(self):
        peak, _ = audio.chunk_levels(pcm([16384, 16384]) + b"\x7f")
        self.assertAlmostEqual(peak, 0.5, places=3)

    def test_the_peak_is_the_loudest_sample_either_way(self):
        peak, _ = audio.chunk_levels(pcm([0, 0, -32768, 100]))
        self.assertEqual(peak, 1.0)

    def test_the_rms_of_a_constant_signal_is_that_constant(self):
        _, rms = audio.chunk_levels(pcm([16384] * 100))
        self.assertAlmostEqual(rms, 0.5, places=3)

    def test_the_rms_sits_below_the_peak_for_a_tone(self):
        peak, rms = audio.chunk_levels(tone(0.1, amplitude=16384))
        self.assertLess(rms, peak)
        self.assertGreater(rms, 0.0)

    def test_neither_number_ever_passes_one(self):
        peak, rms = audio.chunk_levels(pcm([-32768] * 100))
        self.assertEqual(peak, 1.0)
        self.assertEqual(rms, 1.0)


class StereoLevels(unittest.TestCase):
    def test_the_channels_are_read_apart(self):
        left, right = audio.stereo_levels(stereo(pcm([16384] * 50),
                                                 pcm([0] * 50)))
        self.assertAlmostEqual(left, 0.5, places=3)
        self.assertEqual(right, 0.0)

    def test_nothing_at_all(self):
        self.assertEqual(audio.stereo_levels(b""), (0.0, 0.0))

    def test_a_partial_frame_is_ignored(self):
        self.assertEqual(audio.stereo_levels(b"\x00\x01\x00"), (0.0, 0.0))

    def test_a_meeting_with_both_sides_talking(self):
        left, right = audio.stereo_levels(stereo(pcm([8192] * 50),
                                                 pcm([-16384] * 50)))
        self.assertAlmostEqual(left, 0.25, places=3)
        self.assertAlmostEqual(right, 0.5, places=3)


class WriteWav(DikteTest):
    def test_the_header_says_what_the_recorder_captured(self):
        path = audio.write_wav(silence(0.5))
        self.addCleanup(os.unlink, path)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            self.assertEqual(wav.getnchannels(), audio.CHANNELS)
            self.assertEqual(wav.getsampwidth(), audio.SAMPLE_WIDTH)
            self.assertEqual(wav.getframerate(), audio.RATE)
            self.assertEqual(wav.getnframes(), int(audio.RATE * 0.5))

    def test_the_samples_survive(self):
        path = audio.write_wav(pcm([1000, -1000, 2000]))
        self.addCleanup(os.unlink, path)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            samples = array.array("h")
            samples.frombytes(wav.readframes(3))
        self.assertEqual(list(samples), [1000, -1000, 2000])

    def test_a_meeting_is_written_at_two_channels(self):
        path = audio.write_wav(stereo(silence(0.1), silence(0.1)), channels=2)
        self.addCleanup(os.unlink, path)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            self.assertEqual(wav.getnchannels(), 2)


SOURCES = [
    {"name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
     "description": "Built-in Audio Analog Stereo"},
    {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
     "description": "Monitor of Built-in Audio"},
    {"name": "bluez_input.AA_BB.headset", "description": ""},
]


@linux_only
class Devices(DikteTest):
    @contextlib.contextmanager
    def pactl(self, sources=None, sink=None, tools=("pactl",)):
        payloads = {
            "list": FakeCompleted(stdout=json.dumps(
                SOURCES if sources is None else sources)),
            "get-default-sink": FakeCompleted(stdout=(sink or "") + "\n"),
        }

        def run(cmd, **kwargs):
            return payloads["get-default-sink" if "get-default-sink" in cmd
                            else "list"]

        with only_these_tools(*tools), \
                mock.patch.object(subprocess, "run", side_effect=run):
            yield

    def test_no_pactl_installed(self):
        with only_these_tools():
            self.assertEqual(audio.list_sources(), [])
            self.assertEqual(audio.list_monitors(), [])
            self.assertEqual(audio.default_monitor(), "")

    def test_inputs_leave_the_monitors_out(self):
        with self.pactl():
            names = [name for name, _ in audio.list_sources()]
        self.assertEqual(names, [SOURCES[0]["name"], SOURCES[2]["name"]])

    def test_monitors_are_the_other_half(self):
        with self.pactl():
            self.assertEqual([name for name, _ in audio.list_monitors()],
                             [SOURCES[1]["name"]])

    def test_a_device_with_no_description_is_shown_by_its_name(self):
        with self.pactl():
            sources = dict(audio.list_sources())
        self.assertEqual(sources[SOURCES[2]["name"]], SOURCES[2]["name"])

    def test_pactl_output_that_is_not_json(self):
        with only_these_tools("pactl"), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(stdout="not json")):
            self.assertEqual(audio.list_sources(), [])

    def test_pactl_that_will_not_run(self):
        with only_these_tools("pactl"), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(audio.list_sources(), [])

    def test_pactl_that_exits_non_zero(self):
        with only_these_tools("pactl"), \
                mock.patch.object(subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(1, "pactl")):
            self.assertEqual(audio.list_sources(), [])

    def test_the_default_output_is_found_by_its_monitor(self):
        with self.pactl(sink="alsa_output.pci-0000_00_1f.3.analog-stereo"):
            self.assertEqual(audio.default_monitor(),
                             SOURCES[1]["name"])

    def test_a_default_sink_with_no_monitor_of_its_own(self):
        with self.pactl(sink="alsa_output.usb-something"):
            self.assertEqual(audio.default_monitor(), "")

    def test_no_default_sink_at_all(self):
        with self.pactl(sink=""):
            self.assertEqual(audio.default_monitor(), "")

    def test_a_monitor_is_trusted_when_the_list_is_empty(self):
        """pactl answered about the sink but not about the sources."""
        with self.pactl(sources=[], sink="alsa_output.usb-something"):
            self.assertEqual(audio.default_monitor(),
                             "alsa_output.usb-something.monitor")


# What ffmpeg prints when it is asked to list avfoundation's devices: the video
# ones first, then the audio ones, all of it on stderr behind a prefix, and the
# whole thing ending in the error that made it print at all.
AVFOUNDATION_LISTING = """\
[AVFoundation indev @ 0x14f605010] AVFoundation video devices:
[AVFoundation indev @ 0x14f605010] [0] FaceTime HD Camera
[AVFoundation indev @ 0x14f605010] [1] Capture screen 0
[AVFoundation indev @ 0x14f605010] AVFoundation audio devices:
[AVFoundation indev @ 0x14f605010] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x14f605010] [1] Someone's iPhone Microphone
[in#0 @ 0x14f604e00] Error opening input: Input/output error
Error opening input file .
"""


@macos_only
class MacDevices(DikteTest):
    """The same list, read out of ffmpeg's complaints instead of pactl's JSON."""

    @contextlib.contextmanager
    def ffmpeg(self, stderr=AVFOUNDATION_LISTING, tools=("ffmpeg",)):
        with only_these_tools(*tools), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(returncode=1,
                                                             stderr=stderr)):
            yield

    def test_no_ffmpeg_installed(self):
        with only_these_tools():
            self.assertEqual(audio.list_sources(), [])

    def test_the_audio_devices_are_the_ones_listed(self):
        """The video half is on the same stderr, under its own heading."""
        with self.ffmpeg():
            names = [name for name, _ in audio.list_sources()]
        self.assertEqual(names, ["MacBook Pro Microphone",
                                 "Someone's iPhone Microphone"])

    def test_the_name_is_shown_as_well_as_stored(self):
        """macOS has no second, friendlier name to put beside it."""
        with self.ffmpeg():
            self.assertTrue(all(name == shown
                                for name, shown in audio.list_sources()))

    def test_ffmpeg_saying_something_else_entirely(self):
        with self.ffmpeg(stderr="ffmpeg: command not understood\n"):
            self.assertEqual(audio.list_sources(), [])

    def test_ffmpeg_that_will_not_run(self):
        with only_these_tools("ffmpeg"), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(audio.list_sources(), [])

    def test_there_is_no_speaker_output_to_offer(self):
        """macOS hands out no loopback device, so a meeting cannot be recorded
        and the tab says so rather than listing something that will not work."""
        with self.ffmpeg():
            self.assertEqual(audio.list_monitors(), [])
            self.assertEqual(audio.default_monitor(), "")

    def test_a_meeting_says_what_is_missing_rather_than_failing_oddly(self):
        recorder = audio.MeetingRecorder()
        failures = []
        recorder.failed.connect(failures.append)
        recorder.start(self.path("meeting.wav"))
        self.assertIn("macOS", failures[0])
        self.assertFalse(recorder.active)


@macos_only
class MacRecordingCommand(DikteTest):
    """ffmpeg is the recorder here; there is no sound server to ask."""

    def test_nothing_to_record_with(self):
        with only_these_tools():
            self.assertEqual(audio.recording_command(), [])

    def test_what_to_install_names_homebrew(self):
        self.assertIn("brew install ffmpeg", audio.missing_recorder_message())

    def test_the_format_is_the_one_the_rest_of_the_code_expects(self):
        with only_these_tools("ffmpeg"):
            cmd = audio.recording_command()
        self.assertEqual(cmd[cmd.index("-f") + 1], "avfoundation")
        self.assertEqual(cmd[cmd.index("-ar") + 1], str(audio.RATE))
        self.assertEqual(cmd[cmd.index("-ac") + 1], str(audio.CHANNELS))
        self.assertEqual(cmd[-2:], ["s16le", "-"])

    def test_the_video_half_of_the_input_is_left_empty(self):
        """avfoundation takes `video:audio`, and asking for a camera as well
        would open one and record nothing extra worth having."""
        with only_these_tools("ffmpeg"):
            cmd = audio.recording_command("Some Microphone")
        self.assertEqual(cmd[cmd.index("-i") + 1], ":Some Microphone")


# The program the recorder shells out to on this platform, which is the one
# only_these_tools() has to leave standing for recording_command() to find.
RECORDER = "ffmpeg" if sys.platform == "darwin" else "pw-record"


class Trickle(io.BytesIO):
    """A pipe that hands over less than it was asked for, as ffmpeg's does.

    `read(n)` on an unbuffered pipe returns what has arrived rather than what
    was wanted, and the amount is nobody's to promise. A recorder that took
    each piece for a chunk would count a two-second recording as fifteen
    seconds of level readings.
    """

    def __init__(self, data, piece=100):
        super().__init__(data)
        self.piece = piece

    def read(self, size=-1):
        return super().read(size if size < 0 else min(size, self.piece))


class FakeProcess:
    """A recorder that hands over a fixed buffer and then ends."""

    def __init__(self, data, stream=None):
        self.stdout = (stream or io.BytesIO)(data)
        self.stderr = io.BytesIO(b"")
        self.signals = []
        self.returncode = 0
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def send_signal(self, sig):
        self.signals.append(sig)
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self._alive = False


@linux_only
class RecordingCommand(DikteTest):
    """Which program captures the microphone, and how it is asked to."""

    def test_parec_is_preferred(self):
        """It speaks to PulseAudio and to PipeWire's compatibility service, so
        it is the one that works on both desktops."""
        with only_these_tools("parec", "pw-record"):
            self.assertEqual(audio.recording_command()[0], "parec")

    def test_pw_record_is_the_fallback(self):
        with only_these_tools("pw-record"):
            self.assertEqual(audio.recording_command()[0], "pw-record")

    def test_neither_is_installed(self):
        with only_these_tools():
            self.assertEqual(audio.recording_command(), [])

    def test_both_capture_the_format_the_rest_of_the_code_expects(self):
        for tool in ("parec", "pw-record"):
            with self.subTest(tool=tool), only_these_tools(tool):
                cmd = audio.recording_command()
                joined = " ".join(cmd)
                self.assertIn(str(audio.RATE), joined)
                self.assertIn(str(audio.CHANNELS), joined)
                self.assertIn("s16", joined)

    def test_parec_is_asked_for_the_level_meter_s_own_chunk(self):
        """Left alone it buffers about two seconds, which the waveform shows as
        a still bar that jumps once a second, and which can cost the tail of a
        recording when the process is asked to stop."""
        with only_these_tools("parec"):
            self.assertIn(f"--latency-msec={audio.CHUNK_LATENCY_MS}",
                          audio.recording_command())

    def test_the_latency_asked_for_is_the_chunk_the_meter_reads(self):
        self.assertEqual(audio.CHUNK_LATENCY_MS,
                         round(audio.CHUNK_FRAMES / audio.RATE * 1000))

    def test_a_chosen_microphone_reaches_either_one(self):
        with only_these_tools("parec"):
            self.assertIn("--device=alsa_input.usb", audio.recording_command(
                "alsa_input.usb"))
        with only_these_tools("pw-record"):
            self.assertIn("--target=alsa_input.usb", audio.recording_command(
                "alsa_input.usb"))

    def test_no_microphone_named_means_no_device_flag(self):
        for tool, flag in (("parec", "--device="), ("pw-record", "--target=")):
            with self.subTest(tool=tool), only_these_tools(tool):
                self.assertFalse([arg for arg in audio.recording_command()
                                  if arg.startswith(flag)])


class RecorderChain(DikteTest):
    """Start to WAV, with the platform's recorder faked out."""

    def record(self, data, target="", max_seconds=300, stream=None):
        recorder = audio.Recorder()
        results = []
        failures = []
        recorder.stopped.connect(lambda *args: results.append(args))
        recorder.failed.connect(failures.append)
        proc = FakeProcess(data, stream=stream)
        with only_these_tools(RECORDER), \
                mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            recorder.start(target=target, max_seconds=max_seconds)
            recorder._thread.join(timeout=5)
            recorder.stop()
        return recorder, results, failures, popen

    @linux_only
    def test_the_capture_format_is_what_the_rest_of_the_code_expects(self):
        _, _, _, popen = self.record(silence(1.0))
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], "pw-record")
        self.assertIn(f"--rate={audio.RATE}", cmd)
        self.assertIn(f"--channels={audio.CHANNELS}", cmd)
        self.assertIn("--format=s16", cmd)
        self.assertEqual(cmd[-1], "-")

    @linux_only
    def test_no_target_means_no_target_flag(self):
        _, _, _, popen = self.record(silence(0.5))
        self.assertFalse([arg for arg in popen.call_args.args[0]
                          if arg.startswith("--target=")])

    @linux_only
    def test_a_chosen_microphone_is_passed_on(self):
        _, _, _, popen = self.record(silence(0.5), target="alsa_input.usb")
        self.assertIn("--target=alsa_input.usb", popen.call_args.args[0])

    @macos_only
    def test_the_capture_format_ffmpeg_is_asked_for(self):
        _, _, _, popen = self.record(silence(1.0))
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("avfoundation", cmd)
        self.assertEqual(cmd[cmd.index("-ar") + 1], str(audio.RATE))
        self.assertEqual(cmd[cmd.index("-ac") + 1], str(audio.CHANNELS))
        self.assertIn("s16le", cmd)
        self.assertEqual(cmd[-1], "-")

    @macos_only
    def test_no_microphone_named_means_the_system_default(self):
        _, _, _, popen = self.record(silence(0.5))
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-i") + 1], ":default")

    @macos_only
    def test_a_chosen_microphone_goes_in_by_name(self):
        """Its name and not its index: indexes move when a device is plugged
        in, and a setting that means a different microphone tomorrow is worse
        than one that stops matching anything."""
        _, _, _, popen = self.record(silence(0.5), target="MacBook Pro Microphone")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-i") + 1], ":MacBook Pro Microphone")

    def test_a_pipe_that_hands_over_pieces_still_reads_in_chunks(self):
        """One RMS reading per chunk of a known length, however the bytes
        arrive: vad.analyse() multiplies the count by that length, so a piece
        counted as a chunk is a silent recording reported as speech."""
        _, results, failures, _ = self.record(tone(1.0), stream=Trickle)
        self.assertEqual(failures, [])
        _path, duration, rms = results[0]
        self.addCleanup(os.unlink, results[0][0])
        self.assertAlmostEqual(duration, 1.0, places=2)
        # A second is fifteen whole chunks and the beginning of a sixteenth.
        self.assertEqual(len(rms), -(-audio.RATE // audio.CHUNK_FRAMES))

    def test_a_recording_ends_as_a_wav_with_its_duration_and_levels(self):
        _, results, failures, _ = self.record(tone(1.0))
        self.assertEqual(failures, [])
        path, duration, rms = results[0]
        self.addCleanup(os.unlink, path)
        self.assertAlmostEqual(duration, 1.0, places=2)
        self.assertTrue(rms)
        self.assertGreater(max(rms), 0.0)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            self.assertEqual(wav.getnframes(), audio.RATE)

    def test_a_stray_keypress_is_not_a_recording(self):
        _, results, failures, _ = self.record(silence(0.1))
        self.assertEqual(results, [])
        self.assertIn("0.3", failures[0])

    def test_a_cancelled_recording_produces_nothing(self):
        recorder = audio.Recorder()
        results = []
        recorder.stopped.connect(lambda *args: results.append(args))
        proc = FakeProcess(tone(1.0))
        with only_these_tools(RECORDER), \
                mock.patch.object(subprocess, "Popen", return_value=proc):
            recorder.start()
            recorder._thread.join(timeout=5)
            recorder.cancel()
            recorder.stop()
        self.assertEqual(results, [])

    def test_a_recording_that_runs_past_the_limit_is_cut_off(self):
        _, results, _, _ = self.record(tone(3.0), max_seconds=1)
        path, duration, _ = results[0]
        self.addCleanup(os.unlink, path)
        self.assertLessEqual(duration, 1.1)

    def test_a_recorder_that_is_not_installed_at_all(self):
        """Said once, and it names what to install on the platform it is said
        on: a Mac told to install pulseaudio-utils is being sent nowhere."""
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools():
            recorder.start()
        self.assertEqual(len(failures), 1)
        self.assertIn("ffmpeg" if sys.platform == "darwin" else "pulseaudio-utils",
                      failures[0])

    def pump(self, data=b"", stderr=b"", stopping=False, cancelled=False):
        """Run the pump in this thread, where a queued signal would need an
        event loop nobody is running here."""
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        proc = FakeProcess(data)
        proc.stderr = io.BytesIO(stderr)
        proc._alive = False
        recorder._proc = proc
        recorder._max_bytes = 10 ** 9
        recorder._stopping = stopping
        recorder._cancelled = cancelled
        recorder._pump()
        return failures

    def test_a_recorder_that_died_on_its_own_says_so(self):
        """parec refused the device, or the sound server went away."""
        failures = self.pump(stderr=b"connection refused\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("connection refused", failures[0])

    def test_a_death_with_nothing_on_stderr_still_names_the_exit_code(self):
        failures = self.pump()
        self.assertIn("exit code", failures[0])

    def test_a_recording_we_ended_ourselves_is_not_a_death(self):
        """Otherwise a stray keypress produces two errors, and the first one
        sends the user looking for a broken sound server."""
        self.assertEqual(self.pump(stopping=True), [])

    def test_a_cancelled_recording_is_not_a_death(self):
        self.assertEqual(self.pump(cancelled=True), [])

    def test_a_recorder_that_captured_something_first_is_not_a_death(self):
        self.assertEqual(self.pump(data=silence(0.5)), [])

    def test_a_short_recording_reports_only_that(self):
        _, results, failures, _ = self.record(silence(0.1))
        self.assertEqual(results, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("0.3", failures[0])

    def test_a_recorder_that_could_not_start(self):
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools(RECORDER), \
                mock.patch.object(subprocess, "Popen", side_effect=OSError("nope")):
            recorder.start()
        self.assertEqual(len(failures), 1)
        self.assertFalse(recorder.active)


if __name__ == "__main__":
    unittest.main()
