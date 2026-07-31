"""Verified, GitHub Releases-based self-updates for the packaged macOS app."""

import hashlib
import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal

from app_version import (
    APP_BUNDLE_NAME,
    APP_VERSION,
    RELEASE_ASSET,
    RELEASE_REPOSITORY,
    RELEASE_TAG_PREFIX,
)
from i18n import t

API_URL = (
    f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases?per_page=20"
)
DOWNLOAD_PREFIX = (
    f"https://github.com/{RELEASE_REPOSITORY}/releases/download/"
)
BUNDLE_ID = "dev.dikte.app"
LEGACY_BUNDLE_NAME = "Dikte.app"
OFFICIAL_TEAM_ID = "PTQ5FN6P8U"
OFFICIAL_CODE_REQUIREMENT = (
    f'=identifier "{BUNDLE_ID}" and anchor apple generic '
    f'and certificate leaf[subject.OU] = "{OFFICIAL_TEAM_ID}"'
)
MAX_API_BYTES = 2 * 1024 * 1024
MAX_UPDATE_BYTES = 200 * 1024 * 1024
REQUEST_TIMEOUT = 30
VERSION_RE = re.compile(
    rf"^{re.escape(RELEASE_TAG_PREFIX)}(\d+)\.(\d+)\.(\d+)$"
)


@dataclass(frozen=True)
class Release:
    version: str
    version_tuple: tuple
    tag: str
    url: str
    size: int
    digest: str


@dataclass(frozen=True)
class PreparedUpdate:
    release: Release
    stage_root: pathlib.Path
    staged_app: pathlib.Path


def version_tuple(value):
    """Parse a three-part numeric version."""
    parts = str(value).split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"invalid version: {value}")
    return tuple(int(part) for part in parts)


def release_from_payload(payload):
    """Return a verified release candidate, or None for an unsuitable payload."""
    if not isinstance(payload, dict) or payload.get("draft"):
        return None
    match = VERSION_RE.fullmatch(str(payload.get("tag_name") or ""))
    if not match:
        return None

    asset = next(
        (
            item for item in payload.get("assets", [])
            if isinstance(item, dict) and item.get("name") == RELEASE_ASSET
        ),
        None,
    )
    if asset is None:
        return None
    url = str(asset.get("browser_download_url") or "")
    size = asset.get("size")
    digest = str(asset.get("digest") or "").lower()
    if not url.startswith(DOWNLOAD_PREFIX):
        return None
    if not isinstance(size, int) or not 0 < size <= MAX_UPDATE_BYTES:
        return None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return None

    numbers = tuple(int(part) for part in match.groups())
    return Release(
        version=".".join(str(part) for part in numbers),
        version_tuple=numbers,
        tag=payload["tag_name"],
        url=url,
        size=size,
        digest=digest.removeprefix("sha256:"),
    )


def select_latest_release(payloads):
    candidates = [
        release for release in
        (release_from_payload(payload) for payload in payloads)
        if release is not None
    ]
    return max(candidates, key=lambda item: item.version_tuple, default=None)


def fetch_latest_release():
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Dikte/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        if not str(response.geturl()).startswith("https://"):
            raise RuntimeError("the release API redirected to an insecure URL")
        raw = response.read(MAX_API_BYTES + 1)
    if len(raw) > MAX_API_BYTES:
        raise RuntimeError("the release response was unexpectedly large")
    payloads = json.loads(raw.decode("utf-8"))
    if not isinstance(payloads, list):
        raise RuntimeError("the release response was not a list")
    return select_latest_release(payloads)


def packaged_app_path(executable=None):
    executable = pathlib.Path(executable or sys.executable).resolve()
    if executable.parent.name != "MacOS":
        return None
    contents = executable.parent.parent
    app = contents.parent
    if contents.name != "Contents" or app.suffix != ".app":
        return None
    return app


def supported_target_app(path):
    """Updates can replace both legacy and user-visible bundle filenames."""
    path = pathlib.Path(path)
    return path.suffix == ".app" and path.name in {
        LEGACY_BUNDLE_NAME,
        APP_BUNDLE_NAME,
    }


def _safe_archive(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        for item in archive.infolist():
            path = pathlib.PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("the update archive contains an unsafe path")
            allowed = (
                path.parts
                and (
                    path.parts[0] == "Dikte.app"
                    or (
                        path.parts[0] == "__MACOSX"
                        and (
                            (len(path.parts) == 1 and item.is_dir())
                            or (
                                len(path.parts) >= 2
                                and path.parts[1] == "Dikte.app"
                            )
                        )
                    )
                )
            )
            if not allowed:
                raise RuntimeError("the update archive contains an unexpected file")


def _download(release, destination):
    request = urllib.request.Request(
        release.url,
        headers={"User-Agent": f"Dikte/{APP_VERSION}"},
    )
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        if not str(response.geturl()).startswith("https://"):
            raise RuntimeError("the update download redirected to an insecure URL")
        with open(destination, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > release.size or total > MAX_UPDATE_BYTES:
                    raise RuntimeError("the update download exceeded its declared size")
                output.write(chunk)
                digest.update(chunk)
    if total != release.size:
        raise RuntimeError("the update download size did not match GitHub")
    if digest.hexdigest() != release.digest:
        raise RuntimeError("the update SHA-256 digest did not match GitHub")


def verify_bundle(bundle, expected_version):
    bundle = pathlib.Path(bundle)
    executable = bundle / "Contents/MacOS/Dikte"
    plist_path = bundle / "Contents/Info.plist"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("the update does not contain the Dikte executable")
    with open(plist_path, "rb") as handle:
        info = plistlib.load(handle)
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise RuntimeError("the update has the wrong bundle identifier")
    if info.get("CFBundleShortVersionString") != expected_version:
        raise RuntimeError("the update bundle version does not match its release")
    subprocess.run(
        [
            "codesign", "--verify", "--deep", "--strict",
            "-R", OFFICIAL_CODE_REQUIREMENT, str(bundle),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def prepare_update(release, target_app):
    """Download, verify and stage a release on the target app's filesystem."""
    target_app = pathlib.Path(target_app).resolve()
    if not supported_target_app(target_app):
        raise RuntimeError("the application has an unsupported bundle name")
    stage_root = None
    try:
        with tempfile.TemporaryDirectory(prefix="dikte-download-") as directory:
            directory = pathlib.Path(directory)
            archive = directory / RELEASE_ASSET
            extracted = directory / "extracted"
            extracted.mkdir()
            _download(release, archive)
            _safe_archive(archive)
            subprocess.run(
                ["ditto", "-x", "-k", str(archive), str(extracted)],
                check=True,
                capture_output=True,
                text=True,
            )
            source_app = extracted / "Dikte.app"
            verify_bundle(source_app, release.version)

            stage_root = pathlib.Path(tempfile.mkdtemp(
                prefix=".Dikte-update-",
                dir=str(target_app.parent),
            ))
            staged_app = stage_root / "Dikte.app"
            subprocess.run(
                ["ditto", str(source_app), str(staged_app)],
                check=True,
                capture_output=True,
                text=True,
            )
            verify_bundle(staged_app, release.version)
        return PreparedUpdate(release, stage_root, staged_app)
    except Exception:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)
        raise


def install_prepared(prepared, target_app, old_pid):
    """Atomically swap the app and start its packaged finish-update helper."""
    target_app = pathlib.Path(target_app).resolve()
    prepared_root = prepared.stage_root.resolve()
    if prepared_root.parent != target_app.parent:
        raise RuntimeError("the staged update is not on the app filesystem")
    if not prepared_root.name.startswith(".Dikte-update-"):
        raise RuntimeError("the staged update path is invalid")
    if prepared.staged_app.resolve().parent != prepared_root:
        raise RuntimeError("the staged application path is invalid")

    backup = target_app.parent / f".Dikte-backup-{uuid.uuid4().hex}.app"
    target_app.rename(backup)
    try:
        prepared.staged_app.rename(target_app)
        helper = target_app / "Contents/MacOS/Dikte"
        subprocess.Popen(
            [
                str(helper),
                "finish-update",
                str(int(old_pid)),
                str(backup),
                prepared.release.version,
            ],
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        if target_app.exists():
            failed = prepared_root / "failed-Dikte.app"
            target_app.rename(failed)
        backup.rename(target_app)
        raise
    finally:
        try:
            prepared_root.rmdir()
        except OSError:
            pass


def finish_update(arguments):
    """Wait for the old app, remove its backup, then become the updated app."""
    if len(arguments) != 3:
        return 2
    old_pid_text, backup_text, version = arguments
    try:
        old_pid = int(old_pid_text)
    except ValueError:
        return 2
    target_app = packaged_app_path()
    backup = pathlib.Path(backup_text).resolve()
    if target_app is None:
        return 2
    if (
        backup.parent != target_app.parent
        or not backup.name.startswith(".Dikte-backup-")
        or backup.suffix != ".app"
    ):
        return 2

    for _ in range(300):
        try:
            os.kill(old_pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            return 2
        time.sleep(0.1)
    else:
        return 2

    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError:
            # The update itself is already installed. A hidden backup is less
            # harmful than refusing to relaunch the working new application.
            pass
    environment = dict(os.environ)
    environment["DIKTE_UPDATED_TO"] = version
    os.execve(sys.executable, [sys.executable], environment)


class UpdateManager(QObject):
    status_changed = pyqtSignal(str)
    update_available = pyqtSignal(str)
    ready = pyqtSignal(str, bool)
    restart_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = t("Current version: v{version}", version=APP_VERSION)
        self._busy = False
        self._lock = threading.Lock()
        self._prepared = None

    @property
    def supported(self):
        return sys.platform == "darwin" and packaged_app_path() is not None

    def set_status(self, message):
        self.status = message
        self.status_changed.emit(message)

    def check(self, manual=False):
        if not self.supported:
            self.set_status(t(
                "Updates are available in the packaged macOS app."
            ))
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        threading.Thread(
            target=self._check_worker,
            args=(manual,),
            daemon=True,
        ).start()
        return True

    def _check_worker(self, manual):
        try:
            self.set_status(t("Checking for updates…"))
            release = fetch_latest_release()
            if release is None or release.version_tuple <= version_tuple(APP_VERSION):
                self.set_status(t(
                    "Dikte is up to date (v{version}).",
                    version=APP_VERSION,
                ))
                with self._lock:
                    self._busy = False
                return
            self.update_available.emit(release.version)
            self.set_status(t(
                "Downloading Dikte v{version}…",
                version=release.version,
            ))
            target = packaged_app_path()
            prepared = prepare_update(release, target)
            with self._lock:
                self._prepared = prepared
            self.set_status(t(
                "Dikte v{version} is ready to install.",
                version=release.version,
            ))
            self.ready.emit(release.version, manual)
        except Exception as exc:
            self.set_status(t("Update failed: {error}", error=exc))
            with self._lock:
                self._busy = False

    def discard_ready(self):
        with self._lock:
            prepared = self._prepared
            self._prepared = None
            self._busy = False
        if prepared is not None:
            shutil.rmtree(prepared.stage_root, ignore_errors=True)

    def install_ready(self):
        with self._lock:
            prepared = self._prepared
            self._prepared = None
        if prepared is None:
            return False
        threading.Thread(
            target=self._install_worker,
            args=(prepared,),
            daemon=True,
        ).start()
        return True

    def _install_worker(self, prepared):
        try:
            self.set_status(t(
                "Installing Dikte v{version}…",
                version=prepared.release.version,
            ))
            install_prepared(prepared, packaged_app_path(), os.getpid())
            self.set_status(t("Update installed. Restarting Dikte…"))
            self.restart_requested.emit()
        except Exception as exc:
            shutil.rmtree(prepared.stage_root, ignore_errors=True)
            self.set_status(t("Update failed: {error}", error=exc))
            with self._lock:
                self._busy = False
