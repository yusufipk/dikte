import plistlib
import sys
import tempfile
import unittest
import zipfile
from math import pi
from pathlib import Path

import assistant
import autostart
import dikte
import hotkey
import local_whisper
import updater


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


class AnalogClockTests(unittest.TestCase):
    def test_hand_angles_show_half_past_twelve(self):
        hour, minute = dikte._clock_hand_angles(12, 30)
        self.assertAlmostEqual(hour, pi / 12)
        self.assertAlmostEqual(minute, pi)

    def test_hand_angles_wrap_after_noon(self):
        self.assertEqual(
            dikte._clock_hand_angles(15, 0),
            dikte._clock_hand_angles(3, 0),
        )


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


class MacUpdaterTests(unittest.TestCase):
    @staticmethod
    def _release(version="0.3.5", digest=None):
        digest = digest or "a" * 64
        return {
            "tag_name": f"macos-v{version}",
            "draft": False,
            "prerelease": True,
            "assets": [{
                "name": "Dikte-macOS.zip",
                "size": 1234,
                "digest": f"sha256:{digest}",
                "browser_download_url": (
                    "https://github.com/benfirad/dikte-macos/releases/download/"
                    f"macos-v{version}/Dikte-macOS.zip"
                ),
            }],
        }

    def test_latest_verified_release_is_selected(self):
        latest = updater.select_latest_release([
            self._release("0.3.4"),
            self._release("0.4.0"),
            {"tag_name": "unrelated"},
        ])
        self.assertEqual(latest.version, "0.4.0")
        self.assertEqual(latest.version_tuple, (0, 4, 0))

    def test_release_without_digest_is_rejected(self):
        release = self._release()
        release["assets"][0]["digest"] = None
        self.assertIsNone(updater.release_from_payload(release))

    def test_release_from_another_download_host_is_rejected(self):
        release = self._release()
        release["assets"][0]["browser_download_url"] = (
            "https://example.com/Dikte-macOS.zip"
        )
        self.assertIsNone(updater.release_from_payload(release))

    def test_archive_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../outside", "unsafe")
            with self.assertRaisesRegex(RuntimeError, "unsafe path"):
                updater._safe_archive(archive)

    def test_packaged_app_path_is_derived_from_executable(self):
        self.assertEqual(
            updater.packaged_app_path(
                "/Applications/Dikte.app/Contents/MacOS/Dikte"
            ),
            Path("/Applications/Dikte.app"),
        )
        self.assertIsNone(updater.packaged_app_path("/usr/bin/python3"))


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
