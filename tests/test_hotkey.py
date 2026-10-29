"""Parsing a shortcut, and the entry the desktop is asked to register it as."""

import contextlib
import os
import pathlib
import queue
import re
import subprocess
import time
import unittest
from unittest import mock

from PyQt6.QtCore import Qt

from dikte import config as cfg
from dikte import hotkey
from tests.support import DikteTest, FakeCompleted, linux_only

SHORTCUTS_RC = """[services][dikte-toggle.desktop]
_launch=Ctrl+Space

[services][org.kde.spectacle.desktop]
RectangularRegionScreenShot=Meta+Shift+Print\tMeta+Shift+Print\t

[kwin]
Overview=Meta+W,Meta+W,Toggle Overview
Switch Window Down=Meta+Alt+Down,Meta+Alt+Down,Switch to Window Below
"""


class ParseShortcut(unittest.TestCase):
    def test_the_default(self):
        self.assertEqual(hotkey.parse_shortcut("Ctrl+Space"), ({"ctrl"}, 57))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(hotkey.parse_shortcut(" ctrl + SPACE "), ({"ctrl"}, 57))

    def test_several_modifiers(self):
        mods, key = hotkey.parse_shortcut("Ctrl+Alt+Shift+D")
        self.assertEqual(mods, {"ctrl", "alt", "shift"})
        self.assertEqual(key, 32)

    def test_the_synonyms_land_on_one_name(self):
        self.assertEqual(hotkey.parse_shortcut("Control+Space"),
                         hotkey.parse_shortcut("Ctrl+Space"))
        self.assertEqual(hotkey.parse_shortcut("Meta+Space"),
                         hotkey.parse_shortcut("Super+Space"))

    def test_a_key_on_its_own(self):
        self.assertEqual(hotkey.parse_shortcut("F9"), (set(), 67))

    def test_modifiers_with_no_key(self):
        self.assertEqual(hotkey.parse_shortcut("Ctrl+Alt"), (None, None))

    def test_a_key_nobody_mapped(self):
        self.assertEqual(hotkey.parse_shortcut("Ctrl+F13"), (None, None))

    def test_nothing(self):
        self.assertEqual(hotkey.parse_shortcut(""), (None, None))
        self.assertEqual(hotkey.parse_shortcut("+++"), (None, None))

    def test_something_that_is_not_even_a_string(self):
        self.assertEqual(hotkey.parse_shortcut(None), (None, None))


class Table(unittest.TestCase):
    """The one list of global shortcuts. The command line, the settings window
    and install.sh read it instead of keeping a copy each, so what it has to
    hold together is checked here rather than in three places."""

    def test_every_shortcut_remembers_itself_in_a_real_setting(self):
        for name, spec in hotkey.SHORTCUTS.items():
            with self.subTest(name=name):
                self.assertIn(spec.setting, cfg.DEFAULTS)

    def test_no_two_share_a_desktop_entry(self):
        ids = [spec.desktop_id for spec in hotkey.SHORTCUTS.values()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_only_the_toggle_falls_back_to_a_key_of_its_own(self):
        """The rest are off until you pick one, and emptying the box is how you
        turn them off again."""
        self.assertEqual(hotkey.SHORTCUTS["toggle"].fallback, "Ctrl+Space")
        self.assertEqual([name for name, spec in hotkey.SHORTCUTS.items()
                          if spec.fallback], ["toggle"])

    def test_the_uninstaller_takes_every_one_of_them_back(self):
        """uninstall.sh is shell and cannot read the table, so it carries the
        verbs and the desktop files twice; a new shortcut has to be in both."""
        script = (pathlib.Path(__file__).parents[1] / "scripts"
                  / "uninstall.sh").read_text("utf-8")
        verbs = re.search(r"for which in ([\w -]+); do", script).group(1).split()
        ids = re.search(r"for id in ([\w -]+); do", script).group(1).split()
        for name, spec in hotkey.SHORTCUTS.items():
            with self.subTest(name=name):
                self.assertIn(spec.verb, verbs)
                self.assertIn(spec.desktop_id.removesuffix(".desktop"), ids)

    def test_the_fallback_a_mac_gets_is_not_one_macos_already_holds(self):
        """Ctrl+Space switches the input source there and Cmd+Space is
        Spotlight, so the table's own fallback is Linux's and only Linux's."""
        with mock.patch.object(hotkey.sys, "platform", "darwin"):
            self.assertEqual(hotkey.default_combo("toggle"), "Ctrl+Option+Space")
            self.assertEqual(hotkey.default_combo("cancel"), "")
        with mock.patch.object(hotkey.sys, "platform", "linux"):
            self.assertEqual(hotkey.default_combo("toggle"), "Ctrl+Space")
            self.assertEqual(hotkey.default_combo("cancel"), "")


class ModsMatch(unittest.TestCase):
    """The combination has to be exact, or Ctrl+Space fires on Ctrl+Shift+Space."""

    def match(self, held, wanted):
        return hotkey.EvdevHotkey._mods_match(set(held), set(wanted))

    def test_the_wanted_modifier_is_down(self):
        self.assertTrue(self.match({29}, {"ctrl"}))

    def test_either_side_of_the_keyboard_counts(self):
        self.assertTrue(self.match({97}, {"ctrl"}))

    def test_nothing_held_and_nothing_wanted(self):
        self.assertTrue(self.match(set(), set()))

    def test_a_modifier_too_many(self):
        self.assertFalse(self.match({29, 42}, {"ctrl"}))

    def test_a_modifier_missing(self):
        self.assertFalse(self.match(set(), {"ctrl"}))

    def test_the_wrong_modifier(self):
        self.assertFalse(self.match({56}, {"ctrl"}))


@linux_only
class Bindings(DikteTest):
    """start() before it reaches /dev/input, which a test may not read."""

    def test_a_binding_with_no_shortcut_is_skipped(self):
        listener = hotkey.EvdevHotkey()
        with mock.patch.object(listener, "_open_devices", return_value=[]):
            self.assertFalse(listener.start({"toggle": "", "ask": ""}))

    def test_an_unparsable_shortcut_is_reported_and_the_rest_go_on(self):
        listener = hotkey.EvdevHotkey()
        self.addCleanup(listener.stop)
        failures = []
        listener.failed.connect(failures.append)
        with mock.patch.object(listener, "_open_devices", return_value=[99]), \
                mock.patch.object(hotkey.threading, "Thread"):
            self.assertTrue(listener.start({"toggle": "Ctrl+F13",
                                            "ask": "Ctrl+Space"}))
        self.assertEqual(len(failures), 1)
        self.assertIn("Ctrl+F13", failures[0])
        self.assertEqual(list(listener._bindings), [57])

    def test_no_readable_devices_says_what_to_do_about_it(self):
        listener = hotkey.EvdevHotkey()
        failures = []
        listener.failed.connect(failures.append)
        with mock.patch.object(listener, "_open_devices", return_value=[]):
            self.assertFalse(listener.start({"toggle": "Ctrl+Space"}))
        self.assertIn("input", failures[0])

    def test_two_shortcuts_on_one_key_are_both_kept(self):
        listener = hotkey.EvdevHotkey()
        self.addCleanup(listener.stop)
        with mock.patch.object(listener, "_open_devices", return_value=[]), \
                mock.patch.object(hotkey.threading, "Thread") as thread:
            listener._open_devices.return_value = [99]
            self.assertTrue(listener.start({"toggle": "Ctrl+Space",
                                            "ask": "Ctrl+Alt+Space"}))
            thread.assert_called_once()
        self.assertEqual(len(listener._bindings[57]), 2)

    def test_starting_and_discarding_do_not_fire_on_each_other(self):
        """The two defaults are one modifier apart on the same key code, so the
        modifier set is the only thing keeping them apart."""
        listener = hotkey.EvdevHotkey()
        self.addCleanup(listener.stop)
        with mock.patch.object(listener, "_open_devices", return_value=[99]), \
                mock.patch.object(hotkey.threading, "Thread"):
            listener.start({"toggle": "Ctrl+Space", "cancel": "Ctrl+Alt+Space"})

        def fired(held):
            return [name for mods, name in listener._bindings[57]
                    if hotkey.EvdevHotkey._mods_match(held, mods)]

        self.assertEqual(fired({29}), ["toggle"])          # ctrl
        self.assertEqual(fired({29, 56}), ["cancel"])      # ctrl + alt
        self.assertEqual(fired({29, 42}), [])              # ctrl + shift


class Chooser(DikteTest):
    """Which mechanism the session gets, and everything keyed off that."""

    def setUp(self):
        super().setUp()
        self.patch_attr(hotkey.sys, "platform", "linux")
        self.addCleanup(hotkey._REGISTERED.clear)

    @contextlib.contextmanager
    def under(self, desktop, tools=True):
        """A session that says it is this desktop, with or without its tools."""
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}), \
                mock.patch.object(hotkey.shutil, "which",
                                  return_value="/usr/bin/tool" if tools else None):
            yield

    def test_gnome_when_the_session_says_so_and_gsettings_is_there(self):
        with self.under("GNOME"):
            self.assertEqual(hotkey.backend(), hotkey.GNOME)
            self.assertEqual(hotkey.desktop_name(), "GNOME")

    def test_kde_when_the_session_says_so_and_kwriteconfig_is_there(self):
        with self.under("KDE"):
            self.assertEqual(hotkey.backend(), hotkey.KDE)
            self.assertEqual(hotkey.desktop_name(), "KDE")

    def test_a_desktop_with_no_registry_is_the_listeners(self):
        """The bug this replaced: i3 was told KDE, and KWin was not running."""
        for desktop in ("i3", "XFCE", "X-Cinnamon", "sway", "MATE", ""):
            with self.subTest(desktop=desktop), self.under(desktop):
                self.assertEqual(hotkey.backend(), hotkey.LISTENER)

    def test_the_desktop_that_has_no_registry_is_called_by_its_own_name(self):
        with self.under("i3"):
            self.assertEqual(hotkey.desktop_name(), "i3")
        with self.under("XFCE:GNOME-Flashback", tools=False):
            self.assertEqual(hotkey.desktop_name(), "XFCE")
        with self.under(""):
            self.assertEqual(hotkey.desktop_name(), "This desktop")

    def test_a_gnome_session_with_no_gsettings_falls_back_to_the_listener(self):
        """Nothing to write the binding with, and KDE's file is not an answer:
        KWin is no more running here than it is on i3."""
        with self.under("GNOME", tools=False):
            self.assertEqual(hotkey.backend(), hotkey.LISTENER)

    def test_the_desktop_is_matched_loosely(self):
        for desktop in ("GNOME", "ubuntu:GNOME", "gnome"):
            with self.subTest(desktop=desktop), self.under(desktop):
                self.assertEqual(hotkey.backend(), hotkey.GNOME)
        for desktop in ("KDE", "KDE:plasma", "plasma"):
            with self.subTest(desktop=desktop), self.under(desktop):
                self.assertEqual(hotkey.backend(), hotkey.KDE)

    def test_only_a_registry_is_installed_into_and_only_kwin_waits(self):
        with self.under("KDE"):
            self.assertTrue(hotkey.installs_shortcuts())
            self.assertTrue(hotkey.shortcut_needs_restart())
        with self.under("GNOME"):
            self.assertTrue(hotkey.installs_shortcuts())
            self.assertFalse(hotkey.shortcut_needs_restart())
        with self.under("i3"):
            self.assertFalse(hotkey.installs_shortcuts())
            self.assertFalse(hotkey.shortcut_needs_restart())

    def test_installing_goes_to_whichever_it_is(self):
        with self.under("GNOME"), \
                mock.patch.object(hotkey, "install_gnome_shortcut",
                                  return_value=(True, "ok")) as gnome:
            hotkey.install_shortcut("Ctrl+Space", "dikte toggle")
        gnome.assert_called_once()

        with self.under("KDE"), \
                mock.patch.object(hotkey, "install_kde_shortcut",
                                  return_value=(True, "ok")) as kde:
            hotkey.install_shortcut("Ctrl+Space", "dikte toggle")
        kde.assert_called_once()

    def test_a_desktop_with_no_registry_installs_nothing_anywhere(self):
        with self.under("i3"), \
                mock.patch.object(hotkey, "install_kde_shortcut") as kde, \
                mock.patch.object(hotkey, "install_gnome_shortcut") as gnome:
            ok, message = hotkey.install_shortcut("Ctrl+Space", "dikte toggle")
            self.assertEqual(hotkey.shortcut_status(), "Ctrl+Space")
            hotkey.remove_shortcut()
            self.assertIsNone(hotkey.shortcut_status())
        kde.assert_not_called()
        gnome.assert_not_called()
        self.assertTrue(ok)
        self.assertIn("i3", message)
        self.assertNotIn("log out", message)

    def test_removing_and_reading_back_go_to_the_same_one(self):
        with self.under("GNOME"), \
                mock.patch.object(hotkey, "remove_gnome_shortcut") as remove, \
                mock.patch.object(hotkey, "gnome_shortcut_status",
                                  return_value="Ctrl+Space") as status:
            hotkey.remove_shortcut()
            self.assertEqual(hotkey.shortcut_status(), "Ctrl+Space")
        remove.assert_called_once()
        status.assert_called_once()

    def test_only_kde_has_a_list_of_conflicts_to_read(self):
        """A leftover kglobalshortcutsrc from a Plasma the user has since left
        would otherwise refuse combinations nothing is holding."""
        rc = self.path("kglobalshortcutsrc")
        rc.write_text(SHORTCUTS_RC, encoding="utf-8")
        self.patch_attr(hotkey, "SHORTCUTS_FILE", rc)
        with self.under("KDE"):
            self.assertTrue(hotkey.conflicting_shortcuts("Meta+W"))
        with self.under("i3"):
            self.assertEqual(hotkey.conflicting_shortcuts("Meta+W"), [])


@linux_only
class GnomeAccelerator(DikteTest):
    """Qt spells a combination one way, GNOME another."""

    def test_the_default_shortcut(self):
        self.assertEqual(hotkey.gnome_accelerator("Ctrl+Space"), "<Primary>Space")

    def test_several_modifiers_keep_their_order(self):
        self.assertEqual(hotkey.gnome_accelerator("Ctrl+Alt+A"), "<Primary><Alt>a")

    def test_the_synonyms(self):
        self.assertEqual(hotkey.gnome_accelerator("Meta+A"),
                         hotkey.gnome_accelerator("Super+A"))
        self.assertEqual(hotkey.gnome_accelerator("Control+A"),
                         hotkey.gnome_accelerator("Ctrl+A"))

    def test_a_modifier_repeated_is_written_once(self):
        self.assertEqual(hotkey.gnome_accelerator("Ctrl+Control+A"), "<Primary>a")

    def test_modifiers_with_no_key_are_not_a_shortcut(self):
        self.assertEqual(hotkey.gnome_accelerator("Ctrl+Alt"), "")
        self.assertEqual(hotkey.gnome_accelerator(""), "")

    def test_what_goes_out_comes_back_the_way_dikte_writes_it(self):
        for shortcut in ("Ctrl+Space", "Ctrl+Alt+A", "Shift+F9", "Super+M"):
            with self.subTest(shortcut=shortcut):
                accelerator = hotkey.gnome_accelerator(shortcut)
                self.assertEqual(hotkey.display_accelerator(accelerator), shortcut)

    def test_the_control_spelling_gnome_also_uses(self):
        self.assertEqual(hotkey.display_accelerator("<Control>a"), "Ctrl+A")

    def test_an_empty_binding(self):
        self.assertEqual(hotkey.display_accelerator(""), "")


@linux_only
class GsettingsArray(DikteTest):
    def test_a_list_of_paths(self):
        self.assertEqual(
            hotkey._gsettings_array("['/org/gnome/one/', '/org/gnome/two/']"),
            ["/org/gnome/one/", "/org/gnome/two/"])

    def test_the_empty_form_gsettings_prints(self):
        self.assertEqual(hotkey._gsettings_array("@as []"), [])

    def test_nothing_at_all(self):
        self.assertEqual(hotkey._gsettings_array(""), [])

    def test_something_that_is_not_an_array(self):
        with self.assertRaises(ValueError):
            hotkey._gsettings_array("'just a string'")


@linux_only
class GnomeShortcut(DikteTest):
    """The gsettings calls, without a session bus to make them against."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ,
                                          {"XDG_CURRENT_DESKTOP": "GNOME"}))
        self.enterContext(mock.patch.object(hotkey.shutil, "which",
                                            return_value="/usr/bin/gsettings"))

    def gsettings(self, listed="@as []", binding="'<Primary>Space'"):
        def run(cmd, **kwargs):
            if cmd[1] == "get" and cmd[3] == "custom-keybindings":
                return FakeCompleted(stdout=listed)
            return FakeCompleted(stdout=binding)
        return mock.patch.object(subprocess, "run", side_effect=run)

    def written(self, run, key):
        """The value the last `gsettings set ... <key>` was given."""
        for call in reversed(run.mock_calls):
            cmd = call.args[0] if call.args else []
            if len(cmd) > 4 and cmd[1] == "set" and cmd[3] == key:
                return cmd[4]
        return None

    def test_installing_registers_the_path_the_name_and_the_binding(self):
        with self.gsettings() as run:
            ok, message = hotkey.install_shortcut("Ctrl+Space", "dikte toggle")
        self.assertTrue(ok)
        self.assertIn("Ctrl+Space", message)
        self.assertIn(hotkey.DESKTOP_ID.removesuffix(".desktop"),
                      self.written(run, "custom-keybindings"))
        self.assertEqual(self.written(run, "command"), repr("dikte toggle"))
        self.assertEqual(self.written(run, "binding"), repr("<Primary>Space"))

    def test_installing_twice_does_not_list_the_path_twice(self):
        path = hotkey._gnome_path(hotkey.DESKTOP_ID)
        with self.gsettings(listed=repr([path])) as run:
            hotkey.install_shortcut("Ctrl+Space", "dikte toggle")
        self.assertIsNone(self.written(run, "custom-keybindings"))

    def test_each_verb_gets_its_own_path(self):
        self.assertNotEqual(hotkey._gnome_path(hotkey.DESKTOP_ID),
                            hotkey._gnome_path(hotkey.ASK_DESKTOP_ID))

    def test_a_shortcut_gnome_cannot_express(self):
        with self.gsettings():
            ok, message = hotkey.install_shortcut("Ctrl+Alt", "dikte toggle")
        self.assertFalse(ok)
        self.assertIn("Ctrl+Alt", message)

    def test_no_session_bus_to_talk_to(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("no bus")):
            ok, _ = hotkey.install_shortcut("Ctrl+Space", "dikte toggle")
        self.assertFalse(ok)

    def test_reading_back_a_shortcut_that_is_registered(self):
        path = hotkey._gnome_path(hotkey.DESKTOP_ID)
        with self.gsettings(listed=repr([path])):
            self.assertEqual(hotkey.shortcut_status(), "Ctrl+Space")

    def test_reading_back_one_that_is_not(self):
        with self.gsettings(listed="@as []"):
            self.assertIsNone(hotkey.shortcut_status())

    def test_removing_takes_the_path_off_the_list(self):
        path = hotkey._gnome_path(hotkey.DESKTOP_ID)
        with self.gsettings(listed=repr([path, "/org/gnome/other/"])) as run:
            hotkey.remove_shortcut()
        self.assertEqual(self.written(run, "custom-keybindings"),
                         repr(["/org/gnome/other/"]))

    def test_removing_one_that_was_never_installed(self):
        with self.gsettings(listed="@as []"):
            hotkey.remove_shortcut()   # must not raise


@linux_only
class KdeShortcut(DikteTest):
    def setUp(self):
        super().setUp()
        self.apps = self.path("applications")
        self.apps.mkdir(parents=True)
        self.rc = self.path("kglobalshortcutsrc")
        self.patch_attr(hotkey, "APPLICATIONS_DIR", self.apps)
        self.patch_attr(hotkey, "SHORTCUTS_FILE", self.rc)
        # A Plasma session with kwriteconfig6 on it, whatever the machine
        # running the suite happens to be logged into.
        session = mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"})
        session.start()
        self.addCleanup(session.stop)
        self.patch_attr(hotkey.shutil, "which", lambda _name: "/usr/bin/tool")

    def test_installing_writes_a_desktop_file_kwin_will_launch(self):
        with mock.patch.object(subprocess, "run", return_value=FakeCompleted()):
            ok, message = hotkey.install_kde_shortcut("Ctrl+Space", "dikte toggle")
        self.assertTrue(ok)
        text = (self.apps / hotkey.DESKTOP_ID).read_text(encoding="utf-8")
        self.assertIn("Exec=dikte toggle", text)
        self.assertIn("X-KDE-GlobalAccel-CommandShortcut=true", text)
        self.assertIn("log out", message)

    def test_the_shortcut_is_registered_under_the_desktop_id(self):
        with mock.patch.object(subprocess, "run",
                               return_value=FakeCompleted()) as run:
            hotkey.install_kde_shortcut("Meta+D", "dikte ask",
                                        desktop_id=hotkey.ASK_DESKTOP_ID)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "kwriteconfig6")
        self.assertIn(hotkey.ASK_DESKTOP_ID, cmd)
        self.assertEqual(cmd[-1], "Meta+D")

    def test_each_verb_gets_its_own_entry(self):
        with mock.patch.object(subprocess, "run", return_value=FakeCompleted()):
            hotkey.install_kde_shortcut("Ctrl+Space", "dikte toggle")
            hotkey.install_kde_shortcut("Meta+M", "dikte meeting",
                                        desktop_id=hotkey.MEETING_DESKTOP_ID)
        self.assertTrue((self.apps / hotkey.DESKTOP_ID).exists())
        self.assertTrue((self.apps / hotkey.MEETING_DESKTOP_ID).exists())

    def test_no_kwriteconfig_installed(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            ok, message = hotkey.install_kde_shortcut("Ctrl+Space", "dikte toggle")
        self.assertFalse(ok)
        self.assertIn("kglobalshortcutsrc", message)

    def test_removing_takes_the_desktop_file_with_it(self):
        (self.apps / hotkey.DESKTOP_ID).write_text("[Desktop Entry]", encoding="utf-8")
        with mock.patch.object(subprocess, "run", return_value=FakeCompleted()) as run:
            hotkey.remove_kde_shortcut()
        self.assertFalse((self.apps / hotkey.DESKTOP_ID).exists())
        self.assertIn("--delete", run.call_args.args[0])

    def test_removing_one_that_was_never_installed(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            hotkey.remove_kde_shortcut()   # must not raise

    def test_the_registered_shortcut_is_read_back(self):
        (self.apps / hotkey.DESKTOP_ID).write_text("[Desktop Entry]", encoding="utf-8")
        self.rc.write_text(SHORTCUTS_RC, encoding="utf-8")
        self.assertEqual(hotkey.kde_shortcut_status(), "Ctrl+Space")

    def test_no_desktop_file_means_nothing_is_installed(self):
        self.rc.write_text(SHORTCUTS_RC, encoding="utf-8")
        self.assertIsNone(hotkey.kde_shortcut_status())

    def test_a_desktop_file_with_no_entry_beside_it(self):
        (self.apps / hotkey.DESKTOP_ID).write_text("[Desktop Entry]", encoding="utf-8")
        self.rc.write_text("[kwin]\nOverview=Meta+W\n", encoding="utf-8")
        self.assertIsNone(hotkey.kde_shortcut_status())

    def test_no_shortcuts_file_at_all(self):
        (self.apps / hotkey.DESKTOP_ID).write_text("[Desktop Entry]", encoding="utf-8")
        self.assertIsNone(hotkey.kde_shortcut_status())

    def test_a_combination_somebody_else_already_took(self):
        self.rc.write_text(SHORTCUTS_RC, encoding="utf-8")
        hits = hotkey.conflicting_shortcuts("Meta+W")
        self.assertEqual(len(hits), 1)
        self.assertIn("kwin", hits[0])
        self.assertIn("Overview", hits[0])

    def test_our_own_entry_is_not_a_conflict(self):
        self.rc.write_text(SHORTCUTS_RC, encoding="utf-8")
        self.assertEqual(hotkey.conflicting_shortcuts("Ctrl+Space"), [])

    def test_a_free_combination(self):
        self.rc.write_text(SHORTCUTS_RC, encoding="utf-8")
        self.assertEqual(hotkey.conflicting_shortcuts("Ctrl+Alt+J"), [])

    def test_a_tab_separated_entry_is_read_too(self):
        self.rc.write_text(SHORTCUTS_RC, encoding="utf-8")
        hits = hotkey.conflicting_shortcuts("Meta+Shift+Print")
        self.assertEqual(len(hits), 1)
        self.assertIn("spectacle", hits[0])

    def test_no_shortcuts_file_means_no_conflicts(self):
        self.assertEqual(hotkey.conflicting_shortcuts("Ctrl+Space"), [])


# --- macOS ----------------------------------------------------------------

class ParseMacShortcut(unittest.TestCase):
    def test_a_combination_a_mac_would_use(self):
        self.assertEqual(hotkey.parse_macos_shortcut("Cmd+Space"),
                         (hotkey.MAC_MODS["cmd"], 49))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(hotkey.parse_macos_shortcut(" ctrl + option + a "),
                         hotkey.parse_macos_shortcut("Ctrl+Option+A"))

    def test_several_modifiers_are_one_number(self):
        modifiers, key = hotkey.parse_macos_shortcut("Cmd+Shift+M")
        self.assertEqual(modifiers,
                         hotkey.MAC_MODS["cmd"] | hotkey.MAC_MODS["shift"])
        self.assertEqual(key, hotkey.MAC_KEYS["m"])

    def test_the_names_a_mac_keyboard_uses(self):
        for name in ("cmd", "command", "meta", "super"):
            with self.subTest(name=name):
                self.assertEqual(hotkey.parse_macos_shortcut(f"{name}+space"),
                                 hotkey.parse_macos_shortcut("cmd+space"))
        self.assertEqual(hotkey.parse_macos_shortcut("option+a"),
                         hotkey.parse_macos_shortcut("alt+a"))

    def test_a_key_on_its_own(self):
        self.assertEqual(hotkey.parse_macos_shortcut("F5"), (0, 96))

    def test_modifiers_with_no_key(self):
        self.assertEqual(hotkey.parse_macos_shortcut("Cmd+Shift"), (None, None))

    def test_a_key_nobody_mapped(self):
        self.assertEqual(hotkey.parse_macos_shortcut("Cmd+F13"), (None, None))

    def test_two_keys_are_not_a_shortcut(self):
        self.assertEqual(hotkey.parse_macos_shortcut("A+B"), (None, None))

    def test_nothing(self):
        self.assertEqual(hotkey.parse_macos_shortcut(""), (None, None))
        self.assertEqual(hotkey.parse_macos_shortcut(None), (None, None))


class FakeCarbon:
    """Enough of Carbon to watch what the listener asks it for.

    The references are numbers standing in for pointers, which is all the code
    does with them: it collects them and hands them back to be unregistered.
    """

    def __init__(self, install=0, register=0):
        self.install, self.register = install, register   # what they return
        self.registered = []     # (key, modifiers, identifier)
        self.unregistered = []
        self.handlers_removed = 0
        self.pressed_id = 0
        self.parameter_result = 0

    def GetApplicationEventTarget(self):
        return 7000

    def InstallEventHandler(self, target, callback, count, spec, data, out):
        if self.install == 0:
            out._obj.value = 8000
        return self.install

    def RegisterEventHotKey(self, key, modifiers, identifier, target, options, out):
        if self.register != 0:
            return self.register
        self.registered.append((key, modifiers, identifier.id))
        out._obj.value = 9000 + len(self.registered)
        return 0

    def UnregisterEventHotKey(self, reference):
        self.unregistered.append(reference.value)
        return 0

    def RemoveEventHandler(self, handler):
        self.handlers_removed += 1
        return 0

    def GetEventParameter(self, event, kind, name, wanted_type, size, out_size, out):
        out._obj.id = self.pressed_id
        return self.parameter_result


class CarbonListener(DikteTest):
    """What the listener asks macOS for, without a Mac to ask."""

    def setUp(self):
        super().setUp()
        self.carbon = FakeCarbon()
        self.patch_attr(hotkey, "_carbon", lambda: self.carbon)
        self.addCleanup(hotkey._REGISTERED.clear)
        self.listener = hotkey.CarbonHotkey()
        self.addCleanup(self.listener.stop)
        self.failures = []
        self.listener.failed.connect(self.failures.append)

    def test_every_binding_is_asked_for_by_position_and_modifier(self):
        self.assertTrue(self.listener.start({"toggle": "Ctrl+Option+Space"}))
        self.assertEqual(self.carbon.registered,
                         [(49, hotkey.MAC_MODS["ctrl"] | hotkey.MAC_MODS["option"], 1)])
        self.assertEqual(self.failures, [])
        self.assertTrue(self.listener.running)

    def test_a_binding_with_no_shortcut_is_skipped(self):
        self.assertFalse(self.listener.start({"toggle": "", "ask": ""}))
        self.assertEqual(self.carbon.registered, [])
        self.assertFalse(self.listener.running)

    def test_an_unparsable_shortcut_is_reported_and_the_rest_go_on(self):
        self.assertTrue(self.listener.start({"toggle": "Cmd+F13",
                                             "ask": "Cmd+Shift+Space"}))
        self.assertEqual(len(self.failures), 1)
        self.assertIn("Cmd+F13", self.failures[0])
        self.assertEqual(len(self.carbon.registered), 1)

    def test_a_combination_another_application_already_holds(self):
        """The conflict warning macOS has: it is the answer to asking."""
        self.carbon.register = -9878        # eventHotKeyExistsErr
        self.assertFalse(self.listener.start({"toggle": "Cmd+Shift+Space"}))
        self.assertIn("Cmd+Shift+Space", self.failures[0])
        self.assertFalse(self.listener.running)

    def test_a_handler_that_will_not_install(self):
        self.carbon.install = -50
        self.assertFalse(self.listener.start({"toggle": "Cmd+Shift+Space"}))
        self.assertEqual(self.carbon.registered, [])
        self.assertEqual(len(self.failures), 1)

    def test_no_carbon_to_talk_to(self):
        self.patch_attr(hotkey, "_carbon",
                        mock.Mock(side_effect=OSError("no such library")))
        self.assertFalse(self.listener.start({"toggle": "Cmd+Shift+Space"}))
        self.assertIn("no such library", self.failures[0])

    def test_the_key_press_arrives_under_the_name_it_was_registered_with(self):
        self.listener.start({"toggle": "Cmd+Shift+Space", "ask": "Cmd+Shift+A"})
        heard = []
        self.listener.triggered.connect(heard.append)
        self.carbon.pressed_id = 2          # the second binding, which is "ask"
        self.listener._callback(None, 0, None)
        self.assertEqual(heard, ["ask"])

    def test_a_press_carbon_could_not_identify_is_dropped(self):
        self.listener.start({"toggle": "Cmd+Shift+Space"})
        heard = []
        self.listener.triggered.connect(heard.append)
        self.carbon.parameter_result = -50
        self.listener._callback(None, 0, None)
        self.assertEqual(heard, [])

    def test_stopping_hands_every_registration_back(self):
        self.listener.start({"toggle": "Cmd+Shift+Space", "ask": "Cmd+Shift+A"})
        self.listener.stop()
        self.assertEqual(self.carbon.unregistered, [9001, 9002])
        self.assertEqual(self.carbon.handlers_removed, 1)
        self.assertFalse(self.listener.running)

    def test_starting_twice_does_not_leave_the_first_set_behind(self):
        self.listener.start({"toggle": "Cmd+Shift+Space"})
        self.listener.start({"toggle": "Cmd+Shift+A"})
        self.assertEqual(self.carbon.unregistered, [9001])
        self.assertEqual(len(self.listener._registrations), 1)

    def test_what_it_registered_is_what_the_status_line_reads_back(self):
        with mock.patch.object(hotkey.sys, "platform", "darwin"):
            self.listener.start({"toggle": "Ctrl+Option+Space",
                                 "meeting": "Ctrl+Option+M"})
            self.assertEqual(hotkey.shortcut_status(), "Ctrl+Option+Space")
            self.assertEqual(hotkey.shortcut_status(hotkey.MEETING_DESKTOP_ID),
                             "Ctrl+Option+M")
            self.listener.stop()
            self.assertIsNone(hotkey.shortcut_status())


class MacChooser(DikteTest):
    """What the shortcut verbs mean where there is nothing to write them into."""

    def setUp(self):
        super().setUp()
        self.patch_attr(hotkey.sys, "platform", "darwin")
        self.addCleanup(hotkey._REGISTERED.clear)

    def test_the_listener_is_the_one_macos_has(self):
        self.assertIsInstance(hotkey.listener(), hotkey.CarbonHotkey)

    def test_everywhere_else_reads_the_keyboard_itself(self):
        with mock.patch.object(hotkey.sys, "platform", "linux"):
            self.assertIsInstance(hotkey.listener(), hotkey.EvdevHotkey)

    def test_there_is_nothing_to_install_into(self):
        self.assertFalse(hotkey.installs_shortcuts())
        self.assertFalse(hotkey.shortcut_needs_restart())
        self.assertEqual(hotkey.desktop_name(), "macOS")

    def test_installing_records_it_rather_than_writing_anything(self):
        with mock.patch.object(hotkey.subprocess, "run") as run:
            ok, message = hotkey.install_shortcut("Cmd+Shift+Space", "dikte toggle")
        run.assert_not_called()
        self.assertTrue(ok)
        self.assertIn("Cmd+Shift+Space", message)
        self.assertEqual(hotkey.shortcut_status(), "Cmd+Shift+Space")

    def test_removing_takes_it_back_out(self):
        hotkey.install_shortcut("Cmd+Shift+Space", "dikte toggle")
        hotkey.remove_shortcut()
        self.assertIsNone(hotkey.shortcut_status())

    def test_each_verb_is_kept_apart(self):
        hotkey.install_shortcut("Cmd+Shift+Space", "dikte toggle")
        hotkey.install_shortcut("Cmd+Shift+M", "dikte meeting",
                                desktop_id=hotkey.MEETING_DESKTOP_ID)
        self.assertEqual(hotkey.shortcut_status(), "Cmd+Shift+Space")
        self.assertEqual(hotkey.shortcut_status(hotkey.MEETING_DESKTOP_ID),
                         "Cmd+Shift+M")

    def test_no_list_of_conflicts_to_read(self):
        """Not even KDE's file, which a Mac could well have a copy of."""
        self.assertEqual(hotkey.conflicting_shortcuts("Ctrl+Space"), [])

    def test_a_combination_is_checked_against_the_mac_table(self):
        self.assertTrue(hotkey.valid_shortcut("Cmd+Shift+Space"))
        self.assertFalse(hotkey.valid_shortcut("Ctrl+F13"))

    def test_the_other_table_is_the_one_used_elsewhere(self):
        with mock.patch.object(hotkey.sys, "platform", "linux"):
            self.assertTrue(hotkey.valid_shortcut("Ctrl+F1"))
            self.assertFalse(hotkey.valid_shortcut("Cmd+Space"))


# --- Windows ----------------------------------------------------------------

class ParseWindowsShortcut(unittest.TestCase):
    def test_the_default(self):
        self.assertEqual(hotkey.parse_windows_shortcut("Ctrl+Space"),
                         (hotkey.WIN_MODS["ctrl"], 0x20))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(hotkey.parse_windows_shortcut(" ctrl + SPACE "),
                         hotkey.parse_windows_shortcut("Ctrl+Space"))

    def test_several_modifiers_are_one_number(self):
        modifiers, key = hotkey.parse_windows_shortcut("Ctrl+Shift+M")
        self.assertEqual(modifiers,
                         hotkey.WIN_MODS["ctrl"] | hotkey.WIN_MODS["shift"])
        self.assertEqual(key, hotkey.WIN_KEYS["m"])

    def test_the_synonyms_land_on_one_number(self):
        for name in ("meta", "super", "win"):
            with self.subTest(name=name):
                self.assertEqual(hotkey.parse_windows_shortcut(f"{name}+space"),
                                 (hotkey.WIN_MODS["win"], 0x20))
        self.assertEqual(hotkey.parse_windows_shortcut("Control+Space"),
                         hotkey.parse_windows_shortcut("Ctrl+Space"))

    def test_a_key_on_its_own(self):
        self.assertEqual(hotkey.parse_windows_shortcut("F9"),
                         (0, hotkey.WIN_KEYS["f9"]))

    def test_modifiers_with_no_key(self):
        self.assertEqual(hotkey.parse_windows_shortcut("Ctrl+Alt"), (None, None))

    def test_a_key_nobody_mapped(self):
        self.assertEqual(hotkey.parse_windows_shortcut("Ctrl+F13"), (None, None))

    def test_something_that_is_not_even_a_string(self):
        self.assertEqual(hotkey.parse_windows_shortcut(None), (None, None))


class FakeWinHotkeys:
    """user32 and kernel32, as much of both as the listener calls.

    The message queue is a real queue: GetMessageW blocks on it the way the
    real one blocks on the thread's, so the listener runs its actual loop and
    a test presses the key by posting the message a press would.
    """

    def __init__(self):
        self.registered = {}    # identifier -> (modifiers, key)
        self.refused = set()    # (modifiers, key) another program holds
        self.unregistered = []
        self.queue = queue.Queue()

    # --- user32
    def RegisterHotKey(self, hwnd, identifier, modifiers, key):
        if (modifiers & ~hotkey.WIN_MOD_NOREPEAT, key) in self.refused:
            return 0
        self.registered[identifier] = (modifiers, key)
        return 1

    def UnregisterHotKey(self, hwnd, identifier):
        self.unregistered.append(identifier)
        self.registered.pop(identifier, None)
        return 1

    def PeekMessageW(self, reference, hwnd, low, high, remove):
        return 0

    def GetMessageW(self, reference, hwnd, low, high):
        kind, wparam = self.queue.get()
        if kind == hotkey.WM_QUIT:
            return 0
        message = reference._obj
        message.message = kind
        message.wParam = wparam
        return 1

    def PostThreadMessageW(self, thread_id, message, wparam, lparam):
        self.queue.put((message, wparam))
        return 1

    # --- kernel32
    def GetCurrentThreadId(self):
        return 1

    # --- the keyboard
    def press(self, identifier):
        self.queue.put((hotkey.WM_HOTKEY, identifier))


class WinListener(DikteTest):
    """What the listener asks Windows for, without a Windows to ask."""

    def setUp(self):
        super().setUp()
        self.api = FakeWinHotkeys()
        self.patch_attr(hotkey, "_win_input", lambda: (self.api, self.api))
        self.addCleanup(hotkey._REGISTERED.clear)
        self.listener = hotkey.WinHotkey()
        self.addCleanup(self.listener.stop)
        self.failures = []
        # Direct, because the emits come from the listener's own thread and
        # there is no event loop here to carry a queued one across.
        self.listener.failed.connect(self.failures.append,
                                     Qt.ConnectionType.DirectConnection)

    @staticmethod
    def settles(seen, count=1):
        """The signals arrive from the listener's own thread, not this one."""
        deadline = time.monotonic() + 2
        while len(seen) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return seen

    def test_every_binding_is_registered_with_its_modifiers(self):
        self.assertTrue(self.listener.start({"toggle": "Ctrl+Space",
                                             "cancel": "Ctrl+Shift+Space"}))
        norepeat = hotkey.WIN_MOD_NOREPEAT
        self.assertEqual(self.api.registered, {
            1: (hotkey.WIN_MODS["ctrl"] | norepeat, 0x20),
            2: (hotkey.WIN_MODS["ctrl"] | hotkey.WIN_MODS["shift"] | norepeat, 0x20),
        })

    def test_what_landed_is_what_the_status_line_shows(self):
        self.listener.start({"toggle": "Ctrl+Space"})
        self.assertEqual(hotkey._REGISTERED,
                         {hotkey.DESKTOP_ID: "Ctrl+Space"})

    def test_a_press_arrives_under_the_name_it_was_registered_as(self):
        seen = []
        self.listener.triggered.connect(seen.append,
                                        Qt.ConnectionType.DirectConnection)
        self.listener.start({"toggle": "Ctrl+Space", "cancel": "Ctrl+Shift+Space"})
        self.api.press(2)
        self.assertEqual(self.settles(seen), ["cancel"])

    def test_a_held_combination_is_reported_and_the_rest_still_land(self):
        self.api.refused = {(hotkey.WIN_MODS["ctrl"], 0x20)}
        started = self.listener.start({"toggle": "Ctrl+Space",
                                       "cancel": "Ctrl+Shift+Space"})
        self.assertTrue(started)
        self.assertIn("Ctrl+Space", self.settles(self.failures)[0])
        self.assertEqual(list(self.api.registered), [2])

    def test_an_unparsable_binding_is_reported(self):
        self.assertFalse(self.listener.start({"toggle": "Ctrl+F13"}))
        self.assertIn("Ctrl+F13", self.failures[0])

    def test_nothing_but_empty_bindings_does_not_start(self):
        self.assertFalse(self.listener.start({"toggle": "", "cancel": ""}))
        self.assertFalse(self.listener.running)

    def test_stop_lets_go_of_everything(self):
        self.listener.start({"toggle": "Ctrl+Space", "cancel": "Ctrl+Shift+Space"})
        self.listener.stop()
        self.assertEqual(self.api.registered, {})
        self.assertEqual(hotkey._REGISTERED, {})
        self.assertFalse(self.listener.running)

    def test_a_second_start_is_a_clean_slate(self):
        self.listener.start({"toggle": "Ctrl+Space"})
        self.assertTrue(self.listener.start({"toggle": "Ctrl+Shift+Space"}))
        self.assertEqual(self.api.registered,
                         {1: (hotkey.WIN_MODS["ctrl"] | hotkey.WIN_MODS["shift"]
                              | hotkey.WIN_MOD_NOREPEAT, 0x20)})


class WindowsChooser(DikteTest):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(hotkey.sys, "platform", "win32"))
        self.addCleanup(hotkey._REGISTERED.clear)

    def test_the_listener_is_the_windows_hotkey_service(self):
        self.assertIsInstance(hotkey.listener(), hotkey.WinHotkey)

    def test_a_combination_is_checked_against_the_windows_table(self):
        self.assertTrue(hotkey.valid_shortcut("Ctrl+Space"))
        self.assertFalse(hotkey.valid_shortcut("Ctrl+F13"))

    def test_no_registry_to_write_into_and_no_restart_to_wait_for(self):
        self.assertFalse(hotkey.installs_shortcuts())
        self.assertFalse(hotkey.shortcut_needs_restart())
        self.assertEqual(hotkey.desktop_name(), "Windows")

    def test_installing_records_it_rather_than_writing_anything(self):
        with mock.patch.object(hotkey.subprocess, "run") as run:
            ok, message = hotkey.install_shortcut("Ctrl+Space", "dikte toggle")
        run.assert_not_called()
        self.assertTrue(ok)
        self.assertEqual(hotkey.shortcut_status(), "Ctrl+Space")
        hotkey.remove_shortcut()
        self.assertIsNone(hotkey.shortcut_status())

    def test_no_list_of_conflicts_to_read(self):
        """Not even KDE's file, which a dual-boot home directory could hold."""
        self.assertEqual(hotkey.conflicting_shortcuts("Ctrl+Space"), [])


if __name__ == "__main__":
    unittest.main()
