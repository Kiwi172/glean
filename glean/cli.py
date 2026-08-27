"""Command line front end."""

import argparse
import os
import sys
import time

from glean import __version__

USAGE = """glean [options]              drag a box round some text and read it
  glean --full               read the whole screen
  glean --file shot.png      read an image file
  glean --last               reopen the previous capture
  glean doctor               check what works on this machine
  glean hotkey [--bind KEY]  bind glean to a key (GNOME)
  glean hotkey --remove      unbind it
  glean config               write a config file and print where it is"""

COMMANDS = ("doctor", "hotkey", "config")


def build_parser(defaults):
    parser = argparse.ArgumentParser(
        prog="glean", usage=USAGE,
        description="Pull the text off your screen.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=COMMANDS, help=argparse.SUPPRESS)

    what = parser.add_argument_group("what to read")
    what.add_argument("-f", "--full", action="store_true",
                      help="the whole screen, without asking")
    what.add_argument("--file", metavar="IMG",
                      help="an image file instead of the screen")
    what.add_argument("--last", action="store_true",
                      help="the previous capture, from this session")
    what.add_argument("-d", "--delay", type=float, default=0, metavar="SEC",
                      help="wait before grabbing, to open a menu first")

    how = parser.add_argument_group("how to read it")
    how.add_argument("-l", "--lang", default=defaults["lang"],
                     help="tesseract language, e.g. eng or deu+eng "
                          "(default: %(default)s)")
    how.add_argument("-p", "--psm", default=defaults["psm"],
                     help="page segmentation: auto, or a tesseract psm number")
    how.add_argument("--pre", default=defaults["preprocess"],
                     choices=("auto", "binarise", "off"),
                     help="image preparation (default: %(default)s)")
    how.add_argument("-u", "--unwrap", action="store_true",
                     default=_flag(defaults["unwrap"]),
                     help="join lines that a narrow column broke")
    how.add_argument("--backend", default=defaults["backend"] or None,
                     help="force a screenshot backend; see `glean doctor`")

    out = parser.add_argument_group("what to do with it")
    out.add_argument("-r", "--raw", action="store_true",
                     help="print the text and exit, no window")
    out.add_argument("-c", "--copy", action="store_true",
                     help="copy and notify, no window")
    out.add_argument("-n", "--no-copy", action="store_true",
                     help="do not touch the clipboard")
    out.add_argument("-o", "--output", metavar="FILE", help="also write the text here")
    out.add_argument("--png", metavar="FILE", help="also write the captured image here")
    out.add_argument("-k", "--keep", action="store_true",
                     default=_flag(defaults["history"]),
                     help="archive this capture under ~/.local/share/glean")

    parser.add_argument("--bind", metavar="KEY", default=defaults["hotkey"],
                        help=argparse.SUPPRESS)
    parser.add_argument("--remove", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-V", "--version", action="version",
                        version="glean %s" % __version__)
    return parser


def _flag(value):
    return str(value).strip().lower() in ("1", "on", "yes", "true")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from glean import config
    defaults = config.load()
    args = build_parser(defaults).parse_args(argv)

    if args.command == "doctor":
        return doctor()
    if args.command == "config":
        return write_config()
    if args.command == "hotkey":
        return manage_hotkey(args)
    return grab(args)


# ------------------------------------------------------------------ grab ---

def grab(args):
    from glean import capture, clip, notify, ocr, overlay, store
    from glean.gi3 import GdkPixbuf, GLib

    if not ocr.have_tesseract():
        return fail("tesseract is not installed.\n"
                    "  sudo apt install tesseract-ocr", args)

    try:
        pixbuf, source = source_image(args, capture, overlay, store, GdkPixbuf)
    except KeyboardInterrupt:
        return 2
    except (capture.CaptureError, GLib.Error) as exc:
        return fail(getattr(exc, "message", str(exc)), args)
    if pixbuf is None:
        return 2                      # cancelled; not an error

    try:
        result = ocr.read(pixbuf, lang=args.lang, psm=args.psm,
                          preprocessing=args.pre)
    except ocr.OCRError as exc:
        return fail(str(exc), args)

    text = ocr.unwrap(result.text) if args.unwrap else result.text
    store.save_last(pixbuf, text)
    if args.keep:
        try:
            store.archive(pixbuf, text, {"lang": result.lang, "psm": result.psm,
                                         "source": source})
        except (OSError, GLib.Error) as exc:
            warn("could not archive: %s" % exc)

    if args.output:
        try:
            with open(args.output, "wb") as fh:
                fh.write(text.encode("utf-8"))
        except OSError as exc:
            warn("could not write %s: %s" % (args.output, exc))
    if args.png:
        try:
            pixbuf.savev(args.png, "png", [], [])
        except GLib.Error as exc:
            warn("could not write %s: %s" % (args.png, exc))

    if not args.no_copy and text.strip():
        clip.copy(text)

    if args.raw:
        sys.stdout.write(text + ("\n" if text and not text.endswith("\n") else ""))
        sys.stdout.flush()
    elif args.copy:
        if text.strip():
            notify.send("Copied %d characters" % len(text),
                        _preview(text) + _confidence_note(result))
        else:
            notify.send("No text found", "glean read nothing in that region.",
                        icon="dialog-warning")
    else:
        from glean import ui
        final = ui.show(pixbuf, result, {"lang": args.lang, "psm": args.psm,
                                         "preprocess": args.pre,
                                         "unwrap": args.unwrap})
        if not args.no_copy and final.strip():
            clip.copy(final)

    clip.hand_off()
    return 0 if text.strip() else 1


def source_image(args, capture, overlay, store, GdkPixbuf):
    """(pixbuf to read, a word describing where it came from)."""
    if args.file:
        return GdkPixbuf.Pixbuf.new_from_file(args.file), "file"
    if args.last:
        path, _ = store.load_last()
        if not path:
            raise capture.CaptureError("there is no previous capture in this session")
        return GdkPixbuf.Pixbuf.new_from_file(path), "last"
    if args.delay:
        time.sleep(args.delay)
    if args.full:
        return capture.screen(args.backend), "screen"

    screen = capture.screen(args.backend)
    screen, rect = overlay.select(screen)
    if rect is None:
        return None, "cancelled"
    return overlay.crop(screen, rect), "region"


def _preview(text, limit=180):
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _confidence_note(result):
    if result.confidence is None or result.confidence >= 80:
        return ""
    return "\n\nConfidence %d%% — run glean without --copy to correct it." % \
        round(result.confidence)


# ---------------------------------------------------------------- doctor ---

def doctor():
    from glean import capture, clip, hotkey, ocr, store
    from glean.gi3 import Gdk

    print("glean %s" % __version__)
    session = "wayland" if capture.is_wayland() else "x11"
    print("  session         %s, %s" % (session,
                                        os.environ.get("XDG_CURRENT_DESKTOP", "?")))
    display = Gdk.Display.get_default()
    print("  display         %s" % ("connected" if display
                                     else "NONE — is DISPLAY set?"))

    print("\nscreenshot backends")
    working = None
    for backend in capture.backends():
        if not backend.available():
            print("  %-16s unavailable (%s)" % (backend.name, backend.why))
            continue
        started = time.time()
        try:
            pixbuf = backend.grab()
            ms = (time.time() - started) * 1000
            mark = "ok" if working else "ok, chosen"
            working = working or backend.name
            print("  %-16s %s — %dx%d in %dms" % (backend.name, mark,
                                                  pixbuf.get_width(),
                                                  pixbuf.get_height(), ms))
        except capture.CaptureError as exc:
            print("  %-16s FAILS (%s)" % (backend.name, str(exc).splitlines()[0]))
    if not working:
        print("  none — glean cannot see the screen")

    print("\nocr")
    if ocr.have_tesseract():
        langs = ocr.languages()
        print("  engine          %s" % ocr.version())
        print("  languages       %s" % (", ".join(langs) or "none installed"))
        if len(langs) <= 1:
            print("                  more: sudo apt install tesseract-ocr-deu "
                  "(and so on)")
    else:
        print("  engine          MISSING — sudo apt install tesseract-ocr")

    print("\nclipboard")
    helper = clip.helper_name()
    if helper:
        print("  %-16s via %s" % ("persistence", helper))
    else:
        print("  %-16s via glean's own holder process" % "persistence")
        print("                  (install xclip or wl-clipboard to use those instead)")

    print("\nshortcut")
    binding, line = hotkey.status()
    if binding:
        print("  bound           %s → %s" % (binding, line))
    elif hotkey.supported():
        print("  not bound       glean hotkey --bind '<Super><Shift>t'")
    else:
        print("  unsupported     bind %s by hand in your desktop settings"
              % hotkey.command())

    print("\nfiles")
    from glean import config
    print("  config          %s%s" % (config.path(),
                                      "" if os.path.exists(config.path())
                                      else "  (not written; run: glean config)"))
    print("  last capture    %s" % store.runtime_dir())
    return 0 if working and ocr.have_tesseract() else 1


# ---------------------------------------------------------------- config ---

def write_config():
    from glean import config
    path = config.write_default()
    print(path)
    return 0


def manage_hotkey(args):
    from glean import hotkey
    if args.remove:
        print("shortcut removed" if hotkey.uninstall() else "no shortcut was set")
        return 0
    binding = args.bind
    try:
        clashes = hotkey.conflicts(binding)
        if clashes:
            warn("%s is already used by: %s" % (binding, ", ".join(clashes)))
            warn("pick another with --bind, or clear that one in Settings first")
            return 1
        line = hotkey.install(binding)
    except RuntimeError as exc:
        return fail(str(exc), None)
    print("%s now runs %s" % (binding, line))
    return 0


# ----------------------------------------------------------------- errors ---

def warn(message):
    sys.stderr.write("glean: %s\n" % message)


def fail(message, args=None):
    warn(message)
    if args is not None and not args.raw:
        try:
            from glean import notify
            notify.send("glean failed", message.splitlines()[0],
                        icon="dialog-error", urgent=True)
        except Exception:
            pass
    return 1
