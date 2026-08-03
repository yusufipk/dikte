"""Where things live on Linux, and how a process behaves there.

Directories follow the XDG basedir spec. A secret is a secret because of the
mode on the file holding it, the socket is named after the numeric user id,
and a session ending arrives as a signal.
"""

import contextlib
import os
import pathlib
import shutil
import signal
import socket
import subprocess

# What a subprocess is started with so that no console window appears. Nothing
# on Linux does, so there is nothing to ask for.
NO_WINDOW = {}

# The programs this platform expects to find on the PATH, for `dikte doctor` to
# report on. Recording, the clipboard, the key press and the shortcut all go
# through one of these here.
PROGRAMS = ("pw-record", "wl-copy", "ydotool", "ffmpeg", "pactl", "kwriteconfig6")

# The name of the directory Dikte keeps its things in, under each of the three
# roots below. Lowercase, because that is what the rest of ~/.config looks like,
# and so are the two directories inside it.
APP_DIR = "dikte"
RECORDINGS_NAME = "recordings"
MEETINGS_NAME = "meetings"


def _home(var, default):
    return pathlib.Path(os.environ.get(var) or os.path.expanduser(default))


def config_home():
    return _home("XDG_CONFIG_HOME", "~/.config")


def data_home():
    return _home("XDG_DATA_HOME", "~/.local/share")


def cache_home():
    return _home("XDG_CACHE_HOME", "~/.cache")


def config_dir():
    return config_home() / APP_DIR


def data_dir():
    return data_home() / APP_DIR


def cache_dir():
    return cache_home() / APP_DIR


# --- who this is ----------------------------------------------------------


def user_id():
    """A token for the current user, to keep one user's socket out of another's."""
    return str(os.getuid())


def single_instance(name):
    """A handle proving this is the only Dikte running, or None when it is not.

    On Linux the local socket already answers that question: a second instance
    fails to bind it and talks to the first one instead. Nothing further is
    needed, so this always succeeds and the caller falls through to the socket.
    """
    return _Held()


class _Held:
    def release(self):
        pass


# --- secrets --------------------------------------------------------------


def protect(text):
    """What to write into config.json for an API key.

    On Linux the file itself is the protection: mode 600 in the user's own
    config directory, which is where every other program on the machine keeps
    the same kind of thing.
    """
    return text


def unprotect(value):
    return value


def secure_file(path):
    """Keep a file holding secrets to its owner."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


# --- processes ------------------------------------------------------------


def adopt(proc):
    """Tie a child's lifetime to ours. Nothing to do here.

    Linux has no job objects, and the pid file plus ggml.sweep() is what covers
    a Dikte that was killed outright.
    """
    return False


def is_our_process(pid, needles):
    """Whether that pid is a process whose command line holds all of `needles`.

    Asked because pids are handed out again: by the time anyone looks, the
    number could belong to something else entirely, and killing it would be a
    good deal worse than the leak being cleaned up.
    """
    try:
        blob = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return all(str(needle).encode() in blob for needle in needles)


def terminate(pid):
    """Ask a process to end. True when the signal went out."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


# --- the session ----------------------------------------------------------


def signals():
    """The signals a session end arrives as."""
    return (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def wakeup_socketpair():
    """A pair of sockets for set_wakeup_fd, so Qt hears a signal while blocked."""
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    return reader, writer


def cuda_driver_version():
    """Nothing to ask: neither project publishes a CUDA build for Linux.

    A graphics card is reached through Vulkan here, and a Vulkan build either
    finds a loader or does not; there is no runtime version to match up.
    """
    return None


def prepare_console():
    """Nothing to do: a Linux terminal has been UTF-8 for twenty years."""
    return False


def abort_socket(sock):
    """Make a read another thread is blocked on return, now.

    A half-close is enough here: the reader wakes with nothing left to read and
    the call comes back rather than waiting for bytes that are no longer on
    their way.
    """
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    return True


def relaunch(command):
    """Start Dikte again in place of this process. Does not return."""
    os.execv(command[0], list(command))


def open_folder(path):
    """Show a directory in the file manager. True when something was launched."""
    if not shutil.which("xdg-open"):
        return False
    try:
        subprocess.Popen(["xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True
