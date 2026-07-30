#!/usr/bin/env python3
"""Tests for transcript cleanup across its two providers.

No key and no network: a local HTTP server stands in for both OpenRouter and
DeepSeek, and records what was posted to it. What is worth pinning down is the
payload — the two providers ask for thinking in different words, and DeepSeek's
default is the opposite of the one cleanup wants.

    python3 -m unittest test_cleanup -v
"""

import http.server
import json
import os
import threading
import unittest
import unittest.mock

import api
import config as cfg


class FakeChat:
    """Answers /chat/completions and keeps every request body it was sent."""

    def __init__(self, reply=None, status=200):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({
                    "path": self.path,
                    "payload": json.loads(body.decode("utf-8")),
                    "auth": self.headers.get("Authorization"),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                payload = reply if reply is not None else {
                    "choices": [{"message": {"content": " Temizlenmiş metin. "}}]
                }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def last(self):
        return self.requests[-1]


class Base(unittest.TestCase):
    def setUp(self):
        # The messages asserted on below are translated, and building a Config
        # anywhere in the run switches the language to the system's. Pin it.
        import i18n
        self.addCleanup(i18n.set_language, i18n.language())
        i18n.set_language("en")

    def serve(self, reply=None, status=200):
        fake = FakeChat(reply, status)
        self.addCleanup(fake.close)
        return fake

    def target(self, provider, url, model="m", reasoning="", key="k"):
        service = "DeepSeek" if provider == "deepseek" else "OpenRouter"
        return api.Target(provider, service, key, url, model, reasoning)


class Payload(Base):
    def test_the_transcript_is_wrapped_and_the_prompt_is_the_system_message(self):
        fake = self.serve()
        out = api.cleanup(self.target("openrouter", fake.url), "ham metin", "kurallar")
        self.assertEqual(out, "Temizlenmiş metin.")
        sent = fake.last()
        self.assertEqual(sent["path"], "/chat/completions")
        self.assertEqual(sent["payload"]["messages"][0],
                         {"role": "system", "content": "kurallar"})
        self.assertIn("<transcript>\nham metin\n</transcript>",
                      sent["payload"]["messages"][1]["content"])
        self.assertEqual(sent["payload"]["temperature"], 0)
        self.assertEqual(sent["auth"], "Bearer k")

    def test_the_model_id_is_the_target_s(self):
        fake = self.serve()
        api.cleanup(self.target("deepseek", fake.url, model="deepseek-v4-flash"),
                    "x", "y")
        self.assertEqual(fake.last()["payload"]["model"], "deepseek-v4-flash")

    def test_openrouter_gets_its_attribution_headers_and_deepseek_does_not(self):
        fake = self.serve()
        api.cleanup(self.target("openrouter", fake.url), "x", "y")
        self.assertIn("x-title", fake.last()["headers"])
        api.cleanup(self.target("deepseek", fake.url), "x", "y")
        self.assertNotIn("x-title", fake.last()["headers"])


class Thinking(Base):
    def test_an_empty_level_asks_for_nothing_either_way(self):
        fake = self.serve()
        api.cleanup(self.target("openrouter", fake.url, reasoning=""), "x", "y")
        self.assertNotIn("reasoning", fake.last()["payload"])
        api.cleanup(self.target("deepseek", fake.url, reasoning=""), "x", "y")
        self.assertNotIn("thinking", fake.last()["payload"])

    def test_openrouter_keeps_the_level_as_it_is_and_hides_the_thinking(self):
        fake = self.serve()
        api.cleanup(self.target("openrouter", fake.url, reasoning="medium"), "x", "y")
        self.assertEqual(fake.last()["payload"]["reasoning"],
                         {"effort": "medium", "exclude": True})
        self.assertNotIn("thinking", fake.last()["payload"])

    def test_off_turns_deepseek_s_thinking_off_rather_than_down(self):
        fake = self.serve()
        api.cleanup(self.target("deepseek", fake.url, reasoning="none"), "x", "y")
        self.assertEqual(fake.last()["payload"]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning", fake.last()["payload"])

    def test_deepseek_s_two_rungs_take_the_seven_levels(self):
        # The mapping DeepSeek documents for its own API: low and medium land
        # on high, xhigh lands on max.
        fake = self.serve()
        for level, expected in (("minimal", "high"), ("low", "high"),
                                ("medium", "high"), ("high", "high"),
                                ("xhigh", "max"), ("max", "max")):
            api.cleanup(self.target("deepseek", fake.url, reasoning=level), "x", "y")
            self.assertEqual(fake.last()["payload"]["thinking"],
                             {"type": "enabled", "reasoning_effort": expected},
                             level)

    def test_a_level_nobody_recognises_still_asks_for_thinking(self):
        fake = self.serve()
        api.cleanup(self.target("deepseek", fake.url, reasoning="enormous"), "x", "y")
        self.assertEqual(fake.last()["payload"]["thinking"],
                         {"type": "enabled", "reasoning_effort": "high"})


class Failures(Base):
    def test_a_missing_key_names_the_provider_that_wants_one(self):
        with self.assertRaises(api.ApiError) as caught:
            api.cleanup(self.target("deepseek", "http://x", key=""), "x", "y")
        self.assertIn("DeepSeek", str(caught.exception))

    def test_an_empty_reply_is_an_error_rather_than_an_empty_paste(self):
        fake = self.serve({"choices": [{"message": {"content": "  "}}]})
        with self.assertRaises(api.ApiError):
            api.cleanup(self.target("deepseek", fake.url), "x", "y")

    def test_a_reply_spent_entirely_on_thinking_says_so(self):
        # Measured against the real API: with thinking on, deepseek-v4-flash
        # sometimes returns nothing but reasoning_content. The fix is a
        # setting, so the message has to point at the setting.
        fake = self.serve({"choices": [{"message": {
            "content": "", "reasoning_content": "Önce ıı sesini silerim…"}}]})
        with self.assertRaises(api.ApiError) as caught:
            api.cleanup(self.target("deepseek", fake.url, reasoning=""), "x", "y")
        self.assertIn("Off", str(caught.exception))

    def test_an_http_error_arrives_named(self):
        fake = self.serve({"error": {"message": "nope"}}, status=401)
        with self.assertRaises(api.ApiError) as caught:
            api.cleanup(self.target("deepseek", fake.url), "x", "y")
        self.assertIn("DeepSeek", str(caught.exception))


class Targets(unittest.TestCase):
    """config picks the provider once; cleanup and the minutes both follow it."""

    def setUp(self):
        self.conf = cfg.Config()
        self.conf["openrouter_api_key"] = "or-key"
        self.conf["deepseek_api_key"] = "ds-key"

    def test_openrouter_is_the_default_and_carries_its_own_models(self):
        self.conf["cleanup_provider"] = "openrouter"
        cleanup = self.conf.cleanup_target()
        minutes = self.conf.minutes_target()
        self.assertEqual(cleanup.provider, "openrouter")
        self.assertEqual(cleanup.api_key, "or-key")
        self.assertEqual(cleanup.model, self.conf["cleanup_model"])
        self.assertEqual(minutes.model, self.conf["meeting_model"])

    def test_deepseek_swaps_both_jobs_over_together(self):
        self.conf["cleanup_provider"] = "deepseek"
        cleanup = self.conf.cleanup_target()
        minutes = self.conf.minutes_target()
        for target in (cleanup, minutes):
            self.assertEqual(target.provider, "deepseek")
            self.assertEqual(target.api_key, "ds-key")
            self.assertEqual(target.base_url, "https://api.deepseek.com")
        self.assertEqual(cleanup.model, "deepseek-v4-flash")

    def test_dictation_is_shipped_with_deepseek_s_thinking_off(self):
        # Not a preference: with thinking on, cleanup takes seconds instead of
        # about one, and can come back empty.
        self.conf["cleanup_provider"] = "deepseek"
        self.assertEqual(self.conf.cleanup_target().reasoning, "none")

    def test_minutes_are_left_to_think(self):
        self.conf["cleanup_provider"] = "deepseek"
        self.assertEqual(self.conf.minutes_target().reasoning, "")

    def test_the_key_falls_back_to_the_environment(self):
        self.conf["deepseek_api_key"] = ""
        with unittest.mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}):
            self.assertEqual(self.conf.deepseek_key(), "env-key")

    def test_transcription_targets_carry_no_thinking_level(self):
        self.assertEqual(self.conf.transcribe_target().reasoning, "")


if __name__ == "__main__":
    unittest.main()
