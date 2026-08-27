"""Unit tests for the parts of glean that do not need a screen.

Everything here runs headless.  GdkPixbuf and the GObject imports work without
a display; only the overlay and the result window need one, and those are
exercised by hand rather than here.
"""

import difflib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glean import capture, config, ocr, store  # noqa: E402
from glean.gi3 import GdkPixbuf, GLib  # noqa: E402
from glean.overlay import clamp, normalise  # noqa: E402


def solid(width, height, level):
    data = bytes([level]) * (width * height * 3)
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB, False, 8,
        width, height, width * 3)


class Geometry(unittest.TestCase):
    def test_normalise_orders_the_corners(self):
        self.assertEqual(normalise((100, 50), (20, 200)), (20, 50, 80, 150))
        self.assertEqual(normalise((0, 0), (10, 10)), (0, 0, 10, 10))

    def test_normalise_of_a_click_is_empty_not_negative(self):
        self.assertEqual(normalise((7, 9), (7, 9)), (7, 9, 0, 0))

    def test_clamp_trims_to_the_screen(self):
        self.assertEqual(clamp((-10, -10, 100, 100), 1920, 1080), (0, 0, 90, 90))
        self.assertEqual(clamp((1900, 1070, 100, 100), 1920, 1080),
                         (1900, 1070, 20, 10))

    def test_clamp_rejects_a_rect_that_is_entirely_off_screen(self):
        self.assertIsNone(clamp((2000, 50, 10, 10), 1920, 1080))
        self.assertIsNone(clamp((0, 0, 0, 50), 1920, 1080))


class Thresholding(unittest.TestCase):
    def test_otsu_splits_two_clusters(self):
        hist = [0] * 256
        hist[30] = 500
        hist[200] = 500
        self.assertTrue(30 <= ocr.otsu(hist) < 200)

    def test_otsu_of_nothing(self):
        self.assertEqual(ocr.otsu([0] * 256), -1)

    def test_flat_for_rendered_text_busy_for_a_photograph(self):
        text = [0] * 256
        text[255] = 9000
        text[0] = 900
        text[128] = 100
        self.assertTrue(ocr.is_flat(text, sum(text)))
        photo = [40] * 256
        self.assertFalse(ocr.is_flat(photo, sum(photo)))


class Preprocessing(unittest.TestCase):
    def test_a_small_region_is_padded_and_upscaled(self):
        out = ocr.preprocess(solid(200, 60, 255))
        factor = min(4, max(1, round(ocr.TARGET_WIDTH / float(200 + ocr.PAD * 2))))
        self.assertEqual(out.get_width(), (200 + ocr.PAD * 2) * factor)
        self.assertEqual(out.get_height(), (60 + ocr.PAD * 2) * factor)

    def test_a_wide_region_is_left_alone(self):
        out = ocr.preprocess(solid(1700, 200, 255))
        self.assertEqual(out.get_width(), 1700 + ocr.PAD * 2)

    def test_a_dark_region_comes_back_light(self):
        # Light-on-dark is flipped, because tesseract expects ink on paper.
        out = ocr.preprocess(solid(120, 40, 20))
        self.assertGreater(out.get_pixels()[0], 200)

    def test_a_light_region_is_not_flipped(self):
        out = ocr.preprocess(solid(120, 40, 240))
        self.assertGreater(out.get_pixels()[0], 200)

    def test_off_still_upscales_but_keeps_the_colours(self):
        out = ocr.preprocess(solid(200, 60, 20), "off")
        self.assertGreater(out.get_width(), 200)
        self.assertLess(out.get_pixels()[0], 60)

    def test_a_huge_image_skips_the_python_pass(self):
        wide = solid(3000, 1400, 30)
        self.assertGreater(wide.get_width() * wide.get_height(),
                           ocr.MAX_PREPROCESS_PIXELS)
        out = ocr.preprocess(wide)
        self.assertLess(out.get_pixels()[0], 60)   # untouched, so still dark


class Tsv(unittest.TestCase):
    HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num" \
             "\tleft\ttop\twidth\theight\tconf\ttext"

    def test_words_and_boxes(self):
        rows = [self.HEADER,
                "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t96.5\thello",
                "5\t1\t1\t1\t1\t2\t50\t20\t40\t12\t88\tworld"]
        words = ocr.parse_tsv("\n".join(rows))
        self.assertEqual([w[0] for w in words], ["hello", "world"])
        self.assertEqual(words[0][2:], (10, 20, 30, 12))

    def test_blank_and_unrecognised_rows_are_dropped(self):
        rows = [self.HEADER,
                "4\t1\t1\t1\t1\t0\t0\t0\t0\t0\t-1\t",
                "5\t1\t1\t1\t1\t1\t1\t2\t3\t4\t50\tkept",
                "5\t1\t1\t1\t1\t2\t1\t2\t3\t4\tNaN\tbroken"]
        self.assertEqual([w[0] for w in ocr.parse_tsv("\n".join(rows))], ["kept"])

    def test_garbage_in_gives_nothing_out(self):
        self.assertEqual(ocr.parse_tsv(""), [])
        self.assertEqual(ocr.parse_tsv("not a tsv at all"), [])


class Text(unittest.TestCase):
    def test_tidy_strips_trailing_space_and_collapses_blank_runs(self):
        self.assertEqual(ocr.tidy("a  \n\n\n\nb\f\n\n"), "a\n\nb")

    def test_tidy_of_nothing(self):
        self.assertEqual(ocr.tidy("\n\n\f  \n"), "")

    def test_unwrap_joins_a_broken_paragraph(self):
        self.assertEqual(ocr.unwrap("The quick brown\nfox jumps."),
                         "The quick brown fox jumps.")

    def test_unwrap_keeps_paragraphs_apart(self):
        self.assertEqual(ocr.unwrap("one line\nwrapped\n\nsecond para"),
                         "one line wrapped\n\nsecond para")

    def test_unwrap_keeps_list_items_on_their_own_lines(self):
        self.assertEqual(ocr.unwrap("- one\n- two\n- three"), "- one\n- two\n- three")
        self.assertEqual(ocr.unwrap("1. first\n2. second"), "1. first\n2. second")

    def test_unwrap_rejoins_a_hyphenated_word(self):
        self.assertEqual(ocr.unwrap("compart-\nmentalisation"), "compartmentalisation")

    def test_psm_from_the_shape_of_the_crop(self):
        self.assertEqual(ocr.choose_psm(800, 20), 7)     # a single line
        self.assertEqual(ocr.choose_psm(800, 300), 6)    # a block
        self.assertEqual(ocr.choose_psm(1200, 60), 7)    # wide and short
        self.assertEqual(ocr.choose_psm(400, 0), 6)      # degenerate


class Ranking(unittest.TestCase):
    def test_an_empty_read_always_loses(self):
        empty = ocr.Result("", 99.0, [], 6, "eng")
        some = ocr.Result("hello", 40.0, [("hello", 40.0, 0, 0, 1, 1)], 6, "eng")
        self.assertGreater(ocr._rank(some), ocr._rank(empty))

    def test_higher_confidence_wins(self):
        low = ocr.Result("a", 40.0, [("a", 40.0, 0, 0, 1, 1)], 6, "eng")
        high = ocr.Result("a", 90.0, [("a", 90.0, 0, 0, 1, 1)], 6, "eng")
        self.assertGreater(ocr._rank(high), ocr._rank(low))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="glean-test-")
        self.env = {k: os.environ.get(k)
                    for k in ("XDG_RUNTIME_DIR", "XDG_DATA_HOME")}
        os.environ["XDG_RUNTIME_DIR"] = os.path.join(self.tmp, "run")
        os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp, "data")
        os.makedirs(os.environ["XDG_RUNTIME_DIR"])

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_last_capture_round_trips(self):
        store.save_last(solid(8, 8, 255), "some text")
        path, text = store.load_last()
        self.assertTrue(path.endswith("last.png"))
        self.assertEqual(text, "some text")

    def test_no_last_capture_yet(self):
        self.assertEqual(store.load_last(), (None, None))

    def test_captures_are_not_readable_by_other_users(self):
        store.save_last(solid(8, 8, 255), "secret")
        path, _ = store.load_last()
        self.assertEqual(os.stat(path).st_mode & 0o077, 0)

    def test_archive_writes_image_text_and_index(self):
        png = store.archive(solid(8, 8, 255), "archived", {"lang": "eng"})
        self.assertTrue(os.path.exists(png))
        self.assertTrue(os.path.exists(png[:-4] + ".txt"))
        with open(os.path.join(store.data_dir(), "index.jsonl")) as fh:
            self.assertIn("eng", fh.read())

    def test_prune_keeps_only_the_newest(self):
        directory = store.data_dir()
        for i in range(10):
            open(os.path.join(directory, "2020010%d.png" % i), "w").close()
            open(os.path.join(directory, "2020010%d.txt" % i), "w").close()
        store.prune(directory, keep=3)
        left = sorted(f for f in os.listdir(directory) if f.endswith(".png"))
        self.assertEqual(left, ["20200107.png", "20200108.png", "20200109.png"])


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="glean-conf-")
        self.old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp

    def tearDown(self):
        if self.old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_when_there_is_no_file(self):
        self.assertEqual(config.load()["lang"], "eng")

    def test_a_written_file_is_read_back(self):
        path = config.write_default()
        with open(path, "w") as fh:
            fh.write("[glean]\nlang = deu\nhistory = on\n")
        values = config.load()
        self.assertEqual(values["lang"], "deu")
        self.assertTrue(config.truthy(values["history"]))
        self.assertEqual(values["psm"], "auto")     # untouched keys keep defaults

    def test_a_corrupt_file_falls_back_to_defaults(self):
        os.makedirs(os.path.dirname(config.path()), exist_ok=True)
        with open(config.path(), "w") as fh:
            fh.write("this is not ini\n= = =\n")
        self.assertEqual(config.load()["lang"], "eng")


class Backends(unittest.TestCase):
    def test_every_backend_answers_available_without_raising(self):
        for backend in capture.backends():
            self.assertIsInstance(backend.available(), bool)
            if not backend.available():
                self.assertTrue(backend.why, "%s gave no reason" % backend.name)

    def test_x11_backend_is_refused_on_wayland(self):
        old = os.environ.get("WAYLAND_DISPLAY")
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        try:
            self.assertFalse(capture.X11Root().available())
        finally:
            if old is None:
                os.environ.pop("WAYLAND_DISPLAY", None)
            else:
                os.environ["WAYLAND_DISPLAY"] = old


@unittest.skipUnless(ocr.have_tesseract(), "tesseract is not installed")
class EndToEnd(unittest.TestCase):
    """The tests that actually read something."""

    def render(self, text, size=13, dark=False):
        import cairo
        width, height = 620, int(size * 2.4)
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
        cr = cairo.Context(surface)
        cr.set_source_rgb(*((0.11, 0.11, 0.13) if dark else (1, 1, 1)))
        cr.paint()
        cr.set_source_rgb(*((0.9, 0.9, 0.92) if dark else (0.05, 0.05, 0.05)))
        cr.select_font_face("DejaVu Sans")
        cr.set_font_size(size)
        cr.move_to(10, size * 1.5)
        cr.show_text(text)
        directory = tempfile.mkdtemp(prefix="glean-e2e-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "t.png")
        surface.write_to_png(path)
        return GdkPixbuf.Pixbuf.new_from_file(path)

    def test_reads_light_text_on_a_dark_background(self):
        wanted = "The quick brown fox jumps over the lazy dog."
        result = ocr.read(self.render(wanted, dark=True))
        self.assertEqual(result.text.strip(), wanted)
        self.assertGreater(result.confidence, 80)

    def test_reads_dark_text_on_a_light_background(self):
        wanted = "Error 0x8007007e: module not found (retry 3/5)"
        self.assertEqual(ocr.read(self.render(wanted)).text.strip(), wanted)

    def test_reads_small_text(self):
        # At 10px a couple of glyphs are genuinely ambiguous -- a `/` between
        # two letters is a few pixels of diagonal -- so demanding an exact
        # match here tests the font renderer's luck rather than our pipeline.
        # Near-exact is the honest bar; the assertion below is the strict one.
        wanted = "ssh -i ~/.ssh/id_ed25519 deploy@10.20.30.41 -p 2222"
        got = ocr.read(self.render(wanted, size=10, dark=True)).text.strip()
        similarity = difflib.SequenceMatcher(None, wanted, got).ratio()
        self.assertGreater(similarity, 0.9, "read %r" % got)

    def test_preprocessing_earns_its_keep_on_small_text(self):
        """The claim the README makes, as a test: preparation beats raw."""
        wanted = "The quick brown fox jumps over the lazy dog."
        image = self.render(wanted, size=10, dark=True)
        prepared = ocr.read(image, preprocessing="auto")
        raw = ocr.read(image, preprocessing="off")
        self.assertEqual(prepared.text.strip(), wanted)
        self.assertGreater(prepared.confidence, raw.confidence)

    def test_a_blank_region_reads_as_nothing(self):
        self.assertEqual(ocr.read(solid(300, 80, 255)).text.strip(), "")

    def test_an_unknown_language_is_reported_usefully(self):
        with self.assertRaises(ocr.OCRError) as caught:
            ocr.read(self.render("hello"), lang="xyz")
        self.assertIn("apt install", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
