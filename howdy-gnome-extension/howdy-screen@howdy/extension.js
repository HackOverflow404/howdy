/**
 * Howdy Camera Overlay — GNOME Shell Extension (GNOME 45–48)
 *
 * Shows the howdy camera feed above the lock screen ("unlock-dialog") and the
 * GDM login screen ("gdm") while face authentication is in progress. When you
 * are logged in normally ("user" mode) the overlay stays hidden, because
 * compare.py shows its own OpenCV window there — flip SHOW_IN_USER_MODE to
 * true if you want the extension overlay in that case too.
 *
 * Protocol (written by compare.py):
 *   /tmp/howdy-active    — created when auth starts, deleted when it ends
 *   /tmp/howdy-frame.jpg — JPEG frame, atomically replaced each render tick
 *
 * Frames are loaded with GdkPixbuf and pushed straight into a Clutter content
 * via St.ImageContent. We deliberately do NOT use St.Icon + Gio.FileIcon:
 * StTextureCache caches gicon textures by URI with no file-change monitor, so
 * the overlay would freeze on the very first frame.
 */

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import GdkPixbuf from 'gi://GdkPixbuf';
import Cogl from 'gi://Cogl';
import Clutter from 'gi://Clutter';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const ACTIVE_FILE = '/tmp/howdy-active';
const FRAME_FILE  = '/tmp/howdy-frame.jpg';
const POLL_MS     = 100;   // how often to reload the frame while active
const CHECK_MS    = 250;   // how often to check whether auth is active
const DISPLAY_W   = 360;   // overlay width in px; height follows the aspect ratio

// Show the overlay while logged in (normal "user" session) too. Left false so
// the logged-in experience stays the OpenCV window that compare.py opens.
const SHOW_IN_USER_MODE = false;

export default class HowdyCameraOverlay {
    enable() {
        this._overlay   = null;
        this._frame     = null;
        this._watchId   = null;
        this._pollId    = null;
        this._frameTime = 0;

        // Poll for /tmp/howdy-active appearing/disappearing, and for the
        // session mode allowing the overlay.
        this._watchId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, CHECK_MS, () => {
            const shouldShow =
                GLib.file_test(ACTIVE_FILE, GLib.FileTest.EXISTS) &&
                this._modeAllowsOverlay();

            if (shouldShow && !this._overlay)
                this._showOverlay();
            else if (!shouldShow && this._overlay)
                this._hideOverlay();

            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._watchId) {
            GLib.source_remove(this._watchId);
            this._watchId = null;
        }
        this._hideOverlay();
    }

    _modeAllowsOverlay() {
        const mode = Main.sessionMode?.currentMode;
        if (mode === 'user' || mode === undefined)
            return SHOW_IN_USER_MODE;
        // 'unlock-dialog', 'gdm', and any other non-user mode → show.
        return true;
    }

    _showOverlay() {
        this._overlay = new St.BoxLayout({
            vertical: true,
            style: 'background-color: rgba(0,0,0,0.80);' +
                   'border-radius: 12px; padding: 10px; spacing: 6px;',
            reactive: false,
        });

        const label = new St.Label({
            text: 'Howdy — identifying you…',
            style: 'color: white; font-size: 13px; font-weight: bold;',
        });
        this._overlay.add_child(label);

        // The camera image. A bare St.Widget whose Clutter content we replace
        // each frame — no texture cache in the way.
        this._frame = new St.Widget({
            width: DISPLAY_W,
            height: Math.round(DISPLAY_W * 3 / 4),
            style: 'border-radius: 8px;',
        });
        this._frame.set_content_gravity(Clutter.ContentGravity.RESIZE_ASPECT);
        this._overlay.add_child(this._frame);

        // screenShieldGroup sits above the lock-screen shield (and exists in
        // the gdm greeter too); fall back to uiGroup just in case.
        const parent = Main.layoutManager.screenShieldGroup ?? Main.layoutManager.uiGroup;
        parent.add_child(this._overlay);
        this._overlay.set_position(40, 80);

        this._frameTime = 0;
        this._pollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_MS, () => {
            this._updateFrame();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _hideOverlay() {
        if (this._pollId) {
            GLib.source_remove(this._pollId);
            this._pollId = null;
        }
        if (this._overlay) {
            this._overlay.get_parent()?.remove_child(this._overlay);
            this._overlay.destroy();
            this._overlay = null;
            this._frame   = null;
        }
        this._frameTime = 0;
    }

    _updateFrame() {
        if (!this._frame)
            return;

        // Only reload when the file's mtime actually changed.
        let mtime;
        try {
            const info = Gio.File.new_for_path(FRAME_FILE).query_info(
                'time::modified', Gio.FileQueryInfoFlags.NONE, null);
            mtime = info.get_modification_date_time()?.to_unix() ?? 0;
        } catch (_) {
            return; // frame not written yet
        }
        if (mtime === this._frameTime)
            return;

        let pixbuf;
        try {
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(FRAME_FILE);
        } catch (_) {
            return; // mid-write / momentarily unreadable — retry next tick
        }
        this._frameTime = mtime;

        const w = pixbuf.get_width();
        const h = pixbuf.get_height();
        const fmt = pixbuf.get_has_alpha()
            ? Cogl.PixelFormat.RGBA_8888
            : Cogl.PixelFormat.RGB_888;

        const content = St.ImageContent.new_with_preferred_size(w, h);
        const ok = content.set_bytes(
            GLib.Bytes.new(pixbuf.get_pixels()),
            fmt, w, h, pixbuf.get_rowstride());
        if (!ok)
            return;

        this._frame.set_content(content);
        this._frame.set_size(DISPLAY_W, Math.round(DISPLAY_W * h / w));
    }
}
