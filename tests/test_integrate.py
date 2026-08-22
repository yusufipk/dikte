"""What a downloaded build writes into the desktop it landed on.

The half worth pinning is the restraint rather than the writing. Three things
write these same files, and the AppImage is the one a person is most likely to
run once out of curiosity: it has to leave a working installation alone, and it
has to notice AppImageLauncher's entry, which is not under the name ours is.
"""

import os
import pathlib
import plistlib
import re
import sys
import tempfile
import unittest
from unittest import mock

from dikte import integrate
from tests.support import posix_only


class Frozen:
    """A build, standing in for one. The two facts everything here reads.

    sys.frozen is what PyInstaller sets and nothing else does; APPIMAGE is what
    the AppImage runtime exports, and is the file rather than the mount.
    """

    def __init__(self, executable, appimage=None, home=None, platform=None):
        self.patches = [
            mock.patch.object(sys, "executable", executable),
            mock.patch.object(sys, "frozen", True, create=True),
        ]
        environment = {"APPIMAGE": appimage} if appimage else {}
        if home:
            environment["HOME"] = str(home)
            environment["XDG_DATA_HOME"] = str(pathlib.Path(home) / ".local/share")
            environment["XDG_CONFIG_HOME"] = str(pathlib.Path(home) / ".config")
        self.patches.append(mock.patch.dict(os.environ, environment,
                                            clear=bool(home)))
        if platform:
            self.patches.append(mock.patch.object(sys, "platform", platform))

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_):
        for patch in reversed(self.patches):
            patch.stop()


class Home(unittest.TestCase):
    """A test with a home directory of its own to be written into."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Resolved, because target() resolves what it is handed and a Mac's
        # temporary directory is under /var, which is a symlink to /private/var.
        # Left alone, every path here would be compared against the other
        # spelling of itself.
        self.home = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.applications = self.home / ".local/share/applications"
        self.autostart = self.home / ".config/autostart"

    def entry(self, name, exec_line, application="Dikte"):
        self.applications.mkdir(parents=True, exist_ok=True)
        path = self.applications / name
        path.write_text(f"[Desktop Entry]\nType=Application\nName={application}\n"
                        f"Exec={exec_line}\n", encoding="utf-8")
        return path


class WhatToStart(unittest.TestCase):
    def test_a_checkout_names_this_interpreter_and_the_entry_point(self):
        self.assertFalse(integrate.packaged())

    @posix_only
    def test_an_appimage_names_the_file_and_not_the_mount(self):
        """The mount is a fresh /tmp path every run; a shortcut written to it
        would work until the next login and never again."""
        with Frozen("/tmp/.mount_Dikte1a/usr/bin/dikte",
                    appimage="/home/someone/Downloads/Dikte.AppImage"):
            self.assertEqual(str(integrate.target()),
                             "/home/someone/Downloads/Dikte.AppImage")

    @posix_only
    def test_a_mac_names_the_bundle_and_not_the_executable_inside_it(self):
        with Frozen("/Applications/Dikte.app/Contents/MacOS/Dikte",
                    platform="darwin"):
            self.assertEqual(str(integrate.target()), "/Applications/Dikte.app")

    def test_a_checkout_writes_nothing(self):
        self.assertEqual(integrate.ensure(), [])


class BundledTools(unittest.TestCase):
    """The ffmpeg the disk image carries, and how anything finds it."""

    @posix_only
    def test_a_mac_looks_beside_the_bundle_not_beside_the_executable(self):
        with Frozen("/Applications/Dikte.app/Contents/MacOS/Dikte",
                    platform="darwin"):
            self.assertEqual(str(integrate.bundled_bin()),
                             "/Applications/Dikte.app/Contents/Resources/bin")

    def test_a_checkout_has_none_and_leaves_the_path_alone(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            self.assertFalse(integrate.add_bundled_tools())
            self.assertEqual(os.environ["PATH"], "/usr/bin")

    def test_the_directory_goes_in_front(self):
        """In front, so that a Mac with its own ffmpeg from Homebrew still gets
        the build these format strings are known to work against."""
        with tempfile.TemporaryDirectory() as tmp:
            tools = pathlib.Path(tmp) / "bin"
            tools.mkdir()
            with Frozen(str(pathlib.Path(tmp) / "dikte")), \
                 mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
                self.assertTrue(integrate.add_bundled_tools())
                self.assertEqual(os.environ["PATH"], f"{tools}{os.pathsep}/usr/bin")

    def test_a_build_carrying_nothing_leaves_the_path_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Frozen(str(pathlib.Path(tmp) / "dikte")), \
                 mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
                self.assertFalse(integrate.add_bundled_tools())


class LibraryPath(unittest.TestCase):
    """What a build hands to every process it starts.

    The one that catches it first is the AppImage starting itself again, which
    is what the command line does when no instance is running: that goes back
    through AppImageLauncher, a system binary, which will not load against the
    libstdc++ the build was made with. ffmpeg, ydotool and wl-copy are the same
    problem arriving later and harder to trace.
    """

    def test_what_was_there_before_is_put_back(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/.mount_x/_internal",
                                          "LD_LIBRARY_PATH_ORIG": "/opt/cuda/lib"}):
            self.assertTrue(integrate.restore_library_path())
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/opt/cuda/lib")
            self.assertNotIn("LD_LIBRARY_PATH_ORIG", os.environ)

    def test_nothing_there_before_means_unset_rather_than_empty(self):
        """An empty LD_LIBRARY_PATH is not the same as none: the loader reads it
        as the current directory."""
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/.mount_x/_internal"}):
            self.assertTrue(integrate.restore_library_path())
            self.assertNotIn("LD_LIBRARY_PATH", os.environ)

    def test_a_mac_has_its_own_name_for_it(self):
        with mock.patch.dict(os.environ, {"DYLD_LIBRARY_PATH": "/Dikte.app/Contents/Frameworks"}):
            self.assertTrue(integrate.restore_library_path())
            self.assertNotIn("DYLD_LIBRARY_PATH", os.environ)

    def test_a_checkout_has_nothing_to_put_back(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(integrate.restore_library_path())


class Certificates(unittest.TestCase):
    """Where a build looks for the certificates that say who it is talking to.

    An AppImage built on Ubuntu carries an OpenSSL with /usr/lib/ssl compiled
    into it, and Arch, Fedora and openSUSE have no such directory. Left alone
    it is every HTTPS request failing at once, reported as a rejected key.
    """

    def paths(self, cafile=None, capath=None):
        """ssl.get_default_verify_paths(), which reports only what really exists."""
        import ssl
        return mock.patch("ssl.get_default_verify_paths",
                          return_value=ssl.DefaultVerifyPaths(
                              cafile, capath, "SSL_CERT_FILE", "/usr/lib/ssl/cert.pem",
                              "SSL_CERT_DIR", "/usr/lib/ssl/certs"))

    def test_a_store_the_build_cannot_find_is_looked_up(self):
        with Frozen("/tmp/.mount_x/usr/bin/dikte"), \
             mock.patch.dict(os.environ, {}, clear=True), \
             self.paths(), \
             mock.patch("os.path.isfile", lambda p: p == "/etc/ssl/cert.pem"):
            self.assertEqual(integrate.use_system_certificates(), "/etc/ssl/cert.pem")
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/etc/ssl/cert.pem")

    def test_a_directory_will_do_when_no_bundle_is_there(self):
        with Frozen("/tmp/.mount_x/usr/bin/dikte"), \
             mock.patch.dict(os.environ, {}, clear=True), \
             self.paths(), \
             mock.patch("os.path.isfile", return_value=False), \
             mock.patch("os.path.isdir", lambda p: p == "/etc/ssl/certs"):
            self.assertEqual(integrate.use_system_certificates(), "/etc/ssl/certs")
            self.assertEqual(os.environ["SSL_CERT_DIR"], "/etc/ssl/certs")

    def test_a_build_that_can_already_find_them_is_left_alone(self):
        """Which is the AppImage running on the distribution it was built on."""
        with Frozen("/tmp/.mount_x/usr/bin/dikte"), \
             mock.patch.dict(os.environ, {}, clear=True), \
             self.paths(capath="/usr/lib/ssl/certs"):
            self.assertIsNone(integrate.use_system_certificates())
            self.assertNotIn("SSL_CERT_FILE", os.environ)

    def test_somebody_who_has_said_where_is_not_argued_with(self):
        """A network that inspects its own traffic is made to work this way."""
        with Frozen("/tmp/.mount_x/usr/bin/dikte"), \
             mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/opt/work/ca.pem"},
                             clear=True), \
             self.paths():
            self.assertIsNone(integrate.use_system_certificates())
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/opt/work/ca.pem")

    def test_a_checkout_uses_the_python_it_was_installed_against(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(integrate.use_system_certificates())


@posix_only
class Linux(Home):
    def install(self, appimage, force=False):
        with Frozen("/tmp/.mount_x/usr/bin/dikte", appimage=str(appimage),
                    home=self.home, platform="linux"):
            return integrate.install(force=force)

    def test_it_writes_a_menu_entry_an_autostart_entry_and_the_command(self):
        appimage = self.home / "Downloads" / "Dikte.AppImage"
        appimage.parent.mkdir(parents=True)
        appimage.touch()
        self.install(appimage)

        menu = (self.applications / "dikte.desktop").read_text(encoding="utf-8")
        self.assertIn(f"Exec={appimage}", menu)
        self.assertIn("Categories=", menu)
        autostart = (self.autostart / "dikte.desktop").read_text(encoding="utf-8")
        self.assertIn(f"Exec={appimage}", autostart)
        self.assertNotIn("Categories=", autostart)
        self.assertEqual(os.readlink(self.home / ".local/bin/dikte"), str(appimage))

    def test_running_it_again_changes_nothing(self):
        appimage = self.home / "Dikte.AppImage"
        appimage.touch()
        self.install(appimage)
        self.assertEqual(self.install(appimage), [])

    def test_moving_the_appimage_rewrites_the_entries(self):
        """The run after a move is the only moment a stale entry can be
        noticed, which is why this is done on every start."""
        first, second = self.home / "a.AppImage", self.home / "b.AppImage"
        first.touch()
        self.install(first)
        first.rename(second)
        self.install(second)
        self.assertIn(f"Exec={second}",
                      (self.applications / "dikte.desktop").read_text())
        self.assertEqual(os.readlink(self.home / ".local/bin/dikte"), str(second))

    def test_it_stands_aside_for_a_checkout_that_install_sh_set_up(self):
        checkout = self.home / "src" / "dikte" / "__main__.py"
        checkout.parent.mkdir(parents=True)
        checkout.touch()
        self.entry("dikte.desktop", f"/usr/bin/python3 {checkout}")
        appimage = self.home / "Dikte.AppImage"
        appimage.touch()

        self.assertEqual(self.install(appimage), [])
        self.assertIn(str(checkout),
                      (self.applications / "dikte.desktop").read_text())
        self.assertFalse((self.autostart / "dikte.desktop").exists())

    def test_it_stands_aside_for_appimagelauncher(self):
        """Which writes appimagekit_<hash>-dikte.desktop rather than ours, and
        moves the file, so ours beside it would be a second Dikte in the menu
        naming somewhere the AppImage no longer is."""
        moved = self.home / "Applications" / "Dikte_abc.AppImage"
        moved.parent.mkdir(parents=True)
        moved.touch()
        self.entry("appimagekit_abc-dikte.desktop", str(moved))
        appimage = self.home / "Downloads" / "Dikte.AppImage"
        appimage.parent.mkdir(parents=True)
        appimage.touch()

        self.assertEqual(self.install(appimage), [])

    def test_an_entry_naming_a_file_that_is_gone_is_not_in_the_way(self):
        self.entry("dikte.desktop", "/removed/last/week/Dikte.AppImage")
        appimage = self.home / "Dikte.AppImage"
        appimage.touch()
        self.assertTrue(self.install(appimage))

    def test_asking_outright_overrules_all_of_that(self):
        checkout = self.home / "src" / "__main__.py"
        checkout.parent.mkdir(parents=True)
        checkout.touch()
        self.entry("dikte.desktop", f"/usr/bin/python3 {checkout}")
        appimage = self.home / "Dikte.AppImage"
        appimage.touch()

        self.assertTrue(self.install(appimage, force=True))
        self.assertIn(str(appimage),
                      (self.applications / "dikte.desktop").read_text())

    def test_a_command_somebody_else_put_there_is_left_alone(self):
        """install.sh points it into a checkout, and that checkout is a working
        installation this has no business redirecting."""
        command = self.home / ".local/bin/dikte"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\nexec python3 /somewhere/__main__.py\n")
        appimage = self.home / "Dikte.AppImage"
        appimage.touch()

        self.install(appimage)
        self.assertIn("/somewhere/__main__.py", command.read_text())

    def test_a_path_with_a_space_in_it_is_quoted(self):
        appimage = self.home / "My Programs" / "Dikte.AppImage"
        appimage.parent.mkdir(parents=True)
        appimage.touch()
        self.install(appimage)
        self.assertIn(f'Exec="{appimage}"',
                      (self.applications / "dikte.desktop").read_text())

    def test_removing_takes_back_what_it_wrote(self):
        appimage = self.home / "Dikte.AppImage"
        appimage.touch()
        self.install(appimage)
        with Frozen("/tmp/.mount_x/usr/bin/dikte", appimage=str(appimage),
                    home=self.home, platform="linux"):
            integrate.remove()
        self.assertFalse((self.applications / "dikte.desktop").exists())
        self.assertFalse((self.autostart / "dikte.desktop").exists())
        self.assertFalse((self.home / ".local/bin/dikte").is_symlink())

    def test_removing_leaves_a_command_that_is_not_ours(self):
        command = self.home / ".local/bin/dikte"
        command.parent.mkdir(parents=True)
        command.symlink_to("/somewhere/dikte/__main__.py")
        appimage = self.home / "Dikte.AppImage"
        appimage.touch()
        with Frozen("/tmp/.mount_x/usr/bin/dikte", appimage=str(appimage),
                    home=self.home, platform="linux"):
            integrate.remove()
        self.assertTrue(command.is_symlink())

    def test_a_home_it_cannot_write_to_is_not_a_reason_to_refuse_to_start(self):
        appimage = self.home / "Dikte.AppImage"
        appimage.touch()
        with Frozen("/tmp/.mount_x/usr/bin/dikte", appimage=str(appimage),
                    home=self.home, platform="linux"), \
             mock.patch.object(integrate, "_linux_install",
                               side_effect=PermissionError):
            self.assertEqual(integrate.ensure(), [])


@posix_only
class MacOS(Home):
    def agent(self):
        return self.home / "Library/LaunchAgents/io.github.yusufipk.dikte.plist"

    def install(self, app, force=False):
        with Frozen(str(app / "Contents/MacOS/Dikte"), home=self.home,
                    platform="darwin"), \
             mock.patch.object(integrate, "_launchctl_reload"):
            return integrate.install(force=force)

    def remove(self):
        with Frozen("/Applications/Dikte.app/Contents/MacOS/Dikte",
                    home=self.home, platform="darwin"), \
             mock.patch("subprocess.run"):
            return integrate.remove()

    def test_it_writes_a_login_item_and_the_command(self):
        app = self.home / "Applications" / "Dikte.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        self.install(app)

        plist = plistlib.loads(self.agent().read_bytes())
        self.assertEqual(plist["Label"], integrate.AGENT_ID)
        # Through `open` rather than the executable, so that the process is one
        # LaunchServices started and the permissions are the bundle's.
        self.assertEqual(plist["ProgramArguments"][:2], ["/usr/bin/open", "-a"])
        self.assertEqual(plist["ProgramArguments"][2], str(app))
        self.assertFalse(plist["KeepAlive"])

        command = self.home / ".local/bin/dikte"
        self.assertIn(str(app / "Contents/MacOS/Dikte"), command.read_text())
        self.assertTrue(os.access(command, os.X_OK))

    def test_running_it_again_changes_nothing(self):
        app = self.home / "Applications" / "Dikte.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        self.install(app)
        self.assertEqual(self.install(app), [])

    def test_it_stands_aside_for_a_bundle_install_mac_sh_built(self):
        """That one goes under ~/Applications, a disk image is dragged to
        /Applications, and two of them starting at login is one too many."""
        theirs = self.home / "Applications" / "Dikte.app"
        theirs.mkdir(parents=True)
        agent = self.agent()
        agent.parent.mkdir(parents=True)
        agent.write_bytes(plistlib.dumps({
            "Label": integrate.AGENT_ID,
            "ProgramArguments": ["/usr/bin/open", "-a", str(theirs)],
        }))

        mine = self.home / "Volumes" / "Dikte.app"
        (mine / "Contents/MacOS").mkdir(parents=True)
        self.assertEqual(self.install(mine), [])
        self.assertIn(str(theirs), agent.read_text(encoding="utf-8"))

    def test_a_login_item_naming_a_bundle_that_is_gone_is_not_in_the_way(self):
        agent = self.agent()
        agent.parent.mkdir(parents=True)
        agent.write_bytes(plistlib.dumps({
            "Label": integrate.AGENT_ID,
            "ProgramArguments": ["/usr/bin/open", "-a", "/gone/Dikte.app"],
        }))
        app = self.home / "Applications" / "Dikte.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        self.assertTrue(self.install(app))

    def test_a_command_install_mac_sh_wrote_is_left_alone(self):
        command = self.home / ".local/bin/dikte"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\n# Written by install-mac.sh.\n"
                           "exec /opt/homebrew/bin/python3 /src/__main__.py \"$@\"\n")
        app = self.home / "Applications" / "Dikte.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        self.install(app)
        self.assertIn("install-mac.sh", command.read_text())

    def test_removing_takes_back_the_login_item_and_command_it_wrote(self):
        app = self.home / "Applications" / "Dikte.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        self.install(app)
        command = self.home / ".local/bin/dikte"

        self.assertEqual(self.remove(), [self.agent(), command])
        self.assertFalse(self.agent().exists())
        self.assertFalse(command.exists())

    def test_removing_leaves_another_installers_command_alone(self):
        command = self.home / ".local/bin/dikte"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\n# Written by install-mac.sh.\n"
                           "exec /usr/local/bin/python3 /src/__main__.py \"$@\"\n")

        self.assertEqual(self.remove(), [])
        self.assertIn("install-mac.sh", command.read_text())


class Windows(unittest.TestCase):
    """The half of the Windows install the setup program cannot do.

    It writes the Start Menu entry, the command and the uninstaller as it runs,
    and asks once whether Dikte should start at sign-in. Changing that answer
    afterwards is what is left here, and the value is faked rather than the
    registry, so that all of it is read on every platform the tests run on.
    """

    def setUp(self):
        self.value = ""
        for name, function in (("_run_entry", lambda: self.value),
                               ("_write_run_entry", self._write),
                               ("_delete_run_entry", self._delete)):
            patch = mock.patch.object(integrate, name, function)
            patch.start()
            self.addCleanup(patch.stop)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.installed = pathlib.Path(self.tmp.name).resolve()
        self.app = self.installed / "Dikte.exe"
        self.app.write_text("")
        # APPDATA pointed into the sandbox, so that the Startup folder these
        # tests delete from is never the machine's own.
        appdata = mock.patch.dict(os.environ,
                                  {"APPDATA": str(self.installed / "Roaming")})
        appdata.start()
        self.addCleanup(appdata.stop)

    def startup_shortcut(self):
        """Where install.ps1 -Autostart puts a checkout's sign-in entry."""
        return (self.installed / "Roaming" / "Microsoft" / "Windows"
                / "Start Menu" / "Programs" / "Startup" / "Dikte.lnk")

    def _write(self, command):
        self.value = command

    def _delete(self):
        there, self.value = bool(self.value), ""
        return there

    def install(self, force=False):
        with Frozen(str(self.app), platform="win32"):
            return integrate.install(force=force)

    def remove(self):
        with Frozen(str(self.app), platform="win32"):
            return integrate.remove()

    def test_the_windowed_executable_is_what_starts_at_sign_in(self):
        """The console one is what the `dikte` command runs, and a sign-in that
        started that would open a console window nobody asked for."""
        with Frozen(str(self.installed / "dikte-cli.exe"), platform="win32"):
            self.assertEqual(integrate.target(), self.app)

    def test_a_start_does_not_turn_it_on_for_somebody_who_said_no(self):
        self.assertEqual(self.install(), [])
        self.assertEqual(self.value, "")

    def test_typing_it_turns_starting_at_sign_in_on(self):
        self.assertEqual(len(self.install(force=True)), 1)
        self.assertEqual(self.value, f'"{self.app}"')

    def test_an_installation_that_moved_is_pointed_at_where_it_is_now(self):
        self.value = '"D:\\Dikte\\Dikte.exe"'
        self.assertEqual(len(self.install()), 1)
        self.assertEqual(self.value, f'"{self.app}"')

    def test_an_entry_for_another_working_install_is_left_alone(self):
        """The same courtesy the Linux half pays another menu entry: an entry
        naming an executable that still exists is an installation that still
        works, and a start of this one has no business redirecting it."""
        other = self.installed / "Elsewhere" / "Dikte.exe"
        other.parent.mkdir()
        other.write_text("")
        self.value = f'"{other}"'
        self.assertEqual(self.install(), [])
        self.assertEqual(self.value, f'"{other}"')

    def test_asking_outright_overrules_a_working_other_install(self):
        other = self.installed / "Elsewhere" / "Dikte.exe"
        other.parent.mkdir()
        other.write_text("")
        self.value = f'"{other}"'
        self.assertEqual(self.install(force=True), [integrate._run_entry_name()])
        self.assertEqual(self.value, f'"{self.app}"')

    def test_typing_it_sweeps_away_a_checkout_startup_shortcut(self):
        """install.ps1 -Autostart writes it, the Run value replaces it, and
        both left in place would be two Diktes at every sign-in."""
        shortcut = self.startup_shortcut()
        shortcut.parent.mkdir(parents=True)
        shortcut.write_text("")
        changed = self.install(force=True)
        self.assertIn(shortcut, changed)
        self.assertFalse(shortcut.exists())

    def test_a_start_leaves_a_checkout_startup_shortcut_alone(self):
        """The silent call on every start has not been asked to move the
        machine off its checkout."""
        shortcut = self.startup_shortcut()
        shortcut.parent.mkdir(parents=True)
        shortcut.write_text("")
        self.value = f'"{self.app}"'
        self.assertEqual(self.install(), [])
        self.assertTrue(shortcut.exists())

    def test_running_it_again_changes_nothing(self):
        self.install(force=True)
        self.assertEqual(self.install(), [])

    def test_removing_stops_it_starting_and_says_so_once(self):
        self.install(force=True)
        self.assertEqual(len(self.remove()), 1)
        self.assertEqual(self.value, "")
        self.assertEqual(self.remove(), [])


class WindowedExecutable(unittest.TestCase):
    """The windowed executable, looked up beside whichever one is running.

    Beside rather than at a known place: the setup lays both executables into
    one directory wherever that directory was put, so either can find the
    other without knowing where the install is.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.installed = pathlib.Path(self.tmp.name).resolve()

    def test_found_beside_the_named_executable(self):
        windowed = self.installed / "Dikte.exe"
        windowed.write_text("")
        self.assertEqual(
            integrate.windowed_executable(str(self.installed / "dikte-cli.exe")),
            windowed)

    def test_none_when_no_setup_installed_one(self):
        self.assertIsNone(
            integrate.windowed_executable(str(self.installed / "dikte-cli.exe")))


class WindowsExecutableNames(unittest.TestCase):
    """The two Windows executables, read out of the files that name them.

    Windows matches a filename without regard to its case, so Dikte.exe and
    dikte.exe are one file in one directory and whichever was written second is
    the only one installed. Nothing else here would catch that: these tests and
    the builds that check the packaging both run on filesystems where the two
    names are two files.
    """

    root = pathlib.Path(__file__).resolve().parent.parent

    def executables(self):
        """What the spec calls each one, in the order it builds them."""
        spec = (self.root / "packaging" / "dikte.spec").read_text()
        return [re.search(r'name="(.*?)"', block).group(1)
                for block in spec.split("EXE(")[1:]]

    def test_the_two_are_more_than_a_case_apart(self):
        windowed, console = self.executables()
        self.assertNotEqual(windowed.lower(), console.lower())

    def test_the_command_runs_the_console_one(self):
        """The setup writes the shim, so it is the setup that has to be right."""
        console = self.executables()[1]
        setup = (self.root / "packaging" / "dikte.iss").read_text()
        self.assertIn(f"{{app}}\\{console}.exe", setup)


if __name__ == "__main__":
    unittest.main()
