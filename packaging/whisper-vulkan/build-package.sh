#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

: "${SOURCE_DIR:=/src}"
: "${OUT_DIR:=/work/out}"
: "${WHISPER_VERSION:=1.9.3}"
: "${WHISPER_COMMIT:=371b5a7561823ab2bb32142d2751e35e7534727b}"
: "${SOURCE_DATE_EPOCH:=1787219223}"

export SOURCE_DATE_EPOCH TZ=UTC LC_ALL=C LANG=C
asset=whisper-bin-ubuntu-vulkan-x64
build=/work/build
source_copy=/work/source
root="$OUT_DIR/root/$asset"

rm -rf "$build" "$source_copy" "$OUT_DIR"
mkdir -p "$build" "$root/LICENSES"
# Upstream configures bindings/javascript/package.json in the source directory.
# Build a private copy so the checked-out, verified source remains untouched.
cp -a "$SOURCE_DIR" "$source_copy"
chmod -R u+w "$source_copy"
git config --global --add safe.directory "$source_copy"

cmake -S "$source_copy" -B "$build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_BUILD_RPATH='$ORIGIN' \
  -DCMAKE_INSTALL_RPATH='$ORIGIN' \
  -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
  -DCMAKE_C_FLAGS="-ffile-prefix-map=$source_copy=. -fdebug-prefix-map=$source_copy=. -fmacro-prefix-map=$source_copy=." \
  -DCMAKE_CXX_FLAGS="-ffile-prefix-map=$source_copy=. -fdebug-prefix-map=$source_copy=. -fmacro-prefix-map=$source_copy=." \
  -DBUILD_SHARED_LIBS=ON \
  -DGGML_BACKEND_DL=ON \
  -DGGML_CPU_ALL_VARIANTS=ON \
  -DGGML_NATIVE=OFF \
  -DGGML_CCACHE=OFF \
  -DGGML_OPENMP=OFF \
  -DGGML_VULKAN=ON \
  -DWHISPER_BUILD_EXAMPLES=ON \
  -DWHISPER_BUILD_SERVER=ON \
  -DWHISPER_BUILD_TESTS=OFF \
  -DWHISPER_BUILD_IS_DEV=OFF \
  -DWHISPER_CURL=OFF \
  -DWHISPER_SDL2=OFF \
  -DWHISPER_COMMON_FFMPEG=OFF \
  -DWHISPER_BUILD_COMMIT="$WHISPER_COMMIT" \
  -DWHISPER_BUILD_NUMBER=0
cmake --build "$build" --target whisper-server --parallel "$(nproc)"

# Package an allowlist, not everything examples/ happens to build in the future.
cp -a "$build/bin/whisper-server" "$root/"
cp -a "$build/bin"/libwhisper.so* "$root/"
cp -a "$build/bin"/libggml.so* "$root/"
cp -a "$build/bin"/libggml-base.so* "$root/"
cp -a "$build/bin"/libggml-cpu*.so* "$root/"
cp -a "$build/bin"/libggml-vulkan.so* "$root/"
# Strip real ELF files only; preserve the SONAME symlink chains.
while IFS= read -r -d '' file; do
  if file "$file" | grep -q ELF; then
    strip --strip-unneeded "$file"
    patchelf --set-rpath '$ORIGIN' "$file"
  fi
done < <(find "$root" -type f -print0)

cp "$SOURCE_DIR/LICENSE" "$root/LICENSES/whisper.cpp-MIT.txt"
cp /packaging/licenses/cpp-httplib-MIT.txt "$root/LICENSES/"
cp /packaging/licenses/nlohmann-json-MIT.txt "$root/LICENSES/"

cat > "$root/BUILD-INFO.json" <<EOF
{
  "asset": "$asset.tar.gz",
  "source": "https://github.com/ggml-org/whisper.cpp",
  "source_version": "v$WHISPER_VERSION",
  "source_commit": "$WHISPER_COMMIT",
  "source_date_epoch": $SOURCE_DATE_EPOCH,
  "build_platform": "ubuntu-22.04-x86_64",
  "base_image": "ubuntu@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc",
  "cmake": "3.31.6",
  "cmake_flags": [
    "BUILD_SHARED_LIBS=ON",
    "C/CXX_FILE_PREFIX_MAP=/work/source=.",
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
    "WHISPER_COMMON_FFMPEG=OFF"
  ],
  "runtime_contract": {
    "minimum_glibc": "2.34",
    "minimum_glibcxx": "3.4.30",
    "required": ["x86_64 Linux", "glibc", "libstdc++.so.6", "libgcc_s.so.1"],
    "optional_gpu": ["libvulkan.so.1", "a working Vulkan ICD"],
    "cpu_fallback": "dynamic CPU backends are included; -ng forces CPU"
  }
}
EOF

# A deterministic CycloneDX sidecar generated from the files actually shipped.
ROOT="$root" SOURCE_DIR="$source_copy" VERSION="$WHISPER_VERSION" \
COMMIT="$WHISPER_COMMIT" EPOCH="$SOURCE_DATE_EPOCH" \
python3 /packaging/make-sbom.py > "$root/$asset.cdx.json"

(
  cd "$root"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum
) > "$root/SHA256SUMS"

mkdir -p "$OUT_DIR"
tar --sort=name --owner=0 --group=0 --numeric-owner \
  --mtime="@$SOURCE_DATE_EPOCH" \
  --pax-option=delete=atime,delete=ctime \
  -C "$OUT_DIR/root" -cf - "$asset" \
  | gzip -n -9 > "$OUT_DIR/$asset.tar.gz"
(
  cd "$OUT_DIR"
  sha256sum "$asset.tar.gz" > "$asset.tar.gz.sha256"
)
cp "$root/$asset.cdx.json" "$OUT_DIR/$asset.cdx.json"
