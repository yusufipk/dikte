#!/usr/bin/env python3
"""Exercise real Homebrew and released DMGs on disposable macOS Actions runners.

No audio, model downloads, paid APIs, GUI startup or permission changes. Never
run on a personal Mac: the fixture occupies Dikte's normal user-data paths.
"""

import json
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
TAP = "dikte-ci/local"
CASK = f"{TAP}/dikte"
PREVIOUS_VERSION = "1.3.0"
PREVIOUS_SHA = {
    "arm": "bbcea3d2e28076a2b290f2e04d922dd9bb51916ce79d5267b08bdd08a9879d8b",
    "intel": "404201c92617f9ed68a7c4e434ad2279fd5a8803195de4c105ddad58d0364710",
}


def run(*args):
    print("+", " ".join(map(str, args)), flush=True)
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=900)
    if result.returncode:
        print(result.stdout, flush=True)
        result.check_returncode()
    return result.stdout.strip()


def main():
    if (platform.system() != "Darwin"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise SystemExit("Use a disposable macOS GitHub Actions runner.")
    source = (ROOT / "Casks/dikte.rb").read_text()
    current_version = re.search(r'^  version "([\d.]+)"$', source, re.M).group(1)
    previous = re.sub(r'^  version "[\d.]+"$',
                      f'  version "{PREVIOUS_VERSION}"', source, flags=re.M)
    for arch, sha in PREVIOUS_SHA.items():
        previous = re.sub(rf'({arch}:\s+")[a-f0-9]{{64}}"',
                          rf'\g<1>{sha}"', previous)

    support = Path.home() / "Library/Application Support/Dikte"
    cache = Path.home() / "Library/Caches/Dikte"
    for path in (support, cache):
        if path.exists():
            raise SystemExit(f"Refusing to touch existing user data: {path}")
    # These are synthetic preservation fixtures, not real recordings or models.
    fixtures = {
        support / "config.json": b'{"language": "en"}\n',
        support / "history.jsonl": b'{"text": "Synthetic packaging test"}\n',
        support / "recordings/fixture.wav": b"synthetic recording sentinel\n",
        support / "meetings/fixture.md": b"Synthetic meeting\n",
        support / "models/fixture.gguf": b"synthetic model sentinel\n",
        cache / "fixture": b"synthetic cache sentinel\n",
    }
    for path, contents in fixtures.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        path.chmod(0o600)

    def preserved():
        for path, contents in fixtures.items():
            assert path.read_bytes() == contents, f"User data changed: {path}"
            assert path.stat().st_mode & 0o777 == 0o600, f"Permissions changed: {path}"

    # tap-new creates only a local tap. It does not publish or contact a tap repo.
    run("brew", "tap-new", "--no-git", TAP)
    tap = Path(run("brew", "--repository", TAP))
    cask_file = tap / "Casks/dikte.rb"
    cask_file.parent.mkdir(exist_ok=True)
    cask_file.write_text(source)
    print(run("brew", "style", str(cask_file)))
    # --new/--signing test official Homebrew acceptance, which these ad-hoc
    # signed releases cannot meet. The online audit still downloads the DMG.
    print(run("brew", "audit", "--cask", "--online", CASK))

    with tempfile.TemporaryDirectory(prefix="dikte-apps-") as appdir:
        app = Path(appdir) / "Dikte.app"
        binary = Path(run("brew", "--prefix")) / "bin/dikte"

        def installed(version):
            with (app / "Contents/Info.plist").open("rb") as fh:
                info = plistlib.load(fh)
            assert info["CFBundleShortVersionString"] == version, info
            assert info["CFBundleIdentifier"] == "io.github.yusufipk.dikte", info
            assert binary.is_symlink(), "Homebrew CLI link missing"
            assert binary.resolve() == app / "Contents/MacOS/Dikte"
            assert run("lipo", "-archs", str(binary.resolve())) == platform.machine()
            run("codesign", "--verify", "--deep", str(app))
            # Read-only: quarantine must remain on the installed application.
            assert run("xattr", "-p", "com.apple.quarantine", str(app))
            assert run(str(binary), "--version") == f"dikte {version}"
            assert "usage:" in run(str(binary), "--help")
            preserved()

        # First exercise a clean install of exactly the cask under review.
        print(run("brew", "install", "--cask", f"--appdir={appdir}", CASK))
        installed(current_version)
        print(run("brew", "uninstall", "--cask", CASK))
        assert not app.exists() and not binary.is_symlink()
        preserved()

        # Then upgrade real older release bytes using a changed local tap.
        # Do not fake an installed receipt or call reinstall an upgrade test.
        cask_file.write_text(previous)
        print(run("brew", "install", "--cask", f"--appdir={appdir}", CASK))
        installed(PREVIOUS_VERSION)
        cask_file.write_text(source)
        print(run("brew", "upgrade", "--cask", f"--appdir={appdir}", CASK))
        installed(current_version)
        installed_info = json.loads(run("brew", "info", "--cask", "--json=v2", CASK))
        assert installed_info["casks"][0]["installed"] == current_version
        print(run("brew", "uninstall", "--cask", CASK))
        assert not app.exists() and not binary.is_symlink()
        preserved()
    print("Real DMG install, upgrade, CLI and uninstall passed; synthetic user data preserved.")


if __name__ == "__main__":
    main()
