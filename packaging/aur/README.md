# Arch Linux and CachyOS package recipe

This is an in-repository recipe, not an AUR publication or a claim to an AUR
package name. CachyOS uses Arch's package tools; it does not need a separate
package. Review the PKGBUILD before building it.

## Install

On an up-to-date Arch or CachyOS system, from this directory:

```sh
sudo pacman -Syu --needed base-devel
makepkg -si
```

Run `makepkg` as your normal user. It asks for privilege only to install
system dependencies and the resulting package. Start Dikte from your application
menu or with `dikte`. Install the optional clipboard/paste tools shown by pacman
for your desktop: `wl-clipboard`, `ydotool`, `qt6-wayland` on Wayland, or `xclip`
and `xdotool` on X11. Recording needs a running PulseAudio-compatible server
(PipeWire with `pipewire-pulse`, as normally used by CachyOS, or PulseAudio).
`libpulse` supplies `parec` and `pactl`; `ffmpeg` handles file and meeting audio.
The ydotool daemon must already be configured for your session to auto-paste.
The package does not change services, groups, shortcuts or device permissions.

Existing manual installations can shadow `/usr/bin/dikte` with a launcher in
`~/.local/bin`; check `command -v dikte` and review any old user desktop/autostart
entries yourself. The package never removes or overwrites these user files.
Autostart and global shortcuts remain user choices; configure them in your
desktop using `dikte --gui` and `dikte toggle` respectively.

Upgrade by reviewing the newer recipe and running `makepkg -si` again.
Remove with `sudo pacman -R dikte`. Settings, downloaded models and history in
your user directories remain intact. Do not run the checkout's `install.sh`
or `scripts/update.sh` to manage this package.

## Why source, and which source?

The recipe packages stable **v2.0.0**, peeled commit
`b6a15fda93738d6c0f8556e09ea6c3ab62d3e113`, with a reviewed SHA-256 of the source
archive and both local launcher files. The `latest` release is a moving
prerelease and is deliberately not used. The stable release also offers an
x86_64 AppImage, but a source package lets pacman manage Python and Qt updates,
avoids a second bundled runtime/FUSE dependency, and avoids the frozen build's
automatic personal desktop integration. This is pure Python plus generated
icons, so the package is architecture-independent; bundled local inference
engines still have their upstream architecture limitations.

Only application modules, icons, the system menu entry, launcher and README
are installed. There is no `.install` hook, autostart file, model download,
API credential, or root invocation of the personal installer. The launcher
uses Python isolated mode and clears Python path overrides for GUI respawns,
so the working directory and `PYTHONPATH` cannot replace the installed application.

The release pin means unreleased master fixes/features are not included.
For an upstream version update, review the new release and commit, download
and independently hash its archive, update `pkgver`, `_commit`, checksums and
reset `pkgrel=1`. For packaging-only changes increment `pkgrel`. Regenerate
`.SRCINFO` with `makepkg --printsrcinfo > .SRCINFO` and rerun validation. Keep
checksums enforced; never replace them with `SKIP`.

## Reproduce validation

From the repository root with Docker available:

```sh
docker build -t dikte-arch-check packaging/aur
docker run -d --name arch-check dikte-arch-check sleep infinity
docker exec arch-check bash /recipe/validate.sh build
docker network disconnect bridge arch-check
docker exec arch-check bash /recipe/validate.sh lifecycle
docker cp arch-check:/tmp/namcap.log ./namcap.log
docker rm -f arch-check
```

Builds run as an unprivileged container user. The check runs the pinned
release's unit/integration suite and desktop-file validation, compares generated
`.SRCINFO`, and runs namcap (errors fail; review warnings). Lifecycle validation
installs with pacman, starts the real installed CLI and offscreen Qt GUI,
queries it over IPC, quits, upgrades to a synthetic packaging revision, repeats
the smoke test, then uninstalls and verifies user config is retained. That stage
has no network, sound device, real credentials or model downloads. It does not
claim live microphone, desktop shortcut/paste, or transcription/model E2E
coverage. The Arch CI job performs these same steps; existing CI continues to
exercise current master-derived application code on Linux, macOS and Windows.

The source inputs are pinned. Arch dependencies are intentionally resolved
from its current repositories, so this does not promise byte-identical outputs
across different Python/Qt versions or repository snapshots.
