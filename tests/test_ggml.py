"""Fetching a program and a model, and keeping a server alive on them.

No network and no whisper.cpp: the downloads are answered from memory, and the
servers are stand-in scripts that take the same arguments and open their port
when they are told to, which is the only thing the code waits on.
"""

import contextlib
import hashlib
import io
import os
import pathlib
import signal
import sys
import tarfile
import textwrap
import threading
import time
import zipfile
from unittest import mock

from dikte import ggml
from dikte import hub
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


def listed(name, size):
    """A row as a listing hands it over: a name and a size, no bytes."""
    return hub.Item(name, f"https://example.invalid/{name}", size, "a" * 64)


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


def zipball(entries):
    """A .zip laid out the way the Windows releases are."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)
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

    def test_a_target_held_open_keeps_the_finished_download(self):
        # Windows refuses to replace a file a running server holds open. The
        # bytes are complete and verified by then, so the .part must survive
        # the failure rather than being deleted with everything else.
        data = b"a finished, verified download"
        target = self.path("data", "models", "m.bin")
        with fake_urlopen(body(data)):
            with mock.patch.object(pathlib.Path, "replace",
                                   side_effect=PermissionError(13, "in use")):
                with self.assertRaises(ggml.LocalError) as caught:
                    ggml.download(item("m.bin", data), target)
        self.assertIn("held open", str(caught.exception))
        self.assertEqual(target.with_name("m.bin.part").read_bytes(), data)
        self.assertFalse(target.exists())


# --- installing a program -------------------------------------------------


class InstallProgram(Local):
    def setUp(self):
        super().setUp()
        # These fixtures are Ubuntu release archives.  Keep checking that path
        # on every host, including the Mac that checks the macOS backend.
        self.patch_attr(sys, "platform", "linux")
        self.patch_attr(ggml.platform, "machine", lambda: "x86_64")
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
        self.patch_attr(ggml, "_has_vulkan", lambda: False)
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

    def test_the_nightly_pointer_is_followed_to_where_the_builds_are(self):
        """llama.cpp's latest release carries a tag name, not the binaries."""
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: False)
        marker = self.release(ggml.NIGHTLY_TAG)
        nightly = dict(self.release("llama-b10809-bin-ubuntu-x64.tar.gz"),
                       tag_name="b10809")

        def opener(request, timeout=None):
            url = request.full_url
            if url.endswith("/releases/latest"):
                return json_body(marker)
            if url.endswith("/releases/tags/b10809"):
                return json_body(nightly)
            if url.endswith(ggml.NIGHTLY_TAG):
                return body(b"b10809\n")
            return body(self.archive)

        with mock.patch("urllib.request.urlopen", side_effect=opener):
            tag, found = ggml._pick_asset(ggml.LLAMA)
        self.assertEqual(tag, "b10809")
        self.assertEqual(found.name, "llama-b10809-bin-ubuntu-x64.tar.gz")

    def test_without_a_pointer_the_newest_release_that_has_a_build_is_taken(self):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: False)
        marker = self.release("source.zip")
        listing = [dict(self.release("llama-b2-bin-win-cpu-x64.zip"), tag_name="b2"),
                   dict(self.release("llama-b1-bin-ubuntu-x64.tar.gz"), tag_name="b1")]

        def opener(request, timeout=None):
            url = request.full_url
            return json_body(listing if "per_page" in url else marker)

        with mock.patch("urllib.request.urlopen", side_effect=opener):
            tag, found = ggml._pick_asset(ggml.LLAMA)
        self.assertEqual(tag, "b1")
        self.assertEqual(found.name, "llama-b1-bin-ubuntu-x64.tar.gz")
    def test_linux_x64_with_vulkan_takes_diktes_accelerated_build(self):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        listing = self.release("whisper-bin-ubuntu-vulkan-x64.tar.gz")
        listing["tag_name"] = "whisper.cpp-v1.9.3"
        managed_sha = hashlib.sha256(self.archive).hexdigest()
        with mock.patch.object(ggml, "MANAGED_WHISPER_SHA256", managed_sha,
                               create=True):
            with fake_urlopen(listing, body(self.archive)) as calls:
                path = ggml.install_program(ggml.WHISPER)
        urls = [call.full_url for call in calls]
        self.assertIn(
            "/repos/yusufipk/dikte/releases/tags/whisper.cpp-v1.9.3",
            urls[0],
        )
        self.assertTrue(urls[1].endswith(
            "whisper-bin-ubuntu-vulkan-x64.tar.gz"))
        self.assertTrue(os.path.isfile(path))
        self.assertEqual("v1.9.3", ggml.installed_version(ggml.WHISPER))
        self.assertFalse(ggml.vulkan_missing(ggml.WHISPER))

    def test_an_explicit_whisper_version_still_comes_from_upstream(self):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        listing = self.release("whisper-bin-ubuntu-x64.tar.gz")
        with fake_urlopen(listing, body(self.archive)) as calls:
            ggml.install_program(ggml.WHISPER, tag="v1.9.1")
        self.assertIn(
            "/repos/ggml-org/whisper.cpp/releases/tags/v1.9.1",
            calls[0].full_url,
        )

    def test_linux_arm64_keeps_using_the_upstream_cpu_build(self):
        self.patch_attr(ggml, "_arch", lambda: "arm64")
        self.patch_attr(ggml.platform, "machine", lambda: "aarch64")
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        listing = self.release("whisper-bin-ubuntu-arm64.tar.gz")
        with fake_urlopen(listing, body(self.archive)) as calls:
            ggml.install_program(ggml.WHISPER)
        self.assertIn(
            "/repos/ggml-org/whisper.cpp/releases/latest",
            calls[0].full_url,
        )

    def test_linux_non_x86_does_not_try_the_managed_x64_build(self):
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        listing = self.release("whisper-bin-ubuntu-arm64.tar.gz")
        with mock.patch("platform.machine", return_value="ppc64le"):
            with fake_urlopen(listing, listing) as calls:
                with self.assertRaises(ggml.LocalError):
                    ggml.install_program(ggml.WHISPER)
        self.assertIn(
            "/repos/ggml-org/whisper.cpp/releases/latest",
            calls[0].full_url,
        )

    def test_a_missing_managed_build_falls_back_to_upstream_cpu(self):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        managed = self.release("Dikte-1.1.0-x86_64.AppImage")
        managed["tag_name"] = "whisper.cpp-v1.9.3"
        upstream = self.release("whisper-bin-ubuntu-x64.tar.gz")
        with fake_urlopen(managed, upstream, body(self.archive)) as calls:
            path = ggml.install_program(ggml.WHISPER)
        urls = [call.full_url for call in calls]
        self.assertIn(
            "/repos/yusufipk/dikte/releases/tags/whisper.cpp-v1.9.3",
            urls[0],
        )
        self.assertIn("/repos/ggml-org/whisper.cpp/releases/latest", urls[1])
        self.assertTrue(urls[2].endswith("whisper-bin-ubuntu-x64.tar.gz"))
        self.assertTrue(os.path.isfile(path))

    def test_a_managed_build_with_an_unreviewed_digest_falls_back(self):
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        managed = self.release("whisper-bin-ubuntu-vulkan-x64.tar.gz")
        managed["assets"][0]["digest"] = "sha256:" + "0" * 64
        upstream = self.release("whisper-bin-ubuntu-x64.tar.gz")
        with fake_urlopen(managed, upstream, body(self.archive)) as calls:
            try:
                path = ggml.install_program(ggml.WHISPER)
            except ggml.LocalError as exc:
                self.fail(f"unreviewed digest did not fall back: {exc}")
        urls = [call.full_url for call in calls]
        self.assertEqual(3, len(urls))
        self.assertTrue(urls[2].endswith("whisper-bin-ubuntu-x64.tar.gz"))
        self.assertTrue(os.path.isfile(path))

    def test_an_unavailable_managed_release_falls_back_to_upstream_cpu(self):
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        upstream = self.release("whisper-bin-ubuntu-x64.tar.gz")
        with fake_urlopen(http_error(404), upstream,
                          body(self.archive)) as calls:
            path = ggml.install_program(ggml.WHISPER)
        self.assertEqual(3, len(calls))
        self.assertTrue(calls[2].full_url.endswith(
            "whisper-bin-ubuntu-x64.tar.gz"))
        self.assertTrue(os.path.isfile(path))

    def test_a_fallback_to_the_processor_build_is_there_to_be_shown(self):
        """Until the Vulkan package is published every download lands the
        processor build, and a graphics card sitting idle looks exactly like
        one being used. The window asks this and says so."""
        self.patch_attr(ggml, "_arch", lambda: "x64")
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        managed = self.release("Dikte-1.1.0-x86_64.AppImage")
        managed["tag_name"] = "whisper.cpp-v1.9.3"
        upstream = self.release("whisper-bin-ubuntu-x64.tar.gz")
        with fake_urlopen(managed, upstream, body(self.archive)):
            ggml.install_program(ggml.WHISPER)
        self.assertTrue(ggml.vulkan_missing(ggml.WHISPER))

    def test_a_machine_with_no_vulkan_is_not_told_it_is_missing_one(self):
        # Nothing was on offer to fall back from, so there is nothing to say.
        self.install("whisper-bin-ubuntu-x64.tar.gz")
        self.assertFalse(ggml.vulkan_missing(ggml.WHISPER))

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

    def test_a_failed_update_leaves_the_working_install_alone(self):
        # The old install used to be deleted before the new bytes had even
        # arrived, so a bad download left no local server at all.
        path, _ = self.install("whisper-bin-ubuntu-x64.tar.gz")
        with self.assertRaises(ggml.LocalError):
            self.install("whisper-bin-ubuntu-x64.tar.gz",
                         archive=b"not an archive at all")
        self.assertEqual(ggml.installed_program(ggml.WHISPER), path)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(ggml.installed_version(ggml.WHISPER), "v1.9.1")
        # And the half-made sibling did not linger either.
        left = list(self.path("data", "bin", "whisper").glob("*.new"))
        self.assertEqual(left, [])

    def test_an_update_that_never_downloaded_leaves_the_install_alone(self):
        path, _ = self.install("whisper-bin-ubuntu-x64.tar.gz")
        listing = self.release("whisper-bin-ubuntu-x64.tar.gz")
        with serving(listing, self.archive):
            with mock.patch.object(ggml, "download",
                                   side_effect=ggml.LocalError("no route")):
                with self.assertRaises(ggml.LocalError):
                    ggml.install_program(ggml.WHISPER)
        self.assertEqual(ggml.installed_program(ggml.WHISPER), path)
        self.assertTrue(os.path.isfile(path))

    class StubServer:
        """Owns the installed binary, remembers when it was told to stop."""

        def __init__(self):
            self.program = ggml.WHISPER
            self.stops = 0
            self.new_version_was_ready = False

        def settings(self):
            return {"binary": ""}

        def stop(self):
            self.stops += 1
            self.new_version_was_ready = any(
                (ggml.BIN_DIR / "whisper").glob("*.new"))

    def test_the_running_server_is_stopped_only_for_the_swap(self):
        # The outage is the swap, not the transfer: the server keeps answering
        # through a download that can take minutes, and is stopped only once
        # the replacement is unpacked next door and known to be whole.
        self.install("whisper-bin-ubuntu-x64.tar.gz")
        server = self.StubServer()
        with mock.patch.object(ggml, "SERVERS", (server,)):
            self.install("whisper-bin-ubuntu-x64.tar.gz")
        self.assertEqual(server.stops, 1)
        self.assertTrue(server.new_version_was_ready)

    def test_a_download_that_fails_never_stops_the_server(self):
        self.install("whisper-bin-ubuntu-x64.tar.gz")
        server = self.StubServer()
        listing = self.release("whisper-bin-ubuntu-x64.tar.gz")
        with mock.patch.object(ggml, "SERVERS", (server,)):
            with serving(listing, self.archive):
                with mock.patch.object(ggml, "download",
                                       side_effect=ggml.LocalError("no route")):
                    with self.assertRaises(ggml.LocalError):
                        ggml.install_program(ggml.WHISPER)
        self.assertEqual(server.stops, 0)


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

    def test_the_speculative_decoding_heads_are_not_models(self):
        # They are the small files in a repository, so a list sorted by size
        # puts them first, where the eye lands and the click goes.
        tree = GGUF_TREE + [
            {"type": "file", "path": "dflash-Qwen3-8B-Q8_0.gguf",
             "size": 1_120_000_000, "lfs": {"oid": "f" * 64}},
            {"type": "file", "path": "eagle3-gpt-oss-20b-Q8_0.gguf",
             "size": 920_000_000, "lfs": {"oid": "0" * 64}},
        ]
        with fake_urlopen(tree):
            names = [q.name for q in ggml.llm_quants("ggml-org/x-GGUF")]
        self.assertEqual(names,
                         ["gemma-3-4b-it-Q4_K_M.gguf", "gemma-3-4b-it-Q8_0.gguf"])

    def test_a_speech_or_vision_repository_is_not_a_cleanup_publisher(self):
        listing = [{"id": "ggml-org/parakeet-GGUF"},
                   {"id": "ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF"},
                   {"id": "ggml-org/SmolVLM2-256M-Video-Instruct-GGUF"},
                   {"id": "ggml-org/Qwen3-8B-Base-GGUF"},
                   {"id": "ggml-org/SmolLM3-3B-GGUF"}]
        with fake_urlopen(listing):
            found = ggml.llm_repos()
        self.assertEqual([r for r in found if r.startswith("ggml-org/Smol")],
                         ["ggml-org/SmolLM3-3B-GGUF"])
        self.assertNotIn("ggml-org/parakeet-GGUF", found)
        self.assertNotIn("ggml-org/Qwen3-8B-Base-GGUF", found)

    def test_a_publisher_is_not_dropped_for_a_word_it_happens_to_contain(self):
        # The skip marks are matched as plain substrings, and an unanchored
        # "test-" is also inside "Latest-".
        self.assertTrue(ggml.can_clean("ggml-org/Qwen3-Latest-GGUF"))
        self.assertFalse(ggml.can_clean("ggml-org/test-model-router-download"))

    def test_a_base_model_beside_its_tuned_twin_is_dropped(self):
        # Gemma names the base model after the tuned one with the `-it` taken
        # out, so the two sit next to each other and the wrong one answers a
        # cleanup prompt by carrying on writing the transcript.
        listing = [{"id": "ggml-org/gemma-4-E2B-GGUF"},
                   {"id": "ggml-org/gemma-4-E2B-it-GGUF"},
                   {"id": "ggml-org/Qwen3-0.6B-GGUF"}]
        with fake_urlopen(listing):
            found = ggml.llm_repos()
        self.assertNotIn("ggml-org/gemma-4-E2B-GGUF", found)
        self.assertIn("ggml-org/gemma-4-E2B-it-GGUF", found)
        # Nothing named it, so nothing says it is the wrong half of a pair.
        self.assertIn("ggml-org/Qwen3-0.6B-GGUF", found)

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
    import http.server, socketserver, sys, threading, time

    args = sys.argv[1:]

    def opt(name, default=""):
        return args[args.index(name) + 1] if name in args else default

    time.sleep(float(opt("--wait", "0")))

    # After the sleep, so that --wait plus --die is a program that runs for a
    # while and then crashes, the way a bad model dies mid-load.
    if "--die" in args:
        print("could not load model: no such file")
        sys.exit(2)

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

    # The same server, without the reverse lookup of the address it bound.
    # http.server asks the resolver for the name behind 127.0.0.1 in between
    # binding and listening; on a Mac nothing answers and the call sits in a
    # timeout for half a minute, all of it with the port still closed and a
    # start waiting on it.
    class Bound(http.server.HTTPServer):
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    server = Bound((opt("--host"), int(opt("--port"))), Handler)
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

    def count_ports(self):
        """Record every port handed to a launch, one per attempt."""
        ports = []
        real = ggml._free_port
        self.patch_attr(ggml, "_free_port",
                        lambda: ports.append(real()) or ports[-1])
        return ports

    def test_a_program_that_dies_reports_what_it_printed(self):
        server = self.server(extra=["--die"])
        with self.assertRaises(ggml.LocalError) as caught:
            server.serve()
        self.assertIn("no such file", str(caught.exception))
        self.assertFalse(server.running)

    def test_an_early_death_that_never_listened_is_retried(self):
        # A child that loses the bind race fails and exits at once, and which
        # port was lost cannot be told from the log: the shape of the failure,
        # not its wording, is what earns another port.
        ports = self.count_ports()
        server = self.server(extra=["--die"])
        with self.assertRaises(ggml.LocalError):
            server.serve()
        self.assertEqual(len(ports), 3)

    def test_a_late_crash_is_not_retried(self):
        # A program that ran for a while before dying was not a bind race: it
        # would die the same way on any port.
        self.patch_attr(ggml, "EARLY_EXIT_WINDOW", 0.2)
        ports = self.count_ports()
        server = self.server(extra=["--wait", "0.5", "--die"])
        with self.assertRaises(ggml.LocalError) as caught:
            server.serve()
        self.assertEqual(len(ports), 1)
        self.assertIn("no such file", str(caught.exception))

    def test_an_open_port_with_a_dead_child_is_not_ready(self):
        # Another process winning the bind race leaves the port open while our
        # child exits: the open port alone must not be read as ready.
        polls = iter([None, 2])
        proc = mock.Mock()
        proc.poll = lambda: next(polls)
        self.patch_attr(ggml, "_listening", lambda port: True)
        reason, listened = self.server()._wait_ready(proc, 1)
        self.assertEqual(reason, "exited")
        self.assertFalse(listened)

    def test_an_open_port_with_a_child_that_outlives_it_a_beat_is_ready(self):
        proc = mock.Mock()
        proc.poll = lambda: None
        self.patch_attr(ggml, "_listening", lambda port: True)
        reason, listened = self.server()._wait_ready(proc, 1)
        self.assertEqual(reason, "ready")
        self.assertTrue(listened)

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

    def test_an_unverifiable_pid_is_kept_for_a_later_sweep(self):
        # Could not be checked is not the same as known stale: dropping the
        # file here would lose track of a server that may still hold a model.
        server = self.server()
        server._remember(4242)
        with mock.patch.object(server, "_is_ours", return_value=None):
            self.assertFalse(server.sweep())
        self.assertTrue(server._pid_file().exists())

    def test_a_pid_known_stale_is_forgotten(self):
        server = self.server()
        server._remember(4242)
        with mock.patch.object(server, "_is_ours", return_value=False):
            self.assertFalse(server.sweep())
        self.assertFalse(server._pid_file().exists())

    def test_the_pid_file_goes_once_the_kill_was_attempted(self):
        server = self.server()
        server._remember(4242)
        with mock.patch.object(server, "_is_ours", return_value=True):
            with mock.patch.object(ggml.os, "kill") as kill:
                self.assertTrue(server.sweep())
        kill.assert_called_once_with(4242, signal.SIGTERM)
        self.assertFalse(server._pid_file().exists())

    def test_forget_leaves_a_pid_file_that_is_no_longer_ours(self):
        server = self.server()
        server._remember(111)
        # A Dikte started after us wrote its own server's pid over ours;
        # removing the file would hide that server from every future sweep.
        server._pid_file().write_text("222")
        server._forget()
        self.assertEqual(server._pid_file().read_text(), "222")

    def test_forget_removes_the_pid_it_remembered(self):
        server = self.server()
        server._remember(111)
        server._forget()
        self.assertFalse(server._pid_file().exists())

    def test_stop_waits_out_a_start_in_flight_and_kills_it(self):
        # stop_all on quit must not slide past a launch that is mid-load: the
        # child would then survive Dikte with nothing left that knows its pid.
        children = []
        real = ggml.subprocess.Popen

        def popen(*args, **kwargs):
            proc = real(*args, **kwargs)
            children.append(proc)
            return proc

        self.patch_attr(ggml.subprocess, "Popen", popen)
        server = self.server(extra=["--wait", "0.5"])
        thread = threading.Thread(target=server.serve)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not children and time.monotonic() < deadline:
                time.sleep(0.01)     # until the launch is truly in flight
            server.stop()
        finally:
            thread.join(timeout=10)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertFalse(server.running)

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


# --- Windows ----------------------------------------------------------------


class WindowsAssets(Local):
    """Which archive a Windows machine is handed."""

    def setUp(self):
        super().setUp()
        self.patch_attr(sys, "platform", "win32")
        self.patch_attr(ggml, "_arch", lambda: "x64")

    def test_whisper_prefers_the_blas_build(self):
        # On a plain CPU it transcribes about twice as fast as the stock one.
        self.assertEqual(ggml._wanted_assets(ggml.WHISPER),
                         ("whisper-blas-bin-x64.zip", "whisper-bin-x64.zip"))

    def test_llama_takes_the_vulkan_build_when_there_is_a_loader(self):
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        self.assertEqual(ggml._wanted_assets(ggml.LLAMA),
                         ("bin-win-vulkan-x64.zip", "bin-win-cpu-x64.zip"))

    def test_llama_falls_back_to_the_cpu_build_without_one(self):
        self.patch_attr(ggml, "_has_vulkan", lambda: False)
        self.assertEqual(ggml._wanted_assets(ggml.LLAMA),
                         ("bin-win-cpu-x64.zip",))

    def test_an_arm_machine_is_not_handed_the_x64_build(self):
        self.patch_attr(ggml, "_arch", lambda: "arm64")
        self.patch_attr(ggml, "_has_vulkan", lambda: True)
        self.assertEqual(ggml._wanted_assets(ggml.LLAMA),
                         ("bin-win-cpu-arm64.zip",))

    def test_an_arm_machine_is_handed_the_x64_whisper_anyway(self):
        """whisper.cpp publishes no arm64 build for Windows: the release has
        Win32 and x64 and nothing else, so emulated is the only local option
        a Snapdragon has. Pinned here so that a release which does start
        publishing one is noticed rather than quietly ignored."""
        self.patch_attr(ggml, "_arch", lambda: "arm64")
        self.assertEqual(ggml._wanted_assets(ggml.WHISPER),
                         ("whisper-blas-bin-x64.zip", "whisper-bin-x64.zip"))


class InstallOnWindows(Local):
    """The Windows releases are zips, and the binary carries .exe."""

    def setUp(self):
        super().setUp()
        self.patch_attr(sys, "platform", "win32")
        self.patch_attr(ggml, "_arch", lambda: "x64")
        # shutil.which cannot be allowed through to the real one: standing on
        # win32 from another system, Python 3.12's Windows branch of which()
        # reaches for the nt module that is not there.
        self.enterContext(mock.patch("shutil.which", return_value=None))
        self.archive = zipball({
            "Release/whisper-server.exe": b"MZ not really a program",
            "Release/whisper.dll": b"not really a library",
        })

    def release(self, *names):
        digest = hashlib.sha256(self.archive)
        return {"tag_name": "v1.9.1", "assets": [
            {"name": name, "browser_download_url": f"https://example.invalid/{name}",
             "size": 10, "digest": "sha256:" + digest.hexdigest()}
            for name in names]}

    def test_the_zip_lands_and_the_exe_inside_it_is_found(self):
        with serving(self.release("whisper-blas-bin-x64.zip"), self.archive):
            path = ggml.install_program(ggml.WHISPER)
        self.assertTrue(path.endswith("whisper-server.exe"))
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(path),
                                                    "whisper.dll")))
        self.assertEqual(ggml.installed_program(ggml.WHISPER), path)

    def test_the_blas_build_is_the_one_fetched_when_both_are_offered(self):
        listing = self.release("whisper-bin-x64.zip", "whisper-blas-bin-x64.zip")
        with serving(listing, self.archive) as calls:
            ggml.install_program(ggml.WHISPER)
        urls = [call.args[0].full_url for call in calls.call_args_list]
        self.assertTrue(urls[1].endswith("whisper-blas-bin-x64.zip"))

    def test_a_release_with_nothing_for_windows_says_so(self):
        with fake_urlopen(json_body(self.release("whisper-bin-ubuntu-x64.tar.gz"))):
            with self.assertRaises(ggml.LocalError) as caught:
                ggml.install_program(ggml.WHISPER)
        self.assertIn("this machine", str(caught.exception))


class WindowsOwnership(Local):
    """Sweeping on Windows goes by the executable's full path, never its name:
    the base name alone is anyone's whisper-server.exe, and this answer decides
    what gets killed."""

    def setUp(self):
        super().setUp()
        self.patch_attr(sys, "platform", "win32")
        # See InstallOnWindows: the real which() on win32 wants the nt module.
        self.enterContext(mock.patch("shutil.which", return_value=None))
        self.made = ggml.Server(ggml.WHISPER, lambda values: [], {"binary": ""})

    def image(self, path):
        self.patch_attr(ggml, "_win_image_name", lambda pid: path)

    def test_a_binary_under_our_bin_directory_is_ours(self):
        self.image(str(ggml.BIN_DIR / "whisper" / "v1.9.1" / "whisper-server.exe"))
        self.assertIs(self.made._is_ours(1234), True)

    def test_the_configured_binary_is_ours_wherever_it_lives(self):
        mine = self.path("elsewhere", "whisper-server.exe")
        mine.parent.mkdir(parents=True)
        mine.write_bytes(b"MZ")
        mine.chmod(0o755)
        self.made._settings["binary"] = str(mine)
        self.image(str(mine.resolve()))
        self.assertIs(self.made._is_ours(1234), True)

    def test_the_same_name_somewhere_else_is_not_ours(self):
        self.image(str(self.path("theirs", "whisper-server.exe")))
        with mock.patch("shutil.which", return_value=None):
            self.assertIs(self.made._is_ours(1234), False)

    def test_a_process_that_cannot_be_read_is_no_verdict(self):
        # OpenProcess answering nothing covers both "gone" and "not readable
        # from here", and only one of those makes the pid file safe to drop.
        self.image("")
        self.assertIsNone(self.made._is_ours(1234))


class Machine(Local):
    """What this machine can hold, and what that makes worth pointing at."""

    def test_the_memory_is_read_the_way_each_system_reports_it(self):
        # Linux and most Macs answer through sysconf.
        with mock.patch.object(ggml.os, "sysconf", lambda name:
                               4096 if name == "SC_PAGE_SIZE" else 4_194_304):
            self.assertEqual(ggml.total_memory(), 16 * ggml.GB)

    def test_a_mac_without_the_page_count_is_asked_for_the_number(self):
        # Not every build of Python on a Mac carries SC_PHYS_PAGES, and a Mac
        # that answered nothing would be a Mac with none of this on it.
        def answer(args, **kwargs):
            self.assertEqual(args, ["sysctl", "-n", "hw.memsize"])
            return mock.Mock(stdout=f"{32 * ggml.GB}\n")

        with mock.patch.object(ggml.os, "sysconf", side_effect=ValueError), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(ggml.subprocess, "run", answer):
            self.assertEqual(ggml.total_memory(), 32 * ggml.GB)

    def test_a_sysconf_that_shrugs_is_an_unknown_machine_and_not_a_tiny_one(self):
        # sysconf answers -1 for a limit it holds to be indeterminate and
        # CPython hands that back rather than raising, so the product came out
        # negative: a 64 GB workstation was told every model past 512 MB was
        # too big for it, and the machine line read "Memory: -4096 B".
        with mock.patch.object(ggml.os, "sysconf", lambda name:
                               4096 if name == "SC_PAGE_SIZE" else -1):
            self.assertEqual(ggml.total_memory(), 0)
        self.assertTrue(ggml.fits(574 << 20, memory=0))

    def test_the_memory_is_read_once_and_kept(self):
        # A list of thirty rows asks seventy times, and on the Mac path the
        # answer comes from a program rather than a library call.
        calls = []
        with mock.patch.object(ggml, "_read_memory",
                               lambda: calls.append(1) or 16 * ggml.GB):
            self.assertEqual(ggml.total_memory(), 16 * ggml.GB)
            self.assertEqual(ggml.total_memory(), 16 * ggml.GB)
        self.assertEqual(len(calls), 1)

    def test_a_system_that_answers_nothing_is_an_unknown_machine(self):
        with mock.patch.object(ggml.os, "sysconf", side_effect=ValueError), \
                mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(ggml.total_memory(), 0)

    def test_a_mac_is_taken_to_have_a_graphics_interface(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(ggml.accelerator(), "Metal")

    def test_elsewhere_the_vulkan_loader_is_what_says_so(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(ggml.ctypes.util, "find_library",
                                  lambda name: "/usr/lib/libvulkan.so.1"):
            self.assertEqual(ggml.accelerator(), "Vulkan")
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(ggml.ctypes.util, "find_library",
                                  lambda name: None):
            self.assertEqual(ggml.accelerator(), "")

    def test_a_model_is_measured_against_half_the_memory(self):
        self.assertTrue(ggml.fits(2 * ggml.GB, memory=8 * ggml.GB))
        self.assertFalse(ggml.fits(4 * ggml.GB, memory=8 * ggml.GB))

    def test_a_machine_whose_memory_could_not_be_read_holds_anything(self):
        # A wrong "too big" is worse advice than none.
        self.assertTrue(ggml.fits(40 * ggml.GB, memory=0))

    def test_the_smallest_machine_is_not_the_one_where_everything_fits(self):
        # Half of 2 GB less the gigabyte of overhead is nothing, and a budget
        # of nothing used to read as the unknown machine above.
        self.assertFalse(ggml.fits(3 * ggml.GB, memory=2 * ggml.GB))

    def test_a_crowded_machine_is_pointed_at_the_smaller_model(self):
        self.assertEqual(ggml.suggested_whisper(memory=3 * ggml.GB, graphics=""),
                         ggml.SMALL_MACHINE_WHISPER)

    def test_a_card_and_the_memory_for_it_are_pointed_at_the_accurate_one(self):
        self.assertEqual(
            ggml.suggested_whisper(memory=32 * ggml.GB, graphics="Vulkan"),
            ggml.ACCURATE_WHISPER)

    def test_memory_without_a_card_is_pointed_at_the_fast_one(self):
        # Several times the work per second is several times a long wait on a
        # processor, whatever there is room for.
        self.assertEqual(
            ggml.suggested_whisper(memory=32 * ggml.GB, graphics=""),
            ggml.SUGGESTED_WHISPER)

    def test_a_sixteen_gigabyte_machine_counts_as_a_roomy_one(self):
        # What a machine reports is what the firmware and the graphics left
        # of it: 16 GB answers about 15.4, and a threshold written at the
        # number on the box is one no machine ever reaches.
        self.assertEqual(
            ggml.suggested_whisper(memory=int(15.4 * ggml.GB), graphics="Metal"),
            ggml.ACCURATE_WHISPER)

    def test_the_suggestion_that_fits_is_offered_first(self):
        first = ggml.suggested_llm(memory=6 * ggml.GB)[0]
        self.assertTrue(ggml.fits(ggml.SUGGESTED_LLM_SIZE[first],
                                  memory=6 * ggml.GB))
        # Nothing is dropped: what does not fit today fits once something else
        # is closed.
        self.assertEqual(sorted(ggml.suggested_llm(memory=6 * ggml.GB)),
                         sorted(ggml.SUGGESTED_LLM))

    def test_the_wanted_model_wins_when_there_is_room_for_it(self):
        items = [listed("ggml-tiny.bin", 70 << 20),
                 listed("ggml-large-v3-turbo-q5_0.bin", 574 << 20)]
        self.assertEqual(
            ggml.recommended(items, "ggml-large-v3-turbo-q5_0.bin",
                             memory=16 * ggml.GB),
            "ggml-large-v3-turbo-q5_0.bin")

    def test_a_model_too_big_for_the_machine_is_not_recommended(self):
        items = [listed("small.gguf", 1 << 30), listed("huge.gguf", 12 * ggml.GB)]
        self.assertEqual(ggml.recommended(items, "huge.gguf",
                                          memory=8 * ggml.GB), "small.gguf")

    def test_the_full_precision_weights_are_never_the_recommendation(self):
        # Twice the memory and twice the wait for a difference this job
        # cannot see.
        items = [listed("model-Q4_0.gguf", 2 * ggml.GB),
                 listed("model-BF16.gguf", 3 * ggml.GB)]
        self.assertEqual(ggml.recommended(items, memory=32 * ggml.GB),
                         "model-Q4_0.gguf")

    def test_nothing_is_recommended_when_nothing_fits(self):
        self.assertEqual(
            ggml.recommended([listed("huge.gguf", 40 * ggml.GB)],
                             memory=8 * ggml.GB), "")


class Grouping(Local):
    """One group per model, rather than one long list sorted by size."""

    def test_every_spelling_of_a_quantisation_reads_as_its_number(self):
        # One list holds q5_1, Q4_K_M, MXFP4 and BF16, and the number is the
        # whole of what any of them says to somebody choosing a row.
        self.assertEqual(ggml.bit_depth("ggml-small-q5_1.bin"), 5)
        self.assertEqual(ggml.bit_depth("SmolLM3-Q4_K_M.gguf"), 4)
        self.assertEqual(ggml.bit_depth("gpt-oss-20b-MXFP4.gguf"), 4)
        self.assertEqual(ggml.bit_depth("gemma-4-E2B-it-Q8_0.gguf"), 8)
        # bf16 is not f16 read badly.
        self.assertEqual(ggml.bit_depth("gemma-4-E2B-it-BF16.gguf"), 16)
        self.assertEqual(ggml.bit_depth("mmproj-model-f16.gguf"), 16)
        # A whisper file with no mark is the full model, and its name is the
        # one convention here that does not carry the answer.
        self.assertEqual(ggml.bit_depth("ggml-large-v3-turbo.bin"), 0)

    def test_a_quantisation_belongs_to_the_model_it_is_a_copy_of(self):
        self.assertEqual(ggml.whisper_family("ggml-small.en-q5_1.bin"), "small")
        self.assertEqual(ggml.whisper_family("ggml-large-v3-q5_0.bin"),
                         "large-v3")
        self.assertEqual(ggml.whisper_family("ggml-large-v3-turbo.bin"),
                         "large-v3-turbo")
        self.assertEqual(ggml.whisper_family("ggml-medium.en.bin"), "medium")

    def test_turbo_is_a_model_and_not_a_quantisation(self):
        # The last chunk of the name is a quantisation for most of the list
        # and part of the model's name here.
        self.assertEqual(ggml.whisper_family("ggml-large-v3-turbo-q8_0.bin"),
                         "large-v3-turbo")

    def test_the_turbo_files_are_not_scattered_through_the_medium_ones(self):
        # Sorted by size alone, large-v3-turbo-q5_0 lands between the two
        # medium quantisations, half a screen from the model it is a copy of.
        models = [listed("ggml-medium-q5_0.bin", 539 << 20),
                  listed("ggml-large-v3-turbo-q5_0.bin", 574 << 20),
                  listed("ggml-medium-q8_0.bin", 823 << 20),
                  listed("ggml-large-v3-turbo.bin", 1624 << 20)]
        groups = dict(ggml.whisper_groups(models))
        self.assertEqual([i.name for i in groups["large-v3-turbo"]],
                         ["ggml-large-v3-turbo-q5_0.bin",
                          "ggml-large-v3-turbo.bin"])
        self.assertEqual([i.name for i in groups["medium"]],
                         ["ggml-medium-q5_0.bin", "ggml-medium-q8_0.bin"])

    def test_the_smallest_model_comes_first_and_the_english_ones_last(self):
        models = [listed("ggml-small.en-q5_1.bin", 190 << 20),
                  listed("ggml-small-q5_1.bin", 190 << 20),
                  listed("ggml-tiny.bin", 77 << 20)]
        groups = ggml.whisper_groups(models)
        self.assertEqual([family for family, _ in groups], ["tiny", "small"])
        self.assertEqual([i.name for _, group in groups for i in group],
                         ["ggml-tiny.bin", "ggml-small-q5_1.bin",
                          "ggml-small.en-q5_1.bin"])
