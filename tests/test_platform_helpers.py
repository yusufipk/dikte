import sys
import unittest

import assistant
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
