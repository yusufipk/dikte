"""The contract every platform adapter owes, and the other platform's answers.

Two jobs. The first is to say what an adapter is: four modules, each offering a
fixed set of names, so that a port is a directory rather than a branch inside
every function, and so that a name added to one platform and forgotten on the
other is caught here rather than by whoever runs the other one.

The second is the one worth having. Most of this port was written on Windows,
where nothing checks that Linux still gets its XDG directories, its evdev key
codes, its Ubuntu tarballs and a socket named after its user id. Those are
pinned below, loaded through `platforms.adapter(part, name="linux")`, which
imports the Linux half on any machine: it is plain Python and PyQt6, and none
of it calls anything Linux-only until something asks it to.

The reverse does not work. The Windows adapters open user32 and kernel32 at
import, so a Linux machine cannot load them at all, and Windows is where this
half of the guarding has to happen.
"""

import os
import unittest
from unittest import mock

import platforms
from tests.support import windows_only

# What each part of an adapter has to offer, whatever the platform. A name
# missing from one of them is an application that works on one desktop.
CONTRACT = {
    "audio": ("Recorder", "MeetingRecorder",
              "list_sources", "list_monitors", "default_monitor"),
    "clipboard": ("PasteError", "read_clipboard", "copy", "copy_bytes",
                  "paste_ready", "press"),
    "hotkeys": ("Listener", "parse_shortcut", "install_shortcut",
                "remove_shortcut", "shortcut_status", "conflicting_shortcuts",
                "desktop_name"),
    "runtime": ("APP_DIR", "RECORDINGS_NAME", "MEETINGS_NAME", "PROGRAMS",
                "NO_WINDOW",
                "config_dir", "data_dir", "cache_dir",
                "user_id", "single_instance",
                "protect", "unprotect", "secure_file",
                "adopt", "is_our_process", "terminate",
                "signals", "wakeup_socketpair", "abort_socket",
                "prepare_console", "relaunch", "open_folder"),
}


class Contract(unittest.TestCase):
    """This platform's adapters, whichever platform is running the tests."""

    def test_every_part_is_here(self):
        for part in CONTRACT:
            with self.subTest(part=part):
                self.assertIsNotNone(platforms.adapter(part))

    def test_every_part_offers_what_the_contract_says(self):
        for part, names in CONTRACT.items():
            module = platforms.adapter(part)
            for attribute in names:
                with self.subTest(part=part, name=attribute):
                    self.assertTrue(hasattr(module, attribute),
                                    f"{module.__name__} has no {attribute}")

    def test_the_platform_is_supported(self):
        self.assertIn(platforms.NAME,
                      (platforms.LINUX, platforms.WINDOWS, platforms.MACOS))
        self.assertEqual(platforms.IS_WINDOWS, platforms.NAME == platforms.WINDOWS)
        self.assertEqual(platforms.IS_MACOS, platforms.NAME == platforms.MACOS)

    def test_an_adapter_is_imported_once_and_remembered(self):
        self.assertIs(platforms.adapter("runtime"), platforms.adapter("runtime"))

    def test_take_answers_none_for_what_a_platform_does_not_have(self):
        found, missing = platforms.take("runtime", "user_id", "no_such_name")
        self.assertTrue(callable(found))
        self.assertIsNone(missing)


@windows_only
class TheLinuxHalf(unittest.TestCase):
    """What Linux is owed, checked from the machine the port was written on.

    Loaded rather than assumed: these are the answers the Linux application has
    always given, and every one of them is something a careless change on this
    side could quietly move.
    """

    def part(self, name):
        return platforms.adapter(name, name="linux")

    # ---- the shape -------------------------------------------------------

    def test_it_offers_the_same_contract(self):
        for part, names in CONTRACT.items():
            module = self.part(part)
            for attribute in names:
                with self.subTest(part=part, name=attribute):
                    self.assertTrue(hasattr(module, attribute),
                                    f"{module.__name__} has no {attribute}")

    # ---- where things live -----------------------------------------------

    def test_the_directories_are_the_xdg_ones(self):
        runtime = self.part("runtime")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/home/x/.config",
                                          "XDG_DATA_HOME": "/home/x/.local/share",
                                          "XDG_CACHE_HOME": "/home/x/.cache"}):
            self.assertEqual(runtime.config_dir().as_posix(), "/home/x/.config/dikte")
            self.assertEqual(runtime.data_dir().as_posix(),
                             "/home/x/.local/share/dikte")
            self.assertEqual(runtime.cache_dir().as_posix(), "/home/x/.cache/dikte")

    def test_the_directory_names_stay_lowercase(self):
        runtime = self.part("runtime")
        self.assertEqual(runtime.APP_DIR, "dikte")
        self.assertEqual(runtime.RECORDINGS_NAME, "recordings")
        self.assertEqual(runtime.MEETINGS_NAME, "meetings")

    def test_the_socket_is_named_after_the_numeric_user_id(self):
        runtime = self.part("runtime")
        with mock.patch.object(os, "getuid", lambda: 1000, create=True):
            self.assertEqual(runtime.user_id(), "1000")

    # ---- secrets ---------------------------------------------------------

    def test_a_key_is_stored_as_it_was_typed(self):
        """The mode on the file is the protection there, not the bytes in it."""
        runtime = self.part("runtime")
        self.assertEqual(runtime.protect("sk-secret"), "sk-secret")
        self.assertEqual(runtime.unprotect("sk-secret"), "sk-secret")

    # ---- processes -------------------------------------------------------

    def test_nothing_is_asked_of_a_subprocess_that_linux_does_not_have(self):
        self.assertEqual(self.part("runtime").NO_WINDOW, {})

    def test_the_programs_it_expects_are_the_ones_it_shells_out_to(self):
        self.assertEqual(
            self.part("runtime").PROGRAMS,
            ("pw-record", "wl-copy", "ydotool", "ffmpeg", "pactl", "kwriteconfig6"))

    # ---- the desktop -----------------------------------------------------

    def test_the_listener_is_the_evdev_one(self):
        hotkeys = self.part("hotkeys")
        self.assertIs(hotkeys.Listener, hotkeys.EvdevHotkey)

    def test_the_key_codes_are_evdev_codes(self):
        hotkeys = self.part("hotkeys")
        self.assertEqual(hotkeys.parse_shortcut("Ctrl+Space"), ({"ctrl"}, 57))
        self.assertEqual(hotkeys.parse_shortcut("F9"), (set(), 67))

    def test_the_clipboard_still_goes_through_two_programs(self):
        clipboard = self.part("clipboard")
        self.assertEqual(clipboard.WAYLAND.clipboard, "wl-copy")
        self.assertEqual(clipboard.WAYLAND.keyboard, "ydotool")
        self.assertEqual(clipboard.X11.clipboard, "xclip")
        self.assertEqual(clipboard.X11.keyboard, "xdotool")

    def test_the_recorder_still_asks_the_sound_server(self):
        audio = self.part("audio")
        with mock.patch("shutil.which", side_effect=lambda tool:
                        "/usr/bin/parec" if tool == "parec" else None):
            command = audio.recording_command("alsa_input.usb")
        self.assertEqual(command[0], "parec")
        self.assertIn("--device=alsa_input.usb", command)

    def test_the_release_it_wants_is_still_an_ubuntu_tarball(self):
        import ggml

        with mock.patch.object(ggml, "IS_WINDOWS", False), \
                mock.patch.object(ggml, "_arch", lambda: "x64"), \
                mock.patch.object(ggml, "_has_vulkan", lambda: False):
            self.assertEqual(ggml._wanted_assets(ggml.WHISPER),
                             (r"bin-ubuntu-x64\.tar\.gz$",))
            self.assertEqual(ggml.backend_choices(ggml.LLAMA),
                             ("auto", "cpu", "vulkan"))

    def test_the_binary_has_no_extension_there(self):
        import ggml

        with mock.patch.object(ggml, "EXE", ""):
            self.assertEqual(ggml.binary_name(ggml.WHISPER), "whisper-server")


if __name__ == "__main__":
    unittest.main()
