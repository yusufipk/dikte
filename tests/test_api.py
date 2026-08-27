"""The two providers, over a faked urllib.

Nothing here reaches the network. What is checked is the request that would have
gone out, because that is what a new provider changes and what an old one
notices: the URL, the headers, the fields of the multipart body, the JSON.

Stopping one is the exception. Cutting a request off is done to the socket it
is blocked on, and a faked urlopen has no socket to cut, so those tests talk to
a server of their own on the loopback interface.
"""

import http.server
import json
import os
import threading
import time
import unittest

from dikte import api
from dikte import ggml
from tests.support import (
    DikteTest,
    fake_urlopen,
    http_error,
    multipart_fields,
    raw_body,
    sent_json,
    url_error,
)

OPENAI = api.Target("openai", "OpenAI", "sk-test", api.OPENAI_URL, "gpt-4o-transcribe")
GROQ = api.Target("groq", "Groq", "gsk-test", api.GROQ_URL, "whisper-large-v3-turbo")
OPENROUTER = api.Target("openrouter", "OpenRouter", "sk-or-test",
                        api.OPENROUTER_URL, "openai/gpt-4o-transcribe")


class TimestampModel(unittest.TestCase):
    def test_only_whisper_returns_segment_times(self):
        self.assertEqual(api.timestamp_model("openai"), "whisper-1")

    def test_openrouter_namespaces_the_id(self):
        self.assertEqual(api.timestamp_model("openrouter"), "openai/whisper-1")

    def test_groq_keeps_the_model_that_was_chosen(self):
        """Every model it transcribes with is a whisper, so all of them do times."""
        self.assertEqual(api.timestamp_model("groq", "whisper-large-v3"),
                         "whisper-large-v3")

    def test_groq_with_nothing_chosen_falls_back(self):
        self.assertEqual(api.timestamp_model("groq"), "whisper-large-v3-turbo")

    def test_the_others_ignore_what_was_chosen(self):
        self.assertEqual(api.timestamp_model("openai", "gpt-4o-transcribe"),
                         "whisper-1")


class Explain(DikteTest):
    def error(self, status):
        return api.explain(api.ApiError("HTTP", status), "OpenAI")

    def test_a_rejected_key_points_at_the_settings(self):
        for status in (401, 403):
            with self.subTest(status=status):
                message = str(self.error(status))
                self.assertIn("OpenAI", message)
                self.assertIn("Settings", message)

    def test_no_credit(self):
        self.assertIn("credit", str(self.error(402)))

    def test_rate_limited(self):
        self.assertIn("rate limiting", str(self.error(429)))

    def test_anything_else_keeps_the_original_text(self):
        explained = api.explain(api.ApiError("something broke", 500), "OpenRouter")
        self.assertIn("something broke", str(explained))
        self.assertEqual(explained.status, 500)

    def test_the_status_is_carried_through(self):
        self.assertEqual(self.error(429).status, 429)

    def test_so_is_whether_it_is_worth_asking_again(self):
        self.assertTrue(self.error(502).retryable)
        self.assertFalse(self.error(401).retryable)


class Retryable(unittest.TestCase):
    """Which failures a second try can fix, and which will fail the same way."""

    def test_a_gateway_that_gave_up_waiting(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(api.ApiError("x", status).retryable)

    def test_a_request_that_was_wrong(self):
        for status in (400, 401, 402, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.assertFalse(api.ApiError("x", status).retryable)

    def test_an_error_of_our_own_is_not_the_network(self):
        self.assertFalse(api.ApiError("Transcript came back empty.").retryable)

    def test_a_connection_that_dropped_is_worth_a_second_try(self):
        with fake_urlopen(url_error("connection reset")):
            with self.assertRaises(api.ApiError) as caught:
                api._request("https://example.test", b"{}", {})
        self.assertTrue(caught.exception.retryable)


class ExtractError(unittest.TestCase):
    def test_the_usual_shape(self):
        body = json.dumps({"error": {"message": "invalid model"}})
        self.assertEqual(api._extract_error(body), "invalid model")

    def test_an_error_that_is_a_plain_string(self):
        self.assertEqual(api._extract_error(json.dumps({"error": "nope"})), "nope")

    def test_an_error_object_with_no_message(self):
        body = json.dumps({"error": {"code": 42}})
        self.assertIn("42", api._extract_error(body))

    def test_an_error_wrapped_in_an_array(self):
        """Google's 503 arrives this way, and .get() on a list raises."""
        body = json.dumps([{"error": {"code": 503,
                                      "message": "The model is overloaded."}}])
        self.assertEqual(api._extract_error(body), "The model is overloaded.")

    def test_a_body_that_is_not_json(self):
        self.assertEqual(api._extract_error("<html>502</html>"), "<html>502</html>")

    def test_no_shape_at_all_still_comes_back_as_a_string(self):
        """It runs while an ApiError is being raised: throwing here would
        escape the `except ApiError` holding the raw transcript."""
        for body in ("[]", "[1, 2]", '"a string"', "null", "17"):
            with self.subTest(body=body):
                self.assertIsInstance(api._extract_error(body), str)

    def test_a_wall_of_html_is_cut_short(self):
        self.assertEqual(len(api._extract_error("x" * 5000)), 300)


class Multipart(DikteTest):
    def setUp(self):
        super().setUp()
        self.wav = str(self.path("clip.wav"))
        os.makedirs(self.root, exist_ok=True)
        with open(self.wav, "wb") as fh:
            fh.write(b"RIFFfake")

    def build(self, fields):
        return api._multipart(fields, "file", self.wav)

    def test_the_boundary_is_declared_and_used(self):
        body, ctype = self.build([("model", "whisper-1")])
        boundary = ctype.split("boundary=")[1]
        self.assertTrue(ctype.startswith("multipart/form-data"))
        self.assertIn(boundary.encode(), body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_a_field_is_named_and_carries_its_value(self):
        body, _ = self.build([("model", "whisper-1")])
        self.assertIn(b'name="model"', body)
        self.assertIn(b"whisper-1", body)

    def test_empty_fields_are_left_out(self):
        body, _ = self.build([("model", "whisper-1"), ("language", ""),
                              ("prompt", None)])
        self.assertNotIn(b'name="language"', body)
        self.assertNotIn(b'name="prompt"', body)

    def test_the_file_goes_in_with_its_name_and_type(self):
        body, _ = self.build([])
        self.assertIn(b'filename="clip.wav"', body)
        self.assertIn(b"Content-Type: audio/x-wav", body)
        self.assertIn(b"RIFFfake", body)

    def test_a_boundary_is_not_reused_between_requests(self):
        first, _ = self.build([])
        second, _ = self.build([])
        self.assertNotEqual(first, second)


class Headers(unittest.TestCase):
    def test_the_key_is_a_bearer_token(self):
        self.assertEqual(api._headers("openai", "sk-test")["Authorization"],
                         "Bearer sk-test")

    def test_openai_gets_no_extras(self):
        self.assertNotIn("HTTP-Referer", api._headers("openai", "sk-test"))

    def test_openrouter_is_told_who_is_calling(self):
        headers = api._headers("openrouter", "sk-or-test")
        self.assertEqual(headers["HTTP-Referer"], api.APP_URL)
        self.assertEqual(headers["X-Title"], "Dikte")

    def test_a_content_type_is_added_when_there_is_a_body(self):
        headers = api._headers("openai", "k", "application/json")
        self.assertEqual(headers["Content-Type"], "application/json")


class Transcribe(DikteTest):
    def setUp(self):
        super().setUp()
        self.wav = str(self.path("clip.wav"))
        os.makedirs(self.root, exist_ok=True)
        with open(self.wav, "wb") as fh:
            fh.write(b"RIFFfake")

    def test_the_transcript_comes_back_stripped(self):
        with fake_urlopen({"text": "  hello there \n"}):
            self.assertEqual(api.transcribe(OPENAI, self.wav), "hello there")

    def test_it_goes_to_the_transcriptions_endpoint(self):
        with fake_urlopen({"text": "hi"}) as calls:
            api.transcribe(OPENAI, self.wav)
        self.assertEqual(calls[0].full_url,
                         "https://api.openai.com/v1/audio/transcriptions")

    def test_a_custom_base_url_is_honoured(self):
        target = OPENAI._replace(base_url="http://localhost:8080/v1/")
        with fake_urlopen({"text": "hi"}) as calls:
            api.transcribe(target, self.wav)
        self.assertEqual(calls[0].full_url,
                         "http://localhost:8080/v1/audio/transcriptions")

    def test_the_model_and_the_format_are_sent(self):
        with fake_urlopen({"text": "hi"}) as calls:
            api.transcribe(OPENAI, self.wav)
        fields = multipart_fields(calls[0])
        self.assertEqual(fields["model"], "gpt-4o-transcribe")
        self.assertEqual(fields["response_format"], "json")

    def test_a_language_is_sent_but_auto_is_not(self):
        with fake_urlopen({"text": "hi"}) as calls:
            api.transcribe(OPENAI, self.wav, language="tr")
            api.transcribe(OPENAI, self.wav, language="auto")
        self.assertEqual(multipart_fields(calls[0])["language"], "tr")
        self.assertNotIn("language", multipart_fields(calls[1]))

    def test_the_glossary_goes_everywhere_but_openrouter(self):
        """OpenRouter takes the field and throws it away, so spare it the bytes."""
        with fake_urlopen({"text": "hi"}) as calls:
            api.transcribe(OPENAI, self.wav, prompt="Paraşüt, OpenFrame")
            api.transcribe(GROQ, self.wav, prompt="Paraşüt, OpenFrame")
            api.transcribe(OPENROUTER, self.wav, prompt="Paraşüt, OpenFrame")
        self.assertIn("prompt", multipart_fields(calls[0]))
        self.assertIn("prompt", multipart_fields(calls[1]))
        self.assertNotIn("prompt", multipart_fields(calls[2]))

    def test_groq_goes_to_groq(self):
        with fake_urlopen({"text": "hi"}) as calls:
            api.transcribe(GROQ, self.wav)
        self.assertEqual(calls[0].full_url,
                         "https://api.groq.com/openai/v1/audio/transcriptions")
        self.assertEqual(multipart_fields(calls[0])["model"], "whisper-large-v3-turbo")

    def test_a_refused_groq_key_is_explained_in_groq_s_name(self):
        with fake_urlopen(http_error(401, '{"error": {"message": "bad key"}}')), \
                self.assertRaises(api.ApiError) as caught:
            api.transcribe(GROQ, self.wav)
        self.assertIn("Groq", str(caught.exception))

    def test_openrouter_is_attributed(self):
        with fake_urlopen({"text": "hi"}) as calls:
            api.transcribe(OPENROUTER, self.wav)
        self.assertEqual(calls[0].get_header("X-title"), "Dikte")

    def test_no_key_at_all(self):
        with self.assertRaises(api.ApiError) as caught:
            api.transcribe(OPENAI._replace(api_key=""), self.wav)
        self.assertIn("OpenAI", str(caught.exception))

    def test_an_empty_transcript_is_an_error(self):
        with fake_urlopen({"text": "   "}), self.assertRaises(api.ApiError):
            api.transcribe(OPENAI, self.wav)

    def test_a_rejected_key_is_explained_in_the_provider_s_name(self):
        with fake_urlopen(http_error(401, '{"error": {"message": "bad key"}}')), \
                self.assertRaises(api.ApiError) as caught:
            api.transcribe(OPENROUTER, self.wav)
        self.assertIn("OpenRouter", str(caught.exception))
        self.assertEqual(caught.exception.status, 401)

    def test_no_network(self):
        with fake_urlopen(url_error("name or service not known")), \
                self.assertRaises(api.ApiError) as caught:
            api.transcribe(OPENAI, self.wav)
        self.assertIn("connect", str(caught.exception))

    def test_a_reply_that_is_not_json(self):
        with fake_urlopen(raw_body("<html>bad gateway</html>")), \
                self.assertRaises(api.ApiError) as caught:
            api.transcribe(OPENAI, self.wav)
        self.assertIn("parse", str(caught.exception))


class TranscribeSegments(DikteTest):
    def setUp(self):
        super().setUp()
        self.wav = str(self.path("clip.wav"))
        os.makedirs(self.root, exist_ok=True)
        with open(self.wav, "wb") as fh:
            fh.write(b"RIFFfake")

    def reply(self, segments, text=""):
        return {"segments": segments, "text": text}

    def test_it_switches_to_the_model_that_has_timestamps(self):
        with fake_urlopen(self.reply([{"start": 0, "end": 1, "text": "hi"}])) as calls:
            api.transcribe_segments(OPENAI, self.wav)
        fields = multipart_fields(calls[0])
        self.assertEqual(fields["model"], "whisper-1")
        self.assertEqual(fields["response_format"], "verbose_json")
        self.assertEqual(fields["timestamp_granularities[]"], "segment")

    def test_openrouter_uses_the_namespaced_id(self):
        with fake_urlopen(self.reply([{"start": 0, "end": 1, "text": "hi"}])) as calls:
            api.transcribe_segments(OPENROUTER, self.wav)
        self.assertEqual(multipart_fields(calls[0])["model"], "openai/whisper-1")

    def test_groq_stays_on_the_model_it_was_given(self):
        target = GROQ._replace(model="whisper-large-v3")
        with fake_urlopen(self.reply([{"start": 0, "end": 1, "text": "hi"}])) as calls:
            api.transcribe_segments(target, self.wav)
        self.assertEqual(multipart_fields(calls[0])["model"], "whisper-large-v3")

    def test_the_segments_come_back_as_numbers(self):
        with fake_urlopen(self.reply([
            {"start": "0.5", "end": "2.25", "text": " hello "},
            {"start": 2.25, "end": 4.0, "text": "there"},
        ])):
            segments = api.transcribe_segments(OPENAI, self.wav)
        self.assertEqual(segments, [(0.5, 2.25, "hello"), (2.25, 4.0, "there")])

    def test_empty_segments_are_dropped(self):
        with fake_urlopen(self.reply([
            {"start": 0, "end": 1, "text": "  "},
            {"start": 1, "end": 2, "text": "real"},
        ])):
            self.assertEqual(api.transcribe_segments(OPENAI, self.wav),
                             [(1.0, 2.0, "real")])

    def test_an_end_before_its_start_is_pulled_forward(self):
        with fake_urlopen(self.reply([{"start": 5, "end": 1, "text": "hi"}])):
            self.assertEqual(api.transcribe_segments(OPENAI, self.wav),
                             [(5.0, 5.0, "hi")])

    def test_a_model_that_returned_no_segments_still_gives_its_text(self):
        with fake_urlopen(self.reply([], text="the whole thing")):
            self.assertEqual(api.transcribe_segments(OPENAI, self.wav),
                             [(0.0, 0.0, "the whole thing")])

    def test_nothing_at_all(self):
        with fake_urlopen(self.reply([], text="")), \
                self.assertRaises(api.ApiError):
            api.transcribe_segments(OPENAI, self.wav)


def chat_reply(content):
    return {"choices": [{"message": {"content": content}}]}


class Cleanup(DikteTest):
    def call(self, replies, **kwargs):
        with fake_urlopen(replies) as calls:
            result = api.cleanup("uh, hello", "sk-or-test", "some/model",
                                 "you clean up text", **kwargs)
        return result, calls

    def test_the_cleaned_text_comes_back(self):
        result, _ = self.call(chat_reply("  Hello.  "))
        self.assertEqual(result, "Hello.")

    def test_it_goes_to_chat_completions(self):
        _, calls = self.call(chat_reply("Hello."))
        self.assertEqual(calls[0].full_url,
                         "https://openrouter.ai/api/v1/chat/completions")

    def test_the_prompt_and_the_transcript_are_kept_apart(self):
        _, calls = self.call(chat_reply("Hello."))
        payload = sent_json(calls[0])
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][0]["content"], "you clean up text")
        self.assertIn("<transcript>", payload["messages"][1]["content"])
        self.assertIn("uh, hello", payload["messages"][1]["content"])

    def test_the_temperature_is_pinned(self):
        _, calls = self.call(chat_reply("Hello."))
        self.assertEqual(sent_json(calls[0])["temperature"], 0)

    def test_no_effort_asked_for_means_no_reasoning_block(self):
        _, calls = self.call(chat_reply("Hello."))
        self.assertNotIn("reasoning", sent_json(calls[0]))

    def test_an_effort_is_passed_on_and_the_thinking_left_out(self):
        _, calls = self.call(chat_reply("Hello."), reasoning="high")
        self.assertEqual(sent_json(calls[0])["reasoning"],
                         {"effort": "high", "exclude": True})

    def test_gemini_takes_openai_s_flat_field_rather_than_the_object(self):
        _, calls = self.call(chat_reply("Hello."), reasoning="low",
                             provider="gemini", service="Google AI Studio")
        payload = sent_json(calls[0])
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("reasoning", payload)

    def test_off_is_asked_for_as_the_lowest_rung_google_actually_has(self):
        """Sending "none" is a 400, and Flash left alone thinks."""
        _, calls = self.call(chat_reply("Hello."), reasoning="none",
                             provider="gemini", service="Google AI Studio")
        self.assertEqual(sent_json(calls[0])["reasoning_effort"], "minimal")

    def test_a_rung_google_does_not_have_lands_on_the_nearest_one(self):
        for asked in ("xhigh", "max"):
            with self.subTest(asked=asked):
                _, calls = self.call(chat_reply("Hello."), reasoning=asked,
                                     provider="gemini", service="Google AI Studio")
                self.assertEqual(sent_json(calls[0])["reasoning_effort"], "high")

    def test_gemini_left_on_the_model_s_own_default_is_told_nothing(self):
        _, calls = self.call(chat_reply("Hello."), provider="gemini",
                             service="Google AI Studio")
        self.assertNotIn("reasoning_effort", sent_json(calls[0]))

    def test_a_missing_gemini_key_says_google_ai_studio(self):
        with self.assertRaises(api.ApiError) as caught:
            api.cleanup("hello", "", "gemini-3.5-flash-lite", "prompt",
                        provider="gemini", service="Google AI Studio")
        self.assertIn("Google AI Studio", str(caught.exception))

    def test_a_local_base_url(self):
        _, calls = self.call(chat_reply("Hello."), base_url="http://localhost:1234/v1")
        self.assertEqual(calls[0].full_url, "http://localhost:1234/v1/chat/completions")

    def test_no_key(self):
        with self.assertRaises(api.ApiError):
            api.cleanup("hello", "", "some/model", "prompt")

    def test_a_reply_with_no_choices_says_why(self):
        with fake_urlopen({"error": {"message": "model is offline"}}), \
                self.assertRaises(api.ApiError) as caught:
            api.cleanup("hello", "k", "m", "p")
        self.assertIn("model is offline", str(caught.exception))

    def test_an_empty_answer(self):
        with fake_urlopen(chat_reply("   ")), self.assertRaises(api.ApiError):
            api.cleanup("hello", "k", "m", "p")

    def test_a_rate_limit_is_explained(self):
        with fake_urlopen(http_error(429)), \
                self.assertRaises(api.ApiError) as caught:
            api.cleanup("hello", "k", "m", "p")
        self.assertIn("OpenRouter", str(caught.exception))


class Chat(DikteTest):
    def test_the_history_is_sent_after_the_system_prompt(self):
        history = [{"role": "user", "content": "book it"},
                   {"role": "assistant", "content": "done"}]
        with fake_urlopen(chat_reply("moved it")) as calls:
            api.chat(history + [{"role": "user", "content": "move it"}],
                     "k", "some/model", "you are an agent")
        payload = sent_json(calls[0])
        self.assertEqual(payload["messages"][0],
                         {"role": "system", "content": "you are an agent"})
        self.assertEqual(payload["messages"][1:], history +
                         [{"role": "user", "content": "move it"}])

    def test_no_temperature_is_forced_on_a_conversation(self):
        with fake_urlopen(chat_reply("hi")) as calls:
            api.chat([{"role": "user", "content": "hi"}], "k", "m", "p")
        self.assertNotIn("temperature", sent_json(calls[0]))

    def test_no_key(self):
        with self.assertRaises(api.ApiError):
            api.chat([], "", "m", "p")

    def test_an_empty_answer(self):
        with fake_urlopen(chat_reply("")), self.assertRaises(api.ApiError):
            api.chat([{"role": "user", "content": "hi"}], "k", "m", "p")


class KeyStatus(DikteTest):
    def test_a_key_with_no_limit(self):
        with fake_urlopen({"data": {"limit": None, "usage": 3}}):
            self.assertIn("no spending limit",
                          api.openrouter_key_status("sk-or-test"))

    def test_a_key_with_a_limit_reports_both_numbers(self):
        with fake_urlopen({"data": {"limit": 10, "usage": 2.5}}):
            message = api.openrouter_key_status("sk-or-test")
        self.assertIn("2.5", message)
        self.assertIn("10", message)

    def test_no_key(self):
        with self.assertRaises(api.ApiError):
            api.openrouter_key_status("")

    def test_a_key_the_service_rejects(self):
        with fake_urlopen(http_error(401)), \
                self.assertRaises(api.ApiError) as caught:
            api.openrouter_key_status("sk-or-bad")
        self.assertEqual(caught.exception.status, 401)


class ModelLists(DikteTest):
    def test_openrouter_returns_sorted_ids(self):
        with fake_urlopen({"data": [{"id": "z/model"}, {"id": "a/model"}]}):
            self.assertEqual(api.openrouter_models(), ["a/model", "z/model"])

    def test_the_model_list_needs_no_key(self):
        with fake_urlopen({"data": []}) as calls:
            api.openrouter_models()
        self.assertIsNone(calls[0].get_header("Authorization"))

    def test_a_key_is_sent_when_there_is_one(self):
        with fake_urlopen({"data": []}) as calls:
            api.openrouter_models("sk-or-test")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer sk-or-test")

    def test_speech_models_are_asked_for_and_filtered_again(self):
        """A query parameter the API stops honouring must not leak the lot."""
        with fake_urlopen({"data": [
            {"id": "openai/whisper-1",
             "architecture": {"output_modalities": ["transcription"]}},
            {"id": "google/gemini-3.5-flash",
             "architecture": {"output_modalities": ["text"]}},
            {"id": "broken/model"},
        ]}) as calls:
            models = api.openrouter_models(transcription=True)
        self.assertIn("output_modalities=transcription", calls[0].full_url)
        self.assertEqual(models, ["openai/whisper-1"])

    def test_openai_narrows_to_the_audio_models(self):
        with fake_urlopen({"data": [{"id": "gpt-4o"}, {"id": "whisper-1"},
                                    {"id": "gpt-4o-transcribe"}]}):
            self.assertEqual(api.openai_models("sk-test"),
                             ["gpt-4o-transcribe", "whisper-1"])

    def test_a_list_with_no_audio_models_is_shown_whole(self):
        with fake_urlopen({"data": [{"id": "gpt-4o"}, {"id": "o3"}]}):
            self.assertEqual(api.openai_models("sk-test"), ["gpt-4o", "o3"])

    def test_openai_needs_a_key(self):
        with self.assertRaises(api.ApiError):
            api.openai_models("")

    def test_the_same_list_read_from_groq(self):
        with fake_urlopen({"data": [{"id": "llama-3.3-70b"},
                                    {"id": "whisper-large-v3"}]}) as calls:
            models = api.openai_models("gsk-test", api.GROQ_URL, "Groq")
        self.assertEqual(calls[0].full_url, "https://api.groq.com/openai/v1/models")
        self.assertEqual(models, ["whisper-large-v3"])

    def test_a_missing_groq_key_says_groq(self):
        with self.assertRaises(api.ApiError) as caught:
            api.openai_models("", api.GROQ_URL, "Groq")
        self.assertIn("Groq", str(caught.exception))

    def test_gemini_keeps_only_the_models_that_answer_a_chat_request(self):
        with fake_urlopen({"data": [{"id": "gemini-3.5-flash"},
                                    {"id": "text-embedding-004"},
                                    {"id": "imagen-4.0"},
                                    {"id": "gemini-2.5-flash-lite"}]}) as calls:
            models = api.gemini_models("AIza-test")
        self.assertEqual(calls[0].full_url,
                         "https://generativelanguage.googleapis.com/v1beta/openai/models")
        self.assertEqual(models, ["gemini-2.5-flash-lite", "gemini-3.5-flash"])

    def test_the_long_form_of_an_id_is_shortened_to_what_a_request_wants(self):
        with fake_urlopen({"data": [{"id": "models/gemini-3.5-flash-lite"}]}):
            self.assertEqual(api.gemini_models("AIza-test"),
                             ["gemini-3.5-flash-lite"])

    def test_a_gemini_id_that_is_not_a_chat_model_is_left_out(self):
        """Google names its pictures and its voices `gemini` too."""
        with fake_urlopen({"data": [{"id": "gemini-3.5-flash"},
                                    {"id": "gemini-embedding-001"},
                                    {"id": "gemini-2.5-flash-image"},
                                    {"id": "gemini-2.5-flash-preview-tts"},
                                    {"id": "gemini-2.5-native-audio"}]}):
            self.assertEqual(api.gemini_models("AIza-test"), ["gemini-3.5-flash"])

    def test_gemini_sends_the_key_as_a_bearer_token(self):
        with fake_urlopen({"data": []}) as calls:
            api.gemini_models("AIza-test")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer AIza-test")

    def test_a_missing_gemini_key_says_google_ai_studio(self):
        with self.assertRaises(api.ApiError) as caught:
            api.gemini_models("")
        self.assertIn("Google AI Studio", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class FakeServer:
    """A ggml.Server as far as api.py is concerned."""

    def __init__(self, url="http://127.0.0.1:9999/v1", fails="", log=""):
        self.url = url
        self.fails = fails
        self.log = log
        self.starts = 0

    def serve(self):
        self.starts += 1
        if self.fails:
            raise ggml.LocalError(self.fails)
        return self.url

    def error(self):
        return self.log


LOCAL = api.Target("local", "Local whisper", "", "", "ggml-base.bin")


class TranscribeHere(DikteTest):
    def setUp(self):
        super().setUp()
        self.wav = str(self.path("clip.wav"))
        os.makedirs(self.root, exist_ok=True)
        with open(self.wav, "wb") as fh:
            fh.write(b"RIFFfake")
        self.server = FakeServer()
        self.patch_attr(ggml, "whisper", self.server)

    def test_the_address_comes_from_the_server_it_starts(self):
        with fake_urlopen({"text": "hello"}) as calls:
            api.transcribe(LOCAL, self.wav)
        self.assertEqual(self.server.starts, 1)
        self.assertEqual(calls[0].full_url,
                         "http://127.0.0.1:9999/v1/audio/transcriptions")

    def test_nothing_local_is_authorised(self):
        with fake_urlopen({"text": "hello"}) as calls:
            api.transcribe(LOCAL, self.wav)
        self.assertNotIn("Authorization", calls[0].headers)

    def test_a_server_that_will_not_start_is_the_error_shown(self):
        self.patch_attr(ggml, "whisper", FakeServer(fails="no model downloaded"))
        with self.assertRaises(api.ApiError) as caught:
            api.transcribe(LOCAL, self.wav)
        self.assertIn("no model downloaded", str(caught.exception))

    def test_a_server_that_dies_mid_request_says_what_it_printed(self):
        self.patch_attr(ggml, "whisper", FakeServer(log="out of memory"))
        with fake_urlopen(url_error("connection reset")):
            with self.assertRaises(api.ApiError) as caught:
                api.transcribe(LOCAL, self.wav)
        self.assertIn("out of memory", str(caught.exception))

    def test_the_hint_reaches_whisper_as_its_initial_prompt(self):
        with fake_urlopen({"text": "hi"}) as calls:
            api.transcribe(LOCAL, self.wav, prompt="Dikte, Paraşüt")
        self.assertEqual(multipart_fields(calls[0])["prompt"], "Dikte, Paraşüt")

    def test_a_word_broken_over_two_lines_is_put_back_together(self):
        # whisper.cpp cuts on tokens and writes one segment per line, which in
        # Turkish lands inside a word about as often as between two.
        with fake_urlopen({"text": "Onlar akraba değ\niller. Ve\n devamı."}):
            # The line break inside a word leaves nothing in its place; the
            # one between two words is where whisper's own leading space is.
            self.assertEqual(api.transcribe(LOCAL, self.wav),
                             "Onlar akraba değiller. Ve devamı.")

    def test_a_local_timeout_is_not_a_hosted_one(self):
        # Nothing is being spent but time, and a long file on a machine without
        # a graphics card takes a good deal of it.
        with fake_urlopen({"text": "hi"}):
            api.transcribe(LOCAL, self.wav, timeout=300)
        self.assertGreaterEqual(api.LOCAL_TIMEOUT, 600)

    def test_segments_that_continue_a_word_are_merged(self):
        reply = {"segments": [
            {"start": 0.0, "end": 1.0, "text": " Onlar akraba değ"},
            {"start": 1.0, "end": 1.4, "text": "iller."},
            {"start": 2.0, "end": 3.0, "text": " Başka bir cümle."},
        ]}
        with fake_urlopen(reply):
            out = api.transcribe_segments(LOCAL, self.wav)
        self.assertEqual([text for _, _, text in out],
                         ["Onlar akraba değiller.", "Başka bir cümle."])
        self.assertEqual(out[0][1], 1.4)     # the merged cue covers the whole word

    def test_the_loaded_model_is_the_one_asked_for_again(self):
        with fake_urlopen({"segments": [{"start": 0, "end": 1, "text": " hi"}]}) as calls:
            api.transcribe_segments(LOCAL, self.wav)
        self.assertEqual(multipart_fields(calls[0])["model"], "ggml-base.bin")

    # ---- the detected language --------------------------------------------

    def test_auto_mode_asks_whisper_for_the_detected_language(self):
        # The -nlp the server was started with is switched back on for this one
        # request, so whisper's verbose_json reports what it heard.
        reply = {"text": " Merhaba dünya. ", "detected_language": "turkish"}
        with fake_urlopen(reply) as calls:
            text, code = api.transcribe_detected(LOCAL, self.wav, language="auto")
        fields = multipart_fields(calls[0])
        self.assertEqual(fields["response_format"], "verbose_json")
        self.assertEqual(fields["no_language_probabilities"], "false")
        self.assertNotIn("language", fields)
        self.assertEqual(text, "Merhaba dünya.")
        self.assertEqual(code, "tr")

    def test_a_fixed_language_reports_no_detection(self):
        with fake_urlopen({"text": "hello"}) as calls:
            text, code = api.transcribe_detected(LOCAL, self.wav, language="tr")
        self.assertNotIn("no_language_probabilities", multipart_fields(calls[0]))
        self.assertEqual(text, "hello")
        self.assertEqual(code, "")

    def test_a_detected_language_without_a_code_stays_unknown(self):
        with fake_urlopen({"text": "hello", "detected_language": "somali"}):
            _text, code = api.transcribe_detected(LOCAL, self.wav, language="auto")
        self.assertEqual(code, "")

    def test_a_hosted_auto_run_transcribes_without_detection(self):
        with fake_urlopen({"text": "hi"}) as calls:
            text, code = api.transcribe_detected(OPENAI, self.wav, language="auto")
        self.assertNotIn("no_language_probabilities", multipart_fields(calls[0]))
        self.assertEqual(text, "hi")
        self.assertEqual(code, "")


class Stopping(unittest.TestCase):
    """The Stop button, from the far end: a request already blocked on a reply.

    The one that matters is a whisper on this machine, which answers minutes
    after it was asked, so it is a real socket here rather than a fake urlopen.
    Nothing leaves the loopback interface.
    """

    def setUp(self):
        answering = threading.Event()

        class Slow(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                answering.set()
                time.sleep(30)          # the model, thinking

            def log_message(self, *args):
                pass

        self.answering = answering
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/x"

    def post(self, aborter, out):
        try:
            api._request(self.url, b"{}", {}, timeout=30, aborter=aborter)
            out.append("answered")
        except BaseException as exc:      # noqa: BLE001 - the type is the result
            out.append(type(exc).__name__)

    def test_a_request_waiting_on_a_reply_is_cut_off(self):
        aborter, out = api.Aborter(), []
        thread = threading.Thread(target=self.post, args=(aborter, out))
        thread.start()
        self.assertTrue(self.answering.wait(10))
        aborter.abort()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(out, ["Aborted"])

    def test_a_request_that_starts_after_the_stop_never_goes_out(self):
        aborter, out = api.Aborter(), []
        aborter.abort()
        self.post(aborter, out)
        self.assertEqual(out, ["Aborted"])
        self.assertFalse(self.answering.is_set())

    def test_without_one_the_request_is_the_plain_urllib_one(self):
        """Everything that is not stoppable keeps the opener it always had."""
        with fake_urlopen({"text": "hi"}) as calls:
            api._request(self.url, b"{}", {})
        self.assertEqual(len(calls), 1)


class Sockets(unittest.TestCase):
    """The few lines urllib takes between making a connection and blocking on
    it. A stop that lands in there must not leave the request waiting out its
    hour-long local timeout."""

    class FakeConn:
        auto_open = 1
        sock = None
        closed = False

        def close(self):
            self.closed = True

    def test_a_connection_opened_after_the_stop_is_refused(self):
        sockets = api._Sockets()
        sockets.cut()
        with self.assertRaises(api.Aborted):
            sockets.add(self.FakeConn())

    def test_one_that_is_already_open_is_closed_where_it_stands(self):
        sockets, conn = api._Sockets(), self.FakeConn()
        sockets.add(conn)
        sockets.cut()
        self.assertTrue(conn.closed)

    def test_one_with_no_socket_yet_is_stopped_from_making_another(self):
        """close() leaves auto_open on, and the next line would reconnect."""
        sockets, conn = api._Sockets(), self.FakeConn()
        sockets.add(conn)
        sockets.cut()
        self.assertEqual(conn.auto_open, 0)


class Aborter(unittest.TestCase):
    def test_what_was_registered_is_run_once_the_stop_lands(self):
        aborter, cut = api.Aborter(), []
        with aborter.holding(lambda: cut.append(True)):
            aborter.abort()
        self.assertEqual(cut, [True])

    def test_a_block_that_ended_is_not_cut_afterwards(self):
        aborter, cut = api.Aborter(), []
        with aborter.holding(lambda: cut.append(True)):
            pass
        aborter.abort()
        self.assertEqual(cut, [])

    def test_a_stop_that_already_landed_stops_the_next_step_too(self):
        aborter = api.Aborter()
        aborter.abort()
        with self.assertRaises(api.Aborted):
            aborter.check()
        with self.assertRaises(api.Aborted):
            with aborter.holding(lambda: None):
                pass
