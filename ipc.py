"""The socket the running instance listens on, and one request over it.

A command typed at a terminal is answered rather than only obeyed: the reply
carries the transcript, the agent's answer, or the reason nothing happened,
which is what lets a script wait for a dictation instead of guessing when it is
done. One JSON object goes each way per connection. A bare verb is still
understood, because that is what earlier versions sent and what a stale KDE
shortcut may still send.
"""

import json
import hashlib
import os
import sys

from PyQt6.QtNetwork import QLocalSocket

def _windows_sid():
    """The current Windows account SID, or an empty string on API failure."""
    try:
        import ctypes
        from ctypes import wintypes

        token_query = 0x0008
        token_user = 1
        token = wintypes.HANDLE()
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)):
            return ""
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, token_user, None, 0,
                                         ctypes.byref(size))
            if not size.value:
                return ""
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                    token, token_user, buffer, size, ctypes.byref(size)):
                return ""

            class TokenUser(ctypes.Structure):
                _fields_ = [("sid", ctypes.c_void_p),
                            ("attributes", wintypes.DWORD)]

            sid = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents.sid
            text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return ""
            try:
                return text.value or ""
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError, TypeError, ValueError):
        return ""


def user_id(platform=None):
    """A filesystem/pipe-safe token that is stable for the current user."""
    platform = platform or sys.platform
    if not platform.startswith("win"):
        return str(os.getuid())
    identity = (_windows_sid() or os.environ.get("USERNAME")
                or os.environ.get("USER") or "dikte")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


SERVER_NAME = "dikte-" + user_id()

# Long enough for a process that is already running to answer, short enough that
# "nothing is running" is not a noticeable pause in front of a key press.
CONNECT_MS = 800


def script_path():
    return os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "dikte.py")
    )


def command_for(verb):
    """The command line a KDE shortcut runs for one of the verbs."""
    return f"{sys.executable} {script_path()} {verb}"


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
