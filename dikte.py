#!/usr/bin/env python3
"""Dikte: press Ctrl+Space, talk, press again to transcribe, clean up and paste.

This is the application: the tray icon, the state machine, and the socket the
terminal talks to. Every verb it answers is in cli.py, which is also what runs
`dikte.py --help`; the only argument handled here is --gui, which is how the
command line says "there is no instance to talk to, so be one".
"""

import contextlib
import ctypes
import ctypes.util
import json
import os
import signal
import socket
import sys
import threading

# A Wayland client cannot place a window in a screen corner, so the indicator
# is drawn through XWayland.
if os.environ.get("XDG_SESSION_TYPE") == "wayland" and os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

# An application started from the Finder is given none of the shell's PATH, so
# Homebrew's ffmpeg is invisible to it. Put the two places brew installs to in
# front, before anything goes looking for a program.
if sys.platform == "darwin":
    os.environ["PATH"] = os.pathsep.join(
        part for part in ("/opt/homebrew/bin", "/usr/local/bin",
                          os.environ.get("PATH", "")) if part
    )

from PyQt6.QtCore import QTimer, QElapsedTimer, QSocketNotifier  # noqa: E402
from PyQt6.QtGui import QAction, QIcon  # noqa: E402
from PyQt6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

import assistant  # noqa: E402
import audio  # noqa: E402
import cli  # noqa: E402
import config as cfg  # noqa: E402
import ggml  # noqa: E402
import hotkey  # noqa: E402
import i18n  # noqa: E402
import ipc  # noqa: E402
import meeting  # noqa: E402
import trayicon  # noqa: E402
from i18n import t  # noqa: E402
from meeting import MeetingPipeline  # noqa: E402
from overlay import Overlay  # noqa: E402
from settings_ui import SettingsWindow  # noqa: E402
from worker import Pipeline  # noqa: E402

SERVER_NAME = ipc.SERVER_NAME
IDLE, RECORDING, BUSY = "idle", "recording", "busy"
# Dictation and a command for the agent are two runs of the same machinery, kept
# apart so that neither waits on the other: an agent can spend a minute thinking,
# and having dictation blocked for that minute is the whole problem. They share
# only the microphone, which is one device and so can serve one of them at a time.
DICTATION, ASK = "dictation", "ask"
# A meeting runs alongside dictation rather than through it: writing up an hour
# of audio takes minutes, and dictation should not be held hostage to it.
MEETING = "meeting"
M_IDLE, M_RECORDING, M_WORKING = "idle", "recording", "working"

# The KDE shortcut answers a key press by launching a whole Python process, so
# its toggle lands well after the built-in listener has handled the same press.
# Anything arriving inside this window is that echo, not a second press.
ECHO_MS = 2000

# How long the indicator stays up at the start of a meeting. Long enough to see
# both halves of the waveform move, which is the one check that matters, and
# short enough not to sit in the corner for the rest of the hour.
PEEK_MS = 12000


class Dikte:
    def __init__(self, app):
        self.app = app
        self.conf = cfg.Config()
        self.state = IDLE
        self.ask_state = IDLE
        # Which of the two the microphone is currently serving, or None.
        self.recorder_owner = None
        self.meeting_state = M_IDLE
        self.meeting_base = ""
        self.meeting_message = ""
        self.settings_window = None
        self._quitting = False
        # A request that asked to be told how its run ended waits in here until
        # the run gets there, keyed by which of the three it was waiting on.
        self._waiters = {}
        # Whether the next run pastes, when the request said so instead of
        # leaving it to the setting.
        self.paste_override = {}
        # Which recording is the current one, so a timer set for the run that
        # started it cannot stop the one that came after.
        self._run_id = 0

        self.overlay = Overlay(self.conf["overlay_corner"])
        # The agent's indicator sits on top of the dictation one when both are
        # up, and drops into the corner when it is alone there.
        self.ask_overlay = Overlay(self.conf["overlay_corner"], below=self.overlay,
                                   dismissable=True)
        self.recorder = audio.Recorder()
        self.pipeline = Pipeline(self.conf)
        self.ask_pipeline = Pipeline(self.conf)
        self.meeting_recorder = audio.MeetingRecorder()
        self.meetings = MeetingPipeline(self.conf)
        self.evdev = hotkey.listener()
        # Before anything of ours is started: a server from a Dikte that was
        # killed outright is still holding a model in memory.
        ggml.sweep()

        self.recorder.level.connect(self._on_level)
        self.recorder.stopped.connect(self._on_recorded)
        self.recorder.failed.connect(self._on_recorder_error)
        self.pipeline.stage.connect(self.overlay.show_busy)
        self.pipeline.finished.connect(self._on_finished)
        self.pipeline.failed.connect(self._on_error)
        self.ask_pipeline.stage.connect(self.ask_overlay.show_busy)
        self.ask_pipeline.finished.connect(self._on_ask_finished)
        self.ask_pipeline.failed.connect(self._on_ask_error)
        self.ask_pipeline.cancelled.connect(self._on_ask_cancelled)
        self.meeting_recorder.levels.connect(self._on_meeting_levels)
        self.meeting_recorder.stopped.connect(self._on_meeting_recorded)
        self.meeting_recorder.died.connect(self._on_meeting_died)
        self.meeting_recorder.failed.connect(self._on_meeting_error)
        self.meetings.progress.connect(self._on_meeting_progress)
        self.meetings.finished.connect(self._on_meeting_finished)
        self.meetings.failed.connect(self._on_meeting_failed)
        self.evdev.triggered.connect(self._on_evdev)
        self.evdev.failed.connect(self._on_error)

        self.elapsed = QElapsedTimer()
        self.meeting_elapsed = QElapsedTimer()
        self.last_toggle = QElapsedTimer()
        self.last_evdev = {}
        self.ticker = QTimer()
        self.ticker.setInterval(100)
        self.ticker.timeout.connect(self._tick)
        self.meeting_ticker = QTimer()
        self.meeting_ticker.setInterval(500)
        self.meeting_ticker.timeout.connect(self._meeting_tick)

        self.tray = QSystemTrayIcon()
        self._apply_settings()
        self.tray.show()

    # ---- tray ----------------------------------------------------------

    def _build_tray(self):
        # Keep menu and actions on self: PyQt does not take ownership when they
        # are only passed to addAction(), and garbage collection eats them.
        self.menu = QMenu()
        self.toggle_action = QAction(t("Start recording"), self.menu)
        self.toggle_action.triggered.connect(self._toggle)
        self.menu.addAction(self.toggle_action)

        # Named in _refresh_tray, which is where the chosen provider is known.
        self.ask_action = QAction("", self.menu)
        self.ask_action.triggered.connect(self._toggle_ask)
        self.menu.addAction(self.ask_action)

        self.reset_action = QAction(t("Start a new conversation"), self.menu)
        self.reset_action.triggered.connect(self.reset_conversation)
        self.menu.addAction(self.reset_action)

        self.ask_cancel_action = QAction("", self.menu)
        self.ask_cancel_action.triggered.connect(self.cancel_ask)
        self.ask_cancel_action.setEnabled(False)
        self.menu.addAction(self.ask_cancel_action)

        self.cancel_action = QAction(t("Discard the recording"), self.menu)
        # The inner method, so that a menu click is never mistaken for the KDE
        # shortcut echoing the built-in listener's press.
        self.cancel_action.triggered.connect(self._cancel)
        self.cancel_action.setEnabled(False)
        self.menu.addAction(self.cancel_action)
        self.menu.addSeparator()

        self.meeting_action = QAction(t("Record a meeting"), self.menu)
        self.meeting_action.triggered.connect(self._toggle_meeting)
        self.menu.addAction(self.meeting_action)

        self.meeting_cancel_action = QAction(t("Discard the meeting"), self.menu)
        self.meeting_cancel_action.triggered.connect(self.cancel_meeting)
        self.meeting_cancel_action.setEnabled(False)
        self.menu.addAction(self.meeting_cancel_action)
        self.menu.addSeparator()

        self.settings_action = QAction(t("Settings…"), self.menu)
        self.settings_action.triggered.connect(self.open_settings)
        self.menu.addAction(self.settings_action)

        self.restart_action = QAction(t("Restart"), self.menu)
        self.restart_action.triggered.connect(self.restart)
        self.menu.addAction(self.restart_action)
        self.menu.addSeparator()

        self.quit_action = QAction(t("Quit"), self.menu)
        self.quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(self.quit_action)

        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip(t("Dikte: ready"))
        self.tray.activated.connect(self._tray_clicked)
        self._set_icon("audio-input-microphone")

    def _tray_clicked(self, reason):
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        # The icon ends whatever is being recorded rather than only a dictation.
        # The two shortcuts are each tied to their own mode, on purpose, but the
        # icon is one button: having it refuse to stop a recording it can see is
        # just a button that does nothing.
        if self.ask_state == RECORDING:
            self._toggle_ask()
        else:
            self._toggle()

    def _set_icon(self, name):
        # The theme first, so a Linux desktop keeps its own icons, then the ones
        # drawn in trayicon.py. macOS has no theme at all and would otherwise be
        # handed a null icon, which in a menu bar is an item you cannot see.
        icon = QIcon.fromTheme(name)
        if icon.isNull():
            icon = trayicon.icon(name)
        if icon.isNull():
            icon = QIcon.fromTheme("audio-input-microphone")
        self.tray.setIcon(icon)

    # ---- state ----------------------------------------------------------

    @property
    def recording(self):
        """True while the microphone is serving either of the two.

        Read off the states rather than off recorder_owner, which outlives the
        recording: it is still set between stop() and the audio arriving, and
        the microphone is free in that gap.
        """
        return RECORDING in (self.state, self.ask_state)

    def _set_state(self, state):
        self.state = state
        self._refresh_tray()

    def _set_ask_state(self, state):
        self.ask_state = state
        self._refresh_tray()

    def _set_meeting_state(self, state):
        self.meeting_state = state
        if state != M_WORKING:
            self.meeting_message = ""
        self._refresh_tray()

    def _refresh_tray(self):
        labels = {
            IDLE: ("Start recording", "audio-input-microphone", "Dikte: ready"),
            RECORDING: ("Stop and transcribe", "media-record", "Dikte: recording"),
            BUSY: ("Working…", "view-refresh", "Dikte: working"),
        }
        label, icon, tip = labels[self.state]
        agent = assistant.display_name(self.conf)

        self.toggle_action.setText(t(label))
        # Free while the other one is thinking, blocked only while it is holding
        # the microphone.
        self.toggle_action.setEnabled(
            self.state == RECORDING or (self.state == IDLE and not self.recording)
        )
        asked = i18n.name(agent, "dative")
        self.ask_action.setText(
            t("Stop and ask {name}", name=asked) if self.ask_state == RECORDING
            else t("Ask {name}", name=asked)
        )
        self.ask_action.setEnabled(
            self.ask_state == RECORDING
            or (self.ask_state == IDLE and not self.recording)
        )
        self.reset_action.setEnabled(self.ask_state != BUSY)
        self.cancel_action.setEnabled(self.recording)
        # A command to the agent is the one job long enough to be worth calling
        # off once it is already running.
        self.ask_cancel_action.setText(
            t("Stop {name}", name=i18n.name(agent, "accusative"))
        )
        self.ask_cancel_action.setEnabled(self.ask_state == BUSY)

        # The agent speaks through the icon only when dictation has nothing to
        # say, since dictation is the one being waited on in front of a screen.
        if self.state == IDLE and self.ask_state != IDLE:
            if self.ask_state == RECORDING:
                icon, tip = "media-record", "Dikte: recording for Claude"
            else:
                icon, tip = "view-refresh", "Dikte: talking to Claude"

        meeting_labels = {
            M_IDLE: "Record a meeting",
            M_RECORDING: "End the meeting and write it up",
            M_WORKING: "Writing the meeting up…",
        }
        self.meeting_action.setText(t(meeting_labels[self.meeting_state]))
        self.meeting_action.setEnabled(self.meeting_state != M_WORKING)
        self.meeting_cancel_action.setEnabled(self.meeting_state == M_RECORDING)

        # A meeting speaks last: it runs for an hour and then works for minutes,
        # so it would otherwise own the icon for most of the day.
        if self.state == IDLE and self.ask_state == IDLE and self.meeting_state != M_IDLE:
            if self.meeting_state == M_RECORDING:
                icon, tip = "media-record", t("Dikte: in a meeting")
            else:
                icon = "view-refresh"
                tip = self.meeting_message or t("Dikte: writing the meeting up")
            self._set_icon(icon)
            self.tray.setToolTip(tip)
            return
        self._set_icon(icon)
        self.tray.setToolTip(t(tip))

    # ---- actions ---------------------------------------------------------

    def toggle(self):
        """A toggle from outside this process: the KDE shortcut, or the CLI."""
        self._external("toggle", self._toggle)

    def toggle_ask(self):
        self._external("ask", self._toggle_ask)

    def toggle_meeting(self):
        self._external("meeting", self._toggle_meeting)

    def cancel(self):
        self._external("cancel", self._cancel)

    def _external(self, name, handler):
        # The built-in listener sees the key press the instant it happens, so a
        # toggle arriving right behind one is the KDE shortcut catching up on
        # that same press. Its lateness is also the proof we were waiting for
        # that the shortcut is live, which leaves the listener with nothing to
        # do but double every press.
        # Where nothing was installed there is no shortcut to catch up, and
        # retiring the listener would leave the keys with nowhere to arrive.
        timer = self.last_evdev.get(name)
        if (hotkey.installs_shortcuts() and self.evdev.running
                and timer is not None and timer.elapsed() < ECHO_MS):
            self._retire_listener()
            return
        handler()

    def _on_evdev(self, name):
        timer = self.last_evdev.get(name)
        if timer is None:
            timer = self.last_evdev[name] = QElapsedTimer()
        timer.restart()
        handlers = {"meeting": self._toggle_meeting, "ask": self._toggle_ask,
                    "cancel": self._cancel}
        handlers.get(name, self._toggle)()

    def _retire_listener(self):
        self.evdev.stop()
        self.conf["evdev_hotkey"] = False
        self.conf.save()
        self.tray.showMessage(
            "Dikte",
            t("The {desktop} shortcut is live now, so the built-in listener has "
              "been turned off. It was doubling every key press.",
              desktop=hotkey.desktop_name()),
            QSystemTrayIcon.MessageIcon.Information, 8000,
        )

    # ---- requests off the socket ------------------------------------------
    #
    # Every request is answered, and a request can ask to be answered late: not
    # when the recording starts but when the transcript is there. That is what
    # makes a terminal, or something driving one, able to use this at all rather
    # than only able to press its buttons.

    def handle(self, request, reply):
        cmd = str(request.get("cmd") or "settings").strip()
        if cmd in ("toggle", "start", "stop", "record"):
            self._dictation_request(cmd, request, reply)
        elif cmd == "ask":
            self._ask_request(request, reply)
        elif cmd in ("meeting", "meeting-start", "meeting-stop"):
            self._meeting_request(cmd, request, reply)
        elif cmd == "status":
            reply(self.status())
        else:
            handler = {
                "cancel": self.cancel,
                "ask-cancel": self.cancel_ask,
                "ask-reset": self.reset_conversation,
                "meeting-cancel": self.cancel_meeting,
                "settings": self.open_settings,
                "reload": self.reload_settings,
                "restart": self.restart,
                "quit": self.app.quit,
            }.get(cmd)
            if handler is None:
                reply({"ok": False, "error": f"unknown command: {cmd}"})
                return
            if cmd in ("restart", "quit"):
                # Answer while there is still something to answer with.
                reply({"ok": True})
                QTimer.singleShot(120, handler)
                return
            handler()
            reply({"ok": True})

    def _dictation_request(self, cmd, request, reply):
        before = self.state
        # Only a request that said something about pasting changes it, so that
        # the stop half of a `start --paste` does not undo the start half.
        if "paste" in request:
            self.paste_override[DICTATION] = request["paste"]
        if cmd == "toggle":
            self.toggle()
        elif cmd == "stop":
            self.stop()
        else:
            self.start()
            seconds = float(request.get("seconds") or 0)
            if seconds > 0 and self.state == RECORDING:
                run = self._run_id
                QTimer.singleShot(int(seconds * 1000), lambda: self._auto_stop(run))
        self._answer(DICTATION, before, self.state, request, reply)

    def _ask_request(self, request, reply):
        before = self.ask_state
        if "paste" in request:
            self.paste_override[ASK] = request["paste"]
        self.toggle_ask()
        self._answer(ASK, before, self.ask_state, request, reply)

    def _meeting_request(self, cmd, request, reply):
        before = self.meeting_state
        if cmd == "meeting":
            self.toggle_meeting()
        elif cmd == "meeting-start":
            self.start_meeting()
        else:
            self.stop_meeting()
        self._answer(MEETING, before, self.meeting_state, request, reply)

    def _answer(self, kind, before, after, request, reply):
        """Reply now, or once the run this request set going is over."""
        if not request.get("wait"):
            reply({"ok": True, "state": after})
        elif after == before:
            # Nothing moved: the microphone is held by the other one, or this
            # one is still working, or there was nothing to stop. Say so rather
            # than wait for a run that was never started.
            reply({"ok": False, "state": after,
                   "error": f"nothing was started; {kind} is {after}"})
        else:
            self._waiters.setdefault(kind, []).append(reply)

    def _settle(self, kind, payload):
        """Tell whoever was waiting on this run how it ended."""
        for reply in self._waiters.pop(kind, []):
            reply(payload)

    def _auto_stop(self, run):
        """The end of a `record --seconds`, if that recording is still the one."""
        if self._run_id == run and self.state == RECORDING:
            self.stop()

    def status(self):
        return {
            "ok": True,
            "running": True,
            "dictation": self.state,
            "ask": self.ask_state,
            "meeting": self.meeting_state,
            "meeting_base": self.meetings.running_base,
            "meeting_message": self.meeting_message,
            "agent": assistant.display_name(self.conf),
            "provider": assistant.provider(self.conf),
            "listener": self.evdev.running,
            # Asked here rather than by the command line, because on macOS
            # there is no registry to read: a combination is held by this
            # process and by nothing else, so this is the only process that
            # can say whether it is.
            "shortcuts": {name: hotkey.shortcut_status(spec.desktop_id)
                          for name, spec in hotkey.SHORTCUTS.items()},
        }

    def reload_settings(self):
        """Read the config file back after something outside changed it."""
        self.conf.load()
        self._apply_settings()

    def _toggle(self):
        # Two /dev/input nodes can carry the same keyboard, and a menu click can
        # land on top of a key press; swallow the immediate repeat.
        if self._repeated():
            return
        if self.state == RECORDING:
            self.stop()
        elif self.state == IDLE:
            self.start()
        # a request during its own BUSY is ignored; nothing queues up

    def _toggle_ask(self):
        if self._repeated():
            return
        if self.ask_state == RECORDING:
            self.stop_ask()
        elif self.ask_state == IDLE:
            self.start_ask()

    def _repeated(self):
        if self.last_toggle.isValid() and self.last_toggle.elapsed() < 400:
            return True
        self.last_toggle.restart()
        return False

    def start(self):
        if self.state != IDLE or self.recording:
            return
        self.overlay.show_recording()
        self._begin_recording(DICTATION)
        self._set_state(RECORDING)

    def start_ask(self):
        if self.ask_state != IDLE or self.recording:
            return
        self.ask_overlay.show_recording(asking=True)
        self._begin_recording(ASK)
        self._set_ask_state(RECORDING)

    def _begin_recording(self, owner):
        """One microphone, so one of the two holds it at a time."""
        self.recorder_owner = owner
        self._run_id += 1
        self.elapsed.restart()
        self.ticker.start()
        self.recorder.start(self.conf["mic_target"], self.conf["max_seconds"])

    def stop(self):
        if self.state != RECORDING:
            return
        self.ticker.stop()
        self._set_state(BUSY)
        self.overlay.show_busy(t("Transcribing…"))
        self.recorder.stop()

    def stop_ask(self):
        if self.ask_state != RECORDING:
            return
        self.ticker.stop()
        self._set_ask_state(BUSY)
        self.ask_overlay.show_busy(t("Transcribing…"))
        self.recorder.stop()

    def _cancel(self):
        """Throw away whichever recording is running."""
        if not self.recording:
            return
        asking = self.ask_state == RECORDING
        self.ticker.stop()
        self.recorder.cancel()
        self.recorder_owner = None
        # What goes over the socket is read by a program as often as by a
        # person, so it stays in one language; only what a run itself said
        # travels through translated.
        dropped = {"ok": False, "cancelled": True, "error": "cancelled"}
        if asking:
            self.ask_overlay.dismiss()
            self._set_ask_state(IDLE)
            self._settle(ASK, dropped)
        else:
            self.overlay.dismiss()
            self._set_state(IDLE)
            self._settle(DICTATION, dropped)

    def cancel_ask(self):
        """Call off the agent, whether it is still recording or already working."""
        if self.ask_state == RECORDING:
            self._cancel()
        elif self.ask_state == BUSY:
            self.ask_overlay.show_busy(t("Stopping…"))
            self.ask_pipeline.cancel()

    def reset_conversation(self):
        """Drop the thread Claude has been following, so the next command starts
        a conversation of its own."""
        assistant.clear_session()
        self.ask_overlay.show_done(
            t("{name} starts fresh next time.",
              name=assistant.display_name(self.conf)), 2500
        )

    def _recording_overlay(self):
        return self.ask_overlay if self.recorder_owner == ASK else self.overlay

    def _on_level(self, level):
        self._recording_overlay().push_level(level)

    def _tick(self):
        seconds = self.elapsed.elapsed() / 1000.0
        self._recording_overlay().set_seconds(seconds)
        if seconds >= self.conf["max_seconds"]:
            (self.stop_ask if self.recorder_owner == ASK else self.stop)()

    # ---- meetings ---------------------------------------------------------

    def _toggle_meeting(self):
        if self.meeting_state == M_IDLE:
            self.start_meeting()
        elif self.meeting_state == M_RECORDING:
            self.stop_meeting()

    def start_meeting(self):
        if self.meeting_state != M_IDLE:
            return
        base = meeting.new_base()
        _, wav_path = cfg.meeting_paths(base)
        self.meeting_recorder.start(
            str(wav_path),
            self.conf["meeting_mic_target"] or self.conf["mic_target"],
            self.conf["meeting_system_target"],
            self.conf["meeting_max_seconds"],
        )
        if not self.meeting_recorder.active:
            return  # start() has already said what went wrong
        self.meeting_base = base
        self.meeting_elapsed.restart()
        self.meeting_ticker.start()
        self.overlay.show_meeting()
        QTimer.singleShot(PEEK_MS, self._conceal_meeting_overlay)
        self._set_meeting_state(M_RECORDING)

    def stop_meeting(self):
        if self.meeting_state != M_RECORDING:
            return
        self.meeting_ticker.stop()
        self._set_meeting_state(M_WORKING)
        self.overlay.show_busy(t("Ending the meeting…"))
        self.meeting_recorder.stop()

    def cancel_meeting(self):
        if self.meeting_state != M_RECORDING:
            return
        self.meeting_ticker.stop()
        self.meeting_recorder.cancel()
        if self.overlay.state == "meeting":
            self.overlay.dismiss()
        self._set_meeting_state(M_IDLE)
        self._settle(MEETING, {"ok": False, "cancelled": True, "error": "cancelled"})

    def _conceal_meeting_overlay(self):
        if self.overlay.state == "meeting":
            self.overlay.dismiss()

    def _on_meeting_levels(self, mine, theirs):
        self.overlay.push_levels(mine, theirs)

    def _meeting_tick(self):
        seconds = self.meeting_elapsed.elapsed() / 1000.0
        if self.overlay.state == "meeting":
            self.overlay.set_seconds(seconds)
        if self.state == IDLE:
            self.tray.setToolTip(
                t("Dikte: in a meeting ({time})", time=_clock(seconds))
            )
        if seconds >= self.conf["meeting_max_seconds"]:
            self.stop_meeting()

    def _on_meeting_recorded(self, path, duration):
        entry = meeting.new_entry(self.meeting_base, duration)
        try:
            cfg.save_meeting(entry)
        except OSError as exc:
            self._on_meeting_failed(entry["base"], str(exc))
            return
        # On the way out there is no time to write anything up; the recording is
        # on disk and listed, and the Minutes tab can pick it up next time.
        if self._quitting:
            return
        if not self.meetings.run(entry):
            self._set_meeting_state(M_IDLE)
            self._settle(MEETING, {
                "ok": False, "base": entry["base"],
                "error": "recording saved, but the previous meeting is still "
                         "being written up",
            })
            self.tray.showMessage(
                "Dikte",
                t("Recording saved. The previous meeting is still being written "
                  "up, so start this one from Settings → Minutes when it is done."),
                QSystemTrayIcon.MessageIcon.Information, 10000,
            )
            return
        self.overlay.show_done(t("Meeting recorded, writing it up…"), 4000)

    def _on_meeting_progress(self, _base, message):
        self.meeting_message = message
        if self.state == IDLE and self.meeting_state == M_WORKING:
            self.tray.setToolTip(message)

    def _on_meeting_finished(self, base, title):
        self._set_meeting_state(M_IDLE)
        doc_path, _ = cfg.meeting_paths(base)
        self._settle(MEETING, {"ok": True, "base": base, "title": title,
                               "path": str(doc_path)})
        self.overlay.show_done(t("Meeting written up: {title}", title=title), 5000)
        self.tray.showMessage(
            t("Dikte: the meeting is written up"), f"{title}\n{doc_path}",
            QSystemTrayIcon.MessageIcon.Information, 10000,
        )

    def _on_meeting_failed(self, _base, error):
        self._set_meeting_state(M_IDLE)
        self._settle(MEETING, {"ok": False, "base": _base, "error": error})
        first_line = error.strip().splitlines()[0]
        self.overlay.show_error(t("Meeting failed: {error}", error=first_line))
        self.tray.showMessage(
            t("Dikte: the meeting could not be written up"),
            t("{error}\n\nThe recording has been kept. Settings → Minutes can "
              "try again.", error=error),
            QSystemTrayIcon.MessageIcon.Warning, 12000,
        )

    def _on_meeting_error(self, message):
        """The recorder itself could not run."""
        self.meeting_ticker.stop()
        if self.overlay.state == "meeting":
            self.overlay.dismiss()
        self._set_meeting_state(M_IDLE)
        self._settle(MEETING, {"ok": False, "error": message})
        self._on_error(message)

    def _on_meeting_died(self):
        if self.meeting_state != M_RECORDING:
            return
        self.tray.showMessage(
            "Dikte",
            t("The recording stopped on its own; the sound device may have gone "
              "away. Keeping what was captured."),
            QSystemTrayIcon.MessageIcon.Warning, 10000,
        )
        self.stop_meeting()

    def _on_recorded(self, wav_path, duration, rms_values):
        owner, self.recorder_owner = self.recorder_owner, None
        wants_paste = self.paste_override.pop(owner, None)
        if owner == ASK:
            self.ask_pipeline.run(wav_path, duration, rms_values, ask=True,
                                  paste=wants_paste)
        else:
            self.pipeline.run(wav_path, duration, rms_values, paste=wants_paste)

    def _on_finished(self, _raw, text, warning):
        if warning:
            # The text was still pasted, but cleanup did not run. Say so loudly:
            # a rejected key otherwise looks exactly like working dictation.
            self.overlay.show_warning(
                t("Pasted raw, cleanup failed: {error}", error=warning.splitlines()[0])
            )
            self.tray.showMessage(
                t("Dikte: cleanup failed"), warning,
                QSystemTrayIcon.MessageIcon.Warning, 10000,
            )
        else:
            action = t("Pasted") if self.conf["auto_paste"] else t("Copied")
            self.overlay.show_done(
                t("{action}: {preview}", action=action, preview=_preview(text))
            )
        self._set_state(IDLE)
        self._settle(DICTATION, {"ok": True, "text": text, "raw": _raw,
                                 "warning": warning})

    def _on_ask_finished(self, _raw, text, warning):
        agent = assistant.display_name(self.conf)
        if warning:
            # A tool the agent was not allowed to touch otherwise looks exactly
            # like a job that worked: the reply reads perfectly normal.
            self.ask_overlay.show_warning(
                t("{name} answered, but: {error}",
                  name=agent, error=warning.splitlines()[0])
            )
            self.tray.showMessage(
                t("Dikte: {name} could not do all of it", name=agent),
                f"{warning}\n\n{text}", QSystemTrayIcon.MessageIcon.Warning, 10000,
            )
        else:
            # Longer than a dictation's flash: this one is an answer, and it is
            # worth being able to read the start of it in the corner.
            self.ask_overlay.show_done(
                t("{name}: {preview}", name=agent, preview=_preview(text)), 6000
            )
        self._set_ask_state(IDLE)
        self._settle(ASK, {"ok": True, "answer": text, "question": _raw,
                           "warning": warning, "agent": agent})

    def _on_ask_cancelled(self):
        self.ask_overlay.show_done(t("Stopped."), 2000)
        self._set_ask_state(IDLE)
        self._settle(ASK, {"ok": False, "cancelled": True, "error": "stopped"})

    def _on_recorder_error(self, message):
        """The microphone itself could not run, so it belongs to whoever asked."""
        owner, self.recorder_owner = self.recorder_owner, None
        self.ticker.stop()
        (self._on_ask_error if owner == ASK else self._on_error)(message)

    def _on_error(self, message):
        self._report(message, self.overlay)
        self._set_state(IDLE)
        self._settle(DICTATION, {"ok": False, "error": message})

    def _on_ask_error(self, message):
        self._report(message, self.ask_overlay)
        self._set_ask_state(IDLE)
        self._settle(ASK, {"ok": False, "error": message})

    def _report(self, message, overlay):
        first_line = message.strip().splitlines()[0]
        overlay.show_error(first_line)
        if len(message) > len(first_line):
            self.tray.showMessage("Dikte", message, QSystemTrayIcon.MessageIcon.Warning, 8000)

    # ---- settings ---------------------------------------------------------

    def open_settings(self):
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self.conf, self.meetings)
            self.settings_window.applied.connect(self._apply_settings)
            self.settings_window.finished.connect(self._settings_closed)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def _settings_closed(self, *_):
        # Don't drop the object while its own signal is still being delivered.
        QTimer.singleShot(0, lambda: setattr(self, "settings_window", None))

    def _apply_local(self):
        """Pass the local settings on, and hold the models ready if asked to.

        Loading a model takes a second or two for whisper and longer for an LLM.
        Doing it while Dikte starts rather than on the first dictation is the
        whole reason a server is kept alive instead of running the program once
        per recording; the checkboxes are there for the machine whose memory is
        wanted elsewhere.
        """
        self.conf.apply_local()
        wanted = []
        if self.conf["transcribe_provider"] == "local":
            if self.conf["local_preload"] and self.conf.local_whisper_ready():
                wanted.append((ggml.whisper, "whisper"))
        else:
            ggml.whisper.stop()      # give the memory back when it is not in use
        if self.conf.uses_local_llm():
            if self.conf["local_llm_preload"] and self.conf.local_llm_ready():
                wanted.append((ggml.llm, "llama"))
        else:
            ggml.llm.stop()

        def warm():
            for server, name in wanted:
                try:
                    server.serve()
                except ggml.LocalError as exc:
                    # Not worth an indicator: the first dictation raises the
                    # same thing where the user can act on it.
                    print(f"dikte: {name}: {exc}", file=sys.stderr)

        if wanted:
            threading.Thread(target=warm, daemon=True).start()

    def _apply_settings(self):
        self.overlay.corner = self.conf["overlay_corner"]
        self.ask_overlay.corner = self.conf["overlay_corner"]
        self._apply_local()
        self._build_tray()
        self._refresh_tray()
        # Where the desktop has no shortcut registry of its own, the listener is
        # not the fallback the setting offers to turn on: it is the only way the
        # keys arrive at all, so it runs whatever the setting says.
        if self.conf["evdev_hotkey"] or not hotkey.installs_shortcuts():
            self.evdev.start({name: self.conf[spec.setting]
                              for name, spec in hotkey.SHORTCUTS.items()})
        else:
            self.evdev.stop()

    def restart(self):
        """Replace this process with a fresh one, picking up code and settings."""
        if self.settings_window is not None:
            self.settings_window.close()
        self.shutdown()
        QLocalServer.removeServer(SERVER_NAME)
        os.execv(sys.executable, [sys.executable, ipc.script_path(), "--gui"])

    def shutdown(self):
        self._quitting = True
        self.evdev.stop()
        if self.recording:
            self.recorder.cancel()
        # A meeting in progress is closed properly rather than thrown away: the
        # WAV ends up valid and listed, ready to be written up after the restart.
        if self.meeting_state == M_RECORDING:
            self.meeting_ticker.stop()
            self.meeting_recorder.stop()
        self.overlay.dismiss()
        self.ask_overlay.dismiss()
        # Also on the restart path, which replaces the process without ever
        # reaching atexit and would otherwise leave the models in memory.
        ggml.stop_all()
        self.tray.hide()


def _preview(text):
    line = text.replace("\n", " ")
    return line[:48] + ("…" if len(line) > 48 else "")


def _clock(seconds):
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}:{minutes:02d}:{secs:02d}" if hours
            else f"{minutes}:{secs:02d}")


def main():
    argv = sys.argv[1:]
    # Anything typed at a terminal is the command line's business, including
    # --help and the verbs that only need a message sent. It comes back here
    # with --gui when it turns out there is no instance to send one to.
    if "--gui" not in argv:
        return cli.run(argv)
    return run_app([arg for arg in argv if arg != "--gui"])


def install_signal_handlers(app):
    """Quit properly on the signals a session sends, rather than dying where we stand.

    Qt spends its time blocked inside C, and a Python signal handler only runs
    between bytecodes, so on its own it would not run until the next event
    arrived, which for an idle tray icon may be never. set_wakeup_fd writes the
    signal number to a socket instead, and a notifier turns that into an event
    Qt does deliver.

    Worth the trouble because of what shutdown() does: a logout sends SIGTERM,
    and without this a whisper.cpp or llama.cpp server outlives the session
    holding its model in memory. SIGKILL cannot be caught at all, which is what
    ggml.sweep() is for.

    Returns the objects it made; they have to stay alive to keep working.
    """
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    signal.set_wakeup_fd(writer.fileno())
    notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read)

    def woken():
        with contextlib.suppress(OSError):
            reader.recv(64)
        app.quit()          # aboutToQuit runs shutdown()

    notifier.activated.connect(woken)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        # A handler that does nothing, so that the default action, stopping the
        # process where it stands, is replaced by the wakeup above.
        signal.signal(sig, lambda *_: None)
    return reader, writer, notifier


def _stay_out_of_the_dock():
    """Ask macOS to treat this as a menu bar application, not a windowed one.

    LSUIElement in the bundle says the same thing, but it is read for the
    process LaunchServices started, and that is the launcher script rather than
    the Python it runs: the interpreter is a child, and the child inherits the
    registration without inheriting the policy. Said here it holds however Dikte
    was started, including straight from a terminal.

    Accessory rather than Prohibited: a prohibited application cannot put
    anything in the menu bar, which is the whole interface.
    """
    if sys.platform != "darwin":
        return
    NS_ACCESSORY = 1               # NSApplicationActivationPolicyAccessory
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        # objc_msgSend is a trampoline with no signature of its own, and on
        # arm64 the arguments have to be in the registers the real method
        # expects, so each call gets a prototype of its own. Built from the
        # address: handing CFUNCTYPE the imported function object would make a
        # callback into it rather than a call through it, and the crash lands
        # inside the Objective-C runtime with nothing to read.
        send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        shared = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        )(send)
        policy = ctypes.CFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
        )(send)

        application = shared(objc.objc_getClass(b"NSApplication"),
                             objc.sel_registerName(b"sharedApplication"))
        if application:
            policy(application, objc.sel_registerName(b"setActivationPolicy:"),
                   NS_ACCESSORY)
    except (OSError, AttributeError, TypeError):
        # A Dock icon is a blemish, not a failure: everything still works.
        pass


def run_app(args):
    command = args[0] if args else ""

    app = QApplication(sys.argv)
    app.setApplicationName("Dikte")
    app.setDesktopFileName("dikte")
    app.setQuitOnLastWindowClosed(False)
    _stay_out_of_the_dock()
    # Before Dikte is built, because building it is what may start a server, and
    # a signal arriving in the middle of that would otherwise take the default
    # action and leave the server behind. A signal this early lands in the
    # socket and is delivered as soon as the event loop starts. Held in a name
    # so that the notifier and its socket outlive this function.
    signal_plumbing = install_signal_handlers(app)  # noqa: F841

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("dikte: no system tray found, running anyway")

    dikte = Dikte(app)

    server = QLocalServer()
    # Qt puts the socket in /tmp, so keep it to this user: commands like
    # "quit" should not be reachable by anyone else on the machine.
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(SERVER_NAME)
    if not server.listen(SERVER_NAME):
        print(f"dikte: could not open the IPC socket: {server.errorString()}")

    def on_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def reply(payload):
            """One JSON object back, and the connection is done.

            A request that waited for its run may find the terminal gone by the
            time the answer is ready, which is a closed socket and not an error.
            """
            if conn.state() != QLocalSocket.LocalSocketState.ConnectedState:
                return
            conn.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            conn.flush()
            conn.disconnectFromServer()

        def read():
            payload = bytes(conn.readAll()).decode("utf-8", "replace").strip()
            try:
                request = json.loads(payload)
                if not isinstance(request, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                request = {"cmd": payload}   # a bare verb, as older versions sent
            dikte.handle(request, reply)

        conn.readyRead.connect(read)

    server.newConnection.connect(on_connection)
    app.aboutToQuit.connect(dikte.shutdown)

    # No key for the chosen transcription provider means nothing can work yet,
    # so the settings window is the only useful thing to open.
    # A transcription provider that cannot run yet, whether that is a missing
    # API key or a model nobody has downloaded, means nothing can work, so the
    # settings window is the only useful thing to open.
    if command == "settings" or not dikte.conf.transcribe_ready():
        dikte.open_settings()
    elif command == "toggle":
        QTimer.singleShot(0, dikte.toggle)
    elif command == "ask":
        QTimer.singleShot(0, dikte.toggle_ask)
    elif command == "meeting":
        QTimer.singleShot(0, dikte.toggle_meeting)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
