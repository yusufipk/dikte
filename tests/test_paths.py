"""Where the settings and the data are kept, per system.

Its own file because the answer has to be one answer: config.py and ggml.py
both need it, and when each worked it out for itself only one of them knew
about macOS.
"""

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from dikte import config as cfg
from dikte import ggml
from dikte import paths


class Directories(unittest.TestCase):
    """Spelled with forward slashes throughout.

    A backslash separates on Windows only, and every one of these runs on all
    three systems: `as_posix()` is the one spelling they can all be read in.
    """

    def test_linux_keeps_them_apart_and_follows_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/c",
                                          "XDG_DATA_HOME": "/d"}):
            config_dir, data_dir = paths.directories("linux")
        self.assertEqual(config_dir.as_posix(), "/c/dikte")
        self.assertEqual(data_dir.as_posix(), "/d/dikte")

    def test_linux_without_the_variables_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config_dir, data_dir = paths.directories("linux")
        self.assertTrue(config_dir.as_posix().endswith("/.config/dikte"))
        self.assertTrue(data_dir.as_posix().endswith("/.local/share/dikte"))

    def test_a_mac_keeps_both_in_application_support(self):
        config_dir, data_dir = paths.directories("darwin")
        self.assertEqual(config_dir, data_dir)
        self.assertTrue(config_dir.as_posix()
                        .endswith("/Library/Application Support/Dikte"))

    def test_a_mac_does_not_read_the_xdg_variables(self):
        """A Mac with them set from some other tool still stores in one place."""
        # Something no temporary directory can be called: the home this runs
        # under is a mkdtemp path, and a two-letter needle matched the "/c" in
        # somebody's TMPDIR rather than the variable being read.
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg-elsewhere"}):
            config_dir, _ = paths.directories("darwin")
        self.assertNotIn("xdg-elsewhere", config_dir.as_posix())

    def test_windows_keeps_the_models_out_of_the_roaming_profile(self):
        """Settings roam with the account; several gigabytes must not."""
        with mock.patch.dict(os.environ, {"APPDATA": "C:/roam",
                                          "LOCALAPPDATA": "C:/local"}):
            config_dir, data_dir = paths.directories("win32")
        self.assertEqual(config_dir.as_posix(), "C:/roam/Dikte")
        self.assertEqual(data_dir.as_posix(), "C:/local/Dikte")

    def test_windows_without_the_variables_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config_dir, data_dir = paths.directories("win32")
        self.assertTrue(config_dir.as_posix().endswith("/AppData/Roaming/Dikte"))
        self.assertTrue(data_dir.as_posix().endswith("/AppData/Local/Dikte"))


class CacheDir(unittest.TestCase):
    """The third place: files whose whole point is that they can be lost."""

    def test_linux_follows_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/k"}):
            self.assertEqual(paths.cache_dir("linux").as_posix(), "/k/dikte")

    def test_linux_without_the_variable_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(paths.cache_dir("linux").as_posix()
                            .endswith("/.cache/dikte"))

    def test_a_mac_caches_under_library_caches(self):
        """Where Time Machine already knows not to look."""
        self.assertTrue(paths.cache_dir("darwin").as_posix()
                        .endswith("/Library/Caches/Dikte"))

    def test_windows_caches_outside_the_roaming_profile(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/local"}):
            self.assertEqual(paths.cache_dir("win32").as_posix(),
                             "C:/local/Dikte/cache")

    def test_windows_without_the_variable_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(paths.cache_dir("win32").as_posix()
                            .endswith("/AppData/Local/Dikte/cache"))


class ScratchDir(unittest.TestCase):
    """Intermediate audio stays out of a /tmp that may be held in RAM."""

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="dikte-test-cache-")
        self.addCleanup(shutil.rmtree, self.cache, True)

    def test_linux_puts_it_under_the_cache(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache,
                                          "TMPDIR": ""}):
            work = paths.scratch_dir("linux")
        self.assertEqual(work, os.path.join(self.cache, "dikte", "work"))
        self.assertTrue(os.path.isdir(work))

    def test_a_tmpdir_set_on_purpose_wins(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache,
                                          "TMPDIR": "/somewhere"}):
            self.assertIsNone(paths.scratch_dir("linux"))

    def test_elsewhere_tempfile_decides(self):
        self.assertIsNone(paths.scratch_dir("darwin"))
        self.assertIsNone(paths.scratch_dir("win32"))

    def test_a_killed_run_is_swept_and_a_live_one_is_not(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache,
                                          "TMPDIR": ""}):
            work = paths.scratch_dir("linux")
            stale = os.path.join(work, "dikte-file-old")
            live = os.path.join(work, "dikte-file-new")
            os.mkdir(stale)
            os.mkdir(live)
            old = time.time() - paths.STALE_SCRATCH_SECONDS - 60
            os.utime(stale, (old, old))
            paths.scratch_dir("linux")
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.isdir(live))


class OnePlace(unittest.TestCase):
    """The programs and the models go where everything else goes.

    ggml.py used to read XDG_DATA_HOME itself, which is right on Linux and
    wrong on a Mac: the settings and the dictations went to ~/Library while
    several gigabytes of models went to ~/.local/share, where no Mac user looks
    and where `uninstall.sh --purge` would never have found them.
    """

    def test_the_models_live_under_the_data_directory(self):
        self.assertEqual(ggml.DATA_DIR, paths.DATA_DIR)
        self.assertEqual(ggml.MODELS_DIR.parent, paths.DATA_DIR)
        self.assertEqual(ggml.BIN_DIR.parent, paths.DATA_DIR)

    def test_config_and_ggml_cannot_disagree(self):
        self.assertEqual(cfg.DATA_DIR, ggml.DATA_DIR)


if __name__ == "__main__":
    unittest.main()
