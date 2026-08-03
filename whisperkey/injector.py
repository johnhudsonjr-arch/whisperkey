"""Getting transcribed text into whatever window has focus.

Strategy is clipboard-then-synthetic-paste rather than simulated typing: it is
instant regardless of length and unicode-safe, where typing 300 characters one
keystroke at a time is slow and drops characters in some toolkits.

Three X11 facilities are used, none of which need root:
  * python-xlib reads ``_NET_ACTIVE_WINDOW`` to identify the target app, so we
    know whether it needs Ctrl+V or Ctrl+Shift+V.
  * GTK owns the CLIPBOARD selection (the daemon is long-lived, so it can serve
    paste requests -- a one-shot ``xclip`` could not).
  * pynput drives XTEST to synthesise the paste chord.
"""

from __future__ import annotations

import logging
import time

from .config import InjectConfig

log = logging.getLogger(__name__)

_MOD_NAMES = {"ctrl", "control", "shift", "alt", "super", "cmd", "meta"}


class Injector:
    def __init__(self, cfg: InjectConfig):
        self.cfg = cfg
        self._kb = None
        self._display = None
        self._pending_restore = None

    # -- lazy handles ------------------------------------------------------

    @property
    def keyboard(self):
        if self._kb is None:
            from pynput.keyboard import Controller

            self._kb = Controller()
        return self._kb

    @property
    def display(self):
        if self._display is None:
            from Xlib import display as xdisplay

            self._display = xdisplay.Display()
        return self._display

    # -- target identification --------------------------------------------

    def active_window_class(self) -> str:
        """WM_CLASS of the focused window, lowercased. Empty if undeterminable."""
        try:
            from Xlib import X

            disp = self.display
            root = disp.screen().root
            prop = root.get_full_property(
                disp.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType
            )
            if not prop or not prop.value:
                return ""
            win = disp.create_resource_object("window", prop.value[0])
            # WM_CLASS often lives on a parent of the focused child window.
            for _ in range(8):
                if win is None:
                    break
                try:
                    cls = win.get_wm_class()
                except Exception:
                    cls = None
                if cls:
                    return str(cls[-1]).lower()
                try:
                    win = win.query_tree().parent
                except Exception:
                    break
            return ""
        except Exception:
            log.debug("could not read active window class", exc_info=True)
            return ""

    def _method_for(self, wm_class: str) -> str:
        override = self.cfg.per_app_method.get(wm_class)
        if override:
            return override
        return self.cfg.method

    def _combo_for(self, wm_class: str) -> str:
        terminals = {t.lower() for t in self.cfg.terminal_classes}
        if wm_class and any(t in wm_class for t in terminals):
            return self.cfg.terminal_paste_combo
        return self.cfg.paste_combo

    # -- keystrokes --------------------------------------------------------

    def _resolve(self, name: str):
        from pynput.keyboard import Key, KeyCode

        name = name.strip().lower()
        alias = {"control": "ctrl", "super": "cmd", "meta": "cmd"}.get(name, name)
        key = getattr(Key, alias, None)
        if key is not None:
            return key
        if len(name) == 1:
            return KeyCode.from_char(name)
        raise ValueError(f"unknown key {name!r}")

    def send_combo(self, combo: str) -> None:
        parts = [p for p in combo.split("+") if p.strip()]
        if not parts:
            return
        mods = [self._resolve(p) for p in parts[:-1] if p.lower() in _MOD_NAMES]
        final = self._resolve(parts[-1])
        kb = self.keyboard
        for mod in mods:
            kb.press(mod)
        try:
            kb.press(final)
            kb.release(final)
        finally:
            for mod in reversed(mods):
                kb.release(mod)

    # -- clipboard ---------------------------------------------------------

    def _set_clipboard(self, text: str) -> str | None:
        from .gtk3 import Gdk, Gtk

        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        previous = clipboard.wait_for_text()
        clipboard.set_text(text, -1)
        # Let GTK actually take ownership before we fire the paste chord.
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        return previous

    def _restore_clipboard(self, previous: str | None) -> bool:
        from .gtk3 import Gdk, Gtk

        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        if previous is None:
            clipboard.clear()
        else:
            clipboard.set_text(previous, -1)
        return False  # one-shot GLib timeout

    # -- public ------------------------------------------------------------

    def inject(self, text: str) -> str:
        """Place ``text`` into the focused window. Returns the method used."""
        if not text:
            return "noop"
        if self.cfg.trailing_space and not text.endswith(" "):
            text += " "

        wm_class = self.active_window_class()
        method = self._method_for(wm_class)
        log.info("injecting %d chars into %r via %s", len(text),
                 wm_class or "<unknown>", method)

        if method == "type":
            self._type(text)
            return "type"

        previous = None
        try:
            previous = self._set_clipboard(text)
        except Exception:
            log.exception("clipboard failed; falling back to typing")
            self._type(text)
            return "type"

        # Give the focused app a moment to finish processing the hotkey's keyup
        # before we send a chord, otherwise the modifier state can be misread.
        time.sleep(self.cfg.settle_ms / 1000.0)
        try:
            self.send_combo(self._combo_for(wm_class))
        except Exception:
            log.exception("paste chord failed; falling back to typing")
            self._type(text)
            return "type"

        if self.cfg.restore_clipboard:
            try:
                from .gtk3 import GLib

                GLib.timeout_add(self.cfg.restore_clipboard_ms,
                                 self._restore_clipboard, previous)
            except Exception:
                log.debug("could not schedule clipboard restore", exc_info=True)
        return "paste"

    def _type(self, text: str) -> None:
        kb = self.keyboard
        delay = self.cfg.type_delay_ms / 1000.0
        for char in text:
            kb.type(char)
            if delay:
                time.sleep(delay)
