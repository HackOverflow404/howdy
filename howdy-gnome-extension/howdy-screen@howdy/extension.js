/**
 * Howdy Camera Overlay — GNOME Shell Extension (GNOME 45–50, Wayland & X11)
 *
 * Shows the howdy camera feed above the lock screen ("unlock-dialog") and the
 * GDM login screen ("gdm") while face authentication is in progress. When you
 * are logged in normally ("user" mode) the overlay also shows by default,
 * because compare.py runs as root under sudo/pkexec and usually has no display
 * access, so its OpenCV window never appears — flip SHOW_IN_USER_MODE to false
 * if you would rather rely on that window in a logged-in session.
 *
 * This is the only overlay mechanism that works on the GNOME lock screen and
 * greeter, on both Wayland (the only session type in Ubuntu 26.04 / GNOME 50)
 * and X11. It runs entirely inside gnome-shell and never touches the display
 * server directly, so the same code path serves every compositor backend.
 *
 * Protocol (written by compare.py):
 *   /tmp/howdy-active    — created when auth starts, deleted when it ends
 *   /tmp/howdy-frame.jpg — JPEG frame (mode 0644), atomically replaced per tick
 *
 * Frames are loaded with GdkPixbuf and pushed straight into a Clutter content
 * via St.ImageContent. We deliberately do NOT use St.Icon + Gio.FileIcon:
 * StTextureCache caches gicon textures by URI with no file-change monitor, so
 * the overlay would freeze on the very first frame.
 *
 * GNOME version compatibility:
 *   - St.ImageContent is available on GNOME 45+ (Clutter.Image was removed).
 *   - GNOME 48 changed St.ImageContent.set_bytes() to take a Cogl.Context as
 *     its first argument. We detect the context once (see _coglContext) and use
 *     the matching call: the new form on 48+, the legacy form on 45–47.
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

// Show the overlay while logged in (normal "user" session) too. True because
// compare.py runs as root under sudo/pkexec and has no display access, so its
// OpenCV window never appears — the GNOME Shell extension overlay is the only
// reliable visual feedback in that case.
const SHOW_IN_USER_MODE = true;

// Sentinel meaning "Cogl context not looked up yet". Distinct from null, which
// means "looked up and unavailable" (GNOME 45–47, legacy set_bytes signature).
const COGL_UNRESOLVED = undefined;

export default class HowdyCameraOverlay {
    enable() {
        this._overlay     = null;
        this._frame       = null;
        this._watchId     = null;
        this._pollId      = null;
        this._frameTime   = 0;
        this._coglContext = COGL_UNRESOLVED;

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

    /**
     * The Cogl context required by St.ImageContent.set_bytes() on GNOME 48+.
     * Looked up once and cached: returns the context object on 48+, or null on
     * 45–47 (and on any shell where the lookup path is unavailable), in which
     * case the caller uses the legacy set_bytes signature. Resolving this once
     * avoids throwing — and catching — an exception on every single frame.
     */
    _getCoglContext() {
        if (this._coglContext !== COGL_UNRESOLVED)
            return this._coglContext;

        let ctx = null;
        try {
            ctx = global.stage?.context?.get_backend?.()?.get_cogl_context?.() ?? null;
        } catch (_) {
            ctx = null;
        }
        this._coglContext = ctx;
        return ctx;
    }

    /**
     * Lay a St.BoxLayout out vertically across GNOME versions. GNOME 48
     * deprecated the `vertical` property in favour of `orientation`
     * (Clutter.Orientation) and it is slated for removal around GNOME 50;
     * `orientation` in turn does not exist before 48. We detect which property
     * the running shell actually has and set only that one, avoiding both a
     * hard failure on 50 and deprecation warnings on 48+.
     */
    _setVertical(box) {
        let hasOrientation = false;
        try {
            hasOrientation = !!St.BoxLayout.find_property?.('orientation');
        } catch (_) {
            hasOrientation = false;
        }
        if (hasOrientation)
            box.orientation = Clutter.Orientation.VERTICAL;
        else
            box.vertical = true;
    }

    _showOverlay() {
        this._overlay = new St.BoxLayout({
            style: 'background-color: rgba(0,0,0,0.80);' +
                   'border-radius: 12px; padding: 10px; spacing: 6px;',
            reactive: false,
        });
        this._setVertical(this._overlay);

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

        // Only reload when the file's mtime actually changed. Use
        // microsecond resolution: compare.py writes several frames per
        // second, so whole-second mtime (the obvious to_unix()) would
        // collapse them to one and cap the overlay at ~1 FPS.
        let stamp;
        try {
            const info = Gio.File.new_for_path(FRAME_FILE).query_info(
                'time::modified,time::modified-usec',
                Gio.FileQueryInfoFlags.NONE, null);
            const secs = info.get_attribute_uint64('time::modified');
            const usec = info.get_attribute_uint32('time::modified-usec');
            stamp = secs * 1000000 + usec;
        } catch (_) {
            return; // frame not written yet
        }
        if (stamp === this._frameTime)
            return;

        let pixbuf;
        try {
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(FRAME_FILE);
        } catch (_) {
            return; // mid-write / momentarily unreadable — retry next tick
        }
        if (!pixbuf)
            return;
        this._frameTime = stamp;

        const w = pixbuf.get_width();
        const h = pixbuf.get_height();
        if (w <= 0 || h <= 0)
            return;
        const fmt = pixbuf.get_has_alpha()
            ? Cogl.PixelFormat.RGBA_8888
            : Cogl.PixelFormat.RGB_888;

        const content = St.ImageContent.new_with_preferred_size(w, h);

        // GNOME 48 added a leading Cogl.Context argument to set_bytes(); 45–47
        // use the legacy signature. _getCoglContext() tells the two apart.
        const coglContext = this._getCoglContext();
        try {
            if (coglContext) {
                content.set_bytes(
                    coglContext,
                    pixbuf.read_pixel_bytes(),
                    fmt, w, h, pixbuf.get_rowstride());
            } else {
                const ok = content.set_bytes(
                    GLib.Bytes.new(pixbuf.get_pixels()),
                    fmt, w, h, pixbuf.get_rowstride());
                if (!ok)
                    return;
            }
        } catch (_) {
            return; // bad frame / transient encode mismatch — try again next tick
        }

        this._frame.set_content(content);
        this._frame.set_size(DISPLAY_W, Math.round(DISPLAY_W * h / w));
    }
}
