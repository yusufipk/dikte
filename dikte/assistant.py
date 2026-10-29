"""Handing a dictation to an agent as a command, and pasting back its answer.

Six of them, because not everyone has the same one installed:

  Claude Code   `claude -p`, the session you would have opened yourself
  Codex         `codex exec`, the same idea from the other shop
  Antigravity   `agy -p`, Google's, with a browser of its own attached
  OpenRouter    a plain chat request, over the key that is already configured
  OpenCode Go   a plain chat request, over a subscription to open coding models
  Requesty      a plain chat request, the same as OpenRouter's

The first three are the whole machine: they run commands, read files, and reach
whatever skills and services you have connected, which is what makes "put that
in my calendar on Thursday" a thing you can say. The chat requests cannot
touch any of that, and are there so that a question still gets an answer on a
machine with no CLI installed at all.

What each of the three is allowed to do without asking is settled where that
program keeps its own permissions, not here. Dikte hands Claude Code the mode
chosen in Settings because it has a flag for one; Codex gets a sandbox for the
same reason; Antigravity has neither, and reads its own allow-rules instead.

Whichever it is, the reply is pasted exactly where the transcript would have
been, and the conversation carries across dictations so that "and move that to
Friday" knows what "that" is.

The three CLIs are read as they stream rather than waited out. A command that
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
PROVIDERS = ("claude", "codex", "agy", "openrouter", "opencode", "requesty")

# What each one is called where a person reads it: the tray, the corner of
# the screen, and the line an error is written in.
SERVICES = {"claude": "Claude", "codex": "Codex", "agy": "Antigravity",
            "openrouter": "OpenRouter", "opencode": "OpenCode Go",
            "requesty": "Requesty"}

# How many messages of a chat provider's conversation are carried forward. The
# two CLIs keep their own history and need no such number; here every turn is
# resent in full, so the window has to end somewhere.
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
# Antigravity carries a browser around with it, so the handful of names below
# stand in for the couple of dozen browser_* tools it can pick up; being told
# which mouse button moved is not what the corner of the screen is for.
AGY_TOOLS = {
    "run_command": "Running a command…",
    "command_status": "Running a command…",
    "send_command_input": "Running a command…",
    "view_file": "Reading…",
    "read_url_content": "Reading a web page…",
    "list_dir": "Looking through files…",
    "find_by_name": "Looking through files…",
    "grep_search": "Searching the files…",
    "search_web": "Searching the web…",
    "replace_file_content": "Editing a file…",
    "multi_replace_file_content": "Editing a file…",
    "sed_file": "Editing a file…",
    "notebook_edit": "Editing a file…",
    "write_to_file": "Writing a file…",
    "generate_image": "Drawing…",
    "manage_task": "Planning…",
    "invoke_subagent": "Handing it to a subagent…",
    "browser_subagent": "Handing it to a subagent…",
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
# agy has three rungs and no word for off, so the bottom of the ladder lands on
# "low" and the top two on "high". Shared with cleanup, which runs the same
# program for the smaller job.
AGY_EFFORT = {"none": "low", "minimal": "low", "low": "low",
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
    return {"claude": "claude", "codex": "codex", "agy": "agy"}.get(name, "")


def uses_tools(conf):
    """Whether the configured provider can run commands and reach the web.

    The three CLIs can; the chat requests answer from what the model knows.
    """
    return bool(executable(provider(conf)))


def model(conf):
    """Which model answered, for the history to record.

    Each provider keeps its own setting, and the one a CLI is left on has no id
    to report, only a name — the same arrangement cleanup.model() makes.
    """
    name = provider(conf)
    if name == "codex":
        return conf["assistant_codex_model"].strip() or "codex"
    if name == "agy":
        return conf["assistant_agy_model"].strip() or "agy"
    if name == "openrouter":
        return conf["assistant_openrouter_model"]
    if name == "opencode":
        return conf["assistant_opencode_model"]
    if name == "requesty":
        return conf["assistant_requesty_model"]
    return conf["assistant_model"]


def display_name(conf):
    """What to call the thing being asked, in the tray and in the corner."""
    return SERVICES.get(provider(conf), "OpenRouter")


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


# The providers a run can be sent to with no tools at all: Claude Code, which
# takes an empty tool list, and the chat requests, which never had any. Codex
# and Antigravity keep a shell, MCP servers or a browser whatever they are
# told for one run, so text the user did not write is never handed to them.
TOOL_FREE = ("claude", "openrouter", "opencode", "requesty")


def can_run_tool_free(conf):
    return provider(conf) in TOOL_FREE


def ask(prompt, conf, on_stage=None, should_stop=None, keep_session=True,
        tool_free=False, system=None):
    """Run the prompt through the configured agent. Returns (answer, warning).

    `warning` is set when the answer arrived but something about the run should
    be seen anyway, a denied tool above all: the reply still reads like a normal
    one, and only the denial explains why it did not do what it was asked to.

    `keep_session` False runs the prompt on its own: no conversation is resumed
    and none is written back. A question about selected text uses that, so the
    selection neither joins the spoken conversation nor lands in its file.
    The CLIs still keep their own transcripts wherever they keep them.

    `tool_free` is for a prompt carrying text the user did not write, so that
    instructions planted in it have nothing to act with. Claude Code then runs
    with an empty tool list, no MCP servers and a permission mode that Settings
    cannot widen; a provider outside TOOL_FREE is refused outright.

    `system` replaces the instruction from Settings for this one run, whose
    default is written for an answer that gets pasted.
    """
    name = provider(conf)
    if tool_free and name not in TOOL_FREE:
        raise AssistantError(t(
            "{name} cannot run without tools, so selected text is not sent to "
            "it. Use Claude Code or a chat provider.", name=SERVICES[name]))
    if name in ("openrouter", "opencode", "requesty"):
        return _ask_chat(name, SERVICES[name], prompt, conf, on_stage,
                         keep_session, system)

    binary = executable(name)
    if not shutil.which(binary):
        raise AssistantError(t(
            "{binary} not found. Install it, or pick another provider under "
            "Settings → Agent.", binary=binary,
        ))

    run = {"claude": _ask_claude, "codex": _ask_codex, "agy": _ask_agy}[name]
    if not keep_session or tool_free or system:
        extra = {"tool_free": True} if tool_free else {}
        return run(prompt, conf, "", on_stage, should_stop, keep=False,
                   system=system, **extra)
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

def _ask_claude(prompt, conf, session, on_stage, should_stop, keep=True,
                tool_free=False, system=None):
    # A prompt with selected text in it goes in on stdin, which `claude -p`
    # reads when no prompt is given, so that it stays out of the process list.
    cmd = [
        "claude", "-p", *([] if tool_free else [prompt]),
        "--output-format", "stream-json", "--verbose",
        "--model", conf["assistant_model"],
        # dontAsk rather than the configured mode, which may be
        # bypassPermissions: with no tools there is nothing to permit anyway.
        "--permission-mode",
        "dontAsk" if tool_free else conf["assistant_permission_mode"],
        "--append-system-prompt", system or conf.assistant_prompt(),
    ]
    if tool_free:
        # An empty list removes every built-in tool; --strict-mcp-config with
        # no --mcp-config leaves no MCP server either. Nothing is saved to
        # Claude's own session files.
        cmd += ["--tools", "", "--strict-mcp-config", "--no-session-persistence"]
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

    if tool_free:
        code, stderr = _stream(cmd, conf, on_event, should_stop,
                               stdin_text=prompt)
    else:
        code, stderr = _stream(cmd, conf, on_event, should_stop)
    return _conclude(found, code, stderr, session, "claude", keep)


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

def _ask_codex(prompt, conf, session, on_stage, should_stop, keep=True,
               system=None):
    # Codex takes no system prompt of its own, so the instruction rides along in
    # front of the command, kept apart from it so the two are not read as one.
    body = f"{system or conf.assistant_prompt()}\n\n---\n\n{prompt}"
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
    return _conclude(found, code, stderr, session, "codex", keep)


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


# --- Antigravity ----------------------------------------------------------

def _ask_agy(prompt, conf, session, on_stage, should_stop, keep=True,
             system=None):
    # Antigravity takes no system prompt of its own either, so the instruction
    # rides in front of the command, kept apart from it so the two are not read
    # as one.
    body = f"{system or conf.assistant_prompt()}\n\n---\n\n{prompt}"
    cmd = [
        "agy", "-p", body,
        "--output-format", "stream-json",
        # agy stops after five minutes unless it is told otherwise, which is
        # shorter than the timeout this setting offers.
        "--print-timeout", f"{conf['assistant_timeout']}s",
    ]
    # One or the other, always: left with neither, agy picks up whichever
    # project it was last in and works in that project's directory rather than
    # the one _stream is about to start it in.
    cmd += ["--conversation", session] if session else ["--new-project"]
    if conf["assistant_agy_model"].strip():
        cmd += ["--model", conf["assistant_agy_model"].strip()]
    effort = AGY_EFFORT.get(conf["assistant_reasoning"], "")
    if effort:
        # Most of agy's own model ids carry the effort in their suffix already;
        # this is for the ones that do not.
        cmd += ["--effort", effort]

    found = {"answer": "", "warning": "", "session": "", "failure": ""}

    def on_event(event):
        kind = event.get("event")
        if kind == "init":
            found["session"] = event.get("conversation_id") or found["session"]
        elif kind == "step_update":
            step = event.get("step_update") or {}
            # A tool is reported twice, once when it starts and once when it is
            # done; the corner wants the first of those.
            if (on_stage and step.get("step_type") == "tool"
                    and step.get("state") == "ACTIVE"):
                on_stage(_agy_label(step))
        elif kind == "result":
            result = event.get("result") or {}
            found["session"] = result.get("conversation_id") or found["session"]
            answer = (result.get("response") or "").strip()
            if result.get("status") == "SUCCESS":
                found["answer"] = answer
            else:
                found["failure"] = answer or t("{service} ended with an error.",
                                               service="Antigravity")

    code, stderr = _stream(cmd, conf, on_event, should_stop)
    return _conclude(found, code, stderr, session, "agy", keep)


def _agy_label(step):
    name = step.get("tool_name", "")
    if name in AGY_TOOLS:
        return t(AGY_TOOLS[name])
    if name.startswith("browser_") or name.startswith("capture_browser"):
        return t("Working in the browser…")
    if name == "call_mcp_tool":
        server = (step.get("tool_info") or {}).get("parameters") or {}
        return t("Using {name}…", name=server.get("server") or "a tool")
    return t("Using {name}…", name=name or "a tool")


def agy_models():
    """The models Antigravity itself would offer right now, in its own order.

    `agy models` prints one `id<TAB>display name` line per model, so the list
    is as current as the account behind the CLI. Unlike Codex it asks Google
    rather than a cache on disk, a couple of seconds the caller spends off the
    interface thread. A machine without agy, or a call that fails, answers
    with nothing and the caller keeps its built-in list.
    """
    if not shutil.which("agy"):
        return []
    try:
        proc = subprocess.run(["agy", "models"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    ids = []
    for line in (proc.stdout or "").splitlines():
        model_id, tab, _ = line.partition("\t")
        if tab and model_id.strip():
            ids.append(model_id.strip())
    return ids


# --- OpenRouter, OpenCode Go and Requesty ---------------------------------

def _ask_chat(name, service, prompt, conf, on_stage, keep=True, system=None):
    """A plain question and answer, over a chat provider's key.

    No tools, no files, no calendar. It is the fallback for a machine with
    neither CLI on it, so it says what it knows and nothing else. The
    conversation is ours to keep here, since there is no session on the other
    end to resume.
    """
    if on_stage:
        on_stage(t("Thinking…"))
    history = (read_messages(name, conf["assistant_session_minutes"] * 60)
               if keep else [])
    messages = history + [{"role": "user", "content": prompt}]
    # Each of them keeps its model, address and key under its own name.
    model = conf[f"assistant_{name}_model"]
    base_url = conf[f"{name}_base_url"]
    key = conf.api_key(f"{name}_api_key")
    try:
        answer = api.chat(
            messages, key, model, system or conf.assistant_prompt(),
            reasoning=conf["assistant_reasoning"], base_url=base_url,
            timeout=conf["assistant_timeout"], provider=name, service=service,
        )
    except api.ApiError as exc:
        raise AssistantError(str(exc)) from exc
    if keep:
        write_session(name, messages=messages
                      + [{"role": "assistant", "content": answer}])
    return answer, ""


# --- running a CLI --------------------------------------------------------

def _stream(cmd, conf, on_event, should_stop, stdin_text=None):
    """Run cmd, hand every JSON line it prints to on_event.

    Returns (exit code, stderr). Raises Cancelled when the stop was asked for,
    and AssistantError when the clock ran out. `stdin_text`, when given, is
    written to the process's stdin, which is otherwise closed.
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
            cmd, cwd=working_dir(conf),
            stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
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
    if stdin_text is not None:
        # From the side, so a pipe smaller than the prompt cannot stall the
        # read below while the CLI waits for the rest of its input.
        threading.Thread(target=_feed, args=(proc.stdin, stdin_text),
                         daemon=True).start()

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


def _conclude(found, code, stderr, session, name, keep=True):
    """Turn what the stream said into an answer, or into the reason there is none."""
    service = SERVICES.get(name, name)
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
    if found["session"] and keep:
        write_session(name, found["session"])
    return found["answer"], found["warning"]


def _feed(pipe, text):
    try:
        pipe.write(text)
    except (OSError, ValueError):
        pass   # the process is gone; the stream says why
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


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
