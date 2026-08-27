"""Putting a downloaded build into the desktop it landed on.

A checkout has install.sh for this: the menu entry, the entry that starts Dikte
at login, the icon both of those name, and the `dikte` command. Somebody who
downloaded an AppImage or dragged Dikte.app out of a disk image ran no
installer at all, so the application writes those files itself, on its first
run and again whenever the file it was started from has moved.

Windows is the one platform where the download is an installer, and it wrote
the Start Menu entry, the `dikte` command and the uninstaller as it ran. What
is left here is the one thing it can only ask about once: whether Dikte starts
when you sign in. `dikte integrate` turns that on later and `--remove` turns it
off, and a plain start only repairs an entry that is already there.

Nothing here runs from a checkout. install.sh has already written the same
files there, pointing at the interpreter that checkout was installed against,
and overwriting them with a guess would be a downgrade.

Everything is written from the path Dikte is running as, which is why it is
also run again on every start rather than once: an AppImage that was moved out
of ~/Downloads leaves behind a menu entry naming a file that is no longer
there, and the run after the move is the only moment that can be noticed.

The rest of the module is the other half of the same meeting. A build carries
the libraries and the OpenSSL of the machine it was built on, and both of them
have to be reconciled with the machine it is running on before anything else
happens: what it hands to the programs it starts, and where it looks for the
certificates that say who it is talking to.
"""

import os
import pathlib
import plistlib
import shlex
import subprocess
import sys

# The same identifier install-mac.sh uses, because it is the name of the login
# item and both must mean the one thing when a Mac has been installed to twice.
AGENT_ID = "io.github.yusufipk.dikte"
ICON_NAME = "dikte"
DESKTOP_FILE = "dikte.desktop"
MACOS_COMMAND_MARKER = "# Written by Dikte itself. Delete it to be rid of it.\n"
# The windowed executable the Windows setup installs, beside the console one
# the `dikte` command runs.
WINDOWS_APP = "Dikte.exe"
# Where Windows keeps what to start when somebody signs in, and the name the
# setup program files Dikte's entry under. Both halves have to agree: the
# uninstaller deletes this value, and so does `dikte integrate --remove`.
RUN_KEY = "Software\\Microsoft\\Windows\\CurrentVersion\\Run"
RUN_VALUE = "Dikte"


def packaged():
    """Whether this is one of the built downloads rather than a checkout."""
    return bool(getattr(sys, "frozen", False))


def windowed_executable(executable=None):
    """The windowed executable installed beside this one, or None.

    Beside rather than at a known place, because the setup program lays the two
    executables into the same directory wherever that directory was put: asking
    from either of them finds the other without knowing where the install is.
    """
    windowed = pathlib.Path(executable or sys.executable).with_name(WINDOWS_APP)
    return windowed if windowed.is_file() else None


def target():
    """The file a launcher has to name to start this build again.

    The AppImage itself, or Dikte.app, rather than the executable inside
    either: the mount an AppImage runs from is gone by the next login, and a
    Mac starts an application through its bundle.
    """
    if os.environ.get("APPIMAGE"):
        return pathlib.Path(os.environ["APPIMAGE"])
    executable = pathlib.Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent
    if sys.platform == "win32":
        # The windowed executable, whichever of the two is running: the console
        # one is what the `dikte` command names, and a sign-in that started
        # that one would open a console window nobody asked for.
        windowed = windowed_executable(executable)
        if windowed is not None:
            return windowed
    return executable


# The two the dynamic loader reads, and what PyInstaller renames the old value
# to when it takes one over. Only the platform's own is ever set, so looking
# for both costs nothing and keeps the two builds saying the same thing.
LIBRARY_PATHS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")


def restore_library_path():
    """Put the loader's environment back to what it was. Whether it had moved.

    A build points this at the libraries it carries, and every process started
    from it inherits that. Ours are the wrong libraries for anything else on
    the machine: ffmpeg, ydotool, wl-copy and pactl are the distribution's own
    binaries built against the distribution's libstdc++, and handed the copy
    from the machine this was built on they refuse to start. So does
    AppImageLauncher, which is what running the AppImage again goes through,
    and running it again is how the command line becomes the application.

    Safe to do here because nothing of ours is looked up this way. The
    libraries this process runs on are loaded before any of this code does, and
    the ones Qt opens later, its platform plugins and image formats, are found
    through the RPATH written into them.
    """
    moved = False
    for name in LIBRARY_PATHS:
        if name not in os.environ:
            continue
        original = os.environ.pop(name + "_ORIG", None)
        # No _ORIG means there was nothing there to put back: the variable is
        # the build's own, and what the machine expects is for it to be unset.
        if original:
            os.environ[name] = original
        else:
            del os.environ[name]
        moved = True
    return moved


# Where the trust store is, which is not one place but is a short list of them:
# every distribution takes its layout from one of four packages rather than
# inventing one, and this is the list Go's crypto/x509 and curl both carry. The
# first line alone answers Debian, Ubuntu, Arch, Gentoo, Fedora and Alpine,
# which was checked rather than assumed; the rest are the ones that do it their
# own way.
CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",                 # Debian and everything after it
    "/etc/pki/tls/certs/ca-bundle.crt",                   # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",                             # openSUSE
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL 7 and CentOS
    "/etc/pki/tls/cacert.pem",                            # OpenELEC
    "/etc/ssl/cert.pem",                                  # Alpine, and macOS
)
CA_DIRECTORIES = ("/etc/ssl/certs", "/etc/pki/tls/certs")


def use_system_certificates():
    """Point the OpenSSL in this build at the machine's trust store. What it found.

    A build carries the OpenSSL of the machine it was built on, and that
    OpenSSL has one directory compiled into it as the only place it will look:
    /usr/lib/ssl for an AppImage built on Ubuntu, which does not exist on Arch,
    Fedora or openSUSE. Every HTTPS request then fails with
    CERTIFICATE_VERIFY_FAILED, which reads like a rejected API key rather than
    a packaging fault, and takes the model downloads down with it.

    The machine's own store rather than a copy carried along: a copy goes stale
    as roots are rotated, and it would ignore a certificate somebody added
    themselves, which is how a network that inspects its own traffic is made to
    work. Anybody who has already said where to look is not argued with.
    """
    if not packaged():
        return None
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return None

    import ssl
    # Both of these are None unless the path they name is really there, so this
    # asks whether the build's idea of where certificates live survived the trip.
    defaults = ssl.get_default_verify_paths()
    if defaults.cafile or defaults.capath:
        return None

    for name, candidates, exists in (("SSL_CERT_FILE", CA_FILES, os.path.isfile),
                                     ("SSL_CERT_DIR", CA_DIRECTORIES, os.path.isdir)):
        for candidate in candidates:
            if exists(candidate):
                os.environ[name] = candidate
                return candidate
    return None


def bundled_bin():
    """Where a build keeps the helper programs it carries, if it carries any.

    The disk image and the Windows setup both ship an ffmpeg, because both
    systems record through one and neither has anything like it preinstalled,
    so a machine that downloaded Dikte and nothing else would otherwise not be
    able to record at all. The AppImage carries none: Linux records through
    parec or pw-record, which come with the sound server, and the distributions
    all package ffmpeg for the rest.
    """
    binary = pathlib.Path(sys.executable).parent
    if sys.platform == "darwin" and binary.name == "MacOS":
        return binary.parent / "Resources" / "bin"
    return binary / "bin"


def add_bundled_tools():
    """Put that directory in front of PATH. Whether there was one.

    Everything that reaches for ffmpeg goes through shutil.which, so this is
    the whole of the arrangement. In front rather than behind on purpose: a Mac
    with its own ffmpeg from Homebrew still gets ours, which is the build the
    format strings in audio.py and filetranscribe.py are known to work against.
    """
    directory = bundled_bin() if packaged() else None
    if directory is None or not directory.is_dir():
        return False
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
    return True


def ensure():
    """Write whatever is missing or out of date. The paths that changed.

    Called on every start of a packaged build, and quiet when there is nothing
    to do, so that the cost of being started from a new location is one run
    with the wrong shortcuts rather than a reinstall.
    """
    if not packaged():
        return []
    try:
        return install()
    except OSError:
        # A read-only home, a full disk, a $HOME that is not ours. None of it
        # is a reason to refuse to start: Dikte works without a menu entry.
        return []


def install(force=False):
    """Write the launchers for this platform. The paths that changed.

    An installation that is already on the machine and still works is left
    alone unless `force`, which is what typing `dikte integrate` means. Three
    other things write these same files: install.sh for a checkout, its macOS
    half, and AppImageLauncher, which many desktops ship and which writes an
    entry of its own the first time an AppImage is run. Writing over any of
    them because somebody tried a download once would move the machine onto
    that download without saying so, and take the menu entry down with it when
    the file is deleted again.
    """
    if sys.platform == "darwin":
        return _macos_install(target(), force)
    if sys.platform == "win32":
        return _windows_install(target(), force)
    return _linux_install(target(), force)


def remove():
    """Take them away again. The paths that were there to delete."""
    if sys.platform == "darwin":
        return _macos_remove()
    if sys.platform == "win32":
        return _windows_remove()
    return _linux_remove()


# --- the files ------------------------------------------------------------

def _xdg(var, default):
    return pathlib.Path(os.environ.get(var) or os.path.expanduser(default))


def _paths():
    data = _xdg("XDG_DATA_HOME", "~/.local/share")
    return {
        "menu": data / "applications" / DESKTOP_FILE,
        "autostart": _xdg("XDG_CONFIG_HOME", "~/.config") / "autostart" / DESKTOP_FILE,
        "icons": data / "icons",
        "command": pathlib.Path.home() / ".local" / "bin" / "dikte",
    }


def _write(path, text):
    """Write it if it says something else. Whether it was written.

    The comparison is the point rather than an optimisation: these are read at
    login, and rewriting an unchanged autostart entry on every start is a
    modification time that backup tools and the desktop both notice.
    """
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _exec_field(*args):
    """One Exec= line. A path with a space in it is what this is for.

    The desktop entry specification quotes with double quotes and escapes with
    a backslash, which is close enough to POSIX that shlex gets the hard part
    right, and the difference only shows up in characters no download path has.
    """
    return " ".join(
        f'"{arg}"' if any(c in arg for c in ' \t"\\$`') else arg
        for arg in args
    )


def _desktop_entry(command, autostart=False):
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Dikte",
        f"Exec={command}",
        f"Icon={ICON_NAME}",
        "StartupNotify=false",
    ]
    if autostart:
        lines.insert(4, "X-GNOME-Autostart-enabled=true")
    else:
        lines.insert(4, "Comment=Voice dictation: record, transcribe, clean up, paste")
        lines.insert(6, "Categories=Utility;AudioVideo;")
    return "\n".join(lines) + "\n"


def _exec_targets(entry):
    """The files an Exec= line names, out of a desktop entry's text.

    The specification quotes with double quotes and escapes with a backslash,
    which is close enough to a shell that shlex gets the hard part right and
    the difference only shows up in characters no install path has.
    """
    for line in entry.splitlines():
        if line.startswith("Exec="):
            try:
                return shlex.split(line[len("Exec="):])
            except ValueError:
                return []
    return []


def _another_dikte(directory, mine):
    """A menu entry for an installation that is not this one and still works.

    Read across the whole directory rather than at our own file name, because
    AppImageLauncher does not use it: it writes appimagekit_<hash>-dikte.desktop
    and moves the AppImage under ~/Applications, and ours beside it would be a
    second Dikte in the menu naming a file that has been moved away.
    """
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.desktop")):
        try:
            entry = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\nName=Dikte" not in "\n" + entry:
            continue
        words = _exec_targets(entry)
        if str(mine) in words:
            continue
        if any(os.path.exists(word) for word in words):
            return path
    return None


def _linux_install(appimage, force=False):
    paths, written = _paths(), []
    if not force:
        other = _another_dikte(paths["menu"].parent, appimage)
        if other is not None:
            return []

    command = _exec_field(str(appimage))
    if _write(paths["menu"], _desktop_entry(command)):
        written.append(paths["menu"])
    if _write(paths["autostart"], _desktop_entry(command, autostart=True)):
        written.append(paths["autostart"])
    if _icon(paths["icons"]):
        written.append(paths["icons"] / "hicolor")

    # A symlink rather than a copy, so that replacing the AppImage in place
    # replaces the command too. Anything else already sitting there is left
    # where it is: install.sh puts a symlink into a checkout here, and that
    # checkout is a working installation this has no business redirecting.
    link = paths["command"]
    ours = link.is_symlink() and os.readlink(link).endswith(".AppImage")
    if not link.exists() and not link.is_symlink() or ours or force:
        if not link.is_symlink() or os.readlink(link) != str(appimage):
            link.parent.mkdir(parents=True, exist_ok=True)
            link.unlink(missing_ok=True)
            link.symlink_to(appimage)
            written.append(link)
    return written


def _linux_remove():
    paths, gone = _paths(), []
    for key in ("menu", "autostart"):
        if paths[key].exists():
            paths[key].unlink()
            gone.append(paths[key])
    link = paths["command"]
    if link.is_symlink() and pathlib.Path(os.readlink(link)).suffix == ".AppImage":
        link.unlink()
        gone.append(link)
    for size in _icon_sizes():
        icon = paths["icons"] / "hicolor" / f"{size}x{size}" / "apps" / f"{ICON_NAME}.png"
        if icon.exists():
            icon.unlink()
            gone.append(icon)
    return gone


def _icon_sizes():
    from . import trayicon
    return trayicon.HICOLOR_SIZES


def _icon(directory):
    """Draw the icon into hicolor, if there is a GUI to draw with.

    A QPixmap needs a QGuiApplication under it, and `dikte integrate` typed at
    a terminal has only the QCoreApplication the command line builds. Nothing
    is lost by skipping it there: the next start of the application itself
    draws it, and until then the entries fall back to a generic icon.
    """
    from PyQt6.QtGui import QGuiApplication
    if not isinstance(QGuiApplication.instance(), QGuiApplication):
        return False
    from . import trayicon
    first = directory / "hicolor" / "256x256" / "apps" / f"{ICON_NAME}.png"
    if first.exists():
        return False
    trayicon.write_hicolor(directory, ICON_NAME)
    return True


# --- macOS ----------------------------------------------------------------

def _agent_path():
    return pathlib.Path.home() / "Library" / "LaunchAgents" / f"{AGENT_ID}.plist"


def _macos_command_path():
    return pathlib.Path.home() / ".local" / "bin" / "dikte"


def _macos_command_is_ours(command):
    """Whether this is the wrapper a downloaded Mac build wrote itself."""
    try:
        return command.is_file() and MACOS_COMMAND_MARKER in command.read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError):
        return False


def _agent_plist(app):
    """Through `open` rather than the executable inside the bundle, so that the
    process is one LaunchServices started: that is what gives it the bundle's
    identity, and so the microphone and Accessibility permissions that were
    granted to Dikte rather than to launchd."""
    return plistlib.dumps({
        "Label": AGENT_ID,
        "ProgramArguments": ["/usr/bin/open", "-a", str(app)],
        "RunAtLoad": True,
        # Off on purpose: quitting from the menu bar should quit it, not
        # hand it back to launchd to start again.
        "KeepAlive": False,
        "ProcessType": "Interactive",
    })


def _macos_agent_app(agent):
    """The bundle a login item already there starts, if it still exists.

    install-mac.sh writes this same file for a checkout, pointing at the bundle
    it built under ~/Applications, where a disk image is dragged to
    /Applications instead. Two bundles both starting at login is one too many.
    """
    try:
        arguments = plistlib.loads(agent.read_bytes()).get("ProgramArguments", [])
    except (OSError, ValueError):
        return None
    for argument in arguments:
        if argument.endswith(".app") and os.path.exists(argument):
            return argument
    return None


def _macos_install(app, force=False):
    written = []
    agent = _agent_path()
    if not force and agent.exists():
        theirs = _macos_agent_app(agent)
        if theirs is not None and theirs != str(app):
            return []

    plist = _agent_plist(app)
    if not agent.exists() or agent.read_bytes() != plist:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_bytes(plist)
        written.append(agent)
        _launchctl_reload(agent)

    # The command, as a wrapper rather than a symlink: the executable has to be
    # run from inside the bundle for macOS to file its permissions under Dikte,
    # and a symlink somewhere else is a different process to macOS.
    command = _macos_command_path()
    binary = app / "Contents" / "MacOS" / "Dikte"
    script = (f'#!/bin/sh\n{MACOS_COMMAND_MARKER}'
              f'exec {shlex.quote(str(binary))} "$@"\n')
    # install-mac.sh writes its own wrapper here, naming the checkout's Python.
    # Ours only replaces a wrapper it wrote before, or nothing at all.
    ours = _macos_command_is_ours(command)
    if (not command.exists() or ours or force) and _write(command, script):
        command.chmod(0o755)
        written.append(command)
    return written


def _macos_remove():
    gone = []
    agent = _agent_path()
    if agent.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_ID}"],
                       capture_output=True, check=False)
        agent.unlink()
        gone.append(agent)
    command = _macos_command_path()
    if _macos_command_is_ours(command):
        command.unlink()
        gone.append(command)
    return gone


def _launchctl_reload(agent):
    """Load the login item now, so that it does not first take effect a login
    from now. bootout first because bootstrapping a label that is already
    loaded fails, and a reinstall is exactly that case."""
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_ID}"],
                   capture_output=True, check=False)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)],
                   capture_output=True, check=False)


# --- Windows --------------------------------------------------------------
#
# The setup program did the installing here, which leaves one question a
# wizard can only ask while it is on the screen: whether Dikte starts when you
# sign in. That answer is a registry value, so it is one both sides can write:
# the setup program sets it from the tick box, the uninstaller deletes it
# however it got there, and the two functions below are the same switch from a
# terminal, long after the wizard is gone.


def _run_entry():
    """What the autostart entry names, or "" when there is none."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, kind = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return ""
    return value if kind == winreg.REG_SZ and isinstance(value, str) else ""


def _write_run_entry(command):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)


def _delete_run_entry():
    """Whether there was one to delete."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except OSError:
        return False
    return True


def _run_entry_name():
    """What to call the value in a listing, since it is not a file."""
    return f"HKCU\\{RUN_KEY}\\{RUN_VALUE}"


def _run_target(value):
    """The executable a Run value names, out of the quoting the setup wrote.

    Only the first word matters here: it is the file whose existence says
    whether the entry still starts anything.
    """
    if value.startswith('"'):
        closing = value.find('"', 1)
        return value[1:closing] if closing > 0 else ""
    return value.split(" ", 1)[0]


def _startup_shortcut():
    """Where install.ps1 -Autostart puts a checkout's sign-in entry."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return (pathlib.Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / "Dikte.lnk")


def _windows_install(app, force=False):
    """Point the autostart entry at this build. What changed.

    Only `force`, which is what typing `dikte integrate` means, creates one.
    The call on every start repairs an entry that is already there and names an
    executable that is gone, which is what an installation moved to another
    drive or reinstalled into another directory leaves behind. An entry naming
    an executable that still exists is another installation that still works,
    and is stood aside for the way the Linux half stands aside for another
    menu entry; somebody who unticked the box in the wizard, or turned it off
    since, is not asked again by every start either.
    """
    command = f'"{app}"'
    current = _run_entry()
    changed = []
    if force:
        # install.ps1 -Autostart wrote this for a checkout. The Run value
        # written below replaces it, and both left in place would be two
        # Diktes at every sign-in. Only on force: the silent call on every
        # start has not been asked to move the machine off its checkout.
        shortcut = _startup_shortcut()
        if shortcut is not None and shortcut.is_file():
            shortcut.unlink()
            changed.append(shortcut)
    if not current and not force:
        return changed
    if current != command:
        theirs = _run_target(current) if current else ""
        if not force and theirs and theirs != str(app) and os.path.exists(theirs):
            return changed
        _write_run_entry(command)
        changed.append(_run_entry_name())
    return changed


def _windows_remove():
    """Stop starting at sign-in. The Start Menu entry, the command and the
    files are the uninstaller's, and Add/Remove Programs is where they go."""
    return [_run_entry_name()] if _delete_run_entry() else []
