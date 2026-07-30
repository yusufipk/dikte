import sys
import unittest

import hotkey


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


class MacAudioDeviceParserTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS-only AVFoundation test")
    def test_audio_devices_are_listed_as_index_name_pairs(self):
        import audio

        devices = audio._mac_audio_devices()
        self.assertTrue(devices)
        self.assertTrue(all(index.isdigit() and name for index, name in devices))


if __name__ == "__main__":
    unittest.main()
