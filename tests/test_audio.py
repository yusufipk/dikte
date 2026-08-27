"""Level metering, the WAV writer, and what the sound system is asked for.

The device list is where a platform port lands first, so the parsing is pinned
here: on Linux a source that is not a monitor is an input, one that is belongs
to the speakers, and neither list may go missing when pactl is absent. macOS
answers the same three questions out of one ffmpeg listing.

Each class says which machine it is standing on, so both halves run on either
one: nothing here reaches a real sound server.
"""

import array
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
import wave
from unittest import mock

from dikte import audio
from tests.support import (
    DikteTest,
    FakeCompleted,
    only_these_tools,
    pcm,
    silence,
    stereo,
    tone,
)


class OnLinux:
    """A test that runs as if the machine ran PulseAudio or PipeWire."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sys, "platform", "linux"))


class OnMacOS:
    """A test that runs as if the machine were a Mac."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sys, "platform", "darwin"))


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

    def test_the_fast_and_plain_rms_paths_agree(self):
        """sumprod is a speedup, not a different sum: on a 3.11 machine the
        loop must land on the same integers."""
        chunk = tone(0.1)
        with mock.patch.object(audio, "sumprod", None):
            plain = audio.chunk_levels(chunk)
        self.assertEqual(audio.chunk_levels(chunk), plain)


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

    def test_two_mono_streams_are_interleaved_left_then_right(self):
        self.assertEqual(
            list(array.array("h", audio.interleave_mono(
                pcm([100, 200, 300]), pcm([-100, -200, -300])
            ))),
            [100, -100, 200, -200, 300, -300],
        )

    def test_interleaving_stops_at_the_shorter_stream(self):
        self.assertEqual(
            list(array.array("h", audio.interleave_mono(
                pcm([100, 200]), pcm([-100])
            ))),
            [100, -100],
        )


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


class Devices(OnLinux, DikteTest):
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


class FakeProcess:
    """A pw-record that hands over a fixed buffer and then ends."""

    def __init__(self, data):
        self.stdout = io.BytesIO(data)
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


class StalledProcess(FakeProcess):
    """A capture that hands over a buffer and then stops answering at all.

    Not the same thing as one that ends: the device is still there and the pipe
    is still open, and a read of it never comes back.
    """

    def __init__(self, data):
        super().__init__(data)
        self.stdout = _StalledStream(data)


class _StalledStream:
    def __init__(self, data):
        self._data = io.BytesIO(data)
        self._released = threading.Event()

    def read(self, size):
        chunk = self._data.read(size)
        if chunk:
            return chunk
        self._released.wait()
        return b""

    def release(self):
        self._released.set()


class _DribblingStream:
    """A pipe that never fills a whole chunk in one read, the way an unbuffered
    pipe hands data over under load."""

    def __init__(self, data, piece):
        self._data = io.BytesIO(data)
        self._piece = piece

    def read(self, size):
        return self._data.read(min(size, self._piece))


class _RunSwappingStream:
    """A pipe whose recorder moves on mid-read, the way a new recording starts
    while a stale pump is still draining the old one."""

    def __init__(self, data, recorder, swap_at, new_proc):
        self._data = io.BytesIO(data)
        self._recorder = recorder
        self._swap_at = swap_at
        self._new_proc = new_proc
        self.reads = 0

    def read(self, size):
        if self.reads == self._swap_at:
            self._recorder._run = object()
            self._recorder._proc = self._new_proc
        self.reads += 1
        return self._data.read(size)


class _SignalCrashingProcess(FakeProcess):
    """An ffmpeg that calls being interrupted a failure, the way ffmpeg does."""

    def __init__(self, data, code=255):
        super().__init__(data)
        self._code = code

    def poll(self):
        return None if self._alive else self._code


class _HeldStream:
    """A capture that is paused and taken up again partway through, the way a
    key press lands in the middle of a recording rather than between two."""

    def __init__(self, data, recorder, pause_at, resume_at=None):
        self._data = io.BytesIO(data)
        self._recorder = recorder
        self._pause_at = pause_at
        self._resume_at = resume_at
        self.reads = 0

    def read(self, size):
        if self.reads == self._pause_at:
            self._recorder.pause()
        elif self.reads == self._resume_at:
            self._recorder.pause(False)
        self.reads += 1
        return self._data.read(size)


class RecordingCommand(OnLinux, DikteTest):
    """Which program captures the microphone, and how it is asked to."""

    def setUp(self):
        super().setUp()
        # Whether pw-record takes --raw is read off the installed binary, and
        # what is being tested here is the command rather than the machine the
        # test is running on. PwRecordRawOption covers the reading itself. The
        # answer is remembered between calls, so it cannot be remembered
        # between tests.
        self.enterContext(mock.patch.object(audio, "_PW_RAW", None))
        self.enterContext(mock.patch.object(
            audio, "_pw_record_raw_option", return_value=["--raw"]))

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


class PwRecordRawOption(DikteTest):
    """Two pw-record generations want opposite commands for the same stream.

    PipeWire 1.4 added --raw and stopped treating a filename of "-" as raw on
    its own, so the option is refused by everything older and needed by
    everything newer. The help text is the only thing that tells them apart.
    """

    def option(self, **run):
        with mock.patch.object(audio.subprocess, "run", **run):
            return audio._pw_record_raw_option()

    def test_a_version_that_offers_raw_is_asked_for_it(self):
        self.assertEqual(["--raw"], self.option(
            return_value=FakeCompleted(stdout="  -a, --raw   RAW mode\n")))

    def test_a_version_without_it_is_not(self):
        self.assertEqual([], self.option(
            return_value=FakeCompleted(stdout="  --rate  Sample rate\n")))

    def test_help_that_could_not_be_read_keeps_the_option(self):
        """Whatever is installed, the command that worked before this check
        existed is the safer guess."""
        self.assertEqual(["--raw"], self.option(side_effect=OSError))
        self.assertEqual(["--raw"], self.option(
            side_effect=subprocess.TimeoutExpired("pw-record", 2)))

    def test_help_that_said_nothing_keeps_it_too(self):
        self.assertEqual(["--raw"], self.option(return_value=FakeCompleted()))


class PwRawMemo(OnLinux, DikteTest):
    """The --raw probe runs once per process, not once per key press."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(audio, "_PW_RAW", None))

    def test_the_probe_is_asked_once_and_remembered(self):
        with only_these_tools("pw-record"), \
                mock.patch.object(audio, "_pw_record_raw_option",
                                  return_value=["--raw"]) as probe:
            first = audio.recording_command()
            second = audio.recording_command()
        probe.assert_called_once_with()
        self.assertIn("--raw", first)
        self.assertEqual(first, second)


class RecorderChain(OnLinux, DikteTest):
    """Start to WAV, with pw-record faked out."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(audio, "_PW_RAW", None))
        self.enterContext(mock.patch.object(
            audio, "_pw_record_raw_option", return_value=["--raw"]))

    def record(self, data, target="", max_seconds=300):
        recorder = audio.Recorder()
        results = []
        failures = []
        recorder.stopped.connect(lambda *args: results.append(args))
        recorder.failed.connect(failures.append)
        proc = FakeProcess(data)
        with only_these_tools("pw-record"), \
                mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            recorder.start(target=target, max_seconds=max_seconds)
            recorder._thread.join(timeout=5)
            recorder.stop()
        return recorder, results, failures, popen

    def test_the_capture_format_is_what_the_rest_of_the_code_expects(self):
        _, _, _, popen = self.record(silence(1.0))
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], "pw-record")
        self.assertIn(f"--rate={audio.RATE}", cmd)
        self.assertIn(f"--channels={audio.CHANNELS}", cmd)
        self.assertIn("--format=s16", cmd)
        self.assertEqual(cmd[-1], "-")

    def test_no_target_means_no_target_flag(self):
        _, _, _, popen = self.record(silence(0.5))
        self.assertFalse([arg for arg in popen.call_args.args[0]
                          if arg.startswith("--target=")])

    def test_a_chosen_microphone_is_passed_on(self):
        _, _, _, popen = self.record(silence(0.5), target="alsa_input.usb")
        self.assertIn("--target=alsa_input.usb", popen.call_args.args[0])

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
        with only_these_tools("pw-record"), \
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
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools():
            recorder.start()
        self.assertEqual(len(failures), 1)
        self.assertIn("pulseaudio-utils", failures[0])

    def pump(self, data=b"", stderr=b"", stopping=False, cancelled=False,
             alive=False):
        """Run the pump in this thread, where a queued signal would need an
        event loop nobody is running here."""
        recorder = audio.Recorder()
        failures = []
        deaths = []
        recorder.failed.connect(failures.append)
        recorder.died.connect(lambda: deaths.append(True))
        proc = FakeProcess(data)
        proc._alive = alive
        recorder._proc = proc
        recorder._log = io.BytesIO(stderr)
        recorder._stopping = stopping
        recorder._cancelled = cancelled
        recorder._run = run = object()
        recorder._pump(run, proc, proc.stdout, recorder._buffer,
                       recorder._rms, 10 ** 9)
        return failures, deaths

    def test_a_recorder_that_died_on_its_own_says_so(self):
        """parec refused the device, or the sound server went away."""
        failures, _ = self.pump(stderr=b"connection refused\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("connection refused", failures[0])

    def test_a_death_with_nothing_on_stderr_still_names_the_exit_code(self):
        failures, _ = self.pump()
        self.assertIn("exit code", failures[0])

    def test_a_death_that_left_no_exit_code_is_not_named_none(self):
        """A process nobody managed to reap has no code to show, and "exit
        code None" would only puzzle the person reading it."""
        failures, _ = self.pump(alive=True)
        self.assertEqual(len(failures), 1)
        self.assertNotIn("None", failures[0])

    def test_a_recording_we_ended_ourselves_is_not_a_death(self):
        """Otherwise a stray keypress produces two errors, and the first one
        sends the user looking for a broken sound server."""
        self.assertEqual(self.pump(stopping=True), ([], []))

    def test_a_cancelled_recording_is_not_a_death(self):
        self.assertEqual(self.pump(cancelled=True), ([], []))

    def test_a_capture_that_ends_mid_recording_dies_rather_than_fails(self):
        """Sound had already arrived, so this is not a broken installation:
        the app is told the recording died and can rescue what there is."""
        failures, deaths = self.pump(data=silence(0.5))
        self.assertEqual(failures, [])
        self.assertEqual(deaths, [True])

    def test_short_pipe_reads_are_gathered_into_whole_chunks(self):
        """Every RMS entry must stand for one full chunk, or the silence check
        weighs a half-filled read as its own stretch of room tone."""
        half = audio.CHUNK_BYTES // 2
        data = pcm([1000] * (3 * half // 2))   # three half-chunk reads
        recorder = audio.Recorder()
        proc = FakeProcess(b"")
        proc.stdout = _DribblingStream(data, half)
        proc._alive = False
        recorder._proc = proc
        recorder._log = io.BytesIO(b"")
        recorder._run = run = object()
        buffer, rms = bytearray(), []
        recorder._pump(run, proc, proc.stdout, buffer, rms, 10 ** 9)
        self.assertEqual(len(buffer), len(data))
        self.assertEqual(len(rms), 2)   # one whole chunk, then the tail

    def test_a_stale_pump_cannot_touch_the_recording_that_replaced_it(self):
        """A pump that outlives its join must not meter the next run, stop its
        process, push audio into its buffer, or speak on its behalf."""
        recorder = audio.Recorder()
        levels, failures, deaths = [], [], []
        recorder.level.connect(levels.append)
        recorder.failed.connect(failures.append)
        recorder.died.connect(lambda: deaths.append(True))
        new_proc = FakeProcess(b"")
        old_proc = FakeProcess(b"")
        old_proc.stdout = _RunSwappingStream(tone(0.192), recorder,
                                             swap_at=1, new_proc=new_proc)
        recorder._proc = old_proc
        recorder._log = io.BytesIO(b"")
        old_run = object()
        recorder._run = old_run
        recorder._buffer = bytearray()   # the next recording's buffer
        old_buffer, old_rms = bytearray(), []
        recorder._pump(old_run, old_proc, old_proc.stdout, old_buffer, old_rms,
                       2 * audio.CHUNK_BYTES)
        # Metered once, then the new run took over: the over-length cutoff hit
        # on the next chunk and had to stand down instead of stopping a
        # process that was never its own.
        self.assertEqual(len(levels), 1)
        self.assertEqual(len(old_buffer), 2 * audio.CHUNK_BYTES)
        self.assertEqual(new_proc.signals, [])
        self.assertEqual(old_proc.signals, [])
        self.assertEqual(recorder._buffer, bytearray())
        self.assertEqual((failures, deaths), ([], []))

    def test_a_wav_that_cannot_be_written_is_reported_not_raised(self):
        recorder = audio.Recorder()
        results, failures = [], []
        recorder.stopped.connect(lambda *args: results.append(args))
        recorder.failed.connect(failures.append)
        proc = FakeProcess(tone(1.0))
        with only_these_tools("pw-record"), \
                mock.patch.object(subprocess, "Popen", return_value=proc), \
                mock.patch.object(audio, "write_wav",
                                  side_effect=OSError("disk full")):
            recorder.start()
            recorder._thread.join(timeout=5)
            recorder.stop()
        self.assertEqual(results, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("disk full", failures[0])

    def test_a_short_recording_reports_only_that(self):
        _, results, failures, _ = self.record(silence(0.1))
        self.assertEqual(results, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("0.3", failures[0])

    def held(self, data, pause_at, resume_at=None):
        """Record `data` with the recorder paused for part of it."""
        recorder = audio.Recorder()
        results = []
        failures = []
        recorder.stopped.connect(lambda *args: results.append(args))
        recorder.failed.connect(failures.append)
        proc = FakeProcess(data)
        proc.stdout = _HeldStream(data, recorder, pause_at, resume_at)
        with only_these_tools("pw-record"), \
                mock.patch.object(subprocess, "Popen", return_value=proc):
            recorder.start()
            recorder._thread.join(timeout=5)
            # Nothing has ended the capture: a pause holds the microphone.
            self.assertEqual(proc.signals, [])
            recorder.stop()
        return results, failures

    def test_what_is_said_while_it_is_held_is_not_in_the_recording(self):
        """The phone call in the middle of a dictation is the whole feature: it
        must not reach the transcript, and the two halves must meet."""
        results, failures = self.held(tone(2.0), pause_at=8, resume_at=16)
        self.assertEqual(failures, [])
        path, duration, _ = results[0]
        self.addCleanup(os.unlink, path)
        dropped = 8 * audio.CHUNK_FRAMES
        self.assertAlmostEqual(duration, (2 * audio.RATE - dropped) / audio.RATE,
                               places=3)

    def test_a_recording_held_all_the_way_through_captured_nothing(self):
        results, failures = self.held(tone(2.0), pause_at=0)
        self.assertEqual(results, [])
        self.assertIn("0.3", failures[0])

    def test_a_pause_does_not_outlive_the_recording_it_was_asked_for(self):
        recorder = audio.Recorder()
        recorder.pause()
        proc = FakeProcess(tone(0.5))
        with only_these_tools("pw-record"), \
                mock.patch.object(subprocess, "Popen", return_value=proc):
            recorder.start()
            self.assertFalse(recorder.paused)
            recorder._thread.join(timeout=5)
            recorder.cancel()

    def test_a_recorder_that_could_not_start(self):
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools("pw-record"), \
                mock.patch.object(subprocess, "Popen", side_effect=OSError("nope")):
            recorder.start()
        self.assertEqual(len(failures), 1)
        self.assertFalse(recorder.active)


class MeetingCommands(unittest.TestCase):
    """Pulse can share a process; AVFoundation sessions cannot."""

    def commands(self, platform, mic="", system="them"):
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(audio, "_avfoundation_inputs", return_value=[]), \
                mock.patch.object(
                    audio, "_resolve_avfoundation_target",
                    side_effect=lambda target, inputs=None: target or "default"):
            return audio.meeting_commands(mic, system)

    def test_linux_reads_both_through_pulse(self):
        commands = self.commands("linux", mic="mine")
        self.assertEqual(len(commands), 1)
        cmd = commands[0]
        self.assertEqual(cmd.count("pulse"), 2)
        self.assertEqual(cmd[cmd.index("mine") - 1], "-i")
        self.assertEqual(cmd[cmd.index("them") - 1], "-i")

    def test_a_mac_gives_each_avfoundation_device_its_own_process(self):
        commands = self.commands("darwin", mic="mine")
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(command.count("avfoundation") == 1
                            for command in commands))
        self.assertIn(":mine", commands[0])
        self.assertIn(":them", commands[1])

    def test_no_microphone_named_means_the_default_one(self):
        self.assertIn("default", self.commands("linux")[0])
        self.assertIn(":default", self.commands("darwin")[0])

    def test_pulse_merges_the_two_into_one_stereo_stream(self):
        cmd = self.commands("linux")[0]
        self.assertIn(audio.MERGE_FILTER, cmd)
        self.assertEqual(cmd[cmd.index("-map") + 1], "[out]")
        self.assertEqual(cmd[cmd.index("-f", cmd.index("-map")) + 1], "s16le")

    def test_each_mac_process_produces_clock_corrected_mono_pcm(self):
        for cmd in self.commands("darwin"):
            self.assertIn("first_pts=0", cmd[cmd.index("-af") + 1])
            self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
            self.assertEqual(cmd[-2:], ["1", "-"])

    def test_neither_lets_ffmpeg_read_the_terminal(self):
        """It shares stdin with Dikte, and would eat a keypress meant for it."""
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                for command in self.commands(platform):
                    self.assertIn("-nostdin", command)

    def test_both_mac_devices_are_read_off_one_listing(self):
        """Asking twice costs an ffmpeg run, and the second answer could have
        renumbered between the two."""
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(audio, "_avfoundation_inputs",
                                  return_value=[("0", "mine"),
                                                ("1", "them")]) as inputs:
            audio.meeting_commands("mine", "them")
        inputs.assert_called_once_with()


class MacMeetingRecorder(OnMacOS, DikteTest):
    # Whichever way the machine has them ordered, a name is what is saved and
    # the index it happens to hold now is what ffmpeg is given.
    DEVICES = [("0", "External Headset"), ("1", "BlackHole 2ch"),
               ("2", "MacBook Pro Microphone")]

    def devices(self):
        return mock.patch.object(audio, "_avfoundation_inputs",
                                 return_value=self.DEVICES)

    def record(self, mine, theirs):
        path = str(self.path("meeting.wav"))
        recorder = audio.MeetingRecorder()
        stopped, failed, warnings = [], [], []
        recorder.stopped.connect(lambda *args: stopped.append(args))
        recorder.failed.connect(failed.append)
        recorder.warned.connect(warnings.append)
        processes = [FakeProcess(mine), FakeProcess(theirs)]
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(subprocess, "Popen", side_effect=processes) as popen:
            recorder.start(path, "MacBook Pro Microphone", "BlackHole 2ch")
            recorder._thread.join(timeout=5)
            recorder.stop()
        return path, warnings, stopped, failed, processes, popen

    def test_the_two_capture_processes_become_one_stereo_file(self):
        path, _, stopped, failed, _, _ = self.record(
            tone(1.0, freq=440), tone(1.0, freq=880)
        )
        self.assertEqual(failed, [])
        self.assertEqual(len(stopped), 1)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getframerate(), audio.RATE)
            self.assertEqual(wav.getnframes(), audio.RATE)

    def test_each_avfoundation_device_is_opened_by_a_different_process(self):
        _, _, _, _, _, popen = self.record(tone(0.5), tone(0.5))
        commands = [call.args[0] for call in popen.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(command.count("avfoundation") == 1
                            for command in commands))
        self.assertIn(":2", commands[0])
        self.assertIn(":1", commands[1])

    def test_a_mostly_empty_microphone_is_said_out_loud_and_still_kept(self):
        """Half the file is everyone else, and an hour of them is worth more
        than the empty channel costs."""
        path, warnings, stopped, failed, _, _ = self.record(
            silence(11.0), tone(11.0)
        )
        self.assertEqual(failed, [])
        self.assertEqual(len(stopped), 1)
        self.assertTrue(os.path.exists(path))
        self.assertIn("empty", warnings[0])

    def test_a_microphone_that_was_merely_quiet_is_not_complained_about(self):
        _, warnings, stopped, _, _, _ = self.record(tone(11.0), tone(11.0))
        self.assertEqual(warnings, [])
        self.assertEqual(len(stopped), 1)

    def test_a_capture_that_falls_silent_ends_the_meeting_rather_than_hanging(self):
        """One thread taking turns on both pipes would sit on the dead read
        until somebody noticed, an hour later."""
        path = str(self.path("meeting.wav"))
        recorder = audio.MeetingRecorder()
        stopped = []
        recorder.stopped.connect(lambda *args: stopped.append(args))
        mine, theirs = StalledProcess(tone(0.512)), FakeProcess(tone(30.0))
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(audio, "STALL_SECONDS", 0.2), \
                mock.patch.object(subprocess, "Popen", side_effect=(mine, theirs)):
            try:
                recorder.start(path, "MacBook Pro Microphone", "BlackHole 2ch")
                recorder._thread.join(timeout=2)
                self.assertFalse(recorder.active)
                recorder.stop()
            finally:
                mine.stdout.release()
        self.assertAlmostEqual(stopped[0][1], 0.512, places=3)
        self.assertTrue(os.path.exists(path))

    def test_stopping_ends_both_capture_processes(self):
        _, _, _, _, processes, _ = self.record(tone(0.5), tone(0.5))
        self.assertTrue(all(process.signals for process in processes))

    def test_a_stop_we_asked_for_is_not_reported_as_an_ffmpeg_failure(self):
        """ffmpeg exits 255 when interrupted, and the interruption was our own
        stop: a meeting ended at once must say "too short", not "ffmpeg → 255"."""
        path = str(self.path("meeting.wav"))
        recorder = audio.MeetingRecorder()
        failed = []
        recorder.failed.connect(failed.append)
        processes = [_SignalCrashingProcess(tone(0.1)),
                     _SignalCrashingProcess(tone(0.1))]
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(subprocess, "Popen", side_effect=processes):
            recorder.start(path, "MacBook Pro Microphone", "BlackHole 2ch")
            recorder._thread.join(timeout=5)
            recorder.stop()
        self.assertEqual(len(failed), 1)
        self.assertIn("0.3", failed[0])
        self.assertNotIn("255", failed[0])

    def test_an_ffmpeg_that_died_on_its_own_keeps_its_exit_code(self):
        """A process nobody interrupted has a story to tell, and its code is
        the only lead the user gets."""
        path = str(self.path("meeting.wav"))
        recorder = audio.MeetingRecorder()
        failed = []
        recorder.failed.connect(failed.append)
        dead = _SignalCrashingProcess(tone(0.1))
        dead._alive = False   # it fell over before stop() reached it
        processes = [dead, FakeProcess(tone(0.1))]
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(subprocess, "Popen", side_effect=processes):
            recorder.start(path, "MacBook Pro Microphone", "BlackHole 2ch")
            recorder._thread.join(timeout=5)
            recorder.stop()
        self.assertEqual(len(failed), 1)
        self.assertIn("255", failed[0])

    def test_a_legacy_numeric_target_fails_before_recording(self):
        recorder = audio.MeetingRecorder()
        failed = []
        recorder.failed.connect(failed.append)
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(subprocess, "Popen") as popen:
            recorder.start(str(self.path("meeting.wav")), "2", "1")
        popen.assert_not_called()
        self.assertIn("old numeric index", failed[0])

    def test_a_second_capture_process_that_cannot_start_cleans_up_the_first(self):
        """A Mac left holding an open AVFoundation session records nothing
        else until it is let go."""
        path = str(self.path("meeting.wav"))
        recorder = audio.MeetingRecorder()
        failed = []
        recorder.failed.connect(failed.append)
        first = FakeProcess(tone(1.0))
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(subprocess, "Popen",
                                  side_effect=(first, OSError("refused"))):
            recorder.start(path, "MacBook Pro Microphone", "BlackHole 2ch")
        self.assertTrue(first.signals)
        self.assertIn("refused", failed[0])
        self.assertFalse(os.path.exists(path))


class MacDevices(OnMacOS, DikteTest):
    """The one ffmpeg listing all three device questions are answered from."""

    LISTING = (
        "[AVFoundation indev @ 0x7fb] AVFoundation video devices:\n"
        "[AVFoundation indev @ 0x7fb] [0] FaceTime HD Camera\n"
        "[AVFoundation indev @ 0x7fb] [1] Capture screen 0\n"
        "[AVFoundation indev @ 0x7fb] AVFoundation audio devices:\n"
        "[AVFoundation indev @ 0x7fb] [0] MacBook Pro Microphone\n"
        "[AVFoundation indev @ 0x7fb] [1] BlackHole 2ch\n"
        ": Input/output error\n"
    )

    @contextlib.contextmanager
    def listing(self, stderr=None, tools=("ffmpeg",)):
        completed = FakeCompleted(
            returncode=1, stderr=self.LISTING if stderr is None else stderr)
        with only_these_tools(*tools), \
                mock.patch.object(subprocess, "run", return_value=completed):
            yield

    def test_the_audio_half_of_the_listing_is_the_only_half_read(self):
        with self.listing():
            self.assertEqual(audio.list_sources(),
                             [("MacBook Pro Microphone", "MacBook Pro Microphone"),
                              ("BlackHole 2ch", "BlackHole 2ch")])

    def test_the_name_is_both_saved_and_shown(self):
        with self.listing():
            name, description = audio.list_sources()[1]
        self.assertEqual(name, "BlackHole 2ch")
        self.assertIn("BlackHole", description)

    def test_no_ffmpeg_installed(self):
        with only_these_tools():
            self.assertEqual(audio.list_sources(), [])
            self.assertEqual(audio.list_monitors(), [])
            self.assertEqual(audio.default_monitor(), "")

    def test_an_ffmpeg_that_will_not_run(self):
        with only_these_tools("ffmpeg"), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(audio.list_sources(), [])

    def test_a_listing_with_no_audio_section(self):
        with self.listing(stderr="[AVFoundation indev @ 0x7fb] [0] FaceTime\n"):
            self.assertEqual(audio.list_sources(), [])

    def test_the_far_side_of_a_meeting_is_offered_the_same_devices(self):
        """macOS calls none of them an output, so the loopback one is in here."""
        with self.listing():
            self.assertEqual(audio.list_monitors(), audio.list_sources())

    def test_the_loopback_driver_is_picked_out_by_name(self):
        with self.listing():
            self.assertEqual(audio.default_monitor(), "BlackHole 2ch")

    def test_the_other_two_drivers_people_install(self):
        for name in ("Loopback Audio", "Soundflower (2ch)"):
            with self.subTest(name=name):
                listing = ("AVFoundation audio devices:\n"
                           f"[0] Built-in Microphone\n[1] {name}\n")
                with self.listing(stderr=listing):
                    self.assertEqual(audio.default_monitor(), name)

    def test_a_mac_with_nothing_to_record_the_far_side_from(self):
        listing = "AVFoundation audio devices:\n[0] MacBook Pro Microphone\n"
        with self.listing(stderr=listing):
            self.assertEqual(audio.default_monitor(), "")

    def test_a_saved_name_is_resolved_against_the_current_index(self):
        with self.listing():
            self.assertEqual(audio._resolve_avfoundation_target("BlackHole 2ch"), "1")

    def test_a_saved_name_follows_the_device_when_an_earlier_one_disappears(self):
        listing = ("AVFoundation audio devices:\n"
                   "[0] BlackHole 2ch\n[1] MacBook Pro Microphone\n")
        with self.listing(stderr=listing):
            self.assertEqual(
                audio._resolve_avfoundation_target("MacBook Pro Microphone"), "1"
            )

    def test_an_old_numeric_setting_is_not_silently_reused(self):
        with self.assertRaises(audio.AudioDeviceError) as caught:
            audio._resolve_avfoundation_target("1")
        self.assertIn("old numeric index", str(caught.exception))

    def test_a_device_that_went_away_is_said_out_loud(self):
        with self.listing(), self.assertRaises(audio.AudioDeviceError) as caught:
            audio._resolve_avfoundation_target("USB Microphone")
        self.assertIn("no longer connected", str(caught.exception))

    def test_duplicate_names_are_not_guessed_between(self):
        listing = ("AVFoundation audio devices:\n"
                   "[0] USB Microphone\n[1] USB Microphone\n")
        with self.listing(stderr=listing), \
                self.assertRaises(audio.AudioDeviceError) as caught:
            audio._resolve_avfoundation_target("USB Microphone")
        self.assertIn("More than one", str(caught.exception))


class MacRecordingCommand(OnMacOS, DikteTest):
    def test_the_microphone_is_read_through_avfoundation(self):
        with only_these_tools("ffmpeg"):
            cmd = audio.recording_command()
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertEqual(cmd[cmd.index("-f") + 1], "avfoundation")

    def test_the_empty_half_in_front_of_the_colon_is_the_missing_picture(self):
        with only_these_tools("ffmpeg"):
            self.assertIn(":default", audio.recording_command())
        listing = "AVFoundation audio devices:\n[2] USB Microphone\n"
        completed = FakeCompleted(returncode=1, stderr=listing)
        with only_these_tools("ffmpeg"), \
                mock.patch.object(subprocess, "run", return_value=completed):
            self.assertIn(":2", audio.recording_command("USB Microphone"))

    def test_it_captures_the_format_the_rest_of_the_code_expects(self):
        with only_these_tools("ffmpeg"):
            cmd = audio.recording_command()
        self.assertEqual(cmd[cmd.index("-ar") + 1], str(audio.RATE))
        self.assertEqual(cmd[cmd.index("-ac") + 1], str(audio.CHANNELS))
        self.assertEqual(cmd[-2:], ["s16le", "-"])

    def test_no_ffmpeg_installed(self):
        with only_these_tools():
            self.assertEqual(audio.recording_command(), [])

    def test_what_a_mac_is_told_to_install(self):
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools():
            recorder.start()
        self.assertIn("brew install ffmpeg", failures[0])
        self.assertFalse(recorder.active)


class NoFarSideToRecord(DikteTest):
    """Two different answers, and the table is what tells them apart.

    A sound system that records the far side has a device this machine could
    not pick out, and Settings is where to choose one. A sound system that does
    not had nothing to offer there in the first place, and "pick one" would
    send somebody to an empty box and an installation that cannot help.
    """

    def failure(self, meetings):
        recorder = audio.MeetingRecorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools("ffmpeg"), \
                mock.patch.object(audio, "default_monitor", return_value=""), \
                mock.patch.object(audio, "sound",
                                  return_value=audio.PULSE._replace(
                                      meetings=meetings)):
            recorder.start(str(self.path("meeting.wav")))
        self.assertFalse(recorder.active)
        return failures[0]

    def test_a_system_that_records_the_far_side_sends_you_to_settings(self):
        self.assertIn("Settings", self.failure(True))

    def test_a_system_that_does_not_says_that_instead(self):
        message = self.failure(False)
        self.assertIn("nothing that records what the speakers", message)
        self.assertNotIn("Settings", message)

    def test_the_three_sound_systems_each_answer_the_question(self):
        self.assertTrue(audio.PULSE.meetings)
        self.assertTrue(audio.COREAUDIO.meetings)
        self.assertFalse(audio.DSHOW.meetings)


class OnWindows:
    """A test that runs as if the machine ran Windows."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sys, "platform", "win32"))


class WindowsDevices(OnWindows, DikteTest):
    """The one ffmpeg listing the device questions are answered from.

    dshow names devices rather than numbering them, and the names carry
    whatever alphabet the machine speaks, so the listing here does too.
    """

    MIC = "@device_cm_{33D9A762}\\wave_{B1C2}"
    LISTING = (
        '[dshow @ 0000020c] "Integrated Camera" (video)\n'
        '[dshow @ 0000020c]   Alternative name "@device_pnp_\\...."\n'
        '[dshow @ 0000020c] "Mikrofon Dizisi (Intel Smart Sound)" (audio)\n'
        f'[dshow @ 0000020c]   Alternative name "{MIC}"\n'
        '[dshow @ 0000020c] "Kulaklık (Soundcore Life Q30)" (audio)\n'
        '[dshow @ 0000020c] Could not find audio only device with name '
        '"dummy" among source devices of type audio.\n'
        "dummy: Immediate exit requested\n"
    ).encode("utf-8")

    def setUp(self):
        super().setUp()
        # The listing is remembered between calls, so that a dictation does not
        # run ffmpeg of its own. It cannot be remembered between tests, and
        # neither can the pw-record probe's answer.
        audio._DSHOW_SEEN.clear()
        self.addCleanup(audio._DSHOW_SEEN.clear)
        self.enterContext(mock.patch.object(audio, "_PW_RAW", None))

    @contextlib.contextmanager
    def listing(self, stderr=None, tools=("ffmpeg",)):
        completed = FakeCompleted(
            returncode=1, stderr=self.LISTING if stderr is None else stderr)
        with only_these_tools(*tools), \
                mock.patch.object(subprocess, "run",
                                  return_value=completed) as run:
            yield run

    def test_windows_records_through_dshow(self):
        self.assertIs(audio.sound(), audio.DSHOW)

    def test_the_audio_devices_are_the_only_ones_read(self):
        with self.listing():
            self.assertEqual(audio.list_sources(), [
                (self.MIC, "Mikrofon Dizisi (Intel Smart Sound)"),
                ("Kulaklık (Soundcore Life Q30)",
                 "Kulaklık (Soundcore Life Q30)"),
            ])

    def test_the_device_ffmpeg_could_not_open_is_not_one_of_them(self):
        """The command ends by quoting the name it was sent to look for."""
        with self.listing():
            self.assertNotIn("dummy", [name for _, name in audio.list_sources()])

    def test_a_listing_from_ffmpeg_8_which_renamed_the_prefix(self):
        """ffmpeg 8 writes `[in#0 @ ...]` where older builds wrote `[dshow @ ...]`."""
        listing = (
            '[in#0 @ 00000238c3300ac0] "Integrated Camera" (video)\n'
            '[in#0 @ 00000238c3300ac0]   Alternative name "@device_pnp_\\..."\n'
            '[in#0 @ 00000238c3300ac0] "OBS Virtual Camera" (none)\n'
            '[in#0 @ 00000238c3300ac0]   Alternative name "@device_sw_{860B}"\n'
            '[in#0 @ 00000238c3300ac0] "Mikrofon Dizisi (Intel® Smart Sound)" (audio)\n'
            '[in#0 @ 00000238c3300ac0]   Alternative name "@device_cm_{33D9}"\n'
            "Error opening input file dummy.\n"
        ).encode("utf-8")
        with self.listing(stderr=listing):
            self.assertEqual(audio.list_sources(),
                             [("@device_cm_{33D9}",
                               "Mikrofon Dizisi (Intel® Smart Sound)")])

    def test_a_listing_from_an_ffmpeg_that_marks_nothing(self):
        """Older builds print a heading instead of an (audio) on every line."""
        listing = (
            '[dshow @ 0] DirectShow video devices\n'
            '[dshow @ 0]  "Integrated Camera"\n'
            '[dshow @ 0]     Alternative name "@device_pnp_\\..."\n'
            '[dshow @ 0] DirectShow audio devices\n'
            '[dshow @ 0]  "Microphone (Realtek Audio)"\n'
            '[dshow @ 0]     Alternative name "@device_cm_{ABCD}"\n'
        ).encode("utf-8")
        with self.listing(stderr=listing):
            self.assertEqual(audio.list_sources(),
                             [("@device_cm_{ABCD}", "Microphone (Realtek Audio)")])

    def test_two_devices_called_the_same_thing_stay_apart(self):
        """The normal state of a laptop with a headset plugged into it."""
        listing = (
            '[dshow @ 0] "Microphone" (audio)\n'
            '[dshow @ 0]   Alternative name "@device_cm_{ONE}"\n'
            '[dshow @ 0] "Microphone" (audio)\n'
            '[dshow @ 0]   Alternative name "@device_cm_{TWO}"\n'
        ).encode("utf-8")
        with self.listing(stderr=listing):
            sources = audio.list_sources()
        self.assertEqual([identifier for identifier, _ in sources],
                         ["@device_cm_{ONE}", "@device_cm_{TWO}"])
        self.assertEqual({name for _, name in sources}, {"Microphone"})

    def test_no_ffmpeg_installed(self):
        with only_these_tools():
            self.assertEqual(audio.list_sources(), [])
            self.assertEqual(audio.recording_command(), [])

    def test_the_identifier_is_what_the_recorder_is_given_back(self):
        with self.listing():
            cmd = audio.recording_command(self.MIC)
        self.assertEqual(cmd[cmd.index("-f") + 1], "dshow")
        self.assertIn(f"audio={self.MIC}", cmd)

    def test_no_microphone_named_means_the_first_one_listed(self):
        """dshow has no default device for an empty target to mean."""
        with self.listing():
            self.assertIn(f"audio={self.MIC}", audio.recording_command())

    def test_a_dictation_does_not_run_a_listing_of_its_own(self):
        """Two hundred milliseconds of ffmpeg in front of every key press."""
        with self.listing() as run:
            audio.list_sources()
            audio.recording_command()
            audio.recording_command()
        self.assertEqual(run.call_count, 1)

    def test_opening_the_device_list_asks_again(self):
        """Which is what somebody who has just plugged one in does."""
        with self.listing() as run:
            audio.list_sources()
            audio.list_sources()
        self.assertEqual(run.call_count, 2)

    def test_a_machine_with_no_microphone_at_all(self):
        with self.listing(stderr=b'[dshow @ 0] "Integrated Camera" (video)\n'):
            self.assertEqual(audio.recording_command(), [])

    def test_an_ffmpeg_that_will_not_run(self):
        with only_these_tools("ffmpeg"), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(audio.list_sources(), [])

    def test_nothing_offers_the_far_side_of_a_meeting(self):
        """What the speakers play is not a capture device Windows hands out."""
        with self.listing():
            self.assertEqual(audio.list_monitors(), [])
            self.assertEqual(audio.default_monitor(), "")
            self.assertEqual(audio.meeting_commands("mic", "sys"), [])

    def test_a_meeting_says_what_is_wrong_rather_than_where_to_look(self):
        recorder = audio.MeetingRecorder()
        failures = []
        recorder.failed.connect(failures.append)
        with self.listing():
            recorder.start(str(self.path("meeting.wav")))
        self.assertIn("nothing that records what the speakers", failures[0])
        self.assertFalse(recorder.active)


if __name__ == "__main__":
    unittest.main()
