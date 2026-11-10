"""Getting the pixels off the screen.

There is no one way to screenshot a Linux desktop, so this is a chain of
backends tried in order of how little they cost the user.  Reading the X11
root window is instant and silent; the portal is a round trip through a
permission dialog.  `pick()` returns the first one that works, and `doctor`
in the CLI prints the whole chain so a failure is diagnosable.
"""

import os
import shutil
import subprocess
import tempfile

from glean.gi3 import Gdk, GdkPixbuf, Gio, GLib


class CaptureError(Exception):
    pass


def is_wayland():
    return bool(os.environ.get("WAYLAND_DISPLAY")) or \
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def _load(path):
    try:
        return GdkPixbuf.Pixbuf.new_from_file(path)
    except GLib.Error as exc:
        raise CaptureError("could not read %s: %s" % (path, exc.message))


class Backend:
    name = "?"
    why = ""          # filled in when unavailable, shown by `glean doctor`

    def available(self):
        return False

    def grab(self):
        """Return a Pixbuf of the whole virtual screen."""
        raise NotImplementedError


class X11Root(Backend):
    """Read the composited root window directly. No flash, no dialog, no file."""

    name = "x11-root"

    def available(self):
        display = Gdk.Display.get_default()
        if display is None:
            self.why = "no display connection"
            return False
        if is_wayland():
            # A GTK app under Wayland can open an XWayland root window, but it
            # only ever contains the X11 clients, never the Wayland ones.
            self.why = "session is Wayland; the X11 root only shows XWayland windows"
            return False
        return type(display).__name__.startswith("X11")

    def grab(self):
        root = Gdk.get_default_root_window()
        pb = Gdk.pixbuf_get_from_window(root, 0, 0,
                                        root.get_width(), root.get_height())
        if pb is None:
            raise CaptureError("the X server refused to hand over the root window")
        return pb


class GnomeShell(Backend):
    """GNOME's own screenshot method. Works under Wayland, no permission prompt."""

    name = "gnome-shell"

    def available(self):
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                "org.gnome.Shell.Screenshot", "/org/gnome/Shell/Screenshot",
                "org.gnome.Shell.Screenshot", None)
            if proxy.get_name_owner() is None:
                self.why = "org.gnome.Shell.Screenshot has no owner"
                return False
            return True
        except GLib.Error as exc:
            self.why = exc.message
            return False

    def grab(self):
        fd, path = tempfile.mkstemp(prefix="glean-", suffix=".png")
        os.close(fd)
        # GNOME 46+ only serves this API to an allowlist of well-known bus
        # names (see js/ui/screenshot.js: DBusSenderChecker). Borrow the
        # screenshot app's name for the duration of the call.
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        try:
            bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                          "org.freedesktop.DBus", "RequestName",
                          GLib.Variant("(su)", ("org.gnome.Screenshot", 1)),
                          None, Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error:
            pass
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                "org.gnome.Shell.Screenshot", "/org/gnome/Shell/Screenshot",
                "org.gnome.Shell.Screenshot", None)
            ok, used = proxy.call_sync(
                "Screenshot", GLib.Variant("(bbs)", (False, False, path)),
                Gio.DBusCallFlags.NONE, 10000, None).unpack()
            if not ok:
                raise CaptureError("gnome-shell declined to take the screenshot")
            return _load(used or path)
        except GLib.Error as exc:
            raise CaptureError(exc.message)
        finally:
            _unlink(path)
            try:
                bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "ReleaseName",
                              GLib.Variant("(s)", ("org.gnome.Screenshot",)),
                              None, Gio.DBusCallFlags.NONE, -1, None)
            except GLib.Error:
                pass


class Portal(Backend):
    """xdg-desktop-portal. The universal route, and the one that may prompt."""

    name = "portal"

    def available(self):
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot", None)
            if proxy.get_name_owner() is None:
                self.why = "no xdg-desktop-portal on the bus"
                return False
            return True
        except GLib.Error as exc:
            self.why = exc.message
            return False

    def grab(self):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        loop = GLib.MainLoop()
        result = {}

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            code, results = params.unpack()
            result["code"] = code
            result["uri"] = results.get("uri")
            loop.quit()

        token = "glean%d" % os.getpid()
        # The portal derives the request path from our bus name; subscribing to
        # the signal before the call avoids losing a fast reply.
        sender = bus.get_unique_name()[1:].replace(".", "_")
        path = "/org/freedesktop/portal/desktop/request/%s/%s" % (sender, token)
        sub = bus.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
            "Response", path, None, Gio.DBusSignalFlags.NONE, on_response)
        try:
            opts = {"handle_token": GLib.Variant("s", token),
                    "interactive": GLib.Variant("b", False)}
            bus.call_sync(
                "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot", "Screenshot",
                GLib.Variant("(sa{sv})", ("", opts)), None,
                Gio.DBusCallFlags.NONE, -1, None)
            GLib.timeout_add_seconds(60, lambda: (loop.quit(), False)[1])
            loop.run()
        except GLib.Error as exc:
            raise CaptureError(exc.message)
        finally:
            bus.signal_unsubscribe(sub)

        if result.get("code") != 0 or not result.get("uri"):
            raise CaptureError("the screenshot portal denied or cancelled the request")
        local = GLib.filename_from_uri(result["uri"])[0]
        try:
            return _load(local)
        finally:
            # The portal hands us a file in our own cache dir and expects us to
            # own it from here.
            _unlink(local)


class External(Backend):
    """Whatever screenshot binary the desktop happens to ship."""

    def __init__(self, name, argv, wayland_ok=True, x11_ok=True):
        self.name = name
        self._argv = argv
        self._wayland_ok = wayland_ok
        self._x11_ok = x11_ok

    def available(self):
        if shutil.which(self._argv[0]) is None:
            self.why = "not installed"
            return False
        if is_wayland() and not self._wayland_ok:
            self.why = "X11 only"
            return False
        if not is_wayland() and not self._x11_ok:
            self.why = "Wayland only"
            return False
        return True

    def grab(self):
        fd, path = tempfile.mkstemp(prefix="glean-", suffix=".png")
        os.close(fd)
        try:
            argv = [a.replace("{path}", path) for a in self._argv]
            proc = subprocess.run(argv, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE)
            if proc.returncode != 0 or not os.path.getsize(path):
                err = proc.stderr.decode("utf-8", "replace").strip()
                raise CaptureError("%s failed: %s" % (self.name, err or
                                                      "exit %d" % proc.returncode))
            return _load(path)
        finally:
            _unlink(path)


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def backends():
    """Every backend, best first."""
    return [
        X11Root(),
        GnomeShell(),
        Portal(),
        External("grim", ["grim", "{path}"], x11_ok=False),
        External("spectacle", ["spectacle", "-b", "-n", "-f", "-o", "{path}"]),
        External("gnome-screenshot", ["gnome-screenshot", "-f", "{path}"]),
        External("scrot", ["scrot", "-o", "-z", "{path}"], wayland_ok=False),
        External("maim", ["maim", "{path}"], wayland_ok=False),
        External("import", ["import", "-window", "root", "{path}"], wayland_ok=False),
    ]


def pick(preferred=None):
    chain = backends()
    if preferred:
        chain = [b for b in chain if b.name == preferred] or chain
    for backend in chain:
        if backend.available():
            return backend
    raise CaptureError(
        "no way to take a screenshot on this session.\n"
        "Install one of: gnome-screenshot, spectacle, scrot, maim"
        + (", grim (Wayland)" if is_wayland() else ""))


def screen(preferred=None):
    """The whole virtual screen, as a Pixbuf. Raises CaptureError."""
    last = None
    chain = [pick(preferred)] if preferred else \
        [b for b in backends() if b.available()]
    if not chain:
        pick(preferred)          # raises with the full advice text
    for backend in chain:
        try:
            return backend.grab()
        except CaptureError as exc:
            last = exc
    raise last or CaptureError("every screenshot backend failed")
