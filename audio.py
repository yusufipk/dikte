"""Raw PCM capture with a live level meter.

Dictation records one source through pw-record. A meeting records two of them at
once, the microphone and what comes out of the speakers, and for that it goes
through ffmpeg instead: one process reading both devices and merging them into
the two channels of a single stream, which is the only way the two stay aligned
with each other over an hour.
"""

import array
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import wave

from PyQt6.QtCore import QObject, pyqtSignal

import platform_utils as plat
from i18n import t

RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # s16
CHUNK_FRAMES = 1024
CHUNK_BYTES = CHUNK_FRAMES * SAMPLE_WIDTH * CHANNELS
MIN_FRAMES = int(RATE * 0.25)


class Recorder(QObject):
    """Runs pw-record as a child process and reads raw PCM from its stdout."""

    level = pyqtSignal(float)              # 0.0 - 1.0, for the waveform
    started = pyqtSignal()                 # the first sample arrived
    stopped = pyqtSignal(str, float, object)  # wav path, duration (s), per-chunk RMS
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._thread = None
        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, target="", max_seconds=300):
        if self.active:
            return

        if plat.IS_WINDOWS:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                self.failed.emit(t("ffmpeg not found. Install it to record audio."))
                return
            target = target or _win_default_input()
            if not target:
                self.failed.emit(t(
                    "No microphone found. Check that one is plugged in and "
                    "allowed under Windows Settings → Privacy → Microphone."
                ))
                return

            cmd = [
                ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                # DirectShow hands over a whole buffer at a time, and its
                # default is a quarter of a second of one. Shortening it takes
                # that much off the wait before the first sample arrives, which
                # for something started by a key press is felt directly.
                "-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={target}",
                "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-"
            ]
        else:
            if not shutil.which("pw-record"):
                self.failed.emit(t("pw-record not found. Is pipewire-audio installed?"))
                return

            pw_record = shutil.which("pw-record")
            cmd = [
                pw_record,
                "--raw",
                f"--rate={RATE}",
                f"--channels={CHANNELS}",
                "--format=s16",
            ]
            if target:
                cmd.append(f"--target={target}")
            cmd.append("-")

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                **plat.no_window()
            )
        except OSError as exc:
            self.failed.emit(t("Could not start recording: {error}", error=exc))
            return

        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._max_bytes = int(max_seconds * RATE * SAMPLE_WIDTH * CHANNELS)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        stdout = self._proc.stdout
        opening = True
        try:
            while True:
                chunk = stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                if opening:
                    opening = False
                    self.started.emit()
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

    def _terminate(self):
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                if plat.IS_WINDOWS:
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
                proc.wait(timeout=1.5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

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


def write_wav(pcm, rate=RATE, channels=CHANNELS, width=SAMPLE_WIDTH):
    fd, path = tempfile.mkstemp(prefix="dikte-", suffix=".wav")
    with open(fd, "wb") as raw, wave.open(raw, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return path


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
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.failed.emit(t("ffmpeg not found. Install it to record a meeting."))
            return
        if not system_target:
            system_target = default_monitor()
        if not system_target:
            # Windows only lets a program record the speakers through a
            # loopback device the sound card has to offer, and most ship with
            # it switched off. Say where the switch is rather than which
            # setting is empty.
            self.failed.emit(t(
                "Windows has no device that records what the speakers are "
                "playing. Turn on \"Stereo Mix\": right-click the speaker icon "
                "→ Sound settings → More sound settings → Recording, "
                "right-click in the list, show disabled devices, and enable it."
            ) if plat.IS_WINDOWS else t(
                "Could not work out which speaker output to record. "
                "Pick one in Settings → Meeting."
            ))
            return
        if plat.IS_WINDOWS and not mic_target:
            mic_target = _win_default_input()
            if not mic_target:
                self.failed.emit(t(
                    "No microphone found. Check that one is plugged in and "
                    "allowed under Windows Settings → Privacy → Microphone."
                ))
                return

        merge = (
            "[0:a]aresample={rate}:async=1,aformat=sample_fmts=s16:channel_layouts=mono[m];"
            "[1:a]aresample={rate}:async=1,aformat=sample_fmts=s16:channel_layouts=mono[s];"
            "[m][s]amerge=inputs=2[out]"
        ).format(rate=RATE)

        if plat.IS_WINDOWS:
            inputs = [
                "-f", "dshow", "-thread_queue_size", "4096",
                "-audio_buffer_size", "80", "-i", f"audio={mic_target}",
                "-f", "dshow", "-thread_queue_size", "4096",
                "-audio_buffer_size", "80", "-i", f"audio={system_target}",
            ]
        else:
            inputs = [
                "-f", "pulse", "-thread_queue_size", "4096",
                "-i", mic_target or "default",
                "-f", "pulse", "-thread_queue_size", "4096",
                "-i", system_target,
            ]
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"] + inputs + [
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
                cmd, stdout=subprocess.PIPE, stderr=self._log, bufsize=0,
                **plat.no_window()
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
                chunk = stdout.read(block)
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
                if plat.IS_WINDOWS:
                    proc.terminate()
                else:
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
    if plat.IS_WINDOWS:
        return [{"name": open_name, "description": friendly}
                for open_name, friendly in _win_audio_devices()]
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


def refresh_devices():
    """Forget the remembered device list, so the next question asks the system."""
    if plat.IS_WINDOWS:
        _win_audio_devices(refresh=True)


def _pairs(sources):
    return [(src.get("name", ""), src.get("description") or src.get("name", ""))
            for src in sources]


def list_sources():
    """[(name, description)] for every real input source."""
    if plat.IS_WINDOWS:
        # A loopback belongs under "what the speakers are playing", not under
        # microphones, even though DirectShow lists the two together.
        return [(name, description) for name, description in _pairs(_sources())
                if not _is_loopback(name, description)]
    return [(name, description) for name, description in _pairs(_sources())
            if not name.endswith(".monitor")]


def list_monitors():
    """[(name, description)] for the monitor of every output.

    Recording a monitor is recording whatever is being played, which in a
    meeting is the other participants and nothing of your own microphone.
    """
    if plat.IS_WINDOWS:
        return [(name, description) for name, description in _pairs(_sources())
                if _is_loopback(name, description)]
    return [(name, description) for name, description in _pairs(_sources())
            if name.endswith(".monitor")]


def default_monitor():
    """The monitor of the output sound is currently going to, or ''."""
    if plat.IS_WINDOWS:
        monitors = list_monitors()
        return monitors[0][0] if monitors else ""
    if not shutil.which("pactl"):
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


# --- Windows devices ------------------------------------------------------
#
# DirectShow is the only capture interface ffmpeg has on Windows, and it names
# devices in the language Windows is installed in. Two things follow. A device
# is opened by its "alternative name" — the GUID line ffmpeg prints under each
# one — because friendly names longer than about 80 characters come back
# truncated and then cannot be opened at all. And a loopback of the speakers
# exists only if the sound card exposes one ("Stereo Mix", "Stereomix",
# "Was Sie hören"), which most laptops ship with disabled.

# Recording the speakers rather than the microphone. Matched case-insensitively
# against both the friendly name and the driver's own, so a Turkish or German
# Windows finds its own wording too.
LOOPBACK_TERMS = (
    "stereo mix", "stereomix", "stereo karışım", "stereo karisim",
    "loopback", "what u hear", "what you hear", "wave out", "wave-out",
    "was sie hören", "mezcla estéreo", "virtual-audio-capturer", "cable output",
    "voicemeeter output", "vb-audio",
)

_win_device_cache = None


def _win_audio_devices(refresh=False):
    """[(open_name, friendly_name)] for every DirectShow audio device.

    Listing them costs an ffmpeg process, and the settings window asks three
    times over while it builds; the answer only changes when hardware is
    plugged in, so it is remembered until something asks for it fresh.
    """
    global _win_device_cache  # pylint: disable=global-statement
    if _win_device_cache is not None and not refresh:
        return _win_device_cache

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    try:
        res = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False, **plat.no_window(),
        )
    except (subprocess.SubprocessError, OSError):
        return []

    _win_device_cache = _parse_dshow_devices(res.stderr or "")
    return _win_device_cache


def _parse_dshow_devices(text):
    """[(open_name, friendly_name)] out of what `-list_devices` printed.

    Newer ffmpeg tags each device "(audio)" or "(video)"; older builds print
    them under a "DirectShow audio devices" heading instead, so both are read.
    """
    devices = []
    section = ""       # what the last heading said these devices are
    pending = ""       # a friendly name still waiting for its alternative name

    def flush():
        # No alternative name was printed, so the friendly one has to serve as
        # both. That is what older ffmpeg does for some drivers.
        nonlocal pending
        if pending:
            devices.append((pending, pending))
            pending = ""

    for line in text.splitlines():
        stripped = re.sub(r"^\[[^\]]*\]\s*", "", line).strip()

        heading = re.match(r"DirectShow (audio|video) devices", stripped)
        if heading:
            flush()
            section = heading.group(1)
            continue

        alternative = re.match(r'Alternative name\s+"(.+)"$', stripped)
        if alternative:
            if pending:
                devices.append((alternative.group(1), pending))
                pending = ""
            continue

        named = re.match(r'"(.+?)"(?:\s+\((audio|video)\))?$', stripped)
        if not named:
            continue
        flush()
        kind = named.group(2) or section
        if kind == "audio":
            pending = named.group(1)

    flush()
    return devices


def _is_loopback(*names):
    return any(term in name.lower() for name in names if name for term in LOOPBACK_TERMS)


def _win_default_input():
    """The microphone to record when none was chosen, or ''."""
    for open_name, friendly in _win_audio_devices():
        if not _is_loopback(open_name, friendly):
            return open_name
    return ""
