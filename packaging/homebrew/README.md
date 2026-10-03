# Homebrew packaging

`Casks/dikte.rb` installs the existing **v2.0.0** macOS release: a native ARM64
DMG on Apple silicon, or an x86_64 DMG on Intel. Each has a pinned SHA-256,
verified against the downloaded bytes and GitHub release asset digest. The
cask installs `Dikte.app` and links its executable as `dikte` in Homebrew's bin
directory. It does not build Python or download a second copy of ffmpeg.

This is an own-tap-compatible cask in the Dikte repository. No separate tap has
been published, and this is not an accepted official Homebrew cask. Thanks to
[@nomoreshow](https://github.com/nomoreshow) for proposing Homebrew distribution
in [#102](https://github.com/yusufipk/dikte/issues/102); the issue contains no
proposed implementation to reuse.

## Try a reviewed checkout locally

On macOS with Homebrew installed, from the root of this checkout:

```sh
brew tap-new --no-git dikte-local/testing
mkdir -p "$(brew --repository dikte-local/testing)/Casks"
cp Casks/dikte.rb "$(brew --repository dikte-local/testing)/Casks/dikte.rb"
brew install --cask dikte-local/testing/dikte
```

The local tap name above is created on your machine; it does not refer to a
published GitHub repository. Run `tap-new` only once. After reviewing a newer
cask from this repository, copy it to the same local tap again and run:

```sh
brew upgrade --cask dikte-local/testing/dikte
```

`brew update` cannot update this local copy. A published remote tap would be
needed for automatic discovery of cask updates. The cask follows stable tags,
not the replaceable `latest` prerelease. Updating the cask does not include
unreleased Dikte features from master.

If another installation already provides `Dikte.app` or `dikte`, Homebrew
should stop with a conflict. Quit the app, back up your data, disable its login
startup, and choose which installation to keep before retrying. Do not use
`--force` to overwrite an unrelated app or command. `type -a dikte` can reveal
an older `~/.local/bin/dikte` wrapper ahead of Homebrew in PATH.

## Launch, permissions and removal

These release bundles are ad-hoc signed, **not Developer ID signed or
notarized**. A checksum verifies the expected download; it does not confer
Apple trust. Homebrew installation does not remove quarantine or change
Gatekeeper, TCC, microphone or Accessibility settings. macOS may block the
first launch. Review Dikte in System Settings > Privacy & Security and approve
it only if you trust it. Microphone and Accessibility permissions still
require consent and may be requested again after an upgrade.

Use the app for interactive dictation, or `dikte --help` and `dikte --version`
for the CLI. The Homebrew link points to the existing executable; it does not
change Dikte's permission model. Homebrew does not enable login startup. Dikte
itself can create a login item and a `~/.local/bin/dikte` wrapper when launched.

Before uninstalling, verify that the login item belongs to this installed app.
If it does, turn off start-at-login in Dikte; if it belongs to a separate source
installation, leave it in place. Quit the app, then run:

```sh
brew uninstall --cask dikte-local/testing/dikte
```

The cask deliberately has no `zap` or recursive user-data removal. Settings,
history, recordings, meetings and models in `~/Library/Application Support/Dikte`,
and `~/Library/Caches/Dikte`, are preserved during upgrades and removal.
Existing login items and `~/.local/bin` wrappers are also left alone: they may
belong to a source installation. If startup was not disabled, its entry can
remain pointing to a removed app. Removing an entry through Dikte before uninstalling avoids that stale entry,
but only do so after verifying that it belongs to the app being removed.

## Maintain a release

1. Wait for both DMGs on a new stable `vX.Y.Z` release. Do not point this cask
   at the mutable `latest` release, use `:no_check`, or invent an asset name.
2. Download both assets from that release, run `shasum -a 256` on each, and
   compare with GitHub's release asset digests. Change `version` and both
   architecture hashes together in `Casks/dikte.rb`.
3. Keep `PREVIOUS_VERSION` and `PREVIOUS_SHA` in `check.py` on a real older
   stable release with both assets available. Advance that baseline when
   useful. Never manufacture a Homebrew receipt to simulate an upgrade.
4. Run the `homebrew` workflow on both macOS architectures and review its
   results before merging. The online audit follows the newest stable release,
   so a newer release intentionally makes an outdated cask fail its audit.

The workflow creates an unpublished local tap, runs actual `brew style` and
`brew audit --cask --online`, then installs and uninstalls the current release,
installs 1.3.0, upgrades it to the cask version, and uninstalls it. It checks
bundle version, architecture, code-signature integrity, retained quarantine,
CLI help/version, Homebrew receipts and byte-for-byte preservation of synthetic
user data and file permissions. The script is restricted to disposable macOS
GitHub Actions runners. It does not record audio, call transcription APIs,
download models or test interactive permission dialogs. It does not run
`audit --new`/`--signing`: those official-submission checks require trust these
ad-hoc signed releases do not have. If Gatekeeper blocks execution on a runner,
that is a validation failure to report, not a reason to weaken security.

## A future remote tap

A maintainer can later create a separate `homebrew-<tap-name>` repository,
copy the reviewed `Casks/dikte.rb` into its `Casks/` directory, and arrange for
reviewed version/hash updates after releases. Only **after that repository
exists and contains the cask** would `brew tap <owner>/<tap-name>` followed by
`brew install --cask <owner>/<tap-name>/dikte` work. These are placeholders, not
an installation command for an existing service. This change creates no
external repository, registry publication or official Homebrew submission.

See Homebrew's [Cask Cookbook](https://docs.brew.sh/Cask-Cookbook) and
[tap maintenance guide](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap).
