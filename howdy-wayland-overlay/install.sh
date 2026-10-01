#!/usr/bin/env bash
# Install the howdy-overlay Wayland service for sudo/polkit camera feed
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/usr/local/lib/howdy-overlay"
SERVICE_NAME="howdy-overlay.service"
USER_SERVICE_DIR="${HOME}/.config/systemd/user"

echo "=== Howdy Wayland Overlay Installer ==="
echo ""

# ── dependency check ──────────────────────────────────────────────────────────
echo "[1/4] Checking dependencies…"

if ! python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk" 2>/dev/null; then
    echo "ERROR: python3-gi with GTK4 not found."
    echo "  Install: sudo apt install python3-gi gir1.2-gtk-4.0"
    exit 1
fi

if python3 -c "import gi; gi.require_version('Gtk4LayerShell','1.0'); from gi.repository import Gtk4LayerShell" 2>/dev/null; then
    echo "  gtk4-layer-shell: OK (OVERLAY layer support enabled)"
else
    echo "  gtk4-layer-shell: NOT FOUND (will use fallback window mode)"
    echo "  For always-on-top overlay on Hyprland/Sway:"
    echo "    sudo apt install gir1.2-gtk4layershell-1.0"
fi

# ── install files ─────────────────────────────────────────────────────────────
echo "[2/4] Installing overlay app to ${INSTALL_DIR}…"
sudo mkdir -p "${INSTALL_DIR}"
sudo install -m 755 "${SCRIPT_DIR}/howdy-overlay.py" "${INSTALL_DIR}/howdy-overlay.py"

# ── install systemd user service ──────────────────────────────────────────────
echo "[3/4] Installing systemd user service…"
mkdir -p "${USER_SERVICE_DIR}"
# Patch the ExecStart path into the service file
sed "s|ExecStart=.*|ExecStart=${INSTALL_DIR}/howdy-overlay.py|" \
    "${SCRIPT_DIR}/howdy-overlay.service" \
    > "${USER_SERVICE_DIR}/${SERVICE_NAME}"

systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"

# ── enable overlay in howdy config ───────────────────────────────────────────
echo "[4/4] Reminder: enable the overlay in /etc/howdy/config.ini"
echo ""
echo "  sudo nano /etc/howdy/config.ini"
echo "  # Set:"
echo "  overlay = true       # required: writes /tmp/howdy-frame.jpg for this service"
echo "  # show_window is for 'howdy test' only — not needed with this service"
echo ""
echo "=== Installation complete ==="
echo ""
echo "The service is now running. During 'sudo' or 'pkexec' commands that"
echo "trigger Howdy face recognition, a camera overlay will appear."
echo ""
echo "Check status:  systemctl --user status ${SERVICE_NAME}"
echo "View logs:     journalctl --user -u ${SERVICE_NAME} -f"
