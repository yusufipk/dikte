#!/usr/bin/env python3
"""Tests for the local whisper.cpp provider.

Nothing here needs whisper.cpp installed. The download tests serve the bytes
from a local HTTP server, and the process tests run a stand-in for
whisper-server: a script that takes the same arguments, opens its port only
after a pause the way loading a model does, and answers on the inference path.
That covers the two things that can actually go wrong — the arguments Dikte
builds, and what it does while and after the process is up.

    python3 -m unittest test_whispercpp -v
"""

import contextlib
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock

import api
import whispercpp

# A stand-in for whisper-server. It records its argv and every request body so
# the tests can check what Dikte sent, and it binds its port late, which is
# what makes "the port is open" a real signal rather than one that is true
# immediately.
FAKE_SERVER = '''#!/usr/bin/env python3
import http.server, json, os, re, sys, time

argv = sys.argv[1:]
record = os.environ["FAKE_RECORD"]
delay = float(os.environ.get("FAKE_DELAY", "0.2"))
die = os.environ.get("FAKE_DIE", "")

with open(record + ".attempts", "a") as fh:
    fh.write("x")

if die:
    sys.stderr.write("whisper_init_from_file: %s\\n" % die)
    sys.stderr.flush()
    sys.exit(1)

def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default

port = int(flag("--port"))
path = flag("--inference-path")
state = {"argv": argv, "bodies": []}

def save():
    with open(record, "w") as fh:
        json.dump(state, fh)

save()
sys.stderr.write("loading model\\n")
sys.stderr.flush()
time.sleep(delay)          # the model load, which happens before the bind

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        text = body.decode("utf-8", "replace")
        state["bodies"].append({"path": self.path, "body": text})
        save()
        if self.path != path:
            self.send_response(404)
            self.end_headers()
            return
        match = re.search(r'name="response_format"\\r\\n\\r\\n([^\\r]*)', text)
        fmt = match.group(1) if match else "json"
        if fmt == "verbose_json":
            payload = {
                "task": "transcribe", "language": "turkish", "duration": 3.0,
                "text": " bir iki uc",
                "segments": [
                    {"id": 0, "start": 0.0, "end": 1.5, "text": " bir iki"},
                    {"id": 1, "start": 1.5, "end": 3.0, "text": " uc"},
                ],
            }
        else:
            payload = {"text": " bir iki uc"}
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def write_script(directory, body):
    path = pathlib.Path(directory) / "fake-whisper-server"
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dikte-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.models = pathlib.Path(self.tmp) / "models"
        self.models.mkdir()
        self._real_models_dir = whispercpp.MODELS_DIR
        whispercpp.MODELS_DIR = self.models
        self.addCleanup(setattr, whispercpp, "MODELS_DIR", self._real_models_dir)
        self.addCleanup(whispercpp.stop)
        # Leave the singleton the way the next test expects to find it.
        self.addCleanup(whispercpp.configure,
                        model=whispercpp.DEFAULT_MODEL, threads=0, gpu=True, binary="")

    def make_model(self, model_id, content=b"ggml"):
        path = whispercpp.model_path(model_id)
        path.write_bytes(content)
        return path


class Catalogue(Base):
    def test_ids_are_unique(self):
        ids = [m.id for m in whispercpp.MODELS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_default_is_in_the_catalogue(self):
        self.assertIsNotNone(whispercpp.find(whispercpp.DEFAULT_MODEL))

    def test_every_entry_has_a_label_and_a_size(self):
        for model in whispercpp.MODELS:
            self.assertTrue(model.label, model.id)
            self.assertGreater(model.size, 1_000_000, model.id)

    def test_unknown_id_is_not_found(self):
        self.assertIsNone(whispercpp.find("large-v9"))

    def test_model_path_is_under_the_models_directory(self):
        path = whispercpp.model_path("small")
        self.assertEqual(path.parent, self.models)
        self.assertEqual(path.name, "ggml-small.bin")

    def test_installed_follows_the_file(self):
        self.assertFalse(whispercpp.installed("small"))
        self.make_model("small")
        self.assertTrue(whispercpp.installed("small"))
        self.assertEqual(whispercpp.installed_ids(), ["small"])

    def test_an_empty_file_is_not_a_model(self):
        whispercpp.model_path("small").write_bytes(b"")
        self.assertFalse(whispercpp.installed("small"))

    def test_human_size(self):
        self.assertEqual(whispercpp.human_size(512), "512 B")
        self.assertEqual(whispercpp.human_size(1536), "1.5 KB")
        self.assertEqual(whispercpp.human_size(3 * 1024 ** 3), "3.0 GB")

    def test_url_points_at_the_ggml_file(self):
        self.assertTrue(whispercpp.model_url("tiny").endswith("/ggml-tiny.bin"))


class Downloads(Base):
    """The bytes come from a local server, so nothing here touches the network."""

    def serve(self, body, length=None, status=200):
        payload = body

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Length",
                                 str(len(payload) if length is None else length))
                self.end_headers()
                if status == 200:
                    # The tests that stop or refuse a download hang up while
                    # this is still writing, which is not a failure here.
                    with contextlib.suppress(OSError):
                        self.wfile.write(payload)

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        real = whispercpp.HF_BASE
        whispercpp.HF_BASE = base
        self.addCleanup(setattr, whispercpp, "HF_BASE", real)
        return base

    def test_a_download_lands_and_reports_progress(self):
        payload = os.urandom(3 * whispercpp.DOWNLOAD_CHUNK + 17)
        self.serve(payload)
        seen = []
        self.assertTrue(
            whispercpp.download("tiny", on_progress=lambda d, t: seen.append((d, t)))
        )
        self.assertEqual(whispercpp.model_path("tiny").read_bytes(), payload)
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (len(payload), len(payload)))
        # Monotonic, and every step reports the same expected total.
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_stopping_leaves_no_model_and_no_part_file(self):
        self.serve(os.urandom(4 * whispercpp.DOWNLOAD_CHUNK))
        self.assertFalse(whispercpp.download("tiny", should_stop=lambda: True))
        self.assertFalse(whispercpp.installed("tiny"))
        self.assertEqual(list(self.models.iterdir()), [])

    def test_a_short_body_is_not_renamed_into_place(self):
        # What a proxy cutting the connection halfway looks like: the length
        # header promises more than arrives.
        self.serve(b"only the start", length=10_000_000)
        with self.assertRaises(whispercpp.LocalWhisperError):
            whispercpp.download("tiny")
        self.assertFalse(whispercpp.installed("tiny"))
        self.assertEqual(list(self.models.iterdir()), [])

    def test_an_http_error_is_reported(self):
        self.serve(b"", status=404)
        with self.assertRaises(whispercpp.LocalWhisperError) as caught:
            whispercpp.download("tiny")
        self.assertIn("404", str(caught.exception))
        self.assertFalse(whispercpp.installed("tiny"))

    def test_an_unknown_model_is_refused_before_any_request(self):
        with self.assertRaises(whispercpp.LocalWhisperError):
            whispercpp.download("large-v9")


class ServerProcess(Base):
    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.tmp, "record.json")
        os.environ["FAKE_RECORD"] = self.record
        self.addCleanup(os.environ.pop, "FAKE_RECORD", None)
        self.addCleanup(os.environ.pop, "FAKE_DELAY", None)
        self.addCleanup(os.environ.pop, "FAKE_DIE", None)
        self.binary = write_script(self.tmp, FAKE_SERVER)
        self.make_model("small")
        whispercpp.configure(model="small", binary=self.binary, gpu=True, threads=0)

    def recorded(self):
        with open(self.record) as fh:
            return json.load(fh)

    def attempts(self):
        try:
            with open(self.record + ".attempts") as fh:
                return len(fh.read())
        except FileNotFoundError:
            return 0

    def test_missing_binary_names_the_package(self):
        whispercpp.configure(binary=os.path.join(self.tmp, "nope"))
        with self.assertRaises(whispercpp.LocalWhisperError) as caught:
            whispercpp.serve()
        self.assertIn("whisper-cpp", str(caught.exception))

    def test_missing_model_points_at_the_settings(self):
        whispercpp.configure(model="medium")
        with self.assertRaises(whispercpp.LocalWhisperError) as caught:
            whispercpp.serve()
        self.assertIn("medium", str(caught.exception))
        self.assertFalse(whispercpp.running())

    def test_a_process_that_dies_reports_what_it_printed(self):
        os.environ["FAKE_DIE"] = "invalid model file"
        with self.assertRaises(whispercpp.LocalWhisperError) as caught:
            whispercpp.serve()
        self.assertIn("invalid model file", str(caught.exception))
        self.assertFalse(whispercpp.running())
        # A corrupt model fails the same way every time; retrying only makes
        # the user wait longer for the same message.
        self.assertEqual(self.attempts(), 1)

    def test_a_taken_port_is_retried(self):
        # The wording whisper.cpp uses when bind_to_port fails, which is the
        # one failure another go can fix: something grabbed the port between
        # Dikte probing it and the server binding it.
        os.environ["FAKE_DIE"] = "couldn't bind to server socket: hostname=127.0.0.1"
        with self.assertRaises(whispercpp.LocalWhisperError):
            whispercpp.serve()
        self.assertEqual(self.attempts(), 3)

    def test_serve_starts_it_and_hands_back_a_reachable_url(self):
        url = whispercpp.serve()
        self.assertTrue(whispercpp.running())
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/v1$")
        port = int(url.rsplit(":", 1)[1].split("/")[0])
        with contextlib.closing(socket.create_connection(("127.0.0.1", port), 2)):
            pass

    def test_the_arguments_are_the_ones_the_design_relies_on(self):
        whispercpp.serve()
        argv = self.recorded()["argv"]
        self.assertIn("--inference-path", argv)
        self.assertEqual(argv[argv.index("--inference-path") + 1],
                         "/v1/audio/transcriptions")
        self.assertEqual(argv[argv.index("-m") + 1],
                         str(whispercpp.model_path("small")))
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        # "auto" rather than the server's own English default, because api.py
        # leaves the language field out when it is set to detect.
        self.assertEqual(argv[argv.index("-l") + 1], "auto")
        self.assertIn("-sns", argv)
        self.assertIn("-nlp", argv)
        self.assertNotIn("-ng", argv)      # the GPU is on
        self.assertNotIn("-t", argv)       # threads left automatic

    def test_gpu_off_and_a_thread_count_reach_the_command_line(self):
        whispercpp.configure(gpu=False, threads=6)
        whispercpp.serve()
        argv = self.recorded()["argv"]
        self.assertIn("-ng", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "6")

    def test_serving_twice_reuses_the_same_process(self):
        first = whispercpp.serve()
        started = os.path.getmtime(self.record)
        time.sleep(0.05)
        self.assertEqual(whispercpp.serve(), first)
        self.assertEqual(os.path.getmtime(self.record), started)

    def test_changing_the_model_stops_the_running_server(self):
        whispercpp.serve()
        self.make_model("base")
        whispercpp.configure(model="base")
        self.assertFalse(whispercpp.running())
        self.assertEqual(whispercpp.base_url(), "")
        whispercpp.serve()
        self.assertEqual(self.recorded()["argv"][1], str(whispercpp.model_path("base")))

    def test_changing_nothing_leaves_it_alone(self):
        url = whispercpp.serve()
        whispercpp.configure(model="small", gpu=True, threads=0)
        self.assertTrue(whispercpp.running())
        self.assertEqual(whispercpp.base_url(), url)

    def test_stop_is_safe_to_call_twice(self):
        whispercpp.serve()
        whispercpp.stop()
        whispercpp.stop()
        self.assertFalse(whispercpp.running())

    def test_serve_restarts_a_server_that_went_away(self):
        whispercpp.serve()
        whispercpp._SERVER._proc.kill()
        whispercpp._SERVER._proc.wait(timeout=5)
        self.assertFalse(whispercpp.running())
        self.assertTrue(whispercpp.serve())
        self.assertTrue(whispercpp.running())

    def test_startup_that_never_finishes_times_out(self):
        os.environ["FAKE_DELAY"] = "30"
        real = whispercpp.STARTUP_TIMEOUT
        whispercpp.STARTUP_TIMEOUT = 0.5
        self.addCleanup(setattr, whispercpp, "STARTUP_TIMEOUT", real)
        with self.assertRaises(whispercpp.LocalWhisperError):
            whispercpp.serve()
        self.assertFalse(whispercpp.running())

    def test_ready_and_status_follow_the_binary_and_the_file(self):
        self.assertTrue(whispercpp.ready("small", self.binary))
        self.assertFalse(whispercpp.ready("medium", self.binary))
        self.assertIn("medium", whispercpp.status("medium", self.binary))
        self.assertIn("small", whispercpp.status("small", self.binary))
        self.assertIn("whisper-cpp",
                      whispercpp.status("small", os.path.join(self.tmp, "nope")))


class ThroughApi(Base):
    """api.py's side: a local target reaches the server and parses its replies."""

    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.tmp, "record.json")
        os.environ["FAKE_RECORD"] = self.record
        self.addCleanup(os.environ.pop, "FAKE_RECORD", None)
        self.binary = write_script(self.tmp, FAKE_SERVER)
        self.make_model("small")
        whispercpp.configure(model="small", binary=self.binary)
        self.wav = os.path.join(self.tmp, "clip.wav")
        with open(self.wav, "wb") as fh:
            fh.write(b"RIFF----WAVEfmt ")
        self.target = api.Target("local", "Local Whisper", "local", "", "small")

    def bodies(self):
        with open(self.record) as fh:
            return json.load(fh)["bodies"]

    def test_transcribe_fills_in_the_base_url_and_returns_the_text(self):
        text = api.transcribe(self.target, self.wav, language="tr", prompt="Kubernetes")
        self.assertEqual(text, "bir iki uc")
        sent = self.bodies()[-1]
        self.assertEqual(sent["path"], "/v1/audio/transcriptions")
        self.assertIn('name="language"\r\n\r\ntr', sent["body"])
        self.assertIn('name="prompt"\r\n\r\nKubernetes', sent["body"])
        self.assertIn('name="response_format"\r\n\r\njson', sent["body"])

    def test_auto_leaves_the_language_out_so_the_server_detects(self):
        api.transcribe(self.target, self.wav, language="auto")
        self.assertNotIn('name="language"', self.bodies()[-1]["body"])

    def test_segments_come_back_with_their_times(self):
        segments = api.transcribe_segments(self.target, self.wav, language="tr")
        self.assertEqual(segments,
                         [(0.0, 1.5, "bir iki"), (1.5, 3.0, "uc")])
        self.assertIn('name="response_format"\r\n\r\nverbose_json',
                      self.bodies()[-1]["body"])

    def test_the_loaded_model_is_not_swapped_out_for_whisper_1(self):
        # The hosted providers move to whisper-1 for timestamps; the local
        # server only has the file it was started with.
        self.assertEqual(api.timestamp_model("local", "small"), "small")
        api.transcribe_segments(self.target, self.wav)
        self.assertIn('name="model"\r\n\r\nsmall', self.bodies()[-1]["body"])

    def test_a_missing_model_surfaces_as_an_api_error(self):
        whispercpp.configure(model="medium")
        with self.assertRaises(api.ApiError):
            api.transcribe(self.target._replace(model="medium"), self.wav)

    def test_a_local_target_needs_no_api_key(self):
        keyless = self.target._replace(api_key="")
        self.assertEqual(api.transcribe(keyless, self.wav), "bir iki uc")


class WordSplits(unittest.TestCase):
    """whisper.cpp cuts segments on tokens, which lands inside words.

    All of these are the shape the real server returned for a Turkish clip:
    "akraba değ" and "iller." as two segments, and a plain text where the only
    thing between them is the newline whisper.cpp puts after every segment.
    """

    def test_the_line_breaks_come_out_with_nothing_in_their_place(self):
        raw = ("Tom ve Mary'nin soyadları aynı olmasına rağmen akraba değ\n"
               "iller.\n Sorun, kamuoyunda tartışmalara yol açtı.")
        self.assertEqual(
            api._local_text(raw),
            "Tom ve Mary'nin soyadları aynı olmasına rağmen akraba değiller."
            " Sorun, kamuoyunda tartışmalara yol açtı.",
        )

    def test_a_split_word_is_folded_back_into_one_segment(self):
        merged = api._merge_word_splits([
            {"start": 0.0, "end": 3.64, "text": " akraba değ"},
            {"start": 3.64, "end": 4.08, "text": "iller."},
            {"start": 5.38, "end": 7.96, "text": " Sorun, yol açtı."},
        ])
        self.assertEqual([s["text"] for s in merged],
                         [" akraba değiller.", " Sorun, yol açtı."])
        # The merged segment covers both halves of the word.
        self.assertEqual((merged[0]["start"], merged[0]["end"]), (0.0, 4.08))

    def test_a_segment_that_starts_a_word_is_left_alone(self):
        segments = [{"start": 0.0, "end": 1.0, "text": " bir"},
                    {"start": 1.0, "end": 2.0, "text": " iki"}]
        self.assertEqual(len(api._merge_word_splits(segments)), 2)

    def test_three_pieces_of_one_word_all_fold_together(self):
        merged = api._merge_word_splits([
            {"start": 0.0, "end": 1.0, "text": " kar"},
            {"start": 1.0, "end": 2.0, "text": "şı"},
            {"start": 2.0, "end": 3.0, "text": "laştırma."},
        ])
        self.assertEqual([s["text"] for s in merged], [" karşılaştırma."])
        self.assertEqual(merged[0]["end"], 3.0)

    def test_hosted_transcripts_keep_their_line_breaks(self):
        # Only the local branch touches the text; a provider that means its
        # newlines gets to keep them.
        self.assertTrue(api._continues_a_word("değ", "iller"))
        self.assertFalse(api._continues_a_word("değil", " mi"))
        self.assertFalse(api._continues_a_word("", "iller"))


class StaleServers(Base):
    """A Dikte killed outright leaves the model sitting in graphics memory."""

    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.tmp, "record.json")
        os.environ["FAKE_RECORD"] = self.record
        self.addCleanup(os.environ.pop, "FAKE_RECORD", None)
        self.binary = write_script(self.tmp, FAKE_SERVER)
        self.make_model("small")
        whispercpp.configure(model="small", binary=self.binary)

    def test_the_pid_is_written_down_while_the_server_runs(self):
        whispercpp.serve()
        pid = int(whispercpp._pid_file().read_text())
        self.assertEqual(pid, whispercpp._SERVER._proc.pid)

    def test_a_clean_stop_takes_the_note_away(self):
        whispercpp.serve()
        whispercpp.stop()
        self.assertFalse(whispercpp._pid_file().exists())

    def test_a_leaked_server_is_swept_on_the_next_start(self):
        whispercpp.serve()
        proc = whispercpp._SERVER._proc
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        # What a SIGKILL on Dikte leaves behind: the process is still running
        # and nothing in this interpreter knows about it any more.
        whispercpp._SERVER._proc = None
        self.assertIsNone(proc.poll())

        self.assertTrue(whispercpp.sweep())
        proc.wait(timeout=10)
        self.assertIsNotNone(proc.poll())
        self.assertFalse(whispercpp._pid_file().exists())

    def test_a_reused_pid_belonging_to_something_else_is_left_alone(self):
        # pids come round again; killing whatever holds the number now would be
        # a good deal worse than the leak.
        whispercpp._pid_file().parent.mkdir(parents=True, exist_ok=True)
        whispercpp._pid_file().write_text(str(os.getpid()))
        self.assertFalse(whispercpp.sweep())
        self.assertFalse(whispercpp._pid_file().exists())

    def test_no_note_means_nothing_to_sweep(self):
        self.assertFalse(whispercpp.sweep())

    def test_a_pid_that_is_gone_is_not_an_error(self):
        whispercpp._pid_file().parent.mkdir(parents=True, exist_ok=True)
        whispercpp._pid_file().write_text("999999")
        self.assertFalse(whispercpp.sweep())


class ConfigWiring(Base):
    def test_the_target_is_local_and_carries_no_base_url(self):
        import config as cfg
        conf = cfg.Config()
        conf["transcribe_provider"] = "local"
        conf["local_model"] = "small"
        target = conf.transcribe_target()
        self.assertEqual(target.provider, "local")
        self.assertEqual(target.model, "small")
        # api.py fills this in when the server is actually needed; reading the
        # settings must not start a process.
        self.assertEqual(target.base_url, "")
        self.assertFalse(whispercpp.running())

    def test_readiness_asks_about_the_model_not_a_key(self):
        import config as cfg
        conf = cfg.Config()
        conf["transcribe_provider"] = "local"
        conf["local_model"] = "small"
        conf["local_binary"] = write_script(self.tmp, FAKE_SERVER)
        self.assertFalse(conf.transcribe_ready())
        self.make_model("small")
        self.assertTrue(conf.transcribe_ready())

    def test_hosted_providers_still_go_by_the_key(self):
        import config as cfg
        conf = cfg.Config()
        conf["transcribe_provider"] = "openai"
        conf["openai_api_key"] = ""
        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertFalse(conf.transcribe_ready())
        conf["openai_api_key"] = "sk-test"
        self.assertTrue(conf.transcribe_ready())


if __name__ == "__main__":
    unittest.main()
