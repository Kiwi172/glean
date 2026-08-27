"""Binding glean to a key, on the desktops where that can be scripted.

GNOME keeps custom shortcuts in dconf, so this is a settings edit rather than
a global grab: nothing of ours stays resident, and the shortcut survives a
reboot.  Other desktops get told what to bind by hand.
"""

import os
import sys

from glean.gi3 import Gio

BASE = "org.gnome.settings-daemon.plugins.media-keys"
CHILD = BASE + ".custom-keybinding"
PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/glean/"
NAME = "glean (OCR a region of the screen)"


def _schema(name):
    source = Gio.SettingsSchemaSource.get_default()
    return source.lookup(name, True) if source else None


def supported():
    return _schema(BASE) is not None and _schema(CHILD) is not None


def command():
    """The command line a shortcut should run to reach this copy of glean."""
    launcher = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "glean")
    if os.path.isfile(launcher) and os.access(launcher, os.X_OK):
        return launcher
    return "%s -m glean" % sys.executable


def install(binding, argv=None):
    """Bind `binding` (e.g. '<Super><Shift>t'). Returns the command bound."""
    if not supported():
        raise RuntimeError(
            "this desktop does not keep its shortcuts in GNOME's dconf schema.\n"
            "Add one by hand, running:  %s" % (argv or command()))
    settings = Gio.Settings.new(BASE)
    existing = list(settings.get_strv("custom-keybindings"))
    if PATH not in existing:
        existing.append(PATH)
        settings.set_strv("custom-keybindings", existing)
    entry = Gio.Settings.new_with_path(CHILD, PATH)
    line = argv or command()
    entry.set_string("name", NAME)
    entry.set_string("command", line)
    entry.set_string("binding", binding)
    Gio.Settings.sync()
    return line


def uninstall():
    """Remove the shortcut. True if there was one."""
    if not supported():
        return False
    settings = Gio.Settings.new(BASE)
    existing = list(settings.get_strv("custom-keybindings"))
    if PATH not in existing:
        return False
    settings.set_strv("custom-keybindings", [p for p in existing if p != PATH])
    entry = Gio.Settings.new_with_path(CHILD, PATH)
    for key in ("name", "command", "binding"):
        entry.reset(key)
    Gio.Settings.sync()
    return True


def status():
    """(binding, command) if installed, else (None, None)."""
    if not supported():
        return None, None
    settings = Gio.Settings.new(BASE)
    if PATH not in settings.get_strv("custom-keybindings"):
        return None, None
    entry = Gio.Settings.new_with_path(CHILD, PATH)
    return entry.get_string("binding"), entry.get_string("command")


def conflicts(binding):
    """Names of GNOME shortcuts already using this key combination."""
    found = []
    for schema_name in (BASE, "org.gnome.desktop.wm.keybindings",
                        "org.gnome.shell.keybindings",
                        "org.gnome.mutter.keybindings"):
        schema = _schema(schema_name)
        if schema is None:
            continue
        settings = Gio.Settings.new(schema_name)
        for key in schema.list_keys():
            try:
                value = settings.get_value(key)
            except Exception:
                continue
            if value.get_type_string() == "as" and binding in value.unpack():
                found.append("%s %s" % (schema_name.split(".")[-1], key))
            elif value.get_type_string() == "s" and binding == value.unpack() \
                    and key == "binding":
                found.append(schema_name.split(".")[-1])
    return found
