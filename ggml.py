"""Speech to text and cleanup on this machine: whisper.cpp and llama.cpp.

Two programs, one treatment. Fetch a release from GitHub, unpack it under the
data directory, fetch a model from Hugging Face, then keep one server alive on a
port of its own. Both of them speak the shape api.py already sends to the hosted
providers, so what the rest of Dikte sees is a base URL and nothing else:
whisper-server is started on `--inference-path /v1/audio/transcriptions`, the
exact path api.py builds, and llama-server answers /v1/chat/completions the way
OpenRouter does.

A server rather than a one-shot run, because the model is the slow part. Loading
a large whisper model takes a second or two while transcribing a few seconds of
speech takes a fraction of one, and an LLM is worse: a server pays that once and
a run per dictation pays it every time.

Nothing downloaded is trusted for having arrived. Every file is checked against
the sha256 its index published, and the bytes go to a `.part` that is only
renamed once the whole thing is there, so an interrupted download can never be
mistaken for a working one.

This module imports hub and the string table, and nothing else of Dikte's: it
knows how to fetch a file and how to run a process, and nothing about dictation.
Its errors leave as LocalError and api.py turns them into the ApiError the
interface already knows how to show.
"""

import atexit
import collections
import ctypes.util
import hashlib
import http.client
import json
import os
import pathlib
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

import hub
import paths
from i18n import t

HOST = "127.0.0.1"
# The path api.py asks for, so its URL and the server's line up.
INFERENCE_PATH = "/v1/audio/transcriptions"

# Not worked out here: a Mac keeps its data under ~/Library, and a copy of the
# rule that did not know that put several gigabytes of models somewhere no Mac
# user looks and uninstall.sh never deleted from.
DATA_DIR = paths.DATA_DIR
BIN_DIR = DATA_DIR / "bin"
MODELS_DIR = DATA_DIR / "models"

# Loading a large model onto a GPU is the slow part of a start, and on a cold
# page cache a large LLM read from a spinning disk is slower still.
STARTUP_TIMEOUT = 180.0
DOWNLOAD_CHUNK = 1 << 20

# `health` is the path that answers only once the model is in memory. whisper
# does not have one and does not need one: it binds its port after the model is
# loaded, so the port opening is the signal.
Program = collections.namedtuple("Program", "name repo binary health")

WHISPER = Program("whisper", "ggml-org/whisper.cpp", "whisper-server", "")
LLAMA = Program("llama", "ggml-org/llama.cpp", "llama-server", "/health")

# Where the models are listed. Neither list is written into Dikte: a catalogue
# in the source means a release of Dikte for every model somebody else
# publishes.
WHISPER_MODELS_REPO = "ggerganov/whisper.cpp"
LLM_AUTHOR = "ggml-org"

# What the whisper repository holds besides models: Core ML encoders for Apple
# hardware and the odd loose file.
WHISPER_PREFIX = "ggml-"
WHISPER_SUFFIX = ".bin"

# What a GGUF repository holds besides the model: mmproj is the vision half of a
# multimodal model, mtp a draft head for speculative decoding. Neither is a model
# a server can be started on, and offering them is offering a failure.
GGUF_SKIP = ("mmproj", "mtp-")
# Big enough for a 12B at Q4 and far past anything cleanup wants; the point is
# to keep a 400 GB frontier model out of a list somebody might click.
GGUF_MAX_BYTES = 16 << 30

# Suggestions, not a catalogue: the list itself is fetched, and these are only
# the rows that float to the top of it. Small instruction-following models,
# because cleanup is punctuation and filler words rather than anything that
# wants thinking about.
SUGGESTED_LLM = (
    "ggml-org/gemma-3-4b-it-GGUF",
    "ggml-org/gemma-4-E2B-it-GGUF",
    "ggml-org/gemma-4-E4B-it-GGUF",
    "ggml-org/SmolLM3-3B-GGUF",
)
# Turbo at q5_0 is smaller than `small` and better than it, which makes the
# usual "start small" advice point at the same file as "start good".
SUGGESTED_WHISPER = "ggml-large-v3-turbo-q5_0.bin"


class LocalError(Exception):
    pass


def human_size(count):
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024.0
    return f"{count:.1f} GB"


# --- fetching -------------------------------------------------------------


def download(item, target, on_progress=None, should_stop=None, require_hash=True):
    """Fetch one hub.Item to `target`. True when it landed, False when stopped.

    The bytes go to a `.part` that is renamed only after both the length and the
    hash agree with what the index said. A truncated file would otherwise sit
    there looking installed and fail much later, inside a server, as a corrupt
    model; a file that is the right length but the wrong content is worse, and
    this is a program as often as it is a model.

    A file whose index published no hash is refused rather than taken on trust.
    Everything fetched here is either run or parsed by something written in C++,
    and GitHub did not always publish a digest: a release old enough to predate
    that would otherwise install unchecked, which is the one case where this
    would matter most and say least.
    """
    target = pathlib.Path(target)
    if require_hash and not item.sha256:
        raise LocalError(t("{name} is published without a checksum, so there is "
                           "no way to tell what arrived. Nothing was installed.",
                           name=item.name))
    part = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LocalError(t("Could not create {path}: {error}",
                           path=target.parent, error=exc)) from exc

    request = urllib.request.Request(item.url, headers={"User-Agent": hub.USER_AGENT})
    digest = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or item.size or 0)
            with open(part, "wb") as out:
                while True:
                    if should_stop is not None and should_stop():
                        part.unlink(missing_ok=True)
                        return False
                    block = response.read(DOWNLOAD_CHUNK)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    # More than was announced: a body that does not end is the
                    # one way this loop could run until the disk is full.
                    if total and done > total:
                        part.unlink(missing_ok=True)
                        raise LocalError(t("{name} is longer than it said it "
                                           "would be.", name=item.name))
                    if on_progress is not None:
                        on_progress(done, total)
        # A proxy notice or an error page that came back as 200 would otherwise
        # be renamed into place and only fail when something tries to read it.
        if total and done != total:
            part.unlink(missing_ok=True)
            raise LocalError(t("The download stopped early ({done} of {total}).",
                               done=human_size(done), total=human_size(total)))
        if item.sha256 and digest.hexdigest() != item.sha256:
            part.unlink(missing_ok=True)
            raise LocalError(t("{name} does not match its published checksum. "
                               "Nothing was installed.", name=item.name))
        part.replace(target)
        return True
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        exc.close()   # it holds the response body open until it is collected
        raise LocalError(t("Could not download {name}: HTTP {code}",
                           name=item.name, code=exc.code)) from exc
    except urllib.error.URLError as exc:
        part.unlink(missing_ok=True)
        raise LocalError(t("Could not download {name}: {error}",
                           name=item.name, error=exc.reason)) from exc
    except OSError as exc:
        # A connection cut mid-body arrives here too, and gigabytes in is
        # exactly where that happens.
        part.unlink(missing_ok=True)
        raise LocalError(t("Could not write {name}: {error}",
                           name=item.name, error=exc)) from exc


# --- the programs ---------------------------------------------------------


def _arch():
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return "x64"


def _has_vulkan():
    """Whether a Vulkan loader is installed, which decides which build to fetch.

    llama.cpp publishes no CUDA build for Linux, so Vulkan is what a graphics
    card gets here. The build without it is smaller and runs on the CPU, and
    fetching the Vulkan one for a machine that cannot load it would only make
    the download bigger.
    """
    return bool(ctypes.util.find_library("vulkan"))


def _wanted_assets(program):
    """Asset name endings to accept, best first.

    llama.cpp publishes native Metal-enabled macOS archives. whisper.cpp does
    not publish a runnable macOS server archive, so an arm64 Mac must not
    mistake Ubuntu's arm64 archive for a native build.
    """
    arch = _arch()
    if sys.platform == "darwin":
        return () if program is WHISPER else (f"bin-macos-{arch}.tar.gz",)
    if program is LLAMA and _has_vulkan():
        return (f"bin-ubuntu-vulkan-{arch}.tar.gz", f"bin-ubuntu-{arch}.tar.gz")
    return (f"bin-ubuntu-{arch}.tar.gz",)


def _install_record(program):
    return BIN_DIR / program.name / "installed.json"


def installed_program(program):
    """The binary Dikte downloaded, or "" when there is none that still runs."""
    try:
        record = json.loads(_install_record(program).read_text(encoding="utf-8"))
        path = record.get("binary") or ""
    except (OSError, ValueError):
        return ""
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""


def installed_version(program):
    try:
        record = json.loads(_install_record(program).read_text(encoding="utf-8"))
        return record.get("tag") or ""
    except (OSError, ValueError):
        return ""


def program_path(program, custom=""):
    """Which copy of the program to run, or "" when there is none.

    A system one wins over a downloaded one. The distribution package is built
    against whatever the machine has, which on this platform means it may reach
    the graphics card, while the release binaries carry CPU backends only.
    """
    custom = (custom or "").strip()
    if custom:
        return custom if os.path.isfile(custom) and os.access(custom, os.X_OK) else ""
    return shutil.which(program.binary) or installed_program(program)


def system_program(program):
    """Whether the program came from the system rather than from Dikte."""
    return bool(shutil.which(program.binary))


def _find_binary(root, name):
    for path in sorted(pathlib.Path(root).rglob(name)):
        if path.is_file():
            return path
    return None


def _extract(archive, into):
    """Unpack a release tarball, refusing anything that reaches outside `into`.

    The archives lay their libraries next to their binaries and are linked with
    an $ORIGIN runpath, so a whole directory is what has to survive the trip and
    the binary cannot be lifted out of it.
    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                tar.extractall(into, filter="data")
            except TypeError:      # Python without the extraction filters
                tar.extractall(into)
    except (tarfile.TarError, OSError) as exc:
        raise LocalError(t("Could not unpack {name}: {error}",
                           name=os.path.basename(str(archive)), error=exc)) from exc


def install_program(program, tag="", on_progress=None, should_stop=None,
                    refresh=False):
    """Fetch and unpack a release. The path to the binary, or "" when stopped.

    `tag` is empty for whatever the project released last, which is the point:
    a version pinned in Dikte's source would mean a release of Dikte every time
    whisper.cpp has one.
    """
    try:
        tag, assets = hub.release(program.repo, tag or "latest", refresh=refresh)
    except hub.HubError as exc:
        raise LocalError(str(exc)) from exc

    item = None
    for ending in _wanted_assets(program):
        item = next((a for a in assets if a.name.endswith(ending)), None)
        if item:
            break
    if item is None:
        # Nothing to download and nothing to install for you: whisper.cpp
        # publishes no macOS binary, and Homebrew's whisper-cpp is configured
        # with WHISPER_BUILD_SERVER=OFF, so it is whisper-cli that lands and not
        # the server Dikte talks to. Building it is a cmake line, and the
        # binary is picked up from the PATH or from the box above, the same way
        # a distribution's own build is on Linux.
        if sys.platform == "darwin" and program is WHISPER:
            raise LocalError(t(
                "whisper.cpp has no macOS build, and Homebrew's leaves out the "
                "server. Build whisper-server yourself and give its path here, "
                "or transcribe in the cloud. See the README."
            ))
        raise LocalError(t("{repo} {tag} has no build for this machine.",
                           repo=program.repo, tag=tag))

    into = BIN_DIR / program.name / tag
    shutil.rmtree(into, ignore_errors=True)
    archive = BIN_DIR / program.name / item.name
    try:
        if not download(item, archive, on_progress, should_stop):
            return ""
        _extract(archive, into)
        binary = _find_binary(into, program.binary)
        if binary is None:
            raise LocalError(t("{name} was not in the download.",
                               name=program.binary))
        binary.chmod(binary.stat().st_mode | 0o111)
        _install_record(program).write_text(
            json.dumps({"tag": tag, "binary": str(binary)}), encoding="utf-8")
    except OSError as exc:
        raise LocalError(t("Could not install {name}: {error}",
                           name=program.name, error=exc)) from exc
    finally:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
    _drop_old_versions(program, keep=tag)
    return str(binary)


def _drop_old_versions(program, keep):
    """Leave one unpacked release behind, not one per update."""
    root = BIN_DIR / program.name
    try:
        for path in root.iterdir():
            if path.is_dir() and path.name != keep:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# --- the models -----------------------------------------------------------


def whisper_models(refresh=False):
    """[hub.Item] for every whisper model on offer, smallest first."""
    try:
        files = hub.files(WHISPER_MODELS_REPO, refresh=refresh)
    except hub.HubError as exc:
        raise LocalError(str(exc)) from exc
    models = [f for f in files
              if f.name.startswith(WHISPER_PREFIX) and f.name.endswith(WHISPER_SUFFIX)
              and f.size > 0]
    return sorted(models, key=lambda f: f.size)


def llm_repos(refresh=False):
    """Repository ids for the GGUF models on offer, suggestions first."""
    try:
        found = [r.id for r in hub.repos(author=LLM_AUTHOR, refresh=refresh)]
    except hub.HubError:
        # A menu rather than a catalogue: with nothing to show, the suggestions
        # are still worth showing, and whatever is wrong with the network will
        # say so where it matters, when a download is asked for.
        found = []
    if not found:
        return list(SUGGESTED_LLM)
    first = [r for r in SUGGESTED_LLM if r in found]
    return first + [r for r in found if r not in first]


def llm_quants(repo, refresh=False):
    """[hub.Item] for the model files in one GGUF repository, smallest first."""
    try:
        files = hub.files(repo, refresh=refresh)
    except hub.HubError as exc:
        raise LocalError(str(exc)) from exc
    out = []
    for item in files:
        name = item.name.rsplit("/", 1)[-1]
        if not name.endswith(".gguf") or name.startswith(GGUF_SKIP):
            continue
        # A model split across files needs all of them and a different command
        # line; anything cleanup wants fits in one.
        if "-of-000" in name or not 0 < item.size <= GGUF_MAX_BYTES:
            continue
        out.append(item)
    return sorted(out, key=lambda f: f.size)


def whisper_model_path(name):
    return MODELS_DIR / "whisper" / name


def llm_model_path(name):
    return MODELS_DIR / "llm" / name.rsplit("/", 1)[-1]


def have_model(path):
    path = pathlib.Path(path)
    return path.is_file() and path.stat().st_size > 0


def installed_whisper_models():
    return sorted(p.name for p in (MODELS_DIR / "whisper").glob("*.bin"))


def installed_llm_models():
    return sorted(p.name for p in (MODELS_DIR / "llm").glob("*.gguf"))


def delete_model(path):
    try:
        pathlib.Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LocalError(t("Could not delete the model: {error}", error=exc)) from exc


# --- one server -----------------------------------------------------------


def _free_port():
    """A port nothing is listening on, handed straight to the server.

    Between closing this socket and the server binding it, something else could
    take it; that is why a start retries rather than trusting the number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def _listening(port):
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


def _healthy(port, path):
    """Whether the model is in memory, for a server that says so.

    Spoken over http.client rather than urllib because this never leaves the
    machine: it is the same question as _listening, one layer up.
    """
    connection = http.client.HTTPConnection(HOST, port, timeout=2)
    try:
        connection.request("GET", path)
        # 503 for as long as the model is still being read in.
        return connection.getresponse().status == 200
    except (http.client.HTTPException, OSError):
        return False
    finally:
        connection.close()


def _tail(path, lines=3):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            found = [line.strip() for line in fh if line.strip()]
    except OSError:
        return ""
    return " | ".join(found[-lines:])


class Server:
    """One process, started when something needs it and stopped when nothing does.

    `build` turns the settings into a command line; everything else about
    running a server is the same for both programs.
    """

    def __init__(self, program, build, defaults):
        self.program = program
        self._build = build
        self._settings = dict(defaults)
        # Two locks on purpose. `_lock` is held for the length of a dictionary
        # lookup, so the interface can ask what is running while a model is
        # being loaded; `_starting` is held across the start itself, which can
        # take a minute and which two threads must not both do.
        self._lock = threading.Lock()
        self._starting = threading.Lock()
        self._proc = None
        self._port = 0
        self._log = ""
        self._key = None

    # ---- settings --------------------------------------------------------

    def configure(self, **changes):
        """Apply settings. A server started on the old ones is stopped."""
        with self._lock:
            for key, value in changes.items():
                if value is not None and key in self._settings:
                    self._settings[key] = value
            stale = self._proc is not None and self._key != self._settings_key()
        if stale:
            self.stop()

    def settings(self):
        with self._lock:
            return dict(self._settings)

    def _settings_key(self):
        """What a running server would have to be restarted for."""
        return json.dumps(self._settings, sort_keys=True, default=str)

    # ---- process ---------------------------------------------------------

    @property
    def running(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def base_url(self):
        with self._lock:
            return f"http://{HOST}:{self._port}/v1" if self._port else ""

    def error(self):
        """The last thing the server printed, for a failure after it started."""
        with self._lock:
            log = self._log
        return _tail(log) if log else ""

    def serve(self):
        """The base URL of a server that is up and running the current settings."""
        ready = self._current_url()
        if ready:
            return ready
        with self._starting:
            # Somebody may have started it while this thread waited its turn.
            ready = self._current_url()
            if ready:
                return ready
            self.stop()
            with self._lock:
                settings, key = dict(self._settings), self._settings_key()
            proc, port, log = self._launch(settings)
            with self._lock:
                self._proc, self._port, self._log, self._key = proc, port, log, key
            return self.base_url()

    def _current_url(self):
        with self._lock:
            up = self._proc is not None and self._proc.poll() is None
            return (f"http://{HOST}:{self._port}/v1"
                    if up and self._key == self._settings_key() else "")

    def _launch(self, settings):
        args = self._build(settings)        # raises LocalError when unusable
        last = ""
        for _ in range(3):
            port = _free_port()
            log = DATA_DIR / f"{self.program.name}-server.log"
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                sink = open(log, "wb")
            except OSError as exc:
                raise LocalError(t("Could not start {name}: {error}",
                                   name=self.program.name, error=exc)) from exc
            try:
                with sink:
                    proc = subprocess.Popen(
                        args + ["--host", HOST, "--port", str(port)],
                        stdout=sink, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                    )
            except OSError as exc:
                raise LocalError(t("Could not start {name}: {error}",
                                   name=self.program.name, error=exc)) from exc

            # Written before it is ready rather than after, so that a kill
            # during the model load leaves something for the sweep to find.
            self._remember(proc.pid)
            try:
                ready = self._wait_ready(proc, port)
            except BaseException:
                # Whatever went wrong while waiting, the process is ours and
                # nothing else is left holding a reference to it. Leaving it
                # running would leak a loaded model with nobody to ask it
                # anything, which is the whole failure this class is careful
                # about elsewhere.
                self._kill(proc)
                self._forget()
                raise
            if ready:
                return proc, port, str(log)
            last = _tail(log)
            self._forget()
            # A port taken between the probe and the bind is the one failure
            # worth another go; anything else will fail the same way again.
            if "address" not in last.lower() and "bind" not in last.lower():
                break
        raise LocalError(t("{name} did not start: {error}",
                           name=self.program.binary, error=last or t("no output")))

    def _wait_ready(self, proc, port):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            if _listening(port):
                # whisper binds after the model is loaded, so the open port is
                # the answer. llama binds first and answers /health with 503
                # until it is ready.
                if not self.program.health or _healthy(port, self.program.health):
                    return True
            time.sleep(0.1)
        self._kill(proc)
        return False

    @staticmethod
    def _kill(proc, gently=False):
        """Stop a process of ours, and wait for it rather than assume."""
        if proc is None or proc.poll() is not None:
            return
        if gently:
            proc.terminate()
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                pass
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def stop(self):
        with self._lock:
            proc, self._proc = self._proc, None
            self._port, self._log, self._key = 0, "", None
        self._kill(proc, gently=True)
        if proc is not None:
            self._forget()

    # ---- servers a killed Dikte left behind -------------------------------

    def _pid_file(self):
        return DATA_DIR / f"{self.program.name}-server.pid"

    def _remember(self, pid):
        try:
            path = self._pid_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(pid))
        except OSError:
            pass      # the sweep is a safety net, not something to fail a run over

    def _forget(self):
        try:
            self._pid_file().unlink()
        except OSError:
            pass

    def _is_ours(self, pid):
        """Whether that pid is still the server this Dikte started.

        Asked because pids are handed out again: by the time anyone looks, the
        number could belong to something else entirely, and killing it would be
        a good deal worse than the leak being cleaned up. The program name alone
        could be somebody else's copy; the name together with Dikte's own data
        directory on the command line could not.
        """
        try:
            blob = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        return (self.program.binary.encode() in blob
                and str(DATA_DIR).encode() in blob)

    def sweep(self):
        """Kill a server a previous Dikte left behind. True when one was found.

        stop() and atexit cover every exit that gets to run code. A SIGKILL does
        not, and neither does a session torn down from under it, and the server
        would then sit there holding the model with nothing left alive to ask it
        anything.
        """
        try:
            pid = int(self._pid_file().read_text().strip())
        except (OSError, ValueError):
            return False
        self._forget()
        if not self._is_ours(pid):
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True


# --- the two of them ------------------------------------------------------


def _whisper_args(settings):
    binary = program_path(WHISPER, settings["binary"])
    if not binary:
        raise LocalError(t("whisper.cpp is not installed. Settings → API and "
                           "models → Download."))
    model = whisper_model_path(settings["model"])
    if not settings["model"] or not have_model(model):
        raise LocalError(t("No whisper model has been downloaded yet. "
                           "Settings → API and models → Download."))
    args = [
        binary, "-m", str(model),
        "--inference-path", INFERENCE_PATH,
        # Whatever language the request does not name. api.py leaves the field
        # out when the language is "auto", and the server's own default is
        # English rather than detection.
        "-l", "auto",
        # Stock phrases invented for near-silence come from non-speech tokens,
        # and verbose_json otherwise pays for a language probability sweep
        # nothing here reads.
        "-sns", "-nlp",
    ]
    if int(settings["threads"]) > 0:
        args += ["-t", str(int(settings["threads"]))]
    if not settings["gpu"]:
        args.append("-ng")
    return args


def _llm_args(settings):
    binary = program_path(LLAMA, settings["binary"])
    if not binary:
        raise LocalError(t("llama.cpp is not installed. Settings → API and "
                           "models → Download."))
    model = llm_model_path(settings["model"])
    if not settings["model"] or not have_model(model):
        raise LocalError(t("No local cleanup model has been downloaded yet. "
                           "Settings → API and models → Download."))
    args = [binary, "-m", str(model), "-c", str(int(settings["context"]))]
    # All of them, or as many as fit: llama.cpp stops offloading when the card
    # is full rather than failing, and a build with no GPU backend ignores it.
    args += ["-ngl", "99" if settings["gpu"] else "0"]
    if int(settings["threads"]) > 0:
        args += ["-t", str(int(settings["threads"]))]
    return args


whisper = Server(WHISPER, _whisper_args, {
    "model": "",
    "threads": 0,
    "gpu": True,
    "binary": "",
})

llm = Server(LLAMA, _llm_args, {
    "model": "",
    "threads": 0,
    "gpu": True,
    "binary": "",
    # A dictation and its prompt are short. This is sized for the longest
    # cleanup block rather than for a conversation, and it is what the model
    # costs in memory beyond its own weights.
    "context": 8192,
})

SERVERS = (whisper, llm)


def sweep():
    """Clean up after a Dikte that was killed outright. True when one was found."""
    return any([server.sweep() for server in SERVERS])


def stop_all():
    for server in SERVERS:
        server.stop()


# Dikte stops the servers itself on quit and on restart; this catches the paths
# that skip that, such as an unhandled exception on the way out.
atexit.register(stop_all)
