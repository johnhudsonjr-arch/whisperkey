"""End-to-end check of the hotkey path, with no human fingers involved.

Synthesises real X key events with XTEST and asserts the listener turns them
into the right recording actions. This covers the part unit tests cannot: that
the configured key name actually corresponds to the key the keyboard sends.

    .venv/bin/python tools/selftest_hotkey.py [keyname]
"""

from __future__ import annotations

import sys
import time

from Xlib import X, XK, display
from Xlib.ext import xtest

from whisperkey.config import load
from whisperkey.hotkey import Action, HotkeyListener
from whisperkey.xsession import ensure_display

#: config key name -> the X keysym the keyboard actually emits
KEYSYMS = {
    "cmd_l": "Super_L", "cmd_r": "Super_R", "cmd": "Super_L",
    "ctrl_l": "Control_L", "ctrl_r": "Control_R",
    "alt_l": "Alt_L", "alt_r": "Alt_R",
    "caps_lock": "Caps_Lock", "f9": "F9",
}


def keycode_for(disp, keysym_name: str) -> int:
    keysym = XK.string_to_keysym(keysym_name)
    if not keysym:
        raise SystemExit(f"unknown keysym {keysym_name!r}")
    code = disp.keysym_to_keycode(keysym)
    if not code:
        raise SystemExit(f"{keysym_name} is not on this keyboard")
    return code


def main() -> int:
    ensure_display()
    cfg = load()
    key_name = sys.argv[1] if len(sys.argv) > 1 else cfg.hotkey.key
    keysym_name = KEYSYMS.get(key_name)
    if keysym_name is None:
        raise SystemExit(f"no keysym mapping known for {key_name!r}")

    disp = display.Display()
    code = keycode_for(disp, keysym_name)
    print(f"testing key={key_name!r} ({keysym_name}, keycode {code})\n")

    events: list[Action] = []
    cfg.hotkey.key = key_name
    listener = HotkeyListener(cfg.hotkey, events.append)
    listener.start()
    time.sleep(0.6)  # let the X grab settle

    def press(hold_s: float) -> None:
        xtest.fake_input(disp, X.KeyPress, code)
        disp.sync()
        time.sleep(hold_s)
        xtest.fake_input(disp, X.KeyRelease, code)
        disp.sync()
        time.sleep(0.25)

    print("1. hold for 800ms  (expect START then STOP)")
    press(0.8)
    hold_result = list(events)
    events.clear()

    print("2. quick tap       (expect START then DISCARD)")
    press(0.06)
    tap_result = list(events)
    events.clear()

    print("3. second tap      (expect START -> latched)")
    press(0.06)
    latch_result = list(events)
    events.clear()

    print("4. tap to stop     (expect STOP)")
    press(0.06)
    stop_result = list(events)

    listener.stop()

    failures = []

    def check(label, got, want):
        if got == want:
            print(f"  \033[32m✓\033[0m {label}: {[a.value for a in got]}")
        else:
            failures.append(label)
            print(f"  \033[31m✗\033[0m {label}: got {[a.value for a in got]}, "
                  f"expected {[a.value for a in want]}")

    print()
    check("hold-to-talk", hold_result, [Action.START, Action.STOP])
    check("short tap discarded", tap_result, [Action.START, Action.DISCARD])
    check("double-tap latches", latch_result, [Action.START])
    check("tap stops latched", stop_result, [Action.STOP])

    print()
    if failures:
        print(f"\033[31mFAIL\033[0m: {', '.join(failures)}")
        return 1
    print("\033[32mPASS\033[0m: the key produces the right actions end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
