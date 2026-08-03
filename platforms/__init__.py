"""Which operating system this is, and the adapter that speaks to it.

Everything Dikte does that a desktop can refuse is written once per operating
system under here, behind one contract each: capturing a microphone, owning the
clipboard, pressing a key in somebody else's window, registering a shortcut the
whole session sees, naming a socket only this user may open.

The modules above this line (audio.py, paste.py, hotkey.py, config.py) are the
contract. They pick an adapter at import and hand its functions on under the
names the rest of Dikte has always called them by, so worker.py, meeting.py and
dikte.py never learn which desktop they are on.

Four parts, the same four on every platform:

    audio       capturing the microphone and what the speakers are playing
    clipboard   owning the clipboard and pressing a key combination
    hotkeys     a shortcut the session delivers even when Dikte has no focus
    runtime     directories, secrets, process lifetime, single instance

An adapter may leave out what its platform has no answer for; the contract
module is where that is turned into a refusal the interface can show.
"""

import importlib
import sys

WINDOWS = "windows"
LINUX = "linux"

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

# Nothing else is ported. macOS and the BSDs land on the Linux adapters, which
# is right for the parts that go through POSIX and wrong for the parts that go
# through PipeWire; the failure is then a missing program with its name in the
# message rather than an import error at startup.
NAME = WINDOWS if IS_WINDOWS else LINUX

_loaded = {}


def adapter(part, name=""):
    """The module implementing `part`, on this platform unless another is named.

    Imported on demand and remembered: a test that asks for the other
    platform's adapter gets it without the import cost being paid twice, and a
    machine that never records never imports the sound library.

    `NAME` is read here rather than baked into the signature, so that a test
    can point the whole application at the other platform's adapters and load
    them: half of what a port breaks is only visible from the other side.
    """
    name = name or NAME
    key = (name, part)
    if key not in _loaded:
        _loaded[key] = importlib.import_module(f"platforms.{name}.{part}")
    return _loaded[key]


def take(part, *names, name=""):
    """The named attributes of an adapter, for a contract module to re-export.

    Missing ones come back as None rather than raising, so that a contract can
    offer what this platform has and answer for what it does not.
    """
    module = adapter(part, name)
    return tuple(getattr(module, attribute, None) for attribute in names)
