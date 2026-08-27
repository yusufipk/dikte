"""Handing a dictation to an agent as a command, and pasting back its answer.

Three of them, because not everyone has the same one installed:

  Claude Code   `claude -p`, the session you would have opened yourself
  Codex         `codex exec`, the same idea from the other shop
  OpenRouter    a plain chat request, over the key that is already configured

The first two are the whole machine: they run commands, read files, and reach
whatever skills and services you have connected, which is what makes "put that
in my calendar on Thursday" a thing you can say. OpenRouter cannot touch any of
that, and is there so that a question still gets an answer on a machine with
neither CLI installed.

Whichever it is, the reply is pasted exactly where the transcript would have
been, and the conversation carries across dictations so that "and move that to
Friday" knows what "that" is.

The two CLIs are read as they stream rather than waited out. A command that
reaches for the calendar or the web takes long enough that a still indicator is
indistinguishable from a hang, so every tool they pick up is named in the corner
while they work.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from . import api
from . import config as cfg
from . import paths
from .i18n import t

SESSION_FILE = cfg.DATA_DIR / "assistant.json"
PROVIDERS = ("claude", "codex", "openrouter")

# How many messages of an OpenRouter conversation are carried forward. The two
# CLIs keep their own history and need no such number; here every turn is resent
# in full, so the window has to end somewhere.
MAX_HISTORY = 24

# What to say in the indicator for a tool, keyed by the name the CLI uses.
# Anything unlisted is named as it comes, which beats a generic "working" for
# tools arriving from an MCP server nobody wrote this table for.
CLAUDE_TOOLS = {
    "Bash": "Running a command…",
    "BashOutput": "Running a command…",
    "Read": "Reading…",
    "Glob": "Looking through files…",
    "Grep": "Searching the files…",
    "Edit": "Editing a file…",
    "Write": "Writing a file…",
    "NotebookEdit": "Editing a file…",
    "WebSearch": "Searching the web…",
    "WebFetch": "Reading a web page…",
    "Task": "Handing it to a subagent…",
    "TodoWrite": "Planning…",
}
CODEX_ITEMS = {
    "command_execution": "Running a command…",
    "reasoning": "Thinking…",
    "web_search": "Searching the web…",
    "file_change": "Editing a file…",
    "patch_apply": "Editing a file…",
    "todo_list": "Planning…",
}


# How hard to think, in each provider's own vocabulary. The setting is one
# scale, offered once, because "think harder" is one thing to want; what differs
# is only which rungs a provider has. A level it does not have lands on the
# nearest one it does rather than being dropped.
CLAUDE_EFFORT = {"none": "low", "minimal": "low", "low": "low",
                 "medium": "medium", "high": "high", "xhigh": "xhigh",
                 "max": "max"}
# "minimal" was Codex's bottom rung until the newer models replaced it with
# "none", and each of them rejects the other's word for it with a 400. "low" is
# the one every model has, so the two lowest rungs land there instead.
CODEX_EFFORT = {"none": "low", "minimal": "low", "low": "low",
                "medium": "medium", "high": "high", "xhigh": "high",
                "max": "high"}


class AssistantError(Exception):
    pass


class Cancelled(Exception):
    pass


def provider(conf):
    chosen = conf["assistant_provider"]
    return chosen if chosen in PROVIDERS else "claude"


def executable(name):
    """The CLI a provider runs, or "" when it needs none."""
    return {"claude": "claude", "codex": "codex"}.get(name, "")


def display_name(conf):
    """What to call the thing being asked, in the tray and in the corner."""
    return {"claude": "Claude", "codex": "Codex"}.get(provider(conf), "OpenRouter")


# --- the conversation -----------------------------------------------------
#
# One conversation is kept across dictations, so "and move that to tomorrow"
# means something. It is dropped once it has sat unused for long enough: an hour
# later the next command is almost certainly a new subject, and dragging the old
# one along costs tokens and invites an answer to the wrong question. Switching
# provider drops it too, since none of them can pick up another's thread.

def _read_session():
    """The stored conversation row, or {} however the file fails to read."""
    try:
        with open(SESSION_FILE, encoding="utf-8") as fh:
            row = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def _read_row(name, max_age_seconds):
    row = _read_session()
    if row.get("provider") != name:
        return {}
    if max_age_seconds and time.time() - row.get("ts", 0) > max_age_seconds:
        return {}
    return row


def read_session(name, max_age_seconds):
    """The id to resume, for the providers that keep their own history."""
    return str(_read_row(name, max_age_seconds).get("session", ""))


def read_messages(name, max_age_seconds):
    """The conversation so far, for the provider that does not."""
    messages = _read_row(name, max_age_seconds).get("messages")
    return messages if isinstance(messages, list) else []


def write_session(name, session="", messages=None):
    row = {"provider": name, "session": session, "ts": time.time()}
    if messages is not None:
        row["messages"] = messages[-MAX_HISTORY:]
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump(row, fh, ensure_ascii=False)
    except OSError:
        pass


def clear_session():
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def stored_provider():
    """Whose conversation is on disk, whatever the setting says now."""
    return str(_read_session().get("provider", ""))


def session_age():
    """Seconds since the stored conversation was last used, or None."""
    row = _read_session()
    if not (row.get("session") or row.get("messages")):
        return None
    return time.time() - row.get("ts", 0)


# --- the call -------------------------------------------------------------

def working_dir(conf):
    wanted = conf["assistant_dir"].strip()
    if wanted and os.path.isdir(os.path.expanduser(wanted)):
        return os.path.expanduser(wanted)
    return os.path.expanduser("~")


def ask(prompt, conf, on_stage=None, should_stop=None):
    """Run the prompt through the configured agent. Returns (answer, warning).

    `warning` is set when the answer arrived but something about the run should
    be seen anyway, a denied tool above all: the reply still reads like a normal
    one, and only the denial explains why it did not do what it was asked to.
    """
    name = provider(conf)
    if name == "openrouter":
        return _ask_openrouter(prompt, conf, on_stage)

    binary = executable(name)
    if not shutil.which(binary):
        raise AssistantError(t(
            "{binary} not found. Install it, or pick another provider under "
            "Settings → Agent.", binary=binary,
        ))

    run = _ask_claude if name == "claude" else _ask_codex
    session = read_session(name, conf["assistant_session_minutes"] * 60)
    try:
        return run(prompt, conf, session, on_stage, should_stop)
    except _SessionGone:
        # The conversation it pointed at is not there any more: the history was
        # cleared, or it was started somewhere else. Say nothing and start over,
        # because from the outside this is just the first command of the day.
        clear_session()
        return run(prompt, conf, "", on_stage, should_stop)


class _SessionGone(Exception):
    pass


# --- Claude Code ----------------------------------------------------------

def _ask_claude(prompt, conf, session, on_stage, should_stop):
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--model", conf["assistant_model"],
        "--permission-mode", conf["assistant_permission_mode"],
        "--append-system-prompt", conf.assistant_prompt(),
    ]
    effort = CLAUDE_EFFORT.get(conf["assistant_reasoning"], "")
    if effort:
        cmd += ["--effort", effort]
    if session:
        cmd += ["--resume", session]

    found = {"answer": "", "warning": "", "session": "", "failure": ""}

    def on_event(event):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            found["session"] = event.get("session_id") or found["session"]
        elif kind == "assistant" and on_stage:
            for block in event.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    on_stage(_claude_label(block))
        elif kind == "result":
            found["session"] = event.get("session_id") or found["session"]
            answer = (event.get("result") or "").strip()
            if event.get("is_error"):
                found["failure"] = answer or t("Claude ended with an error.")
            else:
                found["answer"] = answer
            found["warning"] = _denial_warning(event)

    code, stderr = _stream(cmd, conf, on_event, should_stop)
    return _conclude(found, code, stderr, session, "Claude")


def _claude_label(block):
    name = block.get("name", "")
    if name in CLAUDE_TOOLS:
        return t(CLAUDE_TOOLS[name])
    if name == "Skill":
        return t("Using {name}…", name=(block.get("input") or {}).get("skill") or "a skill")
    if name.startswith("mcp__"):
        parts = name.split("__")
        return t("Using {name}…", name=parts[1] if len(parts) > 1 else name)
    return t("Using {name}…", name=name or "a tool")


def _denial_warning(event):
    denials = event.get("permission_denials") or []
    names = []
    for denial in denials:
        name = denial.get("tool_name") if isinstance(denial, dict) else str(denial)
        if name and name not in names:
            names.append(name)
    return t("It was not allowed to use: {tools}", tools=", ".join(names)) if names else ""


# --- Codex ----------------------------------------------------------------

def _ask_codex(prompt, conf, session, on_stage, should_stop):
    # Codex takes no system prompt of its own, so the instruction rides along in
    # front of the command, kept apart from it so the two are not read as one.
    body = f"{conf.assistant_prompt()}\n\n---\n\n{prompt}"
    settings = [
        "-c", f'sandbox_mode="{conf["assistant_codex_sandbox"]}"',
        "-c", 'approval_policy="never"',   # there is nobody here to approve
        "--skip-git-repo-check",
        "--json",
    ]
    if conf["assistant_codex_model"].strip():
        settings += ["-m", conf["assistant_codex_model"].strip()]
    effort = CODEX_EFFORT.get(conf["assistant_reasoning"], "")
    if effort:
        settings += ["-c", f'model_reasoning_effort="{effort}"']

    cmd = (["codex", "exec", "resume", session] if session else ["codex", "exec"])
    cmd += settings + [body]

    found = {"answer": "", "warning": "", "session": "", "failure": ""}

    def on_event(event):
        kind = event.get("type")
        if kind == "thread.started":
            found["session"] = event.get("thread_id") or found["session"]
        elif kind in ("item.started", "item.completed"):
            item = event.get("item") or {}
            item_type = item.get("type")
            # Every message is kept rather than only the last: a run can say
            # something, use a tool and speak again, and the closing one is the
            # answer.
            if item_type == "agent_message" and kind == "item.completed":
                found["answer"] = (item.get("text") or "").strip() or found["answer"]
            elif on_stage and kind == "item.started":
                on_stage(_codex_label(item))
        elif kind in ("turn.failed", "error"):
            error = event.get("error") or {}
            found["failure"] = (error.get("message") if isinstance(error, dict)
                                else str(error)) or t("Codex ended with an error.")

    code, stderr = _stream(cmd, conf, on_event, should_stop)
    return _conclude(found, code, stderr, session, "Codex")


def _codex_label(item):
    item_type = item.get("type", "")
    if item_type in CODEX_ITEMS:
        return t(CODEX_ITEMS[item_type])
    if item_type == "mcp_tool_call":
        return t("Using {name}…", name=item.get("server") or item.get("tool") or "a tool")
    return t("Using {name}…", name=item_type or "a tool")


def codex_models():
    """The models Codex itself would offer right now, best first.

    `codex debug models` prints the catalog the CLI's own model picker reads,
    fetched from OpenAI and cached beside Codex's config, so the list is as
    current as the installed Codex and there is no second list to keep up to
    date here. Entries the picker hides are internal and stay hidden. A machine
    without Codex, or one too old to have the command, answers with nothing and
    the caller keeps its built-in list.
    """
    if not shutil.which("codex"):
        return []
    try:
        proc = subprocess.run(["codex", "debug", "models"],
                              capture_output=True, text=True, timeout=30)
        catalog = json.loads(proc.stdout or "null")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if not isinstance(catalog, dict):
        return []
    rows = [row for row in catalog.get("models") or []
            if isinstance(row, dict) and row.get("slug")
            and row.get("visibility") != "hide"]
    rows.sort(key=lambda row: row.get("priority") or 0)
    return [row["slug"] for row in rows]


# --- OpenRouter -----------------------------------------------------------

def _ask_openrouter(prompt, conf, on_stage):
    """No tools, no files, no calendar: a question and an answer.

    It is the fallback for a machine with neither CLI on it, so it says what it
    knows and nothing else. The conversation is ours to keep here, since there
    is no session on the other end to resume.
    """
    if on_stage:
        on_stage(t("Thinking…"))
    history = read_messages("openrouter", conf["assistant_session_minutes"] * 60)
    messages = history + [{"role": "user", "content": prompt}]
    try:
        answer = api.chat(
            messages, conf.openrouter_key(), conf["assistant_openrouter_model"],
            conf.assistant_prompt(), reasoning=conf["assistant_reasoning"],
            base_url=conf["openrouter_base_url"],
            timeout=conf["assistant_timeout"],
        )
    except api.ApiError as exc:
        raise AssistantError(str(exc)) from exc
    write_session("openrouter",
                  messages=messages + [{"role": "assistant", "content": answer}])
    return answer, ""


# --- running a CLI --------------------------------------------------------

def _stream(cmd, conf, on_event, should_stop):
    """Run cmd, hand every JSON line it prints to on_event.

    Returns (exit code, stderr). Raises Cancelled when the stop was asked for,
    and AssistantError when the clock ran out.
    """
    # stderr lands in a file rather than a pipe: nobody drains it while stdout
    # is being read, and a CLI chatty enough on stderr would fill the pipe's
    # buffer and wedge both of us. A file has no such limit, and is read once
    # at the end, which is the only moment stderr matters.
    stderr_file = tempfile.TemporaryFile()
    # On POSIX the run gets its own session, so that ending it can take down
    # every subprocess it started, not just the CLI itself.
    grouped = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = subprocess.Popen(
            cmd, cwd=working_dir(conf), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=stderr_file,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=paths.NO_WINDOW,
            **grouped,
        )
    except OSError as exc:
        stderr_file.close()
        raise AssistantError(t("Could not run {binary}: {error}",
                               binary=cmd[0], error=exc)) from exc

    # Reading the stream blocks between lines, and a model that thinks for a
    # minute sends none. So the clock and the stop button are watched from the
    # side, and they end the run by killing the process: that closes the stream
    # and the loop below falls out of its own accord.
    ended = {"cancelled": False, "timed_out": False}
    watchdog = threading.Thread(
        target=_watch,
        args=(proc, time.monotonic() + conf["assistant_timeout"], should_stop, ended),
        daemon=True,
    )
    watchdog.start()

    try:
        for line in proc.stdout:
            line = line.strip()
            # Both CLIs print the odd unstructured line among the JSON.
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(event, dict):
                on_event(event)
    finally:
        stderr = _finish(proc, stderr_file)
        watchdog.join(timeout=1)

    if ended["cancelled"]:
        raise Cancelled()
    if ended["timed_out"]:
        raise AssistantError(t("It did not finish within {seconds} seconds.",
                               seconds=conf["assistant_timeout"]))
    return proc.returncode, stderr


# Failures that a fresh session cannot cure: an exhausted quota, a signed-out
# CLI, a network that is down. A resumed run that dies with one of these is
# reported as what it is, not retried without the session, because the retry
# would fail the same way after making the user wait through a second run.
_API_TROUBLE = re.compile(
    r"(?i)rate.?limit|quota|overloaded|too many requests|credit|billing|"
    r"insufficient|unauthorized|forbidden|authentication|invalid.{0,8}key|"
    r"log ?in|logged.?out|network|connection|ECONN|ENOTFOUND|ETIMEDOUT|"
    r"\b(401|403|429|5\d\d)\b")


def _conclude(found, code, stderr, session, service):
    """Turn what the stream said into an answer, or into the reason there is none."""
    if code != 0 and not found["answer"]:
        # A resumed run that died with nothing to show is treated as the
        # session being gone, whatever the wording: this code used to look for
        # "session ... not found" in stderr, but a CLI update or another
        # language rewords that and the recovery stops working. Retrying costs
        # one clean start, and cannot loop because the retry resumes nothing.
        # Recognised API trouble is the exception: it is not the session's
        # fault, and the retry would only repeat it.
        blame = last_line(stderr) or found["failure"] or ""
        if session and not _API_TROUBLE.search(blame):
            raise _SessionGone()
        raise AssistantError(blame or t(
            "{service} exited with code {code}.", service=service, code=code))
    if found["failure"] and not found["answer"]:
        raise AssistantError(found["failure"])
    if not found["answer"]:
        raise AssistantError(t("{service} answered with nothing.", service=service))
    if found["session"]:
        write_session("claude" if service == "Claude" else "codex", found["session"])
    return found["answer"], found["warning"]


def _watch(proc, deadline, should_stop, ended):
    while proc.poll() is None:
        if should_stop is not None and should_stop():
            ended["cancelled"] = True
            break
        if time.monotonic() > deadline:
            ended["timed_out"] = True
            break
        time.sleep(0.25)
    if ended["cancelled"] or ended["timed_out"]:
        kill_tree(proc)


def kill_tree(proc):
    """End the process and everything it started.

    A CLI runs tools as subprocesses of its own, and ending only the CLI would
    leave those behind, still working on a question nobody is waiting for.
    Shared with cleanup, which runs the same two programs. Every failure here
    is swallowed: the process being already gone is the outcome being asked for.
    """
    if os.name == "nt":
        # There is no process group to signal on Windows; taskkill walks the
        # tree instead. The wait after it is best-effort, so a tree that will
        # not die does not hang the caller on top of everything else.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=paths.NO_WINDOW,
        )
        try:
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return
    # The Popen was started with start_new_session=True, so the pid names a
    # whole session to signal. SIGTERM first for a clean exit, SIGKILL for a
    # tree that ignored it.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _finish(proc, stderr_file):
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
    # Read back what the CLI wrote to its stderr file, decoded leniently: a
    # dying CLI is exactly the one likely to print something half-encoded.
    try:
        stderr_file.seek(0)
        stderr = stderr_file.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        stderr = ""
    for stream in (proc.stdout, stderr_file):
        try:
            stream.close()
        except OSError:
            pass
    return stderr


def last_line(text):
    """The line worth showing out of a CLI's stderr: the last one it wrote.

    Shared with cleanup, which runs the same two programs for a different job
    and fails the same way when they are unhappy.
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""
