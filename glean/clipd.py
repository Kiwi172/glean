"""A minimal clipboard holder.

X11 has no clipboard: it has a protocol for asking whichever process copied
last to hand the bytes over, which means a tool that copies and exits has
copied nothing.  GNOME ships no clipboard manager, so on a stock Ubuntu
desktop that is exactly what happens.

This is the smallest fix that needs no extra packages: a detached process that
owns the selection, serves it on request, and gets out of the way the moment
anything else copies.  It reads the text from stdin so it never appears in
`ps` output — a screen OCR tool has no business putting what it read into the
process table.
"""

import os
import sys

from glean.gi3 import Gdk, GLib, Gtk

MAX_LIFETIME = 6 * 3600


def main():
    text = sys.stdin.buffer.read().decode("utf-8", "replace")
    if not text:
        return 0
    try:
        os.setsid()
    except OSError:
        pass

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(text, -1)
    # If the session does have a manager, let it take a copy; we stay up
    # anyway, since there is no reliable way to find out whether it did.
    clipboard.store()

    def on_owner_change(_clipboard, _event):
        # We serve our own text locally, so anything else means somebody else
        # has taken the selection and our job is done.
        if clipboard.wait_for_text() != text:
            Gtk.main_quit()
        return False

    clipboard.connect("owner-change", on_owner_change)
    GLib.timeout_add_seconds(MAX_LIFETIME, Gtk.main_quit)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
