# Dikte

Press `Ctrl+Space`, talk, press again. The recording is transcribed by whisper.cpp
on your own machine, a model on OpenRouter cleans it up (dropping the *uh*s, the
restarts, the missing punctuation), and the result lands in your clipboard and
is pasted into whatever window you were typing in. OpenAI and OpenRouter are
there as alternatives for the transcription too.

Built for KDE Plasma 6 on Wayland. No dependencies beyond system packages:
just the Python standard library and PyQt6.

*[Türkçe README](README.tr.md)*

<p align="center">
  <img src="docs/settings-general.webp" width="820" alt="Dikte settings, General tab">
</p>

|  |  |
|---|---|
| <img src="docs/settings-api.webp" width="410" alt="API and models"> | <img src="docs/settings-cleanup.webp" width="410" alt="Cleanup rules"> |
| <img src="docs/settings-audio-file.webp" width="410" alt="Audio file"> | <img src="docs/settings-history.webp" width="410" alt="History"> |

## Install

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
sudo pacman -S --needed whisper-cpp      # local speech to text
sudo pacman -S --needed cuda             # its GPU backend, on an NVIDIA card
systemctl --user enable --now ydotool    # needed for auto-paste

./install.sh                 # or:  ./install.sh "Ctrl+Alt+Space"
dikte                        # the settings window opens on first run
```

`install.sh` adds the `dikte` command, a menu entry, an autostart entry and the
KDE shortcut.

Speech to text runs locally by default, on whisper.cpp. Pick a model under
Settings → API and models and press **Download**: `large-v3-turbo` (1.5 GB) is
the default, and the list runs from `tiny` up to `large-v3`. Models land in
`~/.local/share/dikte/models`. Nothing of the audio leaves the machine, and it
costs nothing per dictation.

Cleanup runs on **DeepSeek** (`deepseek-v4-flash`) or **OpenRouter**
(`google/gemini-3.5-flash-lite`), whichever you pick; the same choice also writes
the meeting minutes. It can be switched off, in which case the raw transcript is
pasted. Transcription can also be moved to **OpenAI** or **OpenRouter** under the
same tab, on a machine that would rather not run a model itself. The keys fall
back to `OPENAI_API_KEY`, `OPENROUTER_API_KEY` and `DEEPSEEK_API_KEY`, and are
stored in `~/.config/dikte/config.json`, mode 600.

One thing worth knowing about DeepSeek: it thinks unless it is told not to, and
cleanup is not a job worth thinking about. Measured on the example below,
thinking took six times as long for the same sentence, spent 95% of its output
tokens on the reasoning, and sometimes came back with nothing to paste at all.
So Dikte ships DeepSeek's cleanup with **Thinking: Off** and leaves the minutes,
which are worth thinking about, to think.

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

- **Transcription runs on this machine.** whisper.cpp is kept alive as a server
  next to Dikte, on the `/v1/audio/transcriptions` path the hosted providers
  use, which is what makes it one more base URL rather than a second code path:
  dictation, file transcription, subtitles and meetings all go through it
  unchanged. The model is loaded while Dikte starts rather than on the first
  dictation, so a few seconds of speech comes back in about as long as it took
  to say — the loading is the slow part, not the transcribing. Turn that off
  under Settings → Local whisper to keep the graphics memory free.
- **Silence never reaches the model.** Handed near-silence, a transcription model
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
  for.
- **Audio and video files** run through the same models under Settings → Audio
  file, optionally with `[mm:ss]` timestamps, chunked through ffmpeg when long,
  and saved as `.txt` or as `.srt` subtitles; their cleanup follows its own rules,
  written for subtitles, so the lines keep their place and nothing is shortened.
- **History** of every dictation under Settings → History, with a size limit and
  right-click to delete.
- **Turkish and English interface**, following the system locale by default.

## The global shortcut needs one logout

KWin only reads `kglobalshortcutsrc` at startup, so the shortcut `install.sh`
writes will not fire until you log out and back in. Until then, Settings →
Shortcut → **built-in listener** reads `/dev/input` and catches the combination
itself. The difference: it does not swallow the key, so `Ctrl+Space` also reaches
the focused application (some editors will pop up autocomplete). The listener
needs your user in the `input` group: `sudo usermod -aG input $USER`.

## Layout

```
dikte.py          entry point, tray icon, state machine, IPC
audio.py          PCM capture: pw-record for dictation, ffmpeg for a meeting
meeting.py        channel split, speaker labelling, cleanup, minutes
assistant.py      running a dictation through Claude Code, Codex or OpenRouter
api.py            transcription on any provider, OpenRouter cleanup (stdlib only)
whispercpp.py     the local whisper.cpp server and its model downloads
worker.py         transcribe → clean up → clipboard → paste
vad.py            deciding whether a recording holds speech at all
filetranscribe.py file transcription: ffmpeg, chunking, timestamps
overlay.py        the corner indicator
settings_ui.py    settings window
hotkey.py         KDE shortcut installation and the evdev listener
paste.py          wl-clipboard and ydotool wrappers
i18n.py           the string table
```

The indicator is drawn through XWayland, because a Wayland client cannot place a
window in a screen corner; `dikte.py` sets `QT_QPA_PLATFORM=xcb` for that.

## License

GPL-3.0, see [LICENSE](LICENSE).
