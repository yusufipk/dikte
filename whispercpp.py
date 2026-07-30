"""Local speech to text: the whisper.cpp server, kept alive next to Dikte.

The point of running a server rather than `whisper-cli` once per dictation is
the model: whisper-cli loads it from scratch every time, which on a large model
costs a second or two before a word is transcribed, while a dictation of a few
seconds is otherwise over almost as soon as it is spoken.

The server is started on `--inference-path /v1/audio/transcriptions`, which is
exactly the path api.py builds for the hosted providers, so a local run is one
more base URL rather than a second transcription code path. It also accepts the
same `language`, `prompt` and `response_format` fields, and its `verbose_json`
carries OpenAI's segment shape, so timestamps come back unchanged too.

Nothing here imports the rest of Dikte apart from the string table: this module
knows how to fetch a model and how to run a process, and nothing about
dictation. Its errors leave as LocalWhisperError and api.py turns them into the
ApiError the interface already knows how to show.
"""

import atexit
import collections
import http.client
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from i18n import t

BINARY = "whisper-server"
HOST = "127.0.0.1"
# The path api.py asks for, so its URL and the server's line up.
INFERENCE_PATH = "/v1/audio/transcriptions"
HF_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
DEFAULT_MODEL = "large-v3-turbo"

# Loading a large model onto the GPU is the slow part of a start; a minute is
# far past that even on a cold page cache.
STARTUP_TIMEOUT = 60.0
DOWNLOAD_CHUNK = 1 << 20

MODELS_DIR = (pathlib.Path(os.environ.get("XDG_DATA_HOME")
                           or os.path.expanduser("~/.local/share"))
              / "dikte" / "models")

# `size` is what the download is expected to weigh, used to warn before it
# starts and to draw the progress bar before the first response header arrives.
Model = collections.namedtuple("Model", "id label size")

MODELS = [
    Model("large-v3-turbo", "large-v3-turbo", 1_624_555_275),
    Model("large-v3-turbo-q5_0", "large-v3-turbo (q5_0)", 574_041_195),
    Model("large-v3", "large-v3", 3_095_033_483),
    Model("large-v3-q5_0", "large-v3 (q5_0)", 1_080_732_493),
    Model("medium", "medium", 1_533_763_059),
    Model("medium-q5_0", "medium (q5_0)", 539_212_467),
    Model("small", "small", 487_601_967),
    Model("base", "base", 147_951_465),
    Model("tiny", "tiny", 77_691_713),
]

MODEL_IDS = [m.id for m in MODELS]


class LocalWhisperError(Exception):
    pass


def find(model_id):
    for model in MODELS:
        if model.id == model_id:
            return model
    return None


def label(model_id):
    model = find(model_id)
    return model.label if model else model_id


def human_size(count):
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024.0
    return f"{count:.1f} GB"


def binary_path(custom=""):
    """The whisper-server to run, or "" when there is none."""
    custom = (custom or "").strip()
    if custom:
        return custom if os.path.isfile(custom) and os.access(custom, os.X_OK) else ""
    return shutil.which(BINARY) or ""


def available(custom=""):
    return bool(binary_path(custom))


def models_dir():
    return MODELS_DIR


def model_path(model_id):
    return MODELS_DIR / f"ggml-{model_id}.bin"


def installed(model_id):
    path = model_path(model_id)
    return path.is_file() and path.stat().st_size > 0


def installed_ids():
    return [m.id for m in MODELS if installed(m.id)]


def model_url(model_id):
    return f"{HF_BASE}/ggml-{model_id}.bin"


def delete(model_id):
    try:
        model_path(model_id).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LocalWhisperError(
            t("Could not delete the model: {error}", error=exc)) from exc


def download(model_id, on_progress=None, should_stop=None):
    """Fetch a model into the models directory. True when it landed.

    The bytes go to a `.part` file that is only renamed once the whole download
    has arrived, so an interrupted one can never be mistaken for a model: a
    truncated .bin would be found by installed() and fail much later, inside the
    server, with a message about a corrupt file.
    """
    if find(model_id) is None:
        raise LocalWhisperError(t("Unknown model: {model}", model=model_id))
    target = model_path(model_id)
    part = target.with_name(target.name + ".part")
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LocalWhisperError(
            t("Could not create the model directory: {error}", error=exc)) from exc

    request = urllib.request.Request(
        model_url(model_id), headers={"User-Agent": "dikte/1.0"})
    done = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            with open(part, "wb") as out:
                while True:
                    if should_stop is not None and should_stop():
                        part.unlink(missing_ok=True)
                        return False
                    block = response.read(DOWNLOAD_CHUNK)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if on_progress is not None:
                        on_progress(done, total)
        # A proxy or an error page that came back as 200 would otherwise be
        # renamed into place and only fail when the server tries to read it.
        if total and done != total:
            part.unlink(missing_ok=True)
            raise LocalWhisperError(
                t("The download stopped early ({done} of {total}).",
                  done=human_size(done), total=human_size(total)))
        part.replace(target)
        return True
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        exc.close()   # it holds the response body open until it is collected
        raise LocalWhisperError(
            t("Could not download the model: HTTP {code}", code=exc.code)) from exc
    except urllib.error.URLError as exc:
        part.unlink(missing_ok=True)
        raise LocalWhisperError(
            t("Could not download the model: {error}", error=exc.reason)) from exc
    except http.client.HTTPException as exc:
        # A connection cut mid-body: not an OSError, and gigabytes in it is
        # exactly where that happens.
        part.unlink(missing_ok=True)
        raise LocalWhisperError(
            t("Could not download the model: {error}", error=exc)) from exc
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise LocalWhisperError(
            t("Could not write the model: {error}", error=exc)) from exc


def _free_port():
    """A port nothing is listening on, handed straight to the server.

    Between closing this socket and the server binding it, something else could
    take it; that is why start() retries rather than trusting the number.
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


def _pid_file():
    return MODELS_DIR.parent / "whisper-server.pid"


def _remember(pid):
    try:
        path = _pid_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid))
    except OSError:
        pass       # the sweep is a safety net, not something to fail a run over


def _forget():
    try:
        _pid_file().unlink()
    except OSError:
        pass


def _is_our_server(pid):
    """Whether that pid is still the whisper-server this Dikte started.

    Asked because pids are handed out again: by the time anyone looks, the
    number could belong to something else entirely, and killing it would be a
    good deal worse than the leak being cleaned up.
    """
    try:
        blob = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    # All three together, rather than the program name alone: the name could
    # belong to a whisper-server somebody else is running, while the model
    # directory and the path Dikte alone asks the server to listen on could
    # not. Matching on the whole command line rather than argv[0] also covers
    # a binary setting that points at a wrapper script.
    return all(part in blob for part in
               (BINARY.encode(), INFERENCE_PATH.encode(), str(MODELS_DIR).encode()))


def sweep():
    """Kill a server a previous Dikte left behind. True when one was found.

    stop() and atexit cover every exit that gets to run code. A SIGKILL does
    not, and neither does the session being torn down from under it, and
    whisper-server would then sit there holding a gigabyte and a half of
    graphics memory with nothing left alive to ask it anything.
    """
    try:
        pid = int(_pid_file().read_text().strip())
    except (OSError, ValueError):
        return False
    _forget()
    if not _is_our_server(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _tail(path, lines=3):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            found = [line.strip() for line in fh if line.strip()]
    except OSError:
        return ""
    return " | ".join(found[-lines:])


class _Server:
    """The one whisper-server process, started when something needs it."""

    def __init__(self):
        self._lock = threading.RLock()
        self._proc = None
        self._port = 0
        self._log = ""
        self._running_key = None
        self._settings = {
            "model": DEFAULT_MODEL,
            "threads": 0,
            "gpu": True,
            "binary": "",
        }

    # ---- settings --------------------------------------------------------

    def configure(self, **changes):
        """Apply settings; a running server started on the old ones is stopped."""
        with self._lock:
            for key, value in changes.items():
                if value is not None and key in self._settings:
                    self._settings[key] = value
            if self._proc is not None and self._running_key != self._key():
                self.stop()

    def settings(self):
        with self._lock:
            return dict(self._settings)

    def _key(self):
        settings = self._settings
        return (str(model_path(settings["model"])), int(settings["threads"]),
                bool(settings["gpu"]), binary_path(settings["binary"]))

    # ---- process ---------------------------------------------------------

    @property
    def running(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def base_url(self):
        with self._lock:
            return f"http://{HOST}:{self._port}/v1" if self.running else ""

    def serve(self):
        """The base URL of a server that is up and running the chosen model."""
        with self._lock:
            if self.running and self._running_key == self._key():
                return self.base_url()
            self.stop()
            self._start()
            return self.base_url()

    def _start(self):
        settings = self._settings
        binary = binary_path(settings["binary"])
        if not binary:
            raise LocalWhisperError(
                t("whisper.cpp is not installed. Install it with: "
                  "sudo pacman -S whisper-cpp"))
        model = model_path(settings["model"])
        if not installed(settings["model"]):
            raise LocalWhisperError(
                t("The local model “{model}” has not been downloaded. Settings → "
                  "API and models → Download.", model=label(settings["model"])))

        last = ""
        for _ in range(3):
            port = _free_port()
            log = tempfile.NamedTemporaryFile(
                prefix="dikte-whisper-", suffix=".log", delete=False)
            log.close()
            args = [
                binary, "-m", str(model),
                "--host", HOST, "--port", str(port),
                "--inference-path", INFERENCE_PATH,
                # Whatever language the request does not name; api.py leaves the
                # field out when the language is "auto", and the server's own
                # default is English rather than detection.
                "-l", "auto",
                # Stock phrases invented for near-silence come from non-speech
                # tokens, and verbose_json otherwise pays for a language
                # probability sweep nothing here reads.
                "-sns", "-nlp",
            ]
            if int(settings["threads"]) > 0:
                args += ["-t", str(int(settings["threads"]))]
            if not settings["gpu"]:
                args.append("-ng")

            try:
                with open(log.name, "wb") as sink:
                    proc = subprocess.Popen(
                        args, stdout=sink, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                    )
            except OSError as exc:
                os.unlink(log.name)
                raise LocalWhisperError(
                    t("Could not start whisper.cpp: {error}", error=exc)) from exc

            # Written now rather than once it is ready, so that a kill during
            # the model load leaves something for the sweep to find too.
            _remember(proc.pid)
            if self._wait_ready(proc, port):
                self._proc, self._port, self._log = proc, port, log.name
                self._running_key = self._key()
                return
            last = _tail(log.name)
            os.unlink(log.name)
            _forget()
            # A port taken between the probe and the bind is the one failure
            # worth another go; anything else will fail the same way again.
            if "address" not in last.lower() and "bind" not in last.lower():
                break
        raise LocalWhisperError(
            t("whisper.cpp did not start: {error}", error=last or t("no output")))

    def _wait_ready(self, proc, port):
        """The port opens only after the model is loaded, so it is the signal."""
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            if _listening(port):
                return True
            time.sleep(0.1)
        proc.kill()
        proc.wait(timeout=5)
        return False

    def stop(self):
        with self._lock:
            proc, log = self._proc, self._log
            self._proc, self._port, self._log, self._running_key = None, 0, "", None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if log:
            try:
                os.unlink(log)
            except OSError:
                pass
        if proc is not None:
            _forget()

    def error(self):
        """The last thing the server printed, for a failure after it started."""
        with self._lock:
            return _tail(self._log) if self._log else ""


_SERVER = _Server()


def configure(**changes):
    _SERVER.configure(**changes)


def settings():
    return _SERVER.settings()


def serve():
    return _SERVER.serve()


def stop():
    _SERVER.stop()


def running():
    return _SERVER.running


def base_url():
    return _SERVER.base_url()


def error():
    return _SERVER.error()


def ready(model_id=None, binary=""):
    """Both halves are in place: the program is installed and the model is here."""
    model_id = model_id or _SERVER.settings()["model"]
    return available(binary) and installed(model_id)


def status(model_id=None, binary=""):
    """One line for the settings window."""
    current = _SERVER.settings()
    model_id = model_id or current["model"]
    if not available(binary):
        return t("whisper.cpp is not installed. Install it with: "
                 "sudo pacman -S whisper-cpp")
    if not installed(model_id):
        model = find(model_id)
        return t("“{model}” has not been downloaded yet ({size}).",
                 model=label(model_id),
                 size=human_size(model.size) if model else "?")
    if _SERVER.running and _SERVER.settings()["model"] == model_id:
        return t("Running: {model}, {device}.", model=label(model_id),
                 device=t("GPU") if current["gpu"] else t("CPU"))
    return t("Ready: {model}, {device}. It loads on the first dictation.",
             model=label(model_id), device=t("GPU") if current["gpu"] else t("CPU"))


# Dikte stops the server itself on quit and on restart; this catches the paths
# that skip that, such as an unhandled exception on the way out.
atexit.register(stop)
