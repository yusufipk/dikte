import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

import assistant
import autostart
import dikte
import hotkey
import local_whisper


class MacShortcutParserTests(unittest.TestCase):
    def test_ctrl_space(self):
        modifiers, key = hotkey._parse_macos_shortcut("Ctrl+Space")
        self.assertEqual(key, 49)
        self.assertEqual(modifiers, 1 << 12)

    def test_command_option_letter(self):
        modifiers, key = hotkey._parse_macos_shortcut("Cmd+Option+D")
        self.assertEqual(key, 2)
        self.assertEqual(modifiers, (1 << 8) | (1 << 11))

    def test_rejects_unknown_key(self):
        self.assertEqual(hotkey._parse_macos_shortcut("Cmd+Nope"), (None, None))


class TrayMenuPolicyTests(unittest.TestCase):
    def test_macos_avoids_the_crashing_native_context_menu(self):
        self.assertFalse(dikte._uses_native_tray_context_menu("darwin"))

    def test_linux_keeps_the_native_context_menu(self):
        self.assertTrue(dikte._uses_native_tray_context_menu("linux"))


class RestartCommandTests(unittest.TestCase):
    def test_packaged_restart_does_not_pass_the_bundled_script(self):
        self.assertEqual(dikte._restart_arguments(True), [sys.executable])

    def test_source_restart_passes_the_python_script(self):
        script = "/tmp/dikte.py"
        self.assertEqual(
            dikte._restart_arguments(False, script),
            [sys.executable, script],
        )


class MacAutostartTests(unittest.TestCase):
    def test_packaged_app_executable_is_recognised(self):
        self.assertTrue(autostart._is_macos_app_executable(
            "/Applications/Dikte.app/Contents/MacOS/Dikte"
        ))
        self.assertFalse(autostart._is_macos_app_executable("/usr/bin/python3"))

    def test_launch_agent_is_created_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dev.dikte.app.plist"
            executable = "/Applications/Dikte.app/Contents/MacOS/Dikte"
            self.assertTrue(autostart.update(True, executable, path))
            payload = plistlib.loads(path.read_bytes())
            self.assertEqual(payload["Label"], "dev.dikte.app")
            self.assertEqual(payload["ProgramArguments"], [executable])
            self.assertTrue(payload["RunAtLoad"])
            self.assertFalse(autostart.update(True, executable, path))
            self.assertTrue(autostart.update(False, executable, path))
            self.assertFalse(path.exists())


class MacAudioDeviceParserTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS-only AVFoundation test")
    def test_audio_devices_are_listed_as_index_name_pairs(self):
        import audio

        devices = audio._mac_audio_devices()
        self.assertTrue(devices)
        self.assertTrue(all(index.isdigit() and name for index, name in devices))


class LocalWhisperTests(unittest.TestCase):
    def test_json_segments_are_converted_to_seconds(self):
        payload = {
            "transcription": [
                {"offsets": {"from": 250, "to": 1750}, "text": " Merhaba dünya. "},
                {"offsets": {"from": 1750, "to": 2600}, "text": "Nasılsın?"},
            ]
        }
        self.assertEqual(
            local_whisper.parse_result(payload),
            [(0.25, 1.75, "Merhaba dünya."), (1.75, 2.6, "Nasılsın?")],
        )


class CleanupProviderTests(unittest.TestCase):
    def test_codex_cleanup_model_name_uses_default(self):
        conf = {
            "cleanup_provider": "codex",
            "cleanup_codex_model": "",
            "cleanup_model": "unused",
        }
        self.assertEqual(assistant.cleanup_model_name(conf), "codex:default")

    def test_openrouter_cleanup_model_name_is_preserved(self):
        conf = {
            "cleanup_provider": "openrouter",
            "cleanup_codex_model": "",
            "cleanup_model": "example/model",
        }
        self.assertEqual(assistant.cleanup_model_name(conf), "example/model")


if __name__ == "__main__":
    unittest.main()
