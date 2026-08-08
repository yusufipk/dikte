"""Where the settings and the data are kept, per system.

Its own file because the answer has to be one answer: config.py and ggml.py
both need it, and when each worked it out for itself only one of them knew
about macOS.
"""

import os
import unittest
from unittest import mock

import config as cfg
import ggml
import paths


class Directories(unittest.TestCase):
    def test_linux_keeps_them_apart_and_follows_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/c",
                                          "XDG_DATA_HOME": "/d"}):
            config_dir, data_dir = paths.directories("linux")
        self.assertEqual(str(config_dir), "/c/dikte")
        self.assertEqual(str(data_dir), "/d/dikte")

    def test_linux_without_the_variables_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config_dir, data_dir = paths.directories("linux")
        self.assertTrue(str(config_dir).endswith("/.config/dikte"))
        self.assertTrue(str(data_dir).endswith("/.local/share/dikte"))

    def test_a_mac_keeps_both_in_application_support(self):
        config_dir, data_dir = paths.directories("darwin")
        self.assertEqual(config_dir, data_dir)
        self.assertTrue(str(config_dir).endswith("/Library/Application Support/Dikte"))

    def test_a_mac_does_not_read_the_xdg_variables(self):
        """A Mac with them set from some other tool still stores in one place."""
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/c"}):
            config_dir, _ = paths.directories("darwin")
        self.assertNotIn("/c", str(config_dir))


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
