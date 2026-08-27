"""Fail if anything under glean/ imports a package that pip would have to fetch.

`dependencies = []` in pyproject.toml is a promise, and it is the kind that
erodes one convenient import at a time -- a Pillow here for a resize, a
requests there.  glean is allowed exactly two non-standard-library names, both
of which arrive as system packages alongside GTK and neither of which should
ever be installed from PyPI:

  gi     PyGObject, from python3-gi
  cairo  pycairo, from python3-cairo

Anything else is a regression against the README.
"""

import ast
import pathlib
import sys

ALLOWED = {"gi", "cairo", "glean"}

if sys.version_info >= (3, 10):
    stdlib = set(sys.stdlib_module_names)
else:
    # sys.stdlib_module_names arrived in 3.10; before that, ask whether the
    # module resolves from somewhere other than site-packages.
    import importlib.util
    import sysconfig
    site = sysconfig.get_paths().get("purelib", "")
    stdlib = None

imported = set()
for path in sorted(pathlib.Path("glean").rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])


def is_from_pypi(name):
    if name in ALLOWED:
        return False
    if stdlib is not None:
        return name not in stdlib
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return False
    return bool(spec and spec.origin and site and spec.origin.startswith(site))


offenders = sorted(name for name in imported if is_from_pypi(name))
if offenders:
    print("glean/ imports packages that are not standard library or system GTK:")
    for name in offenders:
        print("  %s" % name)
    sys.exit(1)

allowed_used = sorted(imported & ALLOWED - {"glean"})
print("glean/ imports %d modules: standard library, plus %s"
      % (len(imported), ", ".join(allowed_used) or "nothing"))
