"""The window you get after a capture.

OCR is never perfectly right, so the useful thing is not a dialog reporting
what was read but an editable buffer holding it, next to the picture it came
from, with the knobs that would change the answer within reach.  Text is
already on the clipboard by the time this appears; the window is for when the
first answer is not good enough.
"""

import os
import time

from glean import clip, ocr, store
from glean.gi3 import Gdk, GdkPixbuf, GLib, Gtk, Pango

CSS = b"""
.glean-conf { padding: 2px 9px; border-radius: 9px; font-weight: bold; }
.glean-good { background: alpha(#26a269, 0.20); color: #26a269; }
.glean-fair { background: alpha(#e5a50a, 0.22); color: #c07f00; }
.glean-poor { background: alpha(#e01b24, 0.20); color: #e01b24; }
.glean-status { font-size: 90%; opacity: 0.7; }
.glean-shot { background: @theme_base_color;
              border: 1px solid alpha(@theme_fg_color, 0.15); }
"""

PSM_CHOICES = [("auto", "Automatic"), ("6", "Block of text"), ("7", "Single line"),
               ("3", "Full page"), ("4", "Column"), ("11", "Scattered text"),
               ("13", "Raw line")]


class ResultWindow(Gtk.Window):
    def __init__(self, pixbuf, result, opts):
        super().__init__(title="glean")
        self.pixbuf = pixbuf
        self.result = result
        self.opts = opts
        self.raw = result.text
        self.copied = result.text
        self._busy = False
        self._preview_width = 0

        self.set_default_size(860, 620)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("accessories-character-map")
        _install_css()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        outer.pack_start(self._toolbar(), False, False, 0)
        outer.pack_start(self._body(), True, True, 0)
        outer.pack_start(self._footer(), False, False, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("destroy", lambda *_: Gtk.main_quit())

    # ------------------------------------------------------------- widgets
    def _toolbar(self):
        bar = Gtk.Box(spacing=8, margin=10)

        self.conf_label = Gtk.Label()
        self.conf_label.get_style_context().add_class("glean-conf")
        bar.pack_start(self.conf_label, False, False, 0)

        self.count_label = Gtk.Label()
        self.count_label.get_style_context().add_class("glean-status")
        bar.pack_start(self.count_label, False, False, 0)

        bar.pack_end(self._rerun_button(), False, False, 0)

        self.psm_combo = Gtk.ComboBoxText()
        for value, label in PSM_CHOICES:
            self.psm_combo.append(value, label)
        self.psm_combo.set_active_id(str(self.opts.get("psm", "auto")))
        self.psm_combo.set_tooltip_text("How tesseract should carve up the image")
        bar.pack_end(self.psm_combo, False, False, 0)

        langs = ocr.languages() or [self.opts.get("lang", "eng")]
        self.lang_combo = Gtk.ComboBoxText()
        for code in langs:
            self.lang_combo.append(code, code)
        active = self.opts.get("lang", "eng")
        if active not in langs:
            self.lang_combo.append(active, active)
        self.lang_combo.set_active_id(active)
        self.lang_combo.set_tooltip_text("Language model")
        bar.pack_end(self.lang_combo, False, False, 0)

        self.unwrap_check = Gtk.CheckButton(label="Join wrapped lines")
        self.unwrap_check.set_tooltip_text(
            "Glue together lines the original column happened to break")
        self.unwrap_check.set_active(bool(self.opts.get("unwrap")))
        self.unwrap_check.connect("toggled", lambda *_: self._render_text())
        bar.pack_end(self.unwrap_check, False, False, 6)
        return bar

    def _rerun_button(self):
        button = Gtk.Button(label="Read again")
        button.connect("clicked", lambda *_: self._rerun())
        self.rerun_button = button
        return button

    def _body(self):
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_position(190)

        shot = Gtk.ScrolledWindow()
        shot.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        shot.get_style_context().add_class("glean-shot")
        self.image = Gtk.Image.new_from_pixbuf(self.pixbuf)
        self.image.set_margin_top(6)
        self.image.set_margin_bottom(6)
        holder = Gtk.Box()
        holder.set_center_widget(self.image)
        shot.add(holder)
        # A capture is usually wider than the window; show it whole rather than
        # behind a horizontal scrollbar, but never blow a small one up.
        shot.connect("size-allocate", self._fit_preview)
        paned.pack1(shot, False, True)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_monospace(True)
        self.view.set_left_margin(10)
        self.view.set_right_margin(10)
        self.view.set_top_margin(8)
        self.view.modify_font(Pango.FontDescription("monospace 11"))
        scroller.add(self.view)
        paned.pack2(scroller, True, False)

        self._render_text()
        self._render_confidence()
        return paned

    def _footer(self):
        bar = Gtk.Box(spacing=8, margin=10)
        self.status = Gtk.Label(xalign=0.0)
        self.status.get_style_context().add_class("glean-status")
        bar.pack_start(self.status, True, True, 0)

        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        bar.pack_end(close, False, False, 0)

        copy = Gtk.Button(label="Copy")
        copy.get_style_context().add_class("suggested-action")
        copy.connect("clicked", lambda *_: self._copy())
        bar.pack_end(copy, False, False, 0)

        save = Gtk.Button(label="Save…")
        save.connect("clicked", lambda *_: self._save())
        bar.pack_end(save, False, False, 0)
        return bar

    def _fit_preview(self, _widget, allocation):
        available = max(1, allocation.width - 16)
        width = self.pixbuf.get_width()
        height = self.pixbuf.get_height()
        if width <= available:
            target, scaled = width, self.pixbuf
        else:
            target = available
            scaled = self.pixbuf.scale_simple(
                target, max(1, int(height * target / float(width))),
                GdkPixbuf.InterpType.BILINEAR)
        if target == self._preview_width:
            return
        self._preview_width = target
        # Setting it from inside the allocation would recurse.
        GLib.idle_add(self.image.set_from_pixbuf, scaled)

    # -------------------------------------------------------------- render
    def _render_text(self):
        text = ocr.unwrap(self.raw) if self.unwrap_check.get_active() else self.raw
        self.view.get_buffer().set_text(text)
        self.copied = text

    def _render_confidence(self):
        conf = self.result.confidence
        if conf is None:
            self.conf_label.set_text("no text")
            style = "glean-poor"
        else:
            self.conf_label.set_text("%d%%" % round(conf))
            style = "glean-good" if conf >= 85 else \
                "glean-fair" if conf >= 65 else "glean-poor"
        context = self.conf_label.get_style_context()
        for name in ("glean-good", "glean-fair", "glean-poor"):
            context.remove_class(name)
        context.add_class(style)
        self.count_label.set_text(
            "%d words · psm %s · %s" % (len(self.result.words), self.result.psm,
                                        self.result.lang))

    def _flash(self, message):
        self.status.set_text(message)
        GLib.timeout_add_seconds(4, self._clear_status, message)

    def _clear_status(self, message):
        if self.status.get_text() == message:
            self.status.set_text("")
        return False

    # ------------------------------------------------------------- actions
    def text(self):
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _copy(self):
        text = self.text()
        if not text.strip():
            self._flash("nothing to copy")
            return
        self.copied = text
        clip.copy(text)
        self._flash("copied %d characters" % len(text))

    def _save(self):
        dialog = Gtk.FileChooserDialog(
            title="Save text", parent=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Save", Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name("glean-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
        dialog.set_current_folder(os.path.expanduser("~"))
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            path = dialog.get_filename()
            try:
                if path.lower().endswith(".png"):
                    self.pixbuf.savev(path, "png", [], [])
                else:
                    with open(path, "wb") as fh:
                        fh.write(self.text().encode("utf-8"))
                self._flash("saved to %s" % path)
            except (OSError, GLib.Error) as exc:
                self._flash("could not save: %s" % exc)
        dialog.destroy()

    def _rerun(self):
        if self._busy:
            return
        self._busy = True
        self.rerun_button.set_sensitive(False)
        self.status.set_text("reading…")
        # Let the label paint before tesseract blocks the loop.
        GLib.idle_add(self._rerun_now)

    def _rerun_now(self):
        psm = self.psm_combo.get_active_id() or "auto"
        lang = self.lang_combo.get_active_id() or "eng"
        try:
            self.result = ocr.read(self.pixbuf, lang=lang, psm=psm,
                                   preprocessing=self.opts.get("preprocess", "auto"))
            self.raw = self.result.text
            self._render_text()
            self._render_confidence()
            self._flash("read again with psm %s" % self.result.psm)
        except ocr.OCRError as exc:
            self._flash(str(exc).splitlines()[0])
        finally:
            self._busy = False
            self.rerun_button.set_sensitive(True)
        return False

    def _on_key(self, _widget, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        name = Gdk.keyval_name(event.keyval)
        if name == "Escape":
            self.destroy()
            return True
        if ctrl and name in ("Return", "KP_Enter"):
            self._copy()
            self.destroy()
            return True
        if ctrl and name in ("s", "S"):
            self._save()
            return True
        if ctrl and name in ("r", "R"):
            self._rerun()
            return True
        if ctrl and name in ("c", "C") and not self.view.has_focus():
            self._copy()
            return True
        return False


def _install_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def show(pixbuf, result, opts):
    """Run the result window. Returns the text as it stood when it closed."""
    window = ResultWindow(pixbuf, result, opts)
    window.show_all()
    window.present()
    window.view.grab_focus()
    Gtk.main()
    return window.copied
