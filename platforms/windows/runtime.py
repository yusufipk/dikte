"""Where things live on Windows, and how a process behaves there.

Four things are different enough from Linux to be worth saying out loud.

Directories are the ones Windows publishes rather than XDG's: settings roam
with the user under %APPDATA%, everything large stays on the machine under
%LOCALAPPDATA%.

A mode-600 file is not protection here. NTFS has an ACL rather than a mode, and
`os.chmod` can only ever clear the read-only attribute, so an API key written in
plain text sits there readable by anything running as this user. The keys are
encrypted with DPAPI instead, which ties them to this Windows account: copied to
another machine, or read by another account, the ciphertext is worthless.

One instance is not settled by the socket. Two servers can bind the same named
pipe on Windows, so both would appear to start; a named mutex is what actually
answers the question, and the pipe is only how the two of them then talk.

A child process here can outlive its parent by default. Model servers are put
in a job object marked kill-on-close, so a Dikte that is force-quit takes the
loaded model down with it instead of leaving gigabytes resident with nothing
left alive to ask it anything.
"""

import base64
import contextlib
import ctypes
import hashlib
import os
import pathlib
import signal
import socket
import subprocess
from ctypes import wintypes

# No console window for whisper, llama, ffmpeg, Claude or Codex: without this
# every subprocess flashes a black box on screen, and a packaged application has
# no console for them to inherit in the first place.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW}

# Windows applications are named the way they are shown, and these are
# directories the user will see in Explorer.
APP_DIR = "Dikte"
RECORDINGS_NAME = "Recordings"
MEETINGS_NAME = "Meetings"

# The only outside program Windows needs. Recording, the clipboard, the key
# press and the shortcuts all happen inside this process here; ffmpeg is what
# turns somebody's .mp4 into something a transcription model will take.
PROGRAMS = ("ffmpeg",)

# Version and salt for a stored secret. The prefix is what tells a plain key
# written by an older version apart from an encrypted one, and the salt is
# extra entropy DPAPI mixes in, so that another program running as this user
# cannot decrypt Dikte's keys by asking Windows nicely with no arguments.
SECRET_PREFIX = "dpapi:1:"
SECRET_SALT = b"dikte.config.v1"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# Handles and pointers are 64 bits here and ctypes assumes a C int for anything
# nobody described to it, so an undeclared OpenProcess would hand back a
# truncated handle and every call taking it would fail for no visible reason.
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
kernel32.LocalFree.restype = wintypes.HLOCAL
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t)]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.ConvertSidToStringSidW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.restype = wintypes.BOOL

ntdll.NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG)]
ntdll.NtQueryInformationProcess.restype = ctypes.c_long

shell32.ShellExecuteW.argtypes = [
    wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = wintypes.HINSTANCE


def _home(var, fallback):
    value = os.environ.get(var)
    if value:
        return pathlib.Path(value)
    return pathlib.Path(os.path.expanduser("~")) / fallback


def config_home():
    """%APPDATA%: small settings, which roam with a domain profile."""
    return _home("APPDATA", "AppData/Roaming")


def data_home():
    """%LOCALAPPDATA%: models, recordings, history, none of which should roam."""
    return _home("LOCALAPPDATA", "AppData/Local")


def cache_home():
    return data_home()


def config_dir():
    return config_home() / APP_DIR


def data_dir():
    return data_home() / APP_DIR


def cache_dir():
    return data_dir() / "Cache"


# --- who this is ----------------------------------------------------------


class _SidHandle(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


TOKEN_QUERY = 0x0008
TokenUser = 1


def _sid_string():
    """The current user's SID as S-1-5-…, or "" when Windows will not say."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_QUERY, ctypes.byref(token)):
        return ""
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        if not size.value:
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TokenUser, buffer,
                                            size, ctypes.byref(size)):
            return ""
        sid = ctypes.cast(buffer, ctypes.POINTER(_SidHandle)).contents.Sid
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return ""
        try:
            return text.value or ""
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def user_id():
    """A token for the current user, to keep one user's pipe out of another's.

    The SID itself would do, but it is long and full of characters a pipe name
    would rather not carry, so what goes into the name is a hash of it. The
    username is the fallback: two accounts on one machine never share it.
    """
    sid = _sid_string() or os.environ.get("USERNAME", "") or "dikte"
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]


ERROR_ALREADY_EXISTS = 183


class _Mutex:
    """A named mutex held for as long as this instance runs."""

    def __init__(self, handle):
        self._handle = handle

    def release(self):
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None


def single_instance(name):
    """A handle proving this is the only Dikte running, or None when it is not.

    Local\\ rather than Global\\: the name is per session, so two users logged
    into the same machine each get their own Dikte rather than the second one
    finding the first one's mutex and quitting.
    """
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        return _Mutex(None)   # no mutex to be had; fall through to the pipe
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return _Mutex(handle)


# --- secrets --------------------------------------------------------------


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _take(blob):
    """Copy a blob Windows allocated, then give the memory back."""
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


CRYPTPROTECT_UI_FORBIDDEN = 0x01


def protect(text):
    """An API key as it should be written into config.json.

    Encrypted to this Windows account, so a config file copied off the machine
    hands over nothing. A key that cannot be encrypted is stored as it was
    rather than lost: an unreadable settings file would be the worse failure,
    and the file is still inside this user's profile.
    """
    if not text:
        return text
    data, _keepalive = _blob(text.encode("utf-8"))
    entropy, _salt = _blob(SECRET_SALT)
    out = _Blob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(data), "dikte", ctypes.byref(entropy),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        return text
    return SECRET_PREFIX + base64.b64encode(_take(out)).decode("ascii")


def unprotect(value):
    """The key behind a stored value, encrypted or not.

    A value with no prefix is a key written by a version of Dikte from before
    this, or by hand; it is returned as it stands and re-encrypted the next time
    the settings are saved.
    """
    if not value or not value.startswith(SECRET_PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(SECRET_PREFIX):], validate=True)
    except (ValueError, TypeError):
        return ""
    data, _keepalive = _blob(raw)
    entropy, _salt = _blob(SECRET_SALT)
    out = _Blob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(data), None, ctypes.byref(entropy),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        # Restored from a backup, or copied from another machine or account.
        # The key is gone rather than wrong, and saying so as an empty key sends
        # the user to the one place that can fix it.
        return ""
    return _take(out).decode("utf-8", "replace")


def secure_file(path):
    """Nothing to tighten, and nothing that would help if there were.

    `os.chmod` on Windows only ever moves the read-only attribute, so the mode
    the Linux side sets would be a comfortable-looking no-op here. What actually
    keeps the keys is that they are encrypted before they are written, plus the
    ACL %APPDATA% already carries, which denies other standard users.
    """
    return False


# --- processes ------------------------------------------------------------


JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _IoCounters(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010

_job = None


def _kill_on_close_job():
    """One job object for the whole application, made the first time it is needed."""
    global _job
    if _job is not None:
        return _job
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle, JobObjectExtendedLimitInformation,
        ctypes.byref(limits), ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(handle)
        return None
    _job = handle
    return _job


def adopt(proc):
    """Put a child in the job object, so it cannot outlive this process.

    The last handle to a kill-on-close job closing is what kills its members,
    and every handle a process holds is closed when it ends however it ends,
    including a kill from Task Manager. That is the case the pid file cannot
    cover, because nothing of ours runs to write one.
    """
    job = _kill_on_close_job()
    if job is None:
        return False
    handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE,
                                  False, int(proc.pid))
    if not handle:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(job, handle))
    finally:
        kernel32.CloseHandle(handle)


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


# Offsets into the 64-bit PEB and RTL_USER_PROCESS_PARAMETERS. Undocumented,
# but unchanged for the whole life of x64 Windows, and the alternative is
# spawning PowerShell for a question asked once at startup.
_PEB_PROCESS_PARAMETERS = 0x20
_PARAMS_COMMAND_LINE = 0x70


def _remote_command_line(handle):
    """The command line of another process, or "" when it cannot be read."""
    info = _ProcessBasicInformation()
    if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info),
                                       ctypes.sizeof(info), None) != 0:
        return ""
    if not info.PebBaseAddress:
        return ""

    def read(address, size):
        buffer = ctypes.create_string_buffer(size)
        read_bytes = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer,
                                        size, ctypes.byref(read_bytes))
        return buffer.raw if ok and read_bytes.value == size else b""

    params = read(info.PebBaseAddress + _PEB_PROCESS_PARAMETERS, 8)
    if not params:
        return ""
    params_address = int.from_bytes(params, "little")
    header = read(params_address + _PARAMS_COMMAND_LINE, 16)
    if not header:
        return ""
    length = int.from_bytes(header[0:2], "little")
    buffer_address = int.from_bytes(header[8:16], "little")
    if not length or not buffer_address:
        return ""
    raw = read(buffer_address, length)
    return raw.decode("utf-16-le", "replace") if raw else ""


def _image_path(handle):
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                               ctypes.byref(size)):
        return ""
    return buffer.value


def is_our_process(pid, needles):
    """Whether that pid is a process whose command line holds all of `needles`.

    Pids are handed out again here as well, and killing whatever inherited the
    number would be a good deal worse than the leak being cleaned up. When the
    command line cannot be read the image path is asked instead, and a needle
    that names a directory is taken to be satisfied when the program itself
    lives inside it, which for a whisper.cpp Dikte downloaded is the case.
    """
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid))
    if not handle:
        return False
    try:
        line = _remote_command_line(handle)
        if line:
            lowered = line.lower()
            return all(str(needle).lower() in lowered for needle in needles)
        image = _image_path(handle).lower()
        if not image:
            return False
        return all(str(needle).lower() in image for needle in needles)
    finally:
        kernel32.CloseHandle(handle)


def terminate(pid):
    """Ask a process to end. Windows has no SIGTERM, so this is the kill."""
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


# --- the session ----------------------------------------------------------


def signals():
    """The signals a session end arrives as.

    No SIGHUP on Windows. SIGBREAK is what Ctrl+Break and a console shutdown
    send, and a logout arrives as WM_ENDSESSION, which Qt turns into quitting
    the application on its own.
    """
    found = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        found.append(signal.SIGBREAK)
    return tuple(found)


def wakeup_socketpair():
    """A pair of sockets for set_wakeup_fd.

    Windows has no pipe a signal can be written to, only sockets, which is what
    `signal.set_wakeup_fd` accepts here and what QSocketNotifier watches.
    """
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    return reader, writer


ws2_32 = ctypes.WinDLL("ws2_32", use_last_error=True)
# A SOCKET is a handle, not an int, and a truncated one closes nothing.
ws2_32.closesocket.argtypes = [ctypes.c_void_p]
ws2_32.closesocket.restype = ctypes.c_int


def abort_socket(sock):
    """Make a read another thread is blocked on return, now.

    Winsock does not promise that a shutdown reaches a recv already in flight:
    it disallows the ones that come after, and the thread sitting inside the
    current one keeps sitting there. Closing the handle underneath it does end
    it, with an error, which is what a cancelled request wants.

    Python will not close it while the connection still holds a file object
    over the socket, which is what the deferred close in socket.close() is for,
    so the handle is taken out and closed directly. The read then fails with
    "not a socket", which is exactly what happened to it.
    """
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    try:
        handle = sock.detach()
    except OSError:
        return False
    if handle == -1:
        return False
    ws2_32.closesocket(handle)
    return True


DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def relaunch(command):
    """Start Dikte again, and let this process finish quitting.

    `os.execv` exists on Windows but does not replace the process the way it
    does on Unix: it starts a new one and ends this one immediately, which
    would skip the shutdown that stops the model servers and takes down the
    tray icon. A detached child plus an ordinary quit does the same job in the
    right order. Detached so that closing the terminal a restart was typed in
    does not take the new instance with it.
    """
    subprocess.Popen(
        list(command), close_fds=True,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    )
    return True


def cuda_driver_version():
    """(major, minor) of the CUDA the installed NVIDIA driver can run, or None.

    A CUDA build of whisper.cpp or llama.cpp needs a driver at least as new as
    the runtime it was built against, and both projects publish several. Asked
    here so that the download can pick the newest one this machine will
    actually load: installed the wrong way round, the server exits on a missing
    DLL that names a CUDA version and nothing about what to do.

    None when there is no NVIDIA driver, which is also an answer: a CUDA build
    on a machine with no NVIDIA card is a download that cannot work.
    """
    try:
        nvcuda = ctypes.WinDLL("nvcuda")
    except OSError:
        return None
    version = ctypes.c_int(0)
    try:
        # Answered without cuInit, so asking costs nothing and starts nothing.
        if nvcuda.cuDriverGetVersion(ctypes.byref(version)) != 0:
            return None
    except (OSError, AttributeError):
        return None
    if version.value <= 0:
        return None
    return version.value // 1000, (version.value % 1000) // 10


CP_UTF8 = 65001


def prepare_console():
    """Let this console print what Dikte has to say.

    A Windows console starts in the machine's ANSI codepage, which on a Turkish
    install is cp1254: a check mark, an arrow, an ellipsis or the word
    "değiştirildi" is not in it, and printing one raises instead of printing.
    Asking for UTF-8 is what makes the answers legible; the streams are
    reconfigured on top of this by the caller, so a console that refuses still
    prints something rather than a traceback.
    """
    try:
        kernel32.SetConsoleOutputCP(CP_UTF8)
        kernel32.SetConsoleCP(CP_UTF8)
    except OSError:
        return False
    return True


def open_folder(path):
    """Show a directory in Explorer. True when something was launched."""
    try:
        result = shell32.ShellExecuteW(None, "open", str(path), None, None, 1)
    except OSError:
        return False
    return int(result) > 32
