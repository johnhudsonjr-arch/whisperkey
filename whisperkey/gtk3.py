"""Single, ordered entry point for GTK 3.

PyGObject resolves a typelib version the first time ``gi.repository.X`` is
imported. This system has both GTK 3 and GTK 4 typelibs installed, so an import
without an explicit ``require_version`` silently binds GTK 4 -- and then fails
later on GTK-3-only API such as ``Gdk.Screen``.

Every module in WhisperKey imports GTK *through here* so the version is pinned
before anything can bind the wrong one::

    from .gtk3 import Gdk, GLib, Gtk
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

__all__ = ["Gdk", "GdkPixbuf", "Gio", "GLib", "GObject", "Gtk", "gi"]
