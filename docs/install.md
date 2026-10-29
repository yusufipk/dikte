# Installing Dikte

The [releases page](https://github.com/yusufipk/dikte/releases) has an AppImage, a disk image per Mac
architecture and a Windows setup. The first two write their own menu entry,
login item and `dikte` command the first time they run, and stand aside for an
installation already on the machine; `dikte integrate --remove` takes them
back. The AppImage still wants the system packages below, for the sound
server, the clipboard and the keyboard. The disk image is signed with no Apple
certificate, so the first launch is refused until you press **Open Anyway**
under System Settings → Privacy & Security, and macOS asks for the microphone
and Accessibility again after each update; installing from a checkout is what
avoids that. The Windows setup installs for your account alone and carries an
ffmpeg with it.

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
systemctl --user enable --now ydotool     # needed for auto-paste

./install.sh                 # or:  ./install.sh "Meta+Space" "Meta+Shift+Space"
dikte                        # the settings window opens on first run
```

On Fedora the packages are named differently, `ffmpeg-free` out of Fedora's own
repositories is enough because Dikte only ever takes the audio track of a video
file, and `ydotool` takes one step more: it ships as a system service, whose
socket stays root-owned and out of your session's reach, so auto-paste fails
with the daemon running. Point it at the path the client already looks at and
hand the socket over:

```sh
sudo dnf install pipewire-utils wl-clipboard ydotool ffmpeg-free python3-pyqt6
sudo mkdir -p /etc/systemd/system/ydotool.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/bin/ydotoold --socket-path=%s/.ydotool_socket --socket-own=%s:%s\n' \
  "$XDG_RUNTIME_DIR" "$(id -u)" "$(id -g)" \
  | sudo tee /etc/systemd/system/ydotool.service.d/override.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now ydotool
```

On Ubuntu/GNOME X11, recording uses PulseAudio and clipboard/paste use the X11
tools instead:

```sh
sudo apt install pulseaudio-utils xclip xdotool ffmpeg
```

On macOS the same `./install.sh` runs and hands over to `scripts/install-mac.sh`, which
puts down a `Dikte.app` in `~/Applications`, the `dikte` command and a
LaunchAgent:

```sh
brew install pyqt ffmpeg   # pyqt brings a Python newer than Apple's 3.9 with it

./install.sh               # or:  ./install.sh "Ctrl+Option+Space" "Ctrl+Option+D"
open -a Dikte
```

The bundle is what macOS files the **Microphone** and **Accessibility**
permissions against, and it asks for each the first time it needs one. The
default there is `Ctrl+Option+Space`, since macOS keeps `Ctrl+Space` for the
input-source switch, and nothing needs a logout: Dikte holds the combination
itself while it runs. PyQt6 comes from brew rather than pip because Homebrew's
Python refuses to be installed into, and `DIKTE_PYTHON=…/venv/bin/python
./install.sh` points the installer at a virtualenv instead.

Local speech to text needs whisper.cpp's server, which `brew install
whisper.cpp` installs, and Dikte finds it even when started from the Finder.
A meeting needs BlackHole or Loopback (`brew install blackhole-2ch`); dictation
does not.

Windows works the same way, holding the keys through the system's own hotkey
service while Dikte runs. The setup on the releases page carries the ffmpeg
recording needs and asks for no administrator; from a checkout it is `winget
install Gyan.FFmpeg`, `pip install PyQt6`, then `python -m dikte`, with an
optional `install.ps1` for the Start Menu entry and the `dikte` command.
Meetings are not supported there yet; the details are in the
[Windows README](../README.windows.md).

`install.sh` adds the `dikte` command, a menu entry, an autostart entry and the
two global shortcuts, whose keys are its two arguments, or the ones already in
your settings when it is given none. `./scripts/update.sh` pulls and puts all of
that back; `./scripts/uninstall.sh` takes it away again and leaves your settings
and dictations alone unless you pass `--purge`. Dikte looks at the releases page
once a day and puts a line in the tray menu when a newer version is out, which
opens the page rather than installing anything; the General tab turns that off
or runs it on the spot.

## Providers and keys

Choose speech-to-text and cleanup providers separately in Settings. Speech to
text runs locally by default; cleanup uses a hosted provider by default, but
can also run on a local model or through an installed CLI. Turn cleanup off to
paste the raw transcript.

| Task | Hosted providers | Other options |
| --- | --- | --- |
| Speech to text | OpenAI, Groq, OpenRouter, Requesty, Google AI Studio, any OpenAI-compatible server | Local model |
| Cleanup | OpenRouter, Requesty, Google AI Studio, DeepSeek, OpenCode Go, any OpenAI-compatible server | Local model; Claude Code, Codex or Antigravity CLI |

Choose models and cleanup reasoning effort in Settings. API keys can be entered
there too; empty keys fall back to `OPENAI_API_KEY`, `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `REQUESTY_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`
and `OPENCODE_API_KEY`.

Settings and any keys entered in the app are saved in `config.json`: under
`~/.config/dikte` on Linux (or `$XDG_CONFIG_HOME/dikte` when set),
`~/Library/Application Support/Dikte` on macOS, and `%APPDATA%\Dikte` on
Windows. On Linux and macOS, the file is written with mode 600.

## The global shortcuts, and the logout KDE needs

KWin only reads `kglobalshortcutsrc` at startup, so the shortcuts `install.sh`
writes will not fire until you log out and back in. Until then, Settings →
Shortcuts → **built-in listener** reads `/dev/input` and catches the combination
itself. The difference: it does not swallow the key, so `Ctrl+Space` also reaches
the focused application (some editors will pop up autocomplete). The listener
needs your user in the `input` group: `sudo usermod -aG input $USER`. On GNOME
the shortcut works the moment it is installed, and on a desktop that keeps no
registry at all (i3, XFCE, sway and most others) the listener is the whole
mechanism: nothing is installed, nothing waits for a logout, and Settings →
Shortcuts shows the command to bind if you would rather your desktop owned the
keys.
