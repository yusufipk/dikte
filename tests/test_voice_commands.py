"""Tests for voice command processing."""

import unittest

from dikte import voice_commands


class VoiceCommands(unittest.TestCase):
    """Voice command detection and processing."""

    def setUp(self):
        self.config_enabled = {"voice_commands_enabled": True, "voice_snippets": {}}
        self.config_disabled = {"voice_commands_enabled": False, "voice_snippets": {}}

    def test_disabled_returns_unchanged(self):
        text = "This is a test. New paragraph. Some more text."
        result = voice_commands.process_commands(text, self.config_disabled)
        self.assertEqual(result, text)

    def test_new_paragraph_command(self):
        text = "First paragraph. New paragraph. Second paragraph."
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertIn("\n\n", result)
        self.assertNotIn("New paragraph", result)

    def test_new_paragraph_turkish(self):
        text = "İlk paragraf. Yeni paragraf. İkinci paragraf."
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertIn("\n\n", result)
        self.assertNotIn("Yeni paragraf", result)

    def test_delete_that_command(self):
        text = "Keep this. Delete this sentence. Delete that. Continue here."
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertNotIn("Delete this sentence", result)
        self.assertIn("Keep this", result)
        self.assertIn("Continue here", result)

    def test_period_command(self):
        text = "This is a sentence period another sentence"
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertIn("sentence.", result)
        self.assertIn("Another sentence", result)  # Capitalized
        self.assertNotIn(" period ", result.lower())

    def test_comma_command(self):
        text = "First item comma second item comma third item"
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertEqual(result.count(","), 2)
        self.assertNotIn(" comma ", result.lower())

    def test_capitalize_command(self):
        text = "this is lowercase capitalize test word"
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertIn("Test word", result)
        self.assertNotIn("capitalize", result.lower())

    def test_new_line_command(self):
        text = "First line. New line. Second line."
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertEqual(result.count("\n"), 1)
        self.assertNotIn("New line", result)

    def test_custom_snippet(self):
        config = {
            "voice_commands_enabled": True,
            "voice_snippets": {
                "my email": "tunahan@example.com",
                "my signature": "Best regards,\nTunahan İpek"
            }
        }
        text = "You can reach me at my email for questions. My signature."
        result = voice_commands.process_commands(text, config)
        self.assertIn("tunahan@example.com", result)
        self.assertIn("Best regards", result)
        self.assertNotIn("my email", result.lower())
        self.assertNotIn("my signature", result.lower())

    def test_multiple_commands_in_sequence(self):
        text = "First sentence period new paragraph second sentence comma with a clause"
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertIn("\n\n", result)
        self.assertIn(".", result)
        self.assertIn(",", result)
        self.assertNotIn("period", result.lower())
        self.assertNotIn("new paragraph", result.lower())

    def test_case_insensitive_matching(self):
        text = "Test NEW PARAGRAPH next part PERIOD end"
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertIn("\n\n", result)
        self.assertIn(".", result)

    def test_scratch_that_variant(self):
        text = "This is wrong. Scratch that. This is correct."
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertNotIn("This is wrong", result)
        self.assertIn("This is correct", result)

    def test_turkish_commands(self):
        text = "Birinci cümle nokta yeni satır ikinci cümle virgül devam ediyor"
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertIn(".", result)
        self.assertIn("\n", result)
        self.assertIn(",", result)

    def test_empty_text(self):
        result = voice_commands.process_commands("", self.config_enabled)
        self.assertEqual(result, "")

    def test_text_with_no_commands(self):
        text = "This is just regular text with no commands at all."
        result = voice_commands.process_commands(text, self.config_enabled)
        self.assertEqual(result, text)

    def test_available_commands_returns_list(self):
        commands = voice_commands.available_commands()
        self.assertIsInstance(commands, list)
        self.assertTrue(len(commands) > 0)
        for trigger, desc in commands:
            self.assertIsInstance(trigger, str)
            self.assertIsInstance(desc, str)


if __name__ == "__main__":
    unittest.main()
