"""Where captures go afterwards.

The last capture always lands in the runtime directory, which lives on tmpfs
and is emptied when the session ends, so `glean --last` works without leaving
a copy of your screen on disk overnight.  A durable history is off unless you
switch it on, because a tool that quietly archives every region you OCR is a
liability on a machine that handles anything sensitive.

The state file is the other kind of memory: numbers only — the region last
selected and the size the result window last had — so the next run can offer
them back.  It holds no text and no image, and `remember = off` in the
config stops it being written or read.
"""

import json
import os
import time

APP = "glean"
KEEP = 200


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or \
        os.path.join("/tmp", "glean-%d" % os.getuid())
    path = os.path.join(base, APP)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def base_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, APP)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def data_dir():
    path = os.path.join(base_dir(), "history")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def state_path():
    return os.path.join(base_dir(), "state.json")


def load_state():
    """The remembered numbers, as a dict. {} if there are none."""
    try:
        with open(state_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(**values):
    """Merge values into the state file. Returns its path, or None."""
    state = load_state()
    state.update(values)
    path = state_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        return None
    return path


def save_last(pixbuf, text):
    directory = runtime_dir()
    png = os.path.join(directory, "last.png")
    txt = os.path.join(directory, "last.txt")
    try:
        pixbuf.savev(png, "png", [], [])
        _write(txt, text)
        os.chmod(png, 0o600)
        os.chmod(txt, 0o600)
    except Exception:
        return None
    return png


def load_last():
    """(png path, text) for the previous capture, or (None, None)."""
    directory = runtime_dir()
    png = os.path.join(directory, "last.png")
    txt = os.path.join(directory, "last.txt")
    if not os.path.exists(png):
        return None, None
    text = ""
    try:
        with open(txt, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        pass
    return png, text


def archive(pixbuf, text, meta=None):
    """Add to the durable history. Returns the png path."""
    directory = data_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    png = os.path.join(directory, "%s.png" % stamp)
    n = 1
    while os.path.exists(png):
        png = os.path.join(directory, "%s-%d.png" % (stamp, n))
        n += 1
    pixbuf.savev(png, "png", [], [])
    os.chmod(png, 0o600)
    _write(png[:-4] + ".txt", text)
    os.chmod(png[:-4] + ".txt", 0o600)
    entry = dict(meta or {})
    entry.update({"time": stamp, "file": os.path.basename(png),
                  "chars": len(text)})
    with open(os.path.join(directory, "index.jsonl"), "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    prune(directory)
    return png


def prune(directory=None, keep=KEEP):
    directory = directory or data_dir()
    pngs = sorted(f for f in os.listdir(directory) if f.endswith(".png"))
    for name in pngs[:-keep] if len(pngs) > keep else []:
        for path in (os.path.join(directory, name),
                     os.path.join(directory, name[:-4] + ".txt")):
            try:
                os.unlink(path)
            except OSError:
                pass


def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8"))
