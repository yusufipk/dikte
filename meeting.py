"""From a two-channel meeting recording to a set of minutes.

The recording arrives with your microphone on the left channel and everything
the other participants said on the right, so attribution is settled before any
model sees the audio: each channel is transcribed on its own, and the two are
then interleaved on one timeline. What a model is asked for is only what models
are good at, turning the words into readable prose and then into minutes.

Every stage the run reaches is written to disk, so a failure in the last one
does not cost the transcription of an hour of audio.
"""

import array
import contextlib
import difflib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave

from PyQt6.QtCore import QObject, pyqtSignal

import api
import config as cfg
import filetranscribe
import vad
from filetranscribe import Cancelled, format_timestamp
from i18n import t

# Where the document stops being prose and starts being the transcript. It is a
# comment, so it never shows up in a rendered document, and it is what a retry
# reads the transcript back out of.
TRANSCRIPT_MARKER = "<!-- dikte:transcript -->"

# A microphone that hears the other side through the speakers puts the same
# sentence on both channels. Ours is the copy to drop, and it is a copy when it
# lands on top of theirs in time and says nearly the same thing.
ECHO_OVERLAP = 0.5
ECHO_SIMILARITY = 0.72

# A pause this long inside one person's turn starts a new line instead.
TURN_GAP = 8.0

# How much of a channel is read at a time when levels are measured, matched to
# the block the dictation level meter uses so the silence thresholds mean the
# same thing here.
LEVEL_FRAMES = 1024


class MeetingPipeline(QObject):
    """Transcribe, clean up and summarise a recorded meeting."""

    progress = pyqtSignal(str, str)     # base, message
    finished = pyqtSignal(str, str)     # base, title
    failed = pyqtSignal(str, str)       # base, error

    def __init__(self, conf, parent=None):
        super().__init__(parent)
        self.conf = conf
        self._thread = None
        self._stop = threading.Event()
        self._base = ""

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def running_base(self):
        return self._base if self.busy else ""

    def run(self, entry):
        """Take a meeting row onwards from wherever it stopped."""
        if self.busy:
            return False
        self._stop.clear()
        self._base = entry.get("base", "")
        self._thread = threading.Thread(target=self._work, args=(dict(entry),),
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def _check(self):
        if self._stop.is_set():
            raise Cancelled

    def _say(self, message):
        self.progress.emit(self._base, message)

    # ---- the chain -------------------------------------------------------

    def _work(self, entry):
        base = entry["base"]
        doc_path, wav_path = cfg.meeting_paths(base)
        workdir = None
        try:
            transcript = self._stored_transcript(entry, doc_path)
            if not transcript:
                if not wav_path.exists():
                    raise api.ApiError(t("The recording is gone: {path}", path=wav_path))
                workdir = tempfile.mkdtemp(prefix="dikte-meeting-")
                transcript = self._transcribe(str(wav_path), workdir)
                if self.conf["meeting_cleanup"]:
                    self._check()
                    self._say(t("Cleaning up…"))
                    transcript = self._cleanup(transcript)
                # On disk before the summary is attempted: if the summary fails,
                # a retry starts from here instead of from the audio.
                self._write(doc_path, "", transcript, entry)
                cfg.update_meeting(base, status="transcribed", error="")

            self._check()
            self._say(t("Writing the minutes…"))
            writer = self.conf.minutes_target()
            minutes = api.cleanup(writer, transcript, self.conf.meeting_prompt(),
                                  timeout=600)
            title = self._write(doc_path, minutes, transcript, entry)
            cfg.update_meeting(base, status="done", error="", title=title,
                               model=writer.model)
            self._discard_audio(wav_path)
            self.finished.emit(base, title)

        except Cancelled:
            cfg.update_meeting(base, error=t("Stopped."))
            self._say(t("Stopped."))
        except (api.ApiError, OSError, subprocess.SubprocessError, wave.Error) as exc:
            # The audio stays put no matter what the keep setting says: it is the
            # only copy of the meeting, and the run can be tried again from it.
            cfg.update_meeting(base, status="failed", error=str(exc))
            self.failed.emit(base, str(exc))
        finally:
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def _stored_transcript(self, entry, doc_path):
        """The transcript an earlier run already paid for, or ''."""
        if entry.get("status") not in ("transcribed", "done"):
            return ""
        try:
            return read_transcript(doc_path.read_text(encoding="utf-8"))
        except OSError:
            return ""

    def _transcribe(self, wav_path, workdir):
        conf = self.conf
        mine, theirs = split_channels(wav_path, workdir)
        target = conf.transcribe_target()
        language = conf["meeting_language"] or conf["language"]
        hint = conf.meeting_hint()

        segments = []
        for path, speaker in ((mine, "mine"), (theirs, "theirs")):
            side = t("you") if speaker == "mine" else t("the others")
            # A directory each: the chunk files are named by their index, and
            # the second channel would otherwise write over the first one's.
            chunk_dir = os.path.join(workdir, speaker)
            os.makedirs(chunk_dir, exist_ok=True)
            chunks = filetranscribe.split_wav(path, chunk_dir)
            for index, (chunk_path, offset) in enumerate(chunks, start=1):
                self._check()
                self._say(t("Transcribing {side}: {index}/{count}…",
                            side=side, index=index, count=len(chunks)))
                # Nobody spoke on this side for these ten minutes: an API call
                # would cost money to be told so, and can invent a sentence.
                if self._silent(chunk_path):
                    continue
                segments.extend(
                    (start + offset, end + offset, text, speaker)
                    for start, end, text in api.transcribe_segments(
                        target, chunk_path, language=language, prompt=hint
                    )
                )
        if not segments:
            raise api.ApiError(t("Neither side of the recording had any speech in it."))

        names = conf.speaker_names()
        return render_turns(merge_turns(segments), *names)

    def _silent(self, path):
        if not self.conf["skip_silent"]:
            return False
        conf = self.conf
        stats = vad.analyse(rms_series(path), LEVEL_FRAMES / wav_rate(path),
                            conf["speech_margin_db"])
        return vad.is_silent(stats, conf["silence_db"], conf["speech_margin_db"],
                             conf["min_voiced_seconds"])

    def _cleanup(self, transcript):
        conf = self.conf
        prompt = conf.cleanup_prompt(with_timestamps=True, with_speakers=True)
        target = conf.cleanup_target()
        out = []
        blocks = filetranscribe.split_text(transcript, True)
        for index, block in enumerate(blocks, start=1):
            self._check()
            if len(blocks) > 1:
                self._say(t("Cleaning up {index}/{count}…",
                            index=index, count=len(blocks)))
            out.append(api.cleanup(target, block, prompt))
        return "\n".join(out)

    def _write(self, doc_path, minutes, transcript, entry):
        """Write the document, and hand back the title it ended up with."""
        title, body = split_title(minutes)
        title = title or entry.get("title") or t("Meeting")
        text = build_document(
            title, entry.get("ts", ""), entry.get("duration", 0.0), body, transcript
        )
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = doc_path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(doc_path)
        return title

    def _discard_audio(self, wav_path):
        if self.conf["meeting_keep_audio"]:
            return
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass


# --- audio ----------------------------------------------------------------

def split_channels(path, workdir):
    """Pull the stereo recording apart into (mine, theirs) mono files."""
    with contextlib.closing(wave.open(path, "rb")) as src:
        if src.getnchannels() != 2 or src.getsampwidth() != 2:
            raise api.ApiError(t("This recording is not a two-channel meeting."))
        rate = src.getframerate()
        mine = os.path.join(workdir, "mine.wav")
        theirs = os.path.join(workdir, "theirs.wav")
        with contextlib.closing(wave.open(mine, "wb")) as left, \
                contextlib.closing(wave.open(theirs, "wb")) as right:
            for out in (left, right):
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
            while True:
                frames = src.readframes(rate)  # a second at a time
                if not frames:
                    break
                samples = array.array("h")
                samples.frombytes(frames)
                left.writeframes(samples[0::2].tobytes())
                right.writeframes(samples[1::2].tobytes())
    return mine, theirs


def rms_series(path):
    """Per-block RMS in 0..1, the input vad.analyse expects."""
    out = []
    with contextlib.closing(wave.open(path, "rb")) as wav:
        while True:
            frames = wav.readframes(LEVEL_FRAMES)
            if not frames:
                break
            samples = array.array("h")
            samples.frombytes(frames[:len(frames) - (len(frames) % 2)])
            if not samples:
                continue
            total = sum(s * s for s in samples) / len(samples)
            out.append(min(1.0, (total ** 0.5) / 32768.0))
    return out


def wav_rate(path):
    with contextlib.closing(wave.open(path, "rb")) as wav:
        return wav.getframerate()


# --- the timeline ----------------------------------------------------------

def merge_turns(segments, gap=TURN_GAP):
    """[(start, speaker, text)] on one timeline, echo dropped, turns joined."""
    ordered = sorted(segments, key=lambda seg: (seg[0], seg[1]))
    theirs = [seg for seg in ordered if seg[3] == "theirs"]
    kept = [seg for seg in ordered if seg[3] == "mine" and not _is_echo(seg, theirs)]
    kept.extend(theirs)
    kept.sort(key=lambda seg: seg[0])

    turns = []
    for start, end, text, speaker in kept:
        if turns and turns[-1][1] == speaker and start - turns[-1][3] <= gap:
            turns[-1][2] += " " + text
            turns[-1][3] = max(turns[-1][3], end)
            continue
        turns.append([start, speaker, text, end])
    return [(start, speaker, text) for start, speaker, text, _ in turns]


def _is_echo(segment, theirs):
    """Did the microphone just pick up the other side through the speakers?"""
    start, end, text, _ = segment
    span = max(end - start, 0.01)
    mine = _normalise(text)
    if not mine:
        return True
    for their_start, their_end, their_text, _ in theirs:
        if their_start > end:
            break
        overlap = min(end, their_end) - max(start, their_start)
        if overlap / span < ECHO_OVERLAP:
            continue
        ratio = difflib.SequenceMatcher(None, mine, _normalise(their_text)).ratio()
        if ratio >= ECHO_SIMILARITY:
            return True
    return False


def _normalise(text):
    return re.sub(r"[^\w\s]", "", text.strip().lower())


def render_turns(turns, mine_label, theirs_label):
    labels = {"mine": mine_label, "theirs": theirs_label}
    return "\n".join(
        f"[{format_timestamp(start)}] {labels[speaker]}: {text.strip()}"
        for start, speaker, text in turns
    )


# --- the document ----------------------------------------------------------

def split_title(minutes):
    """('Title', 'rest of it') from a document whose first line is a heading."""
    text = (minutes or "").strip()
    if not text:
        return "", ""
    head, _, rest = text.partition("\n")
    if head.startswith("#"):
        return head.lstrip("#").strip(), rest.strip()
    return "", text


def build_document(title, when, duration, minutes, transcript):
    minutes = (minutes or "").strip()
    parts = [f"# {title}", "", f"*{when} · {length_label(duration)}*", ""]
    if minutes:
        parts += [minutes, "", "---", ""]
    parts += [TRANSCRIPT_MARKER, f"## {t('Transcript')}", "", transcript.strip(), ""]
    return "\n".join(parts)


def read_transcript(document):
    """The transcript back out of a document written by build_document."""
    _, marker, rest = document.partition(TRANSCRIPT_MARKER)
    if not marker:
        return ""
    lines = rest.strip().splitlines()
    if lines and lines[0].startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def length_label(seconds):
    minutes = int(seconds) // 60
    if minutes < 60:
        return t("{minutes} min", minutes=minutes)
    return t("{hours} h {minutes} min", hours=minutes // 60, minutes=minutes % 60)


def new_base():
    """The stem the recording, the document and the index row all share."""
    return time.strftime("%Y%m%d-%H%M%S")


def new_entry(base, duration):
    """The index row for a meeting that has just been recorded."""
    return {
        "base": base,
        "ts": f"{base[:4]}-{base[4:6]}-{base[6:8]} {base[9:11]}:{base[11:13]}",
        "title": "",
        "duration": round(duration, 1),
        "status": "recorded",
        "error": "",
        "model": "",
    }
