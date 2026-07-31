"""Platform helpers: one import, both platforms."""

import locale
import os
import re
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# CREATE_NO_WINDOW. Every child process this program starts is a helper nobody
# asked to see, and on Windows each one would otherwise flash up a console.
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def no_window():
    """Keyword arguments for subprocess that keep the console out of sight."""
    return {"creationflags": NO_WINDOW} if IS_WINDOWS else {}


def get_config_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "Dikte"

    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "dikte"
    return Path.home() / ".config" / "dikte"


def get_data_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Dikte"

    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "dikte"
    return Path.home() / ".local" / "share" / "dikte"


def get_server_name() -> str:
    if IS_WINDOWS:
        return f"dikte-{os.environ.get('USERNAME', 'user')}"
    # pylint: disable=no-member
    return f"dikte-{os.getuid()}"


def setup_qt_platform() -> None:
    if IS_LINUX:
        if os.environ.get("XDG_SESSION_TYPE") == "wayland" and "DISPLAY" in os.environ:
            os.environ.setdefault("QT_QPA_PLATFORM", "xcb")


def safe_chmod(path, mode: int) -> None:
    if IS_LINUX:
        os.chmod(path, mode)


def _windows_ui_language() -> str:
    """The language Windows itself is shown in, e.g. 'tr-TR'."""
    try:
        import ctypes  # noqa: PLC0415 - only ever needed on Windows

        buffer = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, 85):
            return buffer.value or ""
    except (OSError, AttributeError, ValueError):
        pass
    try:
        return locale.getdefaultlocale()[0] or ""
    except (ValueError, TypeError):
        return ""


def resolve_locale(code: str = None) -> str:
    """'auto' (or anything unknown) -> the language the system is set to."""
    if code in ("tr", "en"):
        return code

    # An explicit environment variable wins on both platforms: someone who sets
    # LANG has said what they want in the plainest way available to them.
    lang = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
            or os.environ.get("LANG") or "")
    if not lang and IS_WINDOWS:
        lang = _windows_ui_language()

    return "tr" if lang.lower().startswith("tr") else "en"


def resolve_binary(name: str):
    """The argv prefix that runs `name`, or [] when it is not installed.

    On Windows a CLI installed through npm is a .cmd shim, and CreateProcess
    reaches it only through cmd.exe, which mangles any argument holding a
    newline — and this program hands whole system prompts over as arguments.
    The shim names the real executable, so it is read out and called directly.
    """
    import shutil  # noqa: PLC0415 - keeps the module import-light

    path = shutil.which(name)
    if not path:
        return []
    if not IS_WINDOWS or not path.lower().endswith((".cmd", ".bat")):
        return [path]

    try:
        script = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [path]

    directory = Path(path).parent
    for token in re.findall(r'"([^"\n]+)"', script):
        target = token.replace("%dp0%", str(directory) + os.sep)
        target = target.replace("%~dp0", str(directory) + os.sep)
        if "%" in target or not target.lower().endswith((".exe", ".js")):
            continue
        resolved = Path(os.path.normpath(target))
        if not resolved.exists():
            continue
        if resolved.suffix.lower() == ".js":
            node = shutil.which("node")
            return [node, str(resolved)] if node else [path]
        return [str(resolved)]
    return [path]
