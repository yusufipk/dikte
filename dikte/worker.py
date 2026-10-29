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
from . import dictionary
from . import i18n
from . import paste
from . import profiles
from . import selection
from . import vad
from .i18n import t

CHUNK_SECONDS = audio.CHUNK_FRAMES / audio.RATE

# A dictation is speech the user has already said and cannot say again, and the
# failure a second try fixes most often is a rate limit whose window is a
# minute or less. So the transcription request climbs this ladder: a wait after
# each retryable failure, the waits growing from ten seconds to two minutes,
# five attempts in all. A run that climbs off the end fails as before, its
# audio kept.
RETRY_WAITS = (10, 30, 60, 120)

# A dictation and a command to the agent run side by side and can finish at the
# same moment. Pasting is not one step but three that must not interleave: read
# what is on the clipboard, put ours there, press the key. Two runs doing that
# at once would paste one answer and restore the other's clipboard over it.
_paste_lock = threading.Lock()


def is_kept_recording(path):
    """Whether `path` is a WAV the chain kept in the recordings directory.

    A retry uploads the file and then deletes or moves it, so a history row
    edited to point anywhere else must come to nothing."""
    if not path or not str(path).lower().endswith(".wav"):
        return False
    real = os.path.realpath(path)
    return (os.path.dirname(real) == os.path.realpath(cfg.RECORDINGS_DIR)
            and os.path.isfile(real))


class Pipeline(QObject):
    stage = pyqtSignal(str)          # human-readable progress line
    finished = pyqtSignal(str, str, str, str)  # raw, final text, warning, language
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    # A question about selected text: a dict with the instruction, the reply,
    # whether it is an edit, and what was selected. Never pasted from here.
    answered = pyqtSignal(object)

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
        # Kept recordings with a retry queued or running. Two clicks on Retry
        # must not send one file twice: the first run moves or deletes it, and
        # the second would fail on a file that is no longer there.
        self._retrying = set()

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def run(self, wav_path, duration, rms_values=(), ask=False, paste=None,
            focus=None, retry=None, profile=None, assist=None):
        """`paste` overrides the setting for this one run, which is what a
        dictation asked for from a terminal wants: the text comes back down the
        socket, and pasting it into whatever had focus is nobody's intention.

        `focus` is the application that was in front when the recording began,
        as a process id, and is where the paste is meant to land.

        A run started while one is going waits its turn rather than being
        dropped: the next dictation can be spoken while the last one is still
        being cleaned up, and each one is finished, pasted and reported in the
        order it was spoken.

        `retry` is the history row of a failed dictation whose kept recording
        `wav_path` is. Its outcome takes that row's place rather than adding a
        new one. False when that recording is already being retried.

        `profile` is the profiles.Choice the recording was started under, or
        app.py's lookup that will produce one (anything with a result()),
        carried with the job so a queued run keeps it. None for the agent,
        which no profile ever shapes.

        `assist` is the selection.Grab of a question about selected text. The
        reply then goes out on `answered` rather than to the clipboard."""
        with self._jobs_lock:
            if retry is not None:
                if wav_path in self._retrying:
                    return False
                self._retrying.add(wav_path)
            self._jobs.append((wav_path, duration, list(rms_values), ask, paste,
                               focus, retry, profile, assist))
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
            try:
                self._work(*job)
            finally:
                with self._jobs_lock:
                    self._retrying.discard(job[0])

    def cancel(self):
        """Give up on a job already under way.

        The two things long enough to be worth interrupting honour it: a
        Claude call mid-answer, and the wait between two tries of a failing
        transcription. Everything else in the chain is over in seconds.
        """
        self._stop.set()

    def _transcribe(self, call, stage):
        """`call`, asked again on the ladder when what failed was the moment,
        not the request.

        A rate limit or a dropped connection says the request was fine and the
        minute was bad, so asking again after a wait beats failing a run whose
        speech the user cannot repeat. The line the user is watching says what
        went wrong and how long the next wait is, and the stage line comes back
        once the wait is over, so a silent minute never looks hung. A stop
        landing mid-wait ends the run with the reason it was retrying;
        anything the ladder cannot fix, or a run past its last wait, raises as
        before and is handled one level up.
        """
        waits = RETRY_WAITS + (None,)  # the last failure has nothing to wait for
        for attempt, wait in enumerate(waits):
            try:
                return call()
            except api.ApiError as exc:
                if wait is None or not exc.retryable:
                    raise
                self.stage.emit(t(
                    "{error} Retrying in {seconds} s ({attempt}/{total})…",
                    error=exc, seconds=wait, attempt=attempt + 2,
                    total=len(waits)))
                # The wait itself is where a stop lands: the event answers
                # True the moment cancel() arrives, and the run ends with the
                # error it was waiting out, its audio kept, not another ask.
                if self._stop.wait(wait):
                    raise
                self.stage.emit(stage)

    def _work(self, wav_path, duration, rms_values, ask, paste_override=None,
              focus=None, retry=None, profile=None, assist=None):
        conf = self.conf
        if ask:
            profile = None
        started = time.monotonic()
        raw = ""
        # The row this run has written, so a failure after it (the clipboard
        # refusing the text) does not add a second, failed row for the same
        # dictation.
        written = None

        if retry is not None and not is_kept_recording(wav_path):
            self.failed.emit(t("The recording is no longer there: {path}",
                               path=wav_path))
            return

        # Room tone only: don't spend an API call, and don't invite a
        # hallucinated sentence back. A retry has no levels to judge by, and
        # its recording passed this check the first time.
        if conf["skip_silent"] and retry is None:
            stats = vad.analyse(rms_values, CHUNK_SECONDS, conf["speech_margin_db"])
            if vad.is_silent(stats, conf["silence_db"], conf["speech_margin_db"],
                             conf["min_voiced_seconds"]):
                self._discard(wav_path)
                self.failed.emit(
                    t("No speech detected ({level} dB)", level=round(stats["speech_db"]))
                )
                return

        try:
            stage = t("Transcribing…")
            self.stage.emit(stage)
            target = conf.transcribe_target()
            # The spoken language is only knowable after the fact, and only the
            # local server says what it heard: auto mode asks it there, and
            # every other run (a fixed language, or a hosted provider that
            # detects but stays silent) transcribes as before.
            auto = conf["language"] == "auto"
            # Read once, at the start the run: a retry asks the same request
            # again, not whatever the settings have drifted to since.
            language, prompt = conf["language"], conf.dictionary_hint()
            if auto:
                raw, detected = self._transcribe(
                    lambda: api.transcribe_detected(
                        target, wav_path, language=language, prompt=prompt,
                    ),
                    stage,
                )
            else:
                raw = self._transcribe(
                    lambda: api.transcribe(
                        target, wav_path, language=language, prompt=prompt,
                    ),
                    stage,
                )
                detected = ""

            if conf["filter_hallucinations"] and vad.looks_like_hallucination(raw, duration):
                message = t("Discarded a stock phrase: “{text}”", text=raw[:60])
                if retry is not None:
                    # Asked for by hand, on a recording the user wanted back:
                    # it stays, and so does its row, now saying why.
                    self.failed.emit(self._give_up(wav_path, message, duration,
                                                   target.model or target.provider, retry,
                                                   profile))
                    return
                self._discard(wav_path)
                self.failed.emit(message)
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
            # Only now: transcription does not need the profile, so a lookup
            # still running has had the whole round trip to finish.
            profile = self._resolved(profile)
            # A translation is the cleanup model's job too, so asking for one
            # runs that step even with cleanup switched off; the prompt then
            # translates without tidying (see Config.cleanup_prompt).
            translating = bool(profile is not None and profile.translate_to)
            translation_failed = False
            # Claude reads through “eee” and “hani” without help, so a dictation
            # on its way there is normally sent as it was heard, one API call and
            # a second or two lighter.
            if (conf["assistant_cleanup"] if ask
                    else conf["cleanup_enabled"] or translating):
                self.stage.emit(self._cleanup_stage(profile))
                cleaned = True
                try:
                    text = cleanup.run(raw, conf, conf.cleanup_prompt(
                        speech=detected, profile=profile))
                except api.ApiError as exc:
                    # Keep the transcript, but never let the failure pass unseen:
                    # a rejected key would otherwise look like working dictation.
                    text = raw
                    warning = str(exc)
                    if translating:
                        # The original goes to the clipboard and not into the
                        # window: the user asked for another language, and
                        # pasting this one where it was meant to go would be
                        # a wrong result that looks like a right one.
                        translation_failed = True
                        warning = t("Translation failed, so the original "
                                    "transcript was copied and not pasted: "
                                    "{error}", error=exc)
                    print(f"dikte: cleanup failed: {exc}", file=sys.stderr)

            question = ""
            selected = selection.NOTHING
            kind = ""
            if ask:
                question = text
                self.stage.emit(t("Asking {name}…", name=i18n.name(
                    assistant.display_name(conf), "dative")))
                if assist is None:
                    text, denied = assistant.ask(
                        question, conf,
                        on_stage=self.stage.emit,
                        should_stop=self._stop.is_set,
                    )
                else:
                    # Collected only now: the key press started it, and the
                    # transcription has given it all the time it needs.
                    selected = assist.result()
                    prompt = selection.build_prompt(
                        question, selected.text, assistant.uses_tools(conf))
                    system = selection.SYSTEM_PROMPT
                    # The instruction from Settings, when it is the user's own,
                    # still holds for a plain question. A run with a selection
                    # keeps to the strict prompt alone.
                    own = conf["assistant_prompt"].strip()
                    if own and not selected.text and own not in (
                            cfg.ASSISTANT_PROMPT_EN.strip(),
                            cfg.ASSISTANT_PROMPT_TR.strip()):
                        system += "\n\n" + own
                    reply, denied = assistant.ask(
                        prompt, conf,
                        on_stage=self.stage.emit,
                        should_stop=self._stop.is_set,
                        keep_session=False,
                        # Selected text was not written by the user and may
                        # carry instructions of its own, so it only goes to a
                        # run with no tools; a provider that cannot do that
                        # refuses it with a message.
                        tool_free=bool(selected.text),
                        system=system,
                    )
                    kind, text = selection.parse_reply(reply, selected.text)
                warning = "\n".join(x for x in (warning, denied) if x)

            wants_paste = (conf["assistant_paste"] if ask else conf["auto_paste"])
            if paste_override is not None:
                wants_paste = paste_override
            if translation_failed:
                wants_paste = False

            # Into the history before the paste is attempted: the record says
            # what was dictated, not whether a key press landed, and a paste
            # that fails must not take the transcript down with it.
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration": round(duration, 1),
                "elapsed": round(time.monotonic() - started, 1),
                "model": target.model or target.provider,
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
            if assist is not None:
                # What kind of reply it was, and nothing of the selection
                # itself: the history keeps what was said and what came back,
                # as it does for every command. An edit's reply is a rewrite
                # of the selection, so much of it is in `text` all the same.
                record["assist"] = kind
            # Only when there is something to tell apart, so that a history
            # written without profiles has the same rows it always had.
            self._mark(record, profile)
            if translating:
                record["translate_to"] = profile.translate_to
                record["translated"] = not translation_failed
            if retry is not None:
                # Listed where it was spoken, not where it was retried.
                record["ts"] = retry.get("ts") or record["ts"]
                written = cfg.replace_history(retry, record)
            if written is None:
                cfg.append_history(record)
                written = record
            try:
                cfg.trim_history(conf["history_limit"])
            except OSError as exc:
                print(f"dikte: could not trim the history: {exc}", file=sys.stderr)

            if assist is not None:
                # Shown, never pasted: the answer window decides what happens
                # to it, and only when the user says so.
                self.answered.emit({
                    "instruction": question, "text": text, "kind": kind,
                    "selection": selected.text, "warning": warning,
                    "focus": focus, "speech_language": speech_language,
                    "reads_selection": getattr(assist, "reads", True),
                    "selection_dropped": bool(getattr(assist, "dropped", False)),
                })
                return

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
                        written = record
                if previous is not None:
                    # Let the focused application consume the temporary
                    # transcription before putting every old clipboard type
                    # back.
                    time.sleep(0.35)
                    paste.copy_bytes(previous)

            self.finished.emit(raw, text, warning, speech_language)
            if (conf["dictionary_learning"] and not ask and retry is None
                    and cleaned and not translating and not warning):
                self._learn(raw, text, speech_language)

        except assistant.Cancelled:
            self.cancelled.emit()
        except (api.ApiError, paste.PasteError, assistant.AssistantError) as exc:
            print(f"dikte: {exc}", file=sys.stderr)
            self.failed.emit(self._failure(wav_path, str(exc), duration, ask,
                                           written, retry, profile))
        except Exception as exc:  # never fail silently
            traceback.print_exc()
            self.failed.emit(self._failure(
                wav_path, t("Unexpected error: {error}", error=exc), duration,
                ask, written, retry, profile))
        finally:
            # A retry that did not get its transcript into the history leaves
            # its recording alone: when the move to a new name failed, the file
            # where it is now is the only copy of the speech.
            if retry is None or written is not None:
                self._discard(wav_path)

    @staticmethod
    def _cleanup_stage(profile):
        """The progress line for the cleanup step, naming the profile and the
        language when there is one to name."""
        if profile is not None and profile.translate_to:
            return t("Translating into {language}…", language=t(
                profiles.language_name(profile.translate_to)))
        if profile is not None and profile.shown:
            return t("Cleaning up ({profile})…",
                     profile=profiles.display_name(profile))
        return t("Cleaning up…")

    @staticmethod
    def _mark(record, profile):
        """Write which profile a row was made under, when that is worth
        saying. The name, not the Choice: a retry looks it up again."""
        if profile is not None and profile.shown:
            record["profile"] = profile.name
            if profile.id:
                record["profile_id"] = profile.id
            # Only when it was asked for: "" then means it could not be found
            # out, which is worth saying, and a skipped lookup is not.
            if profile.looked:
                record["program"] = profile.program
            # The keyword that matched, never the title it matched in.
            if profile.site:
                record["site"] = profile.site

    def _learn(self, raw, text, speech_language):
        """Hand a finished dictation to the dictionary. Never at the cost of
        the dictation itself, which has already been delivered."""
        try:
            dictionary.learn(self.conf, raw, text, speech_language)
        except Exception as exc:  # learning is a bonus, not part of the run
            print(f"dikte: could not learn from the dictation: {exc}",
                  file=sys.stderr)

    def _failure(self, wav_path, message, duration, ask, written, retry,
                 profile=None):
        """The message for a failed run, its audio kept and, for a dictation
        that left no row of its own, a failed row the history can retry."""
        if ask or written is not None:
            return self._keeping(wav_path, message)[0]
        try:
            target = self.conf.transcribe_target()
            # A custom server left to pick its own model has no id to show.
            model = target.model or target.provider
        except Exception:  # the settings themselves may be what failed
            model = ""
        return self._give_up(wav_path, message, duration, model, retry, profile)

    @staticmethod
    def _resolved(profile):
        """A lookup still working out the program in front, handed over by
        app.py so that the main thread never waits on it, becomes its Choice.
        This thread may wait."""
        return profile.result() if hasattr(profile, "result") else profile

    def _give_up(self, wav_path, message, duration, model, retry, profile=None):
        """Keep the audio and record the dictation as failed, so the history
        shows it with a way to try again. A retry that failed once more updates
        its own row instead of adding another."""
        profile = self._resolved(profile)
        message, kept = self._keeping(wav_path, message)
        if not kept:
            return message
        record = {
            "ts": (retry or {}).get("ts") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": round(duration, 1),
            "model": model,
            "mode": "",
            "failed": True,
            "error": message.splitlines()[0] if message else "",
            "audio": kept,
            "raw": "",
            "text": "",
        }
        # A retry is run under the profile it was first spoken under.
        self._mark(record, profile)
        try:
            if retry is None or cfg.replace_history(retry, record) is None:
                cfg.append_history(record)
                cfg.trim_history(self.conf["history_limit"])
        except OSError as exc:
            print(f"dikte: could not record the failure: {exc}", file=sys.stderr)
        return message

    def _keeping(self, wav_path, message):
        """Put the failed run's audio somewhere a retry can find it.

        A dictation that died on the way to the model is speech the user cannot
        say again from memory; deleting it because a server was down turns one
        failure into two. Kept regardless of the keep_audio setting, which is
        about the runs that succeeded. The message and the kept path, '' when
        the move failed.
        """
        kept = self._keep(wav_path)
        if not kept:
            return message, ""
        return (message + "\n" + t("The recording was kept: {path}", path=kept),
                kept)

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
            # A retried recording is already where kept audio lives.
            if os.path.dirname(os.path.realpath(wav_path)) == \
                    os.path.realpath(cfg.RECORDINGS_DIR):
                return
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
