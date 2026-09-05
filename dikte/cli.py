"""Dikte from a terminal, with everything the windows can do.

The verbs that need the microphone are handed to the running instance over its
socket: the microphone is one device, that process owns it, and it is also the
one drawing the indicator. Everything else runs right here and needs nothing
else running, which is what makes transcribing a file or reading a setting work
over ssh and inside a script.

Output is meant for a program as much as for a person. --json turns any answer
into one object on stdout, progress lines go to stderr so they never end up in
a pipe, and the exit code says which of the three things happened: 0 done,
1 failed, 2 the command line was wrong, 3 nothing is running.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser

from PyQt6.QtCore import QCoreApplication, QTimer

from . import api
from . import assistant
from . import audio
from . import cleanup
from . import config as cfg
from . import filetranscribe
from . import ggml
from . import hotkey
from . import hub
from . import ipc
from . import integrate
from . import meeting
from . import paste
from . import update
from . import __version__

NOT_RUNNING = 3

# Verbs that start the application when none is running, which is what a
# shortcut registered with the desktop has always relied on: press the key on a
# fresh login and Dikte comes up recording.
GUI_VERBS = {"", "settings", "toggle", "ask", "meeting"}

# Asking a process that is not there to stop, cancel or quit is not a failure;
# it is already in the state that was asked for.
IDEMPOTENT_VERBS = {"cancel", "stop", "quit", "restart", "ask-cancel",
                    "ask-reset", "meeting-cancel", "pause"}

_app = None


# --- talking to the terminal ----------------------------------------------

def out(opts, payload, text=""):
    """The answer: one JSON object, or the plain thing a person wanted."""
    if opts.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif text:
        print(text)
    return 0


def note(opts, message):
    """A progress line. Never stdout: stdout is the answer."""
    if message and not opts.quiet:
        print(message, file=sys.stderr, flush=True)


def fail(opts, message, code=1, **extra):
    if opts.json:
        payload = {"ok": False, "error": str(message)}
        payload.update(extra)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"dikte: {message}", file=sys.stderr)
    return code


def _pick(flag, fallback):
    """A --thing/--no-thing pair that was not given falls back to the setting."""
    return fallback if flag is None else flag


def _headless(connect, on_interrupt=None):
    """Run one of the signal-driven workers to the end without a window.

    `connect` is handed a callback, wires it to the worker's signals and starts
    the worker; whatever it passes back comes out of here. The event loop is
    only here to carry those signals across from the worker thread.
    """
    app = QCoreApplication.instance()
    box = {}

    def finish(result):
        box["result"] = result
        app.quit()

    def interrupt(*_):
        box.setdefault("result", {"error": "interrupted"})
        if on_interrupt:
            on_interrupt()
        app.quit()

    signal.signal(signal.SIGINT, interrupt)
    # Qt's loop does not run Python between events, so without something ticking
    # a Ctrl+C would sit unnoticed until the job finished on its own.
    ticker = QTimer()
    ticker.timeout.connect(lambda: None)
    ticker.start(200)
    QTimer.singleShot(0, lambda: connect(finish))
    app.exec()
    ticker.stop()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return box.get("result") or {"error": "interrupted"}


# --- the running instance --------------------------------------------------

def _ask_instance(opts, cmd, wait=False, **args):
    """Send a request, or explain why there is nobody to send it to."""
    reply = ipc.send(cmd, wait=wait, timeout=getattr(opts, "timeout", 0), **args)
    if reply is not None:
        return reply
    verb = getattr(opts, "verb", cmd)
    if verb in GUI_VERBS and not wait:
        launch_gui(verb)          # replaces this process; never comes back
    if verb in IDEMPOTENT_VERBS:
        return {"ok": True, "running": False}
    return None


def launch_gui(verb=""):
    """No instance running, so become the application itself."""
    ipc.respawn(([verb] if verb else []) + ["--gui"])
    # respawn only returns on Windows, where the application was started
    # detached and this console process's job is over.
    sys.exit(0)


def _not_running(opts):
    return fail(opts, "Dikte is not running. Start it with: dikte", NOT_RUNNING,
                running=False)


# --- dictation -------------------------------------------------------------

def cmd_record(opts):
    """Record, transcribe, clean up, and print what was said."""
    reply = _ask_instance(
        opts, "record", wait=not opts.no_wait,
        seconds=opts.seconds, paste=bool(opts.paste),
    )
    if reply is None:
        return _not_running(opts)
    return _dictation_result(opts, reply)


def cmd_toggle(opts):
    reply = _ask_instance(opts, opts.verb, wait=opts.wait,
                          paste=opts.paste if opts.paste else None)
    if reply is None:
        return _not_running(opts)
    return _dictation_result(opts, reply)


def _dictation_result(opts, reply):
    if not reply.get("ok"):
        return fail(opts, reply.get("error") or "the recording did not go through")
    if reply.get("warning"):
        note(opts, reply["warning"])
    if "text" not in reply:
        return out(opts, reply, "")
    return out(opts, reply, reply.get("text", ""))


def cmd_cancel(opts):
    reply = _ask_instance(opts, "cancel")
    return 0 if reply is not None else _not_running(opts)


def cmd_plain(opts):
    """The verbs with nothing to say: settings, restart, quit, ask-reset…"""
    reply = _ask_instance(opts, opts.verb)
    if reply is None:
        return _not_running(opts)
    if not reply.get("ok"):
        return fail(opts, reply.get("error") or opts.verb)
    return out(opts, reply, "")


# --- the agent -------------------------------------------------------------

def cmd_ask(opts):
    """Put a command to the agent. With no text, record one first."""
    text = " ".join(opts.text).strip() if opts.text else ""
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        reply = _ask_instance(opts, "ask", wait=opts.wait,
                              paste=opts.paste if opts.paste else None)
        if reply is None:
            return _not_running(opts)
        if not reply.get("ok"):
            return fail(opts, reply.get("error") or "the command did not go through")
        if reply.get("warning"):
            note(opts, reply["warning"])
        return out(opts, reply, reply.get("answer", ""))

    conf = cfg.Config()
    if opts.provider:
        conf["assistant_provider"] = opts.provider
    if opts.model:
        key = {"claude": "assistant_model", "codex": "assistant_codex_model",
               "openrouter": "assistant_openrouter_model",
               "agy": "assistant_agy_model",
               "opencode": "assistant_opencode_model"}[assistant.provider(conf)]
        conf[key] = opts.model
    if opts.dir:
        conf["assistant_dir"] = opts.dir
    if opts.new:
        assistant.clear_session()

    stopped = []
    signal.signal(signal.SIGINT, lambda *_: stopped.append(True))
    started = time.monotonic()
    try:
        answer, warning = assistant.ask(
            text, conf, on_stage=lambda stage: note(opts, stage),
            should_stop=lambda: bool(stopped),
        )
    except assistant.Cancelled:
        return fail(opts, "stopped", 130)
    except (assistant.AssistantError, api.ApiError) as exc:
        return fail(opts, exc)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    if opts.paste or opts.copy:
        try:
            paste.copy(answer)
            if opts.paste:
                paste.press(conf["paste_shortcut"])
        except paste.PasteError as exc:
            note(opts, str(exc))

    cfg.append_history({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": 0.0,
        "elapsed": round(time.monotonic() - started, 1),
        "model": "",
        "cleanup_model": "",
        "cleanup_error": warning,
        "mode": "ask",
        "question": text,
        "assistant": assistant.provider(conf),
        "assistant_model": assistant.model(conf),
        "raw": text,
        "text": answer,
    })
    if warning:
        note(opts, warning)
    return out(opts, {"ok": True, "question": text, "answer": answer,
                      "warning": warning,
                      "provider": assistant.provider(conf)}, answer)


def cmd_session(opts):
    if opts.session == "reset":
        assistant.clear_session()
        return out(opts, {"ok": True, "conversation": None},
                   "The conversation has been dropped.")
    conf = cfg.Config()
    age = assistant.session_age()
    minutes = None if age is None else int(age // 60)
    # The conversation on disk belongs to whoever was asked last, which is not
    # always whoever would be asked next; none of them can pick up another's.
    owner = assistant.stored_provider()
    told = f"{assistant.display_name(conf)}: "
    if minutes is None:
        told += "no conversation going."
    elif owner != assistant.provider(conf):
        told += f"nothing going; the conversation on disk is {owner}'s."
    else:
        told += f"last used {minutes} min ago."
    return out(
        opts,
        {"ok": True, "provider": assistant.provider(conf),
         "agent": assistant.display_name(conf), "conversation": owner or None,
         "idle_minutes": minutes,
         "keeps_minutes": conf["assistant_session_minutes"]},
        told,
    )


# --- a file ----------------------------------------------------------------

def cmd_transcribe(opts):
    path = os.path.expanduser(opts.file)
    if not os.path.isfile(path):
        return fail(opts, f"no such file: {path}")

    conf = cfg.Config()
    # This runs here rather than in the instance, so the local servers have to
    # be handed their settings here too; the GUI does this at startup.
    conf.apply_local()
    timestamps = opts.srt or _pick(opts.timestamps, conf["file_timestamps"])
    worker = filetranscribe.FileTranscriber(conf)

    def begin(finish):
        worker.progress.connect(lambda message: note(opts, message))
        worker.finished.connect(
            lambda text, segments: finish({"text": text, "segments": segments})
        )
        worker.failed.connect(lambda error: finish({"error": error}))
        worker.start(path, timestamps, _pick(opts.cleanup, conf["file_cleanup"]))

    result = _headless(begin, on_interrupt=worker.stop)
    if result.get("error"):
        return fail(opts, result["error"])

    text, segments = result["text"], result["segments"]
    srt = filetranscribe.to_srt(text, segments) if opts.srt else ""
    if opts.srt and not srt:
        return fail(opts, "no timestamped lines to turn into subtitles")

    body = srt or text
    written = ""
    if opts.out:
        written = os.path.expanduser(opts.out)
        try:
            with open(written, "w", encoding="utf-8") as fh:
                fh.write(body if body.endswith("\n") else body + "\n")
        except OSError as exc:
            return fail(opts, exc)
        note(opts, f"Saved: {written}")

    return out(opts, {"ok": True, "text": text, "srt": srt or None,
                      "segments": [list(item) for item in segments],
                      "path": written or None},
               "" if written else body)


# --- meetings ---------------------------------------------------------------

def cmd_meeting(opts):
    """The tray verb: start a meeting, or end it and write it up."""
    reply = _ask_instance(opts, opts.verb, wait=getattr(opts, "wait", False))
    if reply is None:
        return _not_running(opts)
    if not reply.get("ok"):
        return fail(opts, reply.get("error") or "the meeting did not go through")
    title = reply.get("title", "")
    return out(opts, reply, f"{title}\n{reply['path']}" if title else "")


def _find_meeting(which):
    """A meeting by its base, by 1 for the newest, or 'last'."""
    rows = cfg.read_meetings()
    if not rows:
        return None
    if which in ("", "last"):
        return rows[-1]
    # A stem is all digits too, so a number is only a position while there are
    # that many meetings to count back through. Anything larger is a date
    # somebody typed: nobody is looking for the twenty-millionth meeting.
    if which.isdigit() and 0 < int(which) <= len(rows):
        return rows[-int(which)]
    exact = [row for row in rows if row["base"] == which]
    if exact:
        return exact[0]
    near = [row for row in rows if row["base"].startswith(which)]
    return near[-1] if near else None


def cmd_meetings_list(opts):
    rows = cfg.read_meetings()
    lines = []
    for index, row in enumerate(reversed(rows), start=1):
        lines.append(
            f"{index:>3}  {row['base']}  {row.get('ts', ''):16}  "
            f"{int(row.get('duration', 0)) // 60:>4} min  "
            f"{row.get('status', ''):11}  {row.get('title') or ''}".rstrip()
        )
    return out(opts, {"ok": True, "meetings": list(reversed(rows))},
               "\n".join(lines) or "No meetings recorded yet.")


def cmd_meetings_show(opts):
    row = _find_meeting(opts.which)
    if row is None:
        return fail(opts, f"no such meeting: {opts.which}")
    doc_path, wav_path = cfg.meeting_paths(row["base"])
    try:
        document = doc_path.read_text(encoding="utf-8")
    except OSError:
        document = ""
    body = meeting.read_transcript(document) if opts.transcript else document
    if not body:
        return fail(opts, row.get("error") or "nothing has been written yet",
                    base=row["base"], status=row.get("status", ""))
    return out(opts, {"ok": True, "base": row["base"], "title": row.get("title", ""),
                      "status": row.get("status", ""), "path": str(doc_path),
                      "audio": str(wav_path) if wav_path.exists() else None,
                      "text": body},
               body)


def cmd_meetings_retry(opts):
    row = _find_meeting(opts.which)
    if row is None:
        return fail(opts, f"no such meeting: {opts.which}")
    status = ipc.send("status") or {}
    if status.get("meeting_base") == row["base"]:
        return fail(opts, "the application is already writing this one up")

    conf = cfg.Config()
    pipeline = meeting.MeetingPipeline(conf)

    def start(finish):
        pipeline.progress.connect(lambda _base, message: note(opts, message))
        pipeline.finished.connect(
            lambda base, title: finish({"base": base, "title": title})
        )
        pipeline.failed.connect(lambda _base, error: finish({"error": error}))
        pipeline.run(row)

    result = _headless(start, on_interrupt=pipeline.stop)
    if result.get("error"):
        return fail(opts, result["error"], base=row["base"])
    doc_path, _wav = cfg.meeting_paths(result["base"])
    return out(opts, {"ok": True, "base": result["base"], "title": result["title"],
                      "path": str(doc_path)},
               f"{result['title']}\n{doc_path}")


def cmd_meetings_delete(opts):
    bases = []
    for which in opts.which:
        row = _find_meeting(which)
        if row is None:
            return fail(opts, f"no such meeting: {which}")
        bases.append(row["base"])
    status = ipc.send("status") or {}
    if status.get("meeting_base") in bases:
        return fail(opts, "that meeting is being written up right now")
    cfg.delete_meetings(bases)
    return out(opts, {"ok": True, "deleted": bases},
               f"Deleted {len(bases)} " + ("meeting." if len(bases) == 1 else "meetings."))


# --- history ----------------------------------------------------------------

def _find_history(which):
    """A row by 1 for the newest, counting back. 'last' is the same as 1."""
    rows = cfg.read_history()
    if not rows:
        return None
    if which in ("", "last"):
        return rows[-1]
    if not which.isdigit():
        return None
    index = int(which)
    return rows[-index] if 0 < index <= len(rows) else None


def cmd_history_list(opts):
    rows = cfg.read_history(opts.limit if opts.limit > 0 else None)
    lines = []
    for index, row in enumerate(reversed(rows), start=1):
        preview = (row.get("text") or "").replace("\n", " ")
        mode = "ask " if row.get("mode") == "ask" else "    "
        lines.append(
            f"{index:>3}  {row.get('ts', ''):19}  {row.get('duration', 0):>6.1f}s  "
            f"{mode}{preview[:60]}"
        )
    return out(opts, {"ok": True, "history": list(reversed(rows))},
               "\n".join(lines) or "Nothing dictated yet.")


def cmd_history_show(opts):
    row = _find_history(opts.which)
    if row is None:
        return fail(opts, f"no such entry: {opts.which}")
    body = row.get("raw", "") if opts.raw else row.get("text", "")
    return out(opts, {"ok": True, **row}, body)


def cmd_history_delete(opts):
    rows = []
    for which in opts.which:
        row = _find_history(which)
        if row is None:
            return fail(opts, f"no such entry: {which}")
        rows.append(row)
    cfg.delete_history(rows)
    return out(opts, {"ok": True, "deleted": len(rows)},
               f"Deleted {len(rows)} " + ("entry." if len(rows) == 1 else "entries."))


def cmd_history_clear(opts):
    if not opts.yes:
        return fail(opts, "this deletes the whole history; pass --yes to mean it", 2)
    cfg.clear_history()
    return out(opts, {"ok": True}, "History cleared.")


# --- settings ---------------------------------------------------------------

SECRET_KEYS = ("openai_api_key", "groq_api_key", "openrouter_api_key",
               "gemini_api_key", "opencode_api_key")


def _mask(key, value):
    if key in SECRET_KEYS and value:
        return f"…{value[-4:]}"
    return value


def _coerce(key, raw):
    """A value off the command line, in the type the setting is stored as."""
    default = cfg.DEFAULTS[key]
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key} wants true or false, got: {raw}")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return raw


def _tell_instance_to_reload():
    """A setting changed under a running instance means nothing until it reads
    it back, and the window it was not changed in would otherwise overwrite it."""
    ipc.send("reload")


def cmd_config_list(opts):
    conf = cfg.Config()
    # A key belongs to whoever asked for it by name, not to everything that ever
    # prints the whole list; a terminal scrollback is a poor place for one.
    values = {key: conf[key] if opts.reveal else _mask(key, conf[key])
              for key in sorted(cfg.DEFAULTS)}
    lines = []
    for key, value in values.items():
        shown = value
        if isinstance(shown, str) and len(shown) > 60:
            shown = shown[:57].replace("\n", " ") + "…"
        lines.append(f"{key} = {shown}")
    return out(opts, {"ok": True, "config": values, "path": str(cfg.CONFIG_FILE)},
               "\n".join(lines))


def cmd_config_get(opts):
    if opts.key not in cfg.DEFAULTS:
        return fail(opts, f"unknown setting: {opts.key}", 2)
    value = cfg.Config()[opts.key]
    return out(opts, {"ok": True, "key": opts.key, "value": value},
               json.dumps(value, ensure_ascii=False) if not isinstance(value, str)
               else value)


def cmd_config_set(opts):
    if opts.key not in cfg.DEFAULTS:
        return fail(opts, f"unknown setting: {opts.key}", 2)
    raw = opts.value
    if raw is None:
        if sys.stdin.isatty():
            return fail(opts, "no value given, and nothing on stdin to read", 2)
        raw = sys.stdin.read().rstrip("\n")
    try:
        value = _coerce(opts.key, raw)
    except ValueError as exc:
        return fail(opts, exc, 2)

    conf = cfg.Config()
    conf[opts.key] = value
    try:
        conf.save()
    except OSError as exc:
        return fail(opts, exc)
    _tell_instance_to_reload()
    return out(opts, {"ok": True, "key": opts.key, "value": value},
               f"{opts.key} = {_mask(opts.key, value)}")


def cmd_config_reset(opts):
    keys = opts.key or []
    if opts.all:
        keys = list(cfg.DEFAULTS)
    if not keys:
        return fail(opts, "name a setting, or pass --all", 2)
    unknown = [key for key in keys if key not in cfg.DEFAULTS]
    if unknown:
        return fail(opts, f"unknown setting: {unknown[0]}", 2)
    conf = cfg.Config()
    for key in keys:
        conf[key] = cfg.DEFAULTS[key]
    try:
        conf.save()
    except OSError as exc:
        return fail(opts, exc)
    _tell_instance_to_reload()
    return out(opts, {"ok": True, "reset": keys},
               f"Reset {len(keys)} setting(s) to their defaults.")


def cmd_config_path(opts):
    return out(opts,
               {"ok": True, "config": str(cfg.CONFIG_FILE),
                "data": str(cfg.DATA_DIR), "history": str(cfg.HISTORY_FILE),
                "meetings": str(cfg.MEETINGS_DIR),
                "recordings": str(cfg.RECORDINGS_DIR)},
               str(cfg.CONFIG_FILE))


def cmd_prompt(opts):
    """The prompt a run would really use, defaults and glossary folded in."""
    conf = cfg.Config()
    prompts = {
        "cleanup": conf.cleanup_prompt(),
        "subtitles": conf.cleanup_prompt(subtitles=True),
        "meeting": conf.meeting_prompt(),
        "agent": conf.assistant_prompt(),
    }
    if opts.which:
        return out(opts, {"ok": True, "prompt": opts.which,
                          "text": prompts[opts.which]}, prompts[opts.which])
    return out(opts, {"ok": True, "prompts": prompts},
               "\n\n".join(f"--- {name} ---\n{text}"
                           for name, text in prompts.items()))


# --- the machine ------------------------------------------------------------

def cmd_devices(opts):
    conf = cfg.Config()
    default = audio.default_monitor()
    mics = [{"name": name, "description": desc,
             "chosen": name == conf["mic_target"]}
            for name, desc in audio.list_sources()]
    monitors = [{"name": name, "description": desc,
                 "chosen": name == conf["meeting_system_target"],
                 "default": name == default}
                for name, desc in audio.list_monitors()]
    if not mics and not monitors:
        # Which program was asked, and so which one to go and look at, is not
        # the same on all four systems: naming pactl on Windows sends somebody
        # after a program that was never going to be there.
        return fail(opts, audio.sound().missing)

    lines = ["Microphones:"]
    lines += [f"  {'*' if item['chosen'] else ' '} {item['name']}\n"
              f"      {item['description']}" for item in mics]
    lines += ["", "Monitors (what a meeting records the other side from):"]
    lines += [f"  {'*' if item['chosen'] else '·' if item['default'] else ' '} "
              f"{item['name']}\n      {item['description']}" for item in monitors]
    return out(opts, {"ok": True, "microphones": mics, "monitors": monitors},
               "\n".join(lines))


def cmd_models(opts):
    conf = cfg.Config()
    who = cfg.TRANSCRIBERS[opts.provider]
    try:
        if opts.provider == "openrouter":
            models = api.openrouter_models(conf.openrouter_key(),
                                           transcription=opts.transcription)
        else:
            models = api.openai_models(conf.api_key(who.key), conf[who.url],
                                       who.service)
    except api.ApiError as exc:
        return fail(opts, exc)
    return out(opts, {"ok": True, "provider": opts.provider, "models": models},
               "\n".join(models))


def cmd_test_key(opts):
    conf = cfg.Config()
    results = {}
    for name, who in cfg.TRANSCRIBERS.items():
        if opts.which not in (name, "all"):
            continue
        try:
            if name == "openrouter":
                # The one key that also pays for cleanup, so it reports credit
                # rather than a model count.
                message = api.openrouter_key_status(conf.openrouter_key())
            else:
                count = len(api.openai_models(conf.api_key(who.key), conf[who.url],
                                              who.service))
                message = f"connection works, {count} models visible"
            results[name] = {"ok": True, "message": message}
        except api.ApiError as exc:
            results[name] = {"ok": False, "message": str(exc)}
    if opts.which in ("gemini", "all"):
        try:
            count = len(api.gemini_models(conf.gemini_key(),
                                          conf["gemini_base_url"]))
            message = f"connection works, {count} models visible"
            results["gemini"] = {"ok": True, "message": message}
        except api.ApiError as exc:
            results["gemini"] = {"ok": False, "message": str(exc)}
    if opts.which in ("opencode", "all"):
        try:
            count = len(api.openai_models(conf.opencode_key(),
                                          conf["opencode_base_url"], "OpenCode Go"))
            results["opencode"] = {"ok": True,
                                   "message": f"connection works, {count} models visible"}
        except api.ApiError as exc:
            results["opencode"] = {"ok": False, "message": str(exc)}
    everything_ok = all(item["ok"] for item in results.values())
    lines = [f"{'✓' if item['ok'] else '✗'} {name}: {item['message']}"
             for name, item in results.items()]
    out(opts, {"ok": everything_ok, "keys": results}, "\n".join(lines))
    return 0 if everything_ok else 1


def cmd_shortcut(opts):
    conf = cfg.Config()
    if opts.shortcut == "status":
        # The running instance is asked first. On KDE and GNOME this process
        # could read the registry itself and get the same answer; on macOS
        # there is no registry, the combination is held by that process and by
        # nothing else, and asking here would report every shortcut as missing
        # while they all work.
        live = ipc.send("status") or {}
        registered = live.get("shortcuts") or {}
        rows = {}
        for name, spec in hotkey.SHORTCUTS.items():
            rows[name] = {
                "registered": registered.get(
                    name, hotkey.shortcut_status(spec.desktop_id)),
                "configured": conf[spec.setting],
            }
        listener = live.get("listener", conf["evdev_hotkey"])
        lines = [f"{name:8} {row['registered'] or '(not installed)':16} "
                 f"setting: {row['configured'] or '(none)'}"
                 for name, row in rows.items()]
        lines.append(f"built-in listener: {'on' if listener else 'off'}")
        return out(opts, {"ok": True, "shortcuts": rows,
                          "listener": listener}, "\n".join(lines))

    spec = hotkey.SHORTCUTS[opts.which]
    if opts.shortcut == "remove":
        hotkey.remove_shortcut(spec.desktop_id)
        return out(opts, {"ok": True, "removed": opts.which},
                   f"Removed the {opts.which} shortcut.")

    combo = (opts.combo or conf[spec.setting]
             or hotkey.default_combo(opts.which)).strip()
    if not combo:
        return fail(opts, "no combination given and none stored; pass --combo", 2)
    if not hotkey.valid_shortcut(combo):
        return fail(opts, f"cannot parse that combination: {combo}", 2)
    clashes = hotkey.conflicting_shortcuts(combo, spec.desktop_id)
    if clashes and not opts.force:
        return fail(opts, f"{combo} is also used by: {', '.join(clashes[:6])}. "
                          "Pass --force to install it anyway.", 1, conflicts=clashes)

    ok, message = hotkey.install_shortcut(
        combo, ipc.command_for(spec.verb), name=spec.name,
        desktop_id=spec.desktop_id,
    )
    if not ok:
        return fail(opts, message)
    conf[spec.setting] = combo
    try:
        conf.save()
    except OSError as exc:
        return fail(opts, exc)
    _tell_instance_to_reload()
    return out(opts, {"ok": True, "which": opts.which, "shortcut": combo,
                      "message": message, "conflicts": clashes},
               message)


def cmd_integrate(opts):
    """Write, or take away, the launchers a downloaded build installs itself.

    Run for you on every start, so this is for the two cases that start does
    not cover: undoing it, and repairing it from a terminal after the AppImage
    was moved while Dikte was not running. On Windows the setup program wrote
    the rest, and what is left for this is the switch it could only offer while
    it was on the screen: typing it starts Dikte at sign-in, --remove stops it.
    """
    if not integrate.packaged():
        installer = "install.ps1" if sys.platform == "win32" else "./install.sh"
        return fail(opts, "this is a checkout, not a downloaded build; "
                          f"{installer} writes those files here", 2)
    try:
        # force, because typing this is asking for it outright, where the same
        # call on every start stands aside for an installation already there.
        paths = integrate.remove() if opts.remove else integrate.install(force=True)
    except OSError as exc:
        return fail(opts, exc)
    verb = "Removed" if opts.remove else "Wrote"
    listing = "\n".join(f"  {path}" for path in paths)
    return out(opts, {"ok": True, "paths": [str(path) for path in paths]},
               f"{verb}:\n{listing}" if paths else "Nothing to change.")


def cmd_update(opts):
    """Whether a newer Dikte has been released, and where it is.

    It looks and nothing more: what to do about the answer is a download page,
    because the AppImage, the disk image, the Windows setup and a checkout are
    four different installations and only their owner knows which one this is.
    """
    try:
        release = update.latest(refresh=True)
    except hub.HubError as exc:
        return fail(opts, exc)
    # Written down even when there is nothing new, so that the application does
    # not go and ask the same question an hour later.
    update.remember(release)
    waiting = update.newer(release.version)
    payload = {"ok": True, "current": __version__, "latest": release.version,
               "update": waiting, "url": release.url}
    if not waiting:
        return out(opts, payload,
                   f"Dikte {__version__} is the newest release.")
    if opts.open:
        webbrowser.open(release.url)
    return out(opts, payload,
               f"Dikte {release.version} is out; this is {__version__}.\n"
               f"{release.url}")


# --- the models on this machine --------------------------------------------


def _local_where(entry):
    """Where a local model ran, in a phrase: the card, the processor, or neither.

    The backend and the card keep the names the server printed for them. A
    graphics card is a product somebody sells under that name, and translating
    it would be inventing hardware.
    """
    kind = ggml.accel_kind({**entry, "running": True})
    where = {"gpu": "the graphics card", "cpu": "the processor"}.get(
        kind, "something it did not name")
    detail = ggml.accel_detail(entry)
    return where + (f" ({detail})" if detail else "")


def _local_note(name, entry):
    """Why the card is not in use, for a setup that asked for it and got none.

    Three answers, because the thing to do about it differs: no card here, a
    build that could never use one, and a build Dikte downloaded that could
    never use one, which is the only case with a fix worth naming.
    """
    if not entry.get("gpu_wanted"):
        return ""
    if ggml.accel_kind({**entry, "running": True}) != "cpu":
        return ""
    if not ggml.cpu_only_build(entry):
        return "  - the graphics card is switched on and none was found"
    if entry.get("downloaded"):
        return (f"  - the graphics card is switched on and the downloaded build "
                f"has no GPU backend; a {name}-server on your system would be "
                f"used ahead of it")
    return "  - the graphics card is switched on and this build carries none"


def _local_line(name, entry):
    if not entry.get("running"):
        return "not loaded"
    model = entry.get("model") or ""
    return (f"loaded on {_local_where(entry)}"
            + (f", {model}" if model else "") + _local_note(name, entry))


def _last_local(conf):
    """What the local servers last ran on, read off the logs they left behind.

    For a command line asking while nothing is running: there is no process to
    put the question to, and the log outlives the process that wrote it. Every
    entry says `running` is false, because this is an account of the last start
    rather than a reading of a live one.
    """
    rows = {}
    for program, used, gpu, custom in (
            (ggml.WHISPER, conf["transcribe_provider"] == "local",
             bool(conf["local_gpu"]), conf["local_binary"]),
            (ggml.LLAMA, conf.uses_local_llm(), bool(conf["local_llm_gpu"]),
             conf["local_llm_binary"])):
        accel = ggml.last_accel(program)
        binary = ggml.program_path(program, custom)
        rows[program.name] = {
            # Which copy would run now, since the log does not say which one
            # wrote it; the advice only differs for Dikte's own download.
            "downloaded": ggml.is_downloaded(binary),
            # Whether one ever started here at all, which the backend cannot
            # say on its own: a server that ran and named no backend and one
            # that never ran both leave it empty.
            "ran": ggml.server_log(program).exists(),
            "running": False, "used": used, "gpu_wanted": gpu,
            "backend": accel.backend, "device": accel.device,
            "layers": accel.layers, "available": list(accel.available),
        }
    return rows


def cmd_status(opts):
    reply = ipc.send("status")
    if reply is None:
        conf = cfg.Config()
        out(opts, {"ok": True, "running": False,
                   "agent": assistant.display_name(conf)},
            "Dikte is not running.")
        return NOT_RUNNING
    if reply.get("legacy"):
        return fail(opts, "the running instance is from before it could answer "
                          "questions; reload it with: dikte restart", 1, running=True)
    lines = [
        f"dictation: {reply.get('dictation', '?')}"
        + ("  (paused)" if reply.get("paused") else ""),
        f"agent:     {reply.get('ask', '?')}  ({reply.get('agent', '?')})",
        f"meeting:   {reply.get('meeting', '?')}"
        + (f"  {reply['meeting_message']}" if reply.get("meeting_message") else ""),
        f"listener:  {'on' if reply.get('listener') else 'off'}",
    ]
    # Nothing for a setup that uses no model on this machine, and nothing at all
    # from an instance too old to have been asked.
    for name, entry in (reply.get("local") or {}).items():
        if entry.get("used") or entry.get("running"):
            lines.append(f"{name + ':':11}{_local_line(name, entry)}")
    return out(opts, reply, "\n".join(lines))


def cmd_doctor(opts):
    """What the settings window checks behind its buttons, in one pass."""
    conf = cfg.Config()
    # The two the clipboard and the key press go through come out of the table
    # rather than being spelled here, because they are not the same pair on all
    # four systems: X11 pastes with xclip where Wayland pastes with wl-copy, a
    # Mac shells out for one half and Windows for neither. A row saying ydotool
    # is missing on a machine that would never have run it is not a diagnosis,
    # it is a red mark to explain away.
    # Asked once and read twice: whether an instance is running, and what its
    # local servers are doing, which is a question only that process can answer.
    live = ipc.send("status") or {}
    here = paste.desktop()
    wanted = [here.clipboard, here.keyboard]
    if sys.platform.startswith("linux"):
        # Recording, the device list, and KDE's shortcut registry.
        wanted += ["pw-record", "pactl", "kwriteconfig6"]
    wanted += ["ffmpeg",
               assistant.executable(assistant.provider(conf)) or "claude",
               cleanup.executable(cleanup.provider(conf))]
    programs = {name: shutil.which(name) or "" for name in wanted if name}
    target = conf.transcribe_target()
    cleaner = cleanup.provider(conf)
    # What each provider actually needs: the local ones have no key to check,
    # and marking them by the key they do not use reported every fully local
    # setup as broken.
    transcribe_ready = conf.transcribe_ready()
    # Only the ones that answer over HTTP have a key worth looking at. A CLI
    # has a program to find instead, and the model on this machine has neither,
    # so "no key" there has to read as beside the point rather than as one that
    # has gone missing.
    cleanup_service, cleanup_key = {
        "openrouter": ("OpenRouter", conf.openrouter_key()),
        "gemini": ("Google AI Studio", conf.gemini_key()),
        "opencode": ("OpenCode Go", conf.opencode_key()),
    }.get(cleaner, ("", ""))
    if cleanup_service:
        cleanup_ready = bool(cleanup_key)
    elif cleaner == "local":
        cleanup_ready = conf.local_llm_ready()
    else:
        cleanup_ready = bool(programs.get(cleanup.executable(cleaner), ""))
    checks = {
        "programs": programs,
        "transcription": {"provider": target.provider, "model": target.model,
                          "key": bool(target.api_key),
                          "ready": transcribe_ready},
        "cleanup": {"enabled": conf["cleanup_enabled"], "provider": cleaner,
                    "model": cleanup.model(conf),
                    "key": bool(cleanup_key) if cleanup_service else None,
                    "ready": cleanup_ready},
        "agent": {"provider": assistant.provider(conf),
                  "directory": assistant.working_dir(conf)},
        "running": bool(live),
        # Live when there is an instance to ask, off the logs when there is not.
        "local": live.get("local") or _last_local(conf),
    }
    # An instance from before this field existed is not an instance saying
    # nothing is loaded; it is one that cannot be asked, and the two must not
    # print the same line.
    stale = bool(live) and "local" not in live
    if target.provider == "local":
        transcribe_line = (f"{'✓' if transcribe_ready else '✗'} {target.service}, "
                           f"transcribing on {target.model or 'no model yet'}")
    else:
        transcribe_line = (f"{'✓' if transcribe_ready else '✗'} {target.service} "
                           f"key, transcribing on {target.model}")
    if cleanup_service:
        cleanup_line = (f"{'✓' if cleanup_ready else '✗'} {cleanup_service} key, "
                        f"cleaning up on {cleanup.model(conf)}")
    elif cleaner == "local":
        cleanup_line = (f"{'✓' if cleanup_ready else '✗'} Local model, "
                        f"cleaning up on {conf['local_llm_model'] or 'no model yet'}")
    else:
        # Cleanup on a CLI needs no key, so what is checked is the program.
        cleanup_line = (f"{'✓' if cleanup_ready else '✗'} "
                        f"{cleanup.executable(cleaner)}, cleaning up on "
                        f"{cleanup.model(conf)}")
    lines = [f"{'✓' if path else '✗'} {name:14} {path or 'not on your PATH'}"
             for name, path in programs.items()]
    lines += [transcribe_line, cleanup_line]
    # Only the models this setup actually uses: a machine transcribing in the
    # cloud has nothing loaded here and no reason to read about it.
    for name, entry in checks["local"].items():
        if not entry.get("used"):
            continue
        if stale:
            lines.append(f"· {name:14} the running instance is too old to say; "
                         f"reload it with: dikte restart")
        elif entry.get("running"):
            lines.append(f"✓ {name:14} {_local_line(name, entry)}")
        elif live:
            lines.append(f"· {name:14} not loaded")
        elif entry.get("backend"):
            lines.append(f"· {name:14} last run on "
                         f"{_local_where(entry)}{_local_note(name, entry)}")
        elif entry.get("ran"):
            lines.append(f"· {name:14} last run said nothing about what it "
                         f"was running on")
        else:
            lines.append(f"· {name:14} never run here")
    lines.append(
        f"{'✓' if checks['running'] else '·'} application "
        + ("running" if checks["running"] else "not running"))
    return out(opts, {"ok": True, **checks}, "\n".join(lines))


# --- the command line -------------------------------------------------------

EPILOG = """\
examples:
  dikte record --seconds 8 --json
  dikte transcribe talk.mp4 --srt -o talk.srt
  dikte ask "put that in my calendar on Thursday at three"
  dikte config set cleanup_model google/gemini-3.5-flash
  dikte history show 1

Recording needs the application to be running, since that is what holds the
microphone; everything else works on its own. --json is accepted by every
command. Exit codes: 0 done, 1 failed, 2 wrong command line, 3 not running.
"""


def build_parser():
    # Two flags every command takes, before the verb or after it. The
    # subcommands get the copies that keep quiet when they are not given, so
    # that a flag typed before the verb survives; a plain default here would be
    # copied down and overwrite it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the answer as one JSON object")
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="keep progress lines off stderr")

    parser = argparse.ArgumentParser(
        prog="dikte",
        description="Voice dictation: record, transcribe, clean up, paste.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true",
                        help="print the answer as one JSON object")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="keep progress lines off stderr")
    parser.add_argument("--version", action="version", version=f"dikte {__version__}",
                        help="print the version and exit")
    parser.set_defaults(verb="", timeout=0, func=cmd_plain)
    subs = parser.add_subparsers(dest="verb", metavar="COMMAND")

    def leaf(group, name, help_text="", **kwargs):
        """A subcommand. Without a line of help it stays out of the listing,
        which is where the verbs kept for the old spelling belong."""
        if help_text:
            kwargs["help"] = help_text
        return group.add_parser(name, parents=[common], **kwargs)

    # --- dictation --------------------------------------------------------
    record = leaf(subs, "record", "record and print what was said")
    record.add_argument("--seconds", type=float, default=0,
                        help="stop on its own after this many seconds")
    record.add_argument("--paste", action="store_true",
                        help="paste into the focused window as well")
    record.add_argument("--no-wait", action="store_true",
                        help="start it and return, without the transcript")
    record.add_argument("--timeout", type=float, default=0,
                        help="give up waiting after this many seconds")
    record.set_defaults(func=cmd_record)

    for name, help_text in (("toggle", "start or stop recording"),
                            ("start", "start recording"),
                            ("stop", "stop recording and transcribe")):
        page = leaf(subs, name, help_text)
        page.add_argument("--wait", action="store_true",
                          help="wait for the run and print the transcript")
        page.add_argument("--paste", action="store_true",
                          help="paste into the focused window as well")
        page.add_argument("--timeout", type=float, default=0)
        page.set_defaults(func=cmd_toggle)

    # One verb for both halves, the way `toggle` is one verb: a key can only be
    # pressed, so a key that resumed nothing would need a second key.
    leaf(subs, "pause", "hold the recording, or take it up again"
         ).set_defaults(func=cmd_plain)
    leaf(subs, "cancel", "throw away the recording").set_defaults(func=cmd_cancel)

    # --- the agent --------------------------------------------------------
    ask = leaf(subs, "ask", "put a command to the agent")
    ask.add_argument("text", nargs="*", help="the command; read from stdin, or "
                                             "recorded when there is none")
    ask.add_argument("--provider", choices=assistant.PROVIDERS,
                     help="just for this run")
    ask.add_argument("--model", help="just for this run")
    ask.add_argument("--dir", help="working directory, just for this run")
    ask.add_argument("--new", action="store_true",
                     help="start a fresh conversation")
    ask.add_argument("--paste", action="store_true",
                     help="paste the answer into the focused window")
    ask.add_argument("--copy", action="store_true", help="put the answer on the clipboard")
    ask.add_argument("--wait", action="store_true",
                     help="when recording, wait for the answer")
    ask.add_argument("--timeout", type=float, default=0)
    ask.set_defaults(func=cmd_ask)

    session = leaf(subs, "session", "the agent's conversation")
    session.add_argument("session", nargs="?", default="status",
                         choices=("status", "reset"))
    session.set_defaults(func=cmd_session)

    for name in ("ask-cancel", "ask-reset"):
        leaf(subs, name).set_defaults(func=cmd_plain)

    # --- a file -----------------------------------------------------------
    transcribe = leaf(subs, "transcribe", "transcribe an audio or video file")
    transcribe.add_argument("file")
    transcribe.add_argument("-o", "--out", help="write here instead of stdout")
    transcribe.add_argument("--srt", action="store_true",
                            help="subtitles rather than plain text")
    transcribe.add_argument("--timestamps", action=argparse.BooleanOptionalAction,
                            default=None, help="[mm:ss] in front of every line")
    transcribe.add_argument("--cleanup", action=argparse.BooleanOptionalAction,
                            default=None, help="run the cleanup model over it")
    transcribe.set_defaults(func=cmd_transcribe)

    # --- meetings ---------------------------------------------------------
    page = leaf(subs, "meeting", "start a meeting, or end it and write it up")
    page.add_argument("--wait", action="store_true",
                      help="wait for the minutes to be written")
    page.add_argument("--timeout", type=float, default=0)
    page.set_defaults(func=cmd_meeting)
    # No --wait here: a cancel is answered on the spot, and a flag the server
    # would ignore is a promise the help text cannot keep.
    leaf(subs, "meeting-cancel", "").set_defaults(func=cmd_meeting)

    meetings = leaf(subs, "meetings", "recorded meetings and their minutes")
    inner = meetings.add_subparsers(dest="meetings", metavar="")
    meetings.set_defaults(func=_needs_subcommand(meetings))
    leaf(inner, "list", "every meeting, newest first").set_defaults(func=cmd_meetings_list)
    show = leaf(inner, "show", "the minutes of one meeting")
    show.add_argument("which", nargs="?", default="last",
                      help="its base, or 1 for the newest")
    show.add_argument("--transcript", action="store_true",
                      help="the transcript rather than the whole document")
    show.set_defaults(func=cmd_meetings_show)
    retry = leaf(inner, "retry", "write up a meeting that failed")
    retry.add_argument("which", nargs="?", default="last")
    retry.set_defaults(func=cmd_meetings_retry)
    delete = leaf(inner, "delete", "drop meetings and their files")
    delete.add_argument("which", nargs="+")
    delete.set_defaults(func=cmd_meetings_delete)
    for name, verb, help_text in (("start", "meeting-start", "start recording one"),
                                  ("stop", "meeting-stop", "end it and write it up")):
        page = leaf(inner, name, help_text)
        page.add_argument("--wait", action="store_true")
        page.add_argument("--timeout", type=float, default=0)
        page.set_defaults(func=cmd_meeting, verb=verb)
    # cancel takes no --wait: see the top-level meeting-cancel.
    leaf(inner, "cancel", "throw the recording away").set_defaults(
        func=cmd_meeting, verb="meeting-cancel")

    # --- history ----------------------------------------------------------
    history = leaf(subs, "history", "past dictations")
    inner = history.add_subparsers(dest="history", metavar="")
    history.set_defaults(func=_needs_subcommand(history))
    listing = leaf(inner, "list", "the last dictations, newest first")
    listing.add_argument("--limit", type=int, default=20, help="0 for all of them")
    listing.set_defaults(func=cmd_history_list)
    show = leaf(inner, "show", "one dictation in full")
    show.add_argument("which", nargs="?", default="last",
                      help="1 for the newest, counting back")
    show.add_argument("--raw", action="store_true",
                      help="the transcript before cleanup")
    show.set_defaults(func=cmd_history_show)
    delete = leaf(inner, "delete", "drop entries")
    delete.add_argument("which", nargs="+")
    delete.set_defaults(func=cmd_history_delete)
    clear = leaf(inner, "clear", "drop all of them")
    clear.add_argument("--yes", action="store_true")
    clear.set_defaults(func=cmd_history_clear)

    # --- settings ---------------------------------------------------------
    config = leaf(subs, "config", "every setting the window holds")
    inner = config.add_subparsers(dest="config", metavar="")
    config.set_defaults(func=_needs_subcommand(config))
    listing = leaf(inner, "list", "all of them")
    listing.add_argument("--reveal", action="store_true",
                         help="print the API keys in full")
    listing.set_defaults(func=cmd_config_list)
    getter = leaf(inner, "get", "one setting")
    getter.add_argument("key")
    getter.set_defaults(func=cmd_config_get)
    setter = leaf(inner, "set", "change one setting")
    setter.add_argument("key")
    setter.add_argument("value", nargs="?", help="omit it to read stdin")
    setter.set_defaults(func=cmd_config_set)
    resetter = leaf(inner, "reset", "back to the default")
    resetter.add_argument("key", nargs="*")
    resetter.add_argument("--all", action="store_true")
    resetter.set_defaults(func=cmd_config_reset)
    leaf(inner, "path", "where things are stored").set_defaults(func=cmd_config_path)

    prompt = leaf(subs, "prompt", "the prompt a run would really send")
    prompt.add_argument("which", nargs="?",
                        choices=("cleanup", "subtitles", "meeting", "agent"))
    prompt.set_defaults(func=cmd_prompt)

    # --- the machine ------------------------------------------------------
    leaf(subs, "devices", "microphones and monitors").set_defaults(func=cmd_devices)
    models = leaf(subs, "models", "model ids a provider offers")
    models.add_argument("--provider", choices=tuple(cfg.TRANSCRIBERS),
                        default="openrouter")
    models.add_argument("--transcription", action="store_true",
                        help="only the speech-to-text ones")
    models.set_defaults(func=cmd_models)
    test = leaf(subs, "test-key", "check the API keys")
    test.add_argument("which", nargs="?", default="all",
                      choices=("all", *cfg.TRANSCRIBERS, "gemini", "opencode"))
    test.set_defaults(func=cmd_test_key)
    leaf(subs, "doctor", "keys, programs, and what is missing").set_defaults(func=cmd_doctor)

    shortcut = leaf(subs, "shortcut", "the desktop's global shortcuts")
    inner = shortcut.add_subparsers(dest="shortcut", metavar="")
    shortcut.set_defaults(func=_needs_subcommand(shortcut))
    leaf(inner, "status", "what is registered").set_defaults(func=cmd_shortcut)
    install = leaf(inner, "install", "register one")
    install.add_argument("which", nargs="?", default="toggle",
                         choices=tuple(hotkey.SHORTCUTS))
    install.add_argument("--combo", help="e.g. Ctrl+Alt+Space")
    install.add_argument("--force", action="store_true",
                         help="install it even if something else uses it")
    install.set_defaults(func=cmd_shortcut)
    remove = leaf(inner, "remove", "unregister one")
    remove.add_argument("which", nargs="?", default="toggle",
                        choices=tuple(hotkey.SHORTCUTS))
    remove.set_defaults(func=cmd_shortcut)

    integrated = leaf(subs, "integrate",
                      "menu entry, start at sign-in and command, "
                      "for a downloaded build")
    integrated.add_argument("--remove", action="store_true",
                            help="take them away again")
    integrated.set_defaults(func=cmd_integrate)

    # --- the application --------------------------------------------------
    updates = leaf(subs, "update", "whether a newer Dikte has been released")
    updates.add_argument("--open", action="store_true",
                         help="open the release page in a browser")
    updates.set_defaults(func=cmd_update)

    leaf(subs, "status", "what it is doing right now").set_defaults(func=cmd_status)
    for name, help_text in (("settings", "open the settings window"),
                            ("restart", "reload the running instance"),
                            ("quit", "shut it down")):
        leaf(subs, name, help_text).set_defaults(func=cmd_plain)

    helper = leaf(subs, "help", "this text")
    helper.set_defaults(func=lambda _opts: parser.print_help() or 0)
    return parser


def _needs_subcommand(parser):
    def show(_opts):
        parser.print_help()
        return 2
    return show


def run(argv):
    global _app
    # A redirected stdout on Windows falls back to the console codepage,
    # strict, and a transcript (or doctor's ✓) with a character outside it
    # would then fail the run after the work succeeded. Interactively nothing
    # changes: the console is written through its own Unicode API.
    if sys.platform == "win32" and not os.environ.get("PYTHONIOENCODING"):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, OSError):
                pass
    parser = build_parser()
    opts = parser.parse_args(argv)
    # No verb at all is the plain `dikte`, which means the settings window.
    opts.verb = opts.verb or ""
    # Every path here either talks over the socket or drives one of the workers,
    # and both want an event loop under them; a window is what none of them want.
    _app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    try:
        return opts.func(opts)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
