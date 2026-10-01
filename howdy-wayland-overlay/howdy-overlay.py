#!/usr/bin/env python3
"""
howdy-overlay — Wayland camera overlay for Howdy face authentication.

Runs as a systemd user service. Watches /tmp/howdy-active; when Howdy
starts authenticating (sudo, polkit, etc.) it shows a floating camera-feed
window above other windows.

Compositor support:
  wlroots compositors (Hyprland, Sway, river, …):
      Uses wlr-layer-shell via gtk4-layer-shell for a proper OVERLAY-layer
      surface that floats above all normal windows.
      Requires: sudo apt install gir1.2-gtk4layershell-1.0

  Other Wayland / X11 (fallback):
      Creates a normal GTK4 window and calls set_keep_above(). Visibility
      depends on the compositor's window stacking policy.

Frame protocol (written by compare.py as root):
  /run/howdy-active    — exists while authentication is in progress
  /run/howdy-frame.jpg — latest annotated JPEG frame, atomically replaced
"""

import gi
import os
import sys

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GLib, GdkPixbuf, Gdk

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell
    _HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    _HAS_LAYER_SHELL = False

ACTIVE_FILE = "/run/howdy-active"
FRAME_FILE  = "/run/howdy-frame.jpg"
CHECK_MS    = 200   # how often to poll for /run/howdy-active (ms)
POLL_MS     = 90    # frame refresh interval while active (ms) ≈ 11 fps
OVERLAY_W   = 300   # display width; height follows 4:3 aspect


class HowdyOverlayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.howdy.overlay")
        self._window   = None
        self._picture  = None
        self._frame_ns = 0
        self._watch_id = None
        self._poll_id  = None

    # ------------------------------------------------------------------ GTK

    def do_activate(self):
        self.hold()  # prevent GTK4 from exiting when no window is open
        self._watch_id = GLib.timeout_add(CHECK_MS, self._check_active)

    # ------------------------------------------------------------------ polling

    def _check_active(self):
        active = os.path.exists(ACTIVE_FILE)
        if active and self._window is None:
            self._show()
        elif not active and self._window is not None:
            self._hide()
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------------ overlay

    def _show(self):
        win = Gtk.Window(application=self)
        win.set_title("Howdy")
        win.set_resizable(False)
        # Prevent the user from closing the overlay manually
        win.connect("close-request", lambda *_: True)

        if _HAS_LAYER_SHELL:
            LayerShell.init_for_window(win)
            LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
            LayerShell.set_anchor(win, LayerShell.Edge.TOP,  True)
            LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
            LayerShell.set_margin(win, LayerShell.Edge.TOP,  48)
            LayerShell.set_margin(win, LayerShell.Edge.LEFT, 48)
            LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
            LayerShell.set_exclusive_zone(win, 0)
        else:
            win.set_keep_above(True)

        # Layout
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        label = Gtk.Label(label="Howdy — identifying you…")
        box.append(label)

        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_size_request(OVERLAY_W, OVERLAY_W * 3 // 4)
        box.append(self._picture)

        # CSS
        css = Gtk.CssProvider()
        css.load_from_string(
            "window {"
            "  background: rgba(20, 20, 28, 0.88);"
            "  border-radius: 14px;"
            "}"
            "label {"
            "  color: #e0e0f0;"
            "  font-weight: bold;"
            "  font-size: 13px;"
            "}"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        win.set_child(box)
        win.present()
        self._window = win

        self._frame_ns = 0
        self._poll_id = GLib.timeout_add(POLL_MS, self._update_frame)

    def _hide(self):
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        if self._window:
            self._window.destroy()
            self._window  = None
            self._picture = None
        self._frame_ns = 0

    def _update_frame(self):
        if self._picture is None:
            return GLib.SOURCE_CONTINUE
        try:
            ns = os.stat(FRAME_FILE).st_mtime_ns
        except OSError:
            return GLib.SOURCE_CONTINUE

        if ns == self._frame_ns:
            return GLib.SOURCE_CONTINUE

        try:
            pixbuf  = GdkPixbuf.Pixbuf.new_from_file(FRAME_FILE)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            self._picture.set_paintable(texture)
            self._frame_ns = ns
        except Exception:
            pass   # mid-write or decode error — retry next tick

        return GLib.SOURCE_CONTINUE


if __name__ == "__main__":
    app = HowdyOverlayApp()
    sys.exit(app.run(sys.argv))
