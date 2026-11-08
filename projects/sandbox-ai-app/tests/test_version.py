"""The app is released with sandbox-ai: its version is sandbox-ai's (it stood at 0.1.19 from sandbox-ai 0.1.66 on,
and About and the app's log named a version nobody had released). No Qt needed."""
from pathlib import Path
import re

import sandbox_ai_app

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT.parents[1] / "sandbox" / "agent_sandbox.py"  # sandbox-ai's own version, in this repository


def test_the_app_has_the_version_of_sandbox_ai():
    found = re.search(r'^__version__ = "([^"]+)"', LAUNCHER.read_text(), re.M)
    assert found, f"no __version__ in {LAUNCHER}"
    assert sandbox_ai_app.__version__ == found[1], (
        f"sandbox_ai_app.__version__ is {sandbox_ai_app.__version__}, sandbox-ai is {found[1]}: set "
        "sandbox_ai_app/__init__.py to sandbox-ai's version")
