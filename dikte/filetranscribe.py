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
RETRIES = 7                  # tries in all when the gateway refused quickly; the waits add up to about five minutes
SLOW_RETRIES = 3             # tries in all for any other failure, timeouts among them, which may have been billed
QUICK_REFUSALS = frozenset({429, 502, 503})  # answered fast, and the model never ran
RETRY_WAIT = 5               # seconds before the second try, doubled after that
RATE = 16000
MIN_SUBTITLE_SECONDS = 1.5   # how long a cue with no end time of its own stays up
# A diarizing model takes several times as long over the same audio, and the
# gateway's clock does not stretch for it, so its chunks are shorter. The longer
# overlap is what the speaker labels of two chunks are matched up on.
DIARIZE_CHUNK_SECONDS = 300
DIARIZE_OVERLAP_SECONDS = 60
TURN_CHARS = CLEANUP_CHUNK_CHARS // 4  # past this a turn goes on in a new line

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

    def start(self, path, timestamps, do_cleanup, speakers=False):
        if self.busy:
            return
        self._abort = api.Aborter()      # the last one is spent
        self._thread = threading.Thread(
            target=self._work, args=(path, timestamps, do_cleanup, speakers),
            daemon=True,
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

    def wait(self, timeout=None):
        """Let the run finish its cleanup. The thread is a daemon, so a process
        that exits on the finished signal would otherwise kill it before it
        removes its scratch directory."""
        if self._thread is not None:
            self._thread.join(timeout)

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
        attempt = 0
        while True:
            attempt += 1
            self._check()
            try:
                return call()
            except api.ApiError as exc:
                # A gateway turning the request away is cheap to ask again and
                # often over in a minute or two. A timeout cost ten minutes and
                # may have been billed, so it gets fewer goes.
                total = RETRIES if exc.status in QUICK_REFUSALS else SLOW_RETRIES
                if attempt >= total or not exc.retryable:
                    raise
                self.progress.emit(t(
                    "{error} Trying again ({attempt}/{total})…",
                    error=exc, attempt=attempt + 1, total=total))
                self._wait(RETRY_WAIT * 2 ** (attempt - 1))
                self.progress.emit(stage)

    def _work(self, path, timestamps, do_cleanup, speakers=False):
        conf = self.conf
        workdir = None
        pieces = []
        segments = []
        try:
            if not shutil.which("ffmpeg"):
                raise api.ApiError(t("ffmpeg not found. Install it to transcribe files."))

            workdir = tempfile.mkdtemp(prefix="dikte-file-", dir=paths.scratch_dir())
            self.progress.emit(t("Converting audio…"))
            wav_path = _to_wav(path, workdir, self._abort)
            self._check()

            target = conf.transcribe_target()
            self._local = ggml.whisper if target.provider == "local" else None
            # Asked of a model nobody has seen label speakers, the switch is
            # not on offer, and a stale setting is not a reason to fail the run.
            speakers = speakers and bool(api.diarize_model(target))
            chunks = self._chunks(wav_path, workdir, target, timestamps, speakers)
            heard_before = []
            voices = 0
            # Read once for every chunk: the same file, the same dictionary.
            hint = conf.dictionary_hint()
            if len(chunks) > 1:
                self.progress.emit(t("Splitting into {count} chunks…", count=len(chunks)))

            for index, (chunk_path, offset) in enumerate(chunks, start=1):
                self._check()
                stage = (t("Transcribing chunk {index}/{count}…",
                           index=index, count=len(chunks))
                         if len(chunks) > 1 else t("Transcribing…"))
                self.progress.emit(stage)
                if speakers:
                    heard = self._attempt(lambda: api.transcribe_speakers(
                        target,
                        chunk_path,
                        language=conf["language"],
                        prompt=hint,
                        timeout=HOSTED_TIMEOUT,
                        aborter=self._abort,
                    ), stage)
                    heard = link_speakers(heard_before, [
                        (start + offset, end + offset, line, who)
                        for start, end, line, who in heard
                    ], voices)
                    heard_before = heard
                    voices = max([voices] + [who + 1 for *_, who in heard])
                    segments = stitch(segments, heard)
                elif timestamps:
                    heard = self._attempt(lambda: api.transcribe_segments(
                        target,
                        chunk_path,
                        language=conf["language"],
                        prompt=hint,
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
                        prompt=hint,
                        timeout=HOSTED_TIMEOUT,
                        aborter=self._abort,
                    ), stage))

            text, cues = _result(pieces, segments, timestamps, speakers)

            if do_cleanup and text:
                self._check()
                self.progress.emit(t("Cleaning up…"))
                text = self._cleanup(text, timestamps, speakers)

            self.finished.emit(text, cues)

        except Cancelled:
            self.progress.emit(t("Stopped."))
        except (api.ApiError, OSError, subprocess.SubprocessError, wave.Error) as exc:
            # An hour of a long file already heard is not worth throwing away
            # because the chunk after it failed, or because cleanup did. Hand
            # over what there is, and say in the same breath where it stops.
            partial, cues = _result(pieces, segments, timestamps, speakers)
            if partial:
                self.finished.emit(partial, cues)
                self.failed.emit(t("{error} The transcript up to there is below.",
                                   error=exc))
            else:
                self.failed.emit(str(exc))
        finally:
            self._local = None
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def _chunks(self, wav_path, workdir, target, timestamps, speakers=False):
        """[(the file to send, its offset in seconds)], one entry where it can be.

        A server on this machine is handed the WAV as it is: nothing is being
        uploaded, so the encoder would cost quality and buy nothing.
        """
        if target.provider == "local":
            return [(wav_path, 0.0)]

        whole = _to_mp3(wav_path, workdir, "audio.mp3", self._abort)
        duration = wav_seconds(wav_path)
        seconds = chunk_seconds(whole, duration)
        if speakers:
            seconds = min(seconds or duration, DIARIZE_CHUNK_SECONDS)
            if seconds >= duration:
                seconds = 0.0
        if not seconds:
            return [(whole, 0.0)]

        # Only a timestamped run can tell what it has already heard, so only it
        # can afford the overlap that keeps a cue off the cut.
        self._check()
        overlap = (DIARIZE_OVERLAP_SECONDS if speakers
                   else OVERLAP_SECONDS if timestamps else 0)
        pieces = split_wav(wav_path, workdir, seconds, overlap)
        return [(_to_mp3(piece, workdir, f"chunk-{index:03d}.mp3", self._abort), offset)
                for index, (piece, offset) in enumerate(pieces)]

    def _cleanup(self, text, timestamps, speakers=False):
        conf = self.conf
        self._local = ggml.llm if cleanup.provider(conf) == "local" else None
        prompt = conf.cleanup_prompt(with_timestamps=timestamps, with_speakers=speakers,
                                     subtitles=True, participants=False)
        # A labelled transcript is one turn per line, and a block cut inside a
        # line would hand the second half back without the name in front.
        by_line = timestamps or speakers
        out = []
        stage = t("Cleaning up…")
        for block in split_text(text, by_line):
            self._check()
            out.append(self._attempt(
                lambda: cleanup.run(block, conf, prompt, aborter=self._abort), stage))
        return ("\n" if by_line else "\n\n").join(out)


def link_speakers(before, incoming, voices=0):
    """`incoming` with its speaker labels swapped for the ones `before` uses.

    A diarizing model numbers the voices afresh in every request, so one chunk's
    "0" can be the next one's "1". The chunks overlap, and in the overlap both
    of them heard the same people say the same things at the same moments: the
    labels that keep talking over each other there are the same person. Each is
    paired off with the one it shares the most time with. A voice that is silent
    in the overlap is matched by elimination when exactly one known voice is left
    over, and is taken for somebody new otherwise.

    `voices` is how many have been numbered so far in the whole run, not only in
    the chunk before: someone who sat out that chunk is still a known voice, and
    a newcomer must not be handed their number. The labels that come out are
    numbers from 0, in the order the voices were first heard.
    """
    known = list(range(max([voices] + [who + 1 for *_, who in before])))
    shared = {}
    for start, end, _, who in incoming:
        for other_start, other_end, _, other in before:
            overlap = min(end, other_end) - max(start, other_start)
            if overlap > 0:
                shared[(who, other)] = shared.get((who, other), 0.0) + overlap
    mapping, taken = {}, set()
    for (who, other), _ in sorted(shared.items(), key=lambda item: -item[1]):
        if who not in mapping and other not in taken:
            mapping[who] = other
            taken.add(other)
    fresh = len(known)
    for *_, who in incoming:
        if who in mapping:
            continue
        left = [other for other in known if other not in taken]
        if len(left) == 1:
            mapping[who] = left[0]
        else:
            mapping[who] = fresh
            fresh += 1
        taken.add(mapping[who])
    return [(start, end, text, mapping[who]) for start, end, text, who in incoming]


def speaker_name(number):
    return t("Speaker {number}", number=number + 1)


def _result(pieces, segments, timestamps, speakers):
    """(text, cues) for the window: the transcript, and what an SRT is timed by.

    With speakers, every cue carries the name in front of its text, so the SRT
    and the timestamped lines both say who is talking. Without timestamps the
    cues are joined into turns instead, one line for each time the voice changes.
    """
    if not speakers:
        return _joined(pieces, segments, timestamps), (segments if timestamps else [])
    named = [(start, end, f"{speaker_name(who)}: {line}")
             for start, end, line, who in segments]
    if timestamps:
        return _joined([], named, True), named
    turns = []
    for _, _, line, who in segments:
        # An hour of one voice would otherwise be one line, which cleanup takes
        # whole whatever its size, since a block is never cut inside a line.
        if (turns and turns[-1][0] == who
                and sum(len(x) + 1 for x in turns[-1][1]) < TURN_CHARS):
            turns[-1][1].append(line)
        else:
            turns.append((who, [line]))
    return "\n".join(f"{speaker_name(who)}: {' '.join(lines)}"
                     for who, lines in turns), []


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

    # Several cues can share a whole second, so a second holds every segment
    # that began in it and they are handed out in the order they were spoken.
    timing = {}
    for start, end, _ in segments:
        timing.setdefault(int(start), []).append((start, end))
    for cue in cues:
        found = timing.get(cue[0])
        if found:
            # The last one stays, so a second with more lines than it has
            # timings hands the last of them out again rather than falling back
            # to the bare second, which would run backwards from the line above.
            cue[0], cue[1] = found.pop(0) if len(found) > 1 else found[0]
        else:
            cue[0], cue[1] = float(cue[0]), 0.0
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
