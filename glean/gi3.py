"""Pin GTK 3 before anything imports it.

GTK 4 is installed alongside GTK 3 on current Ubuntu, and PyGObject resolves
whichever namespace is asked for first.  A module that imports `Gdk` without
pinning gets 4.0 and poisons the process for every later import, so every
module in glean takes its GObject names from here and nowhere else.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402,F401
