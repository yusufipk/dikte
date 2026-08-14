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
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

import hub
from i18n import t
from platforms import IS_MACOS, IS_WINDOWS, adapter

runtime = adapter("runtime")

HOST = "127.0.0.1"
# The path api.py asks for, so its URL and the server's line up.
INFERENCE_PATH = "/v1/audio/transcriptions"

DATA_DIR = runtime.data_dir()
BIN_DIR = DATA_DIR / "bin"
MODELS_DIR = DATA_DIR / "models"

# What the programs are called once they are unpacked.
EXE = ".exe" if IS_WINDOWS else ""

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
    total = 0
    stopped = False
    overlong = False
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or item.size or 0)
            # Every way out of this block leaves the file closed before the
            # `.part` is deleted. Windows refuses to unlink a file that is
            # still open, so a cancelled download used to fail with a sharing
            # violation on top of having been cancelled.
            with open(part, "wb") as out:
                while True:
                    if should_stop is not None and should_stop():
                        stopped = True
                        break
                    block = response.read(DOWNLOAD_CHUNK)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    # More than was announced: a body that does not end is the
                    # one way this loop could run until the disk is full.
                    if total and done > total:
                        overlong = True
                        break
                    if on_progress is not None:
                        on_progress(done, total)
    except urllib.error.HTTPError as exc:
        _drop(part)
        exc.close()   # it holds the response body open until it is collected
        raise LocalError(t("Could not download {name}: HTTP {code}",
                           name=item.name, code=exc.code)) from exc
    except urllib.error.URLError as exc:
        _drop(part)
        raise LocalError(t("Could not download {name}: {error}",
                           name=item.name, error=exc.reason)) from exc
    except OSError as exc:
        # A connection cut mid-body arrives here too, and gigabytes in is
        # exactly where that happens.
        _drop(part)
        raise LocalError(t("Could not write {name}: {error}",
                           name=item.name, error=exc)) from exc

    if stopped:
        _drop(part)
        return False
    if overlong:
        _drop(part)
        raise LocalError(t("{name} is longer than it said it would be.",
                           name=item.name))
    # A proxy notice or an error page that came back as 200 would otherwise
    # be renamed into place and only fail when something tries to read it.
    if total and done != total:
        _drop(part)
        raise LocalError(t("The download stopped early ({done} of {total}).",
                           done=human_size(done), total=human_size(total)))
    if item.sha256 and digest.hexdigest() != item.sha256:
        _drop(part)
        raise LocalError(t("{name} does not match its published checksum. "
                           "Nothing was installed.", name=item.name))
    try:
        part.replace(target)
    except OSError as exc:
        _drop(part)
        raise LocalError(t("Could not write {name}: {error}",
                           name=item.name, error=exc)) from exc
    return True


def _drop(path):
    """Delete a file, waiting out whatever still has it open.

    On Windows an on-access virus scanner opens a file the moment it is closed,
    and a delete arriving in that window fails outright rather than being
    queued the way it would be on Linux. Half a second of retries is the
    difference between a cancelled download and a stray `.part` nobody cleans
    up.
    """
    path = pathlib.Path(path)
    for attempt in range(6):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    return False


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
    return bool(ctypes.util.find_library("vulkan-1" if IS_WINDOWS else "vulkan"))


# What a user may ask the local programs to run on. "auto" is what Dikte picks
# when nobody has said; the other three are a choice, and a choice that cannot
# be honoured is refused rather than quietly turned into something else.
BACKENDS = ("auto", "cpu", "cuda", "vulkan")

BACKEND_LABELS = {
    "auto": "Auto",
    "cpu": "CPU",
    "cuda": "NVIDIA (CUDA)",
    "vulkan": "Vulkan",
}


def _wanted_assets(program, backend="auto"):
    """Regular expressions matching the release assets to accept, best first.

    Matched rather than compared, because the names carry versions nobody here
    should have to know: whisper's CUDA build is `whisper-cublas-12.4.0-bin-
    x64.zip` today and something else next release. Full patterns rather than
    endings, because `whisper-bin-x64.zip`, `whisper-blas-bin-x64.zip` and
    `whisper-cublas-12.4.0-bin-x64.zip` all end the same way and are three very
    different downloads.
    """
    arch = _arch()
    backend = backend if backend in BACKENDS else "auto"
    if IS_WINDOWS:
        return _windows_assets(program, backend, arch)
    if IS_MACOS:
        # llama.cpp publishes a native Metal-enabled macOS archive. whisper.cpp
        # publishes no runnable macOS server archive; never mistake Ubuntu's
        # arm64 build for a native one.
        return (() if program is WHISPER
                else (rf"bin-macos-{arch}\.tar\.gz$",))
    return _linux_assets(program, backend, arch)


def _linux_assets(program, backend, arch):
    cpu = rf"bin-ubuntu-{arch}\.tar\.gz$"
    vulkan = rf"bin-ubuntu-vulkan-{arch}\.tar\.gz$"
    if program is not LLAMA:
        # whisper.cpp publishes one Linux build, and it is the CPU one.
        return (cpu,)
    if backend == "vulkan":
        return (vulkan,)
    if backend == "cpu":
        return (cpu,)
    if backend == "cuda":
        # There is no CUDA build for Linux; Vulkan is how a card is reached.
        return ()
    # Auto, which is what it has always done: the Vulkan build when a loader is
    # installed, because the plain one carries CPU backends only.
    return (vulkan, cpu) if _has_vulkan() else (cpu,)


def _windows_assets(program, backend, arch):
    if program is LLAMA:
        # Anchored on the program's own name: the CUDA runtime is published as
        # `cudart-llama-bin-win-cuda-12.4-x64.zip`, which ends the same way as
        # the build it belongs to and holds no llama-server at all.
        table = {
            "cpu": (rf"^llama-.*-bin-win-cpu-{arch}\.zip$",),
            "cuda": (rf"^llama-.*-bin-win-cuda-[\d.]+-{arch}\.zip$",),
            "vulkan": (rf"^llama-.*-bin-win-vulkan-{arch}\.zip$",),
        }
    else:
        table = {
            "cpu": (rf"^whisper-bin-{arch}\.zip$",),
            "cuda": (rf"^whisper-cublas-[\d.]+-bin-{arch}\.zip$",),
            # whisper.cpp publishes no Vulkan build for Windows. Saying so is
            # the point: falling back to the CPU one would look like a working
            # graphics card that is simply slow.
            "vulkan": (),
        }
    # Auto is the CPU build. It is the one that runs on every machine, and the
    # one whose failure modes are not "which driver is installed"; a card is
    # something to opt into, once, in the settings.
    return table["cpu"] if backend == "auto" else table[backend]


def backend_choices(program):
    """The backends worth offering for this program on this machine.

    What the projects publish differs by platform and by program: no CUDA
    build of llama.cpp for Linux, no Vulkan build of whisper.cpp for Windows.
    Offering one that does not exist is offering a failure.
    """
    return tuple(name for name in BACKENDS
                 if name == "auto" or _wanted_assets(program, name))


# Both projects publish several CUDA builds at once, against different CUDA
# runtimes: whisper.cpp has 11.8 and 12.4 today, llama.cpp 12.4 and 13.3. A
# driver runs anything up to its own version and nothing above it, so which one
# to fetch is a question about this machine rather than a matter of taste.
_CUDA_IN_NAME = re.compile(r"(?:cublas-|cuda-)(\d+)\.(\d+)")


def _cuda_version(name):
    found = _CUDA_IN_NAME.search(name)
    return (int(found.group(1)), int(found.group(2))) if found else None


def _pick(assets, pattern):
    """The best asset matching `pattern`, or None when none of them will do.

    For everything but CUDA there is one match and it is the answer. For CUDA
    it is the newest build this machine's driver can load: the oldest would
    work and waste the card, and the newest would fail inside the server with a
    missing DLL naming a CUDA version and nothing about what to do.
    """
    found = [asset for asset in assets if re.search(pattern, asset.name)]
    if not found:
        return None
    versions = [(_cuda_version(asset.name), asset) for asset in found]
    if any(version is None for version, _asset in versions):
        return found[0]
    driver = runtime.cuda_driver_version()
    if driver is None:
        # No NVIDIA driver on this machine, so no CUDA build can run on it.
        return None
    usable = [pair for pair in versions if pair[0] <= driver]
    return max(usable)[1] if usable else None


def _no_build(program, tag, backend, assets):
    """Why nothing was fetched, in terms of this machine rather than the release."""
    if backend == "cuda":
        driver = runtime.cuda_driver_version()
        if driver is None:
            return t("There is no NVIDIA driver on this machine, so a CUDA "
                     "build of {name} could not run. Pick another one under "
                     "Runs on.", name=program.name)
        published = sorted(v for v in (_cuda_version(a.name) for a in assets) if v)
        if published:
            return t(
                "{repo} {tag} publishes CUDA {wanted} at the oldest, and this "
                "driver runs CUDA {have}. Update the NVIDIA driver, or pick "
                "another build under Runs on.",
                repo=program.repo, tag=tag,
                wanted=f"{published[0][0]}.{published[0][1]}",
                have=f"{driver[0]}.{driver[1]}",
            )
    if backend not in ("", "auto"):
        return t("{repo} {tag} publishes no {backend} build for this machine.",
                 repo=program.repo, tag=tag,
                 backend=BACKEND_LABELS.get(backend, backend))
    return t("{repo} {tag} has no build for this machine.",
             repo=program.repo, tag=tag)


# The CUDA builds of llama.cpp for Windows are published without the CUDA
# runtime beside them: the DLLs come in a second archive, and the server exits
# on a missing cudart64 without either archive being at fault. Unpacked into
# the same directory, which is where the loader looks first.
_CUDART = re.compile(r"^cudart-llama-bin-win-cuda-([\d.]+)-(\w+)\.zip$")
_CUDA_BUILD = re.compile(r"bin-win-cuda-([\d.]+)-(\w+)\.zip$")


def _companions(program, item, assets):
    """Other assets that have to be unpacked beside `item` for it to run."""
    if program is not LLAMA or not IS_WINDOWS:
        return []
    build = _CUDA_BUILD.search(item.name)
    if not build:
        return []
    wanted = build.groups()
    found = []
    for asset in assets:
        runtime_build = _CUDART.match(asset.name)
        if runtime_build and runtime_build.groups() == wanted:
            found.append(asset)
    return found


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


def installed_backend(program):
    """What the downloaded build runs on, so the settings can show it."""
    try:
        record = json.loads(_install_record(program).read_text(encoding="utf-8"))
        return record.get("backend") or "auto"
    except (OSError, ValueError):
        return "auto"


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


def binary_name(program):
    """What the executable is called once it is unpacked."""
    return program.binary + EXE


def _find_binary(root, name):
    for path in sorted(pathlib.Path(root).rglob(name)):
        if path.is_file():
            return path
    return None


def _extract(archive, into):
    """Unpack a release archive, refusing anything that reaches outside `into`.

    The archives lay their libraries next to their binaries, with an $ORIGIN
    runpath on Linux and the DLLs the loader looks for beside the .exe on
    Windows, so a whole directory is what has to survive the trip and the
    binary cannot be lifted out of it.
    """
    name = os.path.basename(str(archive))
    try:
        if zipfile.is_zipfile(archive):
            _extract_zip(archive, into)
        else:
            with tarfile.open(archive, "r:gz") as tar:
                try:
                    tar.extractall(into, filter="data")
                except TypeError:      # Python without the extraction filters
                    tar.extractall(into)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise LocalError(t("Could not unpack {name}: {error}",
                           name=name, error=exc)) from exc


def _extract_zip(archive, into):
    """A zip, with every member checked before any of it is written.

    A zip entry is a string the archive chose, and it may name `..\\..\\` or an
    absolute path. Python's own extractor strips those, but this is a program
    that will then be run, so the check is made here and out loud: an archive
    that tries it is refused whole rather than quietly flattened.
    """
    root = pathlib.Path(into).resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            landing = (root / name).resolve()
            if landing != root and root not in landing.parents:
                raise LocalError(t(
                    "{archive} tried to write outside its own directory "
                    "({member}). Nothing was installed.",
                    archive=os.path.basename(str(archive)), member=member.filename,
                ))
        zf.extractall(root)


def install_program(program, tag="", on_progress=None, should_stop=None,
                    refresh=False, backend="auto"):
    """Fetch and unpack a release. The path to the binary, or "" when stopped.

    `tag` is empty for whatever the project released last, which is the point:
    a version pinned in Dikte's source would mean a release of Dikte every time
    whisper.cpp has one. `backend` is what the user asked to run on; a machine
    the project publishes no such build for is told so rather than handed the
    CPU one under another name.
    """
    try:
        tag, assets = hub.release(program.repo, tag or "latest", refresh=refresh)
    except hub.HubError as exc:
        raise LocalError(str(exc)) from exc

    item = None
    for pattern in _wanted_assets(program, backend):
        item = _pick(assets, pattern)
        if item:
            break
    if item is None:
        if IS_MACOS and program is WHISPER:
            raise LocalError(t(
                "whisper.cpp publishes no macOS build. Install it with: "
                "brew install whisper-cpp"
            ))
        raise LocalError(_no_build(program, tag, backend, assets))

    into = BIN_DIR / program.name / tag
    shutil.rmtree(into, ignore_errors=True)
    wanted = [item] + _companions(program, item, assets)
    archives = []
    try:
        for asset in wanted:
            archive = BIN_DIR / program.name / asset.name
            archives.append(archive)
            if not download(asset, archive, on_progress, should_stop):
                return ""
            _extract(archive, into)
        binary = _find_binary(into, binary_name(program))
        if binary is None:
            raise LocalError(t("{name} was not in the download.",
                               name=binary_name(program)))
        _make_runnable(binary)
        _install_record(program).write_text(
            json.dumps({"tag": tag, "binary": str(binary), "backend": backend}),
            encoding="utf-8")
    except OSError as exc:
        raise LocalError(t("Could not install {name}: {error}",
                           name=program.name, error=exc)) from exc
    finally:
        for archive in archives:
            _drop(archive)
    _drop_old_versions(program, keep=tag)
    return str(binary)


def _make_runnable(binary):
    """Give the unpacked program the execute bit, where there is one to give."""
    if IS_WINDOWS:
        return
    try:
        binary.chmod(binary.stat().st_mode | 0o111)
    except OSError:
        pass


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
                        # No console window on Windows: a server started from a
                        # tray icon would otherwise put a black box on screen
                        # and keep it there for as long as the model is loaded.
                        **runtime.NO_WINDOW,
                    )
            except OSError as exc:
                raise LocalError(t("Could not start {name}: {error}",
                                   name=self.program.name, error=exc)) from exc

            # Tied to this process where the platform can do that, so a Dikte
            # killed outright takes the loaded model down with it rather than
            # leaving gigabytes resident.
            runtime.adopt(proc)
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
        return runtime.is_our_process(pid, (self.program.binary, str(DATA_DIR)))

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
        return runtime.terminate(pid)


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
