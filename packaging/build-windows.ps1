#!/usr/bin/env pwsh
# The Windows download: one setup program, carrying everything Dikte needs to
# record and to be started again after a sign-in.
#
# Run from anywhere; it works in build\ at the top of the checkout and leaves
# the finished .exe in dist\. x64 only, because that is what the PyQt6 wheel and
# whisper.cpp both publish for Windows; a Windows on ARM machine runs it under
# the emulation it runs everything else under.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build"
$out = Join-Path $root "dist"
$dist = Join-Path $build "dist\dikte"

$env:PYTHONPATH = $root
$version = & python -c "import dikte; print(dikte.__version__)"
if ($LASTEXITCODE -ne 0) { throw "could not read the version out of dikte/__init__.py" }

# A pinned tag and a checksum rather than "whatever is newest": this binary goes
# out inside something people run, so what it is has to be decided here and not
# by whoever pushes to that repository next. The same release the disk image
# takes its ffmpeg from, which is gyan.dev's essentials build repackaged, and
# dshow is in it, which is the one part of ffmpeg recording here goes through.
$ffmpegTag = "b6.1.1"
$ffmpegAsset = "ffmpeg-win32-x64.gz"
$ffmpegSha = "8883A3DFFBD0A16CF4EF95206EA05283F78908DBFB118F73C83F4951DCC06D77"

if (Test-Path $build) { Remove-Item $build -Recurse -Force }
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path $build, $out | Out-Null

# 1. The icon ---------------------------------------------------------------
# Drawn by Dikte itself, offscreen, which is why there is no image file in the
# repository. Before the application, because PyInstaller writes it into the
# executable rather than beside it, and the setup program uses the same file.
$icon = Join-Path $build "Dikte.ico"
$env:QT_QPA_PLATFORM = "offscreen"
& python -m dikte.trayicon --ico $icon
if ($LASTEXITCODE -ne 0) { throw "the icon would not draw" }
Remove-Item Env:\QT_QPA_PLATFORM
$env:DIKTE_ICO = $icon

# 2. The application --------------------------------------------------------
# Two executables in the one directory: Dikte.exe, which is windowed and is
# what a shortcut starts, and dikte-cli.exe, which has a console and is what
# the `dikte` command runs. Their names differ by more than case on purpose;
# Windows would otherwise keep one file for both.
& python -m PyInstaller (Join-Path $root "packaging\dikte.spec") `
  --distpath (Join-Path $build "dist") --workpath (Join-Path $build "work") `
  --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# And the check that the two are really two. Windows matches a filename
# without regard to its case, so a rename that leaves them a case apart puts
# one file in the directory and whichever was written second is what both
# names find. It cost a windowed build once, silently: the machines the
# packaging is otherwise checked on all have case-sensitive filesystems, so
# this only ever shows up here. Subsystem 2 is windowed, 3 is a console, and
# it is the field the loader reads to decide which to give.
function Get-PeSubsystem($path) {
  $bytes = [System.IO.File]::ReadAllBytes($path)
  $header = [BitConverter]::ToInt32($bytes, 0x3C)
  return [BitConverter]::ToUInt16($bytes, $header + 0x5C)
}
foreach ($pair in @(@("Dikte.exe", 2), @("dikte-cli.exe", 3))) {
  $subsystem = Get-PeSubsystem (Join-Path $dist $pair[0])
  if ($subsystem -ne $pair[1]) {
    throw "$($pair[0]) is subsystem $subsystem, expected $($pair[1]): the two executables collided"
  }
}

# 3. ffmpeg -----------------------------------------------------------------
# Recording on Windows goes through ffmpeg's DirectShow input, and Windows
# ships nothing like it, so without this the download would be an application
# that cannot record until the person who downloaded it installs one. bin\
# beside the executables, because integrate.py puts that directory in front of
# PATH at startup and everything reaching for ffmpeg goes through shutil.which.
$archive = Join-Path $build $ffmpegAsset
Invoke-WebRequest -UseBasicParsing -OutFile $archive `
  "https://github.com/eugeneware/ffmpeg-static/releases/download/$ffmpegTag/$ffmpegAsset"
$got = (Get-FileHash $archive -Algorithm SHA256).Hash
if ($got -ne $ffmpegSha) { throw "ffmpeg checksum: expected $ffmpegSha, got $got" }

$bin = Join-Path $dist "bin"
New-Item -ItemType Directory -Path $bin | Out-Null
$compressed = [System.IO.File]::OpenRead($archive)
$stream = New-Object System.IO.Compression.GzipStream(
  $compressed, [System.IO.Compression.CompressionMode]::Decompress)
$binary = [System.IO.File]::Create((Join-Path $bin "ffmpeg.exe"))
try { $stream.CopyTo($binary) } finally { $binary.Dispose(); $stream.Dispose(); $compressed.Dispose() }

# 4. Portable build ---------------------------------------------------------
$portableFile = Join-Path $dist 'portable'
New-Item -ItemType File $portableFile | Out-Null
$zipPath = "dist/Dikte-$version-x64-portable.zip"
Compress-Archive -Path (Join-Path $dist '*') -DestinationPath $zipPath
Remove-Item $portableFile

# 5. The setup program ------------------------------------------------------
# Inno Setup comes with the GitHub runner. On a machine that has not got it:
# winget install JRSoftware.InnoSetup
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
  $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $iscc)) {
  throw "no Inno Setup found. Install it with: winget install JRSoftware.InnoSetup"
}
& $iscc "/DVersion=$version" "/DSource=$dist" "/DIcon=$icon" `
  (Join-Path $root "packaging\dikte.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Write-Host "dist\Dikte-$version-x64-setup.exe"
