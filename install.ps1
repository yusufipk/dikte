# Installs Dikte for this Windows user: a Start Menu entry, an optional
# autostart entry, and a `dikte` command that works from any terminal.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1              # install
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Autostart   # + start at sign-in
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall   # remove
param(
    [switch]$Autostart,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
# The one file that starts the application, whoever is asking: the Start Menu
# entry, the autostart entry and the dikte command all name it.
$entry = Join-Path $repo "dikte\__main__.py"
$startMenu = [Environment]::GetFolderPath("Programs")
$startup = [Environment]::GetFolderPath("Startup")
$shortcut = Join-Path $startMenu "Dikte.lnk"
$autostartLink = Join-Path $startup "Dikte.lnk"
# WindowsApps is already on the user PATH, so a dikte.cmd left there runs from
# any terminal without a PATH edit and without an administrator.
$cmdShim = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\dikte.cmd"

if ($Uninstall) {
    foreach ($path in @($shortcut, $autostartLink)) {
        if (Test-Path $path) { Remove-Item $path -Force; Write-Host "removed: $path" }
    }
    # The packaged install writes the same shim, naming its own dikte-cli.exe.
    # Only the one naming this checkout is ours to delete; taking the other
    # would break the `dikte` command of an install this script never made.
    if (Test-Path $cmdShim) {
        if ((Get-Content $cmdShim -Raw).Contains($entry)) {
            Remove-Item $cmdShim -Force
            Write-Host "removed: $cmdShim"
        } else {
            Write-Host "left alone: $cmdShim (it names another install, not this checkout)"
        }
    }
    Write-Host "Dikte's shortcuts are gone. The repository and your settings are not."
    exit 0
}

# --- what it needs ----------------------------------------------------------
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "No python found. Install it with: winget install Python.Python.3.12"
}
$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]"3.11") {
    Write-Error "Python 3.11 or newer is needed, and this one is $version."
}
& python -c "import PyQt6.QtWidgets" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyQt6..."
    & python -m pip install PyQt6
    if ($LASTEXITCODE -ne 0) { Write-Error "PyQt6 would not install." }
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "No ffmpeg found. Recording needs it: winget install Gyan.FFmpeg"
}

# pythonw.exe runs the same program without a console window behind it.
$pythonw = Join-Path (Split-Path $python.Source) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python.Source }

# --- the Start Menu entry ---------------------------------------------------
$shell = New-Object -ComObject WScript.Shell
foreach ($path in @($shortcut) + $(if ($Autostart) { @($autostartLink) } else { @() })) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = "`"$entry`" --gui"
    $link.WorkingDirectory = $repo
    $link.Description = "Dikte: dictation"
    $link.Save()
    Write-Host "shortcut: $path"
}

if ($Autostart) {
    # The packaged build keeps its sign-in entry in the registry. Left there
    # beside the shortcut written above, both would start a Dikte at sign-in,
    # and this install is the one being asked for.
    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
        -Name "Dikte" -ErrorAction SilentlyContinue
    Write-Host "autostart: the Startup shortcut replaces any registry Run entry a packaged install left"
}

# --- the dikte command ------------------------------------------------------
# The interpreter by its full path rather than by name: the one checked above is
# the one the command line should run, whatever a later PATH change puts first.
$shimDir = Split-Path $cmdShim
if (Test-Path $shimDir) {
    "@echo off`r`n`"$($python.Source)`" `"$entry`" %*" |
        Out-File $cmdShim -Encoding ascii
    Write-Host "command: dikte  ($cmdShim)"
} else {
    Write-Warning "No $shimDir on this machine, so there is no dikte command. Run it as: python `"$entry`""
}

Write-Host ""
Write-Host "Installed. Start it from the Start Menu as 'Dikte', or type 'dikte' in a terminal."
Write-Host "The Settings window opens on the first run: download a model there and pick the shortcut (Ctrl+Space by default)."
