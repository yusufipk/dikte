# How Dikte works

An indicator in the screen corner shows a red dot, a live waveform and the
elapsed time, then the stage it is on. It never takes focus. Pressing
`Ctrl+Space` again while the last dictation is still being cleaned up starts
the next one; it is transcribed and pasted in turn, behind the one still going.
A dictation and a command to the agent do wait on each other for the microphone,
which is one device, but for nothing else: each has its own indicator, and the
second one stacks above the first while both are up.

Everything the settings window holds has a verb of its own too, so a script or
an agent can work the whole thing: `dikte record --seconds 8` says back what was
said, `dikte transcribe talk.mp4 --srt` writes subtitles, and the settings, the
history and the meetings are there beside them. `dikte --help` lists them, they
all take `--json`, and only the ones needing the microphone need the application
running.

## What it does

- **It all runs on this machine by default.** Speech to text on whisper.cpp and
  cleanup on llama.cpp, neither installed beforehand: the settings window fetches
  the program and the model, verifies the sha256 and refuses a download published
  without one, then keeps a server alive while you dictate and hands the memory
  back once it has sat unused for ten minutes. The model list is
  grouped by model rather than by file size, and the row this machine's memory
  and graphics can take is marked. The graphics card is
  reached through CUDA, ROCm or Vulkan where the build allows. No key, no
  account, nothing leaving the machine. On x86_64 Linux, and on Windows with a
  graphics driver, the same button fetches a Vulkan build of whisper-server
  that Dikte publishes itself, because upstream publishes none for either.
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
  you list in the Dictionary tab go to the transcription model as a hint and to the
  cleanup model as a glossary, which is what lets it recognise "kuber netis"
  (the writing profiles, which set a tone per program or translate, share it):

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
  a thing you can say to a window that is not Claude. Codex (`codex exec`) and
  Antigravity (`agy -p`) run the same way, though Antigravity takes neither a
  permission mode nor a sandbox from Dikte: what it may do without asking is
  whatever its own allow-rules say. OpenRouter, OpenCode Go or Requesty is
  there as a
  plain question-and-answer fallback for a machine with no CLI on it. Provider,
  model, permissions and working directory are under Settings → Agent, along
  with an optional key that asks about the selected text, and commands close
  together stay in one conversation.
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
  file, optionally with `[mm:ss]` timestamps and, on a model known to diarize,
  speaker labels, chunked through ffmpeg when long,
  and saved as `.txt` or as `.srt` subtitles; their cleanup follows its own rules,
  written for subtitles, so the lines keep their place and nothing is shortened.
- **History** of every dictation under Settings → History, with a size limit and
  right-click to delete.
- **The speech language is detected, not picked.** Auto is the default: whisper
  on this machine says what it heard, the hosted providers transcribe in
  whatever language comes in without being told, and the detected language
  lands in the history and decides which cleanup prompt (Turkish or the
  language-agnostic one) a run gets. A fixed language still overrides it.
- **Turkish and English interface**, following the system locale by default.

## Layout

Everything below is in the `dikte` package, which is what `python3 -m dikte`
runs and what the `__main__.py` in it hands to every launcher and shortcut.
`scripts/` holds install-mac.sh, update.sh, uninstall.sh and release.sh;
`packaging/` builds the AppImage, the disk image and the Windows setup that
release.sh's tag publishes; install.sh stays at the top, and `tests/` has a
file per module.

```
app.py            entry point, tray icon, state machine
cli.py            the command line: every verb, and what it answers with
ipc.py            one request and one reply over the local socket
audio.py          PCM capture: pw-record for dictation, ffmpeg for a meeting
meeting.py        channel split, speaker labelling, cleanup, minutes
assistant.py      handing a dictation to Claude Code, Codex, agy or a chat model
api.py            transcription and cleanup requests (stdlib only)
cleanup.py        who rewrites the transcript: a hosted model, one here, a CLI
ggml.py           whisper.cpp and llama.cpp here: fetch, verify, keep serving
hub.py            what GitHub and Hugging Face have on offer today
update.py         whether a newer release is out, and the page it is on
worker.py         transcribe → clean up → clipboard → paste
vad.py            deciding whether a recording holds speech at all
filetranscribe.py file transcription: ffmpeg, chunking, timestamps
overlay.py        the corner indicator
settings_ui.py    settings window
hotkey.py         the desktop's shortcut registry, the evdev listener, Carbon on a Mac
paste.py          wl-clipboard and ydotool wrappers, pbcopy and CoreGraphics
trayicon.py       the tray icons, drawn where there is no icon theme
integrate.py      what a downloaded build writes into the desktop it landed on
i18n.py           the string table
```

The indicator is drawn through XWayland, because a Wayland client cannot place a
window in a screen corner; `app.py` sets `QT_QPA_PLATFORM=xcb` for that.
