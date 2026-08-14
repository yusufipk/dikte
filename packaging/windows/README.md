# Building the Windows package

Two executables in one folder, and an installer around them:

```
dist/Dikte/DikteApp.exe    the tray application, no console behind it
dist/Dikte/dikte.exe       the command line, which needs one
dist/Dikte-1.0.0-setup.exe the installer
```

The build has to run **on Windows**. PyInstaller freezes the interpreter it is
run by; there is no cross-compilation, and the Qt libraries it collects are the
ones on this machine.

## What it needs

- Python 3.11, 3.12 or 3.13 — the same versions the tests cover
- `pip install PyQt6 PyAudioWPatch pyinstaller`
- [Inno Setup 6 or 7](https://jrsoftware.org/isdl.php), for the installer

## Building

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Version 1.0.0
```

In order, that: runs the test suite, draws `dikte.ico`, fetches and verifies
FFmpeg, freezes both executables, starts the packaged command line to prove it
starts at all, and compiles the installer.

Useful flags while iterating: `-SkipTests`, `-SkipFfmpeg`, `-SkipInstaller`,
and `-Python` when the environment is not on the PATH as `python`.

## The pieces

| File | What it is |
|---|---|
| `build.ps1` | the whole thing, in order |
| `dikte.spec` | PyInstaller: what to freeze, what to leave out |
| `make_icon.py` | draws `dikte.ico` from the same code the tray icon uses |
| `ffmpeg.json` | the FFmpeg build to carry, pinned by sha256 |
| `fetch_ffmpeg.py` | fetches it, refuses it unless the digest matches |
| `dikte.iss` | Inno Setup: a per-user installer with no elevation |

## Decisions worth knowing about

**One folder, not one file.** A one-file build unpacks itself into a temporary
directory on every start — Qt, the sound library, a few hundred files — before
the tray icon appears. Dikte is started once per login and asked for a key
press a second later.

**Per user, no administrator.** `PrivilegesRequired=lowest`, installed into
`%LOCALAPPDATA%`. This is not tidiness: Windows will not let a program running
as administrator send input to one that is not, so a Dikte installed and run
elevated could not paste into anything ordinary.

**Autostart through `HKCU\...\CurrentVersion\Run`.** A shortcut only exists
while Dikte runs, so Dikte has to be running.

**Uninstalling keeps your things.** Settings, history, meetings and downloaded
models live in `%APPDATA%\Dikte` and `%LOCALAPPDATA%\Dikte`, and the uninstaller
asks before touching them, with No as the default. The models alone can be
several gigabytes somebody chose to download.

**FFmpeg is pinned, not fetched fresh.** `ffmpeg.json` names one build and its
sha256; `fetch_ffmpeg.py` refuses anything else. A build fetched at package
time is a build nobody has run, and "whatever was on the server that afternoon"
is not something to put inside an installer. It is the GPL build, which is what
Dikte's own licence allows; the notice goes into the installer beside Dikte's.

To move to a newer FFmpeg: pick a dated `autobuild-…` tag from
[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases), take the
digest the GitHub API publishes for the `win64-gpl.zip` asset, and put the URL
and the digest in `ffmpeg.json`. Nothing else reads a version number.

**Signing.** The installer is not signed here. An unsigned installer meets
SmartScreen on a machine that has not seen it before; signing it needs a
certificate that cannot live in a repository. Sign `dist\Dikte\*.exe` and the
setup executable with `signtool` before publishing.
