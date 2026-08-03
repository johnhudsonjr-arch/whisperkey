"""Whisper transcription via faster-whisper (CTranslate2).

The model is loaded once and kept resident -- a cold load costs seconds, which
would be intolerable per dictation.

The fragile part on Linux is CUDA: CTranslate2 needs cuDNN 9 and cuBLAS 12
shared libraries that Ubuntu does not ship. We install them as pip wheels and
point LD_LIBRARY_PATH at them (see :func:`cuda_library_paths`, used by both the
launcher and the systemd unit). If CUDA still fails we degrade to CPU int8 with
a loud warning rather than leaving the user with a dead hotkey.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

from .config import ModelConfig

log = logging.getLogger(__name__)


def cuda_library_paths() -> list[str]:
    """Directories holding the pip-installed NVIDIA shared libraries."""
    paths: list[str] = []
    for root in sys.path:
        nvidia = Path(root) / "nvidia"
        if not nvidia.is_dir():
            continue
        for lib in sorted(nvidia.glob("*/lib")):
            if any(lib.glob("*.so*")):
                paths.append(str(lib))
    return paths


def ensure_cuda_libs_visible() -> str:
    """Prepend the NVIDIA wheel lib dirs to LD_LIBRARY_PATH.

    Only affects children spawned after this call -- the dynamic loader reads
    the variable at process start, so the daemon launcher re-execs itself once
    if this changes anything (see __main__).
    """
    paths = cuda_library_paths()
    if not paths:
        return os.environ.get("LD_LIBRARY_PATH", "")
    current = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    added = [p for p in paths if p not in parts]
    if added:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(added + parts)
    return os.environ["LD_LIBRARY_PATH"]


def preload_cuda_libraries() -> list[str]:
    """Load the pip-installed CUDA shared objects into this process.

    ``LD_LIBRARY_PATH`` is only read by the dynamic loader at process start, so
    setting it from Python is too late; the CLI works around that by re-execing
    itself. That leaves every *other* entry point -- a test, a script, anyone
    importing whisperkey as a library -- unable to find libcublas and silently
    falling back to CPU.

    Loading each library by absolute path with ``RTLD_GLOBAL`` fixes it for all
    of them: once an object is in the process, a later ``dlopen("libcublas.so.12")``
    resolves against it by soname instead of searching the filesystem.

    Several passes, because these libraries depend on each other and the right
    order is not known up front (libcublas needs libcublasLt, cuDNN is split
    across several objects).
    """
    import ctypes

    pending: list[str] = []
    for directory in cuda_library_paths():
        pending.extend(str(p) for p in sorted(Path(directory).glob("*.so*")))

    loaded: list[str] = []
    for _ in range(3):
        if not pending:
            break
        failed: list[str] = []
        for path in pending:
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                loaded.append(path)
            except OSError:
                failed.append(path)
        if len(failed) == len(pending):
            break  # no progress; the rest are genuinely unloadable
        pending = failed
    if loaded:
        log.debug("preloaded %d CUDA libraries", len(loaded))
    return loaded


def _resolve_device(cfg: ModelConfig) -> tuple[str, str]:
    device = cfg.device
    if device == "auto":
        device = "cuda" if _cuda_available() else "cpu"
    compute = cfg.compute_type
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def prepare_cuda() -> None:
    """Make CUDA usable regardless of how the process was started."""
    ensure_cuda_libs_visible()  # helps any child processes we spawn
    preload_cuda_libraries()    # helps *this* process, whatever the entry point


class Transcriber:
    """Loads Whisper once, then transcribes numpy audio buffers."""

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.model = None
        self.device = "cpu"
        self.compute_type = "int8"
        self.load_error: str | None = None

    # -- loading -----------------------------------------------------------

    def load(self) -> None:
        prepare_cuda()

        from faster_whisper import WhisperModel

        device, compute = _resolve_device(self.cfg)
        started = time.monotonic()
        try:
            self.model = WhisperModel(self.cfg.name, device=device,
                                      compute_type=compute)
        except Exception as exc:
            if device == "cpu":
                raise
            log.error("CUDA load failed (%s); falling back to CPU int8. "
                      "Run `whisperkey doctor` to diagnose.", exc)
            self.load_error = str(exc)
            device, compute = "cpu", "int8"
            self.model = WhisperModel(self.cfg.name, device=device,
                                      compute_type=compute)
        self.device, self.compute_type = device, compute
        log.info("loaded %s on %s/%s in %.1fs", self.cfg.name, device, compute,
                 time.monotonic() - started)

    def warmup(self) -> None:
        """Force CUDA kernel compilation so the first real dictation is fast."""
        if self.model is None:
            return
        started = time.monotonic()
        silence = np.zeros(8000, dtype=np.float32)
        try:
            segments, _ = self.model.transcribe(silence, beam_size=1,
                                                vad_filter=False)
            list(segments)
        except Exception:
            log.exception("warm-up transcription failed (continuing anyway)")
            return
        log.info("warm-up done in %.2fs", time.monotonic() - started)

    # -- inference ---------------------------------------------------------

    def transcribe(self, audio: np.ndarray, initial_prompt: str = "") -> str:
        if self.model is None:
            raise RuntimeError("model not loaded")
        if audio.size == 0:
            return ""
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        # Whisper expects samples in [-1, 1]. Some capture devices (this
        # machine's digital mic array among them) deliver hotter signal than
        # that, and the resulting clipping measurably hurts recognition.
        peak = float(np.abs(audio).max())
        if peak > 1.0:
            log.debug("input peaked at %.2f; normalising", peak)
            audio = audio / peak
        kwargs = {
            "beam_size": self.cfg.beam_size,
            "vad_filter": self.cfg.vad,
            # Whisper otherwise loops on its own output when audio is short or
            # silent; disabling context is the standard mitigation.
            "condition_on_previous_text": False,
        }
        if self.cfg.language:
            kwargs["language"] = self.cfg.language
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt

        started = time.monotonic()
        segments, info = self.model.transcribe(audio, **kwargs)
        text = "".join(seg.text for seg in segments).strip()
        log.info("transcribed %.1fs of audio in %.2fs -> %d chars",
                 audio.size / 16000, time.monotonic() - started, len(text))
        return text
