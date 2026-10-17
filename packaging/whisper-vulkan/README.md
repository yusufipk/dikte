# The Vulkan whisper-server bundle

whisper.cpp publishes a CPU-only archive for Linux, and CPU, BLAS and CUDA
ones for Windows, so most graphics cards are out of reach through the Download
button. This directory builds the archives upstream does not: `whisper-server`
with a dynamic Vulkan backend next to the CPU ones, for x86_64, against the
Ubuntu 22.04 runtime contract, and for Windows x64 with `windows_bundle.py`.

It is published as a release of Dikte's own, `whisper.cpp-v<version>` for
Linux and `whisper.cpp-v<version>-windows` for Windows, each marked
as a prerelease and kept off Latest so that neither the update check nor the
download page picks it up. `dikte/ggml.py` fetches it by tag and installs it
only when the archive's digest is the reviewed one; anything else falls back
to upstream's CPU archive, and the settings window says when it did.

## Publishing a new bundle

1. Enable GitHub's immutable releases setting for the repository, and give the
   `dependency-release` environment a required reviewer. Both are repository
   settings, not something this workflow can do for itself.
2. Run **whisper.cpp Vulkan bundle** on `master` with the new version, its
   peeled commit, `platforms`, empty expected digests and `publish: false`.
   The run reports each digest and refuses to publish an unreviewed one.
3. Review each digest against a build of your own, then run again with it in
   `expected_sha256` or `expected_windows_sha256` and `publish: true`. With
   `both`, each platform publishes or fails on its own.
4. Write the version and digest into `MANAGED_WHISPER_VERSION` and
   `MANAGED_WHISPER_SHA256` (or their `_WINDOWS_` twins) in `dikte/ggml.py`
   and `REVIEWED_WHISPER_*` in the workflow. Both derive the tag, with a
   `-windows` suffix for Windows. `tests/test_packaging.py` holds them
   together.
5. Ship a Dikte release. Until one goes out, nobody's Dikte knows the new
   bundle exists.

## What this costs, and what it does not promise

The digest lives in Dikte's source, so a backend update is a Dikte release.
Linux x86_64 machines with a Vulkan loader stay on the pinned whisper.cpp
version until step 5 happens, while every other platform follows upstream's
newest release on its own. That is the deliberate trade: an executable Dikte
downloads is not allowed to change without a reviewed digest behind it.

The build is deterministic between two runs of the same builder, not across
time. The base image, the CMake tarball, the LunarG packages and the direct
apt packages are pinned by digest or version, but the Ubuntu and LunarG
repository metadata behind them is not, and LunarG drops superseded packages.
A rebuild months later can fail to resolve, or resolve to something that
produces a different digest. Treat the published archive as the artifact, not
as something reproducible on demand: a version bump means building,
validating, reviewing the new digest and updating the pinned tuple together.

Windows is weaker still: an update of the `windows-latest` image can change
the digest, and its runner has no GPU, so somebody with a card has to try the
Vulkan path before a digest is reviewed. `windows_bundle.py` says why.
