"""Global hotkey handling.

The dictation key is watched, never *grabbed*. pynput's X11 suppression is
all-or-nothing, so grabbing Right Ctrl would break it for normal use. Watching
is enough because a bare modifier press does nothing on its own.

The press/release semantics live in :class:`HotkeyMachine`, which is pure and
clock-injectable so the hold-vs-double-tap logic can be unit tested without an
X server.
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)


class Action(enum.Enum):
    NONE = "none"
    START = "start"      # begin capturing
    STOP = "stop"        # finish and transcribe
    DISCARD = "discard"  # drop whatever was captured


class State(enum.Enum):
    IDLE = "idle"
    HOLD = "hold"              # key down, push-to-talk in progress
    TAP_PENDING = "tap"        # a short tap landed; maybe a double-tap is coming
    LATCHED = "latched"        # hands-free, recording until the next press


class HotkeyMachine:
    """Turns raw key press/release events into recording actions.

    Recording starts on the *press* rather than after a debounce delay, so no
    speech is lost at the start. A press that turns out to be a short tap simply
    has its audio discarded, which costs nothing.

    Flow::

        press ................. START      (push-to-talk begins)
        release after >=hold_min  STOP     (transcribe)

        press/release quickly ... DISCARD  (a tap; audio thrown away)
        press again in-window ... START    (latched; hands-free)
        press once more ........ STOP      (transcribe)
    """

    def __init__(self, hold_min_ms: int = 250, double_tap_ms: int = 400,
                 clock: Callable[[], float] = time.monotonic):
        self.hold_min = hold_min_ms / 1000.0
        self.double_tap = double_tap_ms / 1000.0
        self._clock = clock
        self.state = State.IDLE
        self._pressed_at = 0.0
        self._tap_at = 0.0

    @property
    def recording(self) -> bool:
        return self.state in (State.HOLD, State.LATCHED)

    def on_press(self) -> Action:
        now = self._clock()
        if self.state is State.LATCHED:
            self.state = State.IDLE
            return Action.STOP
        if self.state is State.TAP_PENDING:
            if now - self._tap_at <= self.double_tap:
                self.state = State.LATCHED
                self._pressed_at = now
                return Action.START
            # Window expired -- treat as an ordinary new press.
        if self.state is State.HOLD:
            return Action.NONE  # defensive; auto-repeat is filtered upstream
        self.state = State.HOLD
        self._pressed_at = now
        return Action.START

    def on_release(self) -> Action:
        now = self._clock()
        if self.state is not State.HOLD:
            # Releases while latched (or idle) are irrelevant.
            return Action.NONE
        if now - self._pressed_at >= self.hold_min:
            self.state = State.IDLE
            return Action.STOP
        self.state = State.TAP_PENDING
        self._tap_at = now
        return Action.DISCARD

    def on_abort(self) -> Action:
        if self.recording:
            self.state = State.IDLE
            return Action.DISCARD
        return Action.NONE

    def reset(self) -> Action:
        was = self.recording
        self.state = State.IDLE
        return Action.DISCARD if was else Action.NONE


def resolve_key(name: str):
    """Map a config key name onto a pynput key object or character."""
    from pynput import keyboard

    name = (name or "").strip().lower()
    special = getattr(keyboard.Key, name, None)
    if special is not None:
        return special
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    raise ValueError(f"unknown hotkey {name!r}")


def _same_key(event_key, target) -> bool:
    from pynput import keyboard

    if event_key == target:
        return True
    # Some layouts deliver KeyCode instances for modifiers; compare vk when we can.
    vk_a = getattr(event_key, "vk", None) or getattr(event_key, "value", None)
    vk_b = getattr(target, "vk", None) or getattr(target, "value", None)
    if isinstance(vk_a, keyboard.KeyCode):
        vk_a = vk_a.vk
    if isinstance(vk_b, keyboard.KeyCode):
        vk_b = vk_b.vk
    return vk_a is not None and vk_a == vk_b


class HotkeyListener:
    """Drives a :class:`HotkeyMachine` from real X11 key events."""

    def __init__(self, cfg, on_action: Callable[[Action], None]):
        self.cfg = cfg
        self._on_action = on_action
        self.machine = HotkeyMachine(cfg.hold_min_ms, cfg.double_tap_ms)
        self._key = resolve_key(cfg.key)
        self._abort = resolve_key(cfg.abort_key) if cfg.abort_key else None
        self._down = False  # filters X11 auto-repeat
        self._listener = None
        self.enabled = cfg.enabled

    def start(self) -> None:
        from pynput import keyboard

        self._listener = keyboard.Listener(
            on_press=self._handle_press,
            on_release=self._handle_release,
            name="whisperkey-hotkey",
        )
        self._listener.start()
        log.info("listening for %s (hold to dictate, double-tap to latch)",
                 self.cfg.key)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _emit(self, action: Action) -> None:
        if action is not Action.NONE:
            self._on_action(action)

    def _handle_press(self, key) -> None:
        try:
            if self._abort is not None and _same_key(key, self._abort):
                if self.machine.recording:
                    self._emit(self.machine.on_abort())
                return
            if not _same_key(key, self._key):
                return
            if not self.enabled:
                return
            if self._down:
                return  # auto-repeat
            self._down = True
            self._emit(self.machine.on_press())
        except Exception:  # never let a callback kill the listener thread
            log.exception("hotkey press handler failed")

    def _handle_release(self, key) -> None:
        try:
            if not _same_key(key, self._key):
                return
            self._down = False
            if not self.enabled:
                return
            self._emit(self.machine.on_release())
        except Exception:
            log.exception("hotkey release handler failed")


#: pynput key names -> X keysym names, which is what a GTK accelerator (and
#: therefore the GlobalShortcuts portal's ``preferred_trigger``) is written in.
#: Only keys worth binding as a dictation trigger are listed; anything absent
#: falls through to a capitalised guess, which covers F-keys and plain letters.
_ACCELERATOR_NAMES = {
    "cmd": "Super_L", "cmd_l": "Super_L", "cmd_r": "Super_R",
    "ctrl": "Control_L", "ctrl_l": "Control_L", "ctrl_r": "Control_R",
    "alt": "Alt_L", "alt_l": "Alt_L", "alt_r": "Alt_R", "alt_gr": "ISO_Level3_Shift",
    "shift": "Shift_L", "shift_l": "Shift_L", "shift_r": "Shift_R",
    "caps_lock": "Caps_Lock", "scroll_lock": "Scroll_Lock", "num_lock": "Num_Lock",
    "menu": "Menu", "pause": "Pause", "esc": "Escape", "space": "space",
}


def accelerator_for(name: str) -> str:
    """Translate a config key name into a GTK accelerator string."""
    name = (name or "").strip().lower()
    if not name:
        raise ValueError("no hotkey configured")
    mapped = _ACCELERATOR_NAMES.get(name)
    if mapped:
        return mapped
    if len(name) == 1:
        return name
    if name.startswith("f") and name[1:].isdigit():
        return name.upper()
    # Last resort: Keysym_Case is the usual spelling for multi-word keys.
    return "_".join(part.capitalize() for part in name.split("_"))


class PortalHotkeyListener:
    """Drives a :class:`HotkeyMachine` from GlobalShortcuts portal signals.

    Deliberately the same shape as :class:`HotkeyListener` -- ``start``, ``stop``,
    ``enabled``, ``machine`` -- so the daemon selects a backend once and the rest
    of it never learns which session type it is running on.

    Two things X11 gave us for free are gone here and both are fine. Auto-repeat
    filtering is unnecessary because the portal reports one Activated per physical
    press. The abort key cannot be watched at all: a portal shortcut has to be
    bound and consented to individually, and silently claiming a second global key
    would be worse than losing it. Escape-to-abort therefore only works on X11,
    and the daemon says so at startup rather than leaving it to be discovered.
    """

    def __init__(self, cfg, on_action: Callable[[Action], None]):
        from .portal import GlobalShortcutsHotkey

        self.cfg = cfg
        self._on_action = on_action
        self.machine = HotkeyMachine(cfg.hold_min_ms, cfg.double_tap_ms)
        self.enabled = cfg.enabled
        self._shortcut = GlobalShortcutsHotkey(
            trigger=accelerator_for(cfg.key),
            description="Hold to dictate with WhisperKey",
            on_press=self._handle_press,
            on_release=self._handle_release,
        )

    @property
    def bound_trigger(self) -> str:
        return self._shortcut.bound_trigger

    def start(self) -> None:
        self._shortcut.start()
        bound = self._shortcut.bound_trigger or accelerator_for(self.cfg.key)
        log.info("listening for %s via the GlobalShortcuts portal "
                 "(hold to dictate, double-tap to latch)", bound)

    def stop(self) -> None:
        self._shortcut.close()

    def _emit(self, action: Action) -> None:
        if action is not Action.NONE:
            self._on_action(action)

    def _handle_press(self) -> None:
        if self.enabled:
            self._emit(self.machine.on_press())

    def _handle_release(self) -> None:
        if self.enabled:
            self._emit(self.machine.on_release())


class EvdevHotkeyListener:
    """Drives a :class:`HotkeyMachine` from raw kernel key events.

    Same shape as the other two listeners. Unlike the portal backend this one
    keeps the abort key, because reading ``/dev/input`` sees every key rather
    than only the ones a compositor agreed to hand over.
    """

    def __init__(self, cfg, on_action: Callable[[Action], None]):
        from .evdev_input import EvdevHotkey

        self.cfg = cfg
        self._on_action = on_action
        self.machine = HotkeyMachine(cfg.hold_min_ms, cfg.double_tap_ms)
        self.enabled = cfg.enabled
        self._reader = EvdevHotkey(
            key=cfg.key,
            abort_key=cfg.abort_key,
            on_press=self._handle_press,
            on_release=self._handle_release,
            on_abort=self._handle_abort,
        )

    def start(self) -> None:
        self._reader.start()
        log.info("listening for %s on %d keyboard(s) via evdev "
                 "(hold to dictate, double-tap to latch)",
                 self.cfg.key, self._reader.watched)

    def stop(self) -> None:
        self._reader.stop()

    def _emit(self, action: Action) -> None:
        if action is not Action.NONE:
            self._on_action(action)

    def _handle_press(self) -> None:
        if self.enabled:
            self._emit(self.machine.on_press())

    def _handle_release(self) -> None:
        if self.enabled:
            self._emit(self.machine.on_release())

    def _handle_abort(self) -> None:
        if self.machine.recording:
            self._emit(self.machine.on_abort())
