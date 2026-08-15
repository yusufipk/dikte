"""Where Dikte keeps its settings and its data, for the system it is on.

A module of its own because two others need the answer and one of them cannot
ask the other: config.py imports ggml.py, so ggml.py cannot import config.py
back. Left alone, each worked it out for itself, and only config.py knew about
macOS. The result on a Mac was settings under `~/Library/Application Support`
and several gigabytes of models under `~/.local/share`, which is not a place a
Mac user looks, and not a place `uninstall.sh --purge` would have deleted from.

Read at import, as the modules that use it already do. `directories()` takes the
platform as an argument so that a test can stand on the other one.
"""

import os
import pathlib
import sys


def _path_type(platform):
    """A concrete path here, or the requested system's flavour in tests."""
    if platform.startswith("win"):
        return pathlib.Path if sys.platform.startswith("win") else pathlib.PureWindowsPath
    return pathlib.Path if not sys.platform.startswith("win") else pathlib.PurePosixPath


def _path(platform, value):
    kind = _path_type(platform)
    text = str(value)
    if kind is pathlib.PurePosixPath:
        text = text.replace("\\", "/")
    return kind(text)


def _xdg(var, default, platform):
    return _path(platform, os.environ.get(var) or os.path.expanduser(default))


def directories(platform=None):
    """(settings, data), in the two places this system keeps them.

    macOS keeps both in the one directory a Mac user's backup already knows
    about. Everywhere else they are separate and follow the XDG variables.
    """
    platform = platform or sys.platform
    if platform == "darwin":
        support = _path(platform, pathlib.Path.home()) / "Library/Application Support/Dikte"
        return support, support
    if platform.startswith("win"):
        roaming = _path(platform, os.environ.get("APPDATA")
                        or pathlib.Path.home() / "AppData/Roaming")
        local = _path(platform, os.environ.get("LOCALAPPDATA")
                      or pathlib.Path.home() / "AppData/Local")
        return roaming / "Dikte", local / "Dikte"
    return (_xdg("XDG_CONFIG_HOME", "~/.config", platform) / "dikte",
            _xdg("XDG_DATA_HOME", "~/.local/share", platform) / "dikte")


CONFIG_DIR, DATA_DIR = directories()
