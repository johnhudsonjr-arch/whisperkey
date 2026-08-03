"""End-to-end check of the text-injection path.

Opens a window with a text field, waits until that window genuinely owns input
focus, injects text through the real clipboard + XTEST paste path, and verifies
it arrived.

The focus guard matters: a synthetic Ctrl+V goes wherever focus happens to be.
If this window has not taken focus, the test aborts rather than pasting into
whatever the user was doing.

    .venv/bin/python tools/selftest_inject.py
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from whisperkey.config import load  # noqa: E402
from whisperkey.injector import Injector  # noqa: E402

PAYLOAD = "whisperkey self test ✓ ünïcode 123"
FOCUS_TIMEOUT_MS = 4000


class SelfTest:
    def __init__(self, payload: str = PAYLOAD) -> None:
        self.payload = payload
        self.cfg = load()
        self.injector = Injector(self.cfg.inject)
        self.result = 1
        self.reason = "did not run"
        self.saved_clipboard = None

        self.window = Gtk.Window(title="WhisperKey injection self-test")
        self.window.set_default_size(460, 90)
        self.window.set_keep_above(True)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("waiting for injection…")
        self.window.add(self.entry)
        self.window.connect("destroy", lambda _w: Gtk.main_quit())

    # -- helpers -----------------------------------------------------------

    def _our_xid(self) -> int:
        return self.window.get_window().get_xid()

    def _focused_xid(self) -> int:
        from Xlib import X

        display = self.injector.display
        root = display.screen().root
        prop = root.get_full_property(
            display.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType
        )
        return int(prop.value[0]) if prop and prop.value else 0

    # -- flow --------------------------------------------------------------

    def run(self) -> int:
        self.window.show_all()
        self.window.present()
        self.entry.grab_focus()
        self.elapsed = 0
        GLib.timeout_add(150, self._await_focus)
        Gtk.main()
        print(f"\n{'PASS' if self.result == 0 else 'FAIL'}: {self.reason}")
        return self.result

    def _await_focus(self) -> bool:
        self.elapsed += 150
        try:
            ours, focused = self._our_xid(), self._focused_xid()
        except Exception as exc:
            self._finish(1, f"could not query focus: {exc}")
            return False
        if focused == ours:
            print(f"focus acquired (xid {ours}); injecting…")
            GLib.timeout_add(200, self._inject)
            return False
        if self.elapsed >= FOCUS_TIMEOUT_MS:
            # Refuse to paste blind -- it would land in the user's real window.
            self._finish(2, "test window never got focus; aborted without "
                            "sending any keystroke (nothing was pasted "
                            "anywhere else)")
            return False
        return True

    def _inject(self) -> bool:
        try:
            method = self.injector.inject(self.payload)
            print(f"injected using: {method}")
        except Exception as exc:
            self._finish(1, f"injection raised {type(exc).__name__}: {exc}")
            return False
        GLib.timeout_add(700, self._verify)
        return False

    def _verify(self) -> bool:
        text = self.entry.get_text()
        print(f"entry now contains: {text!r}")
        if self.payload.strip() in text:
            self._finish(0, "clipboard + XTEST paste delivered the exact text, "
                            "unicode intact")
        elif text.strip():
            self._finish(1, f"text arrived but differs: {text!r}")
        else:
            self._finish(1, "nothing arrived in the entry")
        return False

    def _finish(self, code: int, reason: str) -> None:
        self.result, self.reason = code, reason
        GLib.timeout_add(900, lambda: (Gtk.main_quit(), False)[1])


def _payload_from_audio(path: str) -> str:
    """Transcribe a file so the test covers transcription *and* injection."""
    from faster_whisper.audio import decode_audio

    from whisperkey import postprocess
    from whisperkey.transcriber import Transcriber

    cfg = load()
    audio = decode_audio(path, sampling_rate=cfg.audio.sample_rate)
    transcriber = Transcriber(cfg.model)
    transcriber.load()
    text = transcriber.transcribe(
        audio, initial_prompt=postprocess.build_initial_prompt(cfg.model, cfg.vocab))
    print(f"transcribed: {text!r}")
    return postprocess.process(text, cfg.vocab, cfg.cleanup)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(SelfTest(_payload_from_audio(sys.argv[1])).run())
    sys.exit(SelfTest().run())
