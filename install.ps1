<#
.SYNOPSIS
Dikte installer for Windows: dependency check, pip install, Start Menu and
startup shortcuts. The Windows counterpart of install.sh.

.DESCRIPTION
Run it from a checkout of the repository:

    powershell -ExecutionPolicy Bypass -File install.ps1

or right-click the file and choose "Run with PowerShell".
#>
$ErrorActionPreference = "Stop"

if (-not $PSScriptRoot) {
    Write-Host "! Run this from a checkout of the repository, not from a pipe." -ForegroundColor Yellow
    exit 1
}

$Dir = $PSScriptRoot
$Python = Get-Command "python" -ErrorAction SilentlyContinue
if (-not $Python) {
    Write-Host "! Python not found. Please install Python 3." -ForegroundColor Yellow
    exit 1
}
$PythonPath = $Python.Source
Write-Host "Using Python: $PythonPath"

# `python` on PATH is often a project's virtual environment rather than the
# machine's own. Dikte runs from a Start Menu shortcut long after that project
# is forgotten, so installing PyQt6 into it would be installing it nowhere.
if ((Test-Path (Join-Path (Split-Path $PythonPath) "activate")) -or
    (Test-Path (Join-Path (Split-Path (Split-Path $PythonPath)) "pyvenv.cfg"))) {
    Write-Host "! That Python is a virtual environment." -ForegroundColor Yellow
    Write-Host "  Dikte would stop working the moment it is deleted."
    $answer = Read-Host "  Install into it anyway? (y/N)"
    if ($answer -ne "y") {
        Write-Host "  Deactivate the environment, open a new terminal, and run this again."
        exit 1
    }
}

$Pythonw = Join-Path (Split-Path $PythonPath) "pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    $Pythonw = $PythonPath
    Write-Host "! pythonw.exe not found next to python.exe; a console window may appear." -ForegroundColor Yellow
}

$Ffmpeg = Get-Command "ffmpeg" -ErrorAction SilentlyContinue
if (-not $Ffmpeg) {
    Write-Host "! ffmpeg not found. Please install ffmpeg and add it to your PATH." -ForegroundColor Yellow
    Write-Host "  You can install it via winget: winget install Gyan.FFmpeg"
    Write-Host "  Without it Dikte cannot record anything at all."
}

Write-Host "Installing dependencies..."
& $PythonPath -m pip install -r "$Dir\requirements.txt"

& $PythonPath -c "import PyQt6" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "! PyQt6 still will not import. Dikte cannot start without it." -ForegroundColor Red
    exit 1
}

# Generate a silent VBScript launcher to guarantee no console window
$VbsPath = Join-Path $Dir "dikte-launcher.vbs"
$VbsContent = 'Set objShell = CreateObject("WScript.Shell")' + "`r`n"
$VbsContent += 'objShell.Run """{0}"" ""{1}\dikte.py""", 0, False' -f $Pythonw, $Dir
Set-Content -Path $VbsPath -Value $VbsContent -Encoding Ascii

$WshShell = New-Object -comObject WScript.Shell

# Add to Start Menu
$StartMenu = [System.Environment]::GetFolderPath('StartMenu')
$Programs = Join-Path $StartMenu "Programs"
$ShortcutPath = Join-Path $Programs "Dikte.lnk"
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "wscript.exe"
$Shortcut.Arguments = """$VbsPath"""
$Shortcut.WorkingDirectory = $Dir
$Shortcut.IconLocation = "$Dir\docs\icon.ico"
$Shortcut.Save()
Write-Host "v Start Menu shortcut created" -ForegroundColor Green

# Add to Startup
$Startup = [System.Environment]::GetFolderPath('Startup')
$StartupShortcutPath = Join-Path $Startup "Dikte.lnk"
$StartupShortcut = $WshShell.CreateShortcut($StartupShortcutPath)
$StartupShortcut.TargetPath = "wscript.exe"
$StartupShortcut.Arguments = """$VbsPath"""
$StartupShortcut.WorkingDirectory = $Dir
$StartupShortcut.IconLocation = "$Dir\docs\icon.ico"
$StartupShortcut.Save()
Write-Host "v Startup shortcut created (will start automatically on login)" -ForegroundColor Green

Write-Host ""
Write-Host "Done. You can now start Dikte from the Start Menu." -ForegroundColor Green
Write-Host "The settings window opens on first run; add your OpenAI and OpenRouter keys."
Write-Host "Then press Ctrl+Shift+Space to start dictation."
Write-Host "(Windows keeps Ctrl+Space for switching keyboard layout.)"
