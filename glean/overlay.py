"""The fullscreen "drag a box round it" layer.

The screenshot is taken *before* this window is mapped and then drawn as the
window's own background, so what you are dragging over is a still frame.  That
avoids the two classic bugs of live selection overlays: the overlay appearing
in its own capture, and the content moving between the drag and the crop.
"""

import math

import cairo

from glean import capture
from glean.gi3 import Gdk, GdkPixbuf, Gtk

DIM = 0.55                  # how far the unselected screen is knocked back
MIN_DRAG = 5                # px; below this a press-release counts as a click
LOUPE = 130                 # side of the magnifier, px
LOUPE_ZOOM = 8
ACCENT = (0.45, 0.78, 1.0)

HINT = "drag to select   ·   click a window   ·   Esc to cancel"


class Overlay(Gtk.Window):
    """Runs its own main loop. `.rect` is the selection, or None if cancelled."""

    def __init__(self, pixbuf, hint=HINT):
        wayland = capture.is_wayland()
        super().__init__(type=Gtk.WindowType.TOPLEVEL if wayland
                         else Gtk.WindowType.POPUP)
        self.pixbuf = pixbuf
        self.hint = hint
        self.rect = None
        self._anchor = None
        self._cursor = (0, 0)
        self._inside = False

        if wayland:
            # GDK's root window has no size on Wayland; the monitors do. The
            # window lands on the focused monitor, so show only that screen:
            # the view is cropped to it, while selections are still reported
            # in global coordinates for crop() against the full capture.
            self._full = pixbuf
            self._vx, self._vy, self._vw, self._vh = _virtual_bounds(
                self.get_display())
            self._landed = None
            self._set_view(self.get_display().get_primary_monitor()
                           or self.get_display().get_monitor(0))
        else:
            self._full = pixbuf
            root = Gdk.get_default_root_window()
            self._w, self._h = root.get_width(), root.get_height()
            self._vx = self._vy = self._ox = self._oy = 0
            self._vw, self._vh = self._w, self._h
            self._landed = True
            self._sx = pixbuf.get_width() / float(self._w or 1)
            self._sy = pixbuf.get_height() / float(self._h or 1)
            self._view = pixbuf
            self.pixbuf = pixbuf

        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_title("glean")
        if wayland:
            self.fullscreen()
        else:
            self.move(0, 0)
            self.set_default_size(self._w, self._h)
            self.resize(self._w, self._h)

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.KEY_PRESS_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK
                        | Gdk.EventMask.ENTER_NOTIFY_MASK)
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("key-press-event", self._on_key)
        self.connect("realize", self._on_realize)

    # ------------------------------------------------------------ lifecycle
    def _on_realize(self, _w):
        self._land_on_monitor()
        window = self.get_window()
        window.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "crosshair"))

    def _land_on_monitor(self):
        """Once mapped, Wayland tells us which monitor the window covers —
        the focused one. Re-check on each draw: the first frames can land
        before the compositor has assigned the surface."""
        if self.get_window() is None:
            return
        monitor = self.get_display().get_monitor_at_window(self.get_window())
        if monitor is None:
            return
        g = monitor.get_geometry()
        if self._landed is not None and (g.x, g.y, g.width, g.height) == self._landed:
            return
        self._landed = (g.x, g.y, g.width, g.height)
        self._set_view(monitor)

    def _set_view(self, monitor):
        """Point the overlay at one monitor: show only that screen's pixels."""
        g = monitor.get_geometry()
        self._ox, self._oy = g.x, g.y
        self._w, self._h = g.width, g.height
        # The full capture is in device pixels; monitor geometry is logical.
        self._sx = self._full.get_width() / float(self._vw or 1)
        self._sy = self._full.get_height() / float(self._vh or 1)
        x, y = int(g.x * self._sx), int(g.y * self._sy)
        w = max(1, min(int(g.width * self._sx),
                       self._full.get_width() - x))
        h = max(1, min(int(g.height * self._sy),
                       self._full.get_height() - y))
        self._view = self._full.new_subpixbuf(x, y, w, h)
        self.pixbuf = self._view

    def choose(self):
        """Show, block until the user picks or cancels, return a rect or None."""
        self.show_all()
        self.present()
        seat = self.get_display().get_default_seat()
        if seat is not None:
            seat.grab(self.get_window(), Gdk.SeatCapabilities.ALL, False,
                      None, None, None, None)
        Gtk.main()
        return self.rect

    def _finish(self, rect):
        self.rect = rect
        seat = self.get_display().get_default_seat()
        if seat is not None:
            seat.ungrab()
        self.hide()
        # Let the compositor repaint the desktop before anything else looks at
        # it; without this the overlay can still be on screen for a frame.
        while Gtk.events_pending():
            Gtk.main_iteration()
        Gtk.main_quit()

    # --------------------------------------------------------------- events
    def _on_press(self, _w, event):
        if event.button == 1:
            self._anchor = (event.x, event.y)
        else:
            self._finish(None)
        return True

    def _on_motion(self, _w, event):
        self._cursor = (event.x, event.y)
        self._inside = True
        self.queue_draw()
        return True

    def _on_enter(self, _w, _e):
        self._inside = True
        return True

    def _on_leave(self, _w, _e):
        self._inside = False
        self.queue_draw()
        return True

    def _on_release(self, _w, event):
        if event.button != 1 or self._anchor is None:
            return True
        rect = normalise(self._anchor, (event.x, event.y))
        self._anchor = None
        if rect[2] < MIN_DRAG or rect[3] < MIN_DRAG:
            rect = window_at(int(event.x_root), int(event.y_root),
                             exclude=self.get_window())
            if rect is None:
                self.queue_draw()
                return True
        else:
            # window-local -> compositor-global, which is the space the
            # returned rect and crop() work in.
            rect = (rect[0] + self._ox, rect[1] + self._oy, rect[2], rect[3])
        self._finish(clamp(rect, self._vw, self._vh))
        return True

    def _on_key(self, _w, event):
        name = Gdk.keyval_name(event.keyval)
        if name in ("Escape", "q", "Q"):
            self._finish(None)
        elif name in ("Return", "KP_Enter", "space"):
            rect = window_at(int(self._cursor[0]), int(self._cursor[1]),
                             exclude=self.get_window())
            if rect:
                self._finish(clamp(rect, self._vw, self._vh))
        elif name in ("a", "A"):
            # the whole screen the overlay is on, not the whole virtual one
            self._finish((self._ox, self._oy, self._w, self._h))
        return True

    # -------------------------------------------------------------- drawing
    def _on_draw(self, _w, cr):
        self._land_on_monitor()
        cr.save()
        cr.scale(1.0 / self._sx, 1.0 / self._sy)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        cr.restore()

        cr.set_source_rgba(0, 0, 0, DIM)
        cr.paint()

        rect = None
        if self._anchor is not None:
            rect = normalise(self._anchor, self._cursor)

        if rect and rect[2] >= 1 and rect[3] >= 1:
            self._draw_selection(cr, rect)
        elif self._inside:
            self._draw_loupe(cr)
        self._draw_hint(cr)
        return True

    def _draw_selection(self, cr, rect):
        x, y, w, h = rect
        cr.save()
        cr.rectangle(x, y, w, h)
        cr.clip()
        cr.save()
        cr.scale(1.0 / self._sx, 1.0 / self._sy)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        cr.restore()
        cr.restore()

        cr.set_source_rgb(*ACCENT)
        cr.set_line_width(1.0)
        cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
        cr.stroke()
        self._draw_size(cr, rect)

    def _draw_size(self, cr, rect):
        x, y, w, h = rect
        label = "%d × %d" % (w, h)
        cr.select_font_face("monospace")
        cr.set_font_size(13)
        extents = cr.text_extents(label)
        pad = 6
        bw, bh = extents.width + pad * 2, 22
        bx = min(max(0, x), self._w - bw)
        by = y - bh - 6
        if by < 0:
            by = min(y + h + 6, self._h - bh)
        _rounded(cr, bx, by, bw, bh, 4)
        cr.set_source_rgba(0, 0, 0, 0.8)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(bx + pad, by + bh - 7)
        cr.show_text(label)

    def _draw_loupe(self, cr):
        cx, cy = self._cursor
        half = LOUPE / 2.0
        # Keep the loupe on screen and out from under the pointer.
        lx = cx + 20 if cx + 20 + LOUPE < self._w else cx - 20 - LOUPE
        ly = cy + 20 if cy + 20 + LOUPE < self._h else cy - 20 - LOUPE

        cr.save()
        _rounded(cr, lx, ly, LOUPE, LOUPE, 6)
        cr.clip_preserve()
        cr.set_source_rgb(0, 0, 0)
        cr.fill_preserve()
        cr.translate(lx + half, ly + half)
        cr.scale(LOUPE_ZOOM / self._sx, LOUPE_ZOOM / self._sy)
        # Half a pixel, so the pixel under the pointer straddles the crosshair
        # rather than starting at it. self.pixbuf is the monitor view, so
        # window-local pointer coords map straight into it.
        cr.translate(-(cx + 0.5) * self._sx, -(cy + 0.5) * self._sy)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_NEAREST)   # show the pixels
        cr.paint()
        cr.restore()

        cr.set_source_rgba(*ACCENT, 0.9)
        cr.set_line_width(1.0)
        _rounded(cr, lx + 0.5, ly + 0.5, LOUPE - 1, LOUPE - 1, 6)
        cr.stroke()
        cr.move_to(lx, ly + half)
        cr.line_to(lx + LOUPE, ly + half)
        cr.move_to(lx + half, ly)
        cr.line_to(lx + half, ly + LOUPE)
        cr.set_source_rgba(*ACCENT, 0.35)
        cr.stroke()

    def _draw_hint(self, cr):
        display = self.get_display()
        monitor = display.get_monitor_at_point(int(self._cursor[0] + self._ox),
                                               int(self._cursor[1] + self._oy)) \
            or display.get_monitor(0)
        area = monitor.get_geometry()
        cr.select_font_face("sans-serif")
        cr.set_font_size(14)
        extents = cr.text_extents(self.hint)
        pad = 14
        bw, bh = extents.width + pad * 2, 34
        bx = (area.x - self._ox) + (area.width - bw) / 2
        by = (area.y - self._oy) + 40
        _rounded(cr, bx, by, bw, bh, 8)
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.92)
        cr.move_to(bx + pad, by + bh - 12)
        cr.show_text(self.hint)


def _virtual_bounds(display):
    """(x, y, w, h) bounding box of every monitor, in compositor coordinates."""
    x0 = y0 = x1 = y1 = 0
    for i in range(display.get_n_monitors()):
        g = display.get_monitor(i).get_geometry()
        x0, y0 = min(x0, g.x), min(y0, g.y)
        x1, y1 = max(x1, g.x + g.width), max(y1, g.y + g.height)
    return x0, y0, x1 - x0, y1 - y0


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def normalise(start, end):
    """Two corners in any order -> (x, y, w, h) with non-negative extent."""
    x0, y0 = start
    x1, y1 = end
    x, y = int(min(x0, x1)), int(min(y0, y1))
    return (x, y, int(abs(x1 - x0)), int(abs(y1 - y0)))


def clamp(rect, width, height):
    """Trim a rect to the screen. None if nothing is left of it."""
    x, y, w, h = rect
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def window_at(x, y, exclude=None):
    """Frame of the topmost toplevel under (x, y). X11 only; None elsewhere."""
    screen = Gdk.Screen.get_default()
    if screen is None or not hasattr(screen, "get_window_stack"):
        return None
    stack = screen.get_window_stack() or []
    skip = exclude.get_xid() if exclude is not None and \
        hasattr(exclude, "get_xid") else None
    for window in reversed(stack):
        try:
            if skip is not None and window.get_xid() == skip:
                continue
            frame = window.get_frame_extents()
        except (AttributeError, TypeError):
            continue
        if frame.width <= 1 or frame.height <= 1:
            continue
        if frame.x <= x < frame.x + frame.width and \
                frame.y <= y < frame.y + frame.height:
            return (frame.x, frame.y, frame.width, frame.height)
    return None


def select(pixbuf=None, hint=HINT):
    """Freeze the screen, let the user pick. Returns (pixbuf, rect) or (None, None)."""
    if pixbuf is None:
        pixbuf = capture.screen()
    overlay = Overlay(pixbuf, hint)
    rect = overlay.choose()
    overlay.destroy()
    if rect is None:
        return None, None
    return pixbuf, rect


def crop(pixbuf, rect, screen_w=None, screen_h=None):
    """Cut `rect` (in compositor-global coordinates) out of a full-screen pixbuf."""
    x, y, w, h = rect
    if not screen_w or not screen_h:
        _bx, _by, bw, bh = _virtual_bounds(Gdk.Display.get_default())
        screen_w = screen_w or bw
        screen_h = screen_h or bh
    sx = pixbuf.get_width() / float(screen_w or 1)
    sy = pixbuf.get_height() / float(screen_h or 1)
    x, y = int(x * sx), int(y * sy)
    w, h = max(1, int(w * sx)), max(1, int(h * sy))
    w = min(w, pixbuf.get_width() - x)
    h = min(h, pixbuf.get_height() - y)
    if w <= 0 or h <= 0:
        return None
    return GdkPixbuf.Pixbuf.new_subpixbuf(pixbuf, x, y, w, h)
