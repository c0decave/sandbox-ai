"""The `sandbox-ai-app` command of the PyPI package sandbox-ai-app.

`pip install sandbox-ai-app` installs the command line and the web interface (standard library only); the desktop
app's Qt comes with the extra: `pip install 'sandbox-ai-app[gui]'`. Without it this says so instead of a
traceback. Standard library only, Python 3.9 too: it runs before anything of Qt is known to be there.
"""
import importlib.util
import os
import sys

# Never import from the current directory (main.py's reason: the app is often started from a workspace).
sys.path[:] = [entry for entry in sys.path if entry not in ("", ".") and os.path.abspath(entry) != os.getcwd()]


def missing_pyside(find_spec=importlib.util.find_spec):
    """What to run when PySide6 is not installed with this app, else None."""
    try:
        if find_spec("PySide6") is not None:
            return None
    except (ImportError, ValueError):
        pass
    return ("sandbox-ai-app: the desktop app needs PySide6 (Qt), which this installation does not have.\n"
            "sandbox-ai-app: install the package with its extra: pip install 'sandbox-ai-app[gui]'\n"
            "sandbox-ai-app: (installed with pipx: pipx install --force 'sandbox-ai-app[gui]')\n"
            "sandbox-ai-app: the command line and the web interface work without it: sandbox-ai --help, "
            "sandbox-ai web --help")


def main(argv=None):
    problem = missing_pyside()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    from .main import main as app_main
    return app_main(argv)
