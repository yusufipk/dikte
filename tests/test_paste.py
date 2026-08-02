"""The clipboard and the key press, which is where a dictation actually lands.

Everything here shells out, so the tools are faked. What the tests hold onto is
the command line: a paste that presses the wrong keys, or in the wrong order,
types nothing and looks like a hang.

Both desktops owe the same promises, so those are written once and run against
each of them. A third one added to paste.py inherits the same list rather than
needing its own copy of it.
"""

import os
import subprocess
import unittest
from typing import ClassVar
from unittest import mock

import paste
from tests.support import (DikteTest, FakeCompleted, linux_only, macos_only,
                           only_these_tools)


@linux_only
class Chooser(DikteTest):
    """Which pair of programs this session's clipboard goes through."""

    def under(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return paste.desktop()

    def test_a_wayland_session(self):
        self.assertIs(self.under(XDG_SESSION_TYPE="wayland",
                                 WAYLAND_DISPLAY="wayland-0"), paste.WAYLAND)

    def test_an_x11_session(self):
        self.assertIs(self.under(XDG_SESSION_TYPE="x11", DISPLAY=":0"), paste.X11)

    def test_a_display_with_no_wayland_beside_it(self):
        self.assertIs(self.under(DISPLAY=":0"), paste.X11)

    def test_an_x11_display_under_wayland_is_still_wayland(self):
        """XWayland sets DISPLAY too; the session type is the one to believe."""
        self.assertIs(self.under(XDG_SESSION_TYPE="wayland",
                                 DISPLAY=":0", WAYLAND_DISPLAY="wayland-0"),
                      paste.WAYLAND)

    def test_nothing_set_at_all(self):
        self.assertIs(self.under(), paste.WAYLAND)


class DesktopContract:
    """What both desktops owe. Each of them subclasses this once, below."""

    env: ClassVar[dict] = {}
    here = None

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))

    # ---- reading the clipboard -------------------------------------------

    def test_no_reader_installed(self):
        with only_these_tools():
            self.assertIsNone(paste.read_clipboard())

    def test_what_is_on_the_clipboard_comes_back_as_bytes(self):
        with only_these_tools(self.here.read_command[0]), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(stdout=b"hello")) as run:
            self.assertEqual(paste.read_clipboard(), b"hello")
        self.assertEqual(run.call_args.args[0], self.here.read_command)

    def test_an_empty_clipboard_is_not_an_error(self):
        with only_these_tools(self.here.read_command[0]), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(returncode=1)):
            self.assertIsNone(paste.read_clipboard())

    def test_a_reader_that_will_not_run(self):
        with only_these_tools(self.here.read_command[0]), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            self.assertIsNone(paste.read_clipboard())

    # ---- copying ----------------------------------------------------------

    def test_no_clipboard_tool_installed_says_what_to_install(self):
        with only_these_tools(), self.assertRaises(paste.PasteError) as caught:
            paste.copy("hello")
        self.assertIn(self.here.clipboard, str(caught.exception))
        self.assertIn(self.here.packages.split(" and ")[0], str(caught.exception))

    def test_the_text_goes_in_as_utf8(self):
        with only_these_tools(self.here.clipboard), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted()) as run:
            paste.copy("günaydın")
        self.assertEqual(run.call_args.args[0], self.here.copy_command)
        self.assertEqual(run.call_args.kwargs["input"], "günaydın".encode())

    def test_the_pipes_are_closed_so_the_call_can_return(self):
        """The clipboard owner forks; a pipe nobody drains hangs the caller."""
        with only_these_tools(self.here.clipboard), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted()) as run:
            paste.copy("hello")
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_a_non_zero_exit_is_reported(self):
        with only_these_tools(self.here.clipboard), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(returncode=1)), \
                self.assertRaises(paste.PasteError):
            paste.copy("hello")

    def test_a_clipboard_tool_that_will_not_run(self):
        with only_these_tools(self.here.clipboard), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")), \
                self.assertRaises(paste.PasteError):
            paste.copy("hello")

    def test_there_is_nothing_to_restore(self):
        with only_these_tools(self.here.clipboard), \
                mock.patch.object(subprocess, "run") as run:
            paste.copy_bytes(None)
        run.assert_not_called()

    def test_restoring_never_raises(self):
        """It runs after the paste went in; failing here must not undo that."""
        with only_these_tools(self.here.clipboard), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            paste.copy_bytes(b"whatever was there before")

    def test_the_bytes_go_back_untouched(self):
        with only_these_tools(self.here.clipboard), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted()) as run:
            paste.copy_bytes(b"\x89PNG\r\n")
        self.assertEqual(run.call_args.kwargs["input"], b"\x89PNG\r\n")

    # ---- pressing the key -------------------------------------------------

    def press(self, shortcut, result=None):
        with only_these_tools(self.here.keyboard), \
                mock.patch.object(paste.time, "sleep", lambda seconds: None), \
                mock.patch.object(subprocess, "run",
                                  return_value=result or FakeCompleted()) as run:
            paste.press(shortcut)
        return run.call_args.args[0]

    def test_no_keyboard_tool_installed(self):
        with only_these_tools():
            self.assertFalse(paste.paste_ready())
            with self.assertRaises(paste.PasteError) as caught:
                paste.press()
        self.assertIn(self.here.keyboard, str(caught.exception))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(self.press(" Ctrl + V "), self.press("ctrl+v"))

    def test_a_key_nobody_mapped_is_refused_before_the_tool_runs(self):
        """Whichever desktop it is, the shortcut is held to one table."""
        with only_these_tools(self.here.keyboard), \
                mock.patch.object(subprocess, "run") as run, \
                self.assertRaises(paste.PasteError) as caught:
            paste.press("ctrl+f13")
        self.assertIn("f13", str(caught.exception))
        run.assert_not_called()

    def test_a_tool_that_will_not_run(self):
        with only_these_tools(self.here.keyboard), \
                mock.patch.object(paste.time, "sleep", lambda seconds: None), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")), \
                self.assertRaises(paste.PasteError):
            paste.press("ctrl+v")

    def test_a_failed_key_press_names_the_tool_and_what_it_said(self):
        with self.assertRaises(paste.PasteError) as caught:
            self.press("ctrl+v", FakeCompleted(returncode=1, stderr="no socket"))
        self.assertIn(self.here.keyboard, str(caught.exception))
        self.assertIn("no socket", str(caught.exception))


@linux_only
class Wayland(DesktopContract, DikteTest):
    env: ClassVar[dict] = {"XDG_SESSION_TYPE": "wayland",
                           "WAYLAND_DISPLAY": "wayland-0"}
    here = paste.WAYLAND

    def test_ydotool_presses_down_then_lets_go_in_reverse(self):
        self.assertEqual(self.press("ctrl+v"),
                         ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"])

    def test_three_keys(self):
        self.assertEqual(self.press("ctrl+shift+v"),
                         ["ydotool", "key", "29:1", "42:1", "47:1",
                          "47:0", "42:0", "29:0"])

    def test_the_synonyms_land_on_the_same_codes(self):
        self.assertEqual(self.press("control+insert"),
                         ["ydotool", "key", "29:1", "110:1", "110:0", "29:0"])
        self.assertEqual(self.press("super+enter"), self.press("meta+return"))

    def test_a_failure_asks_after_the_daemon(self):
        """ydotool needs ydotoold, and says nothing useful when it is not up."""
        with self.assertRaises(paste.PasteError) as caught:
            self.press("ctrl+v", FakeCompleted(returncode=1, stderr="no socket"))
        self.assertIn("ydotoold", str(caught.exception))


@linux_only
class X11(DesktopContract, DikteTest):
    env: ClassVar[dict] = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
    here = paste.X11

    def test_xdotool_takes_the_combination_as_one_argument(self):
        self.assertEqual(self.press("ctrl+v"),
                         ["xdotool", "key", "--clearmodifiers", "ctrl+v"])

    def test_three_keys(self):
        self.assertEqual(self.press("ctrl+shift+v"),
                         ["xdotool", "key", "--clearmodifiers", "ctrl+shift+v"])

    def test_the_keys_x_spells_differently(self):
        """xdotool wants keysyms, not the names the code table is keyed by."""
        self.assertEqual(self.press("control+insert")[-1], "ctrl+Insert")
        self.assertEqual(self.press("meta+enter")[-1], "super+Return")

    def test_no_daemon_to_ask_after(self):
        with self.assertRaises(paste.PasteError) as caught:
            self.press("ctrl+v", FakeCompleted(returncode=1, stderr="bad keysym"))
        self.assertNotIn("ydotoold", str(caught.exception))


@macos_only
class MacOS(DesktopContract, DikteTest):
    """The third group, and it owes the same list as the other two.

    What it does differently is press the key: AppleScript has no press and
    release to spell out, only a keystroke and the modifiers it is held down
    with, so the command is one string rather than a sequence of codes.
    """

    here = paste.MACOS

    def test_the_session_variables_change_nothing_here(self):
        """A Mac has no XDG_SESSION_TYPE, and something that set one anyway
        would otherwise send the clipboard to a wl-copy that is not there."""
        with mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11",
                                          "DISPLAY": ":0"}, clear=True):
            self.assertIs(paste.desktop(), paste.MACOS)

    def test_the_modifiers_are_held_down_around_one_keystroke(self):
        self.assertEqual(
            self.press("cmd+v"),
            ["osascript", "-e", 'tell application "System Events" to '
                                'keystroke "v" using {command down}'])

    def test_three_keys(self):
        self.assertIn("using {command down, shift down}", self.press("cmd+shift+v")[-1])

    def test_command_is_the_key_linux_calls_super(self):
        self.assertEqual(self.press("cmd+v"), self.press("super+v"))
        self.assertEqual(self.press("command+v"), self.press("meta+v"))

    def test_a_key_that_cannot_be_typed_goes_in_by_its_code(self):
        """AppleScript types characters; Return is not one, so it is pressed."""
        self.assertIn("key code 36 using {command down}", self.press("cmd+return")[-1])

    def test_a_combination_with_no_key_in_it_is_refused(self):
        with self.assertRaises(paste.PasteError) as caught:
            paste.MACOS.key_command("cmd+shift")
        self.assertIn("cmd+shift", str(caught.exception))

    def test_a_failure_points_at_the_permission_it_needs(self):
        """osascript is always installed, so a failure is never a missing
        program: it is macOS refusing to let it type."""
        with self.assertRaises(paste.PasteError) as caught:
            self.press("cmd+v", FakeCompleted(
                returncode=1, stderr="osascript is not allowed to send keystrokes"))
        self.assertIn("Accessibility", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
