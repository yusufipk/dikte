"""Homebrew-owned Whisper discovery for apps launched outside a shell."""

import contextlib
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from dikte import ggml


class HomebrewDiscovery(unittest.TestCase):
    @contextlib.contextmanager
    def machine(self, arch="arm64", platform="darwin", files=(), executable=True,
                path=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "platform", platform))
            stack.enter_context(mock.patch.object(ggml.platform, "machine",
                                                   return_value=arch))
            stack.enter_context(mock.patch.object(ggml.shutil, "which",
                                                   return_value=path))
            stack.enter_context(mock.patch.object(ggml, "installed_program",
                                                   return_value="/managed/whisper-server"))
            stack.enter_context(mock.patch.object(os.path, "isfile",
                                                   side_effect=lambda p: str(p) in files))
            stack.enter_context(mock.patch.object(os, "access", return_value=executable))
            yield

    def test_canonical_formula_is_found_on_both_architectures(self):
        for arch, prefix in (("arm64", "/opt/homebrew"),
                             ("x86_64", "/usr/local")):
            expected = f"{prefix}/opt/whisper.cpp/bin/whisper-server"
            with self.subTest(arch=arch), self.machine(arch, files=(expected,)):
                self.assertEqual(ggml.program_path(ggml.WHISPER), expected)
                self.assertTrue(ggml.system_program(ggml.WHISPER))

    @contextlib.contextmanager
    def keg(self, version="1.9.4", executable=True):
        with tempfile.TemporaryDirectory() as directory:
            prefix = pathlib.Path(directory, "homebrew")
            target = prefix / "Cellar/whisper.cpp" / version / "bin/whisper-server"
            target.parent.mkdir(parents=True)
            target.write_text("server")
            target.chmod(0o755 if executable else 0o644)
            link = prefix / "opt/whisper.cpp/bin/whisper-server"
            link.parent.mkdir(parents=True)
            link.symlink_to(target)
            with mock.patch.object(ggml, "_HOMEBREW_PREFIXES", (prefix,)):
                yield link, target, prefix

    def test_real_opt_symlinks_report_plain_and_revisioned_versions(self):
        for expected in ("1.9.4", "1.9.4_1"):
            with self.subTest(version=expected), \
                    self.keg(expected) as (link, target, _):
                self.assertEqual(ggml.homebrew_version(ggml.WHISPER, link), expected)
                self.assertEqual(ggml.homebrew_version(ggml.WHISPER, target), expected)

    def test_unrelated_or_malformed_cellar_paths_have_no_whisper_version(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = pathlib.Path(directory, "homebrew")
            paths = (
                prefix / "bin/whisper-server",
                prefix / "Cellar/other/1.9.4/bin/whisper-server",
                prefix / "Cellar/whisper.cpp/1.9.4/extra/bin/whisper-server",
                prefix / "Cellar/whisper.cpp/1.9.4/bin/other",
            )
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("server")
                path.chmod(0o755)
            with mock.patch.object(ggml, "_HOMEBREW_PREFIXES", (prefix,)):
                for path in paths:
                    with self.subTest(path=path):
                        self.assertEqual(
                            ggml.homebrew_version(ggml.WHISPER, path), ""
                        )
                self.assertEqual(ggml.homebrew_version(ggml.LLAMA, paths[0]), "")

    def test_non_executable_keg_target_has_no_formula_version(self):
        with self.keg(executable=False) as (link, _, _):
            # Windows does not derive X_OK from POSIX mode bits. Pin the
            # permission result so this tests Dikte's guard on every runner.
            with mock.patch.object(ggml.os, "access", return_value=False):
                self.assertEqual(ggml.homebrew_version(ggml.WHISPER, link), "")

    def test_broken_and_looping_opt_symlinks_have_no_formula_version(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = pathlib.Path(directory, "homebrew")
            broken = prefix / "opt/whisper.cpp/bin/whisper-server"
            broken.parent.mkdir(parents=True)
            broken.symlink_to(
                prefix / "Cellar/whisper.cpp/9.9/bin/whisper-server"
            )
            with mock.patch.object(ggml, "_HOMEBREW_PREFIXES", (prefix,)):
                self.assertEqual(
                    ggml.homebrew_version(ggml.WHISPER, broken), ""
                )
                broken.unlink()
                other = broken.with_name("other")
                broken.symlink_to(other)
                other.symlink_to(broken)
                self.assertEqual(
                    ggml.homebrew_version(ggml.WHISPER, broken), ""
                )

    def test_resolution_precedence_is_explicit_path_homebrew_managed(self):
        current = "/opt/homebrew/opt/whisper.cpp/bin/whisper-server"
        with self.machine(files=(current,), path="/path/whisper-server"):
            self.assertEqual(
                ggml.program_path(ggml.WHISPER), "/path/whisper-server"
            )
        with self.machine(files=(current, "/explicit/whisper-server"),
                          path="/path/whisper-server"):
            self.assertEqual(
                ggml.program_path(ggml.WHISPER, "/explicit/whisper-server"),
                str(pathlib.Path("/explicit/whisper-server").resolve()),
            )
        with self.machine(files=(current,)):
            self.assertEqual(ggml.program_path(ggml.WHISPER), current)
        with self.machine():
            self.assertEqual(
                ggml.program_path(ggml.WHISPER), "/managed/whisper-server"
            )

    def test_invalid_explicit_path_does_not_silently_fall_back(self):
        with self.machine(path="/path/whisper-server"):
            self.assertEqual(
                ggml.program_path(ggml.WHISPER, "/missing/server"), ""
            )

    def test_non_executable_or_non_file_homebrew_candidate_is_ignored(self):
        current = "/opt/homebrew/opt/whisper.cpp/bin/whisper-server"
        for files, executable in (((current,), False), ((), True)):
            with self.subTest(files=files, executable=executable), \
                    self.machine(files=files, executable=executable):
                self.assertEqual(
                    ggml.program_path(ggml.WHISPER),
                    "/managed/whisper-server",
                )
                self.assertFalse(ggml.system_program(ggml.WHISPER))

    def test_homebrew_fallback_is_macos_whisper_only(self):
        current = "/opt/homebrew/opt/whisper.cpp/bin/whisper-server"
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform), \
                    self.machine(platform=platform, files=(current,)):
                self.assertEqual(
                    ggml.program_path(ggml.WHISPER),
                    "/managed/whisper-server",
                )
        llama = "/opt/homebrew/opt/whisper.cpp/bin/llama-server"
        with self.machine(files=(llama,)):
            self.assertEqual(
                ggml.program_path(ggml.LLAMA), "/managed/whisper-server"
            )

    def test_unknown_architecture_does_not_guess_a_prefix(self):
        current = "/opt/homebrew/opt/whisper.cpp/bin/whisper-server"
        with self.machine(arch="unknown", files=(current,)):
            self.assertEqual(
                ggml.program_path(ggml.WHISPER), "/managed/whisper-server"
            )
