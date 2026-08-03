# PyInstaller spec: one folder holding both faces of Dikte.
#
#     DikteApp.exe   the tray application, with no console behind it
#     dikte.exe      the command line, which needs one
#
# One folder rather than one file, because a one-file build unpacks itself into
# a temporary directory on every start: Qt, the sound library and a hundred
# small files, before the tray icon appears. Dikte is started once per login
# and asked for a key press a second later, so the start is what matters.
#
# The two executables share everything else. PyInstaller collects the libraries
# once and both EXEs sit in the same directory beside them, which is also what
# lets `dikte restart` hand over to DikteApp.exe without knowing where it is.
#
#     pyinstaller --noconfirm packaging/windows/dikte.spec

import pathlib
import sys

ROOT = pathlib.Path(SPECPATH).resolve().parents[1]
ICON = str(pathlib.Path(SPECPATH) / "dikte.ico")


def conda_dlls():
    """The libraries a conda interpreter keeps somewhere PyInstaller misses.

    A conda environment puts its C libraries in Library\\bin rather than beside
    the interpreter, and PyInstaller's dependency walk does not go there. The
    one that matters is libffi: without it `import ctypes` fails inside the
    packaged build, which is every Win32 call Dikte makes, and the failure
    arrives as an unhandled exception before the tray icon appears.
    """
    library = pathlib.Path(sys.base_prefix) / "Library" / "bin"
    if not library.is_dir():
        return []
    wanted = ("ffi-8.dll", "ffi-7.dll", "ffi.dll", "libcrypto-3-x64.dll",
              "libssl-3-x64.dll")
    return [(str(library / name), ".") for name in wanted
            if (library / name).is_file()]

# Everything Dikte imports by name at run time rather than at the top of a
# file. The platform adapters are chosen through importlib, so nothing that
# reads the source can see that they are needed.
HIDDEN = [
    "platforms.windows.audio",
    "platforms.windows.clipboard",
    "platforms.windows.hotkeys",
    "platforms.windows.runtime",
    "platforms.windows.resample",
    "platforms.common.pcm",
    "platforms.common.shortcuts",
    "pyaudiowpatch",
]

# The Qt pieces Dikte never touches. Left in, a build carries WebEngine, 3D and
# the SQL drivers — several hundred megabytes of installer for an application
# that draws a rounded rectangle and a waveform.
EXCLUDED = [
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebChannel",
    "PyQt6.Qt3DCore", "PyQt6.Qt3DRender", "PyQt6.QtQuick", "PyQt6.QtQml",
    "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtPdf",
    "PyQt6.QtMultimedia", "PyQt6.QtCharts", "PyQt6.QtDataVisualization",
    "tkinter", "unittest", "pydoc_data", "test",
]

analysis = Analysis(
    [str(ROOT / "dikte.py")],
    pathex=[str(ROOT)],
    binaries=conda_dlls(),
    datas=[(ICON, ".")],
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDED,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

# The tray application. console=False is what keeps a black window from opening
# behind it at every login.
tray = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DikteApp",
    icon=ICON,
    console=False,
    debug=False,
    strip=False,
    upx=False,
    # Dikte is installed for one user and asks for nothing it does not have;
    # an application that requested elevation could not send a key press to
    # anything running without it either.
    uac_admin=False,
)

# The same program with a console, so that `dikte transcribe talk.mp4` can
# print what it did into the terminal it was typed in.
console = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="dikte",
    icon=ICON,
    console=True,
    debug=False,
    strip=False,
    upx=False,
)

COLLECT(
    tray,
    console,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Dikte",
)
