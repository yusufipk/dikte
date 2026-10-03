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
import shutil
import subprocess
import sys
import time

# The one other platform constant every subprocess site needs, kept in this
# leaf so no caller has to pull the audio stack in for it: console programs
# started from a windowless process would otherwise each open a console window
# of their own on Windows.
NO_WINDOW = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
             if sys.platform == "win32" else 0)


def env_path(var, default):
    """The directory a variable names, or the one it stands in for."""
    return pathlib.Path(os.environ.get(var) or os.path.expanduser(default))


def directories(platform=None):
    """(settings, data), in the two places this system keeps them.

    macOS keeps both in the one directory a Mac user's backup already knows
    about. Windows keeps them apart on purpose: settings roam with the account,
    and several gigabytes of models are exactly what a roaming profile must not
    carry. Everywhere else they are separate and follow the XDG variables.
    """
    here = platform or sys.platform
    if here == "darwin":
        support = pathlib.Path.home() / "Library/Application Support/Dikte"
        return support, support
    if here == "win32":
        roaming = env_path("APPDATA", "~/AppData/Roaming")
        local = env_path("LOCALAPPDATA", "~/AppData/Local")
        return roaming / "Dikte", local / "Dikte"
    return (env_path("XDG_CONFIG_HOME", "~/.config") / "dikte",
            env_path("XDG_DATA_HOME", "~/.local/share") / "dikte")


def cache_dir(platform=None):
    """The directory for answers worth keeping but never worth backing up.

    A third place because a cache is neither settings nor data: losing it costs
    a network request, not a model or a preference, and every system sets aside
    a directory for exactly that kind of file, one that backups skip and
    cleanup tools may empty. Storing it with the data would ask a backup to
    carry files whose whole point is that they can be thrown away.
    """
    here = platform or sys.platform
    if here == "darwin":
        return pathlib.Path.home() / "Library/Caches/Dikte"
    if here == "win32":
        return env_path("LOCALAPPDATA", "~/AppData/Local") / "Dikte" / "cache"
    return env_path("XDG_CACHE_HOME", "~/.cache") / "dikte"


# A run killed before its cleanup leaves its directory behind. Anything this
# old is from such a run, never from one still going.
STALE_SCRATCH_SECONDS = 24 * 60 * 60


def scratch_dir(platform=None):
    """Where a long job puts its intermediate audio, or None for the default.

    Many Linux systems mount /tmp as tmpfs, so a file there is held in RAM
    until it is deleted, and the converted WAVs of an hour of audio run to
    hundreds of megabytes. On Linux they go under the cache directory instead,
    which is on disk. macOS and Windows already keep their temp directory on
    disk, so None leaves tempfile to pick it there. A TMPDIR set on purpose
    wins everywhere.

    Directories left by a killed run are swept here, because on disk nothing
    else would ever remove them.
    """
    if (platform or sys.platform) != "linux" or os.environ.get("TMPDIR"):
        return None
    work = cache_dir(platform) / "work"
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    cutoff = time.time() - STALE_SCRATCH_SECONDS
    for old in work.iterdir():
        try:
            if old.is_dir() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    return str(work)


CONFIG_DIR, DATA_DIR = directories()
