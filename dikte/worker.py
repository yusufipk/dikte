"""The dictation chain: transcribe → clean up → clipboard → paste.

The same chain also carries the other thing a dictation can be. Asked to, it
hands the transcript to Claude Code instead of pasting it, and pastes back
whatever came of it: an answer to a question, or a sentence saying what was
done.
"""

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
    finished = pyqtSignal(str, str, str)  # raw transcript, final text, warning
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, conf, parent=None):
        super().__init__(parent)
        self.conf = conf
        self._thread = None
        self._stop = threading.Event()

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def run(self, wav_path, duration, rms_values=(), ask=False, paste=None,
            focus=None):
        """`paste` overrides the setting for this one run, which is what a
        dictation asked for from a terminal wants: the text comes back down the
        socket, and pasting it into whatever had focus is nobody's intention.

        `focus` is the application that was in front when the recording began,
        as a process id, and is where the paste is meant to land."""
        if self.busy:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._work,
            args=(wav_path, duration, list(rms_values), ask, paste, focus),
            daemon=True,
        )
        self._thread.start()

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
            raw = api.transcribe(
                target,
                wav_path,
                language=conf["language"],
                prompt=conf["transcribe_prompt"],
            )

            if conf["filter_hallucinations"] and vad.looks_like_hallucination(raw, duration):
                self._discard(wav_path)
                self.failed.emit(t("Discarded a stock phrase: “{text}”", text=raw[:60]))
                return

            text = raw
            warning = ""
            # Claude reads through “eee” and “hani” without help, so a dictation
            # on its way there is normally sent as it was heard, one API call and
            # a second or two lighter.
            if (conf["assistant_cleanup"] if ask else conf["cleanup_enabled"]):
                self.stage.emit(t("Cleaning up…"))
                try:
                    text = cleanup.run(raw, conf, conf.cleanup_prompt())
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

            with _paste_lock:
                previous = (paste.read_clipboard()
                            if conf["restore_clipboard"] and wants_paste else None)
                try:
                    paste.copy(text)
                    if wants_paste:
                        self.stage.emit(t("Pasting…"))
                        paste.press(conf["paste_shortcut"], focus=focus)
                finally:
                    if previous is not None:
                        # Let the focused application consume the temporary
                        # transcription before putting every old clipboard type
                        # back.  This also runs when key injection fails.
                        time.sleep(0.35)
                        paste.copy_bytes(previous)

            cfg.append_history({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration": round(duration, 1),
                "elapsed": round(time.monotonic() - started, 1),
                "model": target.model,
                "cleanup_model": cleanup.model(conf) if conf["cleanup_enabled"] else "",
                "cleanup_error": warning,
                "mode": "ask" if ask else "",
                "question": question,
                "assistant_model": conf["assistant_model"] if ask else "",
                "raw": raw,
                "text": text,
            })
            try:
                cfg.trim_history(conf["history_limit"])
            except OSError as exc:
                print(f"dikte: could not trim the history: {exc}", file=sys.stderr)
            self.finished.emit(raw, text, warning)

        except assistant.Cancelled:
            self.cancelled.emit()
        except (api.ApiError, paste.PasteError, assistant.AssistantError) as exc:
            print(f"dikte: {exc}", file=sys.stderr)
            self.failed.emit(str(exc))
        except Exception as exc:  # never fail silently
            traceback.print_exc()
            self.failed.emit(t("Unexpected error: {error}", error=exc))
        finally:
            self._discard(wav_path)

    def _discard(self, wav_path):
        if not os.path.exists(wav_path):
            return
        if self.conf["keep_audio"]:
            try:
                cfg.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(wav_path, cfg.RECORDINGS_DIR / (time.strftime("%Y%m%d-%H%M%S") + ".wav"))
                return
            except OSError:
                pass
        try:
            os.unlink(wav_path)
        except OSError:
            pass
