"""Tray icon and desktop notifications.

The tray indicator needs ``gir1.2-ayatanaappindicator3-0.1``, which is the one
piece of WhisperKey that cannot be installed without root. It is therefore
entirely optional: when the typelib is absent the indicator degrades to a no-op
and status is reported through notifications instead (libnotify *is* present).

If the package is installed later the tray lights up on next restart with no
config change.
"""

from __future__ import annotations

import logging

from ..gtk3 import gi  # pins GTK 3 before any gi.repository import

log = logging.getLogger(__name__)

APP_ID = "whisperkey"

_ICONS = {
    "idle": "audio-input-microphone-symbolic",
    "recording": "media-record-symbolic",
    "transcribing": "content-loading-symbolic",
    "error": "dialog-error-symbolic",
}


def _load_indicator():
    """Return the AppIndicator module, or None if unavailable."""
    for name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(name, "0.1")
            module = __import__("gi.repository", fromlist=[name])
            return getattr(module, name)
        except (ValueError, ImportError, AttributeError):
            continue
    return None


class Notifier:
    """Desktop notifications, used for errors and for status without a tray."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._notify = None
        if not enabled:
            return
        try:
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify

            Notify.init("WhisperKey")
            self._notify = Notify
        except Exception:
            log.debug("libnotify unavailable", exc_info=True)

    def send(self, summary: str, body: str = "", icon: str = "", urgency: str = "normal") -> None:
        if not self.enabled or self._notify is None:
            return
        try:
            note = self._notify.Notification.new(
                summary, body, icon or _ICONS["idle"]
            )
            levels = {
                "low": self._notify.Urgency.LOW,
                "normal": self._notify.Urgency.NORMAL,
                "critical": self._notify.Urgency.CRITICAL,
            }
            note.set_urgency(levels.get(urgency, self._notify.Urgency.NORMAL))
            note.show()
        except Exception:
            log.debug("notification failed", exc_info=True)


class Tray:
    """AppIndicator wrapper that quietly does nothing when unsupported."""

    def __init__(self, cfg, actions: dict):
        """``actions`` maps menu labels to callables; see daemon.py."""
        self.cfg = cfg
        self.actions = actions
        self.indicator = None
        self._items: dict[str, object] = {}
        self.available = False
        if not cfg.tray:
            return
        api = _load_indicator()
        if api is None:
            log.info("tray unavailable (install gir1.2-ayatanaappindicator3-0.1 "
                     "for a tray icon); using notifications instead")
            return
        try:
            self._build(api)
            self.available = True
        except Exception:
            log.exception("could not create tray icon")

    def _build(self, api) -> None:
        from ..gtk3 import Gtk

        self.indicator = api.Indicator.new(
            APP_ID, _ICONS["idle"], api.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(api.IndicatorStatus.ACTIVE)
        self._api = api

        menu = Gtk.Menu()

        self._items["status"] = Gtk.MenuItem(label="Ready")
        self._items["status"].set_sensitive(False)
        menu.append(self._items["status"])
        menu.append(Gtk.SeparatorMenuItem())

        toggle = Gtk.CheckMenuItem(label="Dictation enabled")
        toggle.set_active(True)
        toggle.connect("toggled", lambda w: self.actions["set_enabled"](w.get_active()))
        self._items["enabled"] = toggle
        menu.append(toggle)

        cleanup = Gtk.CheckMenuItem(label="Clean up with local LLM")
        cleanup.set_active(bool(self.actions.get("cleanup_enabled", lambda: False)()))
        cleanup.connect("toggled", lambda w: self.actions["set_cleanup"](w.get_active()))
        self._items["cleanup"] = cleanup
        menu.append(cleanup)

        menu.append(Gtk.SeparatorMenuItem())
        for label, key in (("Re-paste last", "repaste"),
                           ("Copy last to clipboard", "copy_last"),
                           ("Edit config…", "edit_config"),
                           ("Show history…", "show_history")):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _w, k=key: self.actions[k]())
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit WhisperKey")
        quit_item.connect("activate", lambda _w: self.actions["quit"]())
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)

    # -- updates -----------------------------------------------------------

    def set_state(self, state: str, detail: str = "") -> None:
        if self.indicator is None:
            return
        try:
            self.indicator.set_icon_full(_ICONS.get(state, _ICONS["idle"]), state)
            item = self._items.get("status")
            if item is not None:
                item.set_label(detail or state.capitalize())
        except Exception:
            log.debug("tray update failed", exc_info=True)

    def set_enabled(self, value: bool) -> None:
        item = self._items.get("enabled")
        if item is not None and item.get_active() != value:
            item.set_active(value)
