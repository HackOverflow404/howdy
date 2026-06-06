# Howdy Camera Overlay (GNOME Shell extension)

Shows the howdy camera feed **above the lock screen and the GDM login screen**
while face authentication runs. On the normal logged-in desktop, `compare.py`
already opens its own OpenCV window, so the overlay stays hidden there (toggle
`SHOW_IN_USER_MODE` in `extension.js` to change that).

## How it fits together

`howdy/src/compare.py` (installed to `/usr/lib/howdy/compare.py`) drives the
camera during PAM auth and writes two files to the shared `/tmp`:

| File | Meaning |
|------|---------|
| `/tmp/howdy-active`    | exists while auth is in progress |
| `/tmp/howdy-frame.jpg` | latest annotated frame, atomically replaced |

The extension polls `/tmp/howdy-active`; when present (and the session is
locked or at the greeter) it shows a small panel and reloads the JPEG ~10x/s.

## Why it now works on the lock screen / greeter

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

## Requirements for the greeter case

- GDM service must **not** use `PrivateTmp` (otherwise it can't see `/tmp/howdy-*`).
- `compare.py` writes the frame world-readable (0644) so the `gdm` user can read it.
