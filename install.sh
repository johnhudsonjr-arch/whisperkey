#!/usr/bin/env bash
#
# WhisperKey installer.
#
# Deliberately needs no root. The only component that would require apt is the
# tray icon (gir1.2-ayatanaappindicator3-0.1); without it WhisperKey reports
# status through desktop notifications instead. Everything else comes from pip
# wheels or tools already present on a stock Ubuntu desktop.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
PY="$VENV/bin/python"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="$SERVICE_DIR/whisperkey.service"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

bold "WhisperKey installer"
echo

# --- 1. sanity ------------------------------------------------------------
bold "Checking the session"
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
  warn "This looks like a Wayland session."
  warn "WhisperKey needs X11: log out and pick 'Ubuntu on Xorg' at the login screen."
fi
command -v python3 >/dev/null || die "python3 not found"
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ok "python $PYVER"
if command -v pw-record >/dev/null; then ok "pw-record (PipeWire capture)"
elif command -v arecord >/dev/null; then ok "arecord (ALSA capture fallback)"
else die "no audio capture tool found (expected pw-record or arecord)"; fi

# --- 2. virtualenv --------------------------------------------------------
echo
bold "Creating the virtual environment"
if [ ! -x "$PY" ]; then
  # --system-site-packages so the distro's PyGObject (GTK) is importable; there
  # is no usable GTK binding on PyPI.
  if ! python3 -m venv --system-site-packages "$VENV" 2>/dev/null; then
    warn "python3-venv is incomplete; creating the venv without pip"
    python3 -m venv --system-site-packages --without-pip "$VENV" \
      || die "could not create a virtualenv"
  fi
fi
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  # Ubuntu ships python3-venv separately and it may be absent. Bootstrapping
  # pip directly avoids needing apt (and therefore root).
  warn "bootstrapping pip (python3-venv not installed)"
  TMP_GETPIP=$(mktemp /tmp/get-pip-XXXXXX.py)
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$TMP_GETPIP" \
    || die "could not download get-pip.py"
  "$PY" "$TMP_GETPIP" >/dev/null || die "could not bootstrap pip"
  rm -f "$TMP_GETPIP"
fi
ok "pip $("$PY" -m pip --version | awk '{print $2}')"

# --- 3. dependencies ------------------------------------------------------
echo
bold "Installing Python dependencies (this pulls ~1.5 GB of CUDA libraries)"
"$PY" -m pip install --quiet --upgrade pip >/dev/null
"$PY" -m pip install --quiet \
  faster-whisper numpy python-xlib six \
  nvidia-cublas-cu12 'nvidia-cudnn-cu12>=9,<10' || die "dependency install failed"
# pynput's declared Linux dependency 'evdev' is a C extension with no wheel, so
# it would need python3-dev (i.e. root). Its X11 backend only needs python-xlib,
# which we installed above, so --no-deps keeps this rootless.
"$PY" -m pip install --quiet --no-deps pynput || die "could not install pynput"
"$PY" -m pip install --quiet -e "$HERE" >/dev/null 2>&1 || true
ok "dependencies installed"

"$PY" - <<'PYEOF' || die "GTK bindings are not importable from the venv"
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: F401
PYEOF
ok "GTK 3 bindings reachable"

# --- 4. CUDA library path -------------------------------------------------
LDPATH=$("$PY" - <<'PYEOF'
import os, sys
from pathlib import Path
paths = []
for root in sys.path:
    nvidia = Path(root) / "nvidia"
    if nvidia.is_dir():
        for lib in sorted(nvidia.glob("*/lib")):
            if any(lib.glob("*.so*")):
                paths.append(str(lib))
print(os.pathsep.join(paths))
PYEOF
)
[ -n "$LDPATH" ] && ok "CUDA libraries located" || warn "no CUDA libraries found; will run on CPU"

# --- 5. config ------------------------------------------------------------
echo
bold "Configuration"
"$PY" -m whisperkey config --init >/dev/null 2>&1 || true
ok "config at ${XDG_CONFIG_HOME:-$HOME/.config}/whisperkey/config.toml"

# --- 5b. free the dictation key ------------------------------------------
# GNOME opens the Activities overview when Super_L is tapped on its own, which
# would fire every time the default hotkey (Left Command) is used. Moving the
# overview to Super_R frees it. Only done when the configured key needs it.
HOTKEY=$("$PY" -c "from whisperkey.config import load; print(load().hotkey.key)" 2>/dev/null || echo "")
if [ "$HOTKEY" = "cmd_l" ] && command -v gsettings >/dev/null; then
  CURRENT=$(gsettings get org.gnome.mutter overlay-key 2>/dev/null || echo "")
  if [ "$CURRENT" = "'Super_L'" ]; then
    gsettings set org.gnome.mutter overlay-key 'Super_R'
    ok "moved GNOME's Activities key to Super_R so Left Command is free"
    echo "      (undo: gsettings reset org.gnome.mutter overlay-key)"
  else
    ok "GNOME Activities key is $CURRENT; Left Command already free"
  fi
fi

# --- 6. model -------------------------------------------------------------
echo
bold "Fetching the speech model (about 1.6 GB, once)"
LD_LIBRARY_PATH="$LDPATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$PY" - <<'PYEOF' || warn "model download failed; it will retry on first run"
from whisperkey.config import load
from faster_whisper import WhisperModel
cfg = load()
WhisperModel(cfg.model.name, device="cpu", compute_type="int8")
print("  model cached")
PYEOF

# --- 7. systemd user service ---------------------------------------------
echo
bold "Installing the user service"
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE" <<EOF
[Unit]
Description=WhisperKey push-to-talk dictation
Documentation=file://$HERE/README.md
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$PY -m whisperkey daemon
Restart=on-failure
RestartSec=3
# The dynamic loader reads LD_LIBRARY_PATH at process start, so cuDNN and cuBLAS
# must be on it before python launches.
Environment=LD_LIBRARY_PATH=$LDPATH
Environment=PYTHONUNBUFFERED=1
Environment=WHISPERKEY_REEXEC=1

[Install]
WantedBy=graphical-session.target
EOF
ok "wrote $SERVICE"

# Put `whisperkey` on PATH; ~/.local/bin is on it by default on Ubuntu.
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/whisperkey" "$HOME/.local/bin/whisperkey"
ok "linked whisperkey into ~/.local/bin"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "~/.local/bin is not on your PATH; add it to ~/.bashrc" ;;
esac

systemctl --user daemon-reload
# Make DISPLAY/XAUTHORITY visible to user services (WhisperKey also probes for
# the display itself, since DISPLAY is not always correct).
systemctl --user import-environment DISPLAY XAUTHORITY 2>/dev/null || true
systemctl --user enable whisperkey.service >/dev/null 2>&1 && ok "enabled at login"

# --- 8. optional tray -----------------------------------------------------
echo
if "$PY" -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1')" 2>/dev/null; then
  ok "tray icon support present"
else
  warn "No tray icon. It is the one piece that needs root; if you want it:"
  echo "      sudo apt install gir1.2-ayatanaappindicator3-0.1"
  echo "      systemctl --user restart whisperkey"
fi

echo
bold "Done."
echo
KEYNAME=$("$PY" -c "
from whisperkey.config import load
k = load().hotkey.key
print({'cmd_l':'Left Command','cmd_r':'Right Command','alt_r':'Right Option',
       'alt_l':'Left Option','ctrl_r':'Right Ctrl','ctrl_l':'Left Ctrl',
       'caps_lock':'Caps Lock'}.get(k, k))" 2>/dev/null || echo "your dictation key")
echo "  Start it now:   systemctl --user start whisperkey"
echo "  Check it:       $VENV/bin/whisperkey doctor"
echo "  Then hold $KEYNAME and talk. Double-tap it to dictate hands-free."
echo
echo "  Not the key you want?  $VENV/bin/whisperkey detect-key"
echo
