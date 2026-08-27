"""Turning a crop of the screen into text.

Tesseract is very good at scanned pages and merely alright at a 12px UI font
on a dark background, so most of this file is about handing it something that
looks more like a scanned page: normalised to dark-on-light, binarised when
the image is flat UI text rather than a photograph, padded, and scaled up.
The heavy pixel work is done in `bytes.translate` and GdkPixbuf's scaler; the
only Python-level loop is the greyscale pass, which runs before upscaling and
so stays proportional to the size of the region you actually selected.
"""

import math
import os
import shutil
import subprocess
import tempfile

from glean.gi3 import GdkPixbuf, GLib

# Above this, skip the Python greyscale pass and let tesseract do its own
# thing; the preprocessing would cost more than it buys back.
MAX_PREPROCESS_PIXELS = 4_000_000

PAD = 20            # quiet border, in pixels of the pre-scaled image
TARGET_WIDTH = 1600  # upscale small regions towards roughly this wide


class OCRError(Exception):
    pass


class Result:
    def __init__(self, text, confidence, words, psm, lang):
        self.text = text
        self.confidence = confidence   # mean word confidence, 0-100, or None
        self.words = words             # [(text, conf, x, y, w, h)]
        self.psm = psm
        self.lang = lang

    def __repr__(self):
        return "<Result %d words, conf %s, psm %s>" % (
            len(self.words), self.confidence, self.psm)


def have_tesseract():
    return shutil.which("tesseract") is not None


def languages():
    if not have_tesseract():
        return []
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                             timeout=15).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    # First line is a header; osd is an orientation model, not a language.
    return sorted(l.strip() for l in out.splitlines()[1:]
                  if l.strip() and l.strip() != "osd")


def version():
    try:
        out = subprocess.run(["tesseract", "--version"], capture_output=True,
                             timeout=15).stdout.decode("utf-8", "replace")
        return out.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown"


# ---------------------------------------------------------------- pixels ---

def _greyscale(pixbuf):
    """(bytes of one byte per pixel, width, height)."""
    w, h = pixbuf.get_width(), pixbuf.get_height()
    nc = pixbuf.get_n_channels()
    rs = pixbuf.get_rowstride()
    px = pixbuf.get_pixels()
    # ITU-R BT.601 luma, via lookup tables so the inner loop is three adds.
    tr = [(i * 299) // 1000 for i in range(256)]
    tg = [(i * 587) // 1000 for i in range(256)]
    tb = [(i * 114) // 1000 for i in range(256)]
    out = bytearray(w * h)
    for y in range(h):
        row = px[y * rs:y * rs + w * nc]
        out[y * w:(y + 1) * w] = bytes(
            map(lambda t: tr[t[0]] + tg[t[1]] + tb[t[2]],
                zip(row[0::nc], row[1::nc], row[2::nc])))
    return bytes(out), w, h


def _histogram(grey):
    return [grey.count(i) for i in range(256)]


def otsu(hist):
    """The threshold that best splits the histogram in two. -1 if it can't."""
    total = sum(hist)
    if total == 0:
        return -1
    sum_all = sum(i * n for i, n in enumerate(hist))
    sum_bg = 0.0
    count_bg = 0
    best_var, best_t = -1.0, -1
    for t in range(256):
        count_bg += hist[t]
        if count_bg == 0:
            continue
        count_fg = total - count_bg
        if count_fg == 0:
            break
        sum_bg += t * hist[t]
        mean_bg = sum_bg / count_bg
        mean_fg = (sum_all - sum_bg) / count_fg
        var = count_bg * count_fg * (mean_bg - mean_fg) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def is_flat(hist, total, share=0.7, levels=10):
    """True for UI text and documents, false for photographs.

    Rendered text has a handful of dominant levels — background, foreground and
    the antialiasing between them — so a few of them account for most of the
    image.  Counting *how few levels it takes to cover most of the pixels* is
    the question; counting how many bins are individually busy is not, because
    a flat spread over the whole range answers that one the same way a page of
    text does.
    """
    if total == 0:
        return False
    covered = 0
    used = 0
    for count in sorted(hist, reverse=True):
        covered += count
        used += 1
        if covered >= total * share:
            break
    return used <= levels


def _pad_and_scale(grey, w, h, bg):
    """Pixbuf: the greyscale bytes, quiet border added, upscaled if small."""
    pw, ph = w + PAD * 2, h + PAD * 2
    row = bytearray([bg]) * pw
    buf = bytearray()
    buf += row * PAD
    for y in range(h):
        buf += row[:PAD]
        buf += grey[y * w:(y + 1) * w]
        buf += row[:PAD]
    buf += row * PAD
    # GdkPixbuf has no 8-bit greyscale, so widen to RGB.
    rgb = bytearray(len(buf) * 3)
    rgb[0::3] = buf
    rgb[1::3] = buf
    rgb[2::3] = buf
    pb = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(rgb)), GdkPixbuf.Colorspace.RGB, False, 8,
        pw, ph, pw * 3)
    return _upscale(pb)


def _upscale(pb):
    w, h = pb.get_width(), pb.get_height()
    if w <= 0 or h <= 0:
        return pb
    factor = min(4, max(1, round(TARGET_WIDTH / float(w))))
    if factor == 1:
        return pb
    return pb.scale_simple(w * factor, h * factor, GdkPixbuf.InterpType.HYPER)


def preprocess(pixbuf, mode="auto"):
    """Normalise a crop for OCR.

    mode: auto | binarise | off.  `auto` converts to greyscale, flips
    light-on-dark round, pads and upscales.  It does *not* binarise: measured
    against real UI text, thresholding costs about eight points of confidence
    because it eats the antialiasing that tells an LSTM a `/` from an `f`.
    `binarise` is kept for images the automatic path visibly mangles.
    """
    if mode == "off":
        return _upscale(pixbuf)
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w * h > MAX_PREPROCESS_PIXELS:
        return _upscale(pixbuf)

    grey, w, h = _greyscale(pixbuf)
    hist = _histogram(grey)
    total = w * h
    background = max(range(256), key=lambda i: hist[i])

    table = list(range(256))
    if background < 110:
        # Light text on a dark background: tesseract wants the other way round.
        table = [255 - i for i in table]
        hist = hist[::-1]
        background = 255 - background

    if mode == "binarise" and is_flat(hist, total):
        threshold = otsu(hist)
        if threshold >= 0:
            table = [0 if table[i] <= threshold else 255 for i in range(256)]
            background = 255

    grey = grey.translate(bytes(table))
    return _pad_and_scale(grey, w, h, background)


# ------------------------------------------------------------ tesseract ---

def parse_tsv(tsv):
    """[(text, conf, x, y, w, h)] for the words tesseract was sure enough about."""
    words = []
    lines = tsv.splitlines()
    if not lines:
        return words
    header = lines[0].split("\t")
    try:
        col = {name: header.index(name) for name in
               ("left", "top", "width", "height", "conf", "text")}
    except ValueError:
        return words
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) <= col["text"]:
            continue
        text = parts[col["text"]].strip()
        if not text:
            continue
        try:
            conf = float(parts[col["conf"]])
            box = tuple(int(parts[col[k]]) for k in ("left", "top", "width", "height"))
        except ValueError:
            continue
        # -1 is tesseract's "this row is structure, not a word"; NaN and inf
        # are not values it emits, so anything non-finite is a corrupt row.
        if not math.isfinite(conf) or conf < 0:
            continue
        words.append((text, conf) + box)
    return words


def tidy(text):
    """Trailing whitespace off, runs of blank lines down to one."""
    out = []
    blanks = 0
    for line in text.replace("\f", "\n").splitlines():
        line = line.rstrip()
        if line:
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks == 1 and out:
                out.append("")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def unwrap(text):
    """Join lines that a narrow column broke, keeping real paragraph breaks."""
    paragraphs = []
    current = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(current)
                current = []
            continue
        # A line that starts a list item or is indented is its own line.
        starts_item = stripped[:2] in ("- ", "* ", "• ") or (
            stripped[0].isdigit() and stripped[1:2] in (".", ")"))
        if current and not starts_item and not line[:1].isspace():
            previous = current[-1]
            if previous.endswith("-") and len(previous) > 1:
                current[-1] = previous[:-1] + stripped
                continue
            current[-1] = previous + " " + stripped
            continue
        current.append(stripped if starts_item else line.strip())
    if current:
        paragraphs.append(current)
    return "\n\n".join("\n".join(p) for p in paragraphs)


def choose_psm(width, height):
    """A first guess at the page segmentation mode from the shape of the crop."""
    if height <= 0:
        return 6
    if height < 40 or width / float(height) > 12:
        return 7        # one line
    return 6            # one uniform block


def _run(path, lang, psm, timeout):
    base = path[:-4] if path.endswith(".png") else path + ".out"
    argv = ["tesseract", path, base, "-l", lang, "--psm", str(psm),
            "--oem", "1", "txt", "tsv"]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout,
                              env=dict(os.environ, OMP_THREAD_LIMIT="1"))
    except FileNotFoundError:
        raise OCRError("tesseract is not installed.\n"
                       "  sudo apt install tesseract-ocr")
    except subprocess.TimeoutExpired:
        raise OCRError("tesseract took longer than %ds and was killed" % timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        if "Failed loading language" in err or "Error opening data file" in err:
            raise OCRError(
                "the '%s' language data is not installed.\n"
                "  sudo apt install tesseract-ocr-%s" % (lang, lang.split("+")[0]))
        raise OCRError(err or "tesseract exited %d" % proc.returncode)
    try:
        with open(base + ".txt", "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
        with open(base + ".tsv", "rb") as fh:
            tsv = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        raise OCRError("tesseract wrote no output: %s" % exc)
    finally:
        for suffix in (".txt", ".tsv"):
            try:
                os.unlink(base + suffix)
            except OSError:
                pass
    return tidy(text), parse_tsv(tsv)


def _score(words):
    if not words:
        return -1.0
    return sum(c for _, c, *_ in words) / len(words)


def read(pixbuf, lang="eng", psm="auto", preprocessing="auto", timeout=60):
    """OCR a Pixbuf. Returns a Result; raises OCRError."""
    if not have_tesseract():
        raise OCRError("tesseract is not installed.\n"
                       "  sudo apt install tesseract-ocr")
    prepared = preprocess(pixbuf, preprocessing)
    fd, path = tempfile.mkstemp(prefix="glean-ocr-", suffix=".png")
    os.close(fd)
    try:
        prepared.savev(path, "png", [], [])
        if psm == "auto":
            modes = [choose_psm(pixbuf.get_width(), pixbuf.get_height())]
            modes.append(3 if modes[0] != 3 else 6)
        else:
            modes = [int(psm)]

        best = None
        for mode in modes:
            text, words = _run(path, lang, mode, timeout)
            candidate = Result(text, _score(words) if words else None,
                               words, mode, lang)
            if best is None or _rank(candidate) > _rank(best):
                best = candidate
            # A confident first answer means the fallback is not worth 200ms.
            if best.confidence is not None and best.confidence >= 80 and best.text:
                break
        return best
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _rank(result):
    """Confidence, but a result with no text at all always loses."""
    if not result.text.strip():
        return -1.0
    return (result.confidence or 0.0) + min(len(result.words), 20) * 0.1
