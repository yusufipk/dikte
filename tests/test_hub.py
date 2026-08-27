"""What GitHub and Hugging Face are asked, and what is believed of the answer."""

import json

from dikte import hub
from dikte import paths
from tests.support import DikteTest, fake_urlopen, http_error, url_error

RELEASE = {
    "tag_name": "v1.9.1",
    "assets": [
        {"name": "whisper-bin-ubuntu-x64.tar.gz",
         "browser_download_url": "https://example.invalid/ubuntu-x64.tar.gz",
         "size": 9379235, "digest": "sha256:" + "a" * 64},
        {"name": "whisper-bin-x64.zip",
         "browser_download_url": "https://example.invalid/win.zip",
         "size": 100, "digest": "sha256:" + "b" * 64},
        {"name": "no-url-here.zip", "size": 1},
    ],
}

TREE = [
    {"type": "file", "path": ".gitattributes", "size": 1477},
    {"type": "file", "path": "ggml-base.bin", "size": 147951465,
     "lfs": {"oid": "c" * 64, "size": 147951465}},
    {"type": "directory", "path": "extra"},
    {"type": "file", "path": "extra/ggml-tiny.bin", "size": 77691713,
     "lfs": {"oid": "d" * 64, "size": 77691713}},
]

MODELS = [
    {"id": "ggml-org/gemma-3-4b-it-GGUF", "downloads": 44606,
     "lastModified": "2026-07-01T00:00:00.000Z"},
    {"id": "ggml-org/gpt-oss-20b-GGUF", "downloads": 47975},
    {"noid": True},
]


class Releases(DikteTest):
    def setUp(self):
        super().setUp()
        self.patch_attr(hub, "CACHE_DIR", self.path("cache"))

    def test_the_tag_and_the_assets_come_back(self):
        with fake_urlopen(RELEASE) as calls:
            tag, assets = hub.release("ggml-org/whisper.cpp")
        self.assertEqual(tag, "v1.9.1")
        self.assertEqual([a.name for a in assets],
                         ["whisper-bin-ubuntu-x64.tar.gz", "whisper-bin-x64.zip"])
        self.assertEqual(calls[0].full_url,
                         "https://api.github.com/repos/ggml-org/whisper.cpp/"
                         "releases/latest")

    def test_the_sha256_prefix_is_dropped(self):
        with fake_urlopen(RELEASE):
            _, assets = hub.release("ggml-org/whisper.cpp")
        self.assertEqual(assets[0].sha256, "a" * 64)

    def test_a_tag_asks_for_that_tag(self):
        with fake_urlopen(RELEASE) as calls:
            hub.release("ggml-org/whisper.cpp", "v1.9.1")
        self.assertTrue(calls[0].full_url.endswith("/releases/tags/v1.9.1"))

    def test_a_release_with_no_assets_is_an_error(self):
        with fake_urlopen({"tag_name": "v1", "assets": []}):
            with self.assertRaises(hub.HubError):
                hub.release("ggml-org/whisper.cpp")

    def test_the_second_call_asks_nobody(self):
        with fake_urlopen(RELEASE) as calls:
            hub.release("ggml-org/whisper.cpp")
            hub.release("ggml-org/whisper.cpp")
        self.assertEqual(len(calls), 1)

    def test_a_refresh_asks_again(self):
        with fake_urlopen(RELEASE) as calls:
            hub.release("ggml-org/whisper.cpp")
            hub.release("ggml-org/whisper.cpp", refresh=True)
        self.assertEqual(len(calls), 2)

    def test_an_old_cache_beats_no_answer(self):
        with fake_urlopen(RELEASE):
            hub.release("ggml-org/whisper.cpp")
        # Old enough that it would normally be fetched again, and no network
        # to fetch it with.
        for path in self.path("cache").iterdir():
            os_utime(path)
        with fake_urlopen(url_error()):
            tag, assets = hub.release("ggml-org/whisper.cpp")
        self.assertEqual(tag, "v1.9.1")
        self.assertEqual(len(assets), 2)

    def test_no_cache_and_no_network_says_so(self):
        with fake_urlopen(url_error("no route to host")):
            with self.assertRaises(hub.HubError) as caught:
                hub.release("ggml-org/whisper.cpp")
        self.assertIn("api.github.com", str(caught.exception))

    def test_an_http_error_names_the_host_and_the_code(self):
        with fake_urlopen(http_error(404, "nope")):
            with self.assertRaises(hub.HubError) as caught:
                hub.release("ggml-org/nothing")
        self.assertIn("404", str(caught.exception))


class Files(DikteTest):
    def setUp(self):
        super().setUp()
        self.patch_attr(hub, "CACHE_DIR", self.path("cache"))

    def test_directories_are_left_out(self):
        with fake_urlopen(TREE):
            files = hub.files("ggerganov/whisper.cpp")
        self.assertEqual([f.name for f in files],
                         [".gitattributes", "ggml-base.bin", "extra/ggml-tiny.bin"])

    def test_the_url_is_the_one_that_serves_the_bytes(self):
        with fake_urlopen(TREE):
            files = hub.files("ggerganov/whisper.cpp")
        self.assertEqual(
            files[1].url,
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin")

    def test_the_lfs_object_id_is_the_checksum(self):
        with fake_urlopen(TREE):
            files = hub.files("ggerganov/whisper.cpp")
        self.assertEqual(files[1].sha256, "c" * 64)
        self.assertEqual(files[1].size, 147951465)

    def test_a_file_outside_lfs_has_no_checksum(self):
        with fake_urlopen(TREE):
            files = hub.files("ggerganov/whisper.cpp")
        self.assertEqual(files[0].sha256, "")

    def test_an_answer_that_is_not_a_list_is_an_error(self):
        with fake_urlopen({"error": "Invalid username or password."}):
            with self.assertRaises(hub.HubError):
                hub.files("ggml-org/whisper.cpp")


class Repos(DikteTest):
    def setUp(self):
        super().setUp()
        self.patch_attr(hub, "CACHE_DIR", self.path("cache"))

    def test_it_asks_for_one_author_and_for_gguf(self):
        with fake_urlopen(MODELS) as calls:
            found = hub.repos(author="ggml-org")
        self.assertIn("author=ggml-org", calls[0].full_url)
        self.assertIn("filter=gguf", calls[0].full_url)
        self.assertEqual([r.id for r in found],
                         ["ggml-org/gemma-3-4b-it-GGUF", "ggml-org/gpt-oss-20b-GGUF"])

    def test_a_missing_download_count_is_zero(self):
        with fake_urlopen(MODELS):
            found = hub.repos(author="ggml-org")
        self.assertEqual(found[0].downloads, 44606)
        self.assertEqual(found[1].updated, "")


def os_utime(path):
    """Backdate a cache file past its time to live."""
    import os
    import time
    old = time.time() - hub.CACHE_TTL - 60
    os.utime(path, (old, old))


class CacheLocation(DikteTest):
    """Resolved at import, like every other path constant."""

    def test_the_cache_lives_in_the_system_cache_directory(self):
        # One answer for both, the same way ggml and config share DATA_DIR:
        # hub asked paths once, at import, and kept what it was told.
        self.assertEqual(hub.CACHE_DIR, paths.cache_dir())


class CacheOnDisk(DikteTest):
    def setUp(self):
        super().setUp()
        self.patch_attr(hub, "CACHE_DIR", self.path("cache"))

    def test_what_is_stored_is_what_came_back(self):
        with fake_urlopen(RELEASE):
            hub.release("ggml-org/whisper.cpp")
        stored = [json.loads(p.read_text()) for p in self.path("cache").iterdir()]
        self.assertEqual(stored[0]["tag_name"], "v1.9.1")

    def test_a_cache_that_cannot_be_written_is_not_a_failure(self):
        self.patch_attr(hub, "CACHE_DIR", self.path("nope", "deeper"))
        self.path("nope").write_text("a file where a directory would go")
        with fake_urlopen(RELEASE):
            tag, _ = hub.release("ggml-org/whisper.cpp")
        self.assertEqual(tag, "v1.9.1")
