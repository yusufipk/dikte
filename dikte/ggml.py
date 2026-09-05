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
import ctypes
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
import zipfile

from . import hub
from . import paths
from .i18n import t

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
# A child that loses the bind race fails and exits at once; a model that fails
# to load takes longer than this to be read in first. The line between "worth
# another port" and "would fail the same way again" is drawn on time.
EARLY_EXIT_WINDOW = 5.0
DOWNLOAD_CHUNK = 1 << 20

# `health` is the path that answers only once the model is in memory. whisper
# does not have one and does not need one: it binds its port after the model is
# loaded, so the port opening is the signal.
Program = collections.namedtuple("Program", "name repo binary health")

WHISPER = Program("whisper", "ggml-org/whisper.cpp", "whisper-server", "")
LLAMA = Program("llama", "ggml-org/llama.cpp", "llama-server", "/health")
DIKTE_REPO = "yusufipk/dikte"
MANAGED_WHISPER_RELEASE = "whisper.cpp-v1.9.3"
MANAGED_WHISPER_VERSION = "v1.9.3"
MANAGED_WHISPER_VULKAN = "whisper-bin-ubuntu-vulkan-x64.tar.gz"
MANAGED_WHISPER_SHA256 = (
    "c25ca76504144da488eb74441390a7b9aa7ce547e5f2f391cbd831253c9b54d8"
)

# Where the models are listed. Neither list is written into Dikte: a catalogue
# in the source means a release of Dikte for every model somebody else
# publishes.
WHISPER_MODELS_REPO = "ggerganov/whisper.cpp"
LLM_AUTHOR = "ggml-org"

# The file llama.cpp attaches to its version releases in place of the binaries:
# a line naming the nightly tag those are published under.
NIGHTLY_TAG = "nightly-tag.txt"

# What the whisper repository holds besides models: Core ML encoders for Apple
# hardware and the odd loose file.
WHISPER_PREFIX = "ggml-"
WHISPER_SUFFIX = ".bin"
# The mark on the whisper models trained on English alone. They are half of the
# list, and they belong under the model they are a variant of rather than
# scattered through it by size.
ENGLISH_ONLY = ".en"

# Full-precision weights, however they are spelled. Several times the memory of
# a quantisation of the same model, for a difference dictation and cleanup
# cannot see, so nothing here ever points at one.
SIXTEEN_BIT = ("bf16", "f16", "fp16")

# How many bits a weight is stored in, read off the file name. Every one of
# these lists spells it differently, `q5_1` and `Q4_K_M` and `MXFP4` and
# `BF16`, and the only part of that anybody choosing between two rows needs is
# the number. Longest mark first, so `bf16` is not read as `f16`.
BIT_DEPTHS = (("mxfp4", 4), ("bf16", 16), ("fp16", 16), ("f16", 16),
              ("q2", 2), ("q3", 3), ("q4", 4), ("q5", 5), ("q6", 6), ("q8", 8))

# What a GGUF repository holds besides the model: mmproj is the vision half of a
# multimodal model, and mtp, dflash, dspark and eagle3 are draft heads for
# speculative decoding. None of them is a model a server can be started on, and
# they are the small files in the repository, so a list sorted by size puts them
# at the top where they are likeliest to be clicked.
GGUF_SKIP = ("mmproj", "mtp-", "dflash-", "dspark-", "eagle3-", "draft-")
# Big enough for a 12B at Q4 and far past anything cleanup wants; the point is
# to keep a 400 GB frontier model out of a list somebody might click.
GGUF_MAX_BYTES = 16 << 30

# Repositories that carry GGUF files but nothing a cleanup server can be started
# on: a vision or audio tower with no text half worth running, a speech model,
# and the base models, which continue text rather than following an instruction
# and answer a cleanup prompt by carrying on writing the transcript.
# Matched as plain substrings, so every one of these carries its own
# delimiters: an unanchored "test-" is also inside "Latest-" and would drop a
# publisher that is perfectly usable.
LLM_REPO_SKIP = ("-Base-GGUF", "-VL-", "-Vision-", "-Omni-", "-Video-",
                 "-TTS-", "parakeet", "/test-")

GB = 1 << 30

# Suggestions, not a catalogue: the list itself is fetched, and these are only
# the rows that float to the top of it. Cleanup is punctuation, capitals and
# filler words rather than anything that wants thinking about, so what it is
# picked on is instruction following at a size a desktop can spare. Gemma 4
# scores 94.6 on IFEval at E2B and 96.7 at E4B, and E2B leads here rather than
# E4B because two points of instruction following is not worth twice the
# weights on a job that runs while somebody waits for their sentence to appear.
# SmolLM3 and Gemma 3 are the older pair below them. Qwen3.5 0.8B is for the
# machines nothing else fits on; it thinks before it answers, which is what the
# Thinking box in the settings window turns off.
SUGGESTED_LLM = (
    "ggml-org/gemma-4-E2B-it-GGUF",
    "ggml-org/gemma-4-E4B-it-GGUF",
    "ggml-org/gemma-3-4b-it-GGUF",
    "ggml-org/SmolLM3-3B-GGUF",
    "ggml-org/Qwen3.5-0.8B-GGUF",
)
# Roughly what each of those weighs at the quantisation cleanup would run, to
# the nearest half gigabyte. Not a catalogue of files: the sizes on the rows
# come from the publisher, and this only decides which suggestion is offered
# first on a machine that has room for some of them and not others.
SUGGESTED_LLM_SIZE = {
    "ggml-org/gemma-4-E2B-it-GGUF": 3 * GB,
    "ggml-org/gemma-4-E4B-it-GGUF": 5 * GB,
    "ggml-org/gemma-3-4b-it-GGUF": 5 * GB // 2,
    "ggml-org/SmolLM3-3B-GGUF": 2 * GB,
    "ggml-org/Qwen3.5-0.8B-GGUF": GB // 2,
}

# What each of them is, in the words somebody choosing between them would
# use. A repository id says the publisher, the parameter count, the shape of
# the weights and nothing at all about whether it is the one to click, and
# `ggml-org/gemma-4-E2B-it-GGUF` reads as four pieces of jargon to everybody
# who has not been reading model cards all year.
SUGGESTED_LLM_NOTE = {
    "ggml-org/gemma-4-E2B-it-GGUF":
        "Google Gemma 4, the small one. The default: nothing else this size "
        "follows an instruction as closely, and cleanup is all instruction.",
    "ggml-org/gemma-4-E4B-it-GGUF":
        "The same model one size up. A little more accurate, about twice the "
        "weights and twice the wait.",
    "ggml-org/gemma-3-4b-it-GGUF":
        "The previous Gemma. Still good, and the smallest of the Gemmas here.",
    "ggml-org/SmolLM3-3B-GGUF":
        "Hugging Face's own small model, for a machine the Gemmas crowd.",
    "ggml-org/Qwen3.5-0.8B-GGUF":
        "The smallest of them, for a machine nothing else fits on. It thinks "
        "before it answers unless Thinking below is off.",
}

# Turbo at q5_0 is smaller than `small` and better than it, which makes the
# usual "start small" advice point at the same file as "start good". It is
# large-v3 with the decoder cut from 32 layers to 4: several times faster, at
# one to two points of word error in English and about two and a half in the
# other languages.
SUGGESTED_WHISPER = "ggml-large-v3-turbo-q5_0.bin"
# Those two and a half points back, for twice the file and several times the
# work per second. Only suggested where there is a card to do the work and
# memory to hold it, because that is where the trade stops costing anything a
# person waiting for a dictation would notice.
ACCURATE_WHISPER = "ggml-large-v3-q5_0.bin"
# What to point at instead on a machine the turbo model would crowd. Same
# quantisation ladder, one rung down in size and in accuracy.
SMALL_MACHINE_WHISPER = "ggml-small-q5_1.bin"
# Under this much system memory, a 600 MB model plus the rest of a desktop is
# already tight, so the suggestion drops to the smaller one. Over the other,
# the accurate model is the one to point at.
SMALL_MACHINE = 4 * GB
# Fifteen and not sixteen: what the machine reports is what is left after the
# firmware and the graphics have taken their reservations out of it, and a
# 16 GB machine answers about 15.4. A threshold written at the number on the
# box is one no machine sold as that size ever reaches.
ROOMY_MACHINE = 15 * GB
# What a model may take of this machine's memory before it is called too big:
# half of it, less a gigabyte for the context and the runtime around the
# weights. A rule of thumb rather than a measurement, and deliberately a
# cautious one, because the failure it is guarding against is a machine that
# swaps itself to a standstill rather than a model that refuses to load.
MEMORY_SHARE = 0.5
MEMORY_OVERHEAD = GB
# What is left to offer on a machine too small for the sum above to leave
# anything. Enough for the smallest whisper models and for a sub-billion
# cleanup model, which is what such a machine can run.
MEMORY_FLOOR = GB // 2
# What total_memory() read the one time it asked. None until it has.
_MEMORY = None


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
            # Windows refuses to delete a file that is open, so nothing is
            # unlinked until the handle is closed again.
            stopped = overlong = False
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
            if stopped:
                part.unlink(missing_ok=True)
                return False
            if overlong:
                part.unlink(missing_ok=True)
                raise LocalError(t("{name} is longer than it said it "
                                   "would be.", name=item.name))
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
        try:
            part.replace(target)
        except PermissionError as exc:
            # Windows refuses to replace a file something has open, and a
            # running server holds its model and its binary open. The bytes
            # are complete and verified: keeping the .part costs a retry,
            # deleting it costs the whole download again.
            raise LocalError(t("{name} downloaded, but the old file is held "
                               "open by the running server. Stop it and try "
                               "again.", name=item.name)) from exc
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
    the download bigger. Windows spells the loader vulkan-1.dll.
    """
    return bool(ctypes.util.find_library("vulkan")
                or (sys.platform == "win32"
                    and ctypes.util.find_library("vulkan-1")))


def _wanted_assets(program):
    """Asset name endings to accept, best first.

    llama.cpp publishes native Metal-enabled macOS archives. whisper.cpp does
    not publish a runnable macOS server archive, so an arm64 Mac must not
    mistake Ubuntu's arm64 archive for a native build.
    """
    arch = _arch()
    if sys.platform == "darwin":
        return () if program is WHISPER else (f"bin-macos-{arch}.tar.gz",)
    if sys.platform == "win32":
        if program is WHISPER:
            # The BLAS build first: on a plain CPU it transcribes about twice
            # as fast as the stock one, and it carries everything it needs.
            # Full names, because "bin-x64.zip" alone would also match the
            # CUDA archives, whichever the release happened to list first.
            #
            # x64 whatever this machine is, because whisper.cpp publishes no
            # arm64 build for Windows: a Snapdragon runs this one emulated,
            # which is slow but is the only local option there is.
            return ("whisper-blas-bin-x64.zip", "whisper-bin-x64.zip")
        if _has_vulkan() and arch == "x64":
            return ("bin-win-vulkan-x64.zip", f"bin-win-cpu-{arch}.zip")
        return (f"bin-win-cpu-{arch}.zip",)
    if program is LLAMA and _has_vulkan():
        return (f"bin-ubuntu-vulkan-{arch}.tar.gz", f"bin-ubuntu-{arch}.tar.gz")
    return (f"bin-ubuntu-{arch}.tar.gz",)


def _managed_whisper(program, tag=""):
    """Whether this machine is one Dikte publishes its own whisper-server for.

    Linux x86_64 with a Vulkan loader on it, and no version asked for by hand:
    a pinned version is upstream's to answer.
    """
    return (not tag and program is WHISPER and sys.platform == "linux"
            and platform.machine().lower() in ("x86_64", "amd64")
            and _has_vulkan())


def _managed_asset(refresh=False):
    """The Vulkan whisper-server Dikte builds itself, or None.

    Taken only when the archive's digest is the reviewed one. Anything else,
    a release that is not there yet, a GitHub that cannot be reached, a file
    that is not the reviewed bytes, leaves upstream's processor build as the
    answer, and the install record says which of the two landed.
    """
    try:
        _, assets = hub.release(DIKTE_REPO, MANAGED_WHISPER_RELEASE,
                                refresh=refresh)
    except hub.HubError:
        return None
    return next((a for a in assets
                 if a.name.endswith(MANAGED_WHISPER_VULKAN)
                 and a.sha256 == MANAGED_WHISPER_SHA256), None)


def _matching_asset(program, assets):
    """The archive this machine wants out of one release's files, or None."""
    for ending in _wanted_assets(program):
        item = next((a for a in assets if a.name.endswith(ending)), None)
        if item:
            return item
    return None


def _pick_asset(program, tag="", refresh=False):
    """(tag, Item) for the release archive to install. Item is None when there
    is none for this machine.

    Dikte's own Vulkan whisper-server comes before upstream's where this
    machine is one it is built for, because whisper.cpp publishes no Vulkan
    archive for Linux at all.

    A named tag is taken as given. For the newest, what GitHub answers is not
    always where the builds are: llama.cpp's latest release is a version marker
    carrying a single nightly-tag.txt, which names the tag the archives are
    actually attached to, and those are prereleases that "latest" never points
    at. The pointer is followed when it is there, and when it is not, the newest
    release that does carry a build for this machine is taken instead.
    """
    if _managed_whisper(program, tag):
        item = _managed_asset(refresh=refresh)
        if item:
            return MANAGED_WHISPER_VERSION, item
    named = bool(tag) and tag != "latest"
    missing = None
    try:
        tag, assets = hub.release(program.repo, tag or "latest", refresh=refresh)
    except hub.HubError as exc:
        # A release carrying no files at all is the case the search below exists
        # for, not a reason to stop before it: the build for this machine may be
        # attached to a prerelease that "latest" never points at. The failure is
        # kept rather than dropped, because an unreachable GitHub arrives here
        # the same way and that one is the message the caller wants.
        if named:
            raise
        missing, assets = exc, []
    item = _matching_asset(program, assets)
    if item or named:
        return tag, item
    # Best effort from here on: a machine this project publishes nothing for is
    # not a failed lookup, and the caller's message about that is the useful
    # one. Whatever goes wrong while looking further leaves it standing.
    try:
        pointer = next((a for a in assets if a.name == NIGHTLY_TAG), None)
        if pointer:
            nightly = hub.text(pointer.url).strip()
            if nightly:
                found, assets = hub.release(program.repo, nightly, refresh=refresh)
                item = _matching_asset(program, assets)
                if item:
                    return found, item
        for found, assets in hub.releases(program.repo, refresh=refresh):
            item = _matching_asset(program, assets)
            if item:
                return found, item
    except hub.HubError:
        pass
    if missing is not None:
        raise missing
    return tag, None


def _install_record(program):
    return BIN_DIR / program.name / "installed.json"


def _read_record(program):
    """The install record, or {} however it fails to read."""
    try:
        record = json.loads(_install_record(program).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def installed_program(program):
    """The binary Dikte downloaded, or "" when there is none that still runs."""
    path = _read_record(program).get("binary") or ""
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""


def installed_version(program):
    return _read_record(program).get("tag") or ""


def vulkan_missing(program):
    """Whether what Dikte installed is the processor build on a machine the
    Vulkan one was fetched for.

    The Vulkan whisper-server is a release of Dikte's own, published by hand
    once the reviewed archive is built, and the install falls back to the
    upstream processor build whenever that release, the file in it, or its
    reviewed digest is not there. Nothing is wrong with the fallback except
    that it is invisible: a graphics card sitting idle looks exactly like a
    graphics card being used.
    """
    return (bool(installed_program(program))
            and _read_record(program).get("backend") == "processor")


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


def _binary_file(program):
    """What the program's file is called on disk here."""
    return f"{program.binary}.exe" if sys.platform == "win32" else program.binary


def _find_binary(root, name):
    for path in sorted(pathlib.Path(root).rglob(name)):
        if path.is_file():
            return path
    return None


def _extract(archive, into):
    """Unpack a release archive, refusing anything that reaches outside `into`.

    The archives lay their libraries next to their binaries and are linked with
    an $ORIGIN runpath, so a whole directory is what has to survive the trip and
    the binary cannot be lifted out of it. Linux and macOS releases come as
    tarballs, Windows ones as zips; zipfile never writes outside its target.
    """
    try:
        if str(archive).endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(into)
            return
        with tarfile.open(archive, "r:gz") as tar:
            try:
                tar.extractall(into, filter="data")
            except TypeError:      # Python without the extraction filters
                tar.extractall(into)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise LocalError(t("Could not unpack {name}: {error}",
                           name=os.path.basename(str(archive)), error=exc)) from exc


def _under(path, root):
    """Whether `path` lies inside `root`, symlinks and case resolved.

    Resolved on both sides, because the same directory can be reached under
    two spellings and this answer decides whether a server gets stopped.
    """
    try:
        pathlib.Path(path).resolve().relative_to(pathlib.Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def install_program(program, tag="", on_progress=None, should_stop=None,
                    refresh=False):
    """Fetch and unpack a release. The path to the binary, or "" when stopped.

    `tag` is empty for whatever the project released last, which is the point:
    a version pinned in Dikte's source would mean a release of Dikte every time
    whisper.cpp has one. The Linux Vulkan whisper-server is the exception, and
    _pick_asset says why.
    """
    managed = _managed_whisper(program, tag)
    try:
        tag, item = _pick_asset(program, tag, refresh=refresh)
    except hub.HubError as exc:
        raise LocalError(str(exc)) from exc

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
    fresh = into.with_name(tag + ".new")
    archive = BIN_DIR / program.name / item.name

    try:
        if not download(item, archive, on_progress, should_stop):
            return ""
        try:
            # Unpacked into a sibling and swapped in only once the binary is
            # known to be inside: a failure anywhere in here leaves the
            # previous install, and its record, exactly as they were.
            shutil.rmtree(fresh, ignore_errors=True)
            _extract(archive, fresh)
            binary = _find_binary(fresh, _binary_file(program))
            if binary is None:
                raise LocalError(t("{name} was not in the download.",
                                   name=program.binary))
            binary.chmod(binary.stat().st_mode | 0o111)
            # A running server holds its binary open, and Windows will not
            # delete an open file: whichever of our servers runs out of this
            # program's directory is stopped here, after the download and the
            # unpack are known good, so the outage is the swap and not the
            # whole transfer.
            for server in SERVERS:
                current = program_path(server.program,
                                       server.settings().get("binary", ""))
                if current and _under(current, BIN_DIR / program.name):
                    server.stop()
            if into.exists():
                try:
                    shutil.rmtree(into)
                except OSError as exc:
                    # Not ignore_errors: silently losing this would rename the
                    # new version somewhere it can never land, and the user can
                    # actually fix it by closing whatever holds the directory.
                    raise LocalError(t(
                        "Could not replace {path}: a file in it is still "
                        "open: {error}", path=into, error=exc)) from exc
            fresh.rename(into)
        except BaseException:
            # Half an unpacked sibling is not worth keeping, and the swap
            # never ran, so the previous install is still whole.
            shutil.rmtree(fresh, ignore_errors=True)
            raise
        # Found under the sibling, run from the final directory.
        binary = into / binary.relative_to(fresh)
        # Written last, so the record never points at anything half-made.
        record = {"tag": tag, "binary": str(binary)}
        if managed:
            # Which of the two builds this machine ended up with. Only written
            # where both were on offer, so an install that never had the
            # choice is not made to look like a fallback.
            record["backend"] = (
                "vulkan" if item.name.endswith(MANAGED_WHISPER_VULKAN)
                else "processor")
        _install_record(program).write_text(json.dumps(record), encoding="utf-8")
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
            if not path.is_dir() or path.name == keep:
                continue
            # A ".new" sibling belongs to an install mid-swap; housekeeping
            # must not pull it out from under it.
            if path.name.endswith(".new"):
                continue
            # ignore_errors on purpose: this is housekeeping, and a locked old
            # version is a little wasted disk rather than a failed install.
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# --- what this machine can run --------------------------------------------


def total_memory():
    """Bytes of memory on this machine, or 0 when it cannot be read.

    Zero is a real answer and not a failure: every caller treats an unknown
    machine as one big enough for whatever it is looking at, because a wrong
    "too big" is worse advice than none.

    Read once and kept. The memory in a machine does not change while Dikte
    runs, and a list of thirty rows asks this question seventy times: on the
    Mac path below, where the answer comes from a program rather than a
    library call, that was seventy processes started on the interface thread
    every time a list was drawn.
    """
    global _MEMORY
    if _MEMORY is None:
        _MEMORY = max(_read_memory(), 0)
    return _MEMORY


def _read_memory():
    """What the system says, which on a bad day is a negative number.

    sysconf answers -1 for a limit it holds to be indeterminate, and CPython
    hands that straight back rather than raising, so the product below can
    come out negative. The caller floors it at zero, which is the answer for
    a machine nothing could be read from: a 64 GB workstation whose sysconf
    shrugged was otherwise being told every model past 512 MB was too big
    for it.
    """
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        pass
    if sys.platform == "darwin":
        # Not every build of Python on a Mac has SC_PHYS_PAGES in its sysconf
        # table, and this is the number the system itself is asked for.
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], check=True,
                                 capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0
    if sys.platform != "win32":
        return 0

    class Status(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    except (AttributeError, OSError, ValueError):
        pass
    return 0


def accelerator():
    """The graphics interface this machine offers, or "".

    The machine's half of the answer only. Whether a card is actually reached
    also depends on which build landed, and the program line above says that:
    a processor build ignores the card whatever is installed here. What this
    is for is the other half, which nothing else on the window says at all.
    """
    if sys.platform == "darwin":
        return "Metal"
    return "Vulkan" if _has_vulkan() else ""


def memory_budget(memory=None):
    """What a model may weigh on this machine, or 0 when that is unknown.

    Floored rather than allowed to reach zero: on a 2 GB machine the share
    less the overhead is nothing at all, and a budget of nothing is the same
    number this returns for a machine it could not read, which would turn the
    tightest machine there is into the one where everything is offered.
    """
    memory = total_memory() if memory is None else memory
    if not memory:
        return 0
    return max(int(memory * MEMORY_SHARE) - MEMORY_OVERHEAD, MEMORY_FLOOR)


def fits(size, memory=None):
    """Whether a model of this size is worth offering on this machine."""
    budget = memory_budget(memory)
    return not budget or size <= budget


def suggested_whisper(memory=None, graphics=None):
    """The whisper model to point at here, by name.

    Three machines. One with no room, which gets the model that leaves some.
    One with a card and memory to spare, which gets the accurate model, because
    the several times the work it is per second is several times a fraction of
    a second there. Everything in between gets turbo, which is the answer
    almost every time somebody asks.

    A Vulkan or Metal loader is not proof of a fast card, so the accurate model
    waits on the memory as well: a machine with 16 GB in it and a driver
    installed is one that will not notice either way.
    """
    memory = total_memory() if memory is None else memory
    graphics = accelerator() if graphics is None else graphics
    if memory and memory < SMALL_MACHINE:
        return SMALL_MACHINE_WHISPER
    if graphics and memory >= ROOMY_MACHINE:
        return ACCURATE_WHISPER
    return SUGGESTED_WHISPER


def suggested_llm(memory=None):
    """The suggested cleanup repositories, the ones that fit here first.

    The order they are written in is the order they are worth having. What
    this changes is only which of them a machine that cannot hold the best one
    is shown first, and nothing is dropped: a model that does not fit today
    fits once something else is closed.
    """
    return sorted(SUGGESTED_LLM,
                  key=lambda repo: not fits(SUGGESTED_LLM_SIZE.get(repo, 0),
                                            memory))


def recommended(items, want="", memory=None):
    """The one row out of `items` worth pointing at here, or "".

    `want` is taken when it is on offer and fits. Without it, which is the
    cleanup list, the smallest file that does is taken: q4 is where these
    lists start, and every rung above it is roughly twice the memory and twice
    the wait for a difference neither dictation nor cleanup can see. The
    16-bit weights are left out for the same reason, twice over.
    """
    fitting = [i for i in items if fits(i.size, memory)]
    if want and any(i.name == want for i in fitting):
        return want
    usable = [i for i in fitting
              if not any(mark in i.name.lower() for mark in SIXTEEN_BIT)]
    return min(usable, key=lambda i: i.size).name if usable else ""


# --- the models -----------------------------------------------------------


def bit_depth(name):
    """The bits per weight the file name says, or 0 when it says nothing."""
    lowered = name.lower()
    for mark, bits in BIT_DEPTHS:
        if mark in lowered:
            return bits
    return 0


def whisper_family(name):
    """The model a whisper file belongs to: ggml-small.en-q5_1.bin is `small`.

    The list arrives sorted by size and nothing else, which interleaves the
    families: `large-v3-turbo-q5_0` lands between the two `medium`
    quantisations, half a screen from the turbo model it is a copy of. Grouping
    is what puts the choice between models above the choice of quantisation,
    which is the order somebody actually makes them in.
    """
    stem = name
    if stem.startswith(WHISPER_PREFIX):
        stem = stem[len(WHISPER_PREFIX):]
    if stem.endswith(WHISPER_SUFFIX):
        stem = stem[:-len(WHISPER_SUFFIX)]
    head, _, last = stem.rpartition("-")
    # q5_0, q5_1, q8_0. `turbo` is the other thing a last chunk can be, and it
    # is part of the model's name rather than a quantisation of it.
    if head and last.startswith("q") and last[1:].replace("_", "").isdigit():
        stem = head
    return stem[:-len(ENGLISH_ONLY)] if stem.endswith(ENGLISH_ONLY) else stem


def whisper_groups(items):
    """[(family, [Item])] for a whisper list: one group per model.

    Groups by how big the model gets rather than by a ladder written down
    here, so a family published next year sorts itself. Inside one, the
    multilingual files come before the English-only ones and the small
    quantisations before the large.
    """
    groups = {}
    for item in items:
        groups.setdefault(whisper_family(item.name), []).append(item)
    ordered = sorted(groups.items(),
                     key=lambda pair: (max(i.size for i in pair[1]), pair[0]))
    return [(family, sorted(files,
                            key=lambda i: (ENGLISH_ONLY in i.name, i.size)))
            for family, files in ordered]


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


def can_clean(repo):
    """Whether a repository could hold a model cleanup can be started on.

    By name, because the alternative is a file listing per repository and the
    list is forty of them. It catches the kinds that are never a cleanup model
    rather than the ones that are too big, which the file sizes answer exactly
    once a publisher is chosen.
    """
    lowered = repo.lower()
    return not any(mark.lower() in lowered for mark in LLM_REPO_SKIP)


def llm_repos(refresh=False):
    """Repository ids for the GGUF models on offer, suggestions first."""
    try:
        found = [r.id for r in hub.repos(author=LLM_AUTHOR, refresh=refresh)
                 if can_clean(r.id)]
    except hub.HubError:
        # A menu rather than a catalogue: with nothing to show, the suggestions
        # are still worth showing, and whatever is wrong with the network will
        # say so where it matters, when a download is asked for.
        found = []
    if not found:
        return list(SUGGESTED_LLM)
    # Gemma publishes its base models under the instruction-tuned one's name
    # with the `-it` taken out, so the two sit next to each other in the list
    # and the wrong one answers a cleanup prompt by carrying on writing the
    # transcript. Dropped only where the tuned sibling is here to drop it for.
    tuned = set(found)
    found = [r for r in found
             if not r.endswith("-GGUF")
             or r[:-len("-GGUF")] + "-it-GGUF" not in tuned]
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


def _win_image_name(pid):
    """The full, lower-cased path of the process's executable, or ''.

    The full path rather than the base name, because the name alone is anyone's
    whisper-server.exe and this answer decides what gets killed. MAX_PATH is a
    convention rather than a limit, so the buffer grows until the query fits.
    """
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        length = 260
        while length <= 32768:
            buffer = ctypes.create_unicode_buffer(length)
            size = ctypes.c_uint32(len(buffer))
            ok = kernel32.QueryFullProcessImageNameW(
                ctypes.c_void_p(handle), 0, buffer, ctypes.byref(size))
            if ok:
                return buffer.value.lower()
            if ctypes.get_last_error() != 122:   # ERROR_INSUFFICIENT_BUFFER
                return ""
            length *= 2
        return ""
    finally:
        kernel32.CloseHandle(handle)


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
        # The pid this instance last wrote to its pid file, so _forget never
        # removes a file some other Dikte wrote after us.
        self._pid = 0

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
            # _stop_now rather than stop(): this thread already holds
            # _starting, and the public stop() waits for it.
            self._stop_now()
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
                        # No console window of its own on Windows.
                        creationflags=paths.NO_WINDOW,
                    )
            except OSError as exc:
                raise LocalError(t("Could not start {name}: {error}",
                                   name=self.program.name, error=exc)) from exc

            # Written before it is ready rather than after, so that a kill
            # during the model load leaves something for the sweep to find.
            self._remember(proc.pid)
            began = time.monotonic()
            try:
                reason, listened = self._wait_ready(proc, port)
            except BaseException:
                # Whatever went wrong while waiting, the process is ours and
                # nothing else is left holding a reference to it. Leaving it
                # running would leak a loaded model with nobody to ask it
                # anything, which is the whole failure this class is careful
                # about elsewhere.
                self._kill(proc)
                self._forget()
                raise
            if reason == "ready":
                return proc, port, str(log)
            last = _tail(log)
            self._forget()
            # Losing the port between the probe and the bind is the one
            # failure another port fixes, and it has a shape rather than a
            # message: the child died at once without the port ever having
            # answered as its own. Grepping the log for "bind" would tie this
            # to one program's wording in one language.
            early = time.monotonic() - began < EARLY_EXIT_WINDOW
            if reason != "exited" or listened or not early:
                break
        raise LocalError(t("{name} did not start: {error}",
                           name=self.program.binary, error=last or t("no output")))

    def _wait_ready(self, proc, port):
        """("ready" | "exited" | "timeout", whether the port answered as ours).

        whisper binds after the model is loaded, so the open port is the
        answer; llama binds first and answers /health with 503 until it is
        ready. For the health-less case the open port alone is not proof: a
        child that lost the bind race exits at once while the winner keeps the
        port open, so "ready" also wants our child alive a beat after the port
        was first seen open.
        """
        deadline = time.monotonic() + STARTUP_TIMEOUT
        seen_open = False
        listened = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return "exited", listened
            if seen_open:
                # Port open on the last pass and our child still alive now:
                # an imposter's port would have left our child dead by here.
                return "ready", True
            if _listening(port):
                if self.program.health:
                    # llama did the binding itself, so the port is its.
                    listened = True
                    if _healthy(port, self.program.health):
                        return "ready", True
                else:
                    seen_open = True
            time.sleep(0.1)
        self._kill(proc)
        return "timeout", listened

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
        # Taking _starting means a stop cannot slide past a launch in flight:
        # serve() finishes registering its child first, and the child is then
        # killed here rather than surviving the shutdown unowned.
        with self._starting:
            self._stop_now()

    def _stop_now(self):
        """stop() for a thread that already holds _starting."""
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
        with self._lock:
            self._pid = pid
        try:
            path = self._pid_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(pid))
        except OSError:
            pass      # the sweep is a safety net, not something to fail a run over

    def _forget(self, pid=None):
        """Remove the pid file, but only while it still holds our own pid.

        Another Dikte started after us writes its pid over ours, and removing
        that file would hide its server from every future sweep.
        """
        if pid is None:
            with self._lock:
                pid = self._pid
        try:
            if int(self._pid_file().read_text().strip()) == pid:
                self._pid_file().unlink()
        except (OSError, ValueError):
            pass

    def _is_ours(self, pid):
        """True: still our server. False: definitely not. None: cannot tell now.

        Asked because pids are handed out again: by the time anyone looks, the
        number could belong to something else entirely, and killing it would be
        a good deal worse than the leak being cleaned up. The tri-state matters
        for the pid file: a definitive "not ours" means the file is stale and
        safe to drop, while "cannot tell" means it has to stay so a later start
        can ask again.

        On Linux the program name alone could be somebody else's copy; the name
        together with Dikte's own data directory on the command line could not.
        Windows offers no command line to read, so the executable's full path
        is the answer there: under our bin directory, or exactly the binary the
        settings point this server at. Never the base name alone, which is
        anyone's whisper-server.exe.
        """
        if sys.platform == "win32":
            path = _win_image_name(pid)
            if not path:
                # OpenProcess said nothing: the process may be gone or merely
                # unreadable from here, and the difference decides whether the
                # pid file may be dropped, so no verdict rather than a wrong one.
                return None
            path = os.path.normcase(path)
            if path.startswith(os.path.normcase(str(BIN_DIR)) + os.sep):
                return True
            with self._lock:
                custom = self._settings.get("binary", "")
            configured = program_path(self.program, custom)
            if configured:
                resolved = os.path.normcase(str(pathlib.Path(configured).resolve()))
                if path == resolved:
                    return True
            return False
        try:
            blob = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            return False          # the process is definitively gone
        except OSError:
            return None           # /proc would not answer just now
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
        owned = self._is_ours(pid)
        if owned is None:
            # Could not be verified rather than known stale: the file stays,
            # so the next start asks again instead of losing track of a server
            # that may still be holding a model.
            return False
        if not owned:
            self._forget(pid)
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            self._forget(pid)
            return False
        self._forget(pid)
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
