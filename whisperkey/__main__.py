"""WhisperKey command line.

``whisperkey`` with no arguments runs the daemon. Everything else either talks
to a running daemon over its control socket or is a diagnostic.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from . import __version__

#: Set once we have re-executed with LD_LIBRARY_PATH pointing at the pip-installed
#: CUDA libraries. The dynamic loader only reads that variable at process start,
#: so setting it in-process is not enough -- we must exec ourselves again.
_REEXEC_FLAG = "WHISPERKEY_REEXEC"

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
)


def _maybe_reexec() -> None:
    if os.environ.get(_REEXEC_FLAG):
        return
    from .transcriber import cuda_library_paths

    paths = cuda_library_paths()
    if not paths:
        return
    current = [p for p in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if p]
    if all(p in current for p in paths):
        return
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(paths + current)
    env[_REEXEC_FLAG] = "1"
    os.execve(sys.executable,
              [sys.executable, "-m", "whisperkey", *sys.argv[1:]], env)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)


# -- diagnostics ----------------------------------------------------------

class Doctor:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  {GREEN}✓{RESET} {label}" + (f" {DIM}{detail}{RESET}" if detail else ""))

    def warn(self, label: str, detail: str = "") -> None:
        self.warnings += 1
        print(f"  {YELLOW}!{RESET} {label}" + (f" {DIM}{detail}{RESET}" if detail else ""))

    def fail(self, label: str, detail: str = "") -> None:
        self.failures += 1
        print(f"  {RED}✗{RESET} {label}" + (f" {DIM}{detail}{RESET}" if detail else ""))


def cmd_doctor(args) -> int:
    from . import config as config_mod
    from . import xsession

    doc = Doctor()
    print("\nWhisperKey diagnostics\n")

    print("Session")
    if xsession.is_wayland():
        doc.fail("Wayland session detected",
                 "log in with 'Ubuntu on Xorg' -- X11 is required")
    else:
        doc.ok("X11 session", os.environ.get("XDG_SESSION_TYPE", "?"))
    try:
        display, xauth = xsession.ensure_display()
        doc.ok("X display reachable", f"{display} auth={xauth or 'default'}")
    except RuntimeError as exc:
        doc.fail("no X display", str(exc))

    print("\nInput and injection")
    try:
        from pynput import keyboard

        keyboard.Controller()
        doc.ok("pynput XTEST backend", keyboard.Listener.__module__)
    except Exception as exc:
        doc.fail("pynput unavailable", f"{type(exc).__name__}: {exc}")
    try:
        from .config import load
        from .injector import Injector

        wm = Injector(load().inject).active_window_class()
        doc.ok("focused-window detection", f"WM_CLASS={wm or 'unknown'}")
    except Exception as exc:
        doc.fail("cannot read the focused window", str(exc))
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401

        doc.ok("GTK 3 (clipboard and overlay)")
    except Exception as exc:
        doc.fail("GTK 3 unavailable", str(exc))

    print("\nAudio")
    from .config import load
    from .recorder import Recorder, RecorderError

    cfg = load()
    try:
        import numpy as np

        recorder = Recorder(cfg.audio)
        recorder.start()
        import time as _time

        _time.sleep(0.7)
        audio = recorder.stop()
        seconds = audio.size / cfg.audio.sample_rate
        if seconds < 0.2:
            doc.fail("microphone produced no audio",
                     f"only {seconds:.2f}s captured")
        else:
            peak = float(np.abs(audio).max()) if audio.size else 0.0
            if peak < 1e-4:
                doc.warn("microphone is silent",
                         "check the input source and that it is unmuted")
            else:
                doc.ok("microphone capture", f"{seconds:.2f}s, peak {peak:.3f}")
    except RecorderError as exc:
        doc.fail("capture backend unavailable", str(exc))
    except Exception as exc:
        doc.fail("microphone test failed", f"{type(exc).__name__}: {exc}")

    print("\nSpeech model")
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
        if count:
            doc.ok("CUDA visible to CTranslate2", f"{count} device(s)")
        else:
            doc.warn("no CUDA device", "transcription will run on CPU and be slow")
    except Exception as exc:
        doc.fail("CTranslate2 unavailable", str(exc))

    from .transcriber import cuda_library_paths

    libs = cuda_library_paths()
    if libs:
        doc.ok("bundled CUDA libraries", f"{len(libs)} directories")
    else:
        doc.warn("no pip-installed CUDA libraries found",
                 "GPU use needs nvidia-cudnn-cu12 and nvidia-cublas-cu12")

    if args.full:
        try:
            from .transcriber import Transcriber

            transcriber = Transcriber(cfg.model)
            transcriber.load()
            transcriber.warmup()
            if transcriber.load_error:
                doc.fail("model fell back to CPU", transcriber.load_error[:160])
            else:
                doc.ok("model loaded",
                       f"{cfg.model.name} on {transcriber.device}/{transcriber.compute_type}")
        except Exception as exc:
            doc.fail("model failed to load", f"{type(exc).__name__}: {exc}")
    else:
        cache = Path.home() / ".cache/huggingface/hub"
        hits = list(cache.glob("**/*large-v3-turbo*")) if cache.exists() else []
        if hits:
            doc.ok("model present in cache")
        else:
            doc.warn("model not downloaded yet",
                     "the first run will fetch about 1.6 GB")
        print(f"  {DIM}(run 'whisperkey doctor --full' to actually load it){RESET}")

    print("\nOptional extras")
    try:
        import gi

        found = False
        for name in ("AyatanaAppIndicator3", "AppIndicator3"):
            try:
                gi.require_version(name, "0.1")
                __import__("gi.repository", fromlist=[name])
                found = True
                doc.ok("tray icon available", name)
                break
            except Exception:
                continue
        if not found:
            doc.warn("no tray icon",
                     "sudo apt install gir1.2-ayatanaappindicator3-0.1")
    except Exception:
        doc.warn("could not check for a tray icon")

    if cfg.cleanup.enabled:
        import json
        import urllib.error
        import urllib.request

        try:
            base = cfg.cleanup.url.rsplit("/api/", 1)[0]
            with urllib.request.urlopen(f"{base}/api/tags", timeout=3) as resp:
                tags = json.loads(resp.read().decode())
            names = [m.get("name", "") for m in tags.get("models", [])]
            if any(n.startswith(cfg.cleanup.model.split(":")[0]) for n in names):
                doc.ok("Ollama cleanup model present", cfg.cleanup.model)
            else:
                doc.fail("cleanup model not pulled",
                         f"run: ollama pull {cfg.cleanup.model}")
        except Exception as exc:
            doc.fail("Ollama unreachable", str(exc))
    else:
        print(f"  {DIM}· LLM cleanup is disabled in config{RESET}")

    print("\nConfiguration")
    if config_mod.CONFIG_PATH.exists():
        doc.ok("config file", str(config_mod.CONFIG_PATH))
    else:
        doc.warn("no config file yet",
                 f"run 'whisperkey config --init' to create {config_mod.CONFIG_PATH}")

    print()
    if doc.failures:
        print(f"{RED}{doc.failures} problem(s){RESET}"
              + (f", {doc.warnings} warning(s)" if doc.warnings else ""))
        return 1
    if doc.warnings:
        print(f"{YELLOW}Ready, with {doc.warnings} warning(s).{RESET}")
    else:
        print(f"{GREEN}Everything checks out.{RESET}")
    return 0


# -- commands -------------------------------------------------------------

def cmd_daemon(args) -> int:
    from .config import load, write_default
    from .daemon import Daemon

    write_default()
    cfg = load()
    if args.cpu:
        cfg.model.device = "cpu"
    if args.model:
        cfg.model.name = args.model
    return Daemon(cfg).run()


def cmd_send(command: str) -> int:
    from .daemon import send_command

    try:
        print(send_command(command))
        return 0
    except ConnectionError as exc:
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        print("Start it with:  systemctl --user start whisperkey", file=sys.stderr)
        return 1


def cmd_transcribe(args) -> int:
    """Run a file through the full pipeline -- the no-microphone test path."""
    from faster_whisper.audio import decode_audio

    from . import postprocess
    from .config import load
    from .transcriber import Transcriber

    cfg = load()
    if args.cpu:
        cfg.model.device = "cpu"
    audio = decode_audio(args.path, sampling_rate=cfg.audio.sample_rate)
    transcriber = Transcriber(cfg.model)
    transcriber.load()
    prompt = postprocess.build_initial_prompt(cfg.model, cfg.vocab)
    raw = transcriber.transcribe(audio, initial_prompt=prompt)
    final = postprocess.process(raw, cfg.vocab, cfg.cleanup)
    if args.raw:
        print(raw)
    else:
        print(final)
    return 0


def cmd_detect_key(args) -> int:
    """Report the config name of whatever key gets pressed.

    Keyboards differ more than you would expect -- an Apple Magic Keyboard has
    no Right Ctrl at all, for instance -- so rather than guessing, press the key
    you want and this prints exactly what to put in the config.
    """
    from pynput import keyboard

    from . import xsession

    try:
        xsession.ensure_display()
    except RuntimeError as exc:
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        return 2

    print("\nPress the key you want to use for dictation.")
    print(f"{DIM}(a modifier you never otherwise press is ideal; "
          f"Esc quits){RESET}\n")
    seen: dict[str, int] = {}

    def name_of(key) -> str:
        if isinstance(key, keyboard.Key):
            return key.name
        char = getattr(key, "char", None)
        return char if char else f"<{getattr(key, 'vk', '?')}>"

    def on_press(key):
        if key == keyboard.Key.esc:
            return False
        label = name_of(key)
        seen[label] = seen.get(label, 0) + 1
        note = ""
        if label in ("cmd", "cmd_l"):
            note = "  ← GNOME opens Activities when this is tapped alone"
        elif label == "cmd_r":
            note = "  ← Right Command; free on this system"
        elif label == "alt_r":
            note = "  ← Right Option; may reveal menu bars in some apps"
        print(f"  key = \"{label}\"{DIM}{note}{RESET}")
        return True

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

    if seen:
        best = max(seen, key=seen.get)
        print(f"\nTo use it, set this in "
              f"{DIM}~/.config/whisperkey/config.toml{RESET}:\n")
        print(f"  [hotkey]\n  key = \"{best}\"\n")
        print(f"then: {DIM}whisperkey restart{RESET}")
    return 0


def cmd_config(args) -> int:
    from .config import CONFIG_PATH, write_default

    if args.init:
        path = write_default(force=args.force)
        print(f"config at {path}")
        return 0
    if args.edit:
        write_default()
        editor = os.environ.get("EDITOR")
        subprocess.call([editor, str(CONFIG_PATH)] if editor
                        else ["xdg-open", str(CONFIG_PATH)])
        return 0
    print(CONFIG_PATH)
    return 0


def cmd_history(args) -> int:
    import datetime as dt

    from .config import load
    from .history import History

    rows = History(load().history).recent(args.limit)
    if not rows:
        print("no dictations recorded yet")
        return 0
    for created, text in rows:
        stamp = dt.datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M")
        print(f"{DIM}{stamp}{RESET}  {text}")
    return 0


def cmd_restart(args) -> int:
    return subprocess.call(["systemctl", "--user", "restart", "whisperkey"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whisperkey",
        description="Push-to-talk dictation that works in any application.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("daemon", help="run the daemon (default)")
    run.add_argument("--cpu", action="store_true", help="force CPU inference")
    run.add_argument("--model", help="override the model name")
    run.set_defaults(func=cmd_daemon)

    for name, help_text in (
        ("status", "show daemon status"),
        ("toggle", "start or stop dictation"),
        ("start", "begin dictating"),
        ("stop", "finish and transcribe"),
        ("last", "re-paste the previous transcript"),
        ("enable", "enable the hotkey"),
        ("disable", "disable the hotkey"),
        ("quit", "stop the daemon"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(func=lambda a, c=name: cmd_send(c))

    cleanup = subparsers.add_parser("cleanup", help="toggle the LLM cleanup pass")
    cleanup.add_argument("state", choices=["on", "off"])
    cleanup.set_defaults(func=lambda a: cmd_send(f"cleanup {a.state}"))

    doctor = subparsers.add_parser("doctor", help="diagnose the installation")
    doctor.add_argument("--full", action="store_true",
                        help="also load the model (slow, but conclusive)")
    doctor.set_defaults(func=cmd_doctor)

    transcribe = subparsers.add_parser(
        "transcribe", help="transcribe an audio file (no microphone needed)")
    transcribe.add_argument("path")
    transcribe.add_argument("--raw", action="store_true",
                            help="skip vocabulary and cleanup")
    transcribe.add_argument("--cpu", action="store_true")
    transcribe.set_defaults(func=cmd_transcribe)

    detect = subparsers.add_parser(
        "detect-key", help="press a key and learn its config name")
    detect.set_defaults(func=cmd_detect_key)

    conf = subparsers.add_parser("config", help="show or edit the config file")
    conf.add_argument("--init", action="store_true", help="write defaults")
    conf.add_argument("--force", action="store_true", help="overwrite defaults")
    conf.add_argument("--edit", action="store_true", help="open in $EDITOR")
    conf.set_defaults(func=cmd_config)

    hist = subparsers.add_parser("history", help="list recent dictations")
    hist.add_argument("-n", "--limit", type=int, default=20)
    hist.set_defaults(func=cmd_history)

    restart = subparsers.add_parser("restart", help="restart the user service")
    restart.set_defaults(func=cmd_restart)

    return parser


def main(argv: list[str] | None = None) -> int:
    _maybe_reexec()
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["daemon"])
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
