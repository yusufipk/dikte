"""The release build that makes Linux Vulkan a one-click install."""

import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from dikte import ggml


ROOT = pathlib.Path(__file__).parents[1]
PACKAGING = ROOT / "packaging" / "whisper-vulkan"
WORKFLOW = ROOT / ".github" / "workflows" / "whisper-vulkan.yml"


class WhisperVulkanPackaging(unittest.TestCase):
    @unittest.skipUnless(sys.platform != "win32" and shutil.which("bash"),
                         "bash syntax check is unavailable")
    def test_the_release_scripts_parse_as_shell(self):
        for name in ("build-package.sh", "validate-package.sh",
                     "smoke-runtime.sh"):
            script = PACKAGING / name
            checked = subprocess.run(
                ["bash", "-n", script], capture_output=True, text=True,
            )
            self.assertEqual("", checked.stderr)
            self.assertEqual(0, checked.returncode)

    def test_the_workflow_builds_validates_smokes_and_publishes(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for step in ("Build deterministic archive",
                     "Verify reviewed archive digest",
                     "Validate archive and ELF contract",
                     "CPU fallback smoke test (no Vulkan loader)",
                     "Vulkan loader present, no device smoke test",
                     "Vulkan plugin-load smoke test (Mesa llvmpipe)",
                     "Publish dependency release"):
            self.assertIn(step, workflow)
        self.assertNotRegex(workflow, r"uses: [^\n]+@v\d+(?:\s|$)")

    def test_publish_is_safe_for_dikte_and_limited_to_reviewed_master(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertGreaterEqual(workflow.count("persist-credentials: false"), 2)
        self.assertIn("github.ref == 'refs/heads/master'", workflow)
        self.assertIn("--prerelease", workflow)
        self.assertIn("--latest=false", workflow)
        self.assertIn("--verify-tag", workflow)
        self.assertIn("refusing to replace existing tag", workflow)
        self.assertIn("^[0-9]+\\.[0-9]+\\.[0-9]+$", workflow)
        self.assertIn("^[0-9a-f]{40}$", workflow)
        publish_script = workflow.split("      - name: Publish dependency release", 1)[1]
        publish_script = publish_script.split("        run: |", 1)[1]
        self.assertNotIn("${{ inputs.", publish_script)

    def test_bundle_ci_runs_only_for_what_the_bundle_is_built_from(self):
        """A 45 minute build on a README typo is a tax on every other change.

        What ties ggml.py to the release is checked in this file instead, and
        this file runs on every pull request in milliseconds."""
        workflow = WORKFLOW.read_text(encoding="utf-8")
        trigger = workflow.split("workflow_dispatch:", 1)[0]
        self.assertIn("- packaging/whisper-vulkan/**", trigger)
        self.assertIn("- .github/workflows/whisper-vulkan.yml", trigger)
        for path in ("dikte/ggml.py", "tests/test_ggml.py",
                     "tests/test_packaging.py", "README.md", "README.tr.md"):
            self.assertNotIn(f"- {path}", trigger)

    def test_the_smoke_tests_run_what_dikte_runs(self):
        """-ng is what Dikte passes when its GPU setting is off, and a run
        with it never asks for a backend at all. The three runs that have to
        hold are the ones without it: no loader, a loader with nothing behind
        it, and a working device."""
        script = (PACKAGING / "smoke-runtime.sh").read_text(encoding="utf-8")
        code = "\n".join(line for line in script.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("-ng", code)
        for mode in ("cpu)", "noicd)", "vulkan)"):
            self.assertIn(mode, script)
        self.assertTrue((PACKAGING / "Dockerfile.runtime-noicd").is_file())

    def test_an_unreviewed_version_is_reported_and_never_published(self):
        """The digest of a version nobody has reviewed cannot be known before
        it is built, so the gate cannot be the only way through."""
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("expected_sha256", workflow)
        self.assertIn(
            "refusing to publish an archive whose digest has not been reviewed",
            workflow)

    def test_the_shape_of_the_inputs_is_checked_before_they_are_used(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(workflow.index("- name: Validate source coordinates"),
                        workflow.index("- name: Check out pinned whisper.cpp"))

    def test_the_validator_checks_tar_links_before_extraction(self):
        validator = (PACKAGING / "validate-package.sh").read_text(
            encoding="utf-8")
        for check in ("member.issym()", "member.islnk()", "member.isdev()"):
            self.assertIn(check, validator)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("bash"),
                         "Linux packaging test is unavailable")
    def test_the_validator_rejects_an_escaping_symlink(self):
        asset = "whisper-bin-ubuntu-vulkan-x64"
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary)
            archive = output / f"{asset}.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                link = tarfile.TarInfo(f"{asset}/whisper-server")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                bundle.addfile(link, io.BytesIO())
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            (output / f"{asset}.tar.gz.sha256").write_text(
                f"{digest}  {asset}.tar.gz\n", encoding="utf-8",
            )
            checked = subprocess.run(
                ["bash", PACKAGING / "validate-package.sh"],
                env=os.environ | {"OUT_DIR": str(output)},
                capture_output=True, text=True,
            )
        self.assertNotEqual(0, checked.returncode)
        self.assertIn("unsafe symlink", checked.stderr)

    def test_the_validator_checks_elf_architecture_dependencies_and_paths(self):
        validator = (PACKAGING / "validate-package.sh").read_text(
            encoding="utf-8")
        for check in ("Advanced Micro Devices X86-64", "unexpected DT_NEEDED",
                      "path.read_bytes()"):
            self.assertIn(check, validator)

    def test_the_builder_and_its_downloads_are_pinned(self):
        dockerfile = (PACKAGING / "Dockerfile.build").read_text(
            encoding="utf-8")
        self.assertRegex(dockerfile, r"FROM ubuntu@sha256:[0-9a-f]{64}")
        self.assertIn("CMAKE_SHA256=", dockerfile)
        self.assertIn("libvulkan-dev=", dockerfile)
        self.assertIn("shaderc=", dockerfile)
        key = (PACKAGING / "lunarg-signing-key-pub.asc").read_bytes()
        key = key.replace(b"\r\n", b"\n")
        self.assertEqual(
            "aa1c3c29673140e77f0d6a9aaeed5d9b5621e305ead51c59fae4458bbb4df92b",
            hashlib.sha256(key).hexdigest(),
        )

    def test_the_bundle_has_portable_dynamic_backends(self):
        script = (PACKAGING / "build-package.sh").read_text(
            encoding="utf-8")
        for flag in ("GGML_BACKEND_DL=ON", "GGML_CPU_ALL_VARIANTS=ON",
                     "GGML_NATIVE=OFF", "GGML_OPENMP=OFF",
                     "GGML_VULKAN=ON"):
            self.assertIn(flag, script)
        self.assertIn("libggml-cpu*.so", script)
        self.assertIn("libggml-vulkan.so", script)

    def test_the_dependency_candidate_can_precede_the_installer_pin(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        script = (PACKAGING / "build-package.sh").read_text(
            encoding="utf-8")
        self.assertEqual("whisper.cpp-v1.9.3",
                         ggml.MANAGED_WHISPER_RELEASE)
        self.assertEqual("v1.9.3", ggml.MANAGED_WHISPER_VERSION)
        self.assertIn("RELEASE_TAG: whisper.cpp-v${{ inputs.whisper_version }}",
                      workflow)
        self.assertIn("WHISPER_VERSION:=1.9.3", script)
        self.assertIn("REVIEWED_WHISPER_VERSION: \"1.9.4\"", workflow)
        commit = "927cfce34f31707e17f2bff35c349632fb9e2c3a"
        digest = "042769c8e70c1f603b0eac8dd57bca4eaaf369f68e1e3cf6a25d1b5984aba055"
        self.assertIn(commit, workflow)
        self.assertIn(digest, workflow)
        self.assertIn(ggml.MANAGED_WHISPER_VULKAN, workflow)

    def test_the_bundle_carries_metadata_and_all_required_licenses(self):
        script = (PACKAGING / "build-package.sh").read_text(
            encoding="utf-8")
        for name in ("BUILD-INFO.json", "SHA256SUMS", ".cdx.json"):
            self.assertIn(name, script)
        for name in ("cpp-httplib-MIT.txt", "nlohmann-json-MIT.txt"):
            self.assertTrue((PACKAGING / "licenses" / name).is_file())

    def _make_test_sbom(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "whisper-server").write_bytes(b"elf")
            sbom = root / "whisper-bin-ubuntu-vulkan-x64.cdx.json"
            environment = os.environ | {
                "ROOT": str(root),
                "VERSION": "1.9.3",
                "COMMIT": "371b5a7561823ab2bb32142d2751e35e7534727b",
                "EPOCH": "1787219223",
                "GGML_VERSION": "9.8.7",
            }
            with sbom.open("w", encoding="utf-8") as output:
                subprocess.run(
                    [sys.executable, PACKAGING / "make-sbom.py"],
                    env=environment, stdout=output, check=True,
                )
            return json.loads(sbom.read_text(encoding="utf-8")), sbom.name

    def test_the_sbom_does_not_record_the_file_being_written(self):
        document, sbom_name = self._make_test_sbom()
        names = {component["name"] for component in document["components"]}
        self.assertNotIn(sbom_name, names)

    def test_the_sbom_lists_ggml(self):
        document, _ = self._make_test_sbom()
        ggml_component = next(component for component in document["components"]
                              if component["name"] == "ggml")
        self.assertEqual(ggml_component["version"], "9.8.7")
        self.assertEqual(ggml_component["purl"],
                         "pkg:github/ggml-org/ggml@v9.8.7")


if __name__ == "__main__":
    unittest.main()
