"""Parsing a shortcut, and the entry the desktop is asked to register it as."""

import contextlib
import os
import subprocess
import unittest
from unittest import mock

import config as cfg
import hotkey
from tests.support import DikteTest, FakeCompleted, linux_only, macos_only

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
        self.assertEqual(hotkey.SHORTCUTS["toggle"].fallback,
                         cfg.DEFAULTS["shortcut"])
        self.assertEqual([name for name, spec in hotkey.SHORTCUTS.items()
                          if spec.fallback], ["toggle"])

    def test_the_fallback_is_a_key_this_platform_can_actually_bind(self):
        """Two lists of key names, and a fallback missing from the one in use
        is a first run with no working shortcut at all."""
        for name, spec in hotkey.SHORTCUTS.items():
            if spec.fallback:
                with self.subTest(name=name):
                    self.assertTrue(hotkey.understands(spec.fallback))


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


@linux_only
class Chooser(DikteTest):
    """Which desktop is asked to register the shortcut."""

    @contextlib.contextmanager
    def under(self, desktop, has_gsettings=True):
        """A session that says it is this desktop, with or without gsettings."""
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}), \
                mock.patch.object(hotkey.shutil, "which",
                                  return_value="/usr/bin/gsettings"
                                  if has_gsettings else None):
            yield

    def test_gnome_when_the_session_says_so_and_gsettings_is_there(self):
        with self.under("GNOME"):
            self.assertEqual(hotkey.desktop_name(), "GNOME")

    def test_kde_otherwise(self):
        with self.under("KDE"):
            self.assertEqual(hotkey.desktop_name(), "KDE")

    def test_a_gnome_session_with_no_gsettings_falls_back(self):
        """Nothing to write the binding with, so KDE's file is the only try."""
        with self.under("GNOME", has_gsettings=False):
            self.assertEqual(hotkey.desktop_name(), "KDE")

    def test_the_desktop_is_matched_loosely(self):
        for desktop in ("GNOME", "ubuntu:GNOME", "gnome"):
            with self.subTest(desktop=desktop), self.under(desktop):
                self.assertEqual(hotkey.desktop_name(), "GNOME")

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

    def test_removing_and_reading_back_go_to_the_same_one(self):
        with self.under("GNOME"), \
                mock.patch.object(hotkey, "remove_gnome_shortcut") as remove, \
                mock.patch.object(hotkey, "gnome_shortcut_status",
                                  return_value="Ctrl+Space") as status:
            hotkey.remove_shortcut()
            self.assertEqual(hotkey.shortcut_status(), "Ctrl+Space")
        remove.assert_called_once()
        status.assert_called_once()


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


@macos_only
class ParseMacShortcut(unittest.TestCase):
    """Combination to (Carbon modifier mask, virtual key code).

    A second table because the numbers are macOS's own and the key names are
    not quite the same list: there is no Insert on a Mac keyboard, and F13 to
    F19 exist there and not in the evdev table.
    """

    def test_the_default(self):
        # controlKey | optionKey, and kVK_Space.
        self.assertEqual(hotkey.parse_mac_shortcut("Ctrl+Alt+Space"),
                         (0x1000 | 0x0800, 0x31))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(hotkey.parse_mac_shortcut(" ctrl + ALT + space "),
                         hotkey.parse_mac_shortcut("Ctrl+Alt+Space"))

    def test_command_answers_to_the_names_the_other_platform_uses(self):
        """A settings file carried over from Linux says Meta or Super."""
        for name in ("Command", "Meta", "Super"):
            with self.subTest(name=name):
                self.assertEqual(hotkey.parse_mac_shortcut(f"{name}+A"),
                                 hotkey.parse_mac_shortcut("Cmd+A"))

    def test_option_is_the_key_linux_calls_alt(self):
        self.assertEqual(hotkey.parse_mac_shortcut("Option+A"),
                         hotkey.parse_mac_shortcut("Alt+A"))

    def test_a_key_on_its_own(self):
        self.assertEqual(hotkey.parse_mac_shortcut("F13"), (0, 0x69))

    def test_modifiers_with_no_key(self):
        self.assertEqual(hotkey.parse_mac_shortcut("Ctrl+Alt"), (None, None))

    def test_two_keys_and_no_room_for_the_second(self):
        self.assertEqual(hotkey.parse_mac_shortcut("Ctrl+A+B"), (None, None))

    def test_a_key_nobody_mapped(self):
        self.assertEqual(hotkey.parse_mac_shortcut("Ctrl+PrintScreen"),
                         (None, None))

    def test_nothing(self):
        self.assertEqual(hotkey.parse_mac_shortcut(""), (None, None))
        self.assertEqual(hotkey.parse_mac_shortcut("+++"), (None, None))
        self.assertEqual(hotkey.parse_mac_shortcut(None), (None, None))

    def test_understands_reads_the_table_this_platform_uses(self):
        """F13 to F19 are on the macOS table and not on the evdev one, and they
        are worth having: nothing on a Mac claims them."""
        self.assertTrue(hotkey.understands("F13"))
        self.assertEqual(hotkey.parse_shortcut("F13"), (None, None))
        self.assertFalse(hotkey.understands("Ctrl+PrintScreen"))


@macos_only
class MacChooser(DikteTest):
    """What the four platform-facing functions answer on macOS.

    Nothing is written to disk here, which is the whole difference: a shortcut
    lives for as long as the process that registered it, so `install` is a
    check that it could be, `status` has nothing to report, and `remove` has
    nothing to take away.
    """

    def test_it_knows_where_it_is(self):
        self.assertEqual(hotkey.desktop_name(), "macOS")

    def test_installing_accepts_a_combination_it_can_bind(self):
        ok, message = hotkey.install_shortcut("Ctrl+Alt+Space", "dikte toggle")
        self.assertTrue(ok)
        self.assertIn("Ctrl+Alt+Space", message)

    def test_installing_refuses_one_it_cannot(self):
        ok, _ = hotkey.install_shortcut("Ctrl+PrintScreen", "dikte toggle")
        self.assertFalse(ok)

    def test_nothing_was_written_anywhere(self):
        hotkey.install_shortcut("Ctrl+Alt+Space", "dikte toggle")
        self.assertFalse((hotkey.APPLICATIONS_DIR / hotkey.DESKTOP_ID).exists())

    def test_there_is_no_registration_to_report(self):
        self.assertIsNone(hotkey.shortcut_status())

    def test_removing_one_is_not_an_error(self):
        hotkey.remove_shortcut()

    def test_macos_will_not_say_what_else_holds_a_key(self):
        """Its own shortcuts are a binary plist of numbers with no names in it;
        a clash shows up as a press that never arrives."""
        self.assertEqual(hotkey.conflicting_shortcuts("Ctrl+Alt+Space"), [])


@macos_only
class CarbonBindings(DikteTest):
    """Registering and unregistering, with Carbon itself faked out.

    The real thing is exercised by pressing the key, which no test can do; what
    is held here is that each combination reaches RegisterEventHotKey once, as
    the numbers macOS expects, and that every one of them is handed back.
    """

    def setUp(self):
        super().setUp()
        self.registered = []      # (key code, modifiers, id)
        self.unregistered = []
        self.carbon = mock.Mock()
        self.carbon.GetApplicationEventTarget.return_value = 1
        self.carbon.InstallEventHandler.return_value = 0
        self.carbon.RemoveEventHandler.return_value = 0

        def register(key, modifiers, hotkey_id, _target, _options, out_ref):
            self.registered.append((key, modifiers, hotkey_id.id))
            out_ref._obj.value = 1000 + len(self.registered)
            return 0

        self.carbon.RegisterEventHotKey.side_effect = register
        self.carbon.UnregisterEventHotKey.side_effect = \
            lambda ref: self.unregistered.append(ref) or 0
        self.patch_attr(hotkey, "_carbon", self.carbon)
        self.patch_attr(hotkey, "_load_carbon", lambda: self.carbon)

    def start(self, **bindings):
        listener = hotkey.CarbonHotkey()
        self.failures = []
        listener.failed.connect(self.failures.append)
        self.addCleanup(listener.stop)
        return listener, listener.start(bindings)

    def test_each_combination_is_registered_once(self):
        listener, ok = self.start(toggle="Ctrl+Alt+Space", cancel="Ctrl+Alt+D")
        self.assertTrue(ok)
        self.assertTrue(listener.running)
        self.assertEqual([(key, mods) for key, mods, _ in self.registered],
                         [(0x31, 0x1800), (0x02, 0x1800)])

    def test_an_empty_combination_is_a_shortcut_turned_off(self):
        _listener, ok = self.start(toggle="Ctrl+Alt+Space", meeting="")
        self.assertTrue(ok)
        self.assertEqual(len(self.registered), 1)

    def test_nothing_bound_at_all_installs_no_handler(self):
        listener, ok = self.start(toggle="", cancel="")
        self.assertFalse(ok)
        self.assertFalse(listener.running)
        self.carbon.InstallEventHandler.assert_not_called()

    def test_a_combination_that_cannot_be_parsed_is_said_and_skipped(self):
        _listener, ok = self.start(toggle="Ctrl+Alt+Space", cancel="Ctrl+Nonsense")
        self.assertTrue(ok)
        self.assertEqual(len(self.registered), 1)
        self.assertIn("Ctrl+Nonsense", self.failures[0])

    def test_one_macos_refuses_is_reported_and_the_rest_go_on(self):
        def refuse_the_second(key, modifiers, hotkey_id, target, options, out_ref):
            if hotkey_id.id == 2:
                return -9878        # eventHotKeyExistsErr
            self.registered.append((key, modifiers, hotkey_id.id))
            out_ref._obj.value = 1000
            return 0

        self.carbon.RegisterEventHotKey.side_effect = refuse_the_second
        listener, ok = self.start(toggle="Ctrl+Alt+Space", cancel="Ctrl+Alt+D",
                                  ask="Ctrl+Alt+A")
        self.assertTrue(ok)
        self.assertTrue(listener.running)
        self.assertEqual(len(self.registered), 2)
        self.assertEqual(len(self.failures), 1)

    def test_stopping_hands_every_key_back(self):
        listener, _ = self.start(toggle="Ctrl+Alt+Space", cancel="Ctrl+Alt+D")
        listener.stop()
        self.assertEqual(len(self.unregistered), 2)
        self.assertFalse(listener.running)
        self.carbon.RemoveEventHandler.assert_called_once()

    def test_starting_again_does_not_stack_a_second_set(self):
        """The settings window saves, and the shortcuts are registered afresh."""
        listener, _ = self.start(toggle="Ctrl+Alt+Space")
        listener.start({"toggle": "Ctrl+Alt+D"})
        self.assertEqual(len(self.unregistered), 1)
        self.assertEqual(len(self.registered), 2)

    def test_a_press_arrives_under_the_name_it_was_bound_to(self):
        listener, _ = self.start(toggle="Ctrl+Alt+Space", cancel="Ctrl+Alt+D")
        fired = []
        listener.triggered.connect(fired.append)

        def read_id(_event, _name, _type, _actual, _size, _actual_size, out):
            out._obj.id = 2         # the second one registered: cancel
            return 0

        self.carbon.GetEventParameter.side_effect = read_id
        listener._on_event(None, None, None)
        self.assertEqual(fired, ["cancel"])

    def test_a_press_for_a_key_that_is_no_longer_ours_is_ignored(self):
        listener, _ = self.start(toggle="Ctrl+Alt+Space")
        fired = []
        listener.triggered.connect(fired.append)
        self.carbon.GetEventParameter.side_effect = None
        self.carbon.GetEventParameter.return_value = 0
        listener._on_event(None, None, None)
        self.assertEqual(fired, [])

    def test_carbon_missing_is_said_rather_than_raised(self):
        """An exception here would come up through the tray icon's constructor."""
        def missing():
            raise OSError("Carbon framework not found")

        self.patch_attr(hotkey, "_load_carbon", missing)
        _listener, ok = self.start(toggle="Ctrl+Alt+Space")
        self.assertFalse(ok)
        self.assertIn("Carbon", self.failures[0])


if __name__ == "__main__":
    unittest.main()
