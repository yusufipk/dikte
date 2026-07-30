"""macOS login-item support through a per-user LaunchAgent."""

import os
import pathlib
import plistlib
import sys

LABEL = "dev.dikte.app"
LAUNCH_AGENT = pathlib.Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _is_macos_app_executable(executable):
    path = pathlib.Path(executable)
    return path.parent.name == "MacOS" and path.parent.parent.name == "Contents" \
        and path.parent.parent.parent.suffix == ".app"


def launch_agent_payload(executable):
    return {
        "Label": LABEL,
        "ProgramArguments": [str(pathlib.Path(executable).resolve())],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }


def update(enabled, executable, path=LAUNCH_AGENT):
    """Create or remove the LaunchAgent. Return True when disk state changed."""
    path = pathlib.Path(path)
    if not enabled:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    payload = plistlib.dumps(
        launch_agent_payload(executable), fmt=plistlib.FMT_XML, sort_keys=True
    )
    try:
        if path.read_bytes() == payload:
            return False
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".plist.tmp")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    return True


def sync(enabled, executable=None):
    """Apply the setting when running from a packaged macOS application."""
    if sys.platform != "darwin":
        return False
    executable = executable or sys.executable
    if not _is_macos_app_executable(executable):
        return False
    return update(enabled, executable)
