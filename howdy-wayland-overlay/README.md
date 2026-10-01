# Howdy Wayland Overlay

A standalone GTK4 overlay service that shows the Howdy camera feed during
`sudo`/`pkexec`/`polkit` authentication in logged-in Wayland sessions
(Hyprland, Sway, GNOME, KDE, etc.).

## How it works

`compare.py` writes two files while face recognition runs:

| File | Meaning |
|------|---------|
| `/tmp/howdy-active`    | exists while auth is in progress |
| `/tmp/howdy-frame.jpg` | latest annotated JPEG frame (0644) |

This service polls for `/tmp/howdy-active` every 200 ms. When it appears, a
camera-feed window is shown. When auth ends (file deleted), the window closes.

## Lock screen vs. logged-in sessions

| Scenario | Overlay |
|----------|---------|
| **Lock screen** (caelestia/Quickshell) | Built into the lock QML — no extra service needed |
| **`sudo` / `pkexec` / `polkit`** in a running session | This service |

## Requirements

```bash
# GTK4 Python bindings — likely already installed
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-gdkpixbuf-2.0

# wlr-layer-shell support (recommended — gives true OVERLAY layer)
sudo apt install gir1.2-gtk4layershell-1.0
```

Without `gtk4-layer-shell`, the overlay falls back to a regular GTK4 window
(`set_keep_above()`). On Hyprland this still appears above most windows; it just
won't be a true compositor layer surface.

## Install

```bash
cd howdy-wayland-overlay
./install.sh
```

The script:
1. Checks dependencies
2. Installs `howdy-overlay.py` to `/usr/local/lib/howdy-overlay/`
3. Installs and enables a **systemd user service** (`howdy-overlay.service`)

## Howdy config

Enable overlay frame-writing in `/etc/howdy/config.ini`:

```ini
[video]
overlay = true        # write /tmp/howdy-frame.jpg — required for this overlay
show_window = true    # also show OpenCV window (XWayland); optional
mirror = true         # mirror the camera feed if needed
```

## Hyprland window rules (optional)

Add to `~/.config/hypr/hyprland/rules.conf` so the overlay window is always
centered and on top without needing hyprctl:

```ini
windowrule = float true,     match:title Howdy
windowrule = pin true,       match:title Howdy
windowrule = center 1,       match:title Howdy
windowrule = size 340 280,   match:title Howdy
```

## Status / logs

```bash
systemctl --user status howdy-overlay
journalctl --user -u howdy-overlay -f
sudo cat /tmp/howdy-debug.log      # compare.py side (needs overlay=true)
```
