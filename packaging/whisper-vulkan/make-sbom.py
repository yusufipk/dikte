#!/usr/bin/env python3
import datetime
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

root = Path(os.environ["ROOT"])
source = Path(os.environ["SOURCE_DIR"])
version = os.environ["VERSION"]
commit = os.environ["COMMIT"]
epoch = int(os.environ["EPOCH"])
asset = "whisper-bin-ubuntu-vulkan-x64"


def one_match(pattern, path, component):
    matches = re.findall(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"expected one {component} version in {path}, got {matches}")
    return matches[0]


def numeric_version(version, component):
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError(f"invalid {component} version: {version}")
    return version


ggml_libraries = [path for path in root.glob("libggml.so.*.*.*")
                  if path.is_file() and not path.is_symlink()]
if len(ggml_libraries) != 1:
    raise ValueError(f"expected one versioned GGML library, got {ggml_libraries}")
ggml_version = numeric_version(
    ggml_libraries[0].name.removeprefix("libggml.so."), "GGML",
)
httplib_version = numeric_version(one_match(
    r'^#define\s+CPPHTTPLIB_VERSION\s+"([^"]+)"\s*$',
    source / "examples/server/httplib.h", "cpp-httplib",
), "cpp-httplib")
json_header = source / "examples/json.hpp"
json_version = numeric_version(".".join(one_match(
    rf"^#define\s+NLOHMANN_JSON_VERSION_{part}\s+([0-9]+)(?:\s|$).*",
    json_header, f"nlohmann-json {part.lower()}",
) for part in ("MAJOR", "MINOR", "PATCH")), "nlohmann-json")
sbom_path = root / f"{asset}.cdx.json"

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

files = []
for path in sorted(root.rglob("*")):
    if path != sbom_path and path.is_file() and not path.is_symlink():
        rel = path.relative_to(root).as_posix()
        files.append({
            "type": "file",
            "bom-ref": f"file:{rel}",
            "name": rel,
            "hashes": [{"alg": "SHA-256", "content": digest(path)}],
        })

ts = datetime.datetime.fromtimestamp(
    epoch, datetime.timezone.utc,
).isoformat().replace("+00:00", "Z")
root_ref = f"pkg:github/ggml-org/whisper.cpp@{version}?commit={commit}"
ggml_ref = f"pkg:github/ggml-org/ggml@v{ggml_version}"
httplib_ref = f"pkg:github/yhirose/cpp-httplib@v{httplib_version}"
json_ref = f"pkg:github/nlohmann/json@v{json_version}"

sbom = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.6",
    "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, root_ref)}",
    "version": 1,
    "metadata": {
        "timestamp": ts,
        "tools": {"components": [
            {"type": "application", "name": "make-sbom.py", "version": "1"},
            {"type": "application", "name": "CMake", "version": "3.31.6"},
            {"type": "application", "name": "glslc", "version": "2025.2"},
        ]},
        "component": {
            "type": "application",
            "bom-ref": root_ref,
            "group": "ggml-org",
            "name": "whisper-server",
            "version": version,
            "purl": root_ref,
            "licenses": [{"expression": "MIT"}],
            "externalReferences": [{
                "type": "vcs",
                "url": f"https://github.com/ggml-org/whisper.cpp/tree/{commit}",
            }],
            "properties": [
                {"name": "dikte:asset-name", "value": f"{asset}.tar.gz"},
                {"name": "dikte:source-commit", "value": commit},
                {"name": "dikte:runtime:glibc-minimum", "value": "2.34"},
                {"name": "dikte:runtime:glibcxx-minimum", "value": "3.4.30"},
                {"name": "dikte:runtime:vulkan-loader", "value": "optional; libvulkan.so.1"},
            ],
        },
    },
    "components": [
        {
            "type": "library",
            "bom-ref": ggml_ref,
            "group": "ggml-org",
            "name": "ggml",
            "version": ggml_version,
            "purl": ggml_ref,
            "licenses": [{"expression": "MIT"}],
            "properties": [{
                "name": "dikte:source",
                "value": "vendored by the pinned whisper.cpp commit",
            }],
        },
        {
            "type": "library",
            "bom-ref": httplib_ref,
            "group": "yhirose",
            "name": "cpp-httplib",
            "version": httplib_version,
            "purl": httplib_ref,
            "licenses": [{"expression": "MIT"}],
        },
        {
            "type": "library",
            "bom-ref": json_ref,
            "group": "nlohmann",
            "name": "json",
            "version": json_version,
            "purl": json_ref,
            "licenses": [{"expression": "MIT"}],
        },
        *files,
    ],
    "dependencies": [{
        "ref": root_ref,
        "dependsOn": [ggml_ref, httplib_ref, json_ref]
                     + [item["bom-ref"] for item in files],
    }],
}
json.dump(sbom, fp=os.sys.stdout, indent=2, sort_keys=True)
print()
