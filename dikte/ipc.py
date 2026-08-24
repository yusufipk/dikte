"""The socket the running instance listens on, and one request over it.

A command typed at a terminal is answered rather than only obeyed: the reply
carries the transcript, the agent's answer, or the reason nothing happened,
which is what lets a script wait for a dictation instead of guessing when it is
done. One JSON object goes each way per connection. A bare verb is still
understood, because that is what earlier versions sent and what a stale KDE
shortcut may still send.
"""

import json
import os
import pathlib
import shlex
import sys

from PyQt6.QtNetwork import QLocalSocket

from . import integrate

SERVER_NAME = "dikte-" + (
    str(os.getuid()) if hasattr(os, "getuid")
    else os.environ.get("USERNAME", "user"))

# Long enough for a process that is already running to answer, short enough that
# "nothing is running" is not a noticeable pause in front of a key press.
CONNECT_MS = 800


def script_path():
    """The package entry point, as a path.

    A shortcut and a relaunch both start a second process, and neither has a
    working directory to run `-m dikte` from, so the file is named outright.
    """
    return os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")
    )


def macos_bundle():
    """The .app containing this process, or None for a plain interpreter."""
    if sys.platform != "darwin":
        return None
    # This branch is macOS-only even when a cross-platform test simulates it;
    # parse it with macOS's POSIX path rules rather than the test host's rules.
    executable = pathlib.PurePosixPath(sys.executable)
    macos = executable.parent
    contents = macos.parent
    bundle = contents.parent
    if (macos.name == "MacOS" and contents.name == "Contents"
            and bundle.suffix == ".app"):
        return str(bundle)
    return None


def launcher():
    """The argv that starts Dikte again on this installation.

    An interpreter and a file is only how a checkout starts. A packaged build
    has no __main__.py on disk to name, and an AppImage is a squashfs mounted
    under a fresh /tmp path every run, so what a shortcut written today has to
    say is the .AppImage file the user keeps, not the binary inside this run's
    mount. APPIMAGE is what the runtime puts that path in.

    The Windows build is two executables over one program, and the one to start
    again is always the windowed one: `dikte toggle` typed at a terminal runs
    the console one, and the application it leaves running should no more be
    tied to that terminal than the one the Start Menu starts.
    """
    if not getattr(sys, "frozen", False):
        return [sys.executable, script_path()]
    if sys.platform == "win32":
        windowed = os.path.join(os.path.dirname(sys.executable),
                                integrate.WINDOWS_APP)
        if os.path.isfile(windowed):
            return [windowed]
    return [os.environ.get("APPIMAGE") or sys.executable]


def command_for(verb):
    """The command line a desktop's shortcut runs for one of the verbs.

    Also what Settings shows an i3 or XFCE user to paste into their own
    configuration, since there is no registry there for Dikte to write into.
    Quoted, because a Mac keeps applications under a path with a space in it
    and an AppImage lives wherever it was downloaded to.
    """
    return shlex.join(launcher() + ([verb] if verb else []))


def send(cmd, wait=False, timeout=0, **args):
    """Send one request; the reply, or None when no instance is running.

    `wait` asks the instance to hold its reply back until the job the request
    started is over, which is how a terminal gets the transcript rather than
    only the fact that recording began. `timeout` bounds that wait in seconds;
    0 waits for as long as the job takes.
    """
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(CONNECT_MS):
        return None

    request = {"cmd": cmd}
    request.update({key: value for key, value in args.items() if value is not None})
    if wait:
        request["wait"] = True
    # A verb carrying nothing goes as the bare word it used to be, so that an
    # instance still running the older code obeys it: that is the one request
    # that has to work across an update, since it is how you install the update.
    line = cmd if list(request) == ["cmd"] else json.dumps(request)
    sock.write((line + "\n").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(CONNECT_MS)

    limit = (int(timeout * 1000) if timeout else -1) if wait else CONNECT_MS
    buffer = b""
    while b"\n" not in buffer:
        if not sock.waitForReadyRead(limit):
            break
        buffer += bytes(sock.readAll())
    sock.disconnectFromServer()

    line = buffer.decode("utf-8", "replace").strip()
    if not line:
        # An instance from before replies existed answers by staying silent, and
        # for a fire-and-forget verb that silence means it went through. A wait
        # that ends this way did not: the run never reported back.
        return ({"ok": False, "legacy": True,
                 "error": "the running instance is too old to answer; "
                          "reload it with: dikte restart"}
                if wait else {"ok": True, "legacy": True})
    try:
        reply = json.loads(line)
    except json.JSONDecodeError:
        return {"ok": True, "legacy": True}
    return reply if isinstance(reply, dict) else {"ok": True, "legacy": True}
