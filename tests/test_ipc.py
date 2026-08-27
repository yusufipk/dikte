"""The request a terminal sends to the running instance, and the reply it reads.

The wire format has to stay backwards compatible in both directions: a stale KDE
shortcut still sends a bare verb, and an instance from before replies existed
answers by saying nothing at all.
"""

import json
import os
import pathlib
import shlex
import sys
import unittest
from unittest import mock

from dikte import ipc
from tests.support import DikteTest


class FakeSocket:
    """QLocalSocket, as much of it as ipc.send() touches."""

    def __init__(self, connected=True, reply=b""):
        self.connected = connected
        self.reply = reply
        self.written = b""
        self.server = ""
        self.disconnected = False
        self.read_limits = []
        self._served = False

    def connectToServer(self, name):
        self.server = name

    def waitForConnected(self, ms):
        return self.connected

    def write(self, data):
        self.written += bytes(data)

    def flush(self):
        pass

    def waitForBytesWritten(self, ms):
        return True

    def waitForReadyRead(self, ms):
        self.read_limits.append(ms)
        if self._served or not self.reply:
            return False
        self._served = True
        return True

    def readAll(self):
        return self.reply

    def disconnectFromServer(self):
        self.disconnected = True


class Paths(unittest.TestCase):
    def test_script_path_points_at_dikte(self):
        # By its parts rather than as a string: the separator is a backslash on
        # Windows, and the path is what a shortcut there runs too.
        path = pathlib.Path(ipc.script_path())
        self.assertEqual(path.parts[-2:], ("dikte", "__main__.py"))
        self.assertTrue(os.path.exists(ipc.script_path()))

    def test_the_shortcut_command_runs_it_with_this_interpreter(self):
        # Read back through the same quoting it went out with: a Windows path
        # is spelled with backslashes and comes out of the join quoted.
        self.assertEqual(shlex.split(ipc.command_for("toggle")),
                         [sys.executable, ipc.script_path(), "toggle"])

    def test_a_packaged_build_names_itself_and_no_interpreter(self):
        """There is no __main__.py on disk in one, and sys.executable is the
        build's own binary rather than a Python anybody could run it with."""
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "/Applications/Dikte.app/Contents/MacOS/Dikte"), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ipc.launcher(),
                             ["/Applications/Dikte.app/Contents/MacOS/Dikte"])

    def test_an_appimage_names_the_file_rather_than_this_run_s_mount(self):
        """A shortcut written to the mount works until the next login."""
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", "/tmp/.mount_ab12/usr/bin/dikte"), \
             mock.patch.dict(os.environ, {"APPIMAGE": "/home/me/Dikte.AppImage"}):
            self.assertEqual(ipc.command_for("toggle"),
                             "/home/me/Dikte.AppImage toggle")

    def test_a_path_with_a_space_in_it_is_quoted(self):
        """Which is every Mac, and an AppImage kept anywhere with a name."""
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.dict(os.environ, {"APPIMAGE": "/home/me/My Things/Dikte.AppImage"}):
            self.assertEqual(ipc.command_for("cancel"),
                             "'/home/me/My Things/Dikte.AppImage' cancel")

    @unittest.skipUnless(hasattr(os, "getuid"),
                         "the socket is named after a user id, which Windows "
                         "has no equivalent of")
    def test_the_socket_is_per_user(self):
        self.assertEqual(ipc.SERVER_NAME, f"dikte-{os.getuid()}")


class Send(unittest.TestCase):
    def send(self, socket, *args, **kwargs):
        with mock.patch.object(ipc, "QLocalSocket", return_value=socket):
            return ipc.send(*args, **kwargs)

    def written_line(self, socket):
        return socket.written.decode("utf-8").strip()

    def test_nothing_running(self):
        sock = FakeSocket(connected=False)
        self.assertIsNone(self.send(sock, "toggle"))
        self.assertEqual(sock.written, b"")

    def test_a_verb_on_its_own_goes_as_the_bare_word(self):
        """An older instance only understands this, and it is how updates land."""
        sock = FakeSocket(reply=b'{"ok": true}\n')
        self.send(sock, "restart")
        self.assertEqual(self.written_line(sock), "restart")

    def test_a_verb_with_arguments_goes_as_json(self):
        sock = FakeSocket(reply=b'{"ok": true}\n')
        self.send(sock, "ask", text="what time is it")
        self.assertEqual(json.loads(self.written_line(sock)),
                         {"cmd": "ask", "text": "what time is it"})

    def test_arguments_that_are_none_are_left_out(self):
        sock = FakeSocket(reply=b'{"ok": true}\n')
        self.send(sock, "record", seconds=None, paste=False)
        self.assertEqual(json.loads(self.written_line(sock)),
                         {"cmd": "record", "paste": False})

    def test_asking_to_be_waited_for_says_so(self):
        sock = FakeSocket(reply=b'{"ok": true, "text": "hello"}\n')
        reply = self.send(sock, "toggle", wait=True)
        self.assertTrue(json.loads(self.written_line(sock))["wait"])
        self.assertEqual(reply["text"], "hello")

    def test_a_wait_with_no_timeout_reads_without_a_deadline(self):
        sock = FakeSocket(reply=b'{"ok": true}\n')
        self.send(sock, "toggle", wait=True)
        self.assertEqual(sock.read_limits[0], -1)

    def test_a_timeout_is_passed_on_in_milliseconds(self):
        sock = FakeSocket(reply=b'{"ok": true}\n')
        self.send(sock, "toggle", wait=True, timeout=2.5)
        self.assertEqual(sock.read_limits[0], 2500)

    def test_a_fire_and_forget_verb_does_not_wait_around(self):
        sock = FakeSocket(reply=b'{"ok": true}\n')
        self.send(sock, "cancel")
        self.assertEqual(sock.read_limits[0], ipc.CONNECT_MS)

    def test_the_reply_comes_back_as_it_was_sent(self):
        sock = FakeSocket(reply=b'{"ok": false, "error": "no microphone"}\n')
        self.assertEqual(self.send(sock, "toggle"),
                         {"ok": False, "error": "no microphone"})

    def test_silence_from_an_old_instance_means_the_verb_went_through(self):
        sock = FakeSocket(reply=b"")
        reply = self.send(sock, "cancel")
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["legacy"])

    def test_silence_during_a_wait_is_a_failure_with_a_way_out(self):
        sock = FakeSocket(reply=b"")
        reply = self.send(sock, "toggle", wait=True)
        self.assertFalse(reply["ok"])
        self.assertIn("dikte restart", reply["error"])

    def test_a_reply_that_is_not_json(self):
        sock = FakeSocket(reply=b"ok\n")
        self.assertEqual(self.send(sock, "toggle"), {"ok": True, "legacy": True})

    def test_a_reply_that_is_json_but_not_an_object(self):
        sock = FakeSocket(reply=b"[1, 2, 3]\n")
        self.assertEqual(self.send(sock, "toggle"), {"ok": True, "legacy": True})

    def test_the_socket_is_always_let_go_of(self):
        sock = FakeSocket(reply=b'{"ok": true}\n')
        self.send(sock, "toggle")
        self.assertTrue(sock.disconnected)


class AlreadyServing(unittest.TestCase):
    """The single-instance check, which listen() cannot be: a Windows pipe
    takes a second server on the same name rather than refusing it."""

    def probe(self, socket):
        with mock.patch.object(ipc, "QLocalSocket", return_value=socket):
            return ipc.already_serving()

    def test_nothing_running_means_go_ahead(self):
        self.assertFalse(self.probe(FakeSocket(connected=False)))

    def test_an_answer_means_yield(self):
        self.assertTrue(self.probe(FakeSocket(reply=b'{"ok": true}\n')))

    def test_the_probe_has_no_side_effect(self):
        """A probe that opened a window would open it during the relaunch a
        slow instance provokes, on top of the verb being forwarded."""
        sock = FakeSocket(reply=b'{"ok": true}\n')
        self.probe(sock)
        self.assertEqual(sock.written.decode("utf-8").strip(), "status")

    def test_an_instance_too_old_to_answer_still_counts_as_running(self):
        self.assertTrue(self.probe(FakeSocket(reply=b"")))


class InstanceLock(DikteTest):
    def setUp(self):
        super().setUp()
        # The lock derives its home from paths, which DikteTest's cfg patches
        # do not cover; without this the test would write into the real one.
        from dikte import paths
        self.patch_attr(paths, "DATA_DIR", self.path("data"))

    def test_one_holder_at_a_time(self):
        first = ipc.instance_lock()
        self.assertIsNotNone(first)
        self.assertTrue(first.tryLock(0))
        second = ipc.instance_lock()
        self.assertFalse(second.tryLock(0))
        first.unlock()
        self.assertTrue(second.tryLock(0))
        second.unlock()

    def test_the_lock_lives_in_the_data_directory(self):
        from dikte import paths
        lock = ipc.instance_lock()
        self.assertTrue(lock.tryLock(0))
        self.assertTrue((paths.DATA_DIR / "dikte.lock").exists())
        lock.unlock()


class Respawn(unittest.TestCase):
    def test_windows_starts_a_detached_process_and_returns(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(ipc, "launcher", return_value=["py", "x"]), \
                mock.patch.object(ipc.subprocess, "Popen") as popen:
            ipc.respawn(["--gui"])
        self.assertEqual(popen.call_args.args[0], ["py", "x", "--gui"])
        self.assertEqual(popen.call_args.kwargs["creationflags"],
                         0x00000008 | 0x00000200)

    def test_everywhere_else_the_process_is_replaced(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(ipc, "launcher", return_value=["py", "x"]), \
                mock.patch.object(ipc.os, "execv") as execv:
            ipc.respawn(["toggle", "--gui"])
        execv.assert_called_once_with("py", ["py", "x", "toggle", "--gui"])


if __name__ == "__main__":
    unittest.main()
