"""Local, API-key-free speech recognition through whisper.cpp."""

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from i18n import t

MODEL_NAME = "ggml-large-v3-turbo-q5_0.bin"
MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
    f"{MODEL_NAME}?download=true"
)
MODEL_SHA1 = "e050f7970618a659205450ad97eb95a18d69c9ee"


class LocalWhisperError(Exception):
    pass


def default_model_path():
    if sys.platform == "darwin":
        root = pathlib.Path.home() / "Library/Application Support/Dikte"
    else:
        root = pathlib.Path(
            os.environ.get("XDG_DATA_HOME")
            or pathlib.Path.home() / ".local/share"
        ) / "dikte"
    return root / "models" / MODEL_NAME


def executable():
    """Return the whisper.cpp CLI installed by Homebrew or a manual build."""
    for name in ("whisper-cli", "whisper-cpp", "main"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def status(model_path=""):
    binary = executable()
    model = pathlib.Path(os.path.expanduser(model_path or str(default_model_path())))
    if not binary:
        return t("whisper.cpp is not installed.")
    if not model.is_file():
        return t("Local Whisper is installed; the model has not been downloaded.")
    return t("Local Whisper is ready (no API key).")


def install_recommended(progress=None):
    """Install whisper.cpp on macOS when needed, then download the model.

    This is only called from the explicit Install button in Settings.
    """
    binary = executable()
    if not binary:
        brew = shutil.which("brew")
        if sys.platform != "darwin" or not brew:
            raise LocalWhisperError(t("Install whisper.cpp first, then try again."))
        run = subprocess.run(
            [brew, "install", "whisper-cpp"],
            capture_output=True, text=True, timeout=900, check=False,
        )
        if run.returncode != 0:
            raise LocalWhisperError(
                t("Could not install whisper.cpp: {error}",
                  error=(run.stderr or run.stdout).strip()[-800:])
            )

    destination = default_model_path()
    if destination.is_file() and _sha1(destination) == MODEL_SHA1:
        if progress:
            progress(destination.stat().st_size, destination.stat().st_size)
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        request = urllib.request.Request(
            MODEL_URL, headers={"User-Agent": "Dikte local Whisper installer"}
        )
        with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
        if _sha1(partial) != MODEL_SHA1:
            raise LocalWhisperError(t("The downloaded Whisper model failed verification."))
        partial.replace(destination)
        return destination
    except (OSError, urllib.error.URLError) as exc:
        raise LocalWhisperError(
            t("Could not download the Whisper model: {error}", error=exc)
        ) from exc
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def transcribe(wav_path, model_path="", language="auto", prompt="", timeout=600):
    segments = transcribe_segments(
        wav_path, model_path=model_path, language=language,
        prompt=prompt, timeout=timeout,
    )
    text = " ".join(piece for _, _, piece in segments).strip()
    if not text:
        raise LocalWhisperError(t("Transcript came back empty."))
    return text


def transcribe_segments(wav_path, model_path="", language="auto", prompt="",
                        timeout=600):
    binary = executable()
    if not binary:
        raise LocalWhisperError(
            t("whisper.cpp is not installed. Open Settings → API and models "
              "and install Local Whisper.")
        )
    model = pathlib.Path(os.path.expanduser(model_path or str(default_model_path())))
    if not model.is_file():
        raise LocalWhisperError(
            t("The local Whisper model is missing. Open Settings → API and "
              "models and click Install local Whisper.")
        )

    with tempfile.TemporaryDirectory(prefix="dikte-whisper-") as workdir:
        output = os.path.join(workdir, "transcript")
        command = [
            binary,
            "-m", str(model),
            "-f", str(wav_path),
            "-l", language if language and language != "auto" else "auto",
            "-oj",
            "-of", output,
            "-np",
        ]
        if prompt:
            command += ["--prompt", prompt]
        try:
            run = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LocalWhisperError(
                t("Could not run Local Whisper: {error}", error=exc)
            ) from exc
        if run.returncode != 0:
            error = (run.stderr or run.stdout).strip()
            raise LocalWhisperError(
                t("Local Whisper failed: {error}",
                  error=error[-1200:] or run.returncode)
            )
        try:
            with open(output + ".json", encoding="utf-8") as fh:
                return parse_result(json.load(fh))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise LocalWhisperError(
                t("Could not read Local Whisper's result: {error}", error=exc)
            ) from exc


def parse_result(payload):
    """Convert whisper-cli JSON to [(start_seconds, end_seconds, text)]."""
    out = []
    for row in payload.get("transcription") or []:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        offsets = row.get("offsets") or {}
        start = float(offsets.get("from") or 0) / 1000.0
        end = float(offsets.get("to") or 0) / 1000.0
        out.append((start, max(start, end), text))
    if not out:
        raise ValueError("empty transcription")
    return out


def _sha1(path):
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
