#!/usr/bin/env bash
#
# Install the Howdy Camera Overlay GNOME Shell extension for BOTH:
#   - your logged-in user session + lock screen  (~/.local/share/...)
#   - the GDM login greeter                       (/usr/share/... + gdm dconf)
#
# Re-run this after editing extension.js / metadata.json to redeploy.
# Needs sudo for the system-wide (GDM greeter) parts.

set -euo pipefail

UUID="howdy-screen@howdy"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$UUID"

USER_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
SYS_DIR="/usr/share/gnome-shell/extensions/$UUID"
GDM_DCONF="/etc/dconf/db/gdm.d/99-howdy-extension"

echo "Source: $SRC"

# 1. User session + lock screen (unlock-dialog) -----------------------------
echo "Installing for user session  -> $USER_DIR"
mkdir -p "$USER_DIR"
cp "$SRC/extension.js" "$SRC/metadata.json" "$USER_DIR/"
gnome-extensions enable "$UUID" 2>/dev/null || true

# 2. GDM login greeter (gdm session mode) -----------------------------------
echo "Installing for GDM greeter   -> $SYS_DIR  (sudo)"
sudo mkdir -p "$SYS_DIR"
sudo cp "$SRC/extension.js" "$SRC/metadata.json" "$SYS_DIR/"

echo "Enabling extension in GDM dconf -> $GDM_DCONF  (sudo)"
sudo tee "$GDM_DCONF" >/dev/null <<'EOF'
# Load the Howdy camera overlay on the GDM login screen.
[org/gnome/shell]
enabled-extensions=['howdy-screen@howdy']
EOF
sudo dconf update

cat <<'EOF'

Done.

To activate:
  - User session + lock screen: log out and back in, OR (X11 only) press
    Alt+F2, type 'r', Enter to restart GNOME Shell.
  - GDM greeter: it picks up the new dconf on its next start. Force it now
    (this LOGS YOU OUT) with:  sudo systemctl restart gdm

Test:
  - Lock the screen (Super+L) and look at the camera — overlay should appear.
  - Suspend + resume to the lock screen — same.
  - Log fully out to the GDM greeter and trigger howdy.

Logs:
  journalctl -f -o cat /usr/bin/gnome-shell      # user shell
  journalctl -f _COMM=gnome-shell                # incl. greeter
  sudo cat /tmp/howdy-debug.log                  # compare.py side
EOF
