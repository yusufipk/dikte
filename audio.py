"""Raw PCM capture with a live level meter.

Dictation records one source through pw-record, or through ffmpeg's
avfoundation input on a Mac, which has no sound server to ask. A meeting
records two sources at once, the microphone and what comes out of the speakers,
and for that it goes through ffmpeg everywhere: one process reading both
devices and merging them into the two channels of a single stream, which is the
only way the two stay aligned with each other over an hour. macOS hands out no
speaker output at all, so a meeting is Linux-only for now.
"""

import array
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave

from PyQt6.QtCore import QObject, pyqtSignal

from i18n import t

RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # s16
CHUNK_FRAMES = 1024
CHUNK_BYTES = CHUNK_FRAMES * SAMPLE_WIDTH * CHANNELS
CHUNK_LATENCY_MS = round(CHUNK_FRAMES / RATE * 1000)
MIN_FRAMES = int(RATE * 0.25)


class Recorder(QObject):
    """Runs the available sound-server recorder and reads raw PCM from stdout."""

    level = pyqtSignal(float)              # 0.0 - 1.0, for the waveform
    stopped = pyqtSignal(str, float, object)  # wav path, duration (s), per-chunk RMS
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._thread = None
        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._stopping = False
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, target="", max_seconds=300):
        if self.active:
            return
        cmd = recording_command(target)
        if not cmd:
            self.failed.emit(missing_recorder_message())
            return

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except OSError as exc:
            self.failed.emit(t("Could not start recording: {error}", error=exc))
            return

        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._stopping = False
        self._max_bytes = int(max_seconds * RATE * SAMPLE_WIDTH * CHANNELS)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        proc = self._proc
        stdout = proc.stdout
        try:
            while True:
                chunk = read_exactly(stdout, CHUNK_BYTES)
                if not chunk:
                    break
                peak, rms = chunk_levels(chunk)
                with self._lock:
                    self._buffer.extend(chunk)
                    self._rms.append(rms)
                    too_long = len(self._buffer) >= self._max_bytes
                self.level.emit(peak)
                if too_long:
                    self._terminate()
                    break
        except (OSError, ValueError):
            pass
        # Nobody asked it to end and it captured nothing: the recorder is not
        # installed properly, or the device was refused. Said out loud here,
        # because stop() would otherwise report it as a recording that was too
        # short, which sends the user looking in the wrong place.
        with self._lock:
            captured = bool(self._buffer)
        if self._stopping or self._cancelled or captured:
            return
        try:
            detail = proc.stderr.read().decode("utf-8", "replace").strip()
        except (AttributeError, OSError):
            detail = ""
        self.failed.emit(t(
            "Audio recorder stopped before receiving sound: {error}",
            error=detail or f"exit code {proc.returncode}",
        ))

    def _terminate(self):
        self._stopping = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=1.5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    def cancel(self):
        self._cancelled = True
        self._terminate()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._proc = None
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

        path = write_wav(pcm)
        self.stopped.emit(path, frames / RATE, rms)


def read_exactly(stream, size):
    """`size` bytes from an unbuffered pipe, or what is left of it at the end.

    `read(n)` hands over what has arrived rather than what was asked for, and
    how much that is belongs to whoever is on the other end: parec, told the
    latency to keep, fills a chunk at a time, while ffmpeg's avfoundation input
    hands over a fraction of one. Both the level meter and the silence check
    are measured in chunks of one known length — vad.analyse() is given the
    seconds a chunk lasts and multiplies — so a stream that arrives in pieces is
    put back together here rather than counted as if each piece were a chunk of
    its own, which would say a two-second recording held fifteen seconds of
    speech.
    """
    parts, remaining = [], size
    while remaining > 0:
        piece = stream.read(remaining)
        if not piece:
            break
        parts.append(piece)
        remaining -= len(piece)
    return b"".join(parts)


def write_wav(pcm, rate=RATE, channels=CHANNELS, width=SAMPLE_WIDTH):
    fd, path = tempfile.mkstemp(prefix="dikte-", suffix=".wav")
    with open(fd, "wb") as raw, wave.open(raw, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return path


def missing_recorder_message():
    """What to install, when recording_command() came back with nothing."""
    if sys.platform == "darwin":
        return t("ffmpeg not found. Install it with: brew install ffmpeg")
    return t("No audio recorder found. Install pulseaudio-utils or pipewire-audio.")


def _avfoundation_command(target=""):
    """Capture through ffmpeg, which is the only way in on a Mac.

    There is no sound server to ask and no small recorder beside it, but ffmpeg
    is already a dependency for the audio-file tab, so this costs nothing extra.
    `default` is avfoundation's own name for whatever the system input is, and a
    target is the device name rather than its index: indexes move when a
    microphone is plugged in, names do not.
    """
    if not shutil.which("ffmpeg"):
        return []
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{target or 'default'}",
        "-ar", str(RATE), "-ac", str(CHANNELS), "-f", "s16le", "-",
    ]


def recording_command(target=""):
    """Return a raw-s16 capture command for the sound server on this desktop.

    parec works with both PulseAudio and PipeWire's PulseAudio compatibility
    service, and its source names are the same ones shown by list_sources().
    Keep pw-record as the fallback for minimal native-PipeWire installations.
    """
    if sys.platform == "darwin":
        return _avfoundation_command(target)
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
        cmd = [
            "pw-record", "--raw", f"--rate={RATE}",
            f"--channels={CHANNELS}", "--format=s16",
        ]
        if target:
            cmd.append(f"--target={target}")
        cmd.append("-")
        return cmd
    return []


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
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._thread = None
        self._wav = None
        self._log = None
        self._path = ""
        self._frames = 0
        self._cancelled = False
        self._stopping = False
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, path, mic_target="", system_target="", max_seconds=14400):
        if self.active:
            return
        if sys.platform == "darwin":
            # Said here rather than left to default_monitor() returning nothing:
            # this is a platform that cannot do it at all, not a machine whose
            # output could not be worked out.
            self.failed.emit(t(
                "Recording a meeting needs what comes out of the speakers, which "
                "macOS does not hand out. Not supported yet."
            ))
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

        merge = (
            "[0:a]aresample={rate}:async=1,aformat=sample_fmts=s16:channel_layouts=mono[m];"
            "[1:a]aresample={rate}:async=1,aformat=sample_fmts=s16:channel_layouts=mono[s];"
            "[m][s]amerge=inputs=2[out]"
        ).format(rate=RATE)
        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "pulse", "-thread_queue_size", "4096", "-i", mic_target or "default",
            "-f", "pulse", "-thread_queue_size", "4096", "-i", system_target,
            "-filter_complex", merge, "-map", "[out]",
            "-f", "s16le", "-ar", str(RATE), "-",
        ]

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._wav = wave.open(path, "wb")
            self._wav.setnchannels(2)
            self._wav.setsampwidth(SAMPLE_WIDTH)
            self._wav.setframerate(RATE)
            # ffmpeg keeps talking to stderr for as long as it runs; a pipe
            # nobody drains would eventually block it, so it writes to a file.
            self._log = tempfile.TemporaryFile()
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=self._log, bufsize=0
            )
        except (OSError, wave.Error) as exc:
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
        self._cancelled = False
        self._stopping = False
        self._max_frames = int(max_seconds * RATE)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        stdout = self._proc.stdout
        block = CHUNK_FRAMES * SAMPLE_WIDTH * 2
        try:
            while True:
                # Whole frames, or the stereo split below would read the two
                # channels the wrong way round for the rest of the meeting.
                chunk = read_exactly(stdout, block)
                if not chunk:
                    break
                mine, theirs = stereo_levels(chunk)
                with self._lock:
                    if self._wav is None:
                        break
                    self._wav.writeframes(chunk)
                    self._frames += len(chunk) // (SAMPLE_WIDTH * 2)
                    too_long = self._frames >= self._max_frames
                self.levels.emit(mine, theirs)
                if too_long:
                    self._terminate()
                    break
        except (OSError, ValueError, wave.Error):
            pass
        # Nobody asked it to end: the sound device went away, or ffmpeg fell
        # over. An hour into a meeting that has to be said out loud rather than
        # discovered afterwards.
        if not self._stopping:
            self.died.emit()

    def _terminate(self):
        self._stopping = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
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
        if self._log is None:
            return ""
        try:
            self._log.seek(0)
            text = self._log.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def _finish_process(self):
        self._terminate()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        code = self._proc.poll() if self._proc else 0
        self._proc = None
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
        if not self._proc:
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
        self.stopped.emit(self._path, frames / RATE)

    def _drop_log(self):
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None


def chunk_levels(chunk):
    """(peak, rms) in 0..1. Peak drives the waveform, RMS drives the silence check."""
    samples = array.array("h")
    usable = len(chunk) - (len(chunk) % 2)
    if usable <= 0:
        return 0.0, 0.0
    samples.frombytes(chunk[:usable])
    peak = max(abs(min(samples)), abs(max(samples))) / 32768.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
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


def _peak(samples):
    if not samples:
        return 0.0
    return min(1.0, max(abs(min(samples)), abs(max(samples))) / 32768.0)


def _sources():
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.run(
            ["pactl", "-f", "json", "list", "sources"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return json.loads(out)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return []


# `[1] MacBook Pro Microphone`, once ffmpeg's own prefix has been taken off.
_AVFOUNDATION_DEVICE = re.compile(r"\[\d+\]\s+(.+)")


def _avfoundation_sources():
    """[(name, name)] for every audio device avfoundation lists.

    ffmpeg prints the list only when it is asked to open a device it cannot
    open, so it always ends in an error and the exit code says nothing. The
    device name is both halves of the pair: macOS has no second, friendlier
    name to show beside it the way PulseAudio does.
    """
    if not shutil.which("ffmpeg"):
        return []
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    out, listening = [], False
    for line in res.stderr.splitlines():
        _, _, rest = line.partition("] ")
        if rest.endswith("devices:"):
            listening = rest.startswith("AVFoundation audio devices")
            continue
        match = _AVFOUNDATION_DEVICE.match(rest) if listening else None
        if match:
            name = match.group(1).strip()
            out.append((name, name))
    return out


def list_sources():
    """[(name, description)] for every real input source."""
    if sys.platform == "darwin":
        return _avfoundation_sources()
    return [
        (src.get("name", ""), src.get("description") or src.get("name", ""))
        for src in _sources()
        if not src.get("name", "").endswith(".monitor")
    ]


def list_monitors():
    """[(name, description)] for the monitor of every output.

    Recording a monitor is recording whatever is being played, which in a
    meeting is the other participants and nothing of your own microphone.
    macOS publishes no such device, so the list is empty there and the meeting
    tab says so rather than offering a choice that cannot be made.
    """
    if sys.platform == "darwin":
        return []
    return [
        (src.get("name", ""), src.get("description") or src.get("name", ""))
        for src in _sources()
        if src.get("name", "").endswith(".monitor")
    ]


def default_monitor():
    """The monitor of the output sound is currently going to, or ''."""
    if sys.platform == "darwin" or not shutil.which("pactl"):
        return ""
    try:
        sink = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""
    if not sink:
        return ""
    monitor = f"{sink}.monitor"
    names = {name for name, _ in list_monitors()}
    return monitor if not names or monitor in names else ""
