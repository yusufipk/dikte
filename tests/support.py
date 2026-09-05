"""What the tests share: a throwaway home, a fake network, small WAV files.

Two things about this codebase shape all of it. Paths are module-level constants
resolved at import time, so they are replaced object by object rather than
re-derived by reloading the module, which would hand every other module a second
copy of it. And the only way out to the network is urllib, so faking one function
is enough to run the whole chain offline.
"""

import array
import contextlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import wave
from unittest import mock

from dikte import assistant
from dikte import config as cfg
from dikte import ggml
from dikte import i18n
from dikte import update

# What the application is, rather than what it does: PipeWire, wl-clipboard,
# ydotool, KDE's shortcut file, /dev/input. A port to another desktop replaces
# all of it, and the tests that pin this half say so rather than failing on a
# machine that never had any of it.
#
# Everything else is expected to pass everywhere, and that is the line worth
# holding: transcription, cleanup, the config file, the history, the agent, the
# command line and the timeline of a meeting are not desktop-specific and must
# not become so.
linux_only = unittest.skipUnless(
    sys.platform.startswith("linux"),
    "covers the Linux desktop stack (PipeWire, wl-clipboard, ydotool, KDE)",
)

# The launchers a downloaded build writes for itself. There are two downloads,
# an AppImage and a disk image, so `integrate` has a Linux half and a macOS half
# and no third one, and the tests that pin them stand in a home laid out the way
# those two systems lay one out: paths that start at the root, a $HOME the
# library reads, a symlink for the command. None of that is a Windows machine,
# where the same code never runs. A Windows build would add an entry there and
# take the mark off these.
posix_only = unittest.skipIf(
    sys.platform == "win32",
    "covers what an AppImage and a .app write into the desktop they landed on",
)


def _no_network(*args, **kwargs):
    raise AssertionError(
        "a test reached the network; wrap the call in support.fake_urlopen"
    )


def _no_exec(*args, **kwargs):
    raise AssertionError(
        "a test reached os.execv, which would replace the test process with the "
        "application; patch cli.launch_gui instead"
    )


class DikteTest(unittest.TestCase):
    """A test that owns its config, its data directory and its language."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="dikte-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

        config_dir = self.path("config", "dikte")
        data_dir = self.path("data", "dikte")
        self.patch_paths(
            CONFIG_DIR=config_dir,
            CONFIG_FILE=config_dir / "config.json",
            DATA_DIR=data_dir,
            HISTORY_FILE=data_dir / "history.jsonl",
            RECORDINGS_DIR=data_dir / "recordings",
            MEETINGS_DIR=data_dir / "meetings",
            MEETINGS_FILE=data_dir / "meetings.jsonl",
        )
        # Resolved from cfg.DATA_DIR when assistant was imported, so it needs
        # moving on its own. The same goes for where the update check writes
        # down when it last ran.
        self.patch_attr(assistant, "SESSION_FILE", data_dir / "assistant.json")
        self.patch_attr(update, "STATE_FILE", data_dir / "update.json")
        # ggml resolves its own three from paths.DATA_DIR at import, the same
        # way cfg does. Left alone, a test asking what is installed or what the
        # last server ran on would be reading whatever this machine happens to
        # have downloaded, and passing or failing on somebody's home directory.
        # program_path prefers a whisper-server or llama-server on the PATH
        # over the copy Dikte downloaded, so on a machine with whisper.cpp
        # installed these tests would be answering from that copy instead of
        # from the install they set up. Every other tool still resolves; the
        # tests that are about the system build patch this again themselves.
        _which = shutil.which
        self.patch_attr(shutil, "which", lambda tool, *args, **rest: (
            None if tool in ("whisper-server", "llama-server")
            else _which(tool, *args, **rest)))
        self.patch_attr(ggml, "DATA_DIR", data_dir)
        self.patch_attr(ggml, "BIN_DIR", data_dir / "bin")
        self.patch_attr(ggml, "MODELS_DIR", data_dir / "models")

        i18n.set_language("en")
        self.addCleanup(i18n.set_language, "en")

        # cli.launch_gui replaces this process with the application when no
        # instance is running. A test that reaches it would take the whole run
        # with it and hang, so it fails loudly here instead.
        self.patch_attr(os, "execv", _no_exec)

        # Every way out of here goes through urllib, so closing it is enough to
        # keep the suite offline. A test that means to answer a request patches
        # this again through fake_urlopen.
        self.patch_attr(urllib.request, "urlopen", _no_network)

    # ---- helpers ---------------------------------------------------------

    def path(self, *parts):
        """A path inside this test's directory, as a pathlib.Path."""
        import pathlib
        return pathlib.Path(self.root, *parts)

    def patch_paths(self, **paths):
        patcher = mock.patch.multiple(cfg, **paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def patch_attr(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def config(self, **values):
        """A Config with nothing stored, then the given settings applied."""
        conf = cfg.Config()
        for key, value in values.items():
            conf[key] = value
        return conf

    def write_config(self, payload):
        """Put a config.json on disk, the way an older version would have."""
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg.CONFIG_FILE.write_text(json.dumps(payload), encoding="utf-8")

    def read_config_file(self):
        return json.loads(cfg.CONFIG_FILE.read_text(encoding="utf-8"))


# --- the network ----------------------------------------------------------


def json_body(payload):
    """A stand-in for what urlopen hands back: a context manager that reads."""
    body = json.dumps(payload).encode("utf-8")
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def raw_body(text):
    """The same, for a reply that is not valid JSON."""
    resp = mock.MagicMock()
    resp.read.return_value = text.encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def http_error(code, body=""):
    return urllib.error.HTTPError(
        "https://example.invalid/v1", code, "boom", {},
        io.BytesIO(body.encode("utf-8")),
    )


def url_error(reason="no route to host"):
    return urllib.error.URLError(reason)


@contextlib.contextmanager
def fake_urlopen(*replies):
    """Answer each call with the next reply; the last one repeats.

    A reply is a payload to encode as JSON, an exception to raise, or an object
    already shaped like a response. The requests are collected so a test can
    check what was actually sent.
    """
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        reply = replies[min(len(calls) - 1, len(replies) - 1)] if replies else {}
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, (dict, list)):
            return json_body(reply)
        return reply

    try:
        with mock.patch("urllib.request.urlopen", side_effect=opener):
            yield calls
    finally:
        # An HTTPError holds a file object and complains when it is collected
        # without one; the tests raise the same one more than once, so closing
        # it is the caller's job rather than the code's.
        for reply in replies:
            if isinstance(reply, urllib.error.HTTPError):
                reply.close()


def sent_json(request):
    """The JSON body of a recorded request."""
    return json.loads(request.data.decode("utf-8"))


def multipart_fields(request):
    """{name: value} for the plain fields of a recorded multipart request."""
    body = request.data.decode("utf-8", "replace")
    fields = {}
    for part in body.split("\r\n--"):
        if 'name="' not in part or "filename=" in part:
            continue
        name = part.split('name="', 1)[1].split('"', 1)[0]
        _, _, value = part.partition("\r\n\r\n")
        fields[name] = value.rstrip("\r\n")
    return fields


# --- audio ----------------------------------------------------------------


def pcm(samples):
    return array.array("h", samples).tobytes()


def tone(seconds, rate=16000, amplitude=8000, channels=1, freq=440.0):
    """Interleaved s16 samples for a sine wave, the same on every channel."""
    frames = int(seconds * rate)
    out = array.array("h")
    for index in range(frames):
        value = int(amplitude * math.sin(2 * math.pi * freq * index / rate))
        out.extend([value] * channels)
    return out.tobytes()


def silence(seconds, rate=16000, channels=1):
    return b"\x00\x00" * int(seconds * rate) * channels


def speech(seconds, rate=16000, amplitude=16000, freq=440.0):
    """A buffer the silence check reads as somebody talking.

    A steady tone does not, however loud it is: the check is relative, and a
    level that never moves is its own noise floor. Speech is quiet, then loud,
    which is what the pauses between words make it.
    """
    half = seconds / 2
    return silence(half, rate) + tone(half, rate, amplitude, freq=freq)


def make_wav(path, data, rate=16000, channels=1, width=2):
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with contextlib.closing(wave.open(str(path), "wb")) as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(data)
    return str(path)


def stereo(left, right):
    """Interleave two equal-length mono buffers into one stereo buffer."""
    a, b = array.array("h"), array.array("h")
    a.frombytes(left)
    b.frombytes(right)
    out = array.array("h")
    for first, second in zip(a, b):
        out.extend((first, second))
    return out.tobytes()


# --- processes ------------------------------------------------------------


class FakeCompleted:
    """What subprocess.run hands back, as much of it as the code reads."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def only_these_tools(*names):
    """shutil.which answers for the named tools and nothing else."""
    wanted = set(names)
    return mock.patch("shutil.which", side_effect=lambda tool: (
        f"/usr/bin/{tool}" if tool in wanted else None))
