"""The socket the running instance listens on, and one request over it.

A command typed at a terminal is answered rather than only obeyed: the reply
carries the transcript, the agent's answer, or the reason nothing happened,
which is what lets a script wait for a dictation instead of guessing when it is
done. One JSON object goes each way per connection. A bare verb is still
understood, because that is what earlier versions sent and what a stale KDE
shortcut may still send.

The name it listens on is per user on both platforms: a Unix socket in /tmp
named after the user id, a named pipe named after a hash of the Windows SID.
Neither is a lock, and on Windows two servers can hold the same pipe name, so
whether this is the only Dikte running is asked of the runtime adapter instead,
and settled there by a mutex.
"""

import json
import os
import pathlib
import sys

from PyQt6.QtNetwork import QLocalSocket

from platforms import adapter

runtime = adapter("runtime")


def _windows_sid():
    return getattr(adapter("runtime", "windows"), "_sid_string")()


def user_id(platform=None):
    platform = platform or sys.platform
    if platform.startswith("win"):
        identity = (_windows_sid() or os.environ.get("USERNAME")
                    or os.environ.get("USER") or "dikte")
        import hashlib
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return str(os.getuid())


SERVER_NAME = "dikte-" + user_id()

# Whether this is a packaged application rather than a checkout being run by a
# Python interpreter. It changes what "start Dikte again" means, and there is
# no dikte.py to point at inside a one-folder build.
FROZEN = bool(getattr(sys, "frozen", False))

# A packaged build has two faces in one directory: the tray application with no
# console, and the command line with one. Which of them is running decides both
# what a bare start means and which one a restart hands over to.
TRAY_EXE = "DikteApp.exe"
IS_TRAY = FROZEN and os.path.basename(sys.executable).lower() == TRAY_EXE.lower()

# Long enough for a process that is already running to answer, short enough that
# "nothing is running" is not a noticeable pause in front of a key press.
CONNECT_MS = 800


def script_path():
    return os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "dikte.py")
    )


def launch_command(*args):
    """The argument list that starts Dikte again, as a list to spawn.

    Packaged, the executable is the application and there is no interpreter and
    no script in front of it. From a checkout it is the interpreter running
    dikte.py, which is also what has to survive a restart after an update.
    """
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, script_path(), *args]


def gui_command(*args):
    """The argument list that starts the tray application.

    Not the same as launch_command in a packaged build: `dikte transcribe` runs
    in a console window, and having it start the tray application would leave
    that window open for as long as Dikte ran. The tray executable sits beside
    it in the same directory.
    """
    if not FROZEN:
        return launch_command(*args)
    tray = pathlib.Path(sys.executable).with_name(TRAY_EXE)
    return [str(tray) if tray.exists() else sys.executable, *args]


def _quoted(part):
    return f'"{part}"' if " " in part else part


def command_for(verb):
    """The command line a desktop shortcut runs for one of the verbs."""
    return " ".join(_quoted(part) for part in launch_command(verb))


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
