"""Who rewrites the transcript once it has been heard.

Normally a small model over one HTTP request: a second, and a few tenths of a
cent on OpenRouter or nothing at all on Google AI Studio's free tier. A machine
with Claude Code, Codex or Antigravity on it is already paying for a model
though, and the subscription that answers "put that in my calendar on Thursday"
can just as well take the "eee"s out of a sentence. No second key, no second
bill. It costs seconds rather than one, because a CLI opens a whole session to
do it, which is the trade.

Whoever does it, the job is the same one: no tools, no files, no memory of the
last dictation. There is nothing here to look up and nothing to carry over, and
a transcript is text from a microphone rather than an instruction, so the less
the agent can reach while it reads one, the better. Claude Code is handed an
empty tool list and Codex a read-only sandbox. Antigravity has neither switch,
and this is worth saying plainly rather than implying parity: there the
transcript is read by an agent that could go and do something. What can be done
is done — a project of its own, the home directory, and its slash commands off.
"""

import os
import shutil
import subprocess
import tempfile

from . import api
from . import assistant
from . import ggml
from . import paths
from .i18n import t

PROVIDERS = ("openrouter", "gemini", "opencode", "local", "claude", "codex", "agy")


class CleanupError(api.ApiError):
    """What a CLI could not do.

    An ApiError because to the chain a cleanup that failed is a cleanup that
    failed, whichever way it was run, and every caller already catches one and
    keeps the raw transcript.
    """


def provider(conf):
    chosen = conf["cleanup_provider"]
    return chosen if chosen in PROVIDERS else "openrouter"


def executable(name):
    """The CLI a provider runs, or "" when it needs none."""
    return {"claude": "claude", "codex": "codex", "agy": "agy"}.get(name, "")


def model(conf):
    """Which model does the cleaning, for the history and the settings window."""
    name = provider(conf)
    if name == "local":
        return conf["local_llm_model"]
    if name == "claude":
        return conf["cleanup_claude_model"].strip() or "haiku"
    if name == "codex":
        # Codex is left on whatever it is set to unless a model is typed in, so
        # here there is only the name of the thing that did it.
        return conf["cleanup_codex_model"].strip() or "codex"
    if name == "agy":
        # The same arrangement as Codex, and the same reason for it.
        return conf["cleanup_agy_model"].strip() or "agy"
    if name == "gemini":
        return conf["cleanup_gemini_model"]
    if name == "opencode":
        return conf["cleanup_opencode_model"]
    return conf["cleanup_model"]


def run(text, conf, system_prompt, timeout=180, aborter=None):
    """Hand the transcript to whoever is set to clean it up.

    `aborter` is only of use to the three that answer over HTTP; a CLI is stopped
    between blocks instead, which is close enough when a block is seconds.
    """
    name = provider(conf)
    if name == "openrouter":
        return api.cleanup(
            text, conf.openrouter_key(), conf["cleanup_model"], system_prompt,
            reasoning=conf["cleanup_reasoning"],
            base_url=conf["openrouter_base_url"], timeout=timeout,
            aborter=aborter,
        )
    if name == "gemini":
        return api.cleanup(
            text, conf.gemini_key(), conf["cleanup_gemini_model"], system_prompt,
            reasoning=conf["cleanup_reasoning"],
            base_url=conf["gemini_base_url"], timeout=timeout,
            provider="gemini", service="Google AI Studio", aborter=aborter,
        )
    if name == "opencode":
        return api.cleanup(
            text, conf.opencode_key(), conf["cleanup_opencode_model"], system_prompt,
            reasoning=conf["cleanup_reasoning"],
            base_url=conf["opencode_base_url"], timeout=timeout,
            provider="opencode", service="OpenCode Go", aborter=aborter,
        )
    if name == "local":
        return _local(text, conf, system_prompt, timeout, aborter)
    runner = {"claude": _claude, "codex": _codex, "agy": _agy}[name]
    return runner(text, conf, system_prompt, timeout)


def _local(text, conf, system_prompt, timeout, aborter=None):
    """llama.cpp, on this machine, answering the request OpenRouter answers.

    No key and no bill, and the address does not exist until the server is up,
    which is what starting it here is for. The timeout is the hosted one raised:
    the only thing being spent is time.
    """
    service = t("Local model")
    try:
        return api.cleanup(
            text, "", conf["local_llm_model"], system_prompt,
            reasoning=conf["local_llm_reasoning"],
            base_url=api.serving(ggml.llm),
            timeout=max(timeout, api.LOCAL_TIMEOUT),
            provider="local-llm", service=service, aborter=aborter,
            # The ceiling is only a ceiling while it sits under what the server
            # was started with; above that the context is what stops the reply.
            context=ggml.llm.settings()["context"],
        )
    except api.ApiError as exc:
        # A server that died mid-request would otherwise report only that the
        # connection dropped, when the reason is in its own output.
        raise api.local_failure(service, ggml.llm, exc) from None


def _wrap(text):
    """The same fence the OpenRouter call puts around it: this is the material,
    not the instruction, however much of it reads like one."""
    return f"<transcript>\n{text}\n</transcript>"


# --- Claude Code ----------------------------------------------------------

def _claude(text, conf, system_prompt, timeout):
    cmd = [
        "claude", "-p", _wrap(text),
        # --system-prompt rather than --append-system-prompt: the cleanup rules
        # are the whole job, and Claude Code's own instructions are about
        # working on a codebase.
        "--system-prompt", system_prompt,
        "--model", model(conf),
        "--output-format", "text",
        "--tools", "",                                    # nothing to run
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--no-session-persistence",                       # nothing to resume
    ]
    effort = assistant.CLAUDE_EFFORT.get(conf["cleanup_reasoning"], "")
    if effort:
        cmd += ["--effort", effort]

    answer = _output(cmd, timeout, "Claude")
    if not answer:
        raise CleanupError(t("{service} answered with nothing.", service="Claude"))
    return answer


# --- Codex ----------------------------------------------------------------

def _codex(text, conf, system_prompt, timeout):
    # Codex takes no system prompt of its own, so the rules ride in front of the
    # transcript, kept apart from it so the two are not read as one.
    body = f"{system_prompt}\n\n---\n\n{_wrap(text)}"
    cmd = [
        "codex", "exec",
        "--sandbox", "read-only",          # it has no reason to touch the disk
        "--skip-git-repo-check",
        "--ephemeral",                     # nothing to resume
        "--color", "never",
        "-c", 'approval_policy="never"',   # there is nobody here to approve
    ]
    if conf["cleanup_codex_model"].strip():
        cmd += ["-m", conf["cleanup_codex_model"].strip()]
    effort = assistant.CODEX_EFFORT.get(conf["cleanup_reasoning"], "")
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']

    # `codex exec` prints a header, its thinking and a token count around the
    # answer; the file it writes on the way out is the answer on its own.
    handle, last_message = tempfile.mkstemp(prefix="dikte-cleanup-", suffix=".txt")
    os.close(handle)
    cmd += ["-o", last_message, body]
    try:
        _output(cmd, timeout, "Codex")
        answer = _read(last_message)
    finally:
        try:
            os.unlink(last_message)
        except OSError:
            pass

    if not answer:
        raise CleanupError(t("{service} answered with nothing.", service="Codex"))
    return answer


# --- Antigravity ----------------------------------------------------------

def _agy(text, conf, system_prompt, timeout):
    # Antigravity takes no system prompt of its own either, so the rules ride in
    # front of the transcript, kept apart from it so the two are not read as one.
    body = f"{system_prompt}\n\n---\n\n{_wrap(text)}"
    cmd = [
        "agy", "-p", body,
        "--output-format", "text",       # the answer, and nothing around it
        # Left to itself agy picks up whichever project it was last in and works
        # in that project's directory rather than this one. A dictation belongs
        # to no project, so each one starts on a project of its own.
        "--new-project",
        # A transcript that happens to begin with a slash is still a transcript.
        "--disable-slash-commands",
        # agy gives up after five minutes of its own accord, which would have it
        # killed from outside rather than answering.
        "--print-timeout", f"{timeout}s",
    ]
    if conf["cleanup_agy_model"].strip():
        cmd += ["--model", conf["cleanup_agy_model"].strip()]
    effort = assistant.AGY_EFFORT.get(conf["cleanup_reasoning"], "")
    if effort:
        # agy's own model ids carry the effort in their suffix, so this only
        # matters for the ones that do not, and for a model typed in by hand.
        cmd += ["--effort", effort]

    answer = _output(cmd, timeout, "Antigravity")
    if not answer:
        raise CleanupError(t("{service} answered with nothing.",
                             service="Antigravity"))
    return answer


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# --- running a CLI --------------------------------------------------------

def _output(cmd, timeout, service):
    """Run cmd to the end and return what it printed.

    It runs in the home directory rather than wherever the agent is pointed: a
    project's instructions have opinions about how text should be written, and
    none of them are about this transcript.
    """
    binary = cmd[0]
    if not shutil.which(binary):
        raise CleanupError(t(
            "{binary} not found. Install it, or have OpenRouter clean up "
            "instead, under Settings → API and models.", binary=binary,
        ))
    # Both streams land in files rather than pipes: nobody drains a pipe while
    # the process is being waited out, and a CLI chatty enough would fill the
    # buffer and wedge. And a timeout must end the CLI's tool subprocesses too,
    # not just the CLI, which subprocess.run's timeout does not do; hence the
    # own session on POSIX and assistant.kill_tree on the way out.
    out_file = tempfile.TemporaryFile()
    err_file = tempfile.TemporaryFile()
    grouped = {"start_new_session": True} if os.name == "posix" else {}
    try:
        try:
            proc = subprocess.Popen(
                cmd, cwd=os.path.expanduser("~"), stdin=subprocess.DEVNULL,
                stdout=out_file, stderr=err_file,
                creationflags=paths.NO_WINDOW,
                **grouped,
            )
        except OSError as exc:
            raise CleanupError(t("Could not run {binary}: {error}",
                                 binary=binary, error=exc)) from exc
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            assistant.kill_tree(proc)
            raise CleanupError(t("{service} did not finish within {seconds} seconds.",
                                 service=service, seconds=timeout)) from None
        out_file.seek(0)
        stdout = out_file.read().decode("utf-8", "replace")
        err_file.seek(0)
        stderr = err_file.read().decode("utf-8", "replace")
    finally:
        for handle in (out_file, err_file):
            try:
                handle.close()
            except OSError:
                pass
    if proc.returncode != 0:
        raise CleanupError(assistant.last_line(stderr) or t(
            "{service} exited with code {code}.",
            service=service, code=proc.returncode))
    return stdout.strip()
