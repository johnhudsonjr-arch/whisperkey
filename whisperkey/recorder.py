"""Microphone capture.

Audio comes from `pw-record`, PipeWire's native recorder, as raw float32 on
stdout (verified: no WAV header). This avoids a PortAudio dependency entirely,
which matters because `sounddevice` ships no Linux binary wheel and libportaudio2
would need root to install. `arecord` is kept as a fallback.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections import deque

import numpy as np

from .config import AudioConfig

log = logging.getLogger(__name__)

BYTES_PER_SAMPLE = 4  # float32
_READ_SIZE = 4096


class RecorderError(RuntimeError):
    pass


def _backend_cmd(cfg: AudioConfig) -> list[str]:
    """Build the capture command for the configured backend."""
    backend = cfg.backend
    if backend == "auto":
        backend = "pw-record" if shutil.which("pw-record") else "arecord"

    if backend == "pw-record":
        if not shutil.which("pw-record"):
            raise RecorderError("pw-record not found")
        cmd = [
            "pw-record",
            "--rate", str(cfg.sample_rate),
            "--channels", "1",
            "--format", "f32",
            "--latency", "20ms",
        ]
        if cfg.device:
            cmd += ["--target", cfg.device]
        return cmd + ["-"]

    if backend == "arecord":
        if not shutil.which("arecord"):
            raise RecorderError("arecord not found")
        cmd = ["arecord", "-q", "-t", "raw", "-f", "FLOAT_LE",
               "-r", str(cfg.sample_rate), "-c", "1"]
        if cfg.device:
            cmd += ["-D", cfg.device]
        return cmd + ["-"]

    raise RecorderError(f"unknown audio backend {backend!r}")


class Recorder:
    """Captures mono float32 audio and exposes a live level for the overlay.

    Two modes, chosen by ``preroll_ms``:

    * ``0`` (default) -- the mic process is spawned on :meth:`start` and killed
      on :meth:`stop`, so GNOME's mic-in-use indicator reflects reality. Costs
      ~50 ms of startup before audio flows.
    * ``>0`` -- the mic stays open continuously and the last N ms are retained,
      so speech that begins fractionally before the keypress is still captured.
    """

    def __init__(self, cfg: AudioConfig, on_limit=None):
        self.cfg = cfg
        self._on_limit = on_limit
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._capturing = False
        self._level = 0.0
        self._samples = 0
        self._limit_fired = False
        self._remainder = b""
        preroll_samples = int(cfg.sample_rate * cfg.preroll_ms / 1000)
        self._ring: deque[np.ndarray] | None = deque() if preroll_samples else None
        self._ring_samples = 0
        self._preroll_samples = preroll_samples

    # -- lifecycle ---------------------------------------------------------

    @property
    def continuous(self) -> bool:
        return self._preroll_samples > 0

    def open(self) -> None:
        """Start the mic process. Only used in continuous (preroll) mode."""
        if self.continuous and self._proc is None:
            self._spawn()

    def _spawn(self) -> None:
        cmd = _backend_cmd(self.cfg)
        log.debug("spawning capture: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise RecorderError(f"could not start {cmd[0]}: {exc}") from exc
        self._remainder = b""
        self._reader = threading.Thread(
            target=self._read_loop, args=(self._proc,), daemon=True,
            name="whisperkey-audio",
        )
        self._reader.start()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()

    def close(self) -> None:
        with self._lock:
            self._capturing = False
        self._kill()

    # -- capture control ---------------------------------------------------

    def start(self) -> None:
        """Begin accumulating audio."""
        with self._lock:
            self._chunks = []
            self._samples = 0
            self._limit_fired = False
            self._capturing = True
            if self._ring is not None and self._ring:
                # Seed with pre-roll so early speech is not clipped.
                self._chunks = list(self._ring)
                self._samples = self._ring_samples
        if not self.continuous:
            self._spawn()

    def stop(self) -> np.ndarray:
        """Stop accumulating and return everything captured."""
        with self._lock:
            self._capturing = False
            chunks = self._chunks
            self._chunks = []
            self._samples = 0
        if not self.continuous:
            self._kill()
        self._level = 0.0
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)

    def abort(self) -> None:
        """Stop and discard."""
        self.stop()

    # -- reader ------------------------------------------------------------

    def _read_loop(self, proc: subprocess.Popen) -> None:
        stdout = proc.stdout
        assert stdout is not None
        max_samples = self.cfg.sample_rate * self.cfg.max_seconds
        while True:
            try:
                data = stdout.read(_READ_SIZE)
            except (ValueError, OSError):
                break
            if not data:
                break
            # stdout reads need not land on float boundaries.
            data = self._remainder + data
            usable = len(data) - (len(data) % BYTES_PER_SAMPLE)
            self._remainder = data[usable:]
            if not usable:
                continue
            block = np.frombuffer(data[:usable], dtype=np.float32)

            rms = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
            # Exponential smoothing keeps the meter from strobing.
            self._level = max(rms, self._level * 0.75)

            fired = False
            with self._lock:
                if self._ring is not None:
                    self._ring.append(block)
                    self._ring_samples += block.size
                    while self._ring_samples - self._ring[0].size >= self._preroll_samples:
                        self._ring_samples -= self._ring.popleft().size
                if self._capturing:
                    self._chunks.append(block)
                    self._samples += block.size
                    if self._samples >= max_samples and not self._limit_fired:
                        self._limit_fired = fired = True
            if fired and self._on_limit:
                log.warning("recording hit the %ss cap", self.cfg.max_seconds)
                self._on_limit()
        if proc is self._proc and not self.continuous:
            log.debug("capture process ended")

    # -- introspection -----------------------------------------------------

    @property
    def level(self) -> float:
        """Smoothed RMS in roughly 0..1, for the overlay meter."""
        return min(1.0, self._level * 4.0)

    @property
    def duration(self) -> float:
        return self._samples / self.cfg.sample_rate

    @property
    def running(self) -> bool:
        return self._capturing
