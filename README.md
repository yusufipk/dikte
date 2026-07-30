# Dikte

Press `Ctrl+Space`, talk, press again. The recording is transcribed either
locally with whisper.cpp (no API key) or through OpenAI/OpenRouter, then lands
in your clipboard and is pasted into whatever window you were typing in.

Runs on KDE Plasma 6 / Wayland and macOS 13 or newer. The macOS port uses
AVFoundation for audio, native Carbon global hotkeys, and the system clipboard.

*[Türkçe README](README.tr.md)*

<p align="center">
  <img src="docs/settings-general.webp" width="820" alt="Dikte settings, General tab">
</p>

|  |  |
|---|---|
| <img src="docs/settings-api.webp" width="410" alt="API and models"> | <img src="docs/settings-cleanup.webp" width="410" alt="Cleanup rules"> |
| <img src="docs/settings-audio-file.webp" width="410" alt="Audio file"> | <img src="docs/settings-history.webp" width="410" alt="History"> |

## Install on Linux

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
systemctl --user enable --now ydotool     # needed for auto-paste

./install.sh                 # or:  ./install.sh "Ctrl+Alt+Space"
dikte                        # the settings window opens on first run
```

`install.sh` adds the `dikte` command, a menu entry, an autostart entry and the
KDE shortcut.

## Install on macOS

The macOS build supports Apple Silicon and Intel Macs. Install the one runtime
dependency, build the app, then move it to Applications:

```sh
brew install python ffmpeg
chmod +x build-macos.sh
./build-macos.sh
open dist
```

Unzip `Dikte-macOS.zip`, then drag `Dikte.app` into `/Applications`. The first recording asks for Microphone
permission. The first automatic paste asks for Accessibility/Automation
permission; if it is not enabled automatically, add Dikte under **System
Settings → Privacy & Security → Accessibility**.

Dikte runs as the **🎙️** icon in the macOS menu bar. Choose another one from
**Settings → General → Menu bar emoji**, or type any emoji you like; it changes
as soon as the settings are saved. Click the icon to open its menu;
**Settings…** is the first item and a direct icon activation opens the Settings
window while Dikte is idle.

The app is locally/ad-hoc signed. A downloaded build may need **Control-click →
Open** on its first launch unless a release has been notarized by an Apple
Developer account. Settings and history live in
`~/Library/Application Support/Dikte`.

GitHub Actions also builds `Dikte-macOS.zip` on every pull request and push.

### Use it without an API key

Under **Settings → API and models → Speech to text**, select **Local Whisper —
no API key**, then click **Install local Whisper**. On macOS the button installs
Homebrew's `whisper-cpp` when needed and downloads the recommended multilingual
`large-v3-turbo-q5_0` model (574 MB) once. Audio and transcripts remain on the
machine. Under **Transcript cleanup**, choose **Codex CLI — no API key** to run
the Cleanup Rules through the signed-in Codex session, or turn cleanup off for
fully local raw transcription.

For spoken questions and actions, Settings → Agent already supports the signed-in
`codex exec` or `claude -p` CLI session. This needs no separately pasted API key:
Local Whisper first turns speech into text, Dikte sends that text to the selected
CLI, and pastes its answer. Codex CLI does not accept audio input directly.

The hosted alternative takes **OpenAI** and/or **OpenRouter** keys. Speech to text
runs on either one; cleanup can run on OpenRouter or Codex CLI, so a single
OpenRouter key can cover both hosted steps. Keys fall back to `OPENAI_API_KEY`
and `OPENROUTER_API_KEY`, and are
stored in `~/.config/dikte/config.json` on Linux or
`~/Library/Application Support/Dikte/config.json` on macOS, mode 600.

## Using it

| What | How |
| --- | --- |
| Start / stop recording | `Ctrl+Space`, or click the tray icon |
| Cancel a recording | Tray menu → *Cancel recording*, or `dikte cancel` |
| Speak a command to an agent | Tray menu → *Ask Claude*, or `dikte ask` |
| Start / end a meeting | Tray menu → *Record a meeting*, or `dikte meeting` |
| Settings | Tray menu → *Settings*, or `dikte settings` |
| Reload after an update | Tray menu → *Restart*, or `dikte restart` |
| Quit | Tray menu → *Quit*, or `dikte quit` |

An indicator in the screen corner shows a red dot, a live waveform and the
elapsed time, then the stage it is on. It never takes focus. Pressing
`Ctrl+Space` again while Dikte is still working does nothing; nothing queues up.
A dictation and a command to the agent do wait on each other for the microphone,
which is one device, but for nothing else: each has its own indicator, and the
second one stacks above the first while both are up.

## What it does

- **Silence never reaches the API.** Handed near-silence, a transcription model
  invents a sentence instead of returning nothing ("Thanks for watching", or in
  Turkish "Altyazı M.K."). A recording is dropped when nothing rose 10 dB above
  *that recording's own* noise floor for at least 0.3 s, which is also what
  removes steady fan noise however loud, or when its loud end sits below
  -55 dBFS. The indicator reports the level it measured, which is what you
  calibrate the threshold against.
- **Misheard words are repaired.** Speech models fail phonetically on proper
  nouns, so the cleanup model is asked to fix those from context, and to leave
  the word alone when the context does not make the intended one clear. The names
  you list under Cleanup rules go to the transcription model as a hint and to the
  cleanup model as a glossary, which is what lets it recognise "kuber netis":

  ```
  raw    ıı bugün şey kuber netis üzerinde çalışan servisleri güncelledim
         yani sonra grafanada bir panel açtım hani ve pay kut ile arayüzü
         şey bitirdim işte

  result Bugün Kubernetes üzerinde çalışan servisleri güncelledim. Sonra
         Grafana'da bir panel açtım ve PyQt ile arayüzü bitirdim.
  ```
- **A failed cleanup is never silent.** The raw transcript is still pasted so the
  dictation is not lost, but the indicator turns amber with the reason instead of
  looking like a normal run.
- **A dictation can be a command instead.** Its own shortcut sends the
  transcript to Claude Code (`claude -p`) rather than pasting it, and pastes back
  what comes of it: the answer, or a sentence saying what was done. It is the
  session you would have opened yourself, so your skills and connected services
  are there, which is what makes "put that in my calendar on Thursday at three"
  a thing you can say to a window that is not Claude. Codex (`codex exec`) runs
  the same way, and OpenRouter is there as a plain question-and-answer fallback
  for a machine with neither CLI on it. Provider, model, permissions and working
  directory are under Settings → Agent, and commands close together stay in one
  conversation.
- **Meetings** are recorded from the microphone and the speaker output at the
  same time, which settles who said what by the channel a voice arrived on
  instead of guessing at it. The two sides are transcribed separately and
  interleaved into one timestamped transcript, and a second model, configured
  under Settings → Meeting along with its own instruction, turns that into
  minutes: decisions, action items, open questions. They land in
  `~/.local/share/dikte/meetings` and in Settings → Minutes. A run that fails
  keeps its recording, and a retry resumes from the transcript it already paid
  for. On macOS, system output must be exposed as an input device with
  [BlackHole](https://github.com/ExistentialAudio/BlackHole), Loopback, or
  Soundflower, then selected under Settings → Meeting. Normal dictation does
  not need a virtual audio device.
- **Audio and video files** run through the same models under Settings → Audio
  file, optionally with `[mm:ss]` timestamps, chunked through ffmpeg when long,
  and saved as `.txt` or as `.srt` subtitles; their cleanup follows its own rules,
  written for subtitles, so the lines keep their place and nothing is shortened.
- **History** of every dictation under Settings → History, with a size limit and
  right-click to delete.
- **Turkish and English interface**, following the system locale by default.

## Global shortcuts

On macOS, the built-in listener uses the native Carbon global-hotkey API and
works as soon as settings are saved. Automatic paste is separate and needs the
Accessibility permission described above.

On Linux, KWin only reads `kglobalshortcutsrc` at startup, so the shortcut `install.sh`
writes will not fire until you log out and back in. Until then, Settings →
Shortcut → **built-in listener** reads `/dev/input` and catches the combination
itself. The difference: it does not swallow the key, so `Ctrl+Space` also reaches
the focused application (some editors will pop up autocomplete). The listener
needs your user in the `input` group: `sudo usermod -aG input $USER`.

## Layout

```
dikte.py          entry point, tray icon, state machine, IPC
audio.py          PCM capture: PipeWire on Linux, AVFoundation on macOS
meeting.py        channel split, speaker labelling, cleanup, minutes
assistant.py      running a dictation through Claude Code, Codex or OpenRouter
api.py            hosted or Local Whisper transcription, OpenRouter cleanup
local_whisper.py  whisper.cpp install, verified model download and transcription
worker.py         transcribe → clean up → clipboard → paste
vad.py            deciding whether a recording holds speech at all
filetranscribe.py file transcription: ffmpeg, chunking, timestamps
overlay.py        the corner indicator
settings_ui.py    settings window
hotkey.py         KDE/evdev and native macOS global shortcuts
paste.py          Wayland and macOS clipboard/paste wrappers
i18n.py           the string table
```

On Linux the indicator is drawn through XWayland, because a Wayland client
cannot place a window in a screen corner; `dikte.py` sets
`QT_QPA_PLATFORM=xcb` for that. macOS uses its native floating tool window.

## License

GPL-3.0, see [LICENSE](LICENSE).
