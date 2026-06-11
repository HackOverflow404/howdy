# Howdy Camera Overlay (GNOME Shell extension)

Shows the howdy camera feed **above the lock screen and the GDM login screen**
while face authentication runs. On the normal logged-in desktop, `compare.py`
already opens its own OpenCV window, so the overlay stays hidden there (toggle
`SHOW_IN_USER_MODE` in `extension.js` to change that).

## Requirements

**GNOME Shell 45 or newer.** The extension uses the ESM (ECMAScript Modules)
format introduced in GNOME 45. Earlier versions used a different, incompatible
API and are not supported.

| Distro               | Min. version    | Notes                                   |
|----------------------|-----------------|-----------------------------------------|
| Ubuntu               | 23.10 (Mantic)  | 22.04 LTS ships GNOME 42 — not supported |
| Fedora               | 39              |                                         |
| Arch Linux           | rolling         | Always current                          |
| Debian               | Trixie (13)     | Bookworm ships GNOME 43 — not supported  |
| openSUSE Tumbleweed  | rolling         | Always current                          |

## Wayland compatibility

The extension is **fully Wayland-native** — it runs inside gnome-shell and does
not touch X11. Set `overlay = true` and use the extension for the best experience
on any compositor.

The `show_window` config option (OpenCV popup during sudo / polkit in a logged-in
session) creates a window via **XWayland**, which is present on virtually all
Wayland desktops. The popup therefore appears on all compositors. What differs
is how the window is raised — `compare.py` detects the compositor by scanning
the user's process environment and picks the right tool automatically:

| Compositor | Tool used | Notes |
|---|---|---|
| **X11** (any WM) | `xdotool` | Full support |
| **sway** | `swaymsg '[pid=X] focus'` | Requires `sway` package |
| **Hyprland** | `hyprctl dispatch focuswindow pid:X` | Requires `hyprland` package |
| **KDE Plasma** | `xdotool` | KDE's XWayland support handles this cleanly |
| **GNOME** | `xdotool` (best-effort) | Mutter restricts external window raising; the window appears but may not come to the front — `overlay = true` is the better solution on GNOME |
| **Other Wayland** | `xdotool` via XWayland | Usually works if XWayland is present |

If the compositor-specific tool is not installed, `compare.py` falls back to
`xdotool`. If XWayland is absent, `show_window` is silently disabled and
authentication continues normally.

## How it fits together

`howdy/src/compare.py` (installed to `/usr/lib/howdy/compare.py`) drives the
camera during PAM auth and writes two files to the shared `/tmp`:

| File | Meaning |
|------|---------|
| `/tmp/howdy-active`    | exists while auth is in progress |
| `/tmp/howdy-frame.jpg` | latest annotated frame, atomically replaced |

The extension polls `/tmp/howdy-active`; when present (and the session is
locked or at the greeter) it shows a small panel and reloads the JPEG ~10x/s.

**Privacy note:** `/tmp/howdy-frame.jpg` is written world-readable (0644, owned
by root). The `gdm` process runs as a non-root user and must be able to read the
file for the greeter overlay. As a result, any local user can read camera frames
from that path while authentication is in progress. This is an intentional
trade-off. Users who find this unacceptable can leave `overlay = false` in the
config (the default) and rely on `show_window` instead, which requires no
world-readable files.

## Why it works on the lock screen / greeter

- **`session-modes: ["user", "unlock-dialog", "gdm"]`** in `metadata.json` —
  without `unlock-dialog`, GNOME Shell *disables the extension the instant the
  screen locks*, so nothing could ever appear there. `gdm` lets it run on the
  login greeter.
- Frames are decoded with **GdkPixbuf** and pushed into a Clutter content via
  **`St.ImageContent`** instead of `St.Icon` + `Gio.FileIcon`. `StTextureCache`
  caches gicons by URI with no file-change monitor, which froze the feed on the
  first frame.
- The greeter is a *separate* gnome-shell running as user `gdm`, so the
  extension must also live in `/usr/share/gnome-shell/extensions/` and be
  enabled in gdm's own dconf profile — `install.sh` handles both.

## Install / redeploy

```bash
./install.sh
```

Deploys to `~/.local/share/...` (your session + lock screen) and, with sudo, to
`/usr/share/...` + a gdm dconf keyfile (the greeter).

Activate:
- Session + lock screen: log out/in, or on X11 `Alt+F2` → `r` → Enter.
- Greeter: `sudo systemctl restart gdm` (logs you out) or next reboot.

Then enable the feature in `/etc/howdy/config.ini`:

```ini
[video]
overlay = true
```

## Optional: show_window (OpenCV popup)

When `show_window = true` is set in `config.ini`, `compare.py` opens an OpenCV
window during face authentication in logged-in sessions (sudo, polkit, etc.).
This is independent of the extension overlay and works on X11 and Wayland
(via XWayland).

To raise the popup above other windows, install the tool for your compositor
(all are optional — the popup still appears without them):

```bash
# X11, GNOME, or KDE — xdotool
sudo apt install xdotool          # Debian / Ubuntu
sudo dnf install xdotool          # Fedora
sudo pacman -S xdotool            # Arch

# sway — already installed with sway itself (swaymsg is part of the sway package)

# Hyprland — already installed with Hyprland itself (hyprctl is part of the package)
```

`compare.py` auto-detects the compositor and picks the right tool. If none is
available the popup opens wherever the compositor places it.

## Requirements for the greeter (GDM) case

### PrivateTmp

PAM modules run as child processes of the GDM daemon. If GDM's systemd unit has
`PrivateTmp=yes`, GDM gets an isolated `/tmp` namespace. Whether the GDM greeter
shell (gnome-shell running as the `gdm` user) shares that namespace with the PAM
child depends on how your distribution launches it. If the overlay does not
appear at the login screen, check:

```bash
sudo systemctl show gdm | grep PrivateTmp
```

If the result is `PrivateTmp=yes` and the overlay is missing, add a drop-in
override:

```bash
sudo systemctl edit gdm
```

In the editor that opens, add:

```ini
[Service]
PrivateTmp=no
```

Save, then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart gdm   # this will log you out
```

**Most default GDM configurations do not set `PrivateTmp`.** This only applies
if your distribution or a security hardening tool (e.g. `systemd-harden`) has
enabled it. The Howdy package itself does not set `PrivateTmp` on GDM.

### Frame permissions

`compare.py` writes `/tmp/howdy-frame.jpg` world-readable (0644) so the `gdm`
user can read it. See the privacy note under [How it fits together](#how-it-fits-together).

## Logs

```bash
journalctl -f -o cat /usr/bin/gnome-shell      # user shell + lock screen
journalctl -f _COMM=gnome-shell                # includes greeter
sudo cat /tmp/howdy-debug.log                  # compare.py side (when overlay=true)
```
