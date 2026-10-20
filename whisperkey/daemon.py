"""The WhisperKey daemon.

Holds the model in VRAM and wires the pieces together. Three threads matter:

* the GLib main loop (main thread) -- owns all GTK: overlay, tray, clipboard;
* the pynput listener thread -- delivers key events;
* a transcription worker -- so a slow transcription never stalls the hotkey.

Rule: audio control may happen on any thread (Recorder is internally locked),
but anything touching GTK is marshalled onto the main loop with GLib.idle_add.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

from .gtk3 import GLib

from . import postprocess, xsession
from .config import SOCKET_PATH, Config
from .history import History
from .hotkey import (Action, EvdevHotkeyListener, HotkeyListener,
                     PortalHotkeyListener)
from .injector import Injector
from .recorder import Recorder, RecorderError
from .sounds import Sounds
from .transcriber import Transcriber
from .ui.overlay import Overlay
from .ui.tray import Notifier, Tray

log = logging.getLogger(__name__)


@dataclass
class Job:
    audio: object
    duration: float
    started: float
    latched: bool


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.notifier = Notifier(cfg.ui.notifications)
        self.sounds = Sounds(cfg.ui.sounds)
        self.history = History(cfg.history)
        self.recorder = Recorder(cfg.audio, on_limit=self._on_recording_limit)
        self.transcriber = Transcriber(cfg.model)
        self.injector = Injector(cfg.inject)
        self.overlay = Overlay(cfg.ui)
        self.overlay.set_level_source(lambda: self.recorder.level)
        self.tray: Tray | None = None
        self.listener: HotkeyListener | None = None

        self._jobs: queue.Queue[Job | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._loop: GLib.MainLoop | None = None
        self._server: socket.socket | None = None
        self._state = "starting"
        self._busy = threading.Lock()
        self._last_text = ""
        self._stopping = False

    # -- backend selection -------------------------------------------------

    def _make_listener(self):
        """Pick the hotkey backend for this session type. Does not start it."""
        if not self.wayland:
            return HotkeyListener(self.cfg.hotkey, self._on_action)

        from . import evdev_input

        # Kernel first: the portal is refused outright on current Ubuntu, and
        # evdev keeps the abort key, which a portal shortcut cannot.
        problem = evdev_input.permission_problem()
        if not problem:
            return EvdevHotkeyListener(self.cfg.hotkey, self._on_action)
        log.info("kernel input unavailable (%s); trying the portal", problem)
        if self.cfg.hotkey.abort_key:
            log.info("abort key %r needs kernel input and is inactive here",
                     self.cfg.hotkey.abort_key)
        return PortalHotkeyListener(self.cfg.hotkey, self._on_action)

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        # X11 is no longer a precondition. GNOME 49 dropped the X11 session and
        # Ubuntu 26.04 ships no /usr/share/xsessions at all, so refusing to start
        # on Wayland would mean refusing to start on a current desktop. The
        # portal backends cover both jobs X11 used to do; see whisperkey.portal.
        self.wayland = xsession.is_wayland()
        if self.wayland:
            log.info("Wayland session; using the desktop portals for input")
        else:
            try:
                xsession.ensure_display()
            except RuntimeError as exc:
                log.error("%s", exc)
                return 2

        self.sounds.ensure()
        self._set_state("loading")
        try:
            self.transcriber.load()
        except Exception as exc:
            log.exception("could not load the speech model")
            self.notifier.send("WhisperKey failed to start", str(exc),
                               urgency="critical")
            return 1
        self.transcriber.warmup()

        if self.transcriber.load_error:
            self.notifier.send(
                "WhisperKey is running on CPU",
                "The GPU could not be used, so dictation will be slow. "
                "Run 'whisperkey doctor' for details.",
                urgency="critical",
            )

        self.tray = Tray(self.cfg.ui, self._tray_actions())
        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="whisperkey-worker")
        self._worker.start()

        if self.recorder.continuous:
            try:
                self.recorder.open()
            except RecorderError as exc:
                log.error("pre-roll capture unavailable: %s", exc)

        self.listener = self._make_listener()
        try:
            self.listener.start()
        except Exception as exc:
            if not self.wayland:
                log.exception("could not register the hotkey")
                self.notifier.send("WhisperKey failed to start", str(exc),
                                   urgency="critical")
                return 1
            # On Wayland a refused shortcut is not fatal. The socket "toggle"
            # verb still works, so a GNOME custom keybinding pointed at
            # `whisperkey toggle` keeps dictation usable -- strictly better than
            # exiting and having systemd restart us every three seconds forever.
            log.error("no hotkey backend is usable: %s", exc)
            self.notifier.send(
                "WhisperKey has no hotkey",
                "Run 'whisperkey doctor' for the two commands that enable "
                "kernel input. Until then, a key bound to 'whisperkey toggle' "
                "still works.",
                urgency="critical")
            self.listener = None

        self._start_control_socket()
        self._set_state("idle")
        device = self.transcriber.device
        if self.listener is None:
            # Saying "hold cmd_l to dictate" with no hotkey registered is the
            # kind of confident wrongness that sends people hunting for a broken
            # microphone, so say what is actually true instead.
            log.info("ready -- no hotkey; use a key bound to 'whisperkey "
                     "toggle' (%s)", device)
            self.notifier.send(
                "WhisperKey ready, without a hotkey",
                f"Dictate with a key bound to 'whisperkey toggle'. ({device})",
            )
        else:
            log.info("ready -- hold %s to dictate (%s)",
                     self.cfg.hotkey.key, device)
            self.notifier.send(
                "WhisperKey ready",
                f"Hold {self._key_label()} to dictate, double-tap to latch. "
                f"({device})",
            )

        self._loop = GLib.MainLoop()
        try:
            self._loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def _key_label(self) -> str:
        return self.cfg.hotkey.key.replace("_r", " (right)").replace("_l", " (left)").title()

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("shutting down")
        if self.listener:
            self.listener.stop()
        self.recorder.close()
        self._jobs.put(None)
        if self._server is not None:
            try:
                self._server.close()
            finally:
                SOCKET_PATH.unlink(missing_ok=True)
        self.overlay.destroy()
        self.history.close()
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()

    # -- hotkey ------------------------------------------------------------

    def _on_action(self, action: Action) -> None:
        """Called on the pynput listener thread."""
        if action is Action.START:
            self._begin_recording()
        elif action is Action.STOP:
            self._finish_recording()
        elif action is Action.DISCARD:
            self._discard_recording()

    def _begin_recording(self) -> None:
        if self._state == "transcribing":
            log.debug("ignoring hotkey while still transcribing")
            return
        try:
            self.recorder.start()
        except RecorderError as exc:
            log.error("could not start recording: %s", exc)
            self.notifier.send("WhisperKey: microphone unavailable", str(exc),
                               urgency="critical")
            return
        latched = self.listener is not None and \
            self.listener.machine.state.value == "latched"
        self.sounds.play("start")
        self._set_state("recording")
        GLib.idle_add(self.overlay.show_recording, latched)

    def _finish_recording(self) -> None:
        started = time.monotonic()
        audio = self.recorder.stop()
        self.sounds.play("stop")
        duration = audio.size / max(1, self.cfg.audio.sample_rate)
        if duration < self.cfg.audio.min_seconds:
            log.info("discarding %.2fs of audio (below min_seconds)", duration)
            self._set_state("idle")
            GLib.idle_add(self.overlay.hide)
            return
        self._set_state("transcribing")
        GLib.idle_add(self.overlay.show_transcribing)
        self._jobs.put(Job(audio=audio, duration=duration, started=started,
                           latched=False))

    def _discard_recording(self) -> None:
        self.recorder.abort()
        self._set_state("idle")
        GLib.idle_add(self.overlay.hide)

    def _on_recording_limit(self) -> None:
        log.warning("hit the recording length cap; finishing automatically")
        if self.listener is not None:
            self.listener.machine.reset()
        self._finish_recording()

    # -- worker ------------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._process(job)
            except Exception:
                log.exception("transcription job failed")
                self.notifier.send("WhisperKey: transcription failed",
                                   "See 'journalctl --user -u whisperkey'.",
                                   urgency="critical")
            finally:
                self._set_state("idle")
                GLib.idle_add(self.overlay.hide)

    def _process(self, job: Job) -> None:
        prompt = postprocess.build_initial_prompt(self.cfg.model, self.cfg.vocab)
        raw = self.transcriber.transcribe(job.audio, initial_prompt=prompt)
        if not raw.strip():
            log.info("no speech detected")
            return
        final = postprocess.process(raw, self.cfg.vocab, self.cfg.cleanup)
        if not final:
            return
        latency = time.monotonic() - job.started
        self._last_text = final
        GLib.idle_add(self._deliver, final, raw, job.duration, latency)

    def _deliver(self, final: str, raw: str, duration: float,
                 latency: float) -> bool:
        """Runs on the main loop: clipboard and paste must not be threaded."""
        wm_class = ""
        method = "none"
        try:
            wm_class = self.injector.active_window_class()
            method = self.injector.inject(final)
        except Exception:
            log.exception("could not inject text")
            self.notifier.send("WhisperKey could not paste",
                               final[:120], urgency="critical")
        self.history.add(duration=duration, latency=latency, raw_text=raw,
                         final_text=final, wm_class=wm_class, method=method)
        log.info("delivered %d chars in %.2fs total", len(final), latency)
        return False

    # -- state -------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        self._state = state
        detail = {
            "idle": "Ready", "recording": "Listening…",
            "transcribing": "Transcribing…", "loading": "Loading model…",
        }.get(state, state)
        if self.tray is not None:
            GLib.idle_add(self.tray.set_state, state, detail)

    # -- tray/CLI actions --------------------------------------------------

    def _tray_actions(self) -> dict:
        return {
            "set_enabled": self.set_enabled,
            "set_cleanup": self.set_cleanup,
            "cleanup_enabled": lambda: self.cfg.cleanup.enabled,
            "repaste": self.repaste_last,
            "copy_last": self.copy_last,
            "edit_config": self.edit_config,
            "show_history": self.show_history,
            "quit": self.shutdown,
        }

    def set_enabled(self, value: bool) -> None:
        if self.listener is not None:
            self.listener.enabled = value
        log.info("dictation %s", "enabled" if value else "disabled")

    def set_cleanup(self, value: bool) -> None:
        self.cfg.cleanup.enabled = value
        log.info("LLM cleanup %s", "enabled" if value else "disabled")

    def repaste_last(self) -> None:
        text = self._last_text or self.history.last()
        if not text:
            self.notifier.send("WhisperKey", "Nothing to re-paste yet.")
            return
        GLib.idle_add(self.injector.inject, text)

    def copy_last(self) -> None:
        text = self._last_text or self.history.last()
        if text:
            GLib.idle_add(self.injector._set_clipboard, text)

    def edit_config(self) -> None:
        from .config import CONFIG_PATH

        subprocess.Popen(["xdg-open", str(CONFIG_PATH)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def show_history(self) -> None:
        rows = self.history.recent(15)
        if not rows:
            self.notifier.send("WhisperKey history", "Nothing recorded yet.")
            return
        body = "\n".join(f"• {text[:70]}" for _ts, text in rows[:8])
        self.notifier.send("Recent dictations", body)

    # -- control socket ----------------------------------------------------

    def _start_control_socket(self) -> None:
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SOCKET_PATH.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        server.listen(4)
        os.chmod(SOCKET_PATH, 0o600)
        self._server = server
        threading.Thread(target=self._serve, args=(server,), daemon=True,
                         name="whisperkey-control").start()
        log.debug("control socket at %s", SOCKET_PATH)

    def _serve(self, server: socket.socket) -> None:
        while not self._stopping:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    command = conn.recv(4096).decode().strip()
                    conn.sendall(self._handle(command).encode())
                except OSError:
                    continue

    def _handle(self, command: str) -> str:
        verb, _, argument = command.partition(" ")
        verb = verb.lower()
        if verb == "status":
            stats = self.history.stats()
            return (f"state={self._state} device={self.transcriber.device} "
                    f"enabled={self.listener.enabled if self.listener else False} "
                    f"cleanup={self.cfg.cleanup.enabled} "
                    f"dictations={stats['count']}")
        if verb == "toggle":
            # Mirrors a double-tap: start if idle, finish if recording.
            if self._state == "recording":
                if self.listener:
                    self.listener.machine.reset()
                self._finish_recording()
                return "stopped"
            self._begin_recording()
            if self.listener:
                from .hotkey import State

                self.listener.machine.state = State.LATCHED
            return "started"
        if verb == "start":
            self._begin_recording()
            return "started"
        if verb == "stop":
            self._finish_recording()
            return "stopped"
        if verb == "last":
            self.repaste_last()
            return "ok"
        if verb == "enable":
            self.set_enabled(True)
            return "enabled"
        if verb == "disable":
            self.set_enabled(False)
            return "disabled"
        if verb == "cleanup":
            self.set_cleanup(argument.strip().lower() in ("1", "on", "true", "yes"))
            return f"cleanup={self.cfg.cleanup.enabled}"
        if verb == "quit":
            GLib.idle_add(self.shutdown)
            return "bye"
        return f"unknown command {verb!r}"


def send_command(command: str, timeout: float = 5.0) -> str:
    """Talk to a running daemon. Raises ConnectionError if it is not up."""
    if not SOCKET_PATH.exists():
        raise ConnectionError("WhisperKey is not running")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(SOCKET_PATH))
        client.sendall(command.encode())
        return client.recv(4096).decode()
    except (OSError, socket.timeout) as exc:
        raise ConnectionError(f"WhisperKey is not responding ({exc})") from exc
    finally:
        client.close()
