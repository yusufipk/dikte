"""Raw PCM capture with a live level meter.

Dictation records one source. A meeting records two of them at once, the
microphone and what comes out of the speakers, and for that it goes through
ffmpeg. PulseAudio hands both devices to a single process, which merges them
into the two channels of one stream and keeps them aligned itself. AVFoundation
cannot be asked the same: two of its sessions inside one process starve each
other, so a Mac captures each device on its own and the two mono streams are
interleaved here as they arrive.

Which programs do the capturing is a property of the machine, not of the code
above: PulseAudio or PipeWire on Linux, AVFoundation through ffmpeg on macOS.
They are gathered into one group each near the bottom of this file, and a
chooser picks between them.
"""

import array
import collections
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave

from PyQt6.QtCore import QObject, pyqtSignal

from . import paths
from .i18n import t

# Squaring a chunk sample by sample in Python is the most expensive thing the
# level meter does, and it does it for every chunk of every recording. sumprod
# stays in C for the whole sum; it arrived in 3.12 and the floor here is 3.11,
# so the plain loop remains as the fallback. Both produce the same integer.
try:
    from math import sumprod
except ImportError:
    sumprod = None

# See paths.NO_WINDOW; re-exported here because this module's callers and
# tests have always read it under this name.
NO_WINDOW = paths.NO_WINDOW

RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # s16
CHUNK_FRAMES = 1024
CHUNK_BYTES = CHUNK_FRAMES * SAMPLE_WIDTH * CHANNELS
CHUNK_LATENCY_MS = round(CHUNK_FRAMES / RATE * 1000)
MIN_FRAMES = int(RATE * 0.25)

# A capture process hands over a block every CHUNK_LATENCY_MS. One that has said
# nothing for this long has stopped rather than fallen behind, and the meeting
# ends and says so instead of sitting on a read that will never return.
STALL_SECONDS = 5.0
# Room for a whole stall of the other stream, so the side still delivering is
# never the one left waiting.
QUEUE_BLOCKS = int(STALL_SECONDS * RATE / CHUNK_FRAMES) + 8

# Exact zeroes are not quiet, they are nothing: a microphone that is really in
# the room has a noise floor. This much of a recording that long means it handed
# nothing over, which is worth saying once the meeting is over and nothing can
# be done about it any more.
QUIET_MIC_SECONDS = 10
QUIET_MIC_SHARE = 0.5


def _interrupt(proc):
    """Ask a recorder process to end.

    SIGINT is the polite way everywhere it exists; Windows has no equivalent a
    child can be sent, so the process is terminated outright. The captured
    audio is not lost either way: it has already been read from the pipe.
    """
    if sys.platform == "win32":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGINT)


class Recorder(QObject):
    """Runs the available sound-server recorder and reads raw PCM from stdout."""

    level = pyqtSignal(float)              # 0.0 - 1.0, for the waveform
    stopped = pyqtSignal(str, float, object)  # wav path, duration (s), per-chunk RMS
    died = pyqtSignal()                    # the capture quit mid-recording
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._thread = None
        self._log = None
        self._run = None
        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._stopping = False
        self._paused = False
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self):
        return self._paused

    def pause(self, value=True):
        """Stop taking sound in without letting go of the microphone.

        The capture program keeps running and keeps handing blocks over; they
        are dropped as they arrive rather than kept. Stopping it instead would
        mean asking the sound server for the device again on the way back, and
        that is the one moment another application can take it: a recording
        would be lost to the phone call it was paused for.

        What was said while it was paused is gone, which is the point. The two
        halves meet as one splice, with none of the room in between.
        """
        self._paused = bool(value)

    def start(self, target="", max_seconds=300):
        if self.active:
            return
        try:
            cmd = recording_command(target)
        except AudioDeviceError as exc:
            self.failed.emit(str(exc))
            return
        if not cmd:
            self.failed.emit(t(sound().missing))
            return

        # The recorder keeps talking to stderr for as long as it runs; a pipe
        # nobody drains would eventually block it, so it writes to a file.
        self._drop_log()
        self._log = tempfile.TemporaryFile()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=self._log, bufsize=0,
                creationflags=NO_WINDOW,
            )
        except OSError as exc:
            self._drop_log()
            self.failed.emit(t("Could not start recording: {error}", error=exc))
            return

        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._stopping = False
        self._paused = False
        self._max_bytes = int(max_seconds * RATE * SAMPLE_WIDTH * CHANNELS)
        # The pump is handed this run's objects rather than reading them off
        # self, and a token to say whose run it still is: a pump that outlives
        # its 2 s join must not touch the recording that comes after it.
        self._run = object()
        self._thread = threading.Thread(
            target=self._pump, daemon=True,
            args=(self._run, self._proc, self._proc.stdout,
                  self._buffer, self._rms, self._max_bytes),
        )
        self._thread.start()

    def _pump(self, run, proc, stdout, buffer, rms, max_bytes):
        try:
            while True:
                chunk = _read_exact(stdout, CHUNK_BYTES)
                if not chunk:
                    break
                if self._paused:
                    # Read and thrown away rather than left in the pipe: a pipe
                    # nobody empties fills up, and the capture program blocks on
                    # a full one instead of waiting quietly for the resume.
                    continue
                peak, chunk_rms = chunk_levels(chunk)
                with self._lock:
                    buffer.extend(chunk)
                    rms.append(chunk_rms)
                    too_long = len(buffer) >= max_bytes
                if self._run is not run:
                    # This recording was given up on; whatever happens now
                    # belongs to the run that replaced it, not to this one.
                    return
                self.level.emit(peak)
                if too_long:
                    self._terminate()
                    break
        except (OSError, ValueError):
            pass
        if self._run is not run:
            return
        if self._stopping or self._cancelled:
            return
        with self._lock:
            captured = bool(buffer)
        if captured:
            # Sound had already arrived and nobody asked it to end: the device
            # went away, or the recorder fell over mid-dictation. That has to
            # be said while there is still something worth keeping.
            self.died.emit()
            return
        # Nobody asked it to end and it captured nothing: the recorder is not
        # installed properly, or the device was refused. Said out loud here,
        # because stop() would otherwise report it as a recording that was too
        # short, which sends the user looking in the wrong place.
        detail = self._error_tail()
        # poll() first, because returncode stays None until somebody reaps the
        # process, and "exit code None" answers nothing.
        code = proc.poll()
        if not detail and code is not None:
            detail = f"exit code {code}"
        if detail:
            self.failed.emit(t(
                "Audio recorder stopped before receiving sound: {error}",
                error=detail,
            ))
        else:
            self.failed.emit(t("Audio recorder stopped before receiving sound"))

    def _error_tail(self):
        log = self._log
        return _last_log_line(log) if log is not None else ""

    def _drop_log(self):
        log, self._log = self._log, None
        _close_log(log)

    def _terminate(self):
        self._stopping = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                _interrupt(proc)
                proc.wait(timeout=1.5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                    # Reaped even after a kill, or the child stays a zombie
                    # holding its slot in the process table.
                    proc.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass

    def cancel(self):
        self._cancelled = True
        self._terminate()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._proc = None
        self._run = None
        self._drop_log()
        with self._lock:
            self._buffer = bytearray()

    def stop(self):
        """End the recording and write the WAV file."""
        if not self._proc:
            return
        self._terminate()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._proc = None
        self._run = None
        self._drop_log()

        # The same buffer object the pump was handed, harvested under the same
        # lock it appends with.
        with self._lock:
            pcm = bytes(self._buffer)
            rms = list(self._rms)
            self._buffer = bytearray()

        if self._cancelled:
            return

        frames = len(pcm) // (SAMPLE_WIDTH * CHANNELS)
        if frames < MIN_FRAMES:  # a stray keypress, not speech
            self.failed.emit(t("Recording too short, speak for at least 0.3 s"))
            return

        try:
            path = write_wav(pcm)
        except (OSError, wave.Error) as exc:
            # A full disk or an unwritable temp directory costs this recording
            # either way; a message beats a traceback in the journal.
            self.failed.emit(t("Could not write the recording: {error}", error=exc))
            return
        self.stopped.emit(path, frames / RATE, rms)


def write_wav(pcm, rate=RATE, channels=CHANNELS, width=SAMPLE_WIDTH):
    fd, path = tempfile.mkstemp(prefix="dikte-", suffix=".wav")
    with open(fd, "wb") as raw, wave.open(raw, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return path


def recording_command(target=""):
    """A raw-s16 capture command for the sound system on this machine."""
    return sound().record(target)


def meeting_commands(mic_target, system_target):
    """The capture processes that produce one stereo meeting stream.

    One of them on PulseAudio, which merges both inputs itself; one per device
    on a Mac, because two AVFoundation sessions in a process starve each other.
    Which of the two it is stays in the table with everything else the sound
    system decides, and MeetingRecorder reads the count rather than the machine.
    """
    return sound().meeting(mic_target, system_target)


class AudioDeviceError(RuntimeError):
    """A saved capture device can no longer be selected safely."""


class MeetingRecorder(QObject):
    """Microphone and speaker output into one stereo file: left is you, right is
    everyone else.

    Who said what then needs no guessing at all, because the two voices never
    shared a channel to begin with. The recording is written to disk as it
    arrives rather than held in memory, so length is not a problem and a crash
    costs the tail of the meeting instead of all of it.
    """

    levels = pyqtSignal(float, float)      # mine, theirs
    stopped = pyqtSignal(str, float)       # wav path, duration (s)
    died = pyqtSignal()                    # ffmpeg quit on its own
    warned = pyqtSignal(str)               # recorded, but something was wrong
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._procs = []
        self._interrupted = set()
        self._thread = None
        self._wav = None
        self._logs = []
        self._path = ""
        self._frames = 0
        self._mic_zero_frames = 0
        self._split_inputs = False
        self._cancelled = False
        self._stopping = False
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, path, mic_target="", system_target="", max_seconds=14400):
        if self.active:
            return
        # Before ffmpeg is looked for, because installing it would not help: a
        # system with no way to capture what the speakers are playing has none
        # whatever else is on the machine.
        if not sound().meetings:
            self.failed.emit(t("This system offers nothing that records what "
                               "the speakers are playing, so a meeting cannot "
                               "be recorded on it."))
            return
        if not shutil.which("ffmpeg"):
            self.failed.emit(t("ffmpeg not found. Install it to record a meeting."))
            return
        if not system_target:
            system_target = default_monitor()
        if not system_target:
            self.failed.emit(t("Could not work out which speaker output to record. "
                               "Pick one in Settings → Meeting."))
            return

        try:
            commands = meeting_commands(mic_target, system_target)
        except AudioDeviceError as exc:
            self.failed.emit(str(exc))
            return

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._wav = wave.open(path, "wb")
            self._wav.setnchannels(2)
            self._wav.setsampwidth(SAMPLE_WIDTH)
            self._wav.setframerate(RATE)
            # ffmpeg keeps talking to stderr for as long as it runs; a pipe
            # nobody drains would eventually block it, so it writes to a file.
            self._logs = [tempfile.TemporaryFile() for _ in commands]
            self._procs = []
            self._interrupted = set()
            for command, log in zip(commands, self._logs):
                self._procs.append(subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=log, bufsize=0,
                    creationflags=NO_WINDOW,
                ))
        except (OSError, wave.Error) as exc:
            # One of two capture processes may already be running, and a Mac
            # left holding an open AVFoundation session records nothing else.
            self._terminate_processes()
            self._procs = []
            self._close_file()
            self._drop_log()
            try:
                os.unlink(path)   # an empty header nobody will ever read
            except OSError:
                pass
            self.failed.emit(t("Could not start recording: {error}", error=exc))
            return

        self._path = path
        self._frames = 0
        self._mic_zero_frames = 0
        self._split_inputs = len(self._procs) == 2
        self._cancelled = False
        self._stopping = False
        self._max_frames = int(max_seconds * RATE)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        if self._split_inputs:
            self._pump_split()
        else:
            self._pump_merged()
        # Nobody asked it to end: the sound device went away, or ffmpeg fell
        # over. An hour into a meeting that has to be said out loud rather than
        # discovered afterwards.
        if not self._stopping:
            self.died.emit()

    def _pump_merged(self):
        stdout = self._procs[0].stdout
        block = CHUNK_FRAMES * SAMPLE_WIDTH * 2
        try:
            while True:
                chunk = stdout.read(block)
                if not chunk:
                    break
                if not self._write_chunk(chunk):
                    break
        except (OSError, ValueError, wave.Error):
            pass

    def _pump_split(self):
        # A reader thread per process. Taking turns on the two pipes from one
        # thread would let a starved microphone hold up the far side too: its
        # blocks would sit unread until the pipe filled and its ffmpeg stopped
        # writing, and an hour of meeting would freeze with nothing said. Each
        # stream is read as fast as it arrives, and a side that goes quiet for
        # STALL_SECONDS ends the recording rather than hanging it.
        block = CHUNK_FRAMES * SAMPLE_WIDTH
        streams = [queue.Queue(maxsize=QUEUE_BLOCKS) for _ in self._procs]
        for proc, blocks in zip(self._procs, streams):
            threading.Thread(target=_read_blocks, daemon=True,
                             args=(proc.stdout, blocks, block)).start()
        try:
            while True:
                mine = _next_block(streams[0])
                theirs = _next_block(streams[1])
                if not mine or not theirs:
                    break
                frames = min(len(mine), len(theirs)) // SAMPLE_WIDTH
                mine = mine[:frames * SAMPLE_WIDTH]
                theirs = theirs[:frames * SAMPLE_WIDTH]
                self._mic_zero_frames += _zero_samples(mine)
                if not self._write_chunk(interleave_mono(mine, theirs)):
                    break
        except (OSError, ValueError, wave.Error):
            pass

    def _write_chunk(self, chunk):
        mine, theirs = stereo_levels(chunk)
        with self._lock:
            if self._wav is None:
                return False
            self._wav.writeframes(chunk)
            self._frames += len(chunk) // (SAMPLE_WIDTH * 2)
            too_long = self._frames >= self._max_frames
        self.levels.emit(mine, theirs)
        if too_long:
            self._terminate()
            return False
        return True

    def _terminate(self):
        self._stopping = True
        self._terminate_processes()

    def _terminate_processes(self):
        running = [proc for proc in self._procs if proc.poll() is None]
        for proc in running:
            try:
                _interrupt(proc)
            except OSError:
                continue
            # ffmpeg reports being interrupted as a failure; stop() needs to
            # know which exits were our own doing and which were real deaths.
            self._interrupted.add(proc)
        for proc in running:
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                    # Reaped even after a kill, or the child stays a zombie
                    # holding its slot in the process table.
                    proc.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass

    def _close_file(self):
        with self._lock:
            wav, self._wav = self._wav, None
        if wav is not None:
            try:
                wav.close()
            except (OSError, wave.Error):
                pass

    def _error_tail(self):
        tails = [_last_log_line(log) for log in self._logs]
        return " | ".join(tail for tail in tails if tail)

    def _finish_process(self):
        self._terminate()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        codes = []
        for proc in self._procs:
            code = proc.poll()
            # A nonzero exit from a process we interrupted ourselves is ffmpeg
            # complaining about our own stop; one that had already died on its
            # own keeps its code, because that one is the story.
            if code and proc in self._interrupted:
                code = 0
            codes.append(code)
        code = next((value for value in codes if value), 0)
        self._procs = []
        self._close_file()
        return code

    def cancel(self):
        self._cancelled = True
        self._finish_process()
        self._drop_log()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def stop(self):
        if not self._procs:
            return
        # The count is read after the join: the pump thread is still appending
        # the last blocks up to the moment it ends.
        code = self._finish_process()
        frames = self._frames
        if self._cancelled:
            self._drop_log()
            return

        # SIGINT is how the recording ends, and ffmpeg reports being interrupted
        # as a failure; only complain when nothing was captured either.
        if frames < MIN_FRAMES:
            tail = self._error_tail()
            self._drop_log()
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self.failed.emit(
                t("Nothing was recorded: {error}", error=tail or f"ffmpeg → {code}")
                if tail or code else t("Recording too short, speak for at least 0.3 s")
            )
            return
        self._drop_log()
        # A microphone that handed nothing over costs the left channel, and the
        # recording is kept anyway: the right one is everyone else, and an hour
        # of them is worth more than an empty channel costs. Only the split
        # capture can starve a device this way; one ffmpeg reading both cannot.
        if (self._split_inputs and frames >= RATE * QUIET_MIC_SECONDS
                and self._mic_zero_frames / frames > QUIET_MIC_SHARE):
            self.warned.emit(t(
                "The microphone handed over almost nothing ({percent}% of the "
                "recording was empty), so your own side of the meeting will be "
                "mostly missing. Check the device before the next one.",
                percent=round(self._mic_zero_frames / frames * 100),
            ))
        self.stopped.emit(self._path, frames / RATE)

    def _drop_log(self):
        for log in self._logs:
            _close_log(log)
        self._logs = []


def _last_log_line(log):
    """The last thing a recorder said before it ended, or ''."""
    try:
        log.seek(0)
        text = log.read().decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _close_log(log):
    if log is None:
        return
    try:
        log.close()
    except OSError:
        pass


def chunk_levels(chunk):
    """(peak, rms) in 0..1. Peak drives the waveform, RMS drives the silence check."""
    samples = array.array("h")
    usable = len(chunk) - (len(chunk) % 2)
    if usable <= 0:
        return 0.0, 0.0
    samples.frombytes(chunk[:usable])
    peak = max(abs(min(samples)), abs(max(samples))) / 32768.0
    power = (sumprod(samples, samples) if sumprod is not None
             else sum(s * s for s in samples))
    rms = math.sqrt(power / len(samples)) / 32768.0
    return min(1.0, peak), min(1.0, rms)


def stereo_levels(chunk):
    """(left peak, right peak) in 0..1 from interleaved stereo s16."""
    samples = array.array("h")
    usable = len(chunk) - (len(chunk) % 4)
    if usable <= 0:
        return 0.0, 0.0
    samples.frombytes(chunk[:usable])
    left, right = samples[0::2], samples[1::2]
    return _peak(left), _peak(right)


def interleave_mono(left, right):
    """Two mono-s16 buffers into one stereo-s16 buffer, the shorter one setting
    the length."""
    left_samples, right_samples = _samples(left), _samples(right)
    frames = min(len(left_samples), len(right_samples))
    stereo_samples = array.array("h", bytes(frames * 2 * SAMPLE_WIDTH))
    stereo_samples[0::2] = left_samples[:frames]
    stereo_samples[1::2] = right_samples[:frames]
    return stereo_samples.tobytes()


def _read_blocks(stream, blocks, size):
    """One stream's blocks onto its queue, ending with the empty one.

    A queue that stays full is the pump having given up on this recording, and
    then there is nobody left to hand anything to.
    """
    try:
        while True:
            block = _read_exact(stream, size)
            blocks.put(block, timeout=STALL_SECONDS)
            if not block:
                return
    except (OSError, ValueError, queue.Full):
        pass


def _next_block(blocks):
    """The next block of a stream, empty once it ends or falls silent."""
    try:
        return blocks.get(timeout=STALL_SECONDS)
    except queue.Empty:
        return b""


def _read_exact(stream, size):
    """Read one meter-sized block, tolerating short unbuffered pipe reads."""
    out = bytearray()
    while len(out) < size:
        chunk = stream.read(size - len(out))
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def _zero_samples(chunk):
    return _samples(chunk).count(0)


def _samples(chunk):
    samples = array.array("h")
    samples.frombytes(chunk[:len(chunk) - len(chunk) % SAMPLE_WIDTH])
    return samples


def _peak(samples):
    if not samples:
        return 0.0
    return min(1.0, max(abs(min(samples)), abs(max(samples))) / 32768.0)


# --- the sound system, one group per machine -------------------------------

# How the one PulseAudio process merges: each input down to mono at our own
# rate, then the two of them into the left and right of one stream. A Mac does
# the first half per process and the second half itself, in interleave_mono().
MERGE_FILTER = (
    f"[0:a]aresample={RATE}:async=1,aformat=sample_fmts=s16:channel_layouts=mono[m];"
    f"[1:a]aresample={RATE}:async=1,aformat=sample_fmts=s16:channel_layouts=mono[s];"
    "[m][s]amerge=inputs=2[out]"
)


# Whether pw-record takes --raw, asked of the binary once per process: the
# probe costs a subprocess, and the answer cannot change under a running
# application. Kept here rather than inside _pw_record_raw_option so the probe
# itself stays testable against different binaries.
_PW_RAW = None


def _pulse_record(target):
    """parec, or pw-record where PulseAudio's tools were left out.

    parec works with both PulseAudio and PipeWire's PulseAudio compatibility
    service, and its source names are the same ones shown by list_sources().
    Keep pw-record as the fallback for minimal native-PipeWire installations.
    """
    global _PW_RAW
    if shutil.which("parec"):
        cmd = [
            "parec", "--record", "--raw", f"--rate={RATE}",
            f"--channels={CHANNELS}", "--format=s16le",
            # Left alone, parec holds about two seconds before handing anything
            # over, and then hands over all of it at once: the level meter sits
            # still and jumps, and the tail of a recording can be lost on the
            # way out. A chunk of the meter is the unit the rest of this file
            # is measured in, so ask for that.
            f"--latency-msec={CHUNK_LATENCY_MS}",
        ]
        if target:
            cmd.append(f"--device={target}")
        return cmd
    if shutil.which("pw-record"):
        if _PW_RAW is None:
            _PW_RAW = _pw_record_raw_option()
        cmd = [
            "pw-record", *_PW_RAW, f"--rate={RATE}",
            f"--channels={CHANNELS}", "--format=s16",
        ]
        if target:
            cmd.append(f"--target={target}")
        cmd.append("-")
        return cmd
    return []


def _pw_record_raw_option():
    """Use --raw only on pw-record releases that provide it.

    PipeWire gained --raw in 1.4, and in the same release stopped treating a
    filename of "-" as raw on its own: before it, the option is refused and the
    recorder dies before any sound arrives; after it, leaving the option out
    wraps the stream in a container the rest of this file would read as noise.
    Ubuntu 24.04 and anything else still on 1.0 or 1.2 sit on the near side of
    that line, so ask the installed binary which form it understands.
    """
    try:
        # utf-8 spelled out: subprocess otherwise decodes with the locale's
        # codec, and help text through a codec it was not written in raises.
        result = subprocess.run(
            ["pw-record", "--help"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=2,
        )
        help_text = (result.stdout or "") + (result.stderr or "")
    except (subprocess.SubprocessError, OSError):
        return ["--raw"]  # preserve the existing command when probing itself fails
    if not help_text.strip():
        return ["--raw"]
    return ["--raw"] if "--raw" in help_text else []


def _pulse_meeting(mic_target, system_target):
    """One process for both devices: PulseAudio keeps them aligned itself."""
    return [[
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "pulse", "-thread_queue_size", "4096", "-i", mic_target or "default",
        "-f", "pulse", "-thread_queue_size", "4096", "-i", system_target,
        "-filter_complex", MERGE_FILTER, "-map", "[out]",
        "-f", "s16le", "-ar", str(RATE), "-",
    ]]


def _pactl_sources():
    if not shutil.which("pactl"):
        return []
    try:
        # utf-8 spelled out: device descriptions carry whatever alphabet the
        # machine speaks, and the locale's codec is not always able to say so.
        out = subprocess.run(
            ["pactl", "-f", "json", "list", "sources"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=True,
        ).stdout
        return json.loads(out)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return []


def _pulse_inputs():
    return [
        (src.get("name", ""), src.get("description") or src.get("name", ""))
        for src in _pactl_sources()
        if not src.get("name", "").endswith(".monitor")
    ]


def _pulse_outputs():
    return [
        (src.get("name", ""), src.get("description") or src.get("name", ""))
        for src in _pactl_sources()
        if src.get("name", "").endswith(".monitor")
    ]


def _pulse_default_output():
    if not shutil.which("pactl"):
        return ""
    try:
        sink = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""
    if not sink:
        return ""
    monitor = f"{sink}.monitor"
    names = {name for name, _ in _pulse_outputs()}
    return monitor if not names or monitor in names else ""


# macOS hands out no monitor of its own: what the speakers are playing is not
# an input, and the only way to record it is a driver that pretends to be one.
# These are the three people install.
LOOPBACK_DEVICES = ("blackhole", "loopback", "soundflower")


def _avfoundation_record(target):
    if not shutil.which("ffmpeg"):
        return []
    target = _resolve_avfoundation_target(target)
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        # AVFoundation names an input "video:audio", so the empty half in front
        # of the colon is what says this recording has no picture in it.
        "-f", "avfoundation", "-i", f":{target or 'default'}",
        "-ac", str(CHANNELS), "-ar", str(RATE), "-f", "s16le", "-",
    ]


def _avfoundation_meeting(mic_target, system_target):
    """A process per device, both names read off the same device listing.

    Asking ffmpeg what is plugged in costs a process of its own, and a listing
    taken twice could renumber in between: the two targets have to be resolved
    against the same one to name the same machine the user picked from.
    """
    inputs = _avfoundation_inputs()
    return [_avfoundation_meeting_capture(mic_target, inputs),
            _avfoundation_meeting_capture(system_target, inputs)]


def _avfoundation_meeting_capture(target, inputs=None):
    target = _resolve_avfoundation_target(target, inputs)
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-thread_queue_size", "4096",
        "-f", "avfoundation", "-i", f":{target or 'default'}",
        "-af", (f"aresample={RATE}:async=1:first_pts=0,"
                "aformat=sample_fmts=s16:channel_layouts=mono"),
        "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-",
    ]


def _avfoundation_inputs():
    """[(index, name)] for every capture device AVFoundation offers.

    The index is what the recorder is given, because that is what ffmpeg takes;
    it changes when devices are plugged in, which is why the name is shown.
    """
    if not shutil.which("ffmpeg"):
        return []
    try:
        # Listing devices is not a thing ffmpeg can do without an input, so it
        # is asked for one it cannot open: the list comes out on stderr and the
        # command then fails, which is the documented way of doing this.
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    devices, listing = [], False
    for line in result.stderr.splitlines():
        if "AVFoundation audio devices:" in line:
            listing = True
            continue
        if not listing:
            continue
        match = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if match:
            devices.append((match.group(1), match.group(2).strip()))
    return devices


def _avfoundation_named_inputs():
    """Stable settings values: the name is saved, never the moving index."""
    return [(description, description)
            for _index, description in _avfoundation_inputs()]


def _resolve_avfoundation_target(target, inputs=None):
    """Resolve a stored device name to its current, positional ffmpeg index."""
    if not target or target == "default":
        return "default"
    if str(target).isdigit():
        raise AudioDeviceError(t(
            "The saved macOS audio device uses an old numeric index. Open "
            "Settings and select the device again before recording."
        ))
    if inputs is None:
        inputs = _avfoundation_inputs()
    matches = [index for index, description in inputs
               if description == target]
    if not matches:
        raise AudioDeviceError(t(
            "The saved macOS audio device is no longer connected: {device}. "
            "Open Settings and select another device.", device=target,
        ))
    if len(matches) > 1:
        raise AudioDeviceError(t(
            "More than one macOS audio device is named {device}. Disconnect the "
            "duplicate or choose a different device.", device=target,
        ))
    return matches[0]


def _avfoundation_default_output():
    for _index, description in _avfoundation_inputs():
        if any(word in description.lower() for word in LOOPBACK_DEVICES):
            return description
    return ""


# Windows records through DirectShow, the one capture API ffmpeg's Windows
# builds all ship with. What the speakers are playing is not offered as a
# device at all, so a meeting has nothing to record the far side from yet.


# A device entry and the line under it, in the two shapes ffmpeg has printed
# this listing in. Newer builds mark each device `(audio)` or `(video)`; older
# ones print no marker and group the devices under a heading instead. Both are
# anchored at each end, so that the error lines the command ends with, which
# quote the device name that was not found, are not read as devices. The
# bracketed prefix is not pinned to a spelling: ffmpeg 8 writes `[in#0 @ ...]`
# where the versions before it wrote `[dshow @ ...]`.
_DSHOW_ENTRY = re.compile(
    r'^(?:\[[^\]]*\]\s*)?"([^"]+)"\s*(?:\(([^)]*)\))?\s*$')
_DSHOW_ALTERNATIVE = re.compile(
    r'^(?:\[[^\]]*\]\s*)?Alternative name\s+"([^"]+)"\s*$')
_DSHOW_HEADING = re.compile(r'DirectShow (audio|video) devices')

# The last listing taken, so that a dictation does not pay for one of its own.
_DSHOW_SEEN = []


def _parse_dshow_listing(text):
    """[(id, name)] for the audio devices in one ffmpeg device listing.

    Two friendly names on one machine are routinely identical: a laptop with a
    headset plugged in shows two microphones called the same thing, and
    `audio=<name>` would reach only the first of them either way. The
    alternative name ffmpeg prints under each device is unique and is what the
    recorder is given back, while the friendly name is what a user picks from.
    """
    devices = []
    heading = ""
    for line in text.splitlines():
        found = _DSHOW_HEADING.search(line)
        if found:
            heading = found.group(1)
            continue
        found = _DSHOW_ALTERNATIVE.match(line.strip())
        if found:
            if devices:
                devices[-1][0] = found.group(1)
            continue
        found = _DSHOW_ENTRY.match(line.strip())
        if found:
            kind = (found.group(2) or heading).lower()
            devices.append([found.group(1), found.group(1), kind])
    return [(identifier, name) for identifier, name, kind in devices
            if "audio" in kind]


def _dshow_devices():
    """[(id, name)] for every DirectShow audio capture device, freshly asked.

    The list comes out on stderr of a command that then fails, the same
    documented trick AVFoundation uses above.
    """
    if not shutil.which("ffmpeg"):
        return []
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=8, check=False, creationflags=NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    devices = _parse_dshow_listing(result.stderr.decode("utf-8", "replace"))
    _DSHOW_SEEN[:] = devices
    return devices


def _dshow_first_device():
    """The device an unset target stands for, without a listing per dictation.

    dshow has no "default" for an empty target to mean, so it has to be turned
    into a name, and asking ffmpeg for one costs a process every time the key
    is pressed. The last listing is used when there is one: opening Settings or
    running `dikte devices` takes a fresh one, which is what somebody who has
    just plugged a microphone in does anyway.
    """
    devices = _DSHOW_SEEN or _dshow_devices()
    return devices[0][0] if devices else ""


def _dshow_record(target):
    if not shutil.which("ffmpeg"):
        return []
    device = target or _dshow_first_device()
    if not device:
        return []
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        # dshow holds half a second of audio before handing anything over;
        # asked for the chunk the level meter is measured in instead.
        "-f", "dshow", "-audio_buffer_size", str(CHUNK_LATENCY_MS),
        "-i", f"audio={device}",
        "-ac", str(CHANNELS), "-ar", str(RATE), "-f", "s16le", "-",
    ]


def _dshow_meeting(mic_target, system_target):
    return []  # no monitor devices to record the far side from


def _dshow_no_outputs():
    return []


def _dshow_no_default_output():
    return ""


Sound = collections.namedtuple(
    "Sound",
    # How to capture one source and how to capture two at once, that one as the
    # list of processes it takes, the two device lists, which device a meeting
    # records the far side from, whether this system can record one at all, and
    # what to say when the programs for any of it are not installed.
    #
    # `meetings` is the sound system's own answer, not this machine's: an empty
    # output list means the tool that lists them is missing, which is a thing a
    # user can go and fix, while False here is a thing they cannot.
    "record meeting inputs outputs default_output meetings missing",
)

PULSE = Sound(
    record=_pulse_record,
    meeting=_pulse_meeting,
    inputs=_pulse_inputs,
    outputs=_pulse_outputs,
    default_output=_pulse_default_output,
    meetings=True,
    missing="No audio recorder found. Install pulseaudio-utils or pipewire-audio.",
)

COREAUDIO = Sound(
    record=_avfoundation_record,
    meeting=_avfoundation_meeting,
    inputs=_avfoundation_named_inputs,
    # Every macOS capture device is offered as the far side of a meeting, the
    # loopback driver among them: there is no way to tell them apart, and an
    # empty list would leave nothing to pick.
    outputs=_avfoundation_named_inputs,
    default_output=_avfoundation_default_output,
    # With a loopback driver installed, which is what the Settings note is for.
    meetings=True,
    missing="ffmpeg not found. Install it with: brew install ffmpeg",
)


DSHOW = Sound(
    record=_dshow_record,
    meeting=_dshow_meeting,
    inputs=_dshow_devices,
    outputs=_dshow_no_outputs,
    default_output=_dshow_no_default_output,
    # Windows offers no capture device for what the speakers are playing, and
    # there is no driver to install that would add one.
    meetings=False,
    missing="ffmpeg or a microphone was not found. Install ffmpeg with: "
            "winget install Gyan.FFmpeg",
)


def sound():
    """The programs this machine records through."""
    if sys.platform == "darwin":
        return COREAUDIO
    if sys.platform == "win32":
        return DSHOW
    return PULSE


def list_sources():
    """[(name, description)] for every real input source."""
    return sound().inputs()


def list_monitors():
    """[(name, description)] for whatever can be recorded as the other side.

    On Linux that is the monitor of an output, and recording it is recording
    whatever is being played: in a meeting the other participants, and nothing
    of your own microphone.
    """
    return sound().outputs()


def default_monitor():
    """The device the far side of a meeting comes from, or ''."""
    return sound().default_output()
