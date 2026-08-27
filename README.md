# glean

[![tests](https://github.com/Kiwi172/glean/actions/workflows/ci.yml/badge.svg)](https://github.com/Kiwi172/glean/actions/workflows/ci.yml)

**Drag a box round some text on the screen and it is on your clipboard.**

Text on a screen is often not text: it is in a screenshot someone pasted into
chat, a video call, a PDF that renders as images, an installer dialog, a VM
console, a device's web UI that will not let you select anything. You can
retype it, or you can `glean` it.

```
  ┌──────────────────────────────────────────────────────────────┐
  │   the screen, frozen and dimmed                              │
  │                                                              │
  │        ┌ 770 × 190 ───────────────────────┐                  │
  │        │  Error 0x8007007e: module not    │  ← drag over it  │
  │        │  found (retry 3/5)               │                  │
  │        └──────────────────────────────────┘                  │
  │                                                        ┌───┐ │
  │   drag to select · click a window · Esc to cancel      │ ⊹ │ │
  └────────────────────────────────────────────────────────┴───┴─┘
                              │                        8× loupe
                              ▼
             tesseract  ──▶  clipboard  +  an editable window
                                            for when it got a letter wrong
```

Everything happens on your machine. No account, no upload, no network calls at
all — the only process glean starts other than itself is `tesseract`.

---

## Install

**Ubuntu 22.04 or newer**, or any distribution with GTK 3 and Python 3.8+.
Two system packages, both in the archive:

```bash
sudo apt install tesseract-ocr python3-gi gir1.2-gtk-3.0
```

Then take the code and run it. There is nothing to build:

```bash
./bin/glean doctor
```

That prints what works on your machine — which screenshot backend it will use,
which languages tesseract has, whether the clipboard will survive. If it is
happy, bind it to a key:

```bash
./bin/glean hotkey --bind '<Super><Shift>t'
```

On GNOME that writes a custom shortcut into dconf and nothing stays resident;
it survives a reboot, and `glean hotkey --remove` takes it away again. On other
desktops, bind the command it prints by hand.

Installing it properly is optional and works the usual way:

```bash
pip install --user .
```

### Languages

Only English is installed by default. Others are one package each, and you can
stack them:

```bash
sudo apt install tesseract-ocr-deu tesseract-ocr-fra
glean --lang deu+eng
```

---

## Using it

Run `glean`, drag a box, and the text is on your clipboard.

| In the overlay | |
|---|---|
| drag | select that region |
| click | select the window under the pointer |
| `a` | the whole screen |
| `Esc`, right-click | cancel |

A window then opens with the text in it, already copied, next to the image it
came from. OCR gets things wrong — a `/` becomes an `f`, a `(` becomes a `{` —
so the text is editable, and the controls that would change the answer are
along the top: the language, how tesseract should carve up the image, and
whether to glue together lines that a narrow column happened to break.

| In the window | |
|---|---|
| `Ctrl+C` | copy |
| `Ctrl+Enter` | copy and close |
| `Ctrl+S` | save to a file |
| `Ctrl+R` | read it again with the current settings |
| `Esc` | close |

### From a script

```bash
glean --raw | grep -i error        # print the text, no window
glean --copy                       # copy and notify, no window
glean --full --raw                 # the whole screen
glean --file screenshot.png --raw  # an image you already have
glean --delay 3                    # time to open a menu first
glean --last                       # the previous capture, without re-grabbing
```

`--raw` exits 0 when it read something and 1 when it did not, so
`glean --raw && ...` does the obvious thing. Cancelling exits 2.

Defaults live in `~/.config/glean/config.ini`; run `glean config` to write one.

---

## How it reads better than the obvious version

Tesseract is built for scanned pages and is only alright at 11px UI text, so
glean hands it something that looks more like a scanned page. Measured, not
guessed at.

On one 770×190 grab of real UI text, mean word confidence out of tesseract:

| | confidence |
|---|---|
| the crop, as-is | 35.5 |
| upscaled | 90.6 |
| greyscale, flipped to dark-on-light, padded, upscaled | **91.8** |
| ...and binarised (Otsu) | 83.5 |

So: greyscale, invert if the background is dark, add a quiet border, and
upscale towards 1600px wide. **No thresholding.** Binarising is the thing
everyone reaches for and it measurably hurts — it eats the antialiasing that
tells the model a `/` from an `f`. It is still there as `--pre binarise` for
images the automatic path mangles.

The greyscale pass is plain Python, which is why it runs *before* the upscale:
it is proportional to the region you selected, not to the enlarged copy. The
whole preparation step costs about 80ms on a selection that size. Everything
after it is C.

glean also picks the page segmentation mode from the shape of your selection —
a short wide crop is one line, a tall one is a block — reads it, and if the
result comes back weak, tries again as a full page and keeps whichever
tesseract was more confident about.

---

## The clipboard, and why there is a second process

X11 has no clipboard. It has a protocol for asking whichever program copied
last to hand the bytes over, which means a tool that copies and exits has
copied nothing at all. GNOME ships no clipboard manager, so on a stock Ubuntu
desktop that is exactly what happens.

If you have `xclip` or `wl-clipboard` installed, glean gives the text to those
and they deal with it. If you do not, glean leaves behind a small process of
its own that owns the selection, serves it when asked, and exits the moment
anything else copies. It reads the text over a pipe rather than an argument,
so what you just OCR'd does not turn up in `ps`.

---

## What it keeps

The last capture — the image and its text — goes in `$XDG_RUNTIME_DIR/glean`,
which is tmpfs and is emptied when your session ends. That is what `--last`
reads.

**Nothing else is stored unless you ask for it.** A screen OCR tool that
quietly archives every region you point it at is a liability on a machine that
handles anything sensitive, so the durable history is off. Switch it on with
`--keep`, or `history = on` in the config, and captures go to
`~/.local/share/glean/history`, mode 0600, oldest pruned past 200.

---

## Limitations

**Wayland is worse, and not by glean's choice.** A Wayland client cannot read
the screen; it has to ask the compositor. glean tries GNOME's own screenshot
interface and then the xdg-desktop-portal, which may show a permission dialog
each time depending on your version. The selection overlay also covers only
one monitor there, because Wayland does not let a window place itself. On X11
everything works with no prompt and no flash: the root window is read
directly, in about 50ms.

**It reads what is rendered.** Text under a translucent panel, mid-animation,
or scaled down to 8px reads badly because it looks bad. Zoom the source in
first and the confidence jumps.

**Tesseract is a 2016-era LSTM.** It is very good on prose and clean UI text,
poor on handwriting, and it will invent plausible characters in dense tables
and box-drawing. The confidence badge in the window tells you when to distrust
it: green is fine, amber is worth reading over, red usually means the wrong
segmentation mode — change it and press `Ctrl+R`.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

41 tests, no display needed. The OCR ones render text with cairo and read it
back, so they check the real pipeline rather than a mock of it; they skip
themselves if tesseract is not installed.

CI runs them on Ubuntu 22.04 and 24.04 under Xvfb, against each image's system
Python and system PyGObject — the arrangement glean is actually installable in.
It also checks that nothing has crept in from PyPI, that the OCR tests did not
quietly skip, and that `.github/e2e_capture.py` can put a phrase on the screen,
screenshot it, and read it back.

---

## Donations

Entirely optional, and nothing in glean behaves differently either way.

Monero:

```
8ACg45xXbJDMn68kJZzofM8JcUzbtasRXed45SnKXpfdXFSay19GSYd4kngj6ex6uqFBmxE3d81JtD3qpPq9ydzBDHbETXf
```

---

## Licence

MIT — see [LICENSE](LICENSE).
