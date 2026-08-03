"""Fetch the pinned FFmpeg build for the Windows package, and check it.

FFmpeg is what turns somebody's .mp4 into something a transcription model will
take, and what a Windows user should not have to install by hand before Dikte
works. It is fetched once at package time from the release named in
ffmpeg.json and refused unless its sha256 is the one written there. That is the
same rule ggml.py holds every downloaded program to, for the same reason: this
is a program the application then runs.

    python packaging/windows/fetch_ffmpeg.py [into/]

Leaves ffmpeg.exe and ffprobe.exe in `into` (default: vendor/ next to this
file). Already there and already the right size, it does nothing.
"""

import hashlib
import json
import pathlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = HERE / "ffmpeg.json"
CHUNK = 1 << 20


def load():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def fetch(url, target, expected, total):
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(url, headers={"User-Agent": "dikte-build/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, \
            open(target, "wb") as out:
        while True:
            block = response.read(CHUNK)
            if not block:
                break
            out.write(block)
            digest.update(block)
            done += len(block)
            if total:
                sys.stderr.write(f"\r  {done / total:6.1%} of {total >> 20} MB")
                sys.stderr.flush()
    sys.stderr.write("\n")
    if digest.hexdigest() != expected:
        raise SystemExit(
            f"ffmpeg does not match its pinned checksum.\n"
            f"  expected {expected}\n  got      {digest.hexdigest()}\n"
            "Nothing was unpacked. Update ffmpeg.json deliberately, or find out "
            "why the bytes changed."
        )


def unpack(archive, wanted, into):
    """Take the named programs out, wherever in the archive they happen to be."""
    found = []
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if name not in wanted:
                continue
            target = into / name
            with zf.open(member) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            found.append(name)
    missing = set(wanted) - set(found)
    if missing:
        raise SystemExit(f"not in the archive: {', '.join(sorted(missing))}")
    return found


def main(argv):
    manifest = load()
    into = pathlib.Path(argv[0]) if argv else (HERE / "vendor")
    into.mkdir(parents=True, exist_ok=True)

    wanted = list(manifest["wanted"])
    if all((into / name).is_file() for name in wanted):
        print(f"ffmpeg already in {into}")
        return 0

    print(f"fetching {manifest['name']}")
    with tempfile.TemporaryDirectory() as scratch:
        archive = pathlib.Path(scratch) / manifest["name"]
        fetch(manifest["url"], archive, manifest["sha256"], manifest.get("size", 0))
        found = unpack(archive, wanted, into)
    print(f"unpacked {', '.join(found)} into {into}")
    print(f"licence: {manifest['license']}, {manifest['license_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
