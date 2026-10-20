"""Input through the kernel, for Wayland sessions.

The portals in :mod:`whisperkey.portal` are the polite way to do this and they
are refused on Ubuntu 26.04 -- see that module for why. This is the impolite way,
and it works everywhere: read key events straight from ``/dev/input/event*``, and
write synthetic ones to ``/dev/uinput``. The kernel does not care what the
compositor thinks, so the same code covers GNOME, KDE, wlroots and X11 alike.

The price is the one thing the installer otherwise avoids: device permissions,
which need a udev rule and a group, which needs root once. ``whisperkey doctor``
prints the two commands when it finds the permissions missing. Nothing here
escalates anything by itself.

Two hazards worth naming, because both are silent:

* **Feedback.** Our own injected keystrokes come back through the readers --
  ``/dev/uinput`` output is indistinguishable from a real keyboard, which is the
  whole point of it. The reader therefore skips any device whose name matches the
  one we create. Without that, pasting Ctrl+V would look like the user pressing
  Ctrl, and a dictation key that happened to be Ctrl would latch on its own paste.
* **Grabbing.** The reader never calls ``EVIOCGRAB``. Grabbing would take the key
  away from everything else on the system, and the dictation key is usually a
  modifier that other things still need. Watching is enough, exactly as it was
  under X11.
"""

from __future__ import annotations

import logging
import os
import selectors
import threading
from typing import Callable

log = logging.getLogger(__name__)

UINPUT_NODE = "/dev/uinput"
#: Name of the virtual keyboard we create. Also the feedback filter.
VIRTUAL_NAME = "WhisperKey virtual keyboard"

#: Config key names -> evdev ``KEY_*`` suffixes. Anything not listed is tried
#: verbatim as ``KEY_<UPPER>``, which covers letters and the function row.
_KEY_ALIASES = {
    "cmd": "LEFTMETA", "cmd_l": "LEFTMETA", "cmd_r": "RIGHTMETA",
    "super": "LEFTMETA", "meta": "LEFTMETA", "win": "LEFTMETA",
    "ctrl": "LEFTCTRL", "ctrl_l": "LEFTCTRL", "ctrl_r": "RIGHTCTRL",
    "control": "LEFTCTRL",
    "alt": "LEFTALT", "alt_l": "LEFTALT", "alt_r": "RIGHTALT",
    "alt_gr": "RIGHTALT",
    "shift": "LEFTSHIFT", "shift_l": "LEFTSHIFT", "shift_r": "RIGHTSHIFT",
    "esc": "ESC", "escape": "ESC", "caps_lock": "CAPSLOCK",
    "scroll_lock": "SCROLLLOCK", "num_lock": "NUMLOCK",
    "menu": "COMPOSE", "space": "SPACE", "enter": "ENTER", "return": "ENTER",
    "tab": "TAB", "backspace": "BACKSPACE", "insert": "INSERT",
    "delete": "DELETE", "pause": "PAUSE",
}


def code_for(name: str) -> int:
    """Translate a config key name into an evdev key code."""
    from evdev import ecodes

    raw = (name or "").strip().lower()
    if not raw:
        raise ValueError("no key given")
    suffix = _KEY_ALIASES.get(raw, raw.upper())
    code = getattr(ecodes, f"KEY_{suffix}", None)
    if code is None:
        raise ValueError(f"no evdev key code for {name!r}")
    return code


def permission_problem() -> str:
    """Why the kernel backend cannot be used, or '' if it can.

    Checked rather than attempted because the failure modes are
    indistinguishable from a missing kernel module at the point of use.
    """
    try:
        import evdev  # noqa: F401
    except Exception as exc:
        return f"the evdev package is not installed ({exc})"
    if not os.path.exists(UINPUT_NODE):
        return f"{UINPUT_NODE} does not exist (try: sudo modprobe uinput)"
    if not os.access(UINPUT_NODE, os.W_OK):
        return f"{UINPUT_NODE} is not writable by this user"
    import evdev

    if not evdev.list_devices():
        return "no readable devices in /dev/input (not in the 'input' group?)"
    return ""


class UinputKeyboard:
    """Synthetic keystrokes via ``/dev/uinput``."""

    def __init__(self) -> None:
        self._ui = None

    @property
    def active(self) -> bool:
        return self._ui is not None

    def start(self) -> None:
        if self._ui is not None:
            return
        from evdev import UInput, ecodes

        # Advertise every key we might ever send, plus the letters, so the
        # device looks like an ordinary keyboard to the compositor.
        keys = sorted({code_for(n) for n in _KEY_ALIASES} |
                      {getattr(ecodes, f"KEY_{c.upper()}")
                       for c in "abcdefghijklmnopqrstuvwxyz"})
        self._ui = UInput({ecodes.EV_KEY: keys}, name=VIRTUAL_NAME)
        log.info("uinput virtual keyboard created")

    def close(self) -> None:
        if self._ui is not None:
            try:
                self._ui.close()
            finally:
                self._ui = None

    def send_combo(self, combo: str) -> None:
        """Press and release a chord such as ``ctrl+shift+v``."""
        from evdev import ecodes

        if self._ui is None:
            self.start()
        parts = [p.strip() for p in combo.split("+") if p.strip()]
        codes = [code_for(p) for p in parts]
        if not codes:
            return
        mods, final = codes[:-1], codes[-1]
        ui = self._ui
        for code in mods:
            ui.write(ecodes.EV_KEY, code, 1)
        ui.write(ecodes.EV_KEY, final, 1)
        ui.write(ecodes.EV_KEY, final, 0)
        for code in reversed(mods):
            ui.write(ecodes.EV_KEY, code, 0)
        ui.syn()


class EvdevHotkey:
    """Watch one key (and optionally an abort key) across every keyboard."""

    def __init__(self, key: str, abort_key: str,
                 on_press: Callable[[], None],
                 on_release: Callable[[], None],
                 on_abort: Callable[[], None]):
        self._code = code_for(key)
        self._abort_code = code_for(abort_key) if abort_key else None
        self._on_press = on_press
        self._on_release = on_release
        self._on_abort = on_abort
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._devices: list = []
        self.watched = 0

    # -- device discovery --------------------------------------------------

    def _keyboards(self) -> list:
        """Every device that looks like a keyboard, minus our own output."""
        import evdev
        from evdev import ecodes

        found = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except Exception:
                continue
            if dev.name == VIRTUAL_NAME:
                dev.close()          # never read back our own injection
                continue
            caps = dev.capabilities()
            keys = caps.get(ecodes.EV_KEY, ())
            # A real keyboard has letters. Power buttons and lids report EV_KEY
            # too, and watching those would just burn a file descriptor.
            if ecodes.KEY_A in keys and self._code in keys:
                found.append(dev)
            else:
                dev.close()
        return found

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        problem = permission_problem()
        if problem:
            raise RuntimeError(problem)
        self._devices = self._keyboards()
        if not self._devices:
            raise RuntimeError("no keyboard exposes that key in /dev/input")
        self.watched = len(self._devices)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="whisperkey-evdev")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass
        self._devices = []

    def _run(self) -> None:
        from evdev import ecodes

        sel = selectors.DefaultSelector()
        for dev in self._devices:
            sel.register(dev, selectors.EVENT_READ)
        try:
            while not self._stop.is_set():
                for key, _mask in sel.select(timeout=0.5):
                    for event in key.fileobj.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        self._dispatch(event)
        except Exception:
            log.exception("evdev reader stopped")
        finally:
            sel.close()

    def _dispatch(self, event) -> None:
        # value 2 is auto-repeat; the state machine wants real transitions only.
        if event.value == 2:
            return
        try:
            if self._abort_code is not None and event.code == self._abort_code:
                if event.value == 1:
                    self._on_abort()
                return
            if event.code != self._code:
                return
            if event.value == 1:
                self._on_press()
            elif event.value == 0:
                self._on_release()
        except Exception:
            log.exception("evdev key handler failed")
