# Dikte on Windows

Press `Ctrl+Space`, talk, press again: what you said is transcribed, cleaned
up and pasted where your cursor is.

## Requirements

Windows 10 or 11. The setup on the [releases page](../../releases) carries
everything else with it, and is x64, which an ARM machine runs emulated the way
it runs whisper.cpp. A checkout wants:

- **Python 3.11+** with **PyQt6** (`pip install PyQt6`; install.ps1 installs
  it when it is missing)
- **ffmpeg** for microphone capture: `winget install Gyan.FFmpeg`

## Installing

`Dikte-<version>-x64-setup.exe` from the releases page installs for your
account alone, so no administrator is asked for, and puts down a Start Menu
entry, a `dikte` command and, unless you untick it, a start at sign-in. It is
signed with no certificate, so SmartScreen offers only **Don't run** until you
press **More info**. Add/Remove Programs uninstalls it, and `dikte integrate`
and `dikte integrate --remove` are the sign-in entry on its own, for changing
your mind about that later. The `dikte` command is the same file install.ps1
writes, so over a checkout the setup takes it over and uninstalling takes it
away; run install.ps1 again to get the checkout's back.

From a checkout instead:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

This adds a **Dikte** entry to the Start Menu and a **`dikte`** command to the
terminal. Add `-Autostart` to also start it at sign-in; `-Uninstall` removes
all of it and leaves the repository and your settings alone.

To try it without installing anything:

```sh
python -m dikte
```

## First run

1. The tray icon appears and the Settings window opens.
2. Under **API and models**, download a local whisper model (the whisper.cpp
   Windows build is fetched automatically) or enter an OpenAI, Groq or
   OpenRouter key.
3. The shortcut defaults to `Ctrl+Space` and is changed under Shortcuts.
   While Dikte runs, Windows' own hotkey service (RegisterHotKey) listens for
   it: nothing to install and no permission to grant.

## What is different from Linux and macOS

- **Meeting recording (microphone + speakers) is not supported yet.** Windows
  does not offer what the speakers are playing as a capture device, so there
  is nothing to record the far side from. Everything else works, including
  transcribing audio and video files.
- **The shortcut is swallowed**: while Dikte holds `Ctrl+Space`, the focused
  application does not see it. This is how macOS behaves too, and unlike the
  Linux listener, which shares the key.
- No external tools for the clipboard or the key press: both go straight
  through the Windows API (the clipboard, SendInput).
- Settings live under `%APPDATA%\Dikte`, models and recordings under
  `%LOCALAPPDATA%\Dikte`.
- If the Windows user folder contains characters that whisper.cpp cannot open,
  choose an ASCII-only **Model folder** under Settings → API and models, such as
  `C:\DikteData`. The setting is shared by the local whisper and llama models.

## Performance

- The local install fetches whisper.cpp's **OpenBLAS build**, which
  transcribes about twice as fast as the stock one on a plain CPU. There is
  no GPU build to fetch for machines without an NVIDIA card, and none for
  Windows on ARM either: whisper.cpp publishes x64 only, so a Snapdragon
  machine runs it under emulation and the cloud is the faster option there.
- Setting Settings → API and models → **Threads** near your physical core
  count helps noticeably; the server's own default is 4.
- If speed matters more than accuracy, `ggml-small` and `ggml-base` are much
  faster; `ggml-large-v3-turbo-q5_0` transcribes best.

## Troubleshooting

- **Recording does not start:** does `dikte doctor` find ffmpeg, and does
  `dikte devices` list your microphone? `devices` also takes a fresh listing,
  which is what to run after plugging one in.
- **Nothing is pasted:** a normal-privilege process cannot type into an
  elevated (administrator) window; run Dikte elevated too, or paste by hand.
  The text lands on the clipboard either way.
- **The shortcut does nothing:** another application already holds the
  combination. Dikte says so in a tray notification when it asks for the key;
  pick a different one under Settings → Shortcuts.
