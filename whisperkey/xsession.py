"""Locating the X display.

``DISPLAY`` cannot be trusted. On this machine Xorg is started by GDM with
``-displayfd``, which assigns the display number dynamically -- the server ended
up on ``:1`` while the inherited environment still advertised ``:0``. A daemon
launched from systemd or a stale shell would fail with a confusing "connection
refused" rather than anything that points at the real cause.

So: try the advertised display, then probe the sockets that actually exist.
"""

from __future__ import annotations

import glob
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

X11_SOCKET_DIR = "/tmp/.X11-unix"
#: Where GDM keeps the auth cookie for the user's session.
_XAUTH_CANDIDATES = (
    "{runtime}/gdm/Xauthority",
    "{home}/.Xauthority",
    "{runtime}/.Xauthority",
)


def _can_connect(display: str, xauthority: str | None) -> bool:
    saved = os.environ.get("DISPLAY"), os.environ.get("XAUTHORITY")
    os.environ["DISPLAY"] = display
    if xauthority:
        os.environ["XAUTHORITY"] = xauthority
    try:
        from Xlib import display as xdisplay

        conn = xdisplay.Display(display)
        conn.close()
        return True
    except Exception:
        return False
    finally:
        for key, value in zip(("DISPLAY", "XAUTHORITY"), saved):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def candidate_displays() -> list[str]:
    """Advertised display first, then every socket actually present."""
    found: list[str] = []
    current = os.environ.get("DISPLAY")
    if current:
        found.append(current)
    for sock in sorted(glob.glob(f"{X11_SOCKET_DIR}/X*")):
        name = ":" + Path(sock).name[1:]
        if name not in found:
            found.append(name)
    return found


def candidate_xauthorities() -> list[str | None]:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    home = str(Path.home())
    found: list[str | None] = []
    current = os.environ.get("XAUTHORITY")
    if current:
        found.append(current)
    for template in _XAUTH_CANDIDATES:
        path = template.format(runtime=runtime, home=home)
        if path not in found and Path(path).exists():
            found.append(path)
    found.append(None)  # let Xlib work it out
    return found


def ensure_display() -> tuple[str, str | None]:
    """Point DISPLAY/XAUTHORITY at a server we can actually reach.

    Sets the environment as a side effect and returns what it settled on.
    Raises RuntimeError if nothing is reachable.
    """
    tried: list[str] = []
    for display in candidate_displays():
        for xauthority in candidate_xauthorities():
            if _can_connect(display, xauthority):
                os.environ["DISPLAY"] = display
                if xauthority:
                    os.environ["XAUTHORITY"] = xauthority
                if tried:
                    log.warning("DISPLAY=%s was unreachable; using %s instead",
                                tried[0], display)
                log.info("X display %s (xauthority=%s)", display, xauthority)
                return display, xauthority
            tried.append(f"{display}|{xauthority}")
    raise RuntimeError(
        "no reachable X display (tried: "
        + ", ".join(t.split("|")[0] for t in tried[:6])
        + "). WhisperKey needs an X11 session."
    )


def is_wayland() -> bool:
    return (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or bool(os.environ.get("WAYLAND_DISPLAY")))
