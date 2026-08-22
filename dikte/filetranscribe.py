"""Transcribe an existing audio/video file with the same models.

ffmpeg converts whatever comes in to 16 kHz mono WAV, and for a hosted API to
mp3 on top of that. Two things decide where a file is cut up: the upload limit,
which uncompressed audio reaches after ten minutes where mp3 takes an hour, and
the clock. An hour of audio in one request is minutes of work at the other end,
and the gateway in front of the model hangs up long before the answer comes
back, which arrives here as a 502 with the whole chunk lost. So a chunk is also
capped at MAX_CHUNK_SECONDS however small it is on disk.

A cut is not free, which is what the encoder buys and why nothing is cut more
finely than that. Whisper hears in thirty second windows and decides for itself
where one cue ends and the next begins; a chunk that starts in the middle of a
sentence can come back as one cue per window, twenty seconds of text at a time,
for the whole rest of the chunk. So what is cut overlaps, and stitch() drops
the half that was heard twice.
"""

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave

from PyQt6.QtCore import QObject, pyqtSignal

from . import api
from . import cleanup
from . import ggml
from . import paths
from .i18n import t

UPLOAD_LIMIT = 24 * 1024 * 1024  # the APIs take 25 MB; leave the form its room
MAX_CHUNK_SECONDS = 900      # as much audio as a hosted request can outlive
MP3_BITRATE = "48k"          # mono speech at 16 kHz: whisper hears nothing less
OVERLAP_SECONDS = 30         # a whisper window: how far back a chunk starts
WAV_CHUNK_SECONDS = 600      # 19 MB, for the caller that uploads the WAV itself
CLEANUP_CHUNK_CHARS = 12000  # keep each cleanup call comfortably small
HOSTED_TIMEOUT = 600         # a quarter hour of audio, with room for the upload
RETRIES = 3                  # how many times one chunk is asked for in all
RETRY_WAIT = 5               # seconds before the second try, doubled after that
RATE = 16000
MIN_SUBTITLE_SECONDS = 1.5   # how long a cue with no end time of its own stays up

# The [mm:ss] or [h:mm:ss] prefix a timestamped line starts with.
STAMP_RE = re.compile(r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\]\s*")


# What a stopped run comes back with, wherever it was stopped: the request that
# was cut off raises it from api, and the steps in between raise it themselves.
Cancelled = api.Aborted


class FileTranscriber(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, list)   # text, [(start, end, text)] when timestamped
    failed = pyqtSignal(str)

    def __init__(self, conf, parent=None):
        super().__init__(parent)
        self.conf = conf
        self._thread = None
        self._abort = api.Aborter()
        # The server on this machine the work is with, when it is with one.
        self._local = None

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, path, timestamps, do_cleanup):
        if self.busy:
            return
        self._abort = api.Aborter()      # the last one is spent
        self._thread = threading.Thread(
            target=self._work, args=(path, timestamps, do_cleanup), daemon=True
        )
        self._thread.start()

    def stop(self):
        """Cut the run off where it stands, rather than at the next step."""
        self._abort.abort()
        # Closing the socket is nothing to a server on this machine: it is a
        # process of ours, and it would grind on to the end of the chunk with
        # nobody left to hand the answer to. Stopping it is what stops the
        # work; the next run starts it again. Killing waits on the process, so
        # not on the thread the window is drawn from.
        local = self._local
        if local is not None:
            threading.Thread(target=local.stop, daemon=True).start()

    def _check(self):
        self._abort.check()

    def _wait(self, seconds):
        """Sleep on it, with the Stop button still able to get through."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check()
            time.sleep(0.25)
        self._check()

    def _attempt(self, call, stage):
        """`call`, asked again when what failed was the network rather than us.

        One chunk is a quarter hour of audio that took a minute to encode and a
        minute to upload, so a gateway having a bad moment is worth waiting out
        rather than throwing the run away over. `stage` is what the status line
        said before the failure, put back once the wait is over.
        """
        for attempt in range(1, RETRIES + 1):
            self._check()
            try:
                return call()
            except api.ApiError as exc:
                if attempt == RETRIES or not exc.retryable:
                    raise
                self.progress.emit(t(
                    "{error} Trying again ({attempt}/{total})…",
                    error=exc, attempt=attempt + 1, total=RETRIES))
                self._wait(RETRY_WAIT * 2 ** (attempt - 1))
                self.progress.emit(stage)

    def _work(self, path, timestamps, do_cleanup):
        conf = self.conf
        workdir = None
        pieces = []
        segments = []
        try:
            if not shutil.which("ffmpeg"):
                raise api.ApiError(t("ffmpeg not found. Install it to transcribe files."))

            workdir = tempfile.mkdtemp(prefix="dikte-file-")
            self.progress.emit(t("Converting audio…"))
            wav_path = _to_wav(path, workdir, self._abort)
            self._check()

            target = conf.transcribe_target()
            self._local = ggml.whisper if target.provider == "local" else None
            chunks = self._chunks(wav_path, workdir, target, timestamps)
            if len(chunks) > 1:
                self.progress.emit(t("Splitting into {count} chunks…", count=len(chunks)))

            for index, (chunk_path, offset) in enumerate(chunks, start=1):
                self._check()
                stage = (t("Transcribing chunk {index}/{count}…",
                           index=index, count=len(chunks))
                         if len(chunks) > 1 else t("Transcribing…"))
                self.progress.emit(stage)
                if timestamps:
                    heard = self._attempt(lambda: api.transcribe_segments(
                        target,
                        chunk_path,
                        language=conf["language"],
                        prompt=conf["transcribe_prompt"],
                        timeout=HOSTED_TIMEOUT,
                        aborter=self._abort,
                    ), stage)
                    segments = stitch(segments, [
                        (start + offset, end + offset, line)
                        for start, end, line in heard
                    ])
                else:
                    pieces.append(self._attempt(lambda: api.transcribe(
                        target,
                        chunk_path,
                        language=conf["language"],
                        prompt=conf["transcribe_prompt"],
                        timeout=HOSTED_TIMEOUT,
                        aborter=self._abort,
                    ), stage))

            text = _joined(pieces, segments, timestamps)

            if do_cleanup and text:
                self._check()
                self.progress.emit(t("Cleaning up…"))
                text = self._cleanup(text, timestamps)

            self.finished.emit(text, segments)

        except Cancelled:
            self.progress.emit(t("Stopped."))
        except (api.ApiError, OSError, subprocess.SubprocessError, wave.Error) as exc:
            # An hour of a long file already heard is not worth throwing away
            # because the chunk after it failed, or because cleanup did. Hand
            # over what there is, and say in the same breath where it stops.
            partial = _joined(pieces, segments, timestamps)
            if partial:
                self.finished.emit(partial, segments)
                self.failed.emit(t("{error} The transcript up to there is below.",
                                   error=exc))
            else:
                self.failed.emit(str(exc))
        finally:
            self._local = None
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def _chunks(self, wav_path, workdir, target, timestamps):
        """[(the file to send, its offset in seconds)], one entry where it can be.

        A server on this machine is handed the WAV as it is: nothing is being
        uploaded, so the encoder would cost quality and buy nothing.
        """
        if target.provider == "local":
            return [(wav_path, 0.0)]

        whole = _to_mp3(wav_path, workdir, "audio.mp3", self._abort)
        seconds = chunk_seconds(whole, wav_seconds(wav_path))
        if not seconds:
            return [(whole, 0.0)]

        # Only a timestamped run can tell what it has already heard, so only it
        # can afford the overlap that keeps a cue off the cut.
        self._check()
        pieces = split_wav(wav_path, workdir, seconds,
                           OVERLAP_SECONDS if timestamps else 0)
        return [(_to_mp3(piece, workdir, f"chunk-{index:03d}.mp3", self._abort), offset)
                for index, (piece, offset) in enumerate(pieces)]

    def _cleanup(self, text, timestamps):
        conf = self.conf
        self._local = ggml.llm if cleanup.provider(conf) == "local" else None
        prompt = conf.cleanup_prompt(with_timestamps=timestamps, subtitles=True)
        out = []
        stage = t("Cleaning up…")
        for block in split_text(text, timestamps):
            self._check()
            out.append(self._attempt(
                lambda: cleanup.run(block, conf, prompt, aborter=self._abort), stage))
        return ("\n" if timestamps else "\n\n").join(out)


def _joined(pieces, segments, timestamps):
    """The transcript as one string, out of whichever of the two is holding it."""
    if timestamps:
        pieces = [f"[{format_timestamp(start)}] {line}" for start, _, line in segments]
    return "\n".join(pieces) if timestamps else " ".join(pieces)


def format_timestamp(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def srt_timestamp(seconds):
    millis = int(round(max(seconds, 0.0) * 1000))
    hours, rest = divmod(millis, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(text, segments):
    """Turn the timestamped transcript into SRT cues.

    The text is the authority on wording, so cleanup edits survive; the segments
    are the authority on timing. They meet at the [mm:ss] prefix, which cleanup
    is told to leave alone: a line's whole-second stamp finds the segment it came
    from, and with it the fractional start and the end time whisper reported. A
    line whose stamp finds nothing runs until the next line starts.
    """
    cues = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = STAMP_RE.match(line)
        body = line[match.end():].strip() if match else line
        if not match:
            if cues and body:      # a wrapped line belongs to the cue above it
                cues[-1][2] += " " + body
            continue
        if not body:
            continue
        hours, minutes, secs = (int(g or 0) for g in match.groups())
        cues.append([hours * 3600 + minutes * 60 + secs, None, body])

    timing = {}
    for start, end, _ in segments:
        timing.setdefault(int(start), (start, end))
    for cue in cues:
        cue[0], cue[1] = timing.get(cue[0], (float(cue[0]), 0.0))
    for index, cue in enumerate(cues):
        following = cues[index + 1][0] if index + 1 < len(cues) else 0.0
        if following > cue[0]:
            cue[1] = min(cue[1], following) if cue[1] > cue[0] else following
        elif cue[1] <= cue[0]:
            cue[1] = cue[0] + MIN_SUBTITLE_SECONDS

    blocks = [
        f"{number}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{body}"
        for number, (start, end, body) in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _reap(proc):
    """Leave nothing running behind a conversion that did not finish."""
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def _to_wav(path, workdir, aborter=None):
    out = os.path.join(workdir, "audio.wav")
    return _ffmpeg(["-i", path, "-vn", "-ac", "1", "-ar", str(RATE),
                    "-c:a", "pcm_s16le", out], out, aborter)


def _to_mp3(wav_path, workdir, name, aborter=None):
    """The same audio at a fifth of the size.

    Which is the whole of it: uncompressed, an hour of speech is four uploads
    and so three cuts, and every cut is a chance of the model losing the thread
    of where its cues should end. Encoded it is one upload and no cuts. The
    bitrate is far above what a 16 kHz mono voice has left to lose.
    """
    out = os.path.join(workdir, name)
    try:
        return _ffmpeg(["-i", wav_path, "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, out],
                       out, aborter)
    except api.ApiError:
        # An ffmpeg built without the encoder, which is rare and not worth
        # failing over: the WAV transcribes just as well, it only has to be cut
        # up more often to fit in a request.
        return wav_path


def _ffmpeg(args, out, aborter=None):
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-y", *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # ffmpeg writes UTF-8 whatever the locale says; read as the Windows
        # codepage its messages mojibake, and a byte the codepage cannot place
        # raises from inside communicate itself.
        text=True, encoding="utf-8", errors="replace",
        creationflags=paths.NO_WINDOW,
    )
    # A two hour film is a minute of ffmpeg, which is a minute of a Stop button
    # doing nothing unless the abort reaches the process itself.
    with contextlib.ExitStack() as stack:
        stack.callback(_reap, proc)
        if aborter is not None:
            stack.enter_context(aborter.holding(proc.kill))
        _stdout, stderr = proc.communicate()
    if aborter is not None:
        aborter.check()
    if proc.returncode != 0 or not os.path.exists(out):
        tail = (stderr or "").strip().splitlines()
        raise api.ApiError(t("Could not read the file: {error}",
                             error=tail[-1] if tail else proc.returncode))
    return out


def wav_seconds(wav_path):
    with contextlib.closing(wave.open(wav_path, "rb")) as src:
        return src.getnframes() / (src.getframerate() or RATE)


def chunk_seconds(path, duration):
    """How many seconds of this audio fit in one request, or 0 when all of it does.

    Whichever of the two limits bites first. How much fits under the upload
    limit is measured rather than worked out: what an encoder makes of an hour
    of speech depends on the speech, and the file on disk is the only honest
    answer. The other limit is MAX_CHUNK_SECONDS, and it is the one that catches
    a long file at this bitrate: an hour and a half of mp3 is two chunks by size
    and one of them is an hour of audio in a single request, which no hosted
    gateway stays on the line for.
    """
    if duration <= 0:
        return 0.0
    size = os.path.getsize(path)
    fits = duration * UPLOAD_LIMIT / size * 0.95 if size > UPLOAD_LIMIT else duration
    seconds = max(60.0, min(fits, MAX_CHUNK_SECONDS))
    return 0.0 if seconds >= duration else seconds


def split_wav(wav_path, workdir, seconds=WAV_CHUNK_SECONDS, overlap=OVERLAP_SECONDS):
    """[(chunk path, offset in seconds)], a single entry for short files.

    Every chunk but the first starts `overlap` seconds inside the one before it,
    so the sentence the cut fell in the middle of is heard whole by one of them.
    stitch() is what drops the telling that was cut short.
    """
    with contextlib.closing(wave.open(wav_path, "rb")) as src:
        rate = src.getframerate()
        total = src.getnframes()
        per_chunk = int(seconds * rate)
        if per_chunk <= 0 or total <= per_chunk:
            return [(wav_path, 0.0)]

        # Half a chunk is the most an overlap can be and still be an overlap.
        step = per_chunk - int(max(0.0, min(overlap, seconds / 2)) * rate)
        chunks = []
        position = 0
        while position < total:
            # What is left is shorter than the overlap, so the chunk before this
            # one already holds all of it.
            if chunks and total - position <= per_chunk - step:
                break
            src.setpos(position)
            frames = src.readframes(per_chunk)
            if not frames:
                break
            path = os.path.join(workdir, f"chunk-{len(chunks):03d}.wav")
            with contextlib.closing(wave.open(path, "wb")) as dst:
                dst.setnchannels(src.getnchannels())
                dst.setsampwidth(src.getsampwidth())
                dst.setframerate(rate)
                dst.writeframes(frames)
            chunks.append((path, position / rate))
            position += step
        return chunks


def stitch(collected, incoming):
    """Add a chunk's segments to the ones before it, minus what was heard twice.

    The chunks overlap, so the sentence the cut landed in is in both of them:
    cut short as the last cue of the chunk before, and whole somewhere in this
    one. This chunk's telling of it is the one that stands, and the chunk before
    gives way from wherever that telling begins, so that nothing is said twice
    and the cues still run forwards.
    """
    if not collected:
        return list(incoming)
    kept = [segment for segment in incoming if segment[1] > collected[-1][0]]
    if not kept:
        return collected
    seam = kept[0][0]
    head = [segment for segment in collected if segment[1] <= seam]
    return (head or collected[:-1]) + kept


def split_text(text, timestamps):
    """Break long text into cleanup-sized blocks, never mid-line."""
    if len(text) <= CLEANUP_CHUNK_CHARS:
        return [text]
    separator = "\n" if timestamps else " "
    blocks, current = [], ""
    for part in text.split(separator):
        candidate = f"{current}{separator}{part}" if current else part
        if len(candidate) > CLEANUP_CHUNK_CHARS and current:
            blocks.append(current)
            current = part
        else:
            current = candidate
    if current:
        blocks.append(current)
    return blocks
