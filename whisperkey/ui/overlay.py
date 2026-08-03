"""The floating status pill shown while dictating.

Deliberately built from ordinary GTK widgets and CSS rather than a custom Cairo
``draw`` handler: this system has pycairo but *not* the gi<->cairo foreign-struct
bridge (python3-gi-cairo), so ``draw`` handlers and ``cairo.Region`` are both
unavailable. Widgets plus a CSS provider get the same look with no such dependency.

Two properties matter more than appearance:

* It must never take focus, or the synthetic paste lands here instead of the
  user's app. A ``POPUP`` window is override-redirect, so the window manager
  never gives it focus at all.
* Clicks should pass through. ``Gdk.Window.set_pass_through`` does this without
  needing ``input_shape_combine_region`` (which requires the missing bridge).
"""

from __future__ import annotations

import logging

from ..gtk3 import Gdk, GLib, Gtk

log = logging.getLogger(__name__)

BAR_COUNT = 14
BAR_MIN_H = 3
BAR_MAX_H = 22
FRAME_MS = 33  # ~30 fps

#: Static per-bar weights so the meter looks like a spectrum rather than a
#: single block moving in lockstep. Centre bars react most.
_WEIGHTS = [0.35, 0.5, 0.68, 0.82, 0.93, 1.0, 0.96, 1.0, 0.9, 0.8, 0.66, 0.52, 0.4, 0.3]

CSS = b"""
window.whisperkey-overlay { background-color: rgba(0,0,0,0); }
box.pill {
    background-color: rgba(24, 24, 28, 0.92);
    border-radius: 20px;
    padding: 10px 18px;
    border: 1px solid rgba(255,255,255,0.10);
}
box.bar { background-color: #ff5f56; border-radius: 2px; }
box.bar.thinking { background-color: #f5c451; }
label.status { color: #f2f2f4; font-size: 12px; font-weight: 600; }
label.hint { color: rgba(242,242,244,0.55); font-size: 11px; }
"""


class Overlay:
    """A small always-on-top pill: live level meter, then a spinner."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.window: Gtk.Window | None = None
        self._bars: list[Gtk.Box] = []
        self._status: Gtk.Label | None = None
        self._hint: Gtk.Label | None = None
        self._spinner: Gtk.Spinner | None = None
        self._meter: Gtk.Box | None = None
        self._timer: int | None = None
        self._level_source = lambda: 0.0
        self._phase = 0
        self._built = False

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        if self._built:
            return
        screen = Gdk.Screen.get_default()
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # POPUP == override-redirect: the WM never focuses it.
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.get_style_context().add_class("whisperkey-overlay")
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_above(True)
        win.set_decorated(False)
        win.set_resizable(False)
        visual = screen.get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)  # real translucency, not a grey box
        win.set_app_paintable(True)

        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        pill.get_style_context().add_class("pill")

        self._spinner = Gtk.Spinner()
        pill.pack_start(self._spinner, False, False, 0)

        meter = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        meter.set_valign(Gtk.Align.CENTER)
        for _ in range(BAR_COUNT):
            bar = Gtk.Box()
            bar.get_style_context().add_class("bar")
            bar.set_size_request(3, BAR_MIN_H)
            bar.set_valign(Gtk.Align.CENTER)
            meter.pack_start(bar, False, False, 0)
            self._bars.append(bar)
        self._meter = meter
        pill.pack_start(meter, False, False, 0)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._status = Gtk.Label(label="Listening")
        self._status.get_style_context().add_class("status")
        self._status.set_xalign(0.0)
        self._hint = Gtk.Label(label="release to transcribe")
        self._hint.get_style_context().add_class("hint")
        self._hint.set_xalign(0.0)
        labels.pack_start(self._status, False, False, 0)
        labels.pack_start(self._hint, False, False, 0)
        pill.pack_start(labels, False, False, 0)

        win.add(pill)
        self.window = win
        self._built = True

    def _make_click_through(self) -> None:
        gdk_window = self.window.get_window() if self.window else None
        if gdk_window is None:
            return
        # Available since GTK 3.18; the cairo.Region route needs the missing bridge.
        if hasattr(gdk_window, "set_pass_through"):
            try:
                gdk_window.set_pass_through(True)
            except Exception:
                log.debug("set_pass_through failed", exc_info=True)

    def _reposition(self) -> None:
        """Bottom-centre of whichever monitor the pointer is on."""
        if self.window is None:
            return
        display = Gdk.Display.get_default()
        try:
            seat = display.get_default_seat()
            pointer = seat.get_pointer()
            screen, px, py = pointer.get_position()
            monitor = display.get_monitor_at_point(px, py)
        except Exception:
            monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        area = monitor.get_workarea()
        width, height = self.window.get_size()
        x = area.x + (area.width - width) // 2
        y = area.y + area.height - height - 90
        self.window.move(x, y)

    # -- public API --------------------------------------------------------

    def set_level_source(self, fn) -> None:
        self._level_source = fn

    def show_recording(self, latched: bool = False) -> None:
        if not self.cfg.overlay:
            return
        self._build()
        self._status.set_text("Listening")
        self._hint.set_text("tap to stop" if latched else "release to transcribe")
        for bar in self._bars:
            bar.get_style_context().remove_class("thinking")
        self._meter.show()
        self._spinner.hide()
        self._spinner.stop()
        self.window.show_all()
        self._spinner.hide()
        self._make_click_through()
        self._reposition()
        if self._timer is None:
            self._timer = GLib.timeout_add(FRAME_MS, self._tick)

    def show_transcribing(self) -> None:
        if not self.cfg.overlay or not self._built:
            return
        self._status.set_text("Transcribing")
        self._hint.set_text("")
        for bar in self._bars:
            bar.get_style_context().add_class("thinking")
        self._meter.hide()
        self._spinner.show()
        self._spinner.start()
        self._reposition()

    def hide(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        if self._spinner is not None:
            self._spinner.stop()
        if self.window is not None:
            self.window.hide()

    def destroy(self) -> None:
        self.hide()
        if self.window is not None:
            self.window.destroy()
            self.window = None
            self._built = False
            self._bars.clear()

    # -- animation ---------------------------------------------------------

    def _tick(self) -> bool:
        try:
            level = max(0.0, min(1.0, float(self._level_source())))
        except Exception:
            level = 0.0
        self._phase += 1
        for index, bar in enumerate(self._bars):
            weight = _WEIGHTS[index % len(_WEIGHTS)]
            # A slow travelling ripple keeps it alive during quiet passages.
            ripple = 1.0 + 0.18 * ((self._phase + index * 2) % 12 - 6) / 6.0
            height = BAR_MIN_H + (BAR_MAX_H - BAR_MIN_H) * level * weight * ripple
            bar.set_size_request(3, int(max(BAR_MIN_H, min(BAR_MAX_H, height))))
        return True
