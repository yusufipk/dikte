#!/usr/bin/env python3
"""Build, validate and smoke-test the Windows x64 Vulkan whisper-server bundle.

The Linux bundle is built in a pinned container by build-package.sh and checked
with readelf by validate-package.sh. Windows has neither a container to pin nor
readelf, so this one script does the same three jobs there:

    build     configure and compile whisper.cpp with MSVC and the Vulkan SDK,
              then pack an allowlist of files into a deterministic zip
    validate  check the zip's layout, digests and PE import tables
    smoke     start whisper-server the way Dikte does and transcribe a sample

The PE import tables are read here in Python rather than with dumpbin, which
is only on PATH inside a Visual Studio developer prompt, or objdump, which a
Windows runner does not promise. It also lets tests/test_packaging.py check
the import rules on any system.

The C runtime is the one decision Linux did not have to make. The bundle links
the dynamic MSVC runtime, like upstream's own Windows archives, and ships the
redistributable DLLs it imports next to the executable (Microsoft's app-local
deployment). A static runtime would give every DLL in the bundle a CRT and a
heap of its own, and ggml hands memory and FILE handles across those DLLs.
The Universal CRT is part of Windows 10 and later and is not shipped.

Reproducibility is weaker than on Linux. /Brepro removes the link timestamps,
/d1trimfile the source copy's directory from __FILE__, and the zip is written
with fixed dates and order, so two runs on the same runner image give the same
digest, but nothing pins the compiler: a runner image update that brings a new
MSVC changes the bytes. The published archive
is the artifact, not something that can be rebuilt on demand years later.
"""

import argparse
import ctypes.util
import hashlib
import json
import os
import pathlib
import posixpath
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

ASSET = "whisper-bin-win-vulkan-x64"
HERE = pathlib.Path(__file__).resolve().parent

CMAKE_FLAGS = (
    "BUILD_SHARED_LIBS=ON",
    "GGML_BACKEND_DL=ON",
    "GGML_CPU_ALL_VARIANTS=ON",
    "GGML_NATIVE=OFF",
    "GGML_CCACHE=OFF",
    "GGML_OPENMP=OFF",
    "GGML_VULKAN=ON",
    "WHISPER_BUILD_EXAMPLES=ON",
    "WHISPER_BUILD_SERVER=ON",
    "WHISPER_BUILD_TESTS=OFF",
    "WHISPER_BUILD_IS_DEV=OFF",
    "WHISPER_CURL=OFF",
    "WHISPER_SDL2=OFF",
    "WHISPER_COMMON_FFMPEG=OFF",
)

# What the archive carries out of build/bin/Release, by pattern. An allowlist,
# not everything examples/ happens to build in the future.
PROGRAM_FILES = ("whisper-server.exe", "whisper.dll", "ggml.dll",
                 "ggml-base.dll", "ggml-vulkan.dll")
CPU_BACKENDS = re.compile(r"ggml-cpu(?:-[a-z0-9_]+)*\.dll", re.IGNORECASE)

# The MSVC redistributable, shipped only when something in the bundle imports
# it. Debug runtimes are never redistributable and are refused outright.
CRT_DLLS = frozenset({
    "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
    "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll", "concrt140.dll",
})
DEBUG_CRT = re.compile(r"(?:vcruntime|msvcp|concrt)140[a-z0-9_]*d\.dll"
                       r"|ucrtbased\.dll", re.IGNORECASE)

# What every Windows 10 and later has, and nothing that comes with a driver.
# A new import outside this list fails validation, so somebody looks at it
# before the archive ships, the same as an unexpected DT_NEEDED on Linux.
SYSTEM_DLLS = frozenset({
    "advapi32.dll", "bcrypt.dll", "cfgmgr32.dll", "comctl32.dll",
    "crypt32.dll", "dbghelp.dll", "gdi32.dll", "kernel32.dll",
    "kernelbase.dll", "mswsock.dll", "ntdll.dll", "ole32.dll",
    "oleaut32.dll", "powrprof.dll", "psapi.dll", "setupapi.dll",
    "shell32.dll", "shlwapi.dll", "ucrtbase.dll", "user32.dll",
    "version.dll", "winmm.dll", "ws2_32.dll",
})
SYSTEM_PREFIXES = ("api-ms-win-", "ext-ms-win-")
VULKAN_LOADER = "vulkan-1.dll"
VULKAN_BACKEND = "ggml-vulkan.dll"
AMD64 = 0x8664


# --- reading PE files -------------------------------------------------------


class NotPE(ValueError):
    pass


def pe_imports(data):
    """(machine, imported DLL names, delay-loaded DLL names) of a PE image.

    Names come back lowercased, because Windows resolves them without regard
    to case. Delay-loaded imports are kept apart only for the error message:
    a delay-loaded vulkan-1.dll is still a hard dependency the moment the
    function behind it is called.
    """
    if data[:2] != b"MZ" or len(data) < 0x40:
        raise NotPE("no MZ header")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise NotPE("no PE signature")
    machine, sections, _, _, _, optional_size, _ = struct.unpack_from(
        "<HHIIIHH", data, pe + 4)
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x20B:
        image_base = struct.unpack_from("<Q", data, optional + 24)[0]
        count = struct.unpack_from("<I", data, optional + 108)[0]
        directories = optional + 112
    elif magic == 0x10B:
        image_base = struct.unpack_from("<I", data, optional + 28)[0]
        count = struct.unpack_from("<I", data, optional + 92)[0]
        directories = optional + 96
    else:
        raise NotPE(f"unknown optional header magic {magic:#x}")
    table = []
    for index in range(sections):
        _, virtual_size, address, raw_size, raw = struct.unpack_from(
            "<8sIIII", data, optional + optional_size + 40 * index)
        table.append((address, max(virtual_size, raw_size), raw))

    def offset(rva):
        for address, size, raw in table:
            if address <= rva < address + size:
                return raw + rva - address
        raise NotPE(f"RVA {rva:#x} is in no section")

    def name_at(rva):
        start = offset(rva)
        end = data.index(b"\0", start)
        return data[start:end].decode("ascii").lower()

    def directory(index):
        if index >= count:
            return 0
        return struct.unpack_from("<I", data, directories + 8 * index)[0]

    imports = []
    rva = directory(1)
    while rva:
        position = offset(rva)
        descriptor = struct.unpack_from("<IIIII", data, position)
        if not any(descriptor):
            break
        imports.append(name_at(descriptor[3]))
        rva += 20
    delayed = []
    rva = directory(13)
    while rva:
        position = offset(rva)
        descriptor = struct.unpack_from("<IIIIIIII", data, position)
        if not any(descriptor):
            break
        attributes, name = descriptor[0], descriptor[1]
        # Bit 0 clear is the old Visual C++ 6 layout, which stored addresses
        # rather than RVAs.
        delayed.append(name_at(name if attributes & 1 else name - image_base))
        rva += 32
    return machine, imports, delayed


def _is_pe(path):
    return path.suffix.lower() in (".exe", ".dll")


def import_problems(root):
    """Everything wrong with the bundle's PE files, as sentences.

    Each file must be x64. Each import must be a file in the bundle, a part
    of Windows, or the Vulkan loader, and the loader only for ggml-vulkan.dll:
    whisper-server.exe has to start on a machine without one, which is what
    GGML_BACKEND_DL arranges and what this holds it to. Nothing may import
    ggml-vulkan.dll itself either, or the loader would come in through it;
    checking every file's own imports covers any chain of them.
    """
    root = pathlib.Path(root)
    shipped = {path.name.lower() for path in root.iterdir() if path.is_file()}
    problems = []
    vulkan_seen = False
    for path in sorted(root.iterdir()):
        if not path.is_file() or not _is_pe(path):
            continue
        try:
            machine, imports, delayed = pe_imports(path.read_bytes())
        except (NotPE, struct.error, ValueError) as exc:
            problems.append(f"{path.name} is not a readable PE file: {exc}")
            continue
        if machine != AMD64:
            problems.append(f"{path.name} is not x64 (machine {machine:#x})")
        for name in imports + delayed:
            how = "delay-loads" if name in delayed else "imports"
            if DEBUG_CRT.fullmatch(name):
                problems.append(f"{path.name} {how} the debug runtime {name}")
            elif name == VULKAN_LOADER:
                if path.name.lower() == VULKAN_BACKEND:
                    vulkan_seen = True
                else:
                    problems.append(f"{path.name} {how} {name}: Vulkan is "
                                    "not plugin-only")
            elif name == VULKAN_BACKEND:
                problems.append(f"{path.name} {how} {name}: the Vulkan "
                                "backend is not plugin-only")
            elif name in shipped:
                continue
            elif name in CRT_DLLS:
                problems.append(f"{path.name} {how} {name}, which is not "
                                "shipped next to it")
            elif not (name in SYSTEM_DLLS or name.startswith(SYSTEM_PREFIXES)):
                problems.append(f"{path.name} {how} unexpected {name}")
    if VULKAN_BACKEND in shipped and not vulkan_seen:
        problems.append(f"{VULKAN_BACKEND} does not import {VULKAN_LOADER}")
    return problems


# --- the archive ------------------------------------------------------------


def _member_problem(info):
    """Why a zip member must not be unpacked, or ""."""
    name = info.filename
    if "\\" in name or ":" in name:
        return f"unportable archive member: {name}"
    normalized = posixpath.normpath(name)
    if (posixpath.isabs(name) or normalized == ".."
            or normalized.startswith("../")
            or normalized.split("/", 1)[0] != ASSET):
        return f"unsafe archive member: {name}"
    mode = info.external_attr >> 16
    if mode and (mode & 0o170000) not in (0, 0o100000, 0o040000):
        return f"special archive member: {name}"
    return ""


def safe_extract(archive, into):
    """Unpack the bundle after checking every member stays under its root."""
    with zipfile.ZipFile(archive) as bundle:
        names = [info.filename for info in bundle.infolist()]
        if len(names) != len(set(names)):
            raise SystemExit("duplicate archive members")
        for info in bundle.infolist():
            problem = _member_problem(info)
            if problem:
                raise SystemExit(problem)
        bundle.extractall(into)
    return pathlib.Path(into) / ASSET


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sums(root):
    """SHA256SUMS in the format sha256sum --check reads."""
    lines = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(f"{sha256(path)}  ./{path.relative_to(root).as_posix()}\n")
    return "".join(lines)


def write_zip(root, archive, epoch):
    """A zip that is the same bytes whenever the same files go in.

    Sorted names, one fixed date, fixed attributes and no extra fields. The
    date is the source commit's, clamped to 1980 because the zip format has
    no earlier one.
    """
    stamp = time.gmtime(max(epoch, 315532800))[:6]
    files = sorted((path for path in root.rglob("*") if path.is_file()),
                   key=lambda p: p.relative_to(root).as_posix())
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in files:
            info = zipfile.ZipInfo(
                f"{ASSET}/{path.relative_to(root).as_posix()}", stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0
            bundle.writestr(info, path.read_bytes(), compresslevel=9)


# --- build ------------------------------------------------------------------


def _run(command, **kwargs):
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def _cache_value(build, key):
    try:
        text = (build / "CMakeCache.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = re.search(rf"^{re.escape(key)}:[A-Z]+=(.*)$", text, re.MULTILINE)
    return found.group(1).strip() if found else ""


def _visual_studio():
    """The installation path of the Visual Studio the build uses."""
    vswhere = (pathlib.Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
               / "Microsoft Visual Studio" / "Installer" / "vswhere.exe")
    install = subprocess.run(
        [str(vswhere), "-latest", "-products", "*", "-requires",
         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
         "-property", "installationPath"],
        check=True, capture_output=True, text=True).stdout.strip()
    if not install:
        raise SystemExit("no Visual Studio with the x64 C++ tools found")
    return pathlib.Path(install)


def _msvc_version():
    """The default MSVC toolset, which is what the build compiled with."""
    return (_visual_studio() / "VC" / "Auxiliary" / "Build"
            / "Microsoft.VCToolsVersion.default.txt").read_text().strip()


def _redist_dir():
    """The x64 CRT folder of the Visual Studio the build used."""
    install = _visual_studio()
    version = (install / "VC" / "Auxiliary" / "Build"
               / "Microsoft.VCRedistVersion.default.txt").read_text().strip()
    folders = sorted((install / "VC" / "Redist" / "MSVC" / version / "x64")
                     .glob("Microsoft.VC*.CRT"))
    if len(folders) != 1:
        raise SystemExit(f"expected one x64 CRT redist folder, got {folders}")
    return version, folders[0]


def _ship_runtime(root):
    """Copy in the redistributable DLLs the bundle imports, and theirs."""
    version, folder = _redist_dir()
    available = {path.name.lower(): path for path in folder.glob("*.dll")}
    shipped = []
    while True:
        wanted = set()
        for path in root.iterdir():
            if path.is_file() and _is_pe(path):
                _, imports, delayed = pe_imports(path.read_bytes())
                wanted.update(name for name in imports + delayed
                              if name in CRT_DLLS)
        missing = sorted(wanted - {path.name.lower() for path in root.iterdir()})
        if not missing:
            return version, sorted(shipped)
        for name in missing:
            if name not in available:
                raise SystemExit(f"{name} is imported but not in {folder}")
            shutil.copy2(available[name], root / name)
            shipped.append(name)


def build(args):
    source = pathlib.Path(args.source).resolve()
    work = pathlib.Path(args.work).resolve()
    out = pathlib.Path(args.out).resolve()
    version = os.environ["WHISPER_VERSION"]
    commit = os.environ["WHISPER_COMMIT"]
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    vulkan_sdk = os.environ["VULKAN_SDK"]
    copy, build_dir = work / "source", work / "build"
    root = out / "root" / ASSET
    for path in (copy, build_dir, out):
        shutil.rmtree(path, ignore_errors=True)
    (root / "LICENSES").mkdir(parents=True)
    # Upstream configures bindings/javascript/package.json in the source
    # directory. Build a private copy so the verified checkout stays untouched.
    shutil.copytree(source, copy, symlinks=True)

    environment = dict(os.environ)
    # /Brepro replaces the link timestamp in every PE header with a hash of
    # the contents, without which no two builds would ever share a digest.
    # /d1trimfile drops the source copy's directory from __FILE__, which
    # ggml's asserts compile in, so a build in another directory gives the
    # same bytes: MSVC's counterpart of -fmacro-prefix-map on Linux. It is
    # an undocumented switch, so a compiler that drops it changes the digest.
    # Through the environment rather than CMAKE_<LANG>_FLAGS, which would
    # replace CMake's MSVC defaults (/EHsc among them) instead of adding to
    # them.
    trim = f"/Brepro /d1trimfile:{copy}{os.sep}"
    environment["CFLAGS"] = environment["CXXFLAGS"] = trim
    environment["LDFLAGS"] = "/Brepro"
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    _run(["cmake", "-S", copy, "-B", build_dir, "-A", "x64",
          "-DCMAKE_BUILD_TYPE=Release",
          *(f"-D{flag}" for flag in CMAKE_FLAGS),
          f"-DWHISPER_BUILD_COMMIT={commit}", "-DWHISPER_BUILD_NUMBER=0"],
         env=environment)
    _run(["cmake", "--build", build_dir, "--config", "Release",
          "--target", "whisper-server", "--parallel", str(os.cpu_count() or 2)],
         env=environment)

    binaries = build_dir / "bin" / "Release"
    for name in PROGRAM_FILES:
        shutil.copy2(binaries / name, root / name)
    cpu = [path for path in binaries.iterdir() if CPU_BACKENDS.fullmatch(path.name)]
    if not cpu:
        raise SystemExit("no ggml-cpu backends were built")
    for path in cpu:
        shutil.copy2(path, root / path.name)
    redist_version, runtime = _ship_runtime(root)

    shutil.copy2(source / "LICENSE", root / "LICENSES" / "whisper.cpp-MIT.txt")
    for name in ("cpp-httplib-MIT.txt", "nlohmann-json-MIT.txt"):
        shutil.copy2(HERE / "licenses" / name, root / "LICENSES" / name)

    cmake_version = subprocess.run(["cmake", "--version"], check=True,
                                   capture_output=True, text=True
                                   ).stdout.splitlines()[0].split()[-1]
    glslc = re.search(r"shaderc v?([0-9][^\s]*)", subprocess.run(
        ["glslc", "--version"], check=True, capture_output=True, text=True
    ).stdout)
    glslc_version = glslc.group(1) if glslc else "unknown"
    info = {
        "asset": f"{ASSET}.zip",
        "source": "https://github.com/ggml-org/whisper.cpp",
        "source_version": f"v{version}",
        "source_commit": commit,
        "source_date_epoch": epoch,
        "build_platform": "windows-x86_64",
        # Recorded, not pinned: GitHub replaces the image and its compiler
        # on its own schedule, and either can change the archive's digest.
        "runner_image": " ".join(filter(None, (os.environ.get("ImageOS", ""),
                                               os.environ.get("ImageVersion", "")))),
        "cmake": cmake_version,
        "cmake_generator": _cache_value(build_dir, "CMAKE_GENERATOR"),
        "msvc": _msvc_version(),
        "vulkan_sdk": pathlib.Path(vulkan_sdk).name,
        "cmake_flags": list(CMAKE_FLAGS) + ["C/CXX flags: /Brepro /d1trimfile:<source>",
                                          "LD flags: /Brepro"],
        "runtime_contract": {
            "required": ["x86_64 Windows 10 or later",
                         "Universal CRT (part of Windows 10 and later)"],
            "bundled_msvc_runtime": {"version": redist_version,
                                     "files": runtime},
            "optional_gpu": [VULKAN_LOADER, "a Vulkan 1.2 driver"],
            "cpu_fallback": "dynamic CPU backends are included; -ng forces CPU",
        },
    }
    (root / "BUILD-INFO.json").write_text(
        json.dumps(info, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")

    sbom = root / f"{ASSET}.cdx.json"
    # Written by make-sbom.py itself: its stdout is a text stream that turns
    # every newline into CRLF on Windows, whatever the parent opened.
    subprocess.run(
        [sys.executable, str(HERE / "make-sbom.py")],
        env=environment | {
            "ROOT": str(root), "SOURCE_DIR": str(copy),
            "VERSION": version, "COMMIT": commit, "EPOCH": str(epoch),
            "ASSET": ASSET, "CMAKE_VERSION": cmake_version,
            "GLSLC_VERSION": glslc_version, "OUTPUT": str(sbom),
        },
        check=True)
    (root / "SHA256SUMS").write_text(_sums(root), encoding="utf-8", newline="\n")

    archive = out / f"{ASSET}.zip"
    write_zip(root, archive, epoch)
    digest = sha256(archive)
    (out / f"{ASSET}.zip.sha256").write_text(
        f"{digest}  {ASSET}.zip\n", encoding="utf-8", newline="\n")
    shutil.copy2(sbom, out / sbom.name)
    shutil.rmtree(out / "root")
    print(f"built {archive.name}: sha256 {digest}")


# --- validate ---------------------------------------------------------------


def validate(args):
    out = pathlib.Path(args.out)
    archive = out / f"{ASSET}.zip"
    expected, _, name = (out / f"{ASSET}.zip.sha256").read_text(
        encoding="utf-8").strip().partition("  ")
    if name != archive.name or sha256(archive) != expected:
        raise SystemExit("archive does not match its digest sidecar")
    with tempfile.TemporaryDirectory() as temporary:
        root = safe_extract(archive, temporary)
        for required in PROGRAM_FILES + (
                "BUILD-INFO.json", "SHA256SUMS", f"{ASSET}.cdx.json",
                "LICENSES/whisper.cpp-MIT.txt",
                "LICENSES/cpp-httplib-MIT.txt",
                "LICENSES/nlohmann-json-MIT.txt"):
            if not (root / required).is_file():
                raise SystemExit(f"{required} is missing from the archive")
        if not any(CPU_BACKENDS.fullmatch(path.name) for path in root.iterdir()):
            raise SystemExit("no ggml-cpu backend in the archive")
        listed = set()
        for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, _, relative = line.partition("  ")
            if sha256(root / relative) != digest:
                raise SystemExit(f"SHA256SUMS does not match {relative}")
            listed.add(relative.removeprefix("./"))
        present = {path.relative_to(root).as_posix() for path in root.rglob("*")
                   if path.is_file() and path.name != "SHA256SUMS"}
        if listed != present:
            raise SystemExit("SHA256SUMS does not cover exactly the archive: "
                             f"{sorted(listed ^ present)}")
        problems = import_problems(root)
        if problems:
            raise SystemExit("\n".join(problems))
        _check_sbom(root, pathlib.Path(args.source) if args.source else None)
    print("structure: PASS")


def _require(condition, message):
    # Not assert, which python -O strips.
    if not condition:
        raise SystemExit(message)


def _check_sbom(root, source):
    with open(root / f"{ASSET}.cdx.json", encoding="utf-8") as stream:
        document = json.load(stream)
    _require(document["bomFormat"] == "CycloneDX", "SBOM is not CycloneDX")
    _require(document["specVersion"] == "1.6", "SBOM is not CycloneDX 1.6")
    _require(document["metadata"]["component"]["name"] == "whisper-server",
             "SBOM does not describe whisper-server")
    names = {component["name"] for component in document["components"]}
    for name in ("ggml", "cpp-httplib", "json", "whisper-server.exe",
                 VULKAN_BACKEND):
        _require(name in names, f"SBOM does not list {name}")
    if source is not None:
        cmake = (source / "ggml" / "CMakeLists.txt").read_text(encoding="utf-8")
        ggml = ".".join(re.search(rf"set\(GGML_VERSION_{part}\s+([0-9]+)\)",
                                  cmake).group(1)
                        for part in ("MAJOR", "MINOR", "PATCH"))
        component = next(component for component in document["components"]
                         if component["name"] == "ggml")
        _require(component["version"] == ggml,
                 f"SBOM says ggml {component['version']}, source says {ggml}")
    print("SBOM components:", len(document["components"]))


# --- smoke ------------------------------------------------------------------


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _multipart(sample):
    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{pathlib.Path(sample).name}\"\r\n"
        "Content-Type: audio/wav\r\n\r\n".encode(),
        pathlib.Path(sample).read_bytes(),
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; "
        "name=\"response_format\"\r\n\r\njson\r\n"
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def smoke(args):
    """Start the bundle the way ggml.py does and transcribe one sample.

    The same arguments and the bundle directory as the working directory, and
    no -ng: Dikte leaves it off whenever the graphics card setting is on, so
    the run that has to survive a machine without a usable card is this one,
    where the backend registry goes looking for Vulkan. A runner has no GPU,
    so this proves the CPU fallback and nothing about the card itself.
    """
    out = pathlib.Path(args.out)
    # The question Dikte's _has_vulkan asks, so the log says which of the two
    # fallbacks this run was: no loader at all, or a loader with no device.
    loader = ctypes.util.find_library("vulkan-1")
    print(f"Vulkan loader on this machine: {loader or 'none'}", flush=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = safe_extract(out / f"{ASSET}.zip", temporary)
        server = root / "whisper-server.exe"
        port = _free_port()
        log_path = pathlib.Path(temporary) / "server.log"
        command = [str(server), "-m", str(pathlib.Path(args.model).resolve()),
                   "--inference-path", "/v1/audio/transcriptions",
                   "-l", "auto", "-sns", "-nlp",
                   "--host", "127.0.0.1", "--port", str(port)]
        body, content_type = _multipart(args.sample)
        with open(log_path, "wb") as log:
            process = subprocess.Popen(command, cwd=str(root), stdout=log,
                                       stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL)
            try:
                reply = None
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/audio/transcriptions",
                        data=body, headers={"Content-Type": content_type})
                    try:
                        with urllib.request.urlopen(request, timeout=180) as response:
                            reply = json.loads(response.read().decode("utf-8"))
                        break
                    except (urllib.error.URLError, ConnectionError, TimeoutError):
                        time.sleep(1)
            finally:
                process.kill()
                process.wait()
        text = log_path.read_text(encoding="utf-8", errors="replace")
        print(text)
        if reply is None:
            raise SystemExit("whisper-server never answered")
        print(json.dumps(reply))
        if "text" not in reply:
            raise SystemExit("the reply carries no transcript")
        # The CPU backend is a plugin too. Seeing it loaded proves the dynamic
        # backend search found the bundle's own directory.
        if "load_backend: loaded CPU backend from" not in text:
            raise SystemExit("the CPU backend was not loaded from the bundle")
    print("smoke: PASS")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build")
    command.add_argument("--source", required=True)
    command.add_argument("--work", required=True)
    command.add_argument("--out", required=True)
    command.set_defaults(run=build)
    command = commands.add_parser("validate")
    command.add_argument("--out", required=True)
    command.add_argument("--source", default="")
    command.set_defaults(run=validate)
    command = commands.add_parser("smoke")
    command.add_argument("--out", required=True)
    command.add_argument("--model", required=True)
    command.add_argument("--sample", required=True)
    command.set_defaults(run=smoke)
    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
