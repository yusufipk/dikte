"""Capturing sound on Windows, through WASAPI.

Dictation opens the microphone. A meeting opens the microphone and a loopback
of whatever is being played at the same time, which is what Windows offers
instead of PulseAudio's monitor sources: no "Stereo Mix" device has to exist
and nothing has to be enabled in a driver panel.

The two meeting streams are not simply written side by side. They are two
devices with two clocks, started a few tens of milliseconds apart, and either
of them can stall: a loopback in particular hands over nothing at all while
the speakers are idle on some machines. So each side is converted to the same
16 kHz timeline and a mixer writes the file against the wall clock: a side with
nothing to give gets silence for that stretch, and a side whose clock runs fast
has its backlog trimmed rather than allowed to grow. Over an hour the two stay
inside a mixer block of each other rather than sliding apart, which is the
whole reason a meeting can be split by channel afterwards.

Everything is written to the file as it arrives. An hour is not held in memory,
and a crash costs the tail of the meeting rather than all of it.
"""

import array
import os
import threading
import time
import wave

from PyQt6.QtCore import QObject, pyqtSignal

from i18n import t
from platforms.common.pcm import (
    CHUNK_FRAMES,
    CHANNELS,
    MIN_FRAMES,
    RATE,
    SAMPLE_WIDTH,
    chunk_levels,
    peak_of,
    write_wav,
)
from platforms.windows.resample import Converter

try:
    import pyaudiowpatch as pyaudio
except ImportError:      # pragma: no cover - depends on what is installed
    pyaudio = None

# How often the meeting mixer writes, and how far behind the wall clock a side
# may fall before what it owes is written as silence. Short enough that the
# level meter moves, long enough that it is not a busy loop.
MIX_INTERVAL = 0.05

# A side whose device clock runs ahead of the wall clock builds up a backlog.
# Half a second of it is kept as slack for scheduling jitter; past that, the
# oldest audio is dropped, which is what keeps the two channels together over
# an hour instead of one of them sliding late.
MAX_BACKLOG = int(RATE * 0.5)

SILENCE = b"\x00\x00"

_engine = None
_engine_lock = threading.Lock()


class AudioError(Exception):
    pass


def _audio():
    """The one PyAudio instance, made the first time something needs it.

    Creating it initialises COM and enumerates every endpoint on the machine,
    which is not something to do at import: a command line that only reads the
    history should not touch the sound system at all.
    """
    global _engine
    if pyaudio is None:
        raise AudioError(t(
            "The Windows sound support is missing. Reinstall Dikte, or run: "
            "pip install PyAudioWPatch"
        ))
    with _engine_lock:
        if _engine is None:
            _engine = pyaudio.PyAudio()
        return _engine


def _forget():
    """Drop the cached instance so the next question re-enumerates the devices."""
    global _engine
    with _engine_lock:
        engine, _engine = _engine, None
    if engine is not None:
        try:
            engine.terminate()
        except Exception:      # noqa: BLE001 - it is being thrown away anyway
            pass


def _wasapi_devices():
    """Every WASAPI endpoint, as the dictionaries PyAudio hands out."""
    try:
        engine = _audio()
        host = engine.get_host_api_info_by_type(pyaudio.paWASAPI)
    except (AudioError, OSError, ValueError):
        return []
    found = []
    for slot in range(int(host.get("deviceCount", 0))):
        try:
            found.append(engine.get_device_info_by_host_api_device_index(
                host["index"], slot))
        except (OSError, ValueError):
            continue
    return found


def _inputs():
    return [d for d in _wasapi_devices()
            if int(d.get("maxInputChannels", 0)) > 0
            and not d.get("isLoopbackDevice")]


def _loopbacks():
    return [d for d in _wasapi_devices()
            if int(d.get("maxInputChannels", 0)) > 0
            and d.get("isLoopbackDevice")]


def list_sources():
    """[(name, description)] for every microphone.

    The name is what gets stored in the settings, and it is the device's own
    name rather than its index: indices are renumbered whenever something is
    plugged in, and a microphone chosen last week has to still be the one
    chosen this week.
    """
    return [(d["name"], d["name"]) for d in _inputs()]


def list_monitors():
    """[(name, description)] for every speaker output that can be recorded back.

    Windows exposes one loopback endpoint per output device, named after it, so
    recording a meeting means recording the loopback of whichever output the
    call is being played through.
    """
    return [(d["name"], _monitor_label(d["name"])) for d in _loopbacks()]


def _monitor_label(name):
    """'Speakers [Loopback]' shown the way the rest of the interface reads."""
    trimmed = name.replace("[Loopback]", "").strip()
    return t("Whatever {device} is playing", device=trimmed) if trimmed else name


def default_monitor():
    """The loopback of the output sound is going to right now, or ''."""
    try:
        engine = _audio()
        host = engine.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = engine.get_device_info_by_index(host["defaultOutputDevice"])
    except (AudioError, OSError, ValueError, KeyError):
        return ""
    wanted = str(speakers.get("name", "")).strip()
    if not wanted:
        return ""
    loopbacks = _loopbacks()
    for device in loopbacks:
        if device["name"].startswith(wanted):
            return device["name"]
    for device in loopbacks:
        if wanted in device["name"]:
            return device["name"]
    return loopbacks[0]["name"] if loopbacks else ""


def _match(devices, wanted):
    """The device the settings name, or the default when they name nothing."""
    wanted = (wanted or "").strip()
    if wanted:
        for device in devices:
            if device["name"] == wanted:
                return device
        for device in devices:
            if wanted in device["name"]:
                return device
        return None
    return devices[0] if devices else None


def _default_input():
    try:
        engine = _audio()
        host = engine.get_host_api_info_by_type(pyaudio.paWASAPI)
        return engine.get_device_info_by_index(host["defaultInputDevice"])
    except (AudioError, OSError, ValueError, KeyError):
        return None


def _microphone(target):
    if not (target or "").strip():
        device = _default_input()
        if device is not None and int(device.get("maxInputChannels", 0)) > 0:
            return device
    return _match(_inputs(), target)


def _blocked_message(device, exc):
    """What to say when Windows refused a device.

    Nearly always the privacy setting rather than anything technical: a
    microphone switched off under Settings → Privacy fails to open with an
    access error, and the number PortAudio reports for it means nothing to
    anybody. Say where the switch is.
    """
    text = str(exc)
    denied = "denied" in text.lower() or "-9996" in text or "-9999" in text
    if denied:
        return t(
            "Windows would not open {device}. Check Settings → Privacy & "
            "security → Microphone: desktop apps need microphone access "
            "turned on. ({error})", device=device, error=text,
        )
    return t("Could not start recording on {device}: {error}",
             device=device, error=text)


class _Stream:
    """One WASAPI input stream, converted to 16 kHz mono as it arrives."""

    def __init__(self, device):
        engine = _audio()
        self.name = device["name"]
        self.rate = int(device.get("defaultSampleRate") or RATE)
        self.channels = max(1, int(device.get("maxInputChannels") or 1))
        # A block of roughly the length the level meter reads, in the device's
        # own sample rate, so one read is one update of the waveform.
        self.block = max(256, round(self.rate * CHUNK_FRAMES / RATE))
        self.stream = engine.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=int(device["index"]),
            frames_per_buffer=self.block,
        )
        self.converter = Converter(self.rate, self.channels, RATE)

    @property
    def latency(self):
        try:
            return float(self.stream.get_input_latency())
        except (OSError, ValueError, AttributeError):
            return 0.0

    def read(self):
        """The next block as 16 kHz mono s16 bytes, or b'' when there is none."""
        raw = self.stream.read(self.block, exception_on_overflow=False)
        return self.converter.feed(raw)

    def close(self):
        try:
            self.stream.stop_stream()
        except (OSError, ValueError):
            pass
        try:
            self.stream.close()
        except (OSError, ValueError):
            pass


class Recorder(QObject):
    """The microphone, for one dictation."""

    level = pyqtSignal(float)                 # 0.0 - 1.0, for the waveform
    stopped = pyqtSignal(str, float, object)  # wav path, duration (s), per-chunk RMS
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stream = None
        self._thread = None
        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._stopping = False
        self._error = ""
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, target="", max_seconds=300):
        if self.active:
            return
        device = _microphone(target)
        if device is None:
            self.failed.emit(
                t("No microphone found. Plug one in, or pick another under "
                  "Settings → General.")
                if not target else
                t("The microphone {device} is not there any more. Pick another "
                  "one under Settings → General.", device=target)
            )
            return
        try:
            self._stream = _Stream(device)
        except AudioError as exc:
            self.failed.emit(str(exc))
            return
        except (OSError, ValueError) as exc:
            # The device list is stale once an endpoint has gone; the next
            # attempt should ask Windows again rather than the cache.
            _forget()
            self.failed.emit(_blocked_message(device["name"], exc))
            return

        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._stopping = False
        self._error = ""
        self._max_bytes = int(max_seconds * RATE * SAMPLE_WIDTH * CHANNELS)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        stream = self._stream
        try:
            while not self._stopping and not self._cancelled:
                chunk = stream.read()
                if not chunk:
                    continue
                peak, rms = chunk_levels(chunk)
                with self._lock:
                    self._buffer.extend(chunk)
                    self._rms.append(rms)
                    too_long = len(self._buffer) >= self._max_bytes
                self.level.emit(peak)
                if too_long:
                    self._stopping = True
                    break
        except (OSError, ValueError) as exc:
            self._error = str(exc)
        # Nobody asked it to end and it captured nothing: the device was taken
        # away, or Windows refused it after the open. Said out loud here,
        # because stop() would otherwise report it as a recording that was too
        # short, which sends the user looking in the wrong place.
        with self._lock:
            captured = bool(self._buffer)
        if self._stopping or self._cancelled or captured:
            return
        self.failed.emit(t(
            "The microphone stopped before any sound arrived: {error}",
            error=self._error or t("the device went away"),
        ))

    def _finish(self):
        self._stopping = True
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def cancel(self):
        self._cancelled = True
        self._finish()
        with self._lock:
            self._buffer = bytearray()

    def stop(self):
        """End the recording and write the WAV file."""
        if self._stream is None and self._thread is None:
            return
        self._finish()

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


class _Side:
    """One half of a meeting: a stream, what it has produced, and where it is."""

    def __init__(self, label):
        self.label = label
        self.stream = None
        self.thread = None
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.primed = False
        self.alive = True
        self.error = ""

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def _interleave(left, right):
    """Two equal-length mono buffers into one stereo buffer."""
    first, second = array.array("h"), array.array("h")
    first.frombytes(left)
    second.frombytes(right)
    out = array.array("h")
    for mine, theirs in zip(first, second):
        out.append(mine)
        out.append(theirs)
    return out.tobytes(), peak_of(first), peak_of(second)


class MeetingRecorder(QObject):
    """Microphone and speaker output into one stereo file: left is you, right is
    everyone else.

    Who said what then needs no guessing at all, because the two voices never
    shared a channel to begin with.
    """

    levels = pyqtSignal(float, float)      # mine, theirs
    stopped = pyqtSignal(str, float)       # wav path, duration (s)
    died = pyqtSignal()                    # a device went away on its own
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mic = _Side("microphone")
        self._system = _Side("system")
        self._mixer = None
        self._wav = None
        self._path = ""
        self._frames = 0
        self._max_frames = 0
        self._cancelled = False
        self._stopping = False
        self._fault = ""
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._mixer is not None and self._mixer.is_alive()

    def start(self, path, mic_target="", system_target="", max_seconds=14400):
        if self.active:
            return
        microphone = _microphone(mic_target)
        if microphone is None:
            self.failed.emit(t("No microphone found. Plug one in, or pick "
                               "another under Settings → General."))
            return
        loopback = _match(_loopbacks(), system_target or default_monitor())
        if loopback is None:
            self.failed.emit(t("Could not work out which speaker output to "
                               "record. Pick one in Settings → Meeting."))
            return

        self._mic = _Side("microphone")
        self._system = _Side("system")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._wav = wave.open(path, "wb")
            self._wav.setnchannels(2)
            self._wav.setsampwidth(SAMPLE_WIDTH)
            self._wav.setframerate(RATE)
        except (OSError, wave.Error) as exc:
            self._close_file()
            self.failed.emit(t("Could not start recording: {error}", error=exc))
            return

        try:
            self._mic.stream = _Stream(microphone)
            self._system.stream = _Stream(loopback)
        except AudioError as exc:
            self._abandon(path)
            self.failed.emit(str(exc))
            return
        except (OSError, ValueError) as exc:
            failed = (microphone["name"] if self._mic.stream is None
                      else loopback["name"])
            self._abandon(path)
            _forget()
            self.failed.emit(_blocked_message(failed, exc))
            return

        self._path = path
        self._frames = 0
        self._cancelled = False
        self._stopping = False
        self._max_frames = int(max_seconds * RATE)

        # The clock both sides are written against. Taken after both streams
        # are open, so that neither one's opening cost is charged to the other.
        started = time.perf_counter()
        for side in (self._mic, self._system):
            side.thread = threading.Thread(
                target=self._pump, args=(side, started), daemon=True)
            side.thread.start()
        self._mixer = threading.Thread(target=self._mix, args=(started,),
                                       daemon=True)
        self._mixer.start()

    def _abandon(self, path):
        """Give up before anything was recorded, leaving no empty header behind."""
        self._mic.close()
        self._system.close()
        self._close_file()
        try:
            os.unlink(path)
        except OSError:
            pass

    def _pump(self, side, started):
        """Read one device until it is done, onto the common timeline."""
        try:
            while not self._stopping:
                chunk = side.stream.read()
                if not chunk:
                    continue
                with side.lock:
                    if not side.primed:
                        side.primed = True
                        # Where this block belongs: it was captured a stream's
                        # latency ago, and it is one block long. Whatever comes
                        # before that on the shared clock is silence this
                        # device was not there for.
                        span = len(chunk) / (SAMPLE_WIDTH * RATE)
                        lead = (time.perf_counter() - side.stream.latency
                                - span - started)
                        side.buffer += SILENCE * max(0, int(lead * RATE))
                    side.buffer += chunk
        except (OSError, ValueError) as exc:
            side.error = str(exc)
        finally:
            side.alive = False

    def _take(self, side, frames):
        """The next `frames` of that side, padded with silence when it is short."""
        want = frames * SAMPLE_WIDTH
        with side.lock:
            # A device clock running ahead of the wall clock: keep half a
            # second of slack for scheduling and drop what is older, which is
            # what stops the two channels sliding apart over an hour.
            excess = len(side.buffer) - want - MAX_BACKLOG * SAMPLE_WIDTH
            if excess > 0:
                del side.buffer[:excess - (excess % SAMPLE_WIDTH)]
            data = bytes(side.buffer[:want])
            del side.buffer[:len(data)]
        if len(data) < want:
            data += SILENCE * ((want - len(data)) // SAMPLE_WIDTH)
        return data

    def _mix(self, started):
        """Write the file against the wall clock, whatever the devices do."""
        while not self._stopping:
            time.sleep(MIX_INTERVAL)
            if self._stopping:
                break
            frames = int((time.perf_counter() - started) * RATE) - self._frames
            if frames <= 0:
                continue
            block, mine, theirs = _interleave(self._take(self._mic, frames),
                                              self._take(self._system, frames))
            with self._lock:
                if self._wav is None:
                    break
                try:
                    self._wav.writeframes(block)
                except (OSError, ValueError, wave.Error) as exc:
                    self._fault = str(exc)
                    break
                self._frames += frames
                too_long = self._frames >= self._max_frames
            self.levels.emit(mine, theirs)
            if too_long:
                self._stopping = True
                break
            # A device that went away is the end of the recording: an hour into
            # a meeting that has to be said out loud rather than discovered
            # afterwards.
            if not (self._mic.alive and self._system.alive):
                self._stopping = True
                self.died.emit()
                break

    def _close_file(self):
        with self._lock:
            wav, self._wav = self._wav, None
        if wav is not None:
            try:
                wav.close()
            except (OSError, wave.Error):
                pass

    def _finish(self):
        self._stopping = True
        for side in (self._mic, self._system):
            if side.thread:
                side.thread.join(timeout=3)
            side.thread = None
            side.close()
        if self._mixer:
            self._mixer.join(timeout=3)
        self._mixer = None
        self._close_file()

    def cancel(self):
        self._cancelled = True
        self._finish()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def stop(self):
        if self._mixer is None and self._wav is None:
            return
        self._finish()
        frames = self._frames
        if self._cancelled:
            return

        if frames < MIN_FRAMES:
            trouble = self._fault or self._mic.error or self._system.error
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self.failed.emit(
                t("Nothing was recorded: {error}", error=trouble) if trouble
                else t("Recording too short, speak for at least 0.3 s")
            )
            return
        self.stopped.emit(self._path, frames / RATE)
