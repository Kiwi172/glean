"""~/.config/glean/config.ini, and the defaults it overrides."""

import configparser
import os

DEFAULTS = {
    "lang": "eng",
    "psm": "auto",
    "preprocess": "auto",
    "history": "off",
    "unwrap": "off",
    "backend": "",
    "hotkey": "<Super><Shift>t",
}


def path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "glean", "config.ini")


def load():
    values = dict(DEFAULTS)
    parser = configparser.ConfigParser()
    try:
        if parser.read(path()) and parser.has_section("glean"):
            for key in DEFAULTS:
                if parser.has_option("glean", key):
                    values[key] = parser.get("glean", key).strip()
    except (configparser.Error, OSError):
        pass
    return values


def write_default():
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        return target
    with open(target, "w") as fh:
        fh.write("[glean]\n")
        for key, value in sorted(DEFAULTS.items()):
            fh.write("%s = %s\n" % (key, value))
    return target


def truthy(value):
    return str(value).strip().lower() in ("1", "on", "yes", "true")
