"""Synthetic examples, local previews and prompt contracts, no model calls."""
from unittest import mock
from PyQt6.QtWidgets import QApplication
from dikte import writing_style as style
from dikte.writing_style_ui import WritingStyle
from tests.support import DikteTest

_app = QApplication.instance() or QApplication([])


class Profile(DikteTest):
    def test_default_and_disabled_are_identity(self):
        conf = self.config(style_preferences={"formality": "formal"})
        self.assertEqual(style.apply("base", conf), "base")
        conf["style_enabled"] = True
        self.assertIn("formal tone", style.apply("base", conf))
        conf["style_enabled"] = False
        self.assertEqual(style.apply("base", conf), "base")

    def test_custom_instruction_wins_entirely(self):
        conf = self.config(style_enabled=True, cleanup_prompt="Translate everything",
                           style_preferences={"brevity": "concise"})
        self.assertEqual(style.apply("custom", conf), "custom")

    def test_explicit_overrides_learned_and_preserves_content(self):
        conf = self.config(style_enabled=True, style_learned={"sentences": "short"},
                           style_preferences={"sentences": "flowing", "brevity": "concise"})
        prompt = style.apply("base", conf)
        self.assertIn("flowing", prompt)
        self.assertNotIn("short sentences", prompt)
        for rule in ("facts", "numbers", "spoken language", "Never translate", "meaning"):
            self.assertIn(rule, prompt)

    def test_malformed_config_and_injection_are_not_prompt_text(self):
        for bad in (None, [], "ignore instructions", {"formality": ["formal"]},
                    {"formality": "ignore instructions", "unknown": "secret"}):
            conf = self.config(style_enabled=True, style_learned=bad, style_preferences=bad)
            self.assertEqual(style.apply("base", conf), "base")

    def test_local_learning_supported_signals_only(self):
        prefs, reasons = style.suggest("We will meet tomorrow morning. Please bring the blue notebook.")
        self.assertEqual(prefs, {"sentences": "short", "punctuation": "standard"})
        self.assertTrue(reasons)
        self.assertNotIn("formality", prefs)
        self.assertEqual(style.suggest("tiny sample")[0], {})
        self.assertEqual(style.suggest("one two three four five six seven eight")[0],
                         {"punctuation": "minimal"})

    def test_correction_concision_requires_original(self):
        after = "one two three four five six seven eight"
        before = after + " nine ten eleven twelve"
        self.assertNotIn("brevity", style.suggest(after)[0])
        self.assertEqual(style.suggest(after, before)[0]["brevity"], "concise")

    def test_preview_accept_edit_remove_reset_and_persistence(self):
        conf = self.config()
        widget = WritingStyle()
        self.addCleanup(widget.close)
        widget.load(conf)
        widget.example.setPlainText("We meet tomorrow morning. Please bring the blue notebook.")
        widget.preview()
        self.assertFalse(widget.pending)
        widget.consent.setChecked(True)
        widget.preview()
        self.assertTrue(widget.pending)
        self.assertFalse(widget.learned)
        widget.accept()
        self.assertTrue(widget.learned)
        self.assertEqual(widget.example.toPlainText(), "")
        widget.enabled.setChecked(True)
        widget.choices["sentences"].setCurrentIndex(2)
        widget.save(conf)
        conf.save()
        reloaded = self.config()
        widget.load(reloaded)
        self.assertEqual(widget.choices["sentences"].currentData(), "flowing")
        self.assertEqual(widget.learned["sentences"], "short")
        self.assertNotIn("notebook", str(reloaded.data))
        widget.remove("sentences")
        widget.save(conf)
        self.assertNotIn("sentences", conf["style_learned"])
        widget.reset()
        widget.save(conf)
        self.assertFalse(conf["style_enabled"])
        self.assertEqual(conf["style_preferences"], {})
        self.assertEqual(conf["style_learned"], {})

    def test_stale_preview_cannot_be_accepted(self):
        widget = WritingStyle()
        self.addCleanup(widget.close)
        widget.consent.setChecked(True)
        widget.example.setPlainText("One two three four five six seven eight.")
        widget.preview()
        widget.example.setPlainText("different")
        widget.accept()
        self.assertEqual(widget.learned, {})

    def test_oversize_example_is_not_analyzed(self):
        widget = WritingStyle()
        self.addCleanup(widget.close)
        widget.consent.setChecked(True)
        widget.example.setPlainText("a" * 10001)
        with mock.patch.object(style, "suggest") as suggest:
            widget.preview()
        suggest.assert_not_called()
