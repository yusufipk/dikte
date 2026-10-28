"""The release builds that make Linux and Windows Vulkan a one-click install."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

from dikte import ggml


ROOT = pathlib.Path(__file__).parents[1]
PACKAGING = ROOT / "packaging" / "whisper-vulkan"
WORKFLOW = ROOT / ".github" / "workflows" / "whisper-vulkan.yml"


def step_script(workflow, name):
    """The `run:` script of the workflow step called `name`.

    Read by indentation, without a YAML library, which the tests do not
    depend on. A `*alias` script is followed to the `&anchor` it names.
    """
    lines = workflow.splitlines()
    start = lines.index(f"      - name: {name}")
    run = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("      - ") or (line.strip() and not line.startswith("        ")):
            break
        if line.startswith("        run: "):
            run, start = line[len("        run: "):], index
            break
    if run is None:
        raise AssertionError(f"step {name!r} has no run script")
    if run.startswith("*"):
        anchor = f"run: &{run[1:]} |"
        start = next(index for index, line in enumerate(lines)
                     if line.strip() == anchor)
    elif not run.endswith("|"):
        return run
    body = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:])
    return "\n".join(body).strip("\n")


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
        self.assertIn("SOURCE_DIR=vendor/whisper.cpp", workflow)

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
        publish_script = step_script(workflow, "Publish dependency release")
        self.assertIn("gh release create", publish_script)
        self.assertNotIn("${{ inputs.", publish_script)

    def test_bundle_ci_runs_only_for_what_the_bundle_is_built_from(self):
        """A 45 minute build on a README typo is a tax on every other change.

        What ties ggml.py to the release is checked in this file instead, and
        this file runs on every pull request in milliseconds."""
        workflow = WORKFLOW.read_text(encoding="utf-8")
        trigger = workflow.split("workflow_dispatch:", 1)[0]
        self.assertIn("- packaging/whisper-vulkan/**", trigger)
        self.assertIn("- .github/workflows/whisper-vulkan.yml", trigger)
        # GitHub applies the patterns in order, so the negation has to come
        # after the directory it carves the README out of.
        self.assertGreater(
            trigger.find('- "!packaging/whisper-vulkan/README.md"'),
            trigger.find("- packaging/whisper-vulkan/**"))
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
        digest = "381ec8f07071d616e243b94d275e29763d6d2b4c7204017c166caf389e1f2349"
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
            temporary = pathlib.Path(temporary)
            root = temporary / "bundle"
            source = temporary / "source"
            root.mkdir()
            (source / "examples/server").mkdir(parents=True)
            (root / "whisper-server").write_bytes(b"elf")
            (root / "libggml.so.9.8.7").write_bytes(b"elf")
            (source / "examples/server/httplib.h").write_text(
                '#define CPPHTTPLIB_VERSION "6.5.4"\n', encoding="utf-8",
            )
            (source / "examples/json.hpp").write_text(
                "#define NLOHMANN_JSON_VERSION_MAJOR 3\n"
                "#define NLOHMANN_JSON_VERSION_MINOR 2\n"
                "#define NLOHMANN_JSON_VERSION_PATCH 1\n",
                encoding="utf-8",
            )
            sbom = root / "whisper-bin-ubuntu-vulkan-x64.cdx.json"
            environment = os.environ | {
                "ROOT": str(root),
                "SOURCE_DIR": str(source),
                "VERSION": "1.9.3",
                "COMMIT": "371b5a7561823ab2bb32142d2751e35e7534727b",
                "EPOCH": "1787219223",
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

    def test_the_sbom_derives_vendored_component_versions(self):
        document, _ = self._make_test_sbom()
        components = {component["name"]: component
                      for component in document["components"]}
        expected = {
            "ggml": "9.8.7",
            "cpp-httplib": "6.5.4",
            "json": "3.2.1",
        }
        for name, version in expected.items():
            self.assertEqual(components[name]["version"], version)
            self.assertEqual(
                components[name]["purl"].rsplit("@", 1)[1], f"v{version}",
            )


def _load_windows_bundle():
    spec = importlib.util.spec_from_file_location(
        "windows_bundle", PACKAGING / "windows_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


windows_bundle = _load_windows_bundle()


def portable_executable(imports=(), delayed=(), machine=0x8664):
    """The smallest PE32+ image the reader has to understand: headers, one
    section, an import directory and a delay-load directory."""
    base = 0x1000
    names = b""
    offsets = {}
    for name in (*imports, *delayed):
        offsets.setdefault(name, len(names))
        names += name.encode("ascii") + b"\0"
    import_size = 20 * (len(imports) + 1)
    delay_size = 32 * (len(delayed) + 1)
    strings = base + import_size + delay_size
    section = b"".join(struct.pack("<IIIII", 0, 0, 0, strings + offsets[n], 0)
                       for n in imports) + bytes(20)
    section += b"".join(struct.pack("<8I", 1, strings + offsets[n], 0, 0, 0, 0, 0, 0)
                        for n in delayed) + bytes(32)
    section += names
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<Q", optional, 24, 0x140000000)
    struct.pack_into("<I", optional, 108, 16)
    if imports:
        struct.pack_into("<II", optional, 112 + 8 * 1, base, import_size)
    if delayed:
        struct.pack_into("<II", optional, 112 + 8 * 13, base + import_size,
                         delay_size)
    header = bytearray(0x200)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    header[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", header, 0x44, machine, 1, 0, 0, 0, 240, 0x22)
    header[0x58:0x58 + 240] = optional
    struct.pack_into("<8sIIIIIIHHI", header, 0x58 + 240, b".idata",
                     len(section), base, len(section), 0x200, 0, 0, 0, 0, 0)
    return bytes(header) + section


GOOD_BUNDLE = {
    "whisper-server.exe": (("whisper.dll", "ggml.dll", "ggml-base.dll",
                            "kernel32.dll", "ws2_32.dll", "msvcp140.dll",
                            "vcruntime140.dll",
                            "api-ms-win-crt-runtime-l1-1-0.dll"), ()),
    "whisper.dll": (("ggml.dll", "ggml-base.dll", "kernel32.dll",
                     "vcruntime140.dll"), ()),
    "ggml.dll": (("ggml-base.dll", "kernel32.dll"), ()),
    "ggml-base.dll": (("kernel32.dll", "vcruntime140_1.dll"), ()),
    "ggml-cpu-haswell.dll": (("ggml-base.dll", "kernel32.dll"), ()),
    "ggml-vulkan.dll": (("ggml-base.dll", "vulkan-1.dll", "kernel32.dll"), ()),
    "msvcp140.dll": (("vcruntime140.dll", "kernel32.dll"), ()),
    "vcruntime140.dll": (("kernel32.dll",), ()),
    "vcruntime140_1.dll": (("vcruntime140.dll", "kernel32.dll"), ()),
}


class WindowsVulkanPackaging(unittest.TestCase):
    """The Windows x64 bundle: what it is built from, what it may import, and
    how the workflow builds, checks and publishes it."""

    def bundle(self, directory, files=None, extra=None):
        root = pathlib.Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        for name, (imports, delayed) in (files or GOOD_BUNDLE).items():
            (root / name).write_bytes(portable_executable(imports, delayed))
        for name, content in (extra or {}).items():
            (root / name).write_bytes(content)
        return root

    def test_the_reader_finds_imports_and_delay_loads(self):
        image = portable_executable(("KERNEL32.dll", "whisper.dll"),
                                    ("vulkan-1.dll",))
        machine, imports, delayed = windows_bundle.pe_imports(image)
        self.assertEqual(0x8664, machine)
        self.assertEqual(["kernel32.dll", "whisper.dll"], imports)
        self.assertEqual(["vulkan-1.dll"], delayed)
        with self.assertRaises(windows_bundle.NotPE):
            windows_bundle.pe_imports(b"\x7fELF" + bytes(100))

    def test_a_good_bundle_has_no_import_problems(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.bundle(temporary)
            self.assertEqual([], windows_bundle.import_problems(root))

    def test_only_the_vulkan_backend_may_import_the_loader(self):
        """The same archive has to start on a machine with no Vulkan loader,
        so the executable must not need one, delay-loaded or not."""
        for imports, delayed in ((("vulkan-1.dll",), ()), ((), ("vulkan-1.dll",))):
            with self.subTest(delayed=bool(delayed)), \
                    tempfile.TemporaryDirectory() as temporary:
                files = dict(GOOD_BUNDLE)
                files["whisper-server.exe"] = (
                    GOOD_BUNDLE["whisper-server.exe"][0] + imports, delayed)
                problems = windows_bundle.import_problems(
                    self.bundle(temporary, files))
                self.assertTrue(any("whisper-server.exe" in problem
                                    and "not plugin-only" in problem
                                    for problem in problems), problems)

    def test_nothing_may_import_the_vulkan_backend(self):
        """A file that links ggml-vulkan.dll brings the loader in through
        it, which the loader check alone would not see."""
        for name in ("whisper-server.exe", "ggml.dll", "whisper.dll"):
            for imports, delayed in (((windows_bundle.VULKAN_BACKEND,), ()),
                                     ((), (windows_bundle.VULKAN_BACKEND,))):
                with self.subTest(name=name, delayed=bool(delayed)), \
                        tempfile.TemporaryDirectory() as temporary:
                    files = dict(GOOD_BUNDLE)
                    files[name] = (GOOD_BUNDLE[name][0] + imports, delayed)
                    problems = windows_bundle.import_problems(
                        self.bundle(temporary, files))
                    self.assertTrue(any(
                        problem.startswith(name) and "ggml-vulkan.dll: the "
                        "Vulkan backend is not plugin-only" in problem
                        for problem in problems), problems)

    def test_a_vulkan_backend_without_the_loader_is_not_the_vulkan_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = dict(GOOD_BUNDLE)
            files["ggml-vulkan.dll"] = (("ggml-base.dll", "kernel32.dll"), ())
            problems = windows_bundle.import_problems(self.bundle(temporary, files))
        self.assertIn("ggml-vulkan.dll does not import vulkan-1.dll", problems)

    def test_the_runtime_it_imports_must_be_shipped_with_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = dict(GOOD_BUNDLE)
            del files["msvcp140.dll"]
            problems = windows_bundle.import_problems(self.bundle(temporary, files))
        self.assertTrue(any("msvcp140.dll, which is not shipped" in problem
                            for problem in problems), problems)

    def test_debug_runtimes_unknown_imports_and_other_machines_fail(self):
        cases = (
            ({"whisper.dll": (("vcruntime140d.dll",), ())}, "debug runtime"),
            ({"whisper.dll": (("nvcuda.dll",), ())}, "unexpected nvcuda.dll"),
        )
        for change, expected in cases:
            with self.subTest(expected=expected), \
                    tempfile.TemporaryDirectory() as temporary:
                problems = windows_bundle.import_problems(
                    self.bundle(temporary, GOOD_BUNDLE | change))
                self.assertTrue(any(expected in problem for problem in problems),
                                problems)
        with tempfile.TemporaryDirectory() as temporary:
            root = self.bundle(temporary)
            (root / "ggml.dll").write_bytes(
                portable_executable(("kernel32.dll",), machine=0x14C))
            problems = windows_bundle.import_problems(root)
        self.assertTrue(any("ggml.dll is not x64" in problem
                            for problem in problems), problems)

    def test_the_zip_is_the_same_bytes_every_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = pathlib.Path(temporary)
            root = self.bundle(temporary / "root")
            (root / "LICENSES").mkdir()
            (root / "LICENSES" / "a.txt").write_text("MIT\n", encoding="utf-8")
            first, second = temporary / "a.zip", temporary / "b.zip"
            windows_bundle.write_zip(root, first, 1787219223)
            time.sleep(0.01)
            windows_bundle.write_zip(root, second, 1787219223)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as bundle:
                names = bundle.namelist()
            self.assertEqual(sorted(names), names)
            self.assertTrue(all(name.startswith(windows_bundle.ASSET + "/")
                                for name in names))

    def test_the_extractor_refuses_members_outside_the_bundle(self):
        asset = windows_bundle.ASSET
        for name in ("../escape.dll", f"{asset}/../../escape.dll",
                     "/absolute.dll", "other/whisper-server.exe"):
            with self.subTest(name=name), \
                    tempfile.TemporaryDirectory() as temporary:
                archive = pathlib.Path(temporary) / "bundle.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr(f"{asset}/whisper-server.exe", b"MZ")
                    bundle.writestr(name, b"MZ")
                with self.assertRaises(SystemExit) as caught:
                    windows_bundle.safe_extract(archive, pathlib.Path(temporary) / "out")
                self.assertIn("unsafe archive member", str(caught.exception))
                self.assertFalse((pathlib.Path(temporary) / "out").exists())

    def test_the_extractor_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = pathlib.Path(temporary) / "bundle.zip"
            link = zipfile.ZipInfo(f"{windows_bundle.ASSET}/whisper.dll")
            link.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(link, "/etc/passwd")
            with self.assertRaises(SystemExit) as caught:
                windows_bundle.safe_extract(archive, pathlib.Path(temporary) / "out")
        self.assertIn("special archive member", str(caught.exception))

    def test_a_complete_bundle_validates(self):
        """Everything validate checks, on a bundle laid out the way build
        lays one out, SBOM included."""
        asset = windows_bundle.ASSET
        with tempfile.TemporaryDirectory() as temporary:
            temporary = pathlib.Path(temporary)
            source = temporary / "source"
            (source / "examples/server").mkdir(parents=True)
            (source / "ggml").mkdir()
            (source / "ggml/CMakeLists.txt").write_text(
                "set(GGML_VERSION_MAJOR 0)\nset(GGML_VERSION_MINOR 23)\n"
                "set(GGML_VERSION_PATCH 0)\n", encoding="utf-8")
            (source / "examples/server/httplib.h").write_text(
                '#define CPPHTTPLIB_VERSION "0.20.1"\n', encoding="utf-8")
            (source / "examples/json.hpp").write_text(
                "#define NLOHMANN_JSON_VERSION_MAJOR 3\n"
                "#define NLOHMANN_JSON_VERSION_MINOR 12\n"
                "#define NLOHMANN_JSON_VERSION_PATCH 0\n", encoding="utf-8")
            root = self.bundle(temporary / "root" / asset)
            (root / "LICENSES").mkdir()
            for name in ("whisper.cpp-MIT.txt", "cpp-httplib-MIT.txt",
                         "nlohmann-json-MIT.txt"):
                (root / "LICENSES" / name).write_text("MIT\n", encoding="utf-8")
            (root / "BUILD-INFO.json").write_text("{}\n", encoding="utf-8")
            sbom = root / f"{asset}.cdx.json"
            # The way build runs it: make-sbom.py writes the file itself, so
            # Windows' text-mode stdout cannot turn its newlines into CRLF.
            environment = os.environ | {
                "ROOT": str(root), "SOURCE_DIR": str(source),
                "VERSION": "1.9.4", "ASSET": asset,
                "COMMIT": "927cfce34f31707e17f2bff35c349632fb9e2c3a",
                "EPOCH": "1787219223",
            }
            printed = subprocess.run(
                [sys.executable, PACKAGING / "make-sbom.py"],
                env=environment | {"SBOM_OUTPUT": str(sbom)},
                stdout=subprocess.PIPE, check=True).stdout
            self.assertEqual(b"", printed)
            written = sbom.read_bytes()
            self.assertNotIn(b"\r", written)
            self.assertTrue(written.endswith(b"}\n"))
            document = json.loads(written)
            components = {component["name"]: component
                          for component in document["components"]}
            self.assertEqual("0.23.0", components["ggml"]["version"])
            properties = {item["name"]: item["value"] for item in
                          document["metadata"]["component"]["properties"]}
            self.assertEqual(f"{asset}.zip", properties["dikte:asset-name"])
            (root / "SHA256SUMS").write_text(windows_bundle._sums(root),
                                             encoding="utf-8")
            out = temporary / "dist"
            out.mkdir()
            windows_bundle.write_zip(root, out / f"{asset}.zip", 1787219223)
            digest = windows_bundle.sha256(out / f"{asset}.zip")
            (out / f"{asset}.zip.sha256").write_text(
                f"{digest}  {asset}.zip\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                windows_bundle.main(["validate", "--out", str(out),
                                     "--source", str(source)])
            self.assertIn("structure: PASS", printed.getvalue())
            (out / f"{asset}.zip.sha256").write_text(
                f"{'0' * 64}  {asset}.zip\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                windows_bundle.main(["validate", "--out", str(out)])

    def test_the_bundle_has_portable_dynamic_backends(self):
        for flag in ("GGML_BACKEND_DL=ON", "GGML_CPU_ALL_VARIANTS=ON",
                     "GGML_NATIVE=OFF", "GGML_OPENMP=OFF", "GGML_VULKAN=ON"):
            self.assertIn(flag, windows_bundle.CMAKE_FLAGS)
        script = (PACKAGING / "windows_bundle.py").read_text(encoding="utf-8")
        self.assertIn("/Brepro", script)

    def test_the_smoke_test_runs_what_dikte_runs(self):
        """No -ng: the run that has to survive a machine without a usable card
        is the one where whisper.cpp goes looking for it."""
        script = (PACKAGING / "windows_bundle.py").read_text(encoding="utf-8")
        smoke = script.split("def smoke(args):", 1)[1].split("\ndef ", 1)[0]
        code = "\n".join(line for line in smoke.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn('"-ng"', code)
        for argument in ('"--inference-path"', '"-sns"', '"-nlp"', "cwd=str(root)"):
            self.assertIn(argument, code)

    def test_the_workflow_builds_validates_smokes_and_publishes_windows(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        windows = workflow.split("  build-windows:", 1)[1].split("\n  publish:", 1)[0]
        for step in ("Install pinned Vulkan SDK", "Build Windows archive",
                     "Verify reviewed Windows archive digest",
                     "Validate archive and PE contract",
                     "CPU fallback smoke test (no GPU)",
                     "Keep line endings as committed"):
            self.assertIn(step, windows)
        self.assertIn("runs-on: windows-latest", windows)
        self.assertIn("refusing to publish an archive whose digest has not "
                      "been reviewed", windows)
        self.assertRegex(windows, r"VULKAN_SDK_SHA256: [0-9a-f]{64}\n")
        self.assertIn("Get-FileHash", windows)
        self.assertLess(windows.index("- name: Validate source coordinates"),
                        windows.index("- name: Check out pinned whisper.cpp"))
        self.assertGreaterEqual(windows.count("persist-credentials: false"), 4)
        sbom = windows.split("  sbom-windows:", 1)[1]
        self.assertIn("needs: build-windows", sbom)
        self.assertNotIn("inputs.publish", sbom)
        self.assertIn("cyclonedx-cli@sha256:", sbom)
        self.assertIn("whisper-bin-win-vulkan-x64.cdx.json", sbom)
        publish, windows_publish = workflow.split("\n  publish:", 1)[1].split(
            "\n  publish-windows:", 1)
        self.assertNotIn("win-vulkan", publish)
        self.assertIn("needs: build\n", publish)
        self.assertIn("inputs.platforms != 'windows'", publish)
        self.assertIn("needs: [build-windows, smoke-windows, sbom-windows]",
                      windows_publish)
        self.assertIn("inputs.platforms != 'linux'", windows_publish)
        self.assertIn("github.ref == 'refs/heads/master'", windows_publish)
        self.assertIn("sha256sum --check whisper-bin-win-vulkan-x64.zip.sha256",
                      windows_publish)
        self.assertIn("dist/whisper-bin-win-vulkan-x64.cdx.json", windows_publish)
        self.assertIn(
            "RELEASE_TAG: whisper.cpp-v${{ inputs.whisper_version }}-windows",
            windows_publish)
        script = step_script(workflow, "Publish Windows dependency release")
        self.assertEqual(step_script(workflow, "Publish dependency release"),
                         script)
        for check in ("--prerelease", "--latest=false", "--verify-tag",
                      "refusing to replace existing tag"):
            self.assertIn(check, script)
        self.assertNotIn("${{ inputs.", script)

    def test_each_platform_is_built_and_published_on_its_own(self):
        """An urgent Linux bump must not wait on a Windows build or review,
        and the default dispatch keeps doing what it did before."""
        workflow = WORKFLOW.read_text(encoding="utf-8")
        inputs = workflow.split("  platforms:", 1)[1].split("\n      publish:", 1)[0]
        self.assertIn("default: linux", inputs)
        self.assertIn("options: [linux, windows, both]", inputs)
        linux = workflow.split("\n  build:", 1)[1].split("\n    steps:", 1)[0]
        self.assertIn("inputs.platforms != 'windows'", linux)
        windows = workflow.split("\n  build-windows:", 1)[1].split("\n    steps:", 1)[0]
        self.assertIn("inputs.platforms != 'linux'", windows)
        for job in (linux, windows):
            self.assertIn("github.event_name == 'pull_request'", job)
        group = re.search(r"\nconcurrency:\n  group: (.*)\n", workflow).group(1)
        self.assertIn("inputs.platforms", group)
        self.assertIn(ggml.MANAGED_WHISPER_WINDOWS_VULKAN,
                      windows_bundle.ASSET + ".zip")

    def test_the_installer_pin_and_the_workflow_agree_on_windows(self):
        """The installer may lag the workflow, as on Linux, but whatever
        digest it trusts has to be one the workflow reviewed."""
        workflow = WORKFLOW.read_text(encoding="utf-8")
        reviewed = re.search(r'REVIEWED_WHISPER_WINDOWS_SHA256: "?([0-9a-f]*)"?\n',
                             workflow).group(1)
        version = re.search(r'REVIEWED_WHISPER_WINDOWS_VERSION: "([0-9.]*)"\n',
                            workflow).group(1)
        pinned = ggml.MANAGED_WHISPER_WINDOWS_SHA256
        self.assertEqual(
            f"whisper.cpp-{ggml.MANAGED_WHISPER_WINDOWS_VERSION}-windows",
            ggml.MANAGED_WHISPER_WINDOWS_RELEASE)
        if pinned == ggml.UNREVIEWED_DIGEST:
            return
        self.assertRegex(pinned, r"^[0-9a-f]{64}$")
        self.assertEqual(reviewed, pinned)
        self.assertEqual(f"v{version}", ggml.MANAGED_WHISPER_WINDOWS_VERSION)
        self.assertEqual(f"whisper.cpp-v{version}-windows",
                         ggml.MANAGED_WHISPER_WINDOWS_RELEASE)


if __name__ == "__main__":
    unittest.main()
