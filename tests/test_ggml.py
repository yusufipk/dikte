"""Fetching a program and a model, and keeping a server alive on them.

No network and no whisper.cpp: the downloads are answered from memory, and the
servers are stand-in scripts that take the same arguments and open their port
when they are told to, which is the only thing the code waits on.
"""

import contextlib
import hashlib
import io
import os
import signal
import sys
import tarfile
import textwrap
import threading
import time
from unittest import mock

import ggml
import hub
from tests.support import (DikteTest, fake_urlopen, http_error, json_body,
                           linux_only, url_error)


def body(data, length=None):
    """What urlopen hands back for a download: a reader with a length header."""
    class Body:
        def __init__(self):
            self._buf = io.BytesIO(data)
            self.headers = {"Content-Length":
                            str(len(data) if length is None else length)}

        def read(self, count=-1):
            return self._buf.read(count)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False
    return Body()


def item(name, data, url="https://example.invalid/f", sha=True):
    return hub.Item(name, url, len(data),
                    hashlib.sha256(data).hexdigest() if sha else "")


@contextlib.contextmanager
def serving(release, archive):
    """Answer by what is being asked for rather than by what came before.

    An install asks GitHub what the release is and then asks for one file out of
    it, and the first of those two comes from the cache the second time around.
    Answering in order would then hand the archive request the release listing.
    """
    def opener(request, timeout=None):
        url = request.full_url
        if "api.github.com" in url:
            return json_body(release)
        return body(archive)

    with mock.patch("urllib.request.urlopen", side_effect=opener) as calls:
        yield calls


def tarball(entries):
    """A .tar.gz laid out the way the releases are: one directory of files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class Local(DikteTest):
    """A test with its own bin, models and cache directories."""

    def setUp(self):
        super().setUp()
        self.patch_attr(ggml, "DATA_DIR", self.path("data"))
        self.patch_attr(ggml, "BIN_DIR", self.path("data", "bin"))
        self.patch_attr(ggml, "MODELS_DIR", self.path("data", "models"))
        self.patch_attr(hub, "CACHE_DIR", self.path("cache"))


# --- downloading ----------------------------------------------------------


class Download(Local):
    def test_it_lands_and_the_part_file_is_gone(self):
        data = b"a model, more or less" * 100
        target = self.path("data", "models", "m.bin")
        with fake_urlopen(body(data)):
            self.assertTrue(ggml.download(item("m.bin", data), target))
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(target.with_name("m.bin.part").exists())

    def test_a_wrong_checksum_installs_nothing(self):
        data = b"the bytes that arrived"
        wrong = hub.Item("m.bin", "https://example.invalid/f", len(data), "f" * 64)
        target = self.path("data", "models", "m.bin")
        with fake_urlopen(body(data)):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml.download(wrong, target)
        self.assertIn("checksum", str(caught.exception))
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name("m.bin.part").exists())

    def test_a_body_shorter_than_its_header_installs_nothing(self):
        data = b"half of it"
        target = self.path("data", "models", "m.bin")
        with fake_urlopen(body(data, length=len(data) * 2)):
            with self.assertRaises(ggml.LocalError):
                ggml.download(item("m.bin", data), target)
        self.assertFalse(target.exists())

    def test_a_file_with_no_published_checksum_is_refused(self):
        # Everything fetched here is run or parsed by something written in C++,
        # and GitHub did not always publish a digest.
        data = b"a program, say"
        target = self.path("data", "models", "m.bin")
        with fake_urlopen(body(data)):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml.download(item("m.bin", data, sha=False), target)
        self.assertIn("checksum", str(caught.exception))
        self.assertFalse(target.exists())

    def test_nothing_is_asked_for_before_it_is_refused(self):
        # The refusal is not worth a gigabyte of somebody's bandwidth first.
        with fake_urlopen(body(b"never read")) as calls:
            with self.assertRaises(ggml.LocalError):
                ggml.download(item("m.bin", b"x", sha=False), self.path("m.bin"))
        self.assertEqual(calls, [])

    def test_stopping_leaves_nothing_behind(self):
        data = b"x" * (ggml.DOWNLOAD_CHUNK * 3)
        target = self.path("data", "models", "m.bin")
        with fake_urlopen(body(data)):
            landed = ggml.download(item("m.bin", data), target,
                                   should_stop=lambda: True)
        self.assertFalse(landed)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name("m.bin.part").exists())

    def test_progress_is_reported_against_the_total(self):
        data = b"y" * (ggml.DOWNLOAD_CHUNK + 5)
        seen = []
        with fake_urlopen(body(data)):
            ggml.download(item("m.bin", data), self.path("data", "m.bin"),
                          on_progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen[-1], (len(data), len(data)))
        self.assertGreater(len(seen), 1)

    def test_a_refused_connection_says_which_file(self):
        with fake_urlopen(url_error("no route to host")):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml.download(item("m.bin", b"x"), self.path("data", "m.bin"))
        self.assertIn("m.bin", str(caught.exception))

    def test_an_http_error_is_not_written_to_disk(self):
        target = self.path("data", "m.bin")
        with fake_urlopen(http_error(404)):
            with self.assertRaises(ggml.LocalError):
                ggml.download(item("m.bin", b"x"), target)
        self.assertFalse(target.exists())


# --- installing a program -------------------------------------------------


class InstallProgram(Local):
    def setUp(self):
        super().setUp()
        # These fixtures are Ubuntu release archives.  Keep checking that path
        # on every host, including the Mac that checks the macOS backend.
        self.patch_attr(sys, "platform", "linux")
        # Built once, because the release listing has to publish its checksum
        # and a tarball is not the same bytes twice.
        self.archive = tarball({
            "whisper-bin-ubuntu-x64/whisper-server": b"#!/bin/sh\nexit 0\n",
            "whisper-bin-ubuntu-x64/libwhisper.so": b"not really a library",
        })

    def release(self, *names, archive=None):
        digest = hashlib.sha256(self.archive if archive is None else archive)
        return {"tag_name": "v1.9.1", "assets": [
            {"name": name, "browser_download_url": f"https://example.invalid/{name}",
             "size": 10, "digest": "sha256:" + digest.hexdigest()}
            for name in names]}

    def install(self, *names, archive=None):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        blob = self.archive if archive is None else archive
        with serving(self.release(*names, archive=blob), blob) as calls:
            path = ggml.install_program(ggml.WHISPER)
        return path, [call.args[0].full_url for call in calls.call_args_list]

    def test_the_binary_and_its_libraries_land_together(self):
        path, _ = self.install("whisper-bin-ubuntu-x64.tar.gz")
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.access(path, os.X_OK))
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(path),
                                                    "libwhisper.so")))

    def test_the_build_for_this_machine_is_the_one_fetched(self):
        _, urls = self.install("whisper-bin-x64.zip", "whisper-bin-ubuntu-arm64.tar.gz",
                               "whisper-bin-ubuntu-x64.tar.gz")
        self.assertTrue(urls[1].endswith("whisper-bin-ubuntu-x64.tar.gz"))

    def test_a_release_with_nothing_for_this_machine_says_so(self):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        with fake_urlopen(self.release("whisper-bin-Win32.zip")):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml.install_program(ggml.WHISPER)
        self.assertIn("this machine", str(caught.exception))

    def test_a_mac_does_not_install_an_ubuntu_archive_for_the_same_architecture(self):
        self.patch_attr(sys, "platform", "darwin")
        self.patch_attr(ggml, "_arch", lambda: "arm64")
        listing = self.release("whisper-bin-ubuntu-arm64.tar.gz")
        with fake_urlopen(listing):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml.install_program(ggml.WHISPER)
        self.assertIn("Build whisper-server yourself", str(caught.exception))

    def test_a_mac_uses_the_native_llama_archive_instead_of_ubuntu(self):
        self.patch_attr(sys, "platform", "darwin")
        self.patch_attr(ggml, "_arch", lambda: "arm64")
        self.assertEqual(
            ggml._wanted_assets(ggml.LLAMA),
            ("bin-macos-arm64.tar.gz",),
        )

    def test_what_was_installed_is_remembered(self):
        path, _ = self.install("whisper-bin-ubuntu-x64.tar.gz")
        self.assertEqual(ggml.installed_program(ggml.WHISPER), path)
        self.assertEqual(ggml.installed_version(ggml.WHISPER), "v1.9.1")

    def test_a_record_pointing_at_a_deleted_binary_counts_for_nothing(self):
        path, _ = self.install("whisper-bin-ubuntu-x64.tar.gz")
        os.unlink(path)
        self.assertEqual(ggml.installed_program(ggml.WHISPER), "")

    def test_the_archive_is_not_kept(self):
        self.install("whisper-bin-ubuntu-x64.tar.gz")
        left = list((self.path("data", "bin", "whisper")).glob("*.tar.gz"))
        self.assertEqual(left, [])

    def test_the_previous_version_is_swept_up(self):
        self.install("whisper-bin-ubuntu-x64.tar.gz")
        old = self.path("data", "bin", "whisper", "v1.9.0")
        old.mkdir(parents=True)
        (old / "whisper-server").write_bytes(b"older")
        self.install("whisper-bin-ubuntu-x64.tar.gz")
        self.assertFalse(old.exists())

    def test_an_archive_without_the_binary_is_refused(self):
        empty = tarball({"whisper-bin-ubuntu-x64/README": b"nothing here"})
        with self.assertRaises(ggml.LocalError) as caught:
            self.install("whisper-bin-ubuntu-x64.tar.gz", archive=empty)
        self.assertIn("whisper-server", str(caught.exception))


    def test_a_release_without_a_published_checksum_is_refused(self):
        # GitHub did not always publish one, and whisper.cpp v1.8.0 and older
        # still have none.
        self.patch_attr(ggml, "_arch", lambda: "x64")
        listing = {"tag_name": "v1.8.0", "assets": [
            {"name": "whisper-bin-ubuntu-x64.tar.gz",
             "browser_download_url": "https://example.invalid/w.tar.gz",
             "size": 10}]}
        with serving(listing, self.archive):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml.install_program(ggml.WHISPER)
        self.assertIn("checksum", str(caught.exception))
        self.assertEqual(ggml.installed_program(ggml.WHISPER), "")

    def test_an_archive_that_is_not_what_was_promised_installs_nothing(self):
        listing = self.release("whisper-bin-ubuntu-x64.tar.gz")
        other = tarball({"whisper-bin-ubuntu-x64/whisper-server": b"#!/bin/sh\nrm -rf\n"})
        self.patch_attr(ggml, "_arch", lambda: "x64")
        with serving(listing, other):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml.install_program(ggml.WHISPER)
        self.assertIn("checksum", str(caught.exception))
        self.assertEqual(ggml.installed_program(ggml.WHISPER), "")

    def test_an_archive_cannot_write_outside_the_directory_it_is_opened_in(self):
        # An archive is not a trusted thing to unpack: a member named ../../ is
        # how one writes over a file it was never given.
        escape = tarball({"../../../escaped": b"should not land"})
        path = self.path("data", "bin", "whisper", "v1.9.1")
        with self.assertRaises(ggml.LocalError):
            self.install("whisper-bin-ubuntu-x64.tar.gz", archive=escape)
        self.assertFalse(self.path("escaped").exists())
        self.assertFalse((path.parent.parent / "escaped").exists())

    def test_a_symlink_out_of_the_directory_does_not_survive_either(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("whisper-bin-ubuntu-x64/whisper-server")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
        with self.assertRaises(ggml.LocalError):
            self.install("whisper-bin-ubuntu-x64.tar.gz", archive=buf.getvalue())

    def test_everything_is_asked_for_over_tls(self):
        for url in (hub.GITHUB_API, hub.HF_API, hub.HF_FILES):
            with self.subTest(url=url):
                self.assertTrue(url.startswith("https://"))

    def test_llama_takes_the_vulkan_build_when_there_is_a_loader(self):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        self.assertEqual(ggml._wanted_assets(ggml.LLAMA)[0],
                         "bin-ubuntu-vulkan-x64.tar.gz")

    def test_llama_falls_back_to_the_plain_build_without_one(self):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: False)
        self.assertEqual(ggml._wanted_assets(ggml.LLAMA), ("bin-ubuntu-x64.tar.gz",))


class WhichCopyRuns(Local):
    def test_a_system_build_wins_over_a_downloaded_one(self):
        self.patch_attr(ggml, "installed_program", lambda program: "/data/whisper-server")
        with mock.patch("shutil.which", return_value="/usr/bin/whisper-server"):
            self.assertEqual(ggml.program_path(ggml.WHISPER), "/usr/bin/whisper-server")

    def test_the_downloaded_one_is_used_when_there_is_no_system_build(self):
        self.patch_attr(ggml, "installed_program", lambda program: "/data/whisper-server")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(ggml.program_path(ggml.WHISPER), "/data/whisper-server")

    def test_a_setting_pointing_at_nothing_is_no_program(self):
        self.assertEqual(ggml.program_path(ggml.WHISPER, "/nowhere/whisper-server"), "")

    def test_a_setting_pointing_at_a_program_wins(self):
        mine = self.path("mine")
        mine.write_text("#!/bin/sh\n")
        mine.chmod(0o755)
        with mock.patch("shutil.which", return_value="/usr/bin/whisper-server"):
            self.assertEqual(ggml.program_path(ggml.WHISPER, str(mine)), str(mine))


# --- the lists ------------------------------------------------------------


WHISPER_TREE = [
    {"type": "file", "path": "ggml-base.bin", "size": 147951465,
     "lfs": {"oid": "a" * 64}},
    {"type": "file", "path": "ggml-large-v3-turbo-q5_0.bin", "size": 574041195,
     "lfs": {"oid": "b" * 64}},
    {"type": "file", "path": "ggml-base-encoder.mlmodelc.zip", "size": 37922638,
     "lfs": {"oid": "c" * 64}},
    {"type": "file", "path": "README.md", "size": 3196},
]

GGUF_TREE = [
    {"type": "file", "path": "gemma-3-4b-it-Q4_K_M.gguf", "size": 2489000000,
     "lfs": {"oid": "a" * 64}},
    {"type": "file", "path": "gemma-3-4b-it-Q8_0.gguf", "size": 4130000000,
     "lfs": {"oid": "b" * 64}},
    {"type": "file", "path": "mmproj-model-f16.gguf", "size": 851000000,
     "lfs": {"oid": "c" * 64}},
    {"type": "file", "path": "mtp-gemma-4-E4B-it-Q4_0.gguf", "size": 59000000,
     "lfs": {"oid": "d" * 64}},
    {"type": "file", "path": "huge-00001-of-00009.gguf", "size": 40000000000,
     "lfs": {"oid": "e" * 64}},
    {"type": "file", "path": "README.md", "size": 100},
]


class Catalogue(Local):
    def test_only_models_are_offered_and_the_small_ones_first(self):
        with fake_urlopen(WHISPER_TREE):
            models = ggml.whisper_models()
        self.assertEqual([m.name for m in models],
                         ["ggml-base.bin", "ggml-large-v3-turbo-q5_0.bin"])

    def test_the_core_ml_encoders_are_not_models(self):
        with fake_urlopen(WHISPER_TREE):
            names = [m.name for m in ggml.whisper_models()]
        self.assertNotIn("ggml-base-encoder.mlmodelc.zip", names)

    def test_the_projector_and_the_draft_head_are_not_models(self):
        with fake_urlopen(GGUF_TREE):
            names = [q.name for q in ggml.llm_quants("ggml-org/gemma-3-4b-it-GGUF")]
        self.assertEqual(names,
                         ["gemma-3-4b-it-Q4_K_M.gguf", "gemma-3-4b-it-Q8_0.gguf"])

    def test_a_model_split_across_files_is_left_out(self):
        with fake_urlopen(GGUF_TREE):
            names = [q.name for q in ggml.llm_quants("ggml-org/gemma-3-4b-it-GGUF")]
        self.assertNotIn("huge-00001-of-00009.gguf", names)

    def test_the_suggestions_come_first_and_the_rest_follow(self):
        listing = [{"id": "ggml-org/something-new-GGUF"},
                   {"id": ggml.SUGGESTED_LLM[0]}]
        with fake_urlopen(listing):
            found = ggml.llm_repos()
        self.assertEqual(found[0], ggml.SUGGESTED_LLM[0])
        self.assertIn("ggml-org/something-new-GGUF", found)

    def test_an_unreachable_list_still_offers_the_suggestions(self):
        with fake_urlopen(url_error()):
            self.assertEqual(ggml.llm_repos(), list(ggml.SUGGESTED_LLM))

    def test_an_unreachable_whisper_list_is_an_error_worth_showing(self):
        with fake_urlopen(url_error()):
            with self.assertRaises(ggml.LocalError):
                ggml.whisper_models()

    def test_what_is_on_disk_is_read_from_disk(self):
        self.assertEqual(ggml.installed_whisper_models(), [])
        path = ggml.whisper_model_path("ggml-base.bin")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"model")
        self.assertEqual(ggml.installed_whisper_models(), ["ggml-base.bin"])
        self.assertTrue(ggml.have_model(path))

    def test_an_empty_file_is_not_a_model(self):
        path = ggml.llm_model_path("ggml-org/x-GGUF/model.gguf")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        self.assertFalse(ggml.have_model(path))

    def test_a_model_is_named_by_its_file_not_its_repository(self):
        self.assertEqual(ggml.llm_model_path("ggml-org/x-GGUF/model.gguf").name,
                         "model.gguf")


# --- keeping a server alive -----------------------------------------------


STAND_IN = textwrap.dedent("""
    import http.server, sys, threading, time

    args = sys.argv[1:]

    def opt(name, default=""):
        return args[args.index(name) + 1] if name in args else default

    if "--die" in args:
        print("could not load model: no such file")
        sys.exit(2)

    time.sleep(float(opt("--wait", "0")))

    started = time.monotonic()
    healthy_after = float(opt("--healthy-after", "0"))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = time.monotonic() - started >= healthy_after
            self.send_response(200 if ok else 503)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer((opt("--host"), int(opt("--port"))), Handler)
    print("listening on " + opt("--port"), flush=True)
    server.serve_forever()
""")


class Servers(Local):
    def setUp(self):
        super().setUp()
        self.path("data").mkdir(parents=True, exist_ok=True)
        # Named for the program and kept inside the data directory, because that
        # is what the sweep looks for on a command line.
        self.script = self.path("data", "whisper-server.py")
        self.script.write_text(STAND_IN)
        self.addCleanup(ggml.stop_all)
        self.servers = []

    def server(self, program=ggml.WHISPER, **settings):
        defaults = {"extra": []}
        defaults.update(settings)
        made = ggml.Server(
            program,
            lambda values: [sys.executable, str(self.script)] + list(values["extra"]),
            defaults,
        )
        self.servers.append(made)
        self.addCleanup(made.stop)
        return made

    def test_a_started_server_hands_back_its_address(self):
        server = self.server()
        url = server.serve()
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/v1$")
        self.assertTrue(server.running)

    def test_the_second_call_does_not_start_a_second_one(self):
        server = self.server()
        first = server.serve()
        self.assertEqual(server.serve(), first)

    def test_a_settings_change_stops_what_was_running(self):
        server = self.server()
        server.serve()
        server.configure(extra=["--wait", "0"])
        self.assertFalse(server.running)

    def test_the_new_settings_are_what_the_next_start_uses(self):
        server = self.server()
        server.serve()
        server.configure(extra=["--healthy-after", "0"])
        second = server.serve()
        self.assertTrue(server.running)
        self.assertTrue(second)

    def test_a_program_that_dies_reports_what_it_printed(self):
        server = self.server(extra=["--die"])
        with self.assertRaises(ggml.LocalError) as caught:
            server.serve()
        self.assertIn("no such file", str(caught.exception))
        self.assertFalse(server.running)

    def test_a_model_that_is_still_loading_is_not_ready_yet(self):
        # llama binds its port first and answers /health with 503 until the
        # model is in memory, so the open port on its own is not the signal.
        server = self.server(program=ggml.LLAMA, extra=["--healthy-after", "0.4"])
        started = time.monotonic()
        server.serve()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_a_start_that_never_becomes_ready_gives_up(self):
        self.patch_attr(ggml, "STARTUP_TIMEOUT", 0.5)
        server = self.server(program=ggml.LLAMA, extra=["--healthy-after", "30"])
        with self.assertRaises(ggml.LocalError):
            server.serve()

    def test_stopping_leaves_nothing_running(self):
        server = self.server()
        server.serve()
        server.stop()
        self.assertFalse(server.running)
        self.assertEqual(server.base_url(), "")

    def test_the_last_thing_it_printed_is_available(self):
        server = self.server()
        server.serve()
        self.assertIn("listening", server.error())

    def test_asking_what_is_running_does_not_wait_for_a_start(self):
        """A model being loaded must not freeze the settings window.

        The interface asks a running server what it is doing while a start is in
        flight, and a lock held across the whole start would stop it dead.
        """
        server = self.server(extra=["--wait", "0.6"])
        answers = []

        def start():
            server.serve()

        thread = __import__("threading").Thread(target=start)
        thread.start()
        try:
            time.sleep(0.15)
            began = time.monotonic()
            answers.append(server.settings())
            answers.append(server.running)
            self.assertLess(time.monotonic() - began, 0.2)
        finally:
            thread.join(timeout=10)

    @linux_only
    def test_a_server_a_killed_dikte_left_behind_is_swept_up(self):
        server = self.server()
        server.serve()
        # What a SIGKILL of Dikte leaves: the child still running, the pid file
        # still on disk, and nothing left that knows about either.
        proc, server._proc = server._proc, None
        self.assertTrue(server.sweep())
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)

    @linux_only
    def test_a_pid_that_belongs_to_something_else_is_left_alone(self):
        server = self.server()
        server._remember(os.getpid())     # this test runner, not a server
        self.assertFalse(server.sweep())

    def test_no_pid_file_is_nothing_to_sweep(self):
        self.assertFalse(self.server().sweep())

    def test_a_start_that_goes_wrong_takes_its_process_with_it(self):
        started = []

        def explode(inner, proc, port):
            started.append(proc)
            raise RuntimeError("something in the wait went wrong")

        self.patch_attr(ggml.Server, "_wait_ready", explode)
        server = self.server()
        with self.assertRaises(RuntimeError):
            server.serve()
        # Nothing else holds a reference to it, so leaving it running would leak
        # a loaded model with nobody left to ask it anything.
        self.assertIsNotNone(started[0].poll())
        self.assertFalse(server.sweep())      # and the pid file went with it


class Arguments(Local):
    """What the two command lines say, since neither program is here to say it."""

    def setUp(self):
        super().setUp()
        self.binary = self.path("whisper-server")
        self.binary.write_text("#!/bin/sh\n")
        self.binary.chmod(0o755)

    def whisper_model(self, name="ggml-base.bin"):
        path = ggml.whisper_model_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")
        return name

    def test_the_inference_path_is_the_one_api_py_builds(self):
        args = ggml._whisper_args({"binary": str(self.binary), "gpu": True,
                                   "threads": 0, "model": self.whisper_model()})
        self.assertIn("--inference-path", args)
        self.assertEqual(args[args.index("--inference-path") + 1],
                         "/v1/audio/transcriptions")

    def test_detection_rather_than_english_when_nothing_is_asked_for(self):
        args = ggml._whisper_args({"binary": str(self.binary), "gpu": True,
                                   "threads": 0, "model": self.whisper_model()})
        self.assertEqual(args[args.index("-l") + 1], "auto")

    def test_the_graphics_card_is_turned_off_rather_than_asked_for(self):
        settings = {"binary": str(self.binary), "gpu": False, "threads": 2,
                    "model": self.whisper_model()}
        args = ggml._whisper_args(settings)
        self.assertIn("-ng", args)
        self.assertEqual(args[args.index("-t") + 1], "2")

    def test_a_missing_model_is_a_message_about_settings(self):
        with self.assertRaises(ggml.LocalError) as caught:
            ggml._whisper_args({"binary": str(self.binary), "gpu": True,
                                "threads": 0, "model": "ggml-nothing.bin"})
        self.assertIn("Settings", str(caught.exception))

    def test_a_missing_program_says_so_before_a_missing_model(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml._whisper_args({"binary": "", "gpu": True, "threads": 0,
                                    "model": self.whisper_model()})
        self.assertIn("whisper.cpp", str(caught.exception))

    def test_the_layers_go_to_the_card_when_there_is_one(self):
        model = ggml.llm_model_path("m.gguf")
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b"gguf")
        args = ggml._llm_args({"binary": str(self.binary), "gpu": True,
                               "threads": 0, "model": "m.gguf", "context": 4096})
        self.assertEqual(args[args.index("-ngl") + 1], "99")
        self.assertEqual(args[args.index("-c") + 1], "4096")

    def test_no_card_means_no_layers_offloaded(self):
        model = ggml.llm_model_path("m.gguf")
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b"gguf")
        args = ggml._llm_args({"binary": str(self.binary), "gpu": False,
                               "threads": 0, "model": "m.gguf", "context": 4096})
        self.assertEqual(args[args.index("-ngl") + 1], "0")


class Sizes(DikteTest):
    def test_bytes_are_written_the_way_a_download_is_talked_about(self):
        self.assertEqual(ggml.human_size(512), "512 B")
        self.assertEqual(ggml.human_size(574041195), "547.4 MB")
        self.assertEqual(ggml.human_size(3_095_033_483), "2.9 GB")
