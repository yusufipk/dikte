# Build the Windows package: the icon, the two executables, the installer.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#   ... -Version 1.2.0 -SkipInstaller -SkipFfmpeg
#
# Has to run on Windows: PyInstaller freezes the interpreter it is run by, and
# there is no cross-compilation. Needs the environment Dikte runs in (PyQt6,
# PyAudioWPatch, pyinstaller) and, for the installer, Inno Setup 6.

[CmdletBinding()]
param(
    [string]$Version = "1.0.0",
    [string]$Python = "python",
    [switch]$SkipFfmpeg,
    [switch]$SkipInstaller,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path (Join-Path $here "..\..")).Path
$dist = Join-Path $root "dist"
$app = Join-Path $dist "Dikte"

function Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

Push-Location $root
try {
    # --- the tests ---------------------------------------------------------
    # Before anything is packaged, not after: an installer built from a tree
    # that does not pass its own tests is an installer somebody will run.
    if (-not $SkipTests) {
        Step "tests"
        & $Python -m unittest discover -s tests -t .
        if ($LASTEXITCODE -ne 0) { throw "the tests did not pass" }
    }

    # --- the icon ----------------------------------------------------------
    Step "icon"
    & $Python (Join-Path $here "make_icon.py")
    if ($LASTEXITCODE -ne 0) { throw "could not draw the icon" }

    # --- ffmpeg ------------------------------------------------------------
    # Fetched and checked against the digest pinned in ffmpeg.json, then
    # dropped in beside the executables so that the loader and shutil.which
    # both find it without anything being installed.
    $vendor = Join-Path $here "vendor"
    if (-not $SkipFfmpeg) {
        Step "ffmpeg"
        & $Python (Join-Path $here "fetch_ffmpeg.py") $vendor
        if ($LASTEXITCODE -ne 0) { throw "could not fetch ffmpeg" }
    }

    # --- the executables ---------------------------------------------------
    Step "pyinstaller"
    if (Test-Path $app) { Remove-Item -Recurse -Force $app }
    & $Python -m PyInstaller --noconfirm --clean `
        --distpath $dist --workpath (Join-Path $root "build") `
        (Join-Path $here "dikte.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

    if (-not $SkipFfmpeg -and (Test-Path $vendor)) {
        Copy-Item (Join-Path $vendor "*.exe") $app -Force
    }

    # A build nobody has started is a build that does not start: this is the
    # smoke test, and it is the one thing that catches a missing hidden import.
    Step "smoke test"
    $cli = Join-Path $app "dikte.exe"
    $out = & $cli --help 2>&1
    if ($LASTEXITCODE -ne 0) { throw "the packaged command line would not run: $out" }
    # doctor imports the sound library, the clipboard and the hotkey adapter,
    # which is where a missing hidden import shows up. Exit 1 is a machine
    # with no API key, not a broken build.
    $out = & $cli doctor --json 2>&1
    if ($LASTEXITCODE -gt 1) { throw "the packaged doctor would not run: $out" }
    Write-Host $out

    # --- the installer -----------------------------------------------------
    if (-not $SkipInstaller) {
        Step "installer"
        $iscc = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $iscc) {
            throw "Inno Setup 6 is not installed. Get it from https://jrsoftware.org/isdl.php, or pass -SkipInstaller."
        }
        & $iscc "/DVersion=$Version" (Join-Path $here "dikte.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    }

    Step "done"
    Get-ChildItem $dist -Filter "*.exe" | ForEach-Object {
        Write-Host ("  {0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB))
    }
    Write-Host "  $app"
}
finally {
    Pop-Location
}
