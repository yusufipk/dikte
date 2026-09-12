#!/usr/bin/env python3
import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path

root = Path(os.environ["ROOT"])
version = os.environ["VERSION"]
commit = os.environ["COMMIT"]
epoch = int(os.environ["EPOCH"])
ggml_version = os.environ["GGML_VERSION"]
asset = "whisper-bin-ubuntu-vulkan-x64"
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
httplib_ref = "pkg:github/yhirose/cpp-httplib@0.20.0"
json_ref = "pkg:github/nlohmann/json@3.11.2"

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
            "version": "0.20.0",
            "purl": httplib_ref,
            "licenses": [{"expression": "MIT"}],
        },
        {
            "type": "library",
            "bom-ref": json_ref,
            "group": "nlohmann",
            "name": "json",
            "version": "3.11.2",
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
