#!/usr/bin/env bash
set -euo pipefail

: "${OUT_DIR:=work/out}"
: "${SOURCE_DIR:=whisper.cpp}"
asset=whisper-bin-ubuntu-vulkan-x64
archive="$OUT_DIR/$asset.tar.gz"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

test -s "$archive"
(cd "$OUT_DIR" && sha256sum --check "$asset.tar.gz.sha256")
ARCHIVE="$archive" ASSET="$asset" python3 - <<'PY'
import os
import posixpath
import tarfile

archive = os.environ["ARCHIVE"]
asset = os.environ["ASSET"]


def under_root(name):
    normalized = posixpath.normpath(name)
    return (not posixpath.isabs(normalized)
            and normalized != ".."
            and not normalized.startswith("../")
            and normalized.split("/", 1)[0] == asset)


with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle:
        if not under_root(member.name):
            raise SystemExit(f"unsafe archive member: {member.name}")
        if member.isdev() or member.isfifo():
            raise SystemExit(f"special archive member: {member.name}")
        if not (member.isdir() or member.isfile()
                or member.issym() or member.islnk()):
            raise SystemExit(f"unsupported archive member: {member.name}")
        if member.issym():
            target = posixpath.join(posixpath.dirname(member.name),
                                    member.linkname)
            if not under_root(target):
                raise SystemExit(f"unsafe symlink: {member.name}")
        if member.islnk() and not under_root(member.linkname):
            raise SystemExit(f"unsafe hardlink: {member.name}")
PY
tar -xzf "$archive" -C "$tmp"
root="$tmp/$asset"

test -x "$root/whisper-server"
test -f "$root/libwhisper.so"
test -f "$root/libggml.so"
test -f "$root/libggml-base.so"
test -f "$root/libggml-vulkan.so"
compgen -G "$root/libggml-cpu-*.so" >/dev/null
test -f "$root/LICENSES/whisper.cpp-MIT.txt"
test -f "$root/LICENSES/cpp-httplib-MIT.txt"
test -f "$root/LICENSES/nlohmann-json-MIT.txt"
(cd "$root" && sha256sum --check SHA256SUMS)

# All shipped ELF objects must be relocatable and must not remember /work.
while IFS= read -r -d '' file; do
  file "$file" | grep -q ELF || continue
  dynamic=$(readelf -d "$file")
  if ! grep -Fq 'Library runpath: [$ORIGIN]' <<<"$dynamic"; then
    echo "runpath is not \$ORIGIN in $file" >&2
    exit 1
  fi
  if grep -Eq '/(home|tmp|work)/' <<<"$dynamic"; then
    echo "build path remains in $file" >&2
    exit 1
  fi
done < <(find "$root" -type f -print0)

# Vulkan remains a plugin dependency. The executable must start without a loader.
if readelf -d "$root/whisper-server" | grep -q 'libvulkan.so'; then
  echo "whisper-server links Vulkan instead of loading it as a plugin" >&2
  exit 1
fi
readelf -d "$root/libggml-vulkan.so" | grep -q 'libvulkan.so.1'

# Ubuntu 22.04 establishes the glibc ceiling promised by this artifact.
ROOT="$root" python3 - <<'PY'
import os, pathlib, re, subprocess
root = pathlib.Path(os.environ['ROOT'])
seen = {'GLIBC': set(), 'GLIBCXX': set(), 'CXXABI': set()}
external = {
    'libc.so.6', 'libgcc_s.so.1', 'libm.so.6', 'libstdc++.so.6',
    'libvulkan.so.1', 'ld-linux-x86-64.so.2',
}
for path in root.iterdir():
    if not path.is_file() or path.is_symlink():
        continue
    header = subprocess.run(['readelf', '-h', path], text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL).stdout
    if not header:
        continue
    if 'Machine:                           Advanced Micro Devices X86-64' not in header:
        raise SystemExit(f'wrong ELF architecture: {path.name}')
    dynamic = subprocess.run(['readelf', '-d', path], text=True,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL).stdout
    needed = re.findall(r'\(NEEDED\).*\[(.*?)\]', dynamic)
    unexpected = [name for name in needed
                  if name not in external
                  and not re.fullmatch(
                      r'lib(?:whisper|ggml(?:-base)?)\.so\.\d+', name)]
    if unexpected:
        raise SystemExit(
            f'unexpected DT_NEEDED in {path.name}: {unexpected}')
    if path.name != 'libggml-vulkan.so' and 'libvulkan.so.1' in needed:
        raise SystemExit(f'Vulkan is not plugin-only in {path.name}')
    contents = path.read_bytes()
    for marker in (b'/home/', b'/tmp/', b'/work/'):
        if marker in contents:
            raise SystemExit(
                f'build path {marker!r} remains in {path.name}')
    text = subprocess.run(['objdump', '-T', path], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    for family in seen:
        pattern = rf'{family}_([0-9]+(?:\.[0-9]+)+)'
        seen[family].update(tuple(map(int, version.split('.')))
                            for version in re.findall(pattern, text))
assert seen['GLIBC'] and max(seen['GLIBC']) <= (2, 34), max(seen['GLIBC'])
assert seen['GLIBCXX'] and max(seen['GLIBCXX']) <= (3, 4, 30), max(seen['GLIBCXX'])
assert seen['CXXABI'] and max(seen['CXXABI']) <= (1, 3, 13), max(seen['CXXABI'])
for family, versions in seen.items():
    print(f'maximum {family} symbol:', '.'.join(map(str, max(versions))))
PY

python3 - "$root/$asset.cdx.json" "$SOURCE_DIR" <<'PY'
import json, pathlib, re, sys
with open(sys.argv[1], encoding='utf-8') as stream:
    doc = json.load(stream)
assert doc['bomFormat'] == 'CycloneDX'
assert doc['specVersion'] == '1.6'
assert doc['metadata']['component']['name'] == 'whisper-server'
assert len(doc['components']) >= 3
root = pathlib.Path(sys.argv[1]).parent
source = pathlib.Path(sys.argv[2])

def one_match(pattern, path):
    matches = re.findall(pattern, path.read_text(encoding='utf-8'), re.MULTILINE)
    assert len(matches) == 1, (path, matches)
    return matches[0]

def numeric_version(version):
    assert re.fullmatch(
        r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)',
        version,
    ), version
    return version

libraries = [path for path in root.glob('libggml.so.*.*.*')
             if path.is_file() and not path.is_symlink()]
assert len(libraries) == 1, libraries
ggml_version = numeric_version(libraries[0].name.removeprefix('libggml.so.'))
httplib_version = numeric_version(one_match(
    r'^#define\s+CPPHTTPLIB_VERSION\s+"([^"]+)"\s*$',
    source / 'examples/server/httplib.h',
))
json_header = source / 'examples/json.hpp'
json_version = numeric_version('.'.join(one_match(
    rf'^#define\s+NLOHMANN_JSON_VERSION_{part}\s+([0-9]+)(?:\s|$).*',
    json_header,
) for part in ('MAJOR', 'MINOR', 'PATCH')))
expected = {
    'ggml': ('ggml-org/ggml', ggml_version),
    'cpp-httplib': ('yhirose/cpp-httplib', httplib_version),
    'json': ('nlohmann/json', json_version),
}
for name, (repository, version) in expected.items():
    components = [component for component in doc['components']
                  if component['name'] == name]
    assert len(components) == 1, (name, components)
    component = components[0]
    assert component['version'] == version, (name, component['version'], version)
    assert component['purl'] == f'pkg:github/{repository}@v{version}'
print('SBOM components:', len(doc['components']))
PY

LD_LIBRARY_PATH='' "$root/whisper-server" --help >/dev/null 2>&1

echo "structure: PASS"
