"""The dictation chain: transcribe → clean up → clipboard → paste.

The same chain also carries the other thing a dictation can be. Asked to, it
hands the transcript to Claude Code instead of pasting it, and pastes back
whatever came of it: an answer to a question, or a sentence saying what was
done.
"""

import collections
import os
import shutil
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QObject, pyqtSignal

from . import api
from . import assistant
from . import audio
from . import cleanup
from . import config as cfg
from . import i18n
from . import paste
from . import vad
from .i18n import t

CHUNK_SECONDS = audio.CHUNK_FRAMES / audio.RATE

# A dictation and a command to the agent run side by side and can finish at the
# same moment. Pasting is not one step but three that must not interleave: read
# what is on the clipboard, put ours there, press the key. Two runs doing that
# at once would paste one answer and restore the other's clipboard over it.
_paste_lock = threading.Lock()


class Pipeline(QObject):
    stage = pyqtSignal(str)          # human-readable progress line
    finished = pyqtSignal(str, str, str, str)  # raw, final text, warning, language
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, conf, parent=None):
        super().__init__(parent)
        self.conf = conf
        self._thread = None
        self._stop = threading.Event()
        # Recordings waiting their turn, and whether a thread is working them
        # off. The flag rather than the thread's own liveness, because a thread
        # stays alive for a moment after deciding it is done, and a job arriving
        # in that moment would be left in the queue with nobody coming back.
        self._jobs = collections.deque()
        self._draining = False
        self._jobs_lock = threading.Lock()

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def run(self, wav_path, duration, rms_values=(), ask=False, paste=None,
            focus=None):
        """`paste` overrides the setting for this one run, which is what a
        dictation asked for from a terminal wants: the text comes back down the
        socket, and pasting it into whatever had focus is nobody's intention.

        `focus` is the application that was in front when the recording began,
        as a process id, and is where the paste is meant to land.

        A run started while one is going waits its turn rather than being
        dropped: the next dictation can be spoken while the last one is still
        being cleaned up, and each one is finished, pasted and reported in the
        order it was spoken."""
        with self._jobs_lock:
            self._jobs.append((wav_path, duration, list(rms_values), ask, paste,
                               focus))
            if self._draining:
                return
            self._draining = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        while True:
            with self._jobs_lock:
                if not self._jobs:
                    self._draining = False
                    return
                job = self._jobs.popleft()
            self._work(*job)

    def cancel(self):
        """Give up on a job already under way.

        Only the Claude call can honour this, and it is the only one long enough
        to be worth interrupting: a transcription is over in seconds, a command
        that went looking through the web is not.
        """
        self._stop.set()

    def _work(self, wav_path, duration, rms_values, ask, paste_override=None,
              focus=None):
        conf = self.conf
        started = time.monotonic()
        raw = ""

        # Room tone only: don't spend an API call, and don't invite a
        # hallucinated sentence back.
        if conf["skip_silent"]:
            stats = vad.analyse(rms_values, CHUNK_SECONDS, conf["speech_margin_db"])
            if vad.is_silent(stats, conf["silence_db"], conf["speech_margin_db"],
                             conf["min_voiced_seconds"]):
                self._discard(wav_path)
                self.failed.emit(
                    t("No speech detected ({level} dB)", level=round(stats["speech_db"]))
                )
                return

        try:
            self.stage.emit(t("Transcribing…"))
            target = conf.transcribe_target()
            # The spoken language is only knowable after the fact, and only the
            # local server says what it heard: auto mode asks it there, and
            # every other run (a fixed language, or a hosted provider that
            # detects but stays silent) transcribes as before.
            auto = conf["language"] == "auto"
            if auto:
                raw, detected = api.transcribe_detected(
                    target, wav_path, language=conf["language"],
                    prompt=conf["transcribe_prompt"],
                )
            else:
                raw = api.transcribe(
                    target,
                    wav_path,
                    language=conf["language"],
                    prompt=conf["transcribe_prompt"],
                )
                detected = ""

            if conf["filter_hallucinations"] and vad.looks_like_hallucination(raw, duration):
                self._discard(wav_path)
                self.failed.emit(t("Discarded a stock phrase: “{text}”", text=raw[:60]))
                return

            text = raw
            warning = ""
            # The language the run actually spoke, reported to the window, the
            # clipboard path and the history alike: the detected code, or the
            # configured one when nothing was detected to replace it.
            speech_language = detected or conf["language"]
            # Remembered rather than re-derived at the history write below: the
            # ask path runs cleanup under a different setting, and the record
            # should say what happened, not what one of the two gates implies.
            cleaned = False
            # Claude reads through “eee” and “hani” without help, so a dictation
            # on its way there is normally sent as it was heard, one API call and
            # a second or two lighter.
            if (conf["assistant_cleanup"] if ask else conf["cleanup_enabled"]):
                self.stage.emit(t("Cleaning up…"))
                cleaned = True
                try:
                    text = cleanup.run(raw, conf, conf.cleanup_prompt(speech=detected))
                except api.ApiError as exc:
                    # Keep the transcript, but never let the failure pass unseen:
                    # a rejected key would otherwise look like working dictation.
                    text = raw
                    warning = str(exc)
                    print(f"dikte: cleanup failed: {exc}", file=sys.stderr)

            question = ""
            if ask:
                question = text
                self.stage.emit(t("Asking {name}…", name=i18n.name(
                    assistant.display_name(conf), "dative")))
                text, denied = assistant.ask(
                    question, conf,
                    on_stage=self.stage.emit,
                    should_stop=self._stop.is_set,
                )
                warning = "\n".join(x for x in (warning, denied) if x)

            wants_paste = (conf["assistant_paste"] if ask else conf["auto_paste"])
            if paste_override is not None:
                wants_paste = paste_override

            # Into the history before the paste is attempted: the record says
            # what was dictated, not whether a key press landed, and a paste
            # that fails must not take the transcript down with it.
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration": round(duration, 1),
                "elapsed": round(time.monotonic() - started, 1),
                "model": target.model,
                "cleanup_model": cleanup.model(conf) if cleaned else "",
                "cleanup_error": warning,
                "mode": "ask" if ask else "",
                "question": question,
                "assistant": assistant.provider(conf) if ask else "",
                "assistant_model": assistant.model(conf) if ask else "",
                "speech_language": speech_language,
                "raw": raw,
                "text": text,
            }
            cfg.append_history(record)
            try:
                cfg.trim_history(conf["history_limit"])
            except OSError as exc:
                print(f"dikte: could not trim the history: {exc}", file=sys.stderr)

            with _paste_lock:
                previous = (paste.read_clipboard()
                            if conf["restore_clipboard"] and wants_paste else None)
                paste.copy(text)
                if wants_paste:
                    self.stage.emit(t("Pasting…"))
                    try:
                        paste.press(conf["paste_shortcut"], focus=focus)
                    except paste.PasteError as exc:
                        # The transcript is on the clipboard and in the history;
                        # a key press that would not land is a warning, not a
                        # failure, and the old clipboard is NOT put back over
                        # the text the user now has to paste by hand.
                        previous = None
                        warning = "\n".join(x for x in (
                            warning,
                            t("Copied, but pasting failed: {error}", error=exc),
                        ) if x)
                        # The row above was written before the paste, so it
                        # has to be told what the paste then did.
                        record = cfg.amend_history(
                            record, cleanup_error=warning) or record
                if previous is not None:
                    # Let the focused application consume the temporary
                    # transcription before putting every old clipboard type
                    # back.
                    time.sleep(0.35)
                    paste.copy_bytes(previous)

            self.finished.emit(raw, text, warning, speech_language)

        except assistant.Cancelled:
            self.cancelled.emit()
        except (api.ApiError, paste.PasteError, assistant.AssistantError) as exc:
            print(f"dikte: {exc}", file=sys.stderr)
            self.failed.emit(self._keeping(wav_path, str(exc)))
        except Exception as exc:  # never fail silently
            traceback.print_exc()
            self.failed.emit(self._keeping(wav_path, t("Unexpected error: {error}",
                                                       error=exc)))
        finally:
            self._discard(wav_path)

    def _keeping(self, wav_path, message):
        """Put the failed run's audio somewhere a retry can find it.

        A dictation that died on the way to the model is speech the user cannot
        say again from memory; deleting it because a server was down turns one
        failure into two. Kept regardless of the keep_audio setting, which is
        about the runs that succeeded.
        """
        kept = self._keep(wav_path)
        if not kept:
            return message
        return message + "\n" + t("The recording was kept: {path}", path=kept)

    def _keep(self, wav_path):
        """Move the WAV into the recordings directory; its new path, or ''."""
        try:
            cfg.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            base = time.strftime("%Y%m%d-%H%M%S")
            # Two runs can finish inside the same second; the first one kept
            # must not be overwritten by the second.
            for suffix in ("",) + tuple(f"-{n}" for n in range(1, 100)):
                target = cfg.RECORDINGS_DIR / f"{base}{suffix}.wav"
                if not target.exists():
                    shutil.move(wav_path, target)
                    return str(target)
            return ""
        except OSError as exc:
            print(f"dikte: could not keep the audio: {exc}", file=sys.stderr)
            return ""

    def _discard(self, wav_path):
        if not os.path.exists(wav_path):
            return
        if self.conf["keep_audio"]:
            if self._keep(wav_path):
                return
            # The move failing is no reason to delete what the user asked to
            # keep: the temporary file stays where it is, named in the log.
            print(f"dikte: the audio stays at {wav_path}", file=sys.stderr)
            return
        try:
            os.unlink(wav_path)
        except OSError:
            pass
