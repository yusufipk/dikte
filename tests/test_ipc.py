"""The request a terminal sends to the running instance, and the reply it reads.

The wire format has to stay backwards compatible in both directions: a stale KDE
shortcut still sends a bare verb, and an instance from before replies existed
answers by saying nothing at all.
"""

import json
import os
import sys
import unittest
from unittest import mock

import ipc


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
        self.assertTrue(ipc.script_path().endswith("dikte.py"))
        self.assertTrue(os.path.exists(ipc.script_path()))

    def test_the_shortcut_command_runs_it_with_this_interpreter(self):
        command = ipc.command_for("toggle")
        self.assertTrue(command.startswith(sys.executable))
        self.assertTrue(command.endswith(" toggle"))

    def test_the_socket_is_per_user(self):
        self.assertEqual(ipc.SERVER_NAME, f"dikte-{ipc.user_id()}")

    def test_unix_uses_the_numeric_user_id(self):
        if not hasattr(os, "getuid"):
            self.skipTest("Windows has no numeric uid")
        self.assertEqual(ipc.user_id("linux"), str(os.getuid()))

    def test_windows_uses_a_short_hash_of_the_sid(self):
        with mock.patch.object(ipc, "_windows_sid", return_value="S-1-5-21-42"):
            first = ipc.user_id("win32")
            second = ipc.user_id("win32")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertNotIn("S-1-5", first)


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


if __name__ == "__main__":
    unittest.main()
