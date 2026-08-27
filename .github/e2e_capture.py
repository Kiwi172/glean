"""Put known text on a real X server, screenshot it, and read it back.

The unit tests hand images straight to the OCR pipeline, which leaves the half
of glean that talks to the display server untested -- exactly the half that
breaks when a distribution changes its screenshot backend.  This draws a phrase
onto the screen, takes a picture of it through the same backend chain glean
uses in anger, crops it the way a selection would, and checks the words come
back.

It needs a display: CI gives it Xvfb.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glean import capture, ocr, overlay          # noqa: E402
from glean.gi3 import GLib, Gtk                  # noqa: E402

PHRASE = "Glean capture probe 42"
BOX = (60, 60, 900, 180)     # x, y, w, h -- where the text is put and looked for


def draw(_widget, cr):
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.set_source_rgb(0, 0, 0)
    cr.select_font_face("DejaVu Sans")
    cr.set_font_size(46)
    cr.move_to(30, 110)
    cr.show_text(PHRASE)
    draw.count += 1
    return False


draw.count = 0


def normalise(text):
    return " ".join(text.split()).casefold()


def main():
    x, y, width, height = BOX
    # A POPUP maps and draws with no window manager running, which is what CI
    # has; a normal toplevel may never be mapped at all.
    window = Gtk.Window(type=Gtk.WindowType.POPUP)
    area = Gtk.DrawingArea()
    area.connect("draw", draw)
    window.add(area)
    window.set_default_size(width, height)
    window.move(x, y)
    window.show_all()

    loop = GLib.MainLoop()
    GLib.timeout_add(800, loop.quit)
    loop.run()

    if not draw.count:
        print("FAIL: the probe window never drew; is there a usable display?")
        return 1

    try:
        screen = capture.screen()
    except capture.CaptureError as exc:
        print("FAIL: could not screenshot the display: %s" % exc)
        return 1
    print("captured %dx%d" % (screen.get_width(), screen.get_height()))

    region = overlay.crop(screen, BOX)
    if region is None:
        print("FAIL: the crop fell outside the screen")
        return 1

    result = ocr.read(region)
    window.destroy()

    got = normalise(result.text)
    print("read back: %r  (confidence %s)"
          % (result.text.strip(), round(result.confidence or -1, 1)))
    if normalise(PHRASE) not in got:
        print("FAIL: expected to find %r" % PHRASE)
        return 1
    print("OK: capture and OCR agree on what is on screen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
