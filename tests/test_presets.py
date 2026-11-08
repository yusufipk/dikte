"""Gateway aliases retain stored settings and their wire protocols."""

import json
import os
from unittest import mock

from dikte import api, assistant, cleanup, cli, config as cfg
from tests.support import DikteTest, fake_urlopen, sent_json, make_wav, speech
from tests.test_api import GROQ, REQUESTY, GEMINI, multipart_fields
from tests.test_cli import captured


class Migration(DikteTest):
    def test_load_and_save_migrate_every_supported_legacy_role(self):
        for role in ("transcribe", "cleanup", "assistant"):
            for name in cfg.ALIASES:
                if role == "transcribe" and not cfg.PRESETS[name].transcription:
                    continue
                with self.subTest(role=role, preset=name):
                    setting = cfg.model_setting(role, name)
                    raw = {f"{role}_provider": name, setting: "chosen-model",
                           cfg.PRESETS[name].key: "stored-key"}
                    self.write_config(raw)
                    conf = cfg.Config()
                    self.assertEqual(conf[f"{role}_provider"], "custom")
                    self.assertEqual(conf[f"{role}_custom_preset"], name)
                    target = conf.custom_target(role)
                    self.assertEqual((target.model, target.api_key), ("chosen-model", "stored-key"))
                    conf.save()
                    reloaded = cfg.Config()
                    self.assertEqual(reloaded.custom_target(role), target)

    def test_config_set_accepts_every_legacy_alias(self):
        for role in ("transcribe", "cleanup", "assistant"):
            for name in cfg.ALIASES:
                with self.subTest(role=role, preset=name):
                    opts = cli.build_parser().parse_args(["config", "set", f"{role}_provider", name])
                    unsupported = role == "transcribe" and not cfg.PRESETS[name].transcription
                    before = cfg.Config().data.copy()
                    with captured() as (out, err), mock.patch.object(cli.ipc, "send") as reload:
                        self.assertEqual(opts.func(opts), 2 if unsupported else 0)
                    if unsupported:
                        self.assertIn("does not support transcription", err.getvalue())
                        self.assertEqual(cfg.Config().data, before)
                        reload.assert_not_called()
                        continue
                    conf = cfg.Config()
                    self.assertEqual(conf[f"{role}_provider"], "custom")
                    self.assertEqual(conf[f"{role}_custom_preset"], name)

    def test_environment_keys_and_legacy_endpoint_overrides_survive(self):
        for name in cfg.ALIASES:
            with self.subTest(preset=name):
                conf = self.config(cleanup_provider=name)
                who = cfg.PRESETS[name]
                conf[f"{name}_base_url"] = "https://example.invalid/v1"
                with mock.patch.dict(os.environ, {who.key.upper(): "env-key"}):
                    target = conf.custom_target("cleanup")
                self.assertEqual(target.api_key, "env-key")
                self.assertEqual(target.base_url, "https://example.invalid/v1")

    def test_ask_provider_aliases_use_custom_and_keep_model_overrides(self):
        for name in cfg.ALIASES:
            with self.subTest(preset=name):
                opts = cli.build_parser().parse_args(["ask", "hello", "--provider", name,
                                                       "--model", "chosen-model"])
                with mock.patch.object(assistant, "ask", return_value=("answer", "")) as ask, captured():
                    self.assertEqual(opts.func(opts), 0)
                conf = ask.call_args.args[1]
                self.assertEqual(assistant.provider(conf), "custom")
                self.assertEqual(conf.custom_target("assistant").preset, name)
                self.assertEqual(assistant.model(conf), "chosen-model")

    def test_test_key_accepts_every_alias_without_changing_saved_selection(self):
        for name in cfg.ALIASES:
            with self.subTest(preset=name):
                self.write_config({"cleanup_provider": "openrouter"})
                opts = cli.build_parser().parse_args(["test-key", name])
                with mock.patch.object(api, "preset_models", return_value=["model"]) as fetch, captured():
                    self.assertEqual(opts.func(opts), 0)
                self.assertEqual(fetch.call_args.args[0], name)
                self.assertEqual(cfg.Config()["cleanup_provider"], "openrouter")

    def test_custom_cli_catalogs_always_target_other(self):
        self.write_config({"cleanup_provider": "requesty", "transcribe_provider": "groq",
                           "custom_api_key": "own-key", "custom_base_url": "https://example.invalid/v1"})
        for args in (["models", "--provider", "custom"],
                     ["models", "--provider", "custom", "--transcription"],
                     ["test-key", "custom"]):
            with self.subTest(args=args):
                opts = cli.build_parser().parse_args(args)
                with captured(), mock.patch.object(api, "preset_models", return_value=["model"]) as fetch:
                    self.assertEqual(opts.func(opts), 0)
                self.assertEqual(fetch.call_args.args[:3],
                                 ("other", "own-key", "https://example.invalid/v1"))
        self.assertEqual(cfg.Config().custom_preset("cleanup"), "requesty")

    def test_test_key_all_checks_each_gateway_once_and_skips_unconfigured_other(self):
        self.write_config({"cleanup_provider": "groq", "groq_api_key": "gateway-key"})
        opts = cli.build_parser().parse_args(["test-key", "all"])
        with captured(), mock.patch.object(cli, "_catalog", return_value=["model"]) as fetch, \
                mock.patch.object(api, "openrouter_key_status", return_value="works"):
            self.assertEqual(opts.func(opts), 0)
        names = [call.args[1] for call in fetch.call_args_list]
        self.assertNotIn("custom", names)
        self.assertEqual(names.count("groq"), 1)

    def test_session_status_matches_chat_resumption(self):
        for name in ("other", "requesty", "opencode"):
            conf = self.config(assistant_provider="custom", assistant_custom_preset=name,
                               custom_base_url="http://localhost:8000/v1")
            conf.save()
            for owner in (assistant.chat_session(conf), "requesty", "opencode",
                          "custom:other:another-endpoint"):
                with self.subTest(preset=name, owner=owner):
                    assistant.write_session(owner, messages=[{"role": "user", "content": "earlier"}])
                    opts = cli.build_parser().parse_args(["session"])
                    with captured() as (out, _):
                        opts.func(opts)
                    resumes = owner == assistant.chat_session(conf) or owner == name and name in ("requesty", "opencode")
                    self.assertEqual("last used" in out.getvalue(), resumes)
                    with mock.patch.object(api, "chat", return_value="answer") as chat:
                        assistant.ask("next", conf)
                    messages = chat.call_args.args[0]
                    self.assertEqual(any(m["content"] == "earlier" for m in messages), resumes)

    def test_profiles_do_not_store_provider_selection(self):
        conf = self.config(profiles=[{"name": "Chat", "apps": ["chat"],
                                     "style": {"formality": "casual"}}])
        before = json.loads(json.dumps(conf["profiles"]))
        conf["cleanup_provider"] = "requesty"
        conf.save()
        self.assertEqual(cfg.Config()["profiles"], before)

    def test_named_provider_lists_contain_no_folded_gateway(self):
        for names in (cfg.TRANSCRIBERS, cleanup.PROVIDERS, assistant.PROVIDERS):
            self.assertTrue(set(cfg.ALIASES).isdisjoint(names))


class WireCompatibility(DikteTest):
    def test_cleanup_payload_and_headers_match_legacy_providers_at_every_effort(self):
        for name in cfg.ALIASES:
            for effort in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
                with self.subTest(preset=name, effort=effort):
                    who = cfg.PRESETS[name]
                    with fake_urlopen({"choices": [{"message": {"content": "Done."}}]}) as calls:
                        api.cleanup("text", "test-key", "model", "rules", reasoning=effort,
                                    base_url=who.base_url, provider=name, service=who.service)
                        api.cleanup("text", "test-key", "model", "rules", reasoning=effort,
                                    base_url=who.base_url, provider="custom", preset=name, service=who.service)
                    self.assertEqual(calls[0].full_url, calls[1].full_url)
                    self.assertEqual(sent_json(calls[0]), sent_json(calls[1]))
                    self.assertEqual(dict(calls[0].header_items()), dict(calls[1].header_items()))

    def test_transcription_timing_follows_the_preset(self):
        for target in (GROQ, REQUESTY, GEMINI):
            with self.subTest(preset=target.provider):
                custom = target._replace(provider="custom", preset=target.provider)
                self.assertEqual(api.gives_times(target.provider), api.gives_times("custom", custom.preset))
                self.assertEqual(api.timestamp_model(target.provider, target.model),
                                 api.timestamp_model("custom", custom.model, preset=custom.preset))
                with fake_urlopen({"text": "hi", "choices": [{"message": {"content": "hi"}}]}) as calls:
                    api.transcribe(custom, make_wav(self.path("clip.wav"), speech(0.2)))
                if custom.preset == "gemini":
                    self.assertTrue(calls[0].full_url.endswith("/chat/completions"))
                    with self.assertRaises(api.ApiError):
                        api.transcribe_segments(custom, make_wav(self.path("clip.wav"), speech(0.2)))
                else:
                    self.assertTrue(calls[0].full_url.endswith("/audio/transcriptions"))
                    self.assertEqual(multipart_fields(calls[0])["model"], custom.model)
                self.assertEqual(calls[0].get_header("Http-referer"),
                                 api.APP_URL if custom.preset == "requesty" else None)

    def test_catalog_dispatch_preserves_each_legacy_fetcher(self):
        for name, who in cfg.PRESETS.items():
            fetcher = {"managed": "requesty_models", "gemini": "gemini_models",
                       "custom": "custom_models", "audio": "custom_models"}.get(who.catalog, "openai_models")
            with self.subTest(preset=name), mock.patch.object(api, fetcher, return_value=["model"]) as fetch:
                self.assertEqual(api.preset_models(name, "key", "https://example.invalid/v1"), ["model"])
                self.assertEqual(fetch.call_args.args[:2], ("key", "https://example.invalid/v1"))

    def test_legacy_audio_catalog_and_key_checks_keep_their_filter_and_auth(self):
        conf = self.config(groq_api_key="key", requesty_api_key="key")
        catalog = {"data": [{"id": "whisper-1"}, {"id": "chat-model"}]}
        with fake_urlopen(catalog) as calls:
            self.assertEqual(cli._catalog(conf, "groq"), ["whisper-1"])
            self.assertEqual(cli._catalog(conf, "requesty", test_key=True), ["whisper-1"])
        self.assertEqual(len(calls), 2)
        conf["requesty_api_key"] = ""
        with mock.patch.dict(os.environ, {"REQUESTY_API_KEY": ""}), self.assertRaises(api.ApiError):
            cli._catalog(conf, "requesty", test_key=True)

    def test_agent_other_accepts_no_key_or_model_and_validates_the_url(self):
        conf = self.config(assistant_provider="custom", custom_base_url="http://localhost:8000/v1")
        with fake_urlopen({"choices": [{"message": {"content": "answer"}}]}) as calls:
            self.assertEqual(assistant.ask("hello", conf), ("answer", ""))
        self.assertNotIn("model", sent_json(calls[0]))
        self.assertIsNone(calls[0].get_header("Authorization"))
        conf["custom_base_url"] = "file:///etc/passwd"
        with self.assertRaises(assistant.AssistantError):
            assistant.ask("hello", conf)

    def test_agent_chat_presets_route_to_their_key_model_and_endpoint(self):
        for name, who in cfg.PRESETS.items():
            with self.subTest(preset=name):
                conf = self.config(assistant_provider="custom", assistant_custom_preset=name,
                                   custom_base_url="http://localhost:8000/v1")
                conf[who.key] = "test-key"
                conf[cfg.model_setting("assistant", name)] = "chosen-model"
                with fake_urlopen({"choices": [{"message": {"content": "answer"}}]}) as calls:
                    assistant.ask("hello", conf)
                self.assertEqual(sent_json(calls[0])["model"], "chosen-model")
                self.assertEqual(calls[0].get_header("Authorization"), "Bearer test-key")
                self.assertEqual(calls[0].full_url, conf.custom_target("assistant").base_url + "/chat/completions")
                self.assertFalse(assistant.uses_tools(conf))
                self.assertTrue(assistant.can_run_tool_free(conf))

    def test_legacy_chat_session_is_carried_over_once(self):
        conf = self.config(assistant_provider="requesty", requesty_api_key="test-key")
        assistant.write_session("requesty", messages=[{"role": "user", "content": "earlier"}])
        with fake_urlopen({"choices": [{"message": {"content": "answer"}}]}) as calls:
            assistant.ask("next", conf)
        self.assertEqual(sent_json(calls[0])["messages"][1]["content"], "earlier")
        self.assertEqual(assistant.stored_provider(conf), "custom")

    def test_router_chat_preserves_explicit_reasoning_off(self):
        for provider, preset in (("openrouter", ""), ("custom", "opencode")):
            with self.subTest(provider=provider), fake_urlopen({"choices": [{"message": {"content": "answer"}}]}) as calls:
                api.chat([], "key", "model", "rules", reasoning="none", provider=provider,
                         preset=preset, base_url="https://example.invalid/v1")
            self.assertEqual(sent_json(calls[0])["reasoning"], {"effort": "none", "exclude": True})

    def test_a_gateway_without_a_model_is_refused_before_sending(self):
        # Groq has no chat default; an empty name would only come back as a 400.
        conf = self.config(cleanup_provider="custom", cleanup_custom_preset="groq",
                           assistant_provider="custom", assistant_custom_preset="groq",
                           groq_api_key="key")
        with fake_urlopen({"choices": [{"message": {"content": "x"}}]}) as calls:
            with self.assertRaisesRegex(api.ApiError, "Groq"):
                cleanup.run("merhaba", conf, "rules")
            with self.assertRaisesRegex(assistant.AssistantError, "Groq"):
                assistant.ask("hello", conf, keep_session=False)
        self.assertEqual(calls, [])

    def test_every_chat_preset_except_groq_has_a_default_model(self):
        for role in ("cleanup", "assistant"):
            for name in cfg.ALIASES:
                if name == "groq" or not cfg.PRESETS[name].chat:
                    continue
                with self.subTest(role=role, preset=name):
                    self.assertTrue(cfg.DEFAULTS[cfg.model_setting(role, name)])

    def test_a_foreign_custom_session_is_reported_without_its_hash(self):
        conf = self.config(assistant_provider="claude")
        assistant.write_session("custom:groq:" + "a" * 64, messages=[{"role": "user", "content": "x"}])
        self.assertEqual(assistant.stored_provider(conf), "custom:groq")

    def test_config_set_refuses_a_chat_only_transcription_preset(self):
        for key in ("transcribe_provider", "transcribe_custom_preset"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                cli._coerce(key, "deepseek")

    def test_switching_custom_endpoints_does_not_forward_conversation(self):
        conf = self.config(assistant_provider="custom", custom_base_url="http://localhost:8000/v1")
        with fake_urlopen({"choices": [{"message": {"content": "answer"}}]}) as calls:
            assistant.ask("first", conf)
            assistant.ask("second", conf)
            conf["custom_base_url"] = "http://localhost:9000/v1"
            assistant.ask("third", conf)
        self.assertEqual(len(sent_json(calls[1])["messages"]), 4)
        self.assertEqual(len(sent_json(calls[2])["messages"]), 2)
        self.assertEqual(assistant.stored_provider(conf), "custom")
