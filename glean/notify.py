"""A desktop notification, without pulling in a notification library."""

import shutil
import subprocess

from glean.gi3 import Gio, GLib

APP = "glean"
_last_id = 0


def send(summary, body="", icon="edit-paste", urgent=False):
    global _last_id
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        hints = {"urgency": GLib.Variant("y", 2 if urgent else 1),
                 "transient": GLib.Variant("b", not urgent)}
        reply = bus.call_sync(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)",
                         (APP, _last_id, icon, summary, body, [], hints,
                          5000 if not urgent else 0)),
            None, Gio.DBusCallFlags.NONE, 3000, None)
        _last_id = reply.unpack()[0]
        return True
    except GLib.Error:
        pass
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", APP, summary, body],
                           stderr=subprocess.DEVNULL, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            pass
    return False
