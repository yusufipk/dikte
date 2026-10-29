"""Which program is in front, on each of the four systems, with the system
itself faked at the one place each backend asks it."""

import ctypes
import json
import os
import subprocess
import sys
from unittest import mock

from dikte import frontapp
from tests.support import DikteTest


def finished(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


class Chooser(DikteTest):
    def backend(self, platform, **env):
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.dict(os.environ, env, clear=True):
            return frontapp.backend()

    def test_each_system_has_its_own(self):
        self.assertIs(self.backend("darwin"), frontapp._macos)
        self.assertIs(self.backend("win32"), frontapp._windows)
        self.assertIs(self.backend("linux", XDG_SESSION_TYPE="x11"), frontapp._x11)
        self.assertIs(self.backend("linux", DISPLAY=":0"), frontapp._x11)
        self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                   DISPLAY=":0", XDG_CURRENT_DESKTOP="KDE"),
                      frontapp._kde_wayland)
        self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                   XDG_CURRENT_DESKTOP="GNOME"),
                      frontapp._gnome)
        self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                   HYPRLAND_INSTANCE_SIGNATURE="abc"),
                      frontapp._hyprland)
        self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                   SWAYSOCK="/run/user/1000/sway.sock"),
                      frontapp._sway)
        self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                   NIRI_SOCKET="/run/user/1000/niri.sock"),
                      frontapp._niri)
        self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                   XDG_CURRENT_DESKTOP="Hyprland"),
                      frontapp._hyprland)
        for desktop in ("river", "Wayfire", "labwc:wlroots", "COSMIC", ""):
            with self.subTest(desktop=desktop):
                self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                           XDG_CURRENT_DESKTOP=desktop),
                              frontapp._unknown)

    def test_a_failing_backend_is_an_unknown_program(self):
        with mock.patch.object(frontapp, "backend",
                               return_value=mock.Mock(side_effect=OSError)):
            self.assertEqual(frontapp.program_name(), "")

    def test_names_come_back_bare_and_lower_case(self):
        with mock.patch.object(frontapp, "backend",
                               return_value=lambda pid, title_for=None: (r"C:\Apps\Slack.EXE", "")):
            self.assertEqual(frontapp.program_name(), "slack")


class X11(DikteTest):
    def answers(self, *replies):
        return mock.patch.object(frontapp.subprocess, "run",
                                 side_effect=[finished(r) for r in replies])

    def test_the_active_windows_process_names_the_program(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                self.answers("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007\n",
                             "_NET_WM_PID(CARDINAL) = 4242\n"
                             'WM_CLASS(STRING) = "Navigator", "firefox"\n') as run, \
                mock.patch.object(frontapp, "_process_name", return_value="electron"):
            # The class, not the runtime the window happens to run on.
            self.assertEqual(frontapp._x11()[0], "firefox")
        self.assertEqual(run.call_args.args[0][:3], ["xprop", "-id", "0x3a00007"])

    def test_without_a_class_the_process_will_do(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                self.answers("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x1\n",
                             "_NET_WM_PID(CARDINAL) = 42\n"), \
                mock.patch.object(frontapp, "_process_name", return_value="xterm"):
            self.assertEqual(frontapp._x11()[0], "xterm")

    def test_an_upgraded_program_loses_the_deleted_mark(self):
        with mock.patch.object(frontapp.os, "readlink",
                               return_value="/usr/lib/thunderbird/thunderbird (deleted)"):
            self.assertEqual(frontapp._process_name(42), "thunderbird")
        with mock.patch.object(frontapp, "backend",
                               return_value=lambda pid, title_for=None: ("Thunderbird (deleted)", "")):
            self.assertEqual(frontapp.program_name(), "thunderbird")

    def test_without_a_pid_the_window_class_will_do(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                self.answers("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x1\n",
                             'WM_CLASS(STRING) = "Navigator", "firefox"\n'):
            self.assertEqual(frontapp._x11()[0], "firefox")

    def test_no_active_window_or_no_xprop_is_unknown(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                self.answers("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"):
            self.assertEqual(frontapp._x11()[0], "")
        with mock.patch.object(frontapp.shutil, "which", return_value=None):
            self.assertEqual(frontapp._x11()[0], "")

    def test_a_hung_xprop_is_unknown_rather_than_a_hang(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                mock.patch.object(frontapp.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("xprop", 0.5)):
            self.assertEqual(frontapp._x11()[0], "")


class KDEWayland(DikteTest):
    def test_kdotool_asks_kwin(self):
        replies = [finished("{1234-abcd}\n"), finished("org.kde.konsole\n")]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/kdotool"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies) as run:
            self.assertEqual(frontapp._kde_wayland()[0], "org.kde.konsole")
        self.assertEqual(run.call_args.args[0][:2], ["kdotool", "getwindowclassname"])

    def test_kdotool_without_a_class_falls_back_to_the_process(self):
        replies = [finished("{1234-abcd}\n"), finished(""), finished("777\n")]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/kdotool"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies), \
                mock.patch.object(frontapp, "_process_name", return_value="code"):
            self.assertEqual(frontapp._kde_wayland()[0], "code")

    def test_without_kdotool_it_is_unknown(self):
        with mock.patch.object(frontapp.shutil, "which", return_value=None):
            self.assertEqual(frontapp._kde_wayland()[0], "")


class Windows(DikteTest):
    def api(self, window=0x10, pid=55, image="C:\\Program Files\\Mozilla Thunderbird\\thunderbird.exe"):
        user32, kernel32 = mock.Mock(), mock.Mock()
        user32.GetForegroundWindow.return_value = window

        def thread_process(_window, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = pid
            return 1

        user32.GetWindowThreadProcessId.side_effect = thread_process
        kernel32.OpenProcess.return_value = 0x99

        def image_name(_handle, _flags, buffer, _size):
            buffer.value = image
            return 1

        kernel32.QueryFullProcessImageNameW.side_effect = image_name
        return user32, kernel32

    def test_the_foreground_windows_image_names_the_program(self):
        user32, kernel32 = self.api()
        with mock.patch.object(frontapp, "_win_api", return_value=(user32, kernel32)), \
                mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(frontapp.program_name(), "thunderbird")
        kernel32.CloseHandle.assert_called_once_with(0x99)

    def test_no_foreground_window_is_unknown(self):
        user32, kernel32 = self.api(window=0)
        with mock.patch.object(frontapp, "_win_api", return_value=(user32, kernel32)):
            self.assertEqual(frontapp._windows()[0], "")

    def test_no_user32_at_all_is_unknown(self):
        with mock.patch.object(frontapp, "_win_api", side_effect=OSError):
            self.assertEqual(frontapp._windows()[0], "")


class MacOS(DikteTest):
    def test_no_appkit_is_unknown(self):
        from dikte import mac_window
        with mock.patch.object(mac_window, "_appkit", side_effect=OSError):
            self.assertEqual(frontapp._macos(4242)[0], "")

    def test_no_front_application_is_unknown(self):
        from dikte import mac_window
        with mock.patch.object(mac_window, "frontmost_pid", return_value=None):
            self.assertEqual(frontapp._macos(None)[0], "")


def is_browser(name):
    return name in ("firefox", "org.mozilla.firefox")


class WaylandCompositors(DikteTest):
    def replies(self, *outputs, tool="/usr/bin/tool"):
        return (mock.patch.object(frontapp.shutil, "which", return_value=tool),
                mock.patch.object(frontapp.subprocess, "run",
                                  side_effect=[finished(o) for o in outputs]))

    def answer(self, function, *outputs, title_for=is_browser):
        which, run = self.replies(*outputs)
        with which, run as called:
            result = function(title_for=title_for)
        return result, called

    def test_hyprland(self):
        result, run = self.answer(frontapp._hyprland, json.dumps(
            {"class": "firefox", "initialClass": "firefox", "title": "Gmail",
             "pid": 7}))
        self.assertEqual(result, ("firefox", "Gmail"))
        self.assertEqual(run.call_args.args[0], ["hyprctl", "activewindow", "-j"])
        result, _ = self.answer(frontapp._hyprland, json.dumps(
            {"class": "", "initialClass": "Slack", "title": "general"}))
        self.assertEqual(result, ("Slack", ""))
        self.assertEqual(self.answer(frontapp._hyprland, "{}")[0], ("", ""))
        self.assertEqual(self.answer(frontapp._hyprland, "not json")[0], ("", ""))

    def test_sway_walks_tiled_and_floating_nodes(self):
        tree = {"nodes": [{"nodes": [{"focused": False, "app_id": "foot"}],
                           "floating_nodes": [{"focused": True, "app_id": "firefox",
                                               "name": "YouTube"}]}]}
        result, run = self.answer(frontapp._sway, json.dumps(tree))
        self.assertEqual(result, ("firefox", "YouTube"))
        self.assertEqual(run.call_args.args[0], ["swaymsg", "-t", "get_tree", "-r"])

    def test_sway_under_xwayland_uses_the_x11_class(self):
        tree = {"nodes": [{"focused": True, "app_id": None, "name": "Doc",
                           "window_properties": {"class": "libreoffice-writer"}}]}
        self.assertEqual(self.answer(frontapp._sway, json.dumps(tree))[0],
                         ("libreoffice-writer", ""))
        self.assertEqual(self.answer(frontapp._sway, json.dumps({"nodes": []}))[0],
                         ("", ""))

    def test_niri(self):
        result, run = self.answer(frontapp._niri, json.dumps(
            {"app_id": "org.mozilla.firefox", "title": "WhatsApp"}))
        self.assertEqual(result, ("org.mozilla.firefox", "WhatsApp"))
        self.assertEqual(run.call_args.args[0], ["niri", "msg", "--json", "focused-window"])
        self.assertEqual(self.answer(frontapp._niri, "null")[0], ("", ""))
        self.assertEqual(self.answer(frontapp._niri, json.dumps({"title": "x"}))[0],
                         ("", ""))

    def test_a_missing_tool_or_a_timeout_is_unknown(self):
        for function in (frontapp._hyprland, frontapp._sway, frontapp._niri,
                         frontapp._gnome):
            with self.subTest(function=function.__name__):
                with mock.patch.object(frontapp.shutil, "which", return_value=None):
                    self.assertEqual(function(), ("", ""))
                with mock.patch.object(frontapp.shutil, "which", return_value="/x"), \
                        mock.patch.object(frontapp.subprocess, "run",
                                          side_effect=subprocess.TimeoutExpired("x", 0.5)):
                    self.assertEqual(function(title_for=is_browser), ("", ""))

    def test_gnome_reads_list_then_details_then_the_title(self):
        windows = [{"id": 11, "wm_class": "org.gnome.Nautilus", "pid": 1},
                   {"id": 22, "wm_class": "firefox", "wm_class_instance": "Navigator",
                    "pid": 2}]
        listed = "(" + repr(json.dumps(windows)) + ",)"
        details = "(" + repr(json.dumps({"id": 22, "focus": True})) + ",)"
        title = "('Inbox - Gmail \u2014 Mozilla Firefox',)"
        result, run = self.answer(frontapp._gnome, listed, details, title)
        self.assertEqual(result, ("firefox", "Inbox - Gmail \u2014 Mozilla Firefox"))
        self.assertEqual(len(run.call_args_list), 3)
        self.assertTrue(run.call_args_list[0].args[0][-1].endswith(".List"))
        self.assertEqual(run.call_args_list[1].args[0][-2:],
                         ["org.gnome.Shell.Extensions.Windows.Details", "22"])
        self.assertEqual(run.call_args_list[2].args[0][-2:],
                         ["org.gnome.Shell.Extensions.Windows.GetTitle", "22"])

    def test_gnome_uses_focus_from_the_list_when_it_has_it(self):
        windows = [{"id": 5, "wm_class": "code", "focus": True}]
        result, run = self.answer(frontapp._gnome, "(" + repr(json.dumps(windows)) + ",)")
        self.assertEqual(result, ("code", ""))
        self.assertEqual(run.call_count, 1)

    def test_gnome_without_the_extension_is_unknown(self):
        failed = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Error: GDBus.Error:org.freedesktop.DBus.Error.UnknownObject")
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/gdbus"), \
                mock.patch.object(frontapp.subprocess, "run", return_value=failed) as run:
            self.assertEqual(frontapp._gnome(title_for=is_browser), ("", ""))
        self.assertEqual(run.call_count, 1)

    def test_gvariant_strings_are_read_robustly(self):
        self.assertEqual(frontapp._gvariant_string("('[1, 2]',)\n"), "[1, 2]")
        self.assertEqual(frontapp._gvariant_string("('it\\'s',)"), "it's")
        self.assertEqual(frontapp._gvariant_string("(\"say 'hi'\",)"), "say 'hi'")
        self.assertEqual(frontapp._gvariant_string("garbage ("), "")
        self.assertEqual(frontapp._gvariant_string("(uint32 5,)"), "")

    def test_gnome_asks_details_of_a_bounded_number_of_windows(self):
        windows = [{"id": i, "wm_class": "x"} for i in range(40)]
        outputs = ["(" + repr(json.dumps(windows)) + ",)"] + \
            ["(" + repr(json.dumps({"focus": False})) + ",)"] * 40
        result, run = self.answer(frontapp._gnome, *outputs)
        self.assertEqual(result, ("", ""))
        self.assertEqual(run.call_count, 1 + frontapp.GNOME_DETAILS_LIMIT)


class DesktopNotes(DikteTest):
    def note(self, backend, present=None):
        from dikte import profiles_ui
        with mock.patch.object(frontapp, "backend", return_value=backend), \
                mock.patch.dict(frontapp.gnome_extension, {"present": present}):
            return profiles_ui.ProfilesEditor.detection_text()

    def test_gnome_without_window_calls_says_so(self):
        """Only once a lookup has heard D-Bus say the interface is missing,
        not from a guess at a folder name."""
        self.assertIn("Window Calls", self.note(frontapp._gnome, present=False))
        self.assertNotIn("Window Calls", self.note(frontapp._gnome, present=True))
        self.assertNotIn("Window Calls", self.note(frontapp._gnome, present=None))

    def test_the_gnome_note_appears_when_the_probe_answers_late(self):
        from dikte import profiles_ui
        with mock.patch.object(frontapp, "backend", return_value=frontapp._gnome), \
                mock.patch.dict(frontapp.gnome_extension, {"present": None}), \
                mock.patch.dict(profiles_ui._PROBE, {"started": True}):
            editor = profiles_ui.ProfilesEditor()
            self.assertTrue(editor.detection.isHidden())
            frontapp.gnome_extension["present"] = False
            editor._probed(False)
            self.assertFalse(editor.detection.isHidden())
            self.assertIn("Window Calls", editor.detection.text())

    def test_a_failed_lookup_records_that_the_extension_is_missing(self):
        failed = subprocess.CompletedProcess(
            [], 1, stdout="",
            stderr="Error: GDBus.Error:org.freedesktop.DBus.Error.UnknownObject: No such object")
        with mock.patch.dict(frontapp.gnome_extension, {"present": None}), \
                mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/gdbus"), \
                mock.patch.object(frontapp.subprocess, "run", return_value=failed):
            self.assertIs(frontapp.probe_gnome(), False)
        ok = finished("('[]',)")
        with mock.patch.dict(frontapp.gnome_extension, {"present": None}), \
                mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/gdbus"), \
                mock.patch.object(frontapp.subprocess, "run", return_value=ok):
            self.assertIs(frontapp.probe_gnome(), True)

    def test_the_gnome_note_is_translated(self):
        from dikte import i18n
        i18n.set_language("tr")
        self.assertIn("eklentisi kurulu olmalı", self.note(frontapp._gnome, present=False))

    def test_an_unknown_desktop_says_profiles_are_not_available(self):
        self.assertIn("not available on this desktop", self.note(frontapp._unknown))

    def test_hyprland_sway_and_niri_need_nothing(self):
        for backend in (frontapp._hyprland, frontapp._sway, frontapp._niri):
            with self.subTest(backend=backend.__name__):
                self.assertEqual(self.note(backend), "")
