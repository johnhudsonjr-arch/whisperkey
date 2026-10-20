"""XDG desktop portal backends, for Wayland sessions.

GNOME 49 removed the X11 session and Ubuntu 26.04 ships no ``/usr/share/xsessions``
at all, so the X11 assumptions in :mod:`whisperkey.hotkey` and
:mod:`whisperkey.injector` have nothing left to run on. The portals are the
supported replacement and, importantly, they need no root: no ``uinput`` node, no
``input`` group, no udev rule. That keeps the promise the installer makes.

Two portals do the two jobs X11 used to:

* ``org.freedesktop.portal.GlobalShortcuts`` replaces the pynput key listener. It
  emits ``Activated`` *and* ``Deactivated``, which is the whole reason push-to-talk
  survives the move -- a portal that only reported activation would have forced a
  tap-to-toggle UX.
* ``org.freedesktop.portal.RemoteDesktop`` replaces XTEST. Only the paste *chord*
  needs it; the clipboard itself is GTK's and was never X11-specific.

Everything here is driven over D-Bus through GLib, which the daemon already runs a
main loop for. No new dependency enters the venv.

KNOWN BROKEN ON UBUNTU 26.04, AND NOT BY ANYTHING HERE. xdg-desktop-portal 1.20
made non-sandboxed apps claim an identity through
``org.freedesktop.host.portal.Registry.Register`` before using a portal, and 1.21
turned a missing app id into a hard refusal -- GlobalShortcuts answers
``NotAllowed: An app id is required`` and RemoteDesktop ends the request with
code 2. Ubuntu ships 1.21.1 with the Registry *compiled in* but exported on no
reachable bus name: the symbols are in the binary, yet
``/org/freedesktop/host/portal/registry`` does not exist on any service. So there
is no way for a host app to claim an app id, and therefore no way past the gate.

This module is kept because nothing in it is wrong and the gate is the
distribution's, not ours: it starts working the moment the Registry is exported,
or immediately if WhisperKey is ever packaged as a Flatpak (which gets an app id
for free). Until then the daemon falls back -- see ``Daemon._make_listener``.

The awkward part of the portal protocol is that nothing returns its answer
directly. A call returns a :class:`Request` object path and the real result
arrives later as a ``Response`` signal on it. :func:`_await_request` hides that,
subscribing *before* it issues the call -- the reply can land first otherwise, and
the signal would be missed with no error anywhere.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Callable

from .gtk3 import Gio, GLib

log = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
GLOBAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"

#: ``NotifyKeyboardKeycode`` speaks evdev, not X11. These are from
#: ``linux/input-event-codes.h``; only what a paste chord can name is listed.
EVDEV_KEYS = {
    "ctrl": 29, "control": 29, "leftctrl": 29,
    "shift": 42, "leftshift": 42,
    "alt": 56, "leftalt": 56,
    "super": 125, "cmd": 125, "meta": 125, "leftmeta": 125,
    "insert": 110, "delete": 111, "space": 57, "enter": 28, "return": 28,
    "tab": 15, "escape": 1, "esc": 1, "backspace": 14,
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
}

#: ``RemoteDesktop.SelectDevices`` device-type bitmask.
DEVICE_KEYBOARD = 1
#: ``persist_mode``: 2 means "persist until explicitly revoked", which is what
#: buys a restore token and therefore a single consent prompt ever.
PERSIST_UNTIL_REVOKED = 2

KEY_RELEASED, KEY_PRESSED = 0, 1


class PortalError(RuntimeError):
    """A portal call failed, was cancelled, or timed out."""


def _token() -> str:
    return "whisperkey_" + secrets.token_hex(8)


def _unique_name(bus: Gio.DBusConnection) -> str:
    """Our bus name with the punctuation the Request path spec strips out."""
    name = bus.get_unique_name() or ""
    return name.lstrip(":").replace(".", "_")


def _await_request(bus, iface, method, body, *, timeout_ms=120_000):
    """Issue a portal call and block on its ``Response`` signal.

    Returns the results dict. ``timeout_ms`` is generous because two of these
    calls put a consent dialog in front of a human.
    """
    handle_token = _token()
    expected = f"{PORTAL_PATH}/request/{_unique_name(bus)}/{handle_token}"

    loop = GLib.MainLoop()
    captured: dict = {}

    def on_response(_conn, _sender, _path, _iface, _signal, params):
        code, results = params.unpack()
        captured["code"] = code
        captured["results"] = results
        if loop.is_running():
            loop.quit()

    # Subscribe before calling: the Response can beat the method reply back.
    sub = bus.signal_subscribe(PORTAL_BUS, REQUEST_IFACE, "Response", expected,
                               None, Gio.DBusSignalFlags.NONE, on_response)
    timed_out = {"hit": False}

    def on_timeout():
        timed_out["hit"] = True
        if loop.is_running():
            loop.quit()
        return False

    timer = GLib.timeout_add(timeout_ms, on_timeout)
    try:
        options = dict(body[-1])
        options["handle_token"] = GLib.Variant("s", handle_token)
        args = GLib.Variant(_signature_for(method), tuple(body[:-1]) + (options,))
        reply = bus.call_sync(PORTAL_BUS, PORTAL_PATH, iface, method, args, None,
                              Gio.DBusCallFlags.NONE, 30_000, None)
        actual = reply.unpack()[0]
        if actual != expected:
            # Older portals hand back a path that does not follow the predictable
            # scheme. Re-subscribe on what we were actually given.
            bus.signal_unsubscribe(sub)
            sub = bus.signal_subscribe(PORTAL_BUS, REQUEST_IFACE, "Response",
                                       actual, None, Gio.DBusSignalFlags.NONE,
                                       on_response)
        loop.run()
    finally:
        GLib.source_remove(timer)
        bus.signal_unsubscribe(sub)

    if timed_out["hit"]:
        raise PortalError(f"{method} timed out after {timeout_ms} ms")
    code = captured.get("code")
    if code != 0:
        why = {1: "cancelled by the user", 2: "ended by the portal"}.get(code, f"code {code}")
        raise PortalError(f"{method} failed: {why}")
    return captured.get("results") or {}


def _signature_for(method: str) -> str:
    """D-Bus input signature per portal method (options dict always last)."""
    return {
        "CreateSession": "(a{sv})",
        "SelectDevices": "(oa{sv})",
        "Start": "(osa{sv})",
        "BindShortcuts": "(oa(sa{sv})sa{sv})",
    }[method]


class RemoteDesktopKeyboard:
    """Synthetic keystrokes under Wayland, via ``RemoteDesktop``.

    Only the paste chord goes through here. Typing a whole transcription one
    ``NotifyKeyboardKeycode`` at a time would be a round trip per character, so
    the clipboard remains the carrier and this just fires Ctrl+V.

    ``Start`` shows a consent dialog. ``persist_mode`` buys a restore token which
    is cached on disk, so that dialog is answered once rather than every login.
    """

    def __init__(self, token_path: Path | None = None):
        self._bus = None
        self._session = None
        self._token_path = token_path or (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "whisperkey" / "remote-desktop.token")

    # -- restore token -----------------------------------------------------

    def _load_token(self) -> str:
        try:
            return self._token_path.read_text().strip()
        except Exception:
            return ""

    def _save_token(self, token: str) -> None:
        if not token:
            return
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(token)
            self._token_path.chmod(0o600)
        except Exception:
            log.debug("could not cache the RemoteDesktop restore token", exc_info=True)

    # -- lifecycle ---------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._session is not None

    def start(self) -> None:
        if self._session is not None:
            return
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        results = _await_request(self._bus, REMOTE_DESKTOP, "CreateSession",
                                 ({"session_handle_token": GLib.Variant("s", _token())},))
        session = results.get("session_handle")
        if not session:
            raise PortalError("RemoteDesktop.CreateSession returned no session handle")

        select: dict = {
            "types": GLib.Variant("u", DEVICE_KEYBOARD),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        cached = self._load_token()
        if cached:
            select["restore_token"] = GLib.Variant("s", cached)
        _await_request(self._bus, REMOTE_DESKTOP, "SelectDevices", (session, select))

        started = _await_request(self._bus, REMOTE_DESKTOP, "Start", (session, "", {}))
        self._save_token(started.get("restore_token", ""))
        self._session = session
        log.info("RemoteDesktop session started%s",
                 " (restored, no prompt)" if cached else " (consent granted)")

    def close(self) -> None:
        if self._session is None:
            return
        try:
            self._bus.call_sync(PORTAL_BUS, self._session, SESSION_IFACE, "Close",
                                None, None, Gio.DBusCallFlags.NONE, 5_000, None)
        except Exception:
            log.debug("closing the RemoteDesktop session failed", exc_info=True)
        finally:
            self._session = None

    # -- keystrokes --------------------------------------------------------

    def _notify(self, keycode: int, state: int) -> None:
        if self._session is None:
            raise PortalError("RemoteDesktop session is not running")
        self._bus.call_sync(
            PORTAL_BUS, PORTAL_PATH, REMOTE_DESKTOP, "NotifyKeyboardKeycode",
            GLib.Variant("(oa{sv}iu)", (self._session, {}, keycode, state)),
            None, Gio.DBusCallFlags.NONE, 5_000, None)

    def send_combo(self, combo: str) -> None:
        """Press and release a chord such as ``ctrl+shift+v``."""
        parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
        try:
            codes = [EVDEV_KEYS[p] for p in parts]
        except KeyError as exc:
            raise PortalError(f"no evdev keycode for {exc.args[0]!r} in {combo!r}") from exc
        if not codes:
            return
        mods, final = codes[:-1], codes[-1]
        for code in mods:
            self._notify(code, KEY_PRESSED)
        try:
            self._notify(final, KEY_PRESSED)
            self._notify(final, KEY_RELEASED)
        finally:
            for code in reversed(mods):
                self._notify(code, KEY_RELEASED)


class GlobalShortcutsHotkey:
    """Push-to-talk under Wayland, via ``GlobalShortcuts``.

    ``Activated`` and ``Deactivated`` map exactly onto the press and release that
    :class:`whisperkey.hotkey.HotkeyMachine` already understands, so the state
    machine is reused untouched and stays unit-testable without a compositor.

    The binding itself belongs to the user, not to us: ``preferred_trigger`` is a
    request, and GNOME may hand back a different key. Whatever it settles on is
    read back from ``ListShortcuts`` and logged, because silently listening for a
    key the user was never told about is indistinguishable from being broken.
    """

    SHORTCUT_ID = "dictate"

    def __init__(self, trigger: str, description: str,
                 on_press: Callable[[], None], on_release: Callable[[], None]):
        self.trigger = trigger
        self.description = description
        self._on_press = on_press
        self._on_release = on_release
        self._bus = None
        self._session = None
        self._subs: list[int] = []
        self.bound_trigger = ""

    @property
    def active(self) -> bool:
        return self._session is not None

    def start(self) -> None:
        if self._session is not None:
            return
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        results = _await_request(self._bus, GLOBAL_SHORTCUTS, "CreateSession",
                                 ({"session_handle_token": GLib.Variant("s", _token())},))
        session = results.get("session_handle")
        if not session:
            raise PortalError("GlobalShortcuts.CreateSession returned no session handle")
        self._session = session

        for signal, handler in (("Activated", self._activated),
                                ("Deactivated", self._deactivated)):
            self._subs.append(self._bus.signal_subscribe(
                PORTAL_BUS, GLOBAL_SHORTCUTS, signal, PORTAL_PATH, None,
                Gio.DBusSignalFlags.NONE, handler))

        shortcut = (self.SHORTCUT_ID, {
            "description": GLib.Variant("s", self.description),
            "preferred_trigger": GLib.Variant("s", self.trigger),
        })
        bound = _await_request(self._bus, GLOBAL_SHORTCUTS, "BindShortcuts",
                               (session, [shortcut], "", {}))
        self._remember_trigger(bound.get("shortcuts"))
        log.info("global shortcut bound to %s", self.bound_trigger or "<unreported>")

    def _remember_trigger(self, shortcuts) -> None:
        for entry in shortcuts or []:
            try:
                ident, meta = entry
            except Exception:
                continue
            if ident == self.SHORTCUT_ID:
                self.bound_trigger = str(meta.get("trigger_description", "") or "")

    def close(self) -> None:
        for sub in self._subs:
            try:
                self._bus.signal_unsubscribe(sub)
            except Exception:
                pass
        self._subs.clear()
        if self._session is not None:
            try:
                self._bus.call_sync(PORTAL_BUS, self._session, SESSION_IFACE, "Close",
                                    None, None, Gio.DBusCallFlags.NONE, 5_000, None)
            except Exception:
                log.debug("closing the GlobalShortcuts session failed", exc_info=True)
            self._session = None

    # -- signal handlers ---------------------------------------------------

    def _matches(self, params) -> bool:
        try:
            session, shortcut_id = params.unpack()[:2]
        except Exception:
            return False
        return session == self._session and shortcut_id == self.SHORTCUT_ID

    def _activated(self, _c, _s, _p, _i, _sig, params) -> None:
        if self._matches(params):
            try:
                self._on_press()
            except Exception:
                log.exception("shortcut press handler failed")

    def _deactivated(self, _c, _s, _p, _i, _sig, params) -> None:
        if self._matches(params):
            try:
                self._on_release()
            except Exception:
                log.exception("shortcut release handler failed")
