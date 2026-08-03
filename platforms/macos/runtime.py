"""macOS directories, process lifetime and session integration.

Audio, clipboard and global shortcuts remain in their established native
implementations while the cross-platform runtime contract is shared with the
Windows port.
"""

import contextlib
import os
import pathlib
import shutil
import signal
import socket
import subprocess

NO_WINDOW = {}
PROGRAMS = ("ffmpeg",)
APP_DIR = "Dikte"
RECORDINGS_NAME = "recordings"
MEETINGS_NAME = "meetings"


def config_home():
    return pathlib.Path.home() / "Library/Application Support"


def data_home():
    return config_home()


def cache_home():
    return pathlib.Path.home() / "Library/Caches"


def config_dir():
    return config_home() / APP_DIR


def data_dir():
    return data_home() / APP_DIR


def cache_dir():
    return cache_home() / APP_DIR


def user_id():
    return str(os.getuid())


class _Held:
    def release(self):
        pass


def single_instance(name):
    return _Held()


def protect(text):
    return text


def unprotect(value):
    return value


def secure_file(path):
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def adopt(proc):
    return False


def is_our_process(pid, needles):
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and all(
        str(needle) in result.stdout for needle in needles)


def terminate(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def signals():
    return (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def wakeup_socketpair():
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    return reader, writer


def cuda_driver_version():
    return None


def prepare_console():
    return False


def abort_socket(sock):
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    return True


def relaunch(command):
    os.execv(command[0], list(command))


def open_folder(path):
    if not shutil.which("open"):
        return False
    try:
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True
