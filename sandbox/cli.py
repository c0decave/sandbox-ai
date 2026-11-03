"""Console entry points of the PyPI package.

Each command runs its module as a script in isolated mode (python -I), exactly
like the wrapper scripts of a checkout: the launcher loads runtime.py by path and mounts
it into the sandbox, so the modules must run from their files.
"""
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent


def _python(script, *prefix):
    path = str(HERE / script)
    os.execv(sys.executable, [sys.executable, "-I", path, *prefix, *sys.argv[1:]])


def claude():
    _python("agent_sandbox.py", "claude")


def codex():
    _python("agent_sandbox.py", "codex")


def opencode():
    _python("agent_sandbox.py", "opencode")


def session():
    _python("sessions.py")


def mcp():
    _python("mcp.py")


def wizard():
    _python("wizard.py")


def main():
    _python("main.py")


def build_image():
    bash = shutil.which("bash")
    if not bash:
        sys.exit("sandbox-build-image: bash is required")
    os.execv(bash, [bash, str(HERE / "build_image.sh"), *sys.argv[1:]])
