"""Putting text on the clipboard so that it is still there afterwards.

On X11 and Wayland alike the process that copied serves the data on demand, so
copying and exiting copies nothing.  Preferred order: hand the text to a helper
that daemonises itself (wl-copy, xclip), otherwise hold the selection in a
child of our own.
"""

import os
import shutil
import subprocess
import sys

import glean
from glean.gi3 import Gdk, Gtk

_pending = None


def _helper():
    for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                 ["xsel", "--clipboard", "--input"]):
        if shutil.which(argv[0]):
            return argv
    return None


def _feed(argv, text):
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    proc.stdin.write(text.encode("utf-8"))
    proc.stdin.close()
    return proc


def copy(text):
    """Put text on the clipboard. Call hand_off() before exiting.

    Returns the mechanism used: an external helper's name, or "self".
    """
    global _pending
    if not text:
        return None
    argv = _helper()
    if argv:
        try:
            _feed(argv, text)
            _pending = None
            return argv[0]
        except (OSError, subprocess.SubprocessError):
            pass
    # Own it locally so the text is available immediately, even while this
    # process is still in the foreground with a window open.
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(text, -1)
    _pending = text
    return "self"


def hand_off():
    """Pass ownership to a detached holder so the text outlives this process."""
    global _pending
    if not _pending:
        return False
    root = os.path.dirname(os.path.dirname(os.path.abspath(glean.__file__)))
    bootstrap = ("import sys; sys.path.insert(0, %r); "
                 "from glean.clipd import main; sys.exit(main())" % root)
    try:
        _feed([sys.executable, "-c", bootstrap], _pending)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        _pending = None
    return True


def helper_name():
    argv = _helper()
    return argv[0] if argv else None
