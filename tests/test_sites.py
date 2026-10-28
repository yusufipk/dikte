"""Profiles chosen by the site in a browser tab: matching, the title readers on
each system, the keyword proposed from a title, and the editor's Add site."""

import ctypes
import subprocess
import sys
from unittest import mock

from PyQt6.QtWidgets import QApplication

from dikte import config as cfg
from dikte import frontapp
from dikte import profiles
from dikte import profiles_ui
from tests.support import DikteTest
from tests.test_worker import ChainBase

_app = QApplication.instance() or QApplication([])

EM, EN = "\u2014", "\u2013"


def finished(stdout):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class Matching(DikteTest):
    def setUp(self):
        super().setUp()
        self.conf = self.config(profiles=[
            {"name": "Chat", "sites": ["WhatsApp"], "apps": ["slack"]},
            {"name": "Mail", "sites": ["Gmail", "Outlook"], "apps": ["thunderbird"]},
            {"name": "Browsing", "apps": ["firefox"]},
        ])

    def choose(self, program, title):
        return profiles.choose(self.conf, program, True, title)

    def test_a_site_wins_then_the_program_then_default(self):
        self.assertEqual(self.choose("firefox", f"(2) WhatsApp {EM} Mozilla Firefox").name,
                         "Chat")
        self.assertEqual(self.choose("firefox", f"News {EM} Mozilla Firefox").name,
                         "Browsing")
        self.assertEqual(self.choose("google-chrome", "News - Google Chrome").name, "")
        self.assertEqual(self.choose("thunderbird", "").name, "Mail")

    def test_the_matched_keyword_is_the_site_and_nothing_else(self):
        choice = self.choose("firefox", f"Inbox - Gmail {EM} Mozilla Firefox")
        self.assertEqual((choice.name, choice.site), ("Mail", "Gmail"))
        self.assertNotIn("Inbox", repr(choice))

    def test_only_the_site_part_of_the_title_counts(self):
        """A page that names another site belongs to the site it is on."""
        conf = self.config(profiles=[{"name": "Mail", "sites": ["Gmail"]},
                                     {"name": "Reading", "sites": ["Medium"]}])
        self.assertEqual(profiles.choose(conf, "firefox", True,
                                         f"Why I left Gmail - Medium {EM} Mozilla Firefox").name,
                         "Reading")
        conf = self.config(profiles=[{"name": "Mail", "sites": ["Gmail"]}])
        self.assertEqual(profiles.choose(conf, "firefox", True,
                                         f"Why I left Gmail - Medium {EM} Mozilla Firefox").name,
                         "")
        # A site-first title still finds its site.
        self.assertEqual(profiles.choose(conf, "opera", True,
                                         "Gmail - Inbox (3) - Opera").name, "Mail")

    def test_the_site_segment_wins_over_another_match(self):
        conf = self.config(profiles=[{"name": "Docs", "sites": ["Report"]},
                                     {"name": "Google", "sites": ["Google Docs"]}])
        self.assertEqual(profiles.choose(conf, "chrome", True,
                                         "Report - Google Docs - Google Chrome").name,
                         "Google")

    def test_turkish_i_folds_every_way(self):
        self.assertTrue(profiles.site_in("ılık", "Ilık - Brave"))
        self.assertTrue(profiles.site_in("Işık", "IŞIK haberleri"))

    def test_only_a_browser_title_counts(self):
        """A document called Gmail in an editor is not the site."""
        self.assertEqual(self.choose("code", "Gmail notes.md - Code").name, "")

    def test_browser_forms(self):
        for name in ("org.gnome.Epiphany", "falkon", "mullvad-browser", "tor-browser",
                     "io.github.ungoogled_software.ungoogled_chromium", "firefox", "Navigator", "org.mozilla.firefox", "zen",
                     "app.zen_browser.zen", "Google-chrome", "chromium-browser",
                     "brave-browser", "com.brave.Browser", "vivaldi-stable",
                     "msedge.exe", "microsoft-edge", "librewolf", "floorp", "waterfox"):
            with self.subTest(name=name):
                self.assertTrue(profiles.is_browser(name))
        for name in ("thunderbird", "code", "org.kde.konsole", ""):
            with self.subTest(name=name):
                self.assertFalse(profiles.is_browser(name))

    def test_keywords_fold_and_match_whole_words(self):
        self.assertTrue(profiles.site_in("gmail", "Inbox - GMAIL"))
        self.assertTrue(profiles.site_in("İzmir", "izmir haberleri - Chromium"))
        self.assertTrue(profiles.site_in("Google Docs", "Report - Google Docs"))
        self.assertFalse(profiles.site_in("Mail", "Inbox - Gmail"))
        self.assertFalse(profiles.site_in("", "anything"))

    def test_sites_turn_on_the_lookup_and_the_title(self):
        conf = self.config(profiles=[{"name": "Chat", "sites": ["WhatsApp"]}])
        self.assertTrue(profiles.wants_detection(conf))
        self.assertTrue(profiles.wants_titles(conf))
        conf["profile_by_app"] = False
        self.assertFalse(profiles.wants_titles(conf))
        self.assertEqual(profiles.choose(conf, "firefox", True, "WhatsApp").name, "")

    def test_sites_are_cleaned(self):
        conf = self.config(profiles=[{"name": "A", "sites": ["Gmail", "gmail", 3,
                                                            "  You  Tube "]}])
        self.assertEqual(profiles.named_profiles(conf)[0]["sites"], ["Gmail", "You Tube"])

    def test_a_retry_keeps_its_site(self):
        choice = profiles.again(self.conf, {"profile": "Chat", "site": "WhatsApp",
                                            "program": "firefox"})
        self.assertEqual((choice.name, choice.site), ("Chat", "WhatsApp"))


class Proposal(DikteTest):
    def test_typical_titles(self):
        for title, expected in (
            (f"Inbox (3) - you@example.com - Gmail {EM} Mozilla Firefox", "Gmail"),
            ("Never Gonna Give You Up - YouTube - Google Chrome", "YouTube"),
            (f"(2) WhatsApp {EM} Zen Browser", "WhatsApp"),
            (f"Dikte | Notion {EM} Mozilla Firefox", "Notion"),
            (f"Pull requests {EN} GitHub - Brave", "GitHub"),
            ("web.whatsapp.com - Chromium", "whatsapp"),
            ("Knowledge base - Confluence - Work - Microsoft\u200b Edge", "Confluence"),
            ("Video - YouTube - Personal - Microsoft Edge", "YouTube"),
            ("Gmail - Inbox (3) - Opera", "Gmail"),
            ("Notion - Home - Brave", "Notion"),
            ("Report (2) - Google Docs - Google Chrome", "Google Docs"),
            ("news.bbc.co.uk - Brave", "bbc"),
            ("www.e-devlet.gov.tr - Mozilla Firefox", "e-devlet"),
            ("Mozilla Firefox", ""),
            ("New Tab - Google Chrome", ""),
            (f"Zen Browser", ""),
            ("about:blank", ""),
            ("Brave", ""),
            ("WhatsApp", "WhatsApp"),
            ("", ""),
        ):
            with self.subTest(title=title):
                self.assertEqual(profiles.propose_site(title), expected)


class Readers(DikteTest):
    def test_x11_asks_for_the_title_only_once_it_knows_a_browser(self):
        replies = [finished("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2\n"),
                   finished('WM_CLASS(STRING) = "Navigator", "firefox"\n'),
                   finished('_NET_WM_NAME(UTF8_STRING) = "Inbox \\"new\\" - Gmail '
                            f'{EM} Mozilla Firefox"\n'
                            'WM_NAME(STRING) = "fallback"\n')]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies) as run, \
                mock.patch.object(frontapp, "backend", return_value=frontapp._x11):
            program, title = frontapp.front(None, profiles.is_browser)
        self.assertEqual((program, title),
                         ("firefox", f'Inbox "new" - Gmail {EM} Mozilla Firefox'))
        self.assertEqual(run.call_count, 3)
        self.assertNotIn("_NET_WM_NAME", run.call_args_list[1].args[0])
        self.assertIn("_NET_WM_NAME", run.call_args_list[2].args[0])

    def test_xprop_escapes_and_latin1(self):
        self.assertEqual(frontapp._xprop_string('"a\\nb\\tc\\\\d\\"e"'), 'a\nb\tc\\d"e')
        self.assertEqual(frontapp._xprop_string('"caf\\351"', "latin-1"), "café")
        self.assertEqual(frontapp._xprop_string('"\\303\\274"'), "ü")

    def test_x11_falls_back_to_wm_name(self):
        replies = [finished("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2\n"),
                   finished('WM_CLASS(STRING) = "x", "chromium"\n'),
                   finished('WM_NAME(STRING) = "YouTube - Chromium"\n')]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies):
            self.assertEqual(frontapp._x11(title_for=profiles.is_browser),
                             ("chromium", "YouTube - Chromium"))

    def test_x11_asks_no_title_of_a_program_that_is_not_a_browser(self):
        replies = [finished("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2\n"),
                   finished('WM_CLASS(STRING) = "x", "code"\n')]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies) as run:
            self.assertEqual(frontapp._x11(title_for=profiles.is_browser), ("code", ""))
        self.assertEqual(run.call_count, 2)

    def test_kde_reads_the_title_with_one_more_call_and_only_for_a_browser(self):
        replies = [finished("{w}\n"), finished("org.mozilla.firefox\n"),
                   finished(f"WhatsApp {EM} Mozilla Firefox\n")]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/kdotool"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies) as run:
            self.assertEqual(frontapp._kde_wayland(title_for=profiles.is_browser),
                             ("org.mozilla.firefox", f"WhatsApp {EM} Mozilla Firefox"))
        self.assertEqual(run.call_args.args[0][:2], ["kdotool", "getwindowname"])
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/kdotool"), \
                mock.patch.object(frontapp.subprocess, "run",
                                  side_effect=[finished("{w}\n"),
                                               finished("org.kde.konsole\n")]) as run:
            self.assertEqual(frontapp._kde_wayland(title_for=profiles.is_browser),
                             ("org.kde.konsole", ""))
        self.assertEqual(run.call_count, 2)

    def test_windows_reads_the_foreground_windows_text(self):
        user32, kernel32 = mock.Mock(), mock.Mock()
        user32.GetForegroundWindow.return_value = 0x10

        def thread_process(_window, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 7
            return 1

        def image(_handle, _flags, buffer, _size):
            buffer.value = "C:\\Program Files\\Google\\Chrome\\chrome.exe"
            return 1

        def text(_window, buffer, _size):
            buffer.value = "Gmail - Google Chrome"
            return len(buffer.value)

        user32.GetWindowThreadProcessId.side_effect = thread_process
        user32.GetWindowTextLengthW.return_value = 21
        user32.GetWindowTextW.side_effect = text
        kernel32.OpenProcess.return_value = 0x99
        kernel32.QueryFullProcessImageNameW.side_effect = image
        with mock.patch.object(frontapp, "_win_api", return_value=(user32, kernel32)), \
                mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(frontapp.front(None, profiles.is_browser),
                             ("chrome", "Gmail - Google Chrome"))

    def test_macos_reads_no_title(self):
        with mock.patch.object(frontapp, "_macos_name", return_value="Firefox"):
            self.assertEqual(frontapp._macos(42, title_for=profiles.is_browser),
                             ("Firefox", ""))

    def test_the_lookup_reads_titles_only_when_a_profile_lists_a_site(self):
        from dikte import app
        apps_only = self.config(profiles=[{"name": "A", "apps": ["firefox"]}])
        with mock.patch.object(app.frontapp, "front") as front, \
                mock.patch.object(app.frontapp, "program_name", return_value="firefox"):
            app._ProfileLookup(apps_only, None).result()
        front.assert_not_called()
        with_sites = self.config(profiles=[{"name": "Chat", "sites": ["WhatsApp"]}])
        with mock.patch.object(app.frontapp, "front",
                               return_value=("firefox", "(1) WhatsApp")):
            choice = app._ProfileLookup(with_sites, None).result()
        self.assertEqual((choice.name, choice.site), ("Chat", "WhatsApp"))


class TitleNeverStored(ChainBase):
    def test_the_history_keeps_the_site_and_not_the_title(self):
        self.conf["profiles"] = [{"name": "Chat", "sites": ["WhatsApp"]}]
        title = f"(4) WhatsApp: Private chat with Ayşe {EM} Mozilla Firefox"
        choice = profiles.choose(self.conf, "firefox", True, title)
        self.run_chain(profile=choice)
        stored = cfg.HISTORY_FILE.read_text(encoding="utf-8")
        row = cfg.read_history()[0]
        self.assertEqual((row["profile"], row["site"], row["program"]),
                         ("Chat", "WhatsApp", "firefox"))
        self.assertNotIn("Ayşe", stored)
        self.assertNotIn("Private chat", stored)


class Editor(DikteTest):
    def editor(self, conf):
        editor = profiles_ui.ProfilesEditor()
        self.addCleanup(editor.deleteLater)
        editor.load(conf)
        editor.select(1)
        return editor

    def test_a_keyword_can_be_typed(self):
        conf = self.config(profiles=[{"name": "Chat"}])
        editor = self.editor(conf)
        editor.site_edit.setText("WhatsApp")
        editor._add_typed_site()
        self.assertFalse(editor.add_site("whatsapp"))
        editor.store(conf)
        self.assertEqual(conf["profiles"][0]["sites"], ["WhatsApp"])

    def test_add_site_counts_down_reads_and_proposes(self):
        conf = self.config(profiles=[{"name": "Chat"}])
        editor = self.editor(conf)
        with mock.patch.object(profiles_ui.threading, "Thread") as thread, \
                mock.patch.object(frontapp, "front",
                                  return_value=("firefox", f"(2) WhatsApp {EM} Mozilla Firefox")):
            editor.start_site_countdown()
            self.assertIn("Switch to the tab… 3", editor.site_status.text())
            self.assertFalse(editor.read_site_button.isEnabled())
            editor._tick()
            self.assertIn("2", editor.site_status.text())
            editor._tick()
            editor._tick()
            # Read off the main thread.
            thread.return_value.start.assert_called_once_with()
            thread.call_args.kwargs["target"]()
        QApplication.processEvents()
        self.assertEqual(editor.current()["sites"], ["WhatsApp"])
        # One line on screen; what was read, in its tooltip.
        self.assertEqual(editor.site_status.text(), "Added “WhatsApp”.")
        self.assertIn("firefox", editor.site_status.toolTip())
        self.assertIn("Mozilla Firefox", editor.site_status.toolTip())
        self.assertTrue(editor.read_site_button.isEnabled())
        # A page's title is its own: markup in it is shown, never drawn.
        editor._status("x", "<b>Sign in</b> & go")
        self.assertEqual(editor.site_status.toolTip(),
                         "<qt>&lt;b&gt;Sign in&lt;/b&gt; &amp; go</qt>")
        editor.store(conf)
        self.assertNotIn("Mozilla", repr(conf["profiles"]))

    def test_a_program_that_is_not_a_browser_is_said_so(self):
        editor = self.editor(self.config(profiles=[{"name": "Chat"}]))
        editor._site_question = 5
        editor._site_target = editor.current()
        editor._titled(5, "code", "")
        self.assertIn("not a browser", editor.site_status.text())
        self.assertEqual(editor.current()["sites"], [])

    def test_the_default_profile_has_no_sites(self):
        editor = self.editor(self.config(profiles=[{"name": "Chat"}]))
        editor.select(0)
        self.assertFalse(editor.read_site_button.isEnabled())
        self.assertFalse(editor.add_site("WhatsApp"))

    def test_remove_site(self):
        conf = self.config(profiles=[{"name": "Chat", "sites": ["WhatsApp", "Gmail"]}])
        editor = self.editor(conf)
        editor.sites.item(0).setSelected(True)
        editor.remove_selected_sites()
        self.assertEqual(editor.current()["sites"], ["Gmail"])

    def test_the_tab_says_what_is_read_and_where_it_is_not(self):
        with mock.patch.object(frontapp, "backend", return_value=frontapp._macos):
            self.assertEqual(profiles_ui.ProfilesEditor.sites_text(),
                             "Site matching is not available on macOS.")
        with mock.patch.object(frontapp, "backend", return_value=frontapp._x11):
            self.assertIn("never saved", profiles_ui.ProfilesEditor.sites_tooltip())


class CountdownEdges(DikteTest):
    def editor(self, conf):
        editor = profiles_ui.ProfilesEditor()
        self.addCleanup(editor.deleteLater)
        editor.load(conf)
        editor.select(1)
        return editor

    def test_a_profile_removed_during_the_countdown_is_said_so(self):
        editor = self.editor(self.config(profiles=[{"name": "Chat"}]))
        with mock.patch.object(profiles_ui.threading, "Thread"):
            editor.start_site_countdown()
            for _ in range(3):
                editor._tick()
        asked = editor._site_question
        editor.remove_current()
        editor._titled(asked, "firefox", "(2) WhatsApp")
        self.assertIn("profile changed", editor.site_status.text())

    def test_a_reload_during_the_countdown_is_said_so(self):
        conf = self.config(profiles=[{"name": "Chat"}])
        editor = self.editor(conf)
        with mock.patch.object(profiles_ui.threading, "Thread"):
            editor.start_site_countdown()
            for _ in range(3):
                editor._tick()
        asked = editor._site_question
        editor.load(conf)
        editor._titled(asked, "firefox", "(2) WhatsApp")
        self.assertIn("profile changed", editor.site_status.text())

    def test_selecting_another_profile_does_not_re_enable_the_button(self):
        editor = self.editor(self.config(profiles=[{"name": "Chat"}, {"name": "Mail"}]))
        editor.start_site_countdown()
        editor.select(2)
        self.assertFalse(editor.read_site_button.isEnabled())
        editor._countdown_timer.stop()

    def test_the_answer_does_not_enable_the_button_on_default(self):
        editor = self.editor(self.config(profiles=[{"name": "Chat"}]))
        with mock.patch.object(profiles_ui.threading, "Thread"):
            editor.start_site_countdown()
            for _ in range(3):
                editor._tick()
        asked = editor._site_question
        editor.select(0)
        editor._titled(asked, "firefox", "(2) WhatsApp")
        self.assertFalse(editor.read_site_button.isEnabled())

    def test_a_blank_tab_asks_for_the_site_tab_and_adds_nothing(self):
        editor = self.editor(self.config(profiles=[{"name": "Chat"}]))
        editor._site_question, editor._site_target = 9, editor.current()
        editor._titled(9, "firefox", "Mozilla Firefox")
        self.assertIn("open the site's tab first", editor.site_status.text())
        self.assertEqual(editor.current()["sites"], [])


class EdgeAndFirstParts(DikteTest):
    def test_edge_without_a_profile_keeps_the_site(self):
        self.assertEqual(profiles.propose_site(
            "Knowledge base - Confluence - Microsoft\u200b Edge"), "Confluence")

    def test_edge_with_a_profile_drops_it(self):
        for profile in ("Personal", "Work", "Profile 2", "İş", "Kişisel", "you@example.com"):
            with self.subTest(profile=profile):
                self.assertEqual(profiles.propose_site(
                    f"Video - YouTube - {profile} - Microsoft Edge"), "YouTube")

    def test_firefox_and_zen_profiles_are_dropped_too(self):
        # Firefox and Zen with more than one profile put its name before
        # their own, the way Edge does.
        for title in ("(1) WhatsApp \u2014 Original profile \u2014 Zen Browser",
                      "WhatsApp \u2014 Default Profile \u2014 Mozilla Firefox",
                      "WhatsApp \u2014 Work \u2014 Mozilla Firefox"):
            with self.subTest(title=title):
                self.assertEqual(profiles.propose_site(title), "WhatsApp")
        conf = self.config(profiles=[{"name": "Chat", "sites": ["WhatsApp"]}])
        self.assertEqual(profiles.choose(
            conf, "zen", True,
            "(1) WhatsApp \u2014 Original profile \u2014 Zen Browser").name, "Chat")

    def test_edge_beta_and_more_pages(self):
        self.assertEqual(profiles.propose_site(
            "Inbox - Outlook and 3 more pages - Work - Microsoft Edge Beta"), "Outlook")
        conf = self.config(profiles=[{"name": "Wiki", "sites": ["Confluence"]}])
        self.assertEqual(profiles.choose(
            conf, "msedge", True, "Knowledge base - Confluence - Team - Microsoft Edge").name,
            "Wiki")

    def test_a_title_of_browser_like_words_keeps_its_last_part(self):
        self.assertEqual(profiles.title_parts("Google - Google Chrome"), ["Google"])
        self.assertEqual(profiles.propose_site("Google Chrome"), "")

    def test_the_first_part_counts_only_after_a_generic_last_part(self):
        conf = self.config(profiles=[{"name": "Chat", "sites": ["Slack"]},
                                     {"name": "Mail", "sites": ["Gmail"]}])
        for title in ("slack - Google Search - Google Chrome",
                      f"Leaving Gmail - Medium {EM} Mozilla Firefox"):
            with self.subTest(title=title):
                self.assertEqual(profiles.choose(conf, "firefox", True, title).name, "")
        self.assertEqual(profiles.choose(conf, "opera", True,
                                         "Gmail - Inbox (3) - Opera").name, "Mail")

    def test_the_help_text_says_page_text_does_not_match(self):
        with mock.patch.object(frontapp, "backend", return_value=frontapp._x11):
            self.assertIn("not the text of the page",
                          profiles_ui.ProfilesEditor.sites_tooltip())


class Selection(DikteTest):
    def backend(self, **env):
        import os
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, env, clear=True):
            return frontapp.backend()

    def test_a_leftover_socket_does_not_misroute_another_desktop(self):
        self.assertIs(self.backend(WAYLAND_DISPLAY="w", XDG_CURRENT_DESKTOP="KDE",
                                   SWAYSOCK="/tmp/sway.sock"), frontapp._kde_wayland)
        self.assertIs(self.backend(WAYLAND_DISPLAY="w", XDG_CURRENT_DESKTOP="GNOME",
                                   HYPRLAND_INSTANCE_SIGNATURE="x"), frontapp._gnome)
        self.assertIs(self.backend(WAYLAND_DISPLAY="w", SWAYSOCK="/tmp/sway.sock"),
                      frontapp._sway)

    def test_compositor_variables_come_before_the_display_fallback(self):
        self.assertIs(self.backend(DISPLAY=":0", NIRI_SOCKET="/tmp/niri"), frontapp._niri)

    def test_only_gnome_itself_is_gnome(self):
        self.assertIs(self.backend(WAYLAND_DISPLAY="w", XDG_CURRENT_DESKTOP="Budgie:GNOME"),
                      frontapp._unknown)
        for desktop in ("Budgie:GNOME", "X-Cinnamon", "MATE", "Unity"):
            with self.subTest(desktop=desktop):
                self.assertIs(self.backend(XDG_SESSION_TYPE="x11",
                                           XDG_CURRENT_DESKTOP=desktop), frontapp._x11)


class ProbeOnce(DikteTest):
    def test_the_gnome_probe_runs_once_a_session(self):
        with mock.patch.object(frontapp, "backend", return_value=frontapp._gnome), \
                mock.patch.dict(frontapp.gnome_extension, {"present": None}), \
                mock.patch.dict(profiles_ui._PROBE, {"started": False}), \
                mock.patch.object(profiles_ui.threading, "Thread") as thread:
            for _ in range(2):
                editor = profiles_ui.ProfilesEditor()
                self.addCleanup(editor.deleteLater)
        self.assertEqual(thread.call_count, 1)

    def test_a_deleted_editor_ignores_a_late_probe(self):
        editor = profiles_ui.ProfilesEditor()
        editor.deleteLater()
        QApplication.processEvents()
        profiles_ui._relay().probed.emit(False)
        QApplication.processEvents()


class EdgeFallback(DikteTest):
    def choose(self, conf, title):
        return profiles.choose(conf, "msedge", True, title).name

    def test_the_site_part_wins_and_the_part_before_is_only_a_fallback(self):
        both = self.config(profiles=[{"name": "Mail", "sites": ["Gmail"]},
                                     {"name": "Reading", "sites": ["Medium"]}])
        self.assertEqual(self.choose(both, "Leaving Gmail - Medium - Microsoft Edge"), "Reading")
        mail = self.config(profiles=[{"name": "Mail", "sites": ["Gmail"]}])
        self.assertEqual(self.choose(mail, "Inbox - Gmail - Team - Microsoft Edge"), "Mail")

    def test_never_the_page_text_once_a_profile_was_taken_off(self):
        mail = self.config(profiles=[{"name": "Mail", "sites": ["Gmail"]}])
        self.assertEqual(self.choose(mail, "Leaving Gmail - Medium - Work - Microsoft Edge"), "")

    def test_a_turkish_profile_name_is_recognised_folded(self):
        self.assertEqual(profiles.propose_site(
            "Video - YouTube - Varsayılan - Microsoft Edge"), "YouTube")


class GnomeSessions(DikteTest):
    def backend(self, desktop):
        import os
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "w",
                                             "XDG_CURRENT_DESKTOP": desktop}, clear=True):
            return frontapp.backend()

    def test_distribution_sessions_run_gnome_shell(self):
        for desktop in ("GNOME", "ubuntu:GNOME", "pop:GNOME", "zorin:GNOME"):
            with self.subTest(desktop=desktop):
                self.assertIs(self.backend(desktop), frontapp._gnome)

    def test_desktops_built_on_gnome_are_not_gnome_shell(self):
        for desktop in ("Budgie:GNOME", "X-Cinnamon:GNOME", "Unity:GNOME", "Pantheon:GNOME"):
            with self.subTest(desktop=desktop):
                self.assertIs(self.backend(desktop), frontapp._unknown)
