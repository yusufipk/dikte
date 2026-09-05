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


# --- what it ended up running on ------------------------------------------


# Trimmed from real logs. The first is this project's own bug report: the
# graphics card is switched on, whisper asked for one, and the build had none
# to give.
WHISPER_CPU = """\
load_backend: loaded CPU backend from /opt/whisper/libggml-cpu-haswell.so
whisper_init_from_file_with_params_no_state: loading model from 'ggml-small.bin'
whisper_init_with_params_no_state: use gpu    = 1
whisper_model_load:          CPU total size =   189.49 MB
whisper_backend_init_gpu: device 0: CPU (type: 0)
whisper_backend_init_gpu: no GPU found
"""

WHISPER_CUDA = """\
load_backend: loaded CUDA backend from /opt/whisper/libggml-cuda.so
load_backend: loaded CPU backend from /opt/whisper/libggml-cpu-haswell.so
whisper_init_with_params_no_state: use gpu    = 1
whisper_model_load:        CUDA0 total size =   189.49 MB
whisper_backend_init_gpu: device 0: NVIDIA GeForce RTX 4070 (type: 1)
whisper_backend_init_gpu: using CUDA0 backend
"""

# A card listed, tried, and refused: whisper says so and carries on without it,
# and the weights stay where they were put. Reading the listing alone would
# report a graphics card that is doing nothing.
WHISPER_GPU_FAILED = """\
load_backend: loaded Vulkan backend from /usr/lib/ggml/libggml-vulkan.so
load_backend: loaded CPU backend from /usr/lib/ggml/libggml-cpu-haswell.so
whisper_model_load:          CPU total size =   189.49 MB
whisper_backend_init_gpu: device 0: Vulkan0 (type: 1)
whisper_backend_init_gpu: found GPU device 0: Vulkan0 (type: 1, cnt: 0)
whisper_backend_init_gpu: failed to initialize Vulkan0 backend
"""

# Both backends in one build. The Vulkan listing is there and is not the one
# that ran, so naming the card out of it would name the wrong device.
WHISPER_MIXED = """\
ggml_vulkan: Found 1 Vulkan devices:
ggml_vulkan: 0 = Intel UHD Graphics 770 (ANV TGL) (anv) | uma: 1
load_backend: loaded CUDA backend from /opt/whisper/libggml-cuda.so
load_backend: loaded Vulkan backend from /opt/whisper/libggml-vulkan.so
load_backend: loaded CPU backend from /opt/whisper/libggml-cpu-haswell.so
  Device 0: NVIDIA GeForce RTX 4070, compute capability 8.9, VMM: yes
whisper_model_load:        CUDA0 total size =   189.49 MB
whisper_backend_init_gpu: device 0: CUDA0 (type: 1)
whisper_backend_init_gpu: using CUDA0 backend
"""

# The same start on a card whisper names only by its slot. The card's own name
# is one line further up, printed by the backend as it enumerates.
WHISPER_VULKAN = """\
ggml_vulkan: Found 1 Vulkan devices:
ggml_vulkan: 0 = AMD Radeon RX 6600 (RADV NAVI23) (radv) | uma: 0 | fp16: dot2
load_backend: loaded Vulkan backend from /usr/lib/ggml/libggml-vulkan.so
load_backend: loaded CPU backend from /usr/lib/ggml/libggml-cpu-haswell.so
whisper_model_load:      Vulkan0 total size =   189.49 MB
whisper_backend_init_gpu: device 0: Vulkan0 (type: 1)
whisper_backend_init_gpu: using Vulkan0 backend
"""

# A whisper built by hand on a Mac: Metal is compiled in rather than loaded, so
# there is no line to read and no honest answer but "it did not say".
WHISPER_QUIET = """\
whisper_init_from_file_with_params_no_state: loading model from 'ggml-base.bin'
whisper_model_load: model size    =  147.37 MB
"""

LLAMA_GPU = """\
load_backend: loaded Vulkan backend from /opt/llama/libggml-vulkan.so
load_backend: loaded CPU backend from /opt/llama/libggml-cpu.so
load_tensors: offloading 28 repeating layers to GPU
load_tensors: offloaded 29/29 layers to GPU
"""

LLAMA_CPU = """\
load_backend: loaded Vulkan backend from /opt/llama/libggml-vulkan.so
load_backend: loaded CPU backend from /opt/llama/libggml-cpu.so
load_tensors: offloaded 0/29 layers to GPU
"""


# A downloaded processor-only build pointed at the system's Vulkan backend
# through GGML_BACKEND_PATH. whisper numbers every device it can see in one
# sequence, so the card is its device 1 while still being Vulkan0.
WHISPER_LENT_BACKEND = """\
load_backend: loaded CPU backend from /data/bin/whisper/libggml-cpu-haswell.so
ggml_vulkan: Found 1 Vulkan devices:
ggml_vulkan: 0 = AMD Radeon RX 6600 (RADV NAVI23) (radv) | uma: 0
load_backend: loaded Vulkan backend from /usr/lib/ggml/libggml-vulkan.so
whisper_model_load:      Vulkan0 total size =   189.49 MB
whisper_backend_init_gpu: device 0: CPU (type: 0)
whisper_backend_init_gpu: device 1: Vulkan0 (type: 1)
whisper_backend_init_gpu: found GPU device 1: Vulkan0 (type: 1, cnt: 0)
whisper_backend_init_gpu: using Vulkan0 backend
"""

# Two cards, and the one that ran is not the one in the slot the handle names.
# Reading whisper's listing by the handle's digit would name the other card.
WHISPER_TWO_CARDS = """\
ggml_vulkan: Found 1 Vulkan devices:
ggml_vulkan: 0 = AMD Radeon RX 6600 (RADV NAVI23) (radv) | uma: 0
load_backend: loaded CUDA backend from /opt/whisper/libggml-cuda.so
load_backend: loaded Vulkan backend from /opt/whisper/libggml-vulkan.so
load_backend: loaded CPU backend from /opt/whisper/libggml-cpu-haswell.so
whisper_model_load:      Vulkan0 total size =   189.49 MB
whisper_backend_init_gpu: device 0: NVIDIA GeForce RTX 4070 (type: 1)
whisper_backend_init_gpu: device 1: Vulkan0 (type: 1)
whisper_backend_init_gpu: using Vulkan0 backend
"""


class WhatItRunsOn(Local):
    """Reading the backend back out of the log the server wrote."""

    def log(self, text):
        path = self.path("server.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def read(self, program, text):
        return ggml._read_accel(program, self.log(text))

    def test_a_card_that_was_asked_for_and_not_found_is_the_processor(self):
        accel = self.read(ggml.WHISPER, WHISPER_CPU)
        self.assertEqual(accel.backend, "CPU")
        self.assertEqual(ggml.accel_kind(accel), "cpu")

    def test_a_build_with_nothing_but_a_processor_backend_says_so(self):
        self.assertTrue(ggml.cpu_only_build(self.read(ggml.WHISPER, WHISPER_CPU)))
        self.assertFalse(ggml.cpu_only_build(self.read(ggml.WHISPER, WHISPER_CUDA)))

    def test_a_card_that_was_found_is_named(self):
        accel = self.read(ggml.WHISPER, WHISPER_CUDA)
        self.assertEqual(accel.backend, "CUDA")
        self.assertEqual(accel.device, "NVIDIA GeForce RTX 4070")
        self.assertEqual(ggml.accel_kind(accel), "gpu")
        self.assertEqual(ggml.accel_detail(accel),
                         "CUDA, NVIDIA GeForce RTX 4070")

    def test_a_card_named_only_by_its_slot_is_looked_up(self):
        accel = self.read(ggml.WHISPER, WHISPER_VULKAN)
        self.assertEqual(accel.backend, "Vulkan")
        # "Vulkan0" says which slot; the point of the line is which card.
        self.assertEqual(accel.device, "AMD Radeon RX 6600 (RADV NAVI23)")

    def test_the_driver_behind_the_card_is_not_part_of_its_name(self):
        # "(radv)" is how it is reached; "(RADV NAVI23)" is what it is called.
        self.assertNotIn("(radv)",
                         self.read(ggml.WHISPER, WHISPER_VULKAN).device)

    def test_a_card_numbered_one_way_and_handled_another_is_still_named(self):
        accel = self.read(ggml.WHISPER, WHISPER_LENT_BACKEND)
        self.assertEqual(accel.backend, "Vulkan")
        self.assertEqual(accel.device, "AMD Radeon RX 6600 (RADV NAVI23)")

    def test_the_card_named_is_the_one_the_handle_belongs_to(self):
        # whisper's device 0 is the other card. The handle is Vulkan0, and
        # Vulkan's own device 0 is the AMD one.
        accel = self.read(ggml.WHISPER, WHISPER_TWO_CARDS)
        self.assertEqual(accel.device, "AMD Radeon RX 6600 (RADV NAVI23)")
        self.assertNotIn("NVIDIA", ggml.accel_detail(accel))

    def test_a_card_that_failed_to_start_is_not_a_card_in_use(self):
        # It was listed, it was tried, it did not work, and whisper went on
        # without it. The listing alone would have called this a graphics card.
        accel = self.read(ggml.WHISPER, WHISPER_GPU_FAILED)
        self.assertEqual(accel.backend, "CPU")
        self.assertEqual(ggml.accel_kind(accel), "cpu")

    def test_the_card_named_is_the_one_that_ran(self):
        accel = self.read(ggml.WHISPER, WHISPER_MIXED)
        self.assertEqual(accel.backend, "CUDA")
        self.assertEqual(accel.device, "NVIDIA GeForce RTX 4070")
        self.assertNotIn("Intel", ggml.accel_detail(accel))

    def test_a_slot_number_is_not_a_name(self):
        # "Vulkan0" says which slot; with no listing to look it up in, saying
        # nothing beats saying that.
        self.assertEqual(self.read(ggml.LLAMA, LLAMA_GPU).device, "")

    def test_a_log_that_says_nothing_is_not_guessed_at(self):
        accel = self.read(ggml.WHISPER, WHISPER_QUIET)
        self.assertEqual(accel.backend, "")
        self.assertEqual(ggml.accel_kind(accel), "unknown")

    def test_a_log_that_is_not_there_is_not_guessed_at_either(self):
        self.assertEqual(ggml._read_accel(ggml.WHISPER, self.path("gone.log")),
                         ggml.NO_ACCEL)

    def test_the_layers_llama_offloaded_are_read_back(self):
        accel = self.read(ggml.LLAMA, LLAMA_GPU)
        self.assertEqual(accel.backend, "Vulkan")
        self.assertEqual(accel.layers, "29/29")
        self.assertEqual(ggml.accel_detail(accel), "Vulkan, 29/29 layers")

    def test_a_llama_that_offloaded_nothing_is_on_the_processor(self):
        accel = self.read(ggml.LLAMA, LLAMA_CPU)
        self.assertEqual(accel.backend, "CPU")
        self.assertEqual(ggml.accel_kind(accel), "cpu")
        # The build could have used the card; this run did not.
        self.assertFalse(ggml.cpu_only_build(accel))

    def test_the_processor_is_not_named_twice(self):
        # whisper prints CPU as the backend and as the device, and saying it
        # twice reads like two different things.
        self.assertEqual(ggml.accel_detail(self.read(ggml.WHISPER, WHISPER_CPU)),
                         "CPU")

    def test_which_copy_is_running_decides_what_advice_is_worth_giving(self):
        mine = ggml.BIN_DIR / "whisper" / "b1" / "whisper-server"
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text("#!/bin/sh\n")
        self.assertTrue(ggml.is_downloaded(str(mine)))
        self.assertFalse(ggml.is_downloaded("/usr/bin/whisper-server"))
        self.assertFalse(ggml.is_downloaded(""))

    def test_nothing_is_running_is_not_a_backend(self):
        self.assertEqual(ggml.accel_kind({"running": False, "backend": "CUDA"}),
                         "off")


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

    # The startup chatter a real server prints before it binds, so that the
    # log has something for _read_accel to find. Flushed, because stdout here
    # is a file and nothing would reach it before the port opened.
    if "--backend" in args:
        print("load_backend: loaded " + opt("--backend") + " backend from /x.so",
              flush=True)
        print("whisper_backend_init_gpu: device 0: Test Card (type: 1)",
              flush=True)
        # The line that says one of them worked, which is the one read back.
        print("whisper_backend_init_gpu: using " + opt("--backend") + "0 backend",
              flush=True)

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

    def test_nothing_started_is_a_state_saying_so(self):
        state = self.server().state()
        self.assertFalse(state["running"])
        self.assertEqual(ggml.accel_kind(state), "off")

    def test_a_running_server_says_what_it_settled_on(self):
        server = self.server(extra=["--backend", "CUDA"], gpu=True)
        server.serve()
        state = server.state()
        self.assertTrue(state["running"])
        self.assertIn(f":{state['port']}/v1", server.base_url())
        self.assertEqual(state["backend"], "CUDA")
        self.assertEqual(state["device"], "Test Card")
        self.assertTrue(state["gpu_wanted"])
        self.assertEqual(ggml.accel_kind(state), "gpu")

    def test_a_setting_changed_mid_start_does_not_rename_what_is_running(self):
        # A save that lands while the model is being read in finds no process
        # to stop, so it changes the settings under a start already in flight.
        # The line must name the model that is loaded, not the one that will be.
        server = self.server(model="first")
        launch = server._launch

        def during(settings):
            result = launch(settings)
            server.configure(model="second")
            return result

        self.patch_attr(server, "_launch", during)
        server.serve()
        self.assertEqual(server.state()["model"], "first")
        self.assertEqual(server.settings()["model"], "second")

    def test_stopping_takes_the_backend_with_it(self):
        server = self.server(extra=["--backend", "CUDA"])
        server.serve()
        server.stop()
        self.assertEqual(server.state()["backend"], "")

    def test_a_server_that_announced_nothing_is_not_guessed_at(self):
        server = self.server()
        server.serve()
        self.assertEqual(ggml.accel_kind(server.state()), "unknown")

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
