"""Short audio cues for start/stop.

Generated once with numpy rather than shipped as binaries, then played through
whichever CLI player exists. Playback is fire-and-forget so it never delays the
dictation path.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from .config import DATA_DIR

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
_PLAYERS = (["paplay"], ["aplay", "-q"], ["pw-play"])


def _tone(freq_from: float, freq_to: float, ms: int = 90,
          volume: float = 0.18) -> np.ndarray:
    n = int(SAMPLE_RATE * ms / 1000)
    t = np.linspace(0.0, ms / 1000.0, n, endpoint=False)
    freq = np.linspace(freq_from, freq_to, n)
    wave_data = np.sin(2 * np.pi * freq * t)
    # Short raised-cosine envelope; a hard edge would click.
    edge = max(1, n // 8)
    envelope = np.ones(n)
    envelope[:edge] = np.linspace(0, 1, edge) ** 2
    envelope[-edge:] = np.linspace(1, 0, edge) ** 2
    return (wave_data * envelope * volume).astype(np.float32)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SAMPLE_RATE)
        fh.writeframes(pcm.tobytes())


class Sounds:
    def __init__(self, enabled: bool = True, directory: Path | None = None):
        self.enabled = enabled
        self.dir = directory or (DATA_DIR / "sounds")
        self._player: list[str] | None = None
        self._ready = False

    def _find_player(self) -> list[str] | None:
        for candidate in _PLAYERS:
            if shutil.which(candidate[0]):
                return candidate
        return None

    def ensure(self) -> None:
        if self._ready:
            return
        self._ready = True
        self._player = self._find_player()
        try:
            start = self.dir / "start.wav"
            stop = self.dir / "stop.wav"
            if not start.exists():
                _write_wav(start, _tone(660, 990, ms=80))
            if not stop.exists():
                _write_wav(stop, _tone(880, 560, ms=90))
        except Exception:
            log.debug("could not generate audio cues", exc_info=True)
            self.enabled = False

    def play(self, name: str) -> None:
        if not self.enabled:
            return
        self.ensure()
        if self._player is None:
            return
        path = self.dir / f"{name}.wav"
        if not path.exists():
            return
        try:
            subprocess.Popen(
                self._player + [str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            log.debug("could not play %s", path, exc_info=True)
