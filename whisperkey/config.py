"""Configuration for WhisperKey.

Config lives at ~/.config/whisperkey/config.toml and is written with sensible
defaults on first run. Reading uses stdlib tomllib; writing is done by emitting
the DEFAULT_TOML template, so the file the user edits always carries comments.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "whisperkey"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "whisperkey"
HISTORY_DB = DATA_DIR / "history.db"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
SOCKET_PATH = RUNTIME_DIR / "whisperkey.sock"


@dataclass
class HotkeyConfig:
    #: pynput key name, e.g. "cmd_l", "cmd_r", "alt_r", "caps_lock", "f9".
    #: Modifier-only keys are intentionally not suppressed -- see hotkey.py.
    #: Run `whisperkey detect-key` to find the name for any physical key;
    #: keyboards vary (an Apple Magic Keyboard has no Right Ctrl at all).
    key: str = "cmd_l"
    #: A press shorter than this counts as a tap, not a dictation.
    hold_min_ms: int = 250
    #: Second tap must land within this window to latch recording on.
    double_tap_ms: int = 400
    #: Key that discards an in-progress recording.
    abort_key: str = "esc"
    enabled: bool = True


@dataclass
class AudioConfig:
    #: PipeWire target (node name or id). Empty means the default source.
    device: str = ""
    sample_rate: int = 16000
    #: Hard cap so a stuck key cannot fill memory. 300 s ~= 18 MB of float32.
    max_seconds: int = 300
    #: Recordings shorter than this are dropped as accidental key taps.
    min_seconds: float = 0.35
    #: Keep the mic stream permanently open and retain this much audio from
    #: *before* the key was pressed. 0 disables it, so the mic only opens on
    #: keydown and GNOME's mic indicator stays honest.
    preroll_ms: int = 0
    backend: str = "auto"  # auto | pw-record | arecord


@dataclass
class ModelConfig:
    name: str = "large-v3-turbo"
    device: str = "auto"  # auto | cuda | cpu
    compute_type: str = "auto"  # auto -> float16 on cuda, int8 on cpu
    beam_size: int = 1
    language: str = "en"  # empty string enables autodetect
    vad: bool = True
    #: Extra context sentence given to Whisper; vocabulary is appended to this.
    initial_prompt: str = ""


@dataclass
class InjectConfig:
    method: str = "paste"  # paste | type
    paste_combo: str = "ctrl+v"
    terminal_paste_combo: str = "ctrl+shift+v"
    #: WM_CLASS values that need shift+paste. Matched case-insensitively.
    terminal_classes: list[str] = field(
        default_factory=lambda: [
            "gnome-terminal-server",
            "org.gnome.terminal",
            "alacritty",
            "kitty",
            "org.wezfurlong.wezterm",
            "konsole",
            "xterm",
            "terminator",
            "tilix",
            "foot",
            "xfce4-terminal",
        ]
    )
    #: WM_CLASS -> "type" to force character-by-character entry for apps where
    #: synthetic paste is unreliable.
    per_app_method: dict[str, str] = field(default_factory=dict)
    #: Let the app finish processing the hotkey's keyup before pasting.
    settle_ms: int = 60
    #: How long to wait before putting the user's old clipboard back.
    restore_clipboard_ms: int = 500
    restore_clipboard: bool = True
    #: Delay between synthetic characters in "type" mode.
    type_delay_ms: int = 6
    #: Append a trailing space so consecutive dictations do not run together.
    trailing_space: bool = True


@dataclass
class VocabConfig:
    #: Proper nouns and jargon, biased into recognition via initial_prompt.
    words: list[str] = field(default_factory=list)
    #: Applied after transcription, case-insensitive, on word boundaries.
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class CleanupConfig:
    """Optional second pass through a local Ollama model."""

    enabled: bool = False
    model: str = "llama3.2:3b"
    url: str = "http://localhost:11434/api/generate"
    timeout_s: float = 6.0
    #: Reject the rewrite if it is wildly shorter/longer than the input --
    #: small models sometimes answer the text instead of cleaning it.
    min_ratio: float = 0.5
    max_ratio: float = 1.6


@dataclass
class UIConfig:
    overlay: bool = True
    tray: bool = True
    sounds: bool = True
    notifications: bool = True


@dataclass
class HistoryConfig:
    enabled: bool = True
    max_rows: int = 10000


@dataclass
class Config:
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    vocab: VocabConfig = field(default_factory=VocabConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)


def _merge(target: Any, data: dict[str, Any], path: str = "") -> None:
    """Overlay a parsed-TOML dict onto a dataclass instance, in place.

    Unknown keys are warned about rather than raising, so a config written by a
    newer version does not brick an older daemon.
    """
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        where = f"{path}{key}"
        spec = known.get(key)
        if spec is None:
            log.warning("ignoring unknown config key %r", where)
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value, path=f"{where}.")
        else:
            setattr(target, key, value)


def load(path: Path | None = None) -> Config:
    """Load config, falling back to defaults for anything unset or unparsable."""
    path = path or CONFIG_PATH
    cfg = Config()
    if not path.exists():
        return cfg
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.error("could not read %s (%s); using defaults", path, exc)
        return cfg
    _merge(cfg, data)
    return cfg


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def write_default(path: Path | None = None, force: bool = False) -> Path:
    """Write the commented default config. Never clobbers unless forced."""
    path = path or CONFIG_PATH
    ensure_dirs()
    if path.exists() and not force:
        return path
    path.write_text(DEFAULT_TOML)
    return path


DEFAULT_TOML = """\
# WhisperKey configuration.
# Restart the daemon after editing:  systemctl --user restart whisperkey
# (or just:  whisperkey restart)

[hotkey]
# The dictation key. Hold it to dictate; double-tap it to latch recording on,
# then tap once more to stop.
#
# "cmd_l" is Left Command on an Apple keyboard (Super/Windows elsewhere).
# NOTE: GNOME normally opens the Activities overview when Super_L is tapped.
# The installer moves that to Super_R so this key is free:
#   gsettings set org.gnome.mutter overlay-key 'Super_R'
# Undo with:  gsettings reset org.gnome.mutter overlay-key
#
# Not sure what a key is called? Run:  whisperkey detect-key
# Common values: cmd_l, cmd_r, alt_r, ctrl_r, caps_lock, f9
key = "cmd_l"
hold_min_ms = 250      # shorter than this counts as a tap, not a dictation
double_tap_ms = 400    # window for the second tap of a double-tap
abort_key = "esc"      # discards an in-progress recording
enabled = true

[audio]
device = ""            # PipeWire source name or id; empty = system default
sample_rate = 16000
max_seconds = 300
min_seconds = 0.35     # drop anything shorter as an accidental tap
preroll_ms = 0         # >0 keeps the mic always open to catch early speech
backend = "auto"       # auto | pw-record | arecord

[model]
name = "large-v3-turbo"
device = "auto"        # auto | cuda | cpu
compute_type = "auto"  # auto | float16 | int8_float16 | int8
beam_size = 1          # 1 is greedy and fastest; 5 is marginally more accurate
language = "en"        # "" to autodetect
vad = true             # trim silence before transcribing
initial_prompt = ""

[inject]
method = "paste"       # paste (fast, unicode-safe) | type (slow, more compatible)
paste_combo = "ctrl+v"
terminal_paste_combo = "ctrl+shift+v"
settle_ms = 60
restore_clipboard = true
restore_clipboard_ms = 500
type_delay_ms = 6
trailing_space = true
# Force a method for specific apps, keyed by WM_CLASS:
# [inject.per_app_method]
# "jetbrains-idea" = "type"

[vocab]
# Names and jargon. These are fed to Whisper as context, which measurably
# improves recognition of anything unusual.
words = []
# Applied after transcription, case-insensitive, on word boundaries:
# [vocab.replacements]
# "clod code" = "Claude Code"

[cleanup]
# Optional second pass through a local Ollama model to fix punctuation and
# strip filler words. Needs:  ollama pull llama3.2:3b
enabled = false
model = "llama3.2:3b"
url = "http://localhost:11434/api/generate"
timeout_s = 6.0
min_ratio = 0.5
max_ratio = 1.6

[ui]
overlay = true         # floating pill with a live level meter while recording
tray = true            # needs gir1.2-ayatanaappindicator3-0.1; ignored if absent
sounds = true
notifications = true

[history]
enabled = true
max_rows = 10000
"""
