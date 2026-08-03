# WhisperKey

System-wide push-to-talk dictation for Linux. Hold a key, talk, release — the
text appears in whatever window has focus. Browser, terminal, editor, chat app,
anywhere. This is the thing Wispr Flow does on macOS, for GNOME on X11.

Everything runs locally. Audio never leaves the machine; transcription happens
on your GPU.

```
   hold Left Command  ──▶  🎙 speak  ──▶  release  ──▶  text appears, ~0.5 s later
```

## How it feels

- **Hold to dictate.** Press and hold the key, talk, let go. Text lands where
  your cursor is.
- **Double-tap to latch.** Two quick taps and it keeps recording hands-free —
  for long-form dictation. Tap once more to finish.
- **Esc cancels.** Discards the recording without pasting anything.

A small pill appears near the bottom of the screen while it listens, with a live
level meter so you always know it is hearing you.

## Requirements

- **X11**, not Wayland. Log in with *"Ubuntu on Xorg"* if unsure — GNOME's
  session type is shown by `echo $XDG_SESSION_TYPE`.
- An NVIDIA GPU is strongly recommended (CPU works, but is slow).
- No root required. See [Why no sudo](#why-no-sudo).

## Install

```bash
./install.sh
systemctl --user start whisperkey
whisperkey doctor
```

Then hold **Left Command** and talk.

The installer creates a virtualenv, pulls the model (~1.6 GB, once), writes a
config file, and installs a systemd user service so it starts at login.

## Choosing your key

The default is **Left Command** (`cmd_l`). Keyboards vary a lot — an Apple Magic
Keyboard, for instance, has **no Right Ctrl key at all** — so if you want a
different one, ask:

```bash
whisperkey detect-key     # press any key; it prints the name to use
```

Then set it in `~/.config/whisperkey/config.toml` and run `whisperkey restart`.

> **Note on Left Command:** GNOME normally opens the Activities overview when
> `Super_L` is tapped alone, which would fire on every dictation. The installer
> moves that to `Super_R`:
> ```bash
> gsettings set org.gnome.mutter overlay-key 'Super_R'   # undo: gsettings reset ...
> ```

Good alternatives: `cmd_r` (Right Command), `alt_r`, `caps_lock`, `f9`.

## Everyday commands

| Command | What it does |
|---|---|
| `whisperkey doctor` | Check everything; run this first if something misbehaves |
| `whisperkey status` | Current state, device, dictation count |
| `whisperkey last` | Re-paste the previous transcript (when it went to the wrong window) |
| `whisperkey history` | Recent dictations |
| `whisperkey toggle` | Start/stop dictating — bind to a shortcut if you prefer |
| `whisperkey cleanup on\|off` | Toggle the LLM cleanup pass |
| `whisperkey transcribe FILE` | Transcribe an audio file (no microphone needed) |
| `whisperkey config --edit` | Open the config file |

Logs: `journalctl --user -u whisperkey -f`

## Accuracy: teach it your vocabulary

The single biggest real-world win. Whisper guesses badly at names and jargon
unless told about them. In `~/.config/whisperkey/config.toml`:

```toml
[vocab]
words = ["Anthropic", "Kubernetes", "PostgreSQL", "Wispr Flow"]

[vocab.replacements]
"clod code" = "Claude Code"
"post gres" = "PostgreSQL"
```

`words` is fed to Whisper as context so it recognises them in the first place;
`replacements` repairs whatever still comes out wrong, matched case-insensitively
on whole words.

## Optional: LLM cleanup

A second pass through a local model that fixes punctuation and strips filler
words ("um", "you know"), like Wispr Flow's polished mode.

```bash
ollama pull llama3.2:3b
whisperkey cleanup on
```

It adds roughly 400 ms and fails open — if Ollama is down or the model returns
something suspicious, you get the raw transcript rather than nothing. Small
models sometimes *answer* dictated text instead of tidying it, so a rewrite that
is much shorter or longer than the input is rejected automatically.

## Configuration highlights

`~/.config/whisperkey/config.toml`:

| Setting | Default | Notes |
|---|---|---|
| `hotkey.key` | `cmd_l` | Use `whisperkey detect-key` to find names |
| `hotkey.hold_min_ms` | `250` | Shorter presses count as taps, not dictation |
| `audio.preroll_ms` | `0` | Set to `300` if your first word gets clipped — see below |
| `model.name` | `large-v3-turbo` | `distil-large-v3` is faster, `large-v3` more accurate |
| `model.beam_size` | `1` | `5` is slightly more accurate and slower |
| `inject.method` | `paste` | `type` for apps where paste misbehaves |
| `inject.trailing_space` | `true` | Keeps consecutive dictations from running together |

### If your first word gets clipped

By default the microphone only opens when you press the key, so GNOME's
"microphone in use" indicator tells the truth. That costs ~50 ms of startup, and
if you start talking the instant you press, you can lose a syllable. Setting
`audio.preroll_ms = 300` keeps the mic open continuously and retains the last
300 ms, so speech that begins before the keypress is still captured — at the
cost of a permanently live microphone. Your call.

## Why no sudo

Everything installs into a virtualenv using pip wheels and tools already present
on a stock Ubuntu desktop:

| Need | How, without root |
|---|---|
| Audio capture | `pw-record` (PipeWire, preinstalled). `sounddevice` was avoided because it ships no Linux wheel and would need `libportaudio2` |
| Global hotkey | `pynput`, installed `--no-deps` — its declared `evdev` dependency is a C extension with no wheel, and is only needed for its Wayland backend |
| Synthetic paste | X11 XTEST via `pynput`, so no `xdotool` |
| Focused-window detection | `python-xlib` (pure Python), so no `xdotool` |
| Clipboard | GTK, via the preinstalled `python3-gi` |
| Status | libnotify (preinstalled) |

**The one exception** is the tray icon, which needs
`gir1.2-ayatanaappindicator3-0.1`. Without it WhisperKey reports status through
desktop notifications instead. To add it:

```bash
sudo apt install gir1.2-ayatanaappindicator3-0.1 && whisperkey restart
```

## Architecture

One long-lived daemon keeps the model resident in VRAM — loading it per
dictation would cost seconds every time.

```
key down ──▶ hotkey.py ──▶ daemon.py ──▶ recorder.py  (pw-record, 16 kHz mono)
                              │                │ RMS level
                              │                ▼
key up   ─────────────────▶   │           ui/overlay.py
                              ▼
                     transcriber.py   (faster-whisper, CUDA float16)
                              ▼
                     postprocess.py   (vocabulary → optional Ollama)
                              ▼
                     injector.py      (clipboard + XTEST paste)
                              ▼
                     history.py       (sqlite)
```

Three threads: the GLib main loop owns everything GTK (overlay, clipboard); the
pynput listener delivers key events; a worker thread transcribes so a slow
transcription never stalls the hotkey. Anything touching GTK is marshalled onto
the main loop with `GLib.idle_add`.

Measured on an RTX 3070 Laptop: 11 s of speech transcribes in **0.55 s**.

## Testing

```bash
.venv/bin/python -m unittest discover -s tests -t .   # 37 tests, no X/GPU/mic needed
.venv/bin/python tools/selftest_inject.py             # real clipboard + paste
.venv/bin/python tools/selftest_hotkey.py cmd_l       # synthetic key events
whisperkey transcribe tests/fixtures/jfk.flac         # full transcription path
```

`selftest_inject.py` waits until its own window genuinely holds focus before
sending any keystroke, so it can never paste into whatever you were doing.

## Troubleshooting

**Nothing happens when I press the key.** Run `whisperkey detect-key` and press
it — if nothing prints, your keyboard does not send that key. Apple keyboards
have no Right Ctrl.

**Text goes to the wrong window.** Focus moved between release and paste. Run
`whisperkey last` to re-paste it where you are now.

**It pastes into a terminal as garbage.** Terminals need `Ctrl+Shift+V`.
WhisperKey detects common ones by `WM_CLASS`; add yours to
`inject.terminal_classes`.

**"Running on CPU" warning.** cuDNN could not load. `whisperkey doctor --full`
shows why. The usual cause is `LD_LIBRARY_PATH` not reaching the pip-installed
NVIDIA libraries; the systemd unit sets it explicitly.

**Wayland.** Not supported. Global key monitoring and synthetic input both
require privileges X11 grants freely. The hotkey and injector sit behind small
interfaces so evdev/uinput backends could be added later.

## License

MIT
