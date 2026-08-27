"""The clipboard and the key press, which is where a dictation actually lands.

Everything here is faked: the programs the two Linux desktops shell out to, and
the frameworks macOS goes through. What the tests hold onto is what was asked
for. A paste that presses the wrong keys, or in the wrong order, types nothing
and looks like a hang.

Every system owes the same promises about the clipboard, so those are written
once and run against each of them. A fourth one added to paste.py inherits the
same list rather than needing its own copy of it. Each class says which system
it is standing on, which is why none of this is skipped anywhere: the Linux half
is checked on a Mac and the macOS half on Linux, and a change to the chooser
cannot quietly break the platform nobody is sitting at.
"""

import ctypes
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import ClassVar
from unittest import mock

from dikte import paste
from tests.support import DikteTest, FakeCompleted, only_these_tools


class Chooser(DikteTest):
    """Which pair of programs this session's clipboard goes through."""

    def under(self, platform="linux", **env):
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.dict(os.environ, env, clear=True):
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

    def test_a_mac(self):
        self.assertIs(self.under("darwin"), paste.MACOS)

    def test_windows(self):
        self.assertIs(self.under("win32"), paste.WINDOWS)

    def test_a_mac_running_an_x_server_is_still_a_mac(self):
        """XQuartz sets DISPLAY, and none of X's programs are what pastes here."""
        self.assertIs(self.under("darwin", DISPLAY=":0"), paste.MACOS)


class Standing:
    """A test that runs as if it were sitting at one particular system."""

    env: ClassVar[dict] = {}
    platform = "linux"
    here = None

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sys, "platform", self.platform))
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))


class ClipboardContract(Standing):
    """What every system owes the text on its way to the clipboard."""

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

    def test_no_clipboard_tool_installed_names_it(self):
        with only_these_tools(), self.assertRaises(paste.PasteError) as caught:
            paste.copy("hello")
        self.assertIn(self.here.clipboard, str(caught.exception))

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

    def test_the_paste_key_it_offers_is_one_it_can_press(self):
        """Whatever Settings lists, pressing it must not come back unknown."""
        for shortcut in self.here.shortcuts:
            with self.subTest(shortcut=shortcut):
                self.assertTrue(self.pressing(shortcut))


class KeyProgramContract(Standing):
    """The half of it that is another program: ydotool, xdotool."""

    def press(self, shortcut, result=None):
        with only_these_tools(self.here.keyboard), \
                mock.patch.object(paste.time, "sleep", lambda seconds: None), \
                mock.patch.object(subprocess, "run",
                                  return_value=result or FakeCompleted()) as run:
            paste.press(shortcut)
        return run.call_args.args[0]

    def pressing(self, shortcut):
        """The command a shortcut would run, for the contract above."""
        return self.press(shortcut)

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

    def test_nothing_named_presses_the_one_this_desktop_pastes_with(self):
        self.assertEqual(self.press(""), self.press(self.here.shortcuts[0]))


class Wayland(ClipboardContract, KeyProgramContract, DikteTest):
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


class X11(ClipboardContract, KeyProgramContract, DikteTest):
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


class FakeCoreGraphics:
    """Enough of the two frameworks to watch what a paste does to them.

    The events are numbers standing in for pointers, which is all the code
    treats them as: it makes them, sets flags on them, posts them, and hands
    them back.
    """

    def __init__(self, trusted=True, makes=None):
        self.trusted = trusted
        self.makes = makes   # how many it will hand out; None for as many as asked
        self.made = []       # (keycode, is_down)
        self.flags = []      # (event, flags)
        self.posted = []     # (tap, event)
        self.released = []
        self.prompted = []   # the options dictionaries asked with

    # --- ApplicationServices
    def AXIsProcessTrusted(self):
        return self.trusted

    def AXIsProcessTrustedWithOptions(self, options):
        self.prompted.append(options)
        return self.trusted

    def CGEventCreateKeyboardEvent(self, source, keycode, down):
        self.made.append((keycode, down))
        if self.makes is not None and len(self.made) > self.makes:
            return None
        return 1000 + len(self.made)

    def CGEventSetFlags(self, event, flags):
        self.flags.append((event, flags))

    def CGEventPost(self, tap, event):
        self.posted.append((tap, event))

    # --- CoreFoundation
    def CFRelease(self, event):
        self.released.append(event)


class MacOS(ClipboardContract, DikteTest):
    platform = "darwin"
    here = paste.MACOS

    # A stand-in for the CFDictionary: the real one is built out of constants
    # read from the frameworks, which a fake has none of.
    OPTIONS = 4242

    def setUp(self):
        super().setUp()
        self.api = FakeCoreGraphics()
        self.patch_attr(paste, "_macos_api", lambda: (self.api, self.api))
        self.patch_attr(paste, "_macos_prompt_options",
                        lambda services, core: self.OPTIONS)
        self.patch_attr(paste.time, "sleep", lambda seconds: None)
        # It opens the settings pane once per run; each test gets its own run.
        self.patch_attr(paste, "_asked_for_permission", False)
        self.opened = self.patch_attr(paste.subprocess, "Popen", mock.Mock())

    def pressing(self, shortcut):
        paste.press(shortcut)
        return self.api.posted

    def test_the_key_goes_in_by_position_with_the_modifiers_on_it(self):
        paste.press("cmd+v")
        self.assertEqual(self.api.made, [(9, True), (9, False)])
        self.assertEqual([flags for _, flags in self.api.flags],
                         [paste.MAC_FLAGS["command"]] * 2)
        self.assertEqual([tap for tap, _ in self.api.posted],
                         [paste.HID_EVENT_TAP] * 2)

    def test_the_down_is_posted_before_the_up(self):
        paste.press("cmd+v")
        self.assertEqual([event for _, event in self.api.posted], [1001, 1002])

    def test_both_events_are_handed_back(self):
        """CoreGraphics gives out memory that nothing else will free."""
        paste.press("cmd+v")
        self.assertEqual(sorted(self.api.released), [1001, 1002])

    def test_several_modifiers_are_one_number(self):
        paste.press("cmd+shift+v")
        self.assertEqual(self.api.flags[0][1],
                         paste.MAC_FLAGS["command"] | paste.MAC_FLAGS["shift"])

    def test_the_names_a_mac_keyboard_uses(self):
        for name in ("cmd", "command", "meta", "super"):
            with self.subTest(name=name):
                self.assertEqual(paste._macos_keys(f"{name}+v"),
                                 (9, paste.MAC_FLAGS["command"]))
        self.assertEqual(paste._macos_keys("option+v"), paste._macos_keys("alt+v"))
        self.assertEqual(paste._macos_keys("control+v"), paste._macos_keys("ctrl+v"))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(paste._macos_keys(" Cmd + V "), paste._macos_keys("cmd+v"))

    def test_a_key_nobody_mapped_is_refused_before_anything_is_posted(self):
        with self.assertRaises(paste.PasteError) as caught:
            paste.press("cmd+f13")
        self.assertIn("f13", str(caught.exception))
        self.assertEqual(self.api.posted, [])

    def test_a_modifier_nobody_mapped(self):
        with self.assertRaises(paste.PasteError) as caught:
            paste.press("hyper+v")
        self.assertIn("hyper", str(caught.exception))

    def test_nothing_at_all(self):
        with self.assertRaises(paste.PasteError):
            paste.press("+")

    def test_nothing_is_typed_until_macos_says_so(self):
        self.api.trusted = False
        with self.assertRaises(paste.PasteError) as caught:
            paste.press("cmd+v")
        self.assertIn("Accessibility", str(caught.exception))
        self.assertEqual(self.api.posted, [])

    def test_the_permission_pane_is_opened_once_and_not_again(self):
        """It is a window in the user's face, and one paste is every dictation."""
        self.api.trusted = False
        for _ in range(3):
            with self.assertRaises(paste.PasteError):
                paste.press("cmd+v")
        self.opened.assert_called_once()
        self.assertIn("Privacy_Accessibility", self.opened.call_args.args[0][1])

    def test_a_system_that_will_not_open_the_pane_still_says_what_is_wrong(self):
        self.api.trusted = False
        self.opened.side_effect = OSError("no open(1) here")
        with self.assertRaises(paste.PasteError) as caught:
            paste.press("cmd+v")
        self.assertIn("Accessibility", str(caught.exception))

    def test_asking_is_what_puts_dikte_in_the_accessibility_list(self):
        """Opening the pane is not enough on its own.

        AXIsProcessTrusted only answers the question; an application that has
        never asked with the prompt is not in the list, so the pane opens on a
        list Dikte is not in and the only way through is the + button.
        """
        self.api.trusted = False
        with self.assertRaises(paste.PasteError):
            paste.press("cmd+v")
        self.assertEqual(self.api.prompted, [self.OPTIONS])
        # The dictionary is ours to release, and nothing else made an event.
        self.assertIn(self.OPTIONS, self.api.released)

    def test_it_asks_once_however_many_dictations_fail(self):
        self.api.trusted = False
        for _ in range(3):
            with self.assertRaises(paste.PasteError):
                paste.press("cmd+v")
        self.assertEqual(len(self.api.prompted), 1)

    def test_a_trusted_process_is_never_prompted(self):
        paste.press("cmd+v")
        self.assertEqual(self.api.prompted, [])

    def test_readiness_is_the_permission_rather_than_a_program(self):
        self.assertTrue(paste.paste_ready())
        self.api.trusted = False
        self.assertFalse(paste.paste_ready())

    def test_an_event_that_could_not_be_made_takes_its_pair_with_it(self):
        self.api.makes = 1            # the second one comes back null
        with self.assertRaises(paste.PasteError):
            paste.press("cmd+v")
        self.assertEqual(self.api.released, [1001])
        self.assertEqual(self.api.posted, [])

    def test_the_frameworks_not_being_there_is_not_a_crash(self):
        """Every other system imports this module too, and must survive it."""
        self.patch_attr(paste, "_macos_api",
                        mock.Mock(side_effect=paste.PasteError("no such library")))
        self.assertFalse(paste.paste_ready())


class MacPasteGoesWhereTheDictationStarted(MacOS):
    """The keys land in the frontmost window, so the front is what decides
    where a transcript ends up."""

    def setUp(self):
        super().setUp()
        from dikte import mac_window
        self.mac_window = mac_window
        self.activated = []
        self.patch_attr(mac_window, "activate", self.activated.append)

    def frontmost(self, dikte_is):
        self.patch_attr(self.mac_window, "is_frontmost", lambda: dikte_is)

    def test_a_dikte_that_took_the_front_hands_it_back_before_pressing(self):
        self.frontmost(True)
        paste.press("cmd+v", focus=4242)
        self.assertEqual(self.activated, [4242])
        self.assertEqual([event for _, event in self.api.posted], [1001, 1002])

    def test_another_application_in_front_is_where_the_user_went_and_is_left(self):
        self.frontmost(False)
        paste.press("cmd+v", focus=4242)
        self.assertEqual(self.activated, [])

    def test_a_run_that_remembered_nobody_asks_nothing(self):
        self.frontmost(True)
        paste.press("cmd+v")
        self.assertEqual(self.activated, [])

    def test_the_front_is_handed_back_only_once_macos_trusts_dikte(self):
        """Pulling the user out of their window and then failing to type would
        be the worst of both."""
        self.frontmost(True)
        self.api.trusted = False
        with self.assertRaises(paste.PasteError):
            paste.press("cmd+v", focus=4242)
        self.assertEqual(self.activated, [])

class MacClipboardSnapshot(DikteTest):
    def test_every_native_type_is_restored_and_the_files_are_removed(self):
        directory = tempfile.mkdtemp(prefix="dikte-test-clipboard-")
        manifest = '[[{"type":"public.tiff","file":"0-0.bin"}]]'
        pathlib.Path(directory, "0-0.bin").write_bytes(b"a TIFF")
        snapshot = paste._MAC_SNAPSHOT(directory, manifest)

        with mock.patch.object(subprocess, "run",
                               return_value=FakeCompleted()) as run:
            paste.copy_bytes(snapshot)

        self.assertEqual(run.call_args.kwargs["input"], manifest)
        self.assertEqual(run.call_args.kwargs["env"]["DIKTE_PASTEBOARD_DIR"],
                         directory)
        self.assertFalse(os.path.exists(directory))

    def test_a_failed_snapshot_leaves_no_temporary_directory(self):
        directory = tempfile.mkdtemp(prefix="dikte-test-clipboard-")
        with mock.patch.object(paste.tempfile, "mkdtemp", return_value=directory), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(stdout=b"not json")):
            self.assertIsNone(paste._macos_snapshot())
        self.assertFalse(os.path.exists(directory))


class FakeWin32:
    """user32 and kernel32, as much of both as paste.py calls.

    The clipboard is a string held here. A read materialises it as this
    machine's own wide characters, which is what wstring_at reads wherever the
    test runs; a write arrives as the UTF-16 the real clipboard is handed, so
    what the code sent is exactly what is checked.
    """

    def __init__(self):
        self.text = None
        self.buffers = {}
        self.next_handle = 1
        self.pressed = []          # (virtual key, flags), in the order sent
        self.send_result = None    # None: report every event as delivered
        self.held = False          # another program has the clipboard open
        self.out_of_memory = False

    def _keep(self, buffer):
        handle = self.next_handle
        self.next_handle += 1
        self.buffers[handle] = buffer
        return handle

    # --- user32
    def OpenClipboard(self, owner):
        return 0 if self.held else 1

    def CloseClipboard(self):
        return 1

    def EmptyClipboard(self):
        self.text = None
        return 1

    def GetClipboardData(self, fmt):
        if self.text is None:
            return 0
        return self._keep(ctypes.create_unicode_buffer(self.text))

    def SetClipboardData(self, fmt, handle):
        raw = self.buffers[handle].raw
        self.text = raw.decode("utf-16-le").split("\x00", 1)[0]
        return handle

    def SendInput(self, count, inputs, size):
        self.pressed.extend((entry.union.ki.wVk, entry.union.ki.dwFlags)
                            for entry in inputs)
        return count if self.send_result is None else self.send_result

    # --- kernel32
    def GlobalAlloc(self, flags, size):
        if self.out_of_memory:
            return 0
        return self._keep(ctypes.create_string_buffer(size))

    def GlobalLock(self, handle):
        buffer = self.buffers.get(handle)
        return ctypes.addressof(buffer) if buffer else 0

    def GlobalUnlock(self, handle):
        return 1

    def GlobalFree(self, handle):
        self.buffers.pop(handle, None)
        return 1


class Windows(Standing, DikteTest):
    """Windows shells out to nothing: both halves are calls into the system."""

    platform = "win32"
    here = paste.WINDOWS

    def setUp(self):
        super().setUp()
        self.api = FakeWin32()
        self.patch_attr(paste, "_win_api", lambda: (self.api, self.api))
        self.patch_attr(paste.time, "sleep", lambda seconds: None)

    def test_what_is_copied_is_what_reads_back(self):
        paste.copy("ığüşöç İ")
        self.assertEqual(paste.read_clipboard(), "ığüşöç İ".encode("utf-8"))

    def test_an_empty_clipboard_reads_as_empty_text(self):
        self.assertEqual(paste.read_clipboard(), b"")

    def test_what_was_saved_goes_back_after_the_paste(self):
        paste.copy("mine")
        saved = paste.read_clipboard()
        paste.copy("the dictation")
        paste.copy_bytes(saved)
        self.assertEqual(self.api.text, "mine")

    def test_a_copy_that_fails_leaves_what_was_there(self):
        """EmptyClipboard is the point of no return, so nothing runs after it."""
        paste.copy("mine")
        for failure in ("out_of_memory", "held"):
            with self.subTest(failure=failure):
                setattr(self.api, failure, True)
                with self.assertRaises(paste.PasteError):
                    paste.copy("the dictation")
                self.assertEqual(self.api.text, "mine")
                setattr(self.api, failure, False)

    def test_the_handle_is_not_leaked_when_the_copy_fails(self):
        self.api.held = True
        with self.assertRaises(paste.PasteError):
            paste.copy("the dictation")
        self.assertEqual(self.api.buffers, {})

    def test_readiness_asks_for_no_program_and_no_permission(self):
        with only_these_tools():
            self.assertTrue(paste.paste_ready())

    def test_the_keys_go_down_in_order_and_up_in_reverse(self):
        paste.press("ctrl+v")
        keyup = 0x0002
        self.assertEqual(self.api.pressed,
                         [(0x11, 0), (0x56, 0), (0x56, keyup), (0x11, keyup)])

    def test_three_keys(self):
        paste.press("ctrl+shift+v")
        self.assertEqual([code for code, _ in self.api.pressed],
                         [0x11, 0x10, 0x56, 0x56, 0x10, 0x11])

    def test_the_other_spellings_land_on_the_same_keys(self):
        paste.press("super+enter")
        first, self.api.pressed = self.api.pressed, []
        paste.press("meta+return")
        self.assertEqual(self.api.pressed, first)

    def test_a_key_nobody_mapped_is_refused_before_anything_is_sent(self):
        with self.assertRaises(paste.PasteError):
            paste.press("ctrl+f13")
        self.assertEqual(self.api.pressed, [])

    def test_a_press_the_system_did_not_take_says_so(self):
        self.api.send_result = 0
        with self.assertRaises(paste.PasteError) as caught:
            paste.press("ctrl+v")
        self.assertIn("SendInput", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
